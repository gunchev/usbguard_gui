"""Main window showing a table of all connected USB devices."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QAbstractTableModel, QByteArray, QModelIndex, QPoint, QSettings, QSortFilterProxyModel, Qt, \
    QTimer
from PyQt6.QtGui import QAction, QCloseEvent, QColor, QShowEvent
from PyQt6.QtWidgets import QAbstractItemView, QHeaderView, QMainWindow, QMenu, QMessageBox, QTableView, QToolBar, \
    QVBoxLayout, QWidget

from usbguard_gui.device import Device, DeviceTarget, Persistence, rule_is_broader_than_device, rule_matches_device

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from usbguard_gui.dbus_client import USBGuardClient
    from usbguard_gui.screensaver import ScreensaverMonitor

COLUMNS = ["#", "Status", "Persistence", "USB ID", "Name", "Serial", "Port", "Interfaces", "Type",
           "Connection"]

# Colour carries the live target; the shade carries whether it is durable.  A device
# sitting on the implicit floor and one with an explicit permanent rule must not look
# the same -- they differ in exactly the way the user is being asked to care about.
_COLOR_ALLOW_PERMANENT = QColor(0, 100, 0)
_COLOR_ALLOW_TEMPORARY = QColor(0, 80, 120)
_COLOR_BLOCK_PERMANENT = QColor(130, 30, 30)
_COLOR_BLOCK_TEMPORARY = QColor(125, 85, 0)
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
        self._permanent_rules: list[str] = []
        self._persistence: list[str] = []
        self._row_colors: list[QColor | None] = []

    def set_devices(self, devices: list[Device], permanent_rules: list[str]) -> None:
        """Replace the rows.

        `permanent_rules` is the daemon's ruleset in rule order, which is the order
        the daemon applies them.  Persistence is resolved once here rather than per
        cell: a refresh touches every cell of every row, and re-matching the whole
        ruleset for each one is work we only need to do once per device.  The row
        colour is derived from the same answer for the same reason -- Qt asks for
        the background and the foreground of every cell separately, so anything
        left un-cached here is paid for twice per cell per repaint.
        """
        self.beginResetModel()
        self._devices = list(devices)
        self._permanent_rules = list(permanent_rules)
        self._persistence = [self._resolve_persistence(d) for d in self._devices]
        self._row_colors = [self._resolve_bg_color(device, persistence)
                            for device, persistence in zip(self._devices, self._persistence, strict=True)]
        self.endResetModel()

    def _resolve_persistence(self, device: Device) -> str:
        """`Permanent allow` / `Permanent allow (wildcard)` / `Temporary` / `Unknown`.

        The first rule that matches wins, matching the daemon.  A rule we cannot
        read above the match yields `Unknown` rather than a confident wrong answer:
        the daemon may have stopped there, and telling the user the device is
        temporary in that case would be a guess dressed up as a fact.

        `(wildcard)` marks a rule that covers this device without naming it --
        `allow id 2109:2817` governs every hub of that model.  It matters
        because `Once` cannot clear one: the user clicked a temporary action and
        the device is in fact permanently allowed, so the row has to say which
        kind of permanent it is.
        """
        for rule in self._permanent_rules:
            verdict = rule_matches_device(rule, device)
            if verdict is True:
                verb = rule.strip().split(None, 1)[0].lower() if rule.strip() else "rule"
                if rule_is_broader_than_device(rule, device):
                    return f"Permanent {verb} (wildcard)"
                return f"Permanent {verb}"
            if verdict is None:
                return "Unknown"
        return "Temporary"

    def persistence_at(self, row: int) -> str:
        if 0 <= row < len(self._persistence):
            return self._persistence[row]
        return "Unknown"

    def device_at(self, row: int) -> Device | None:
        if 0 <= row < len(self._devices):
            return self._devices[row]
        return None

    def _bg_color(self, row: int) -> QColor | None:
        """The row's colour, precomputed in `set_devices`.  A lookup, never a match."""
        if 0 <= row < len(self._row_colors):
            return self._row_colors[row]
        return None

    @staticmethod
    def _resolve_bg_color(device: Device, persistence: str) -> QColor | None:
        rule = device.rule.lower()
        permanent = persistence.startswith("Permanent")
        if rule == "allow":
            return _COLOR_ALLOW_PERMANENT if permanent else _COLOR_ALLOW_TEMPORARY
        if rule == "block":
            return _COLOR_BLOCK_PERMANENT if permanent else _COLOR_BLOCK_TEMPORARY
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
            return self._display_data(device, col, index.row())
        if role == Qt.ItemDataRole.BackgroundRole:
            return self._bg_color(index.row())
        if role == Qt.ItemDataRole.ForegroundRole and self._bg_color(index.row()) is not None:
            return QColor(Qt.GlobalColor.white)
        return None

    def _display_data(self, device: Device, col: int, row: int) -> str:
        if col == 0:
            return str(device.number)
        if col == 1:
            return device.rule.capitalize()
        if col == 2:
            return self.persistence_at(row)
        if col == 3:
            return device.id
        if col == 4:
            return device.name
        if col == 5:
            return device.serial
        if col == 6:
            return device.via_port
        if col == 7:
            return " ".join(device.with_interface)
        if col == 8:
            return device.class_description_string()
        if col == 9:
            return device.with_connect_type
        return ""


