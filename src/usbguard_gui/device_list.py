"""Main window showing a table of all connected USB devices."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QAbstractTableModel, QByteArray, QModelIndex, QPoint, QSettings, QSortFilterProxyModel, Qt, \
    QTimer
from PyQt6.QtGui import QAction, QCloseEvent, QColor
from PyQt6.QtWidgets import QAbstractItemView, QHeaderView, QMainWindow, QMenu, QTableView, QToolBar, QVBoxLayout, \
    QWidget

from usbguard_gui.device import Device, DeviceTarget, parse_device_rule

if TYPE_CHECKING:
    from usbguard_gui.dbus_client import USBGuardClient

COLUMNS = ["#", "Status", "USB ID", "Name", "Serial", "Port", "Interfaces", "Type", "Connection"]

_COLOR_ALLOW_PERMANENT = QColor(0, 80, 0)
_COLOR_ALLOW_TEMPORARY = QColor(0, 50, 100)
_COLOR_BLOCK = QColor(80, 40, 0)
_COLOR_REJECT = QColor(80, 0, 0)


class DeviceSortProxyModel(QSortFilterProxyModel):
    """QSortFilterProxyModel with numeric comparison for the '#' column."""

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        if left.column() == 0:
            try:
                return int(left.data() or 0) < int(right.data() or 0)
            except (ValueError, TypeError):
                pass
        return super().lessThan(left, right)


class DeviceTableModel(QAbstractTableModel):
    """Table model backed by a list of Device objects."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._devices: list[Device] = []
        self._permanent_allow_hashes: set[str] = set()

    def set_devices(self, devices: list[Device], permanent_allow_hashes: set[str]) -> None:
        self.beginResetModel()
        self._devices = list(devices)
        self._permanent_allow_hashes = permanent_allow_hashes
        self.endResetModel()

    def device_at(self, row: int) -> Device | None:
        if 0 <= row < len(self._devices):
            return self._devices[row]
        return None

    def _bg_color(self, device: Device) -> QColor | None:
        rule = device.rule.lower()
        if rule == "allow":
            if device.hash and device.hash in self._permanent_allow_hashes:
                return _COLOR_ALLOW_PERMANENT
            return _COLOR_ALLOW_TEMPORARY
        if rule == "block":
            return _COLOR_BLOCK
        if rule == "reject":
            return _COLOR_REJECT
        return None

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        return len(self._devices)

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        return len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        device = self._devices[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_data(device, col)
        if role == Qt.ItemDataRole.BackgroundRole:
            return self._bg_color(device)
        if role == Qt.ItemDataRole.ForegroundRole and self._bg_color(device) is not None:
            return QColor(Qt.GlobalColor.white)
        return None

    @staticmethod
    def _display_data(device: Device, col: int) -> str:
        if col == 0:
            return str(device.number)
        if col == 1:
            return device.rule.capitalize()
        if col == 2:
            return device.id
        if col == 3:
            return device.name
        if col == 4:
            return device.serial
        if col == 5:
            return device.via_port
        if col == 6:
            return " ".join(device.with_interface)
        if col == 7:
            return device.class_description_string()
        if col == 8:
            return device.with_connect_type
        return ""


class DeviceListWindow(QMainWindow):
    """Window displaying all USB devices with context-menu actions."""

    def __init__(self, client: USBGuardClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._client = client
        self._pending_apply: tuple[Device, DeviceTarget, bool] | None = None
        self._refresh_pending = False
        self._pending_devices: list[Device] = []
        self._refresh_id = 0
        self.setWindowTitle("USBGuard — Devices")
        self.setMinimumSize(900, 500)

        self._model = DeviceTableModel(self)
        self._proxy = DeviceSortProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._columns_sized = False

        self._view = QTableView()
        self._view.setModel(self._proxy)
        self._view.setSortingEnabled(True)
        self._view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._show_context_menu)
        self._view.horizontalHeader().setStretchLastSection(True)
        self._view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._view.horizontalHeader().setSectionsMovable(True)
        self._view.horizontalHeader().sectionMoved.connect(self._on_section_moved)
        self._view.verticalHeader().setVisible(False)

        self._settings = QSettings("usbguard_gui", "DeviceList")
        saved_geometry = self._settings.value("geometry")
        if isinstance(saved_geometry, QByteArray) and not saved_geometry.isEmpty():
            self.restoreGeometry(saved_geometry)
        saved_state = self._settings.value("header_state")
        header = self._view.horizontalHeader()
        if isinstance(saved_state, QByteArray) and not saved_state.isEmpty() and header.restoreState(saved_state):
            self._columns_sized = True

        toolbar = QToolBar("Actions")
        toolbar.setMovable(False)
        refresh_action = QAction("Refresh", self)
        refresh_action.triggered.connect(self._request_refresh)
        toolbar.addAction(refresh_action)
        self.addToolBar(toolbar)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)
        self.setCentralWidget(central)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(500)
        self._refresh_timer.timeout.connect(self._do_refresh)

        self._client.device_presence_changed.connect(self._schedule_refresh)
        self._client.device_policy_changed.connect(self._schedule_refresh)

    def _schedule_refresh(self) -> None:
        self._refresh_pending = True
        self._refresh_timer.start()

    def _request_refresh(self) -> None:
        self._refresh_pending = True
        self._do_refresh()

    def _do_refresh(self) -> None:
        self._refresh_id += 1
        refresh_id = self._refresh_id
        self._refresh_pending = True
        self._pending_devices = []
        self._client.list_devices_result.connect(lambda d: self._on_list_devices_result(d, refresh_id))
        self._client.list_rules_result.connect(lambda r: self._on_list_rules_result(r, refresh_id))
        self._client.list_devices()

    def _on_list_devices_result(self, devices: list[Device], refresh_id: int) -> None:
        if refresh_id != self._refresh_id:
            return
        self._pending_devices = devices
        self._client.list_rules()

    def _on_list_rules_result(self, rules: list[tuple[int, str]], refresh_id: int) -> None:
        if refresh_id != self._refresh_id:
            return
        if self._pending_apply:
            device, target, permanent = self._pending_apply
            self._pending_apply = None
            self._do_apply_with_rules(device, target, permanent, rules)
        elif self._refresh_pending:
            self._refresh_pending = False
            self._model.set_devices(self._pending_devices, _permanent_allow_hashes(rules))
            if not self._columns_sized:
                self._view.resizeColumnsToContents()
                self._columns_sized = True

    def showEvent(self, event: QPoint) -> None:
        super().showEvent(event)
        self._request_refresh()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._settings.setValue("geometry", self.saveGeometry())
        self._settings.setValue("header_state", self._view.horizontalHeader().saveState())
        super().closeEvent(event)

    def _selected_device(self) -> Device | None:
        indexes = self._view.selectionModel().selectedRows()
        if not indexes:
            return None
        source_index = self._proxy.mapToSource(indexes[0])
        return self._model.device_at(source_index.row())

    def _on_section_moved(self, logical: int, old_visual: int, new_visual: int) -> None:
        """Keep the '#' column (logical 0) anchored at visual position 0."""
        header = self._view.horizontalHeader()
        if header.visualIndex(0) != 0:
            header.blockSignals(True)
            header.moveSection(header.visualIndex(0), 0)
            header.blockSignals(False)

    def _show_context_menu(self, pos: QPoint) -> None:
        device = self._selected_device()
        if not device:
            return

        menu = QMenu(self)
        menu.addAction("Allow (Permanent)", lambda: self._apply(device, DeviceTarget.ALLOW, permanent=True))
        menu.addAction("Allow (Temporary)", lambda: self._apply(device, DeviceTarget.ALLOW, permanent=False))
        menu.addSeparator()
        menu.addAction("Block", lambda: self._apply(device, DeviceTarget.BLOCK, permanent=False))
        menu.addAction("Reject", lambda: self._apply(device, DeviceTarget.REJECT, permanent=False))
        menu.exec(self._view.viewport().mapToGlobal(pos))

    def _apply(self, device: Device, target: DeviceTarget, permanent: bool) -> None:
        self._pending_apply = (device, target, permanent)
        self._client.list_rules()

    def _do_apply_with_rules(
        self, device: Device, target: DeviceTarget, permanent: bool, rules: list[tuple[int, str]]
    ) -> None:
        if target == DeviceTarget.ALLOW and not permanent and device.hash:
            for rule_id, rule_str in rules:
                parsed = parse_device_rule(rule_str)
                if parsed["rule"] == "allow" and parsed["hash"] == device.hash:
                    self._client.remove_rule(rule_id)
        self._client.apply_device_policy(device.number, target, permanent)
        self._request_refresh()


def _permanent_allow_hashes(rules: list[tuple[int, str]]) -> set[str]:
    """Return the set of device hashes that have a permanent allow rule."""
    hashes: set[str] = set()
    for _, rule_str in rules:
        parsed = parse_device_rule(rule_str)
        if parsed["rule"] == "allow" and parsed["hash"]:
            hashes.add(parsed["hash"])
    return hashes