class DeviceListWindow(QMainWindow):
    """Window displaying all USB devices with context-menu actions."""

    def __init__(self, client: USBGuardClient, parent: QWidget | None = None,
                 screensaver: ScreensaverMonitor | None = None,
                 settings: QSettings | None = None) -> None:
        super().__init__(parent)
        self._client = client
        self._screensaver = screensaver
        # Window-geometry store, injected so tests can point it at a temp file
        # instead of the developer's ~/.config/usbguard_gui/device_list.conf
        # (which test runs would otherwise overwrite with offscreen geometry).
        self._settings = settings if settings is not None else QSettings("usbguard_gui", "device_list")
        self._refresh_pending = False
        self._pending_devices: list[Device] = []
        self.setWindowTitle("USBGuard — Devices")
        self.resize(900, 500)

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
        hh = self._view.horizontalHeader()
        assert hh is not None
        hh.setStretchLastSection(True)
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setSectionsMovable(True)
        hh.sectionMoved.connect(self._on_section_moved)
        vh = self._view.verticalHeader()
        assert vh is not None
        vh.setVisible(False)

        saved_geometry = self._settings.value("geometry")
        if isinstance(saved_geometry, QByteArray) and not saved_geometry.isEmpty():
            self.restoreGeometry(saved_geometry)
        saved_state = self._settings.value("header_state")
        if isinstance(saved_state, QByteArray) and not saved_state.isEmpty() and hh.restoreState(saved_state):
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
        # Connect result signals once — previously _do_refresh() added a fresh
        # lambda per call, leaking one connection per refresh.
        self._client.list_devices_result.connect(self._on_list_devices_result)
        self._client.list_rules_result.connect(self._on_list_rules_result)

    def _schedule_refresh(self) -> None:
        self._refresh_pending = True
        self._refresh_timer.start()

    def _request_refresh(self) -> None:
        self._refresh_pending = True
        self._do_refresh()

    def _do_refresh(self) -> None:
        self._refresh_pending = True
        self._pending_devices = []
        self._client.list_devices()

    def _on_list_devices_result(self, devices: list[Device]) -> None:
        if not self._refresh_pending:
            return
        self._pending_devices = devices
        self._client.list_rules()

    def _on_list_rules_result(self, rules: list[tuple[int, str]]) -> None:
        if self._refresh_pending:
            self._refresh_pending = False
            self._model.set_devices(self._pending_devices, _permanent_rule_strings(rules))
            if not self._columns_sized:
                self._view.resizeColumnsToContents()
                self._columns_sized = True

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._request_refresh()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._settings.setValue("geometry", self.saveGeometry())
        hh = self._view.horizontalHeader()
        assert hh is not None
        self._settings.setValue("header_state", hh.saveState())
        super().closeEvent(event)

    def _selected_device(self) -> Device | None:
        sm = self._view.selectionModel()
        assert sm is not None
        indexes = sm.selectedRows()
        if not indexes:
            return None
        source_index = self._proxy.mapToSource(indexes[0])
        return self._model.device_at(source_index.row())

    def _on_section_moved(self, logical: int, old_visual: int, new_visual: int) -> None:
        """Keep the '#' column (logical 0) anchored at visual position 0."""
        header = self._view.horizontalHeader()
        assert header is not None
        if header.visualIndex(0) != 0:
            header.blockSignals(True)
            header.moveSection(header.visualIndex(0), 0)
            header.blockSignals(False)

    def _show_context_menu(self, pos: QPoint) -> None:
        device = self._selected_device()
        if not device:
            return

        menu = QMenu(self)
        for index, (label, target, persistence) in enumerate(self._menu_actions(device)):
            if index == 2:
                menu.addSeparator()
            menu.addAction(label, lambda _checked=False, t=target, p=persistence: self._apply(device, t, p))
        vp = self._view.viewport()
        assert vp is not None
        menu.exec(vp.mapToGlobal(pos))

    #: The action set, shared verbatim with the tray dialog.
    _ACTION_SET: tuple[tuple[str, DeviceTarget, Persistence], ...] = (
        ("Allow Always", DeviceTarget.ALLOW, Persistence.ALWAYS),
        ("Allow Once", DeviceTarget.ALLOW, Persistence.ONCE),
        ("Block Once", DeviceTarget.BLOCK, Persistence.ONCE),
        ("Block Always", DeviceTarget.BLOCK, Persistence.ALWAYS),
    )

    def _menu_actions(self, device: Device) -> list[tuple[str, DeviceTarget, Persistence]]:
        """The actions offered for `device`, as (label, target, persistence).

        Returned as data rather than built inline so the menu contract can be
        checked against the dialog's without popping a QMenu.
        """
        return list(self._ACTION_SET)

    def _apply(self, device: Device, target: DeviceTarget, persistence: Persistence) -> None:
        # Hand the decision to the client and let it own persistence.  `Always`
        # upserts the device's permanent rule; `Once` deletes it.  Both are
        # keyed on device identity (`_rule_identity`), which is what makes the
        # deletion safe: it can only reach a rule that names this device, never
        # a hand-written class rule such as `reject with-interface all-of {}`.
        #
        # The comment here used to say the GUI must never remove a rule because
        # permanent and temporary rules are indistinguishable from the rule
        # string.  That predates identity-keyed dedup and is now the wrong
        # invariant: `Once` *must* remove, or the old rule survives the click
        # and silently re-asserts at the next reboot -- the exact divergence
        # this action set exists to eliminate.
        if not self._client.connected:
            log.warning("Action %s on device %d not applied: USBGuard daemon not connected", target.name, device.number)
            QMessageBox.warning(
                self,
                "USBGuard GUI",
                "The USBGuard daemon is not connected.\nThe action was not applied — "
                "try again once the connection is restored.",
            )
            return
        # All allow/deny functionality is disabled while screen locking is
        # unavailable: without the ability to lock first, allowing a
        # keyboard would hand an attached-device attacker an unlocked
        # session — exactly what this app exists to prevent.
        if self._screensaver is not None and not self._screensaver.connected:
            log.warning(
                "Action %s on device %d not applied: screen locking is unavailable",
                target.name,
                device.number,
            )
            QMessageBox.warning(
                self,
                "USBGuard GUI",
                "Screen locking is unavailable — device actions are disabled.\n"
                "Devices remain blocked by USBGuard's policy.",
            )
            return
        # `Once` needs the raw rule too, not just `Always`: the client keys the
        # deletion on device identity, and that identity comes from this string.
        # Withholding it would make `Once` a silent no-op from the UI.
        self._client.apply_device_policy(device.number, target, persistence,
                                         device.raw_rule if persistence is not Persistence.UNCHANGED else None)
        self._request_refresh()


def _permanent_rule_strings(rules: list[tuple[int, str]]) -> list[str]:
    """The permanent ruleset in rule order, which is the order the daemon applies it."""
    return [rule_str for _, rule_str in rules]
