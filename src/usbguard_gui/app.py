"""Main application: system tray icon, event routing, and lifecycle."""

from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from usbguard_gui.dbus_client import USBGuardClient
from usbguard_gui.device import Device, DeviceTarget, PresenceEvent
from usbguard_gui.device_dialog import DeviceActionDialog
from usbguard_gui.device_list import DeviceListWindow
from usbguard_gui.screensaver import ScreensaverMonitor

log = logging.getLogger(__name__)

# SVG bundled in the source tree for dev-mode runs (make run).
_DEV_SVG = Path(__file__).parent.parent.parent / "rpm" / "usbguard_gui.svg"


def _app_icon() -> QIcon:
    """Return the application icon.

    Installed: picked up from the hicolor theme (put there by the RPM).
    Dev (make run): loaded directly from rpm/usbguard_gui.svg in the source tree.
    Fallback: generic drive-removable-media theme icon.
    """
    icon = QIcon.fromTheme("usbguard_gui")
    if not icon.isNull():
        return icon
    if _DEV_SVG.exists():
        return QIcon(str(_DEV_SVG))
    return QIcon.fromTheme("drive-removable-media")

# Seconds to wait before locking the screen for HID devices
HID_LOCK_DELAY = 3
# Seconds between reconnection attempts
RECONNECT_INTERVAL = 5


class USBGuardTrayApp:
    """System tray application for USBGuard."""

    def __init__(self, app: QApplication) -> None:
        self._app = app
        self._client = USBGuardClient()
        self._screensaver = ScreensaverMonitor()
        self._device_list_window: DeviceListWindow | None = None
        self._open_dialogs: dict[int, DeviceActionDialog] = {}
        self._screensaver_pending_devices: set[int] = set()

        # Reconnect timer
        self._reconnect_timer = QTimer()
        self._reconnect_timer.setInterval(RECONNECT_INTERVAL * 1000)
        self._reconnect_timer.timeout.connect(self._try_connect)

        self._setup_tray()
        self._connect_signals()

    def _setup_tray(self) -> None:
        self._tray = QSystemTrayIcon(_app_icon(), self._app)
        self._tray.setToolTip("USBGuard GUI — connecting...")

        menu = QMenu()
        self._action_show = QAction("Show Devices")
        self._action_show.triggered.connect(self._show_device_list)
        menu.addAction(self._action_show)

        menu.addSeparator()

        self._action_quit = QAction("Quit")
        self._action_quit.triggered.connect(self._quit)
        menu.addAction(self._action_quit)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _connect_signals(self) -> None:
        self._client.device_presence_changed.connect(self._on_device_presence_changed)
        self._client.device_policy_changed.connect(self._on_device_policy_changed)
        self._client.connection_changed.connect(self._on_connection_changed)
        self._screensaver.active_changed.connect(self._on_screensaver_changed)

    def start(self) -> None:
        """Initialize D-Bus connections and start the application."""
        self._screensaver.connect()
        if not self._client.connect():
            log.warning("USBGuard daemon not available, will retry...")
            self._reconnect_timer.start()

    def _try_connect(self) -> None:
        if self._client.connect():
            self._reconnect_timer.stop()

    def _on_connection_changed(self, connected: bool) -> None:
        if connected:
            self._tray.setToolTip("USBGuard GUI — connected")
            self._reconnect_timer.stop()
        else:
            self._tray.setToolTip("USBGuard GUI — disconnected (retrying...)")
            if not self._reconnect_timer.isActive():
                self._reconnect_timer.start()

    def _on_device_presence_changed(
        self, device_id: int, event: int, target: int, device_rule: str, attributes: dict
    ) -> None:
        log.debug(
            "DevicePresenceChanged: id=%d event=%d(%s) target=%d(%s) rule=%r attributes=%r",
            device_id,
            event,
            PresenceEvent(event).name if event in PresenceEvent._value2member_map_ else "?",
            target,
            DeviceTarget(target).name if target in DeviceTarget._value2member_map_ else "?",
            device_rule,
            attributes,
        )

        if event == PresenceEvent.REMOVE:
            # Close any open dialog for this device
            dialog = self._open_dialogs.pop(device_id, None)
            if dialog:
                dialog.close()
            return

        # Only react to new insertions — PRESENT fires for devices already
        # connected at daemon start, UPDATE fires on policy changes; neither
        # should spawn a dialog.
        if event != PresenceEvent.INSERT:
            log.debug("DevicePresenceChanged: id=%d skipped (not INSERT)", device_id)
            return

        # Use the target from the signal directly — more reliable than
        # parsing the rule string, which may not reflect the applied target.
        if target == int(DeviceTarget.ALLOW):
            log.debug("DevicePresenceChanged: id=%d skipped (target=ALLOW)", device_id)
            return

        # If screensaver is active, defer
        if self._screensaver.active:
            self._screensaver_pending_devices.add(device_id)
            log.info("Device %d inserted while screen locked, deferring", device_id)
            return

        device = Device.from_dbus(device_id, device_rule)

        # HID security: lock screen first, then allow after authentication
        if device.is_hid():
            self._handle_hid_device(device)
            return

        self._show_device_dialog(device)

    def _on_device_policy_changed(
        self, device_id: int, target_old: int, target_new: int, device_rule: str, rule_id: int, attributes: dict
    ) -> None:
        log.debug(
            "DevicePolicyChanged: id=%d %s->%s",
            device_id,
            DeviceTarget(target_old).name if target_old in DeviceTarget._value2member_map_ else target_old,
            DeviceTarget(target_new).name if target_new in DeviceTarget._value2member_map_ else target_new,
        )
        if target_new == int(DeviceTarget.ALLOW):
            # Device was allowed by a permanent rule after the initial block —
            # dismiss any dialog that opened on the INSERT event.
            dialog = self._open_dialogs.pop(device_id, None)
            if dialog:
                log.debug("DevicePolicyChanged: id=%d closing dialog (device now allowed)", device_id)
                dialog.close()
            self._screensaver_pending_devices.discard(device_id)

    def _handle_hid_device(self, device: Device) -> None:
        """Handle HID device insertion: warn user and lock screen."""
        self._tray.showMessage(
            "New keyboard/HID attached",
            "Locking screen. Enter your password to activate the device. "
            "If you did not attach a keyboard, check for malicious devices.",
            QSystemTrayIcon.MessageIcon.Warning,
            5000,
        )
        # Allow the HID device temporarily after a delay so the user can
        # unlock the screen with it, then lock.
        QTimer.singleShot(
            HID_LOCK_DELAY * 1000,
            lambda: self._allow_hid_and_lock(device),
        )

    def _allow_hid_and_lock(self, device: Device) -> None:
        self._client.apply_device_policy(device.number, DeviceTarget.ALLOW, permanent=False)
        self._screensaver.lock()

    def _on_screensaver_changed(self, active: bool) -> None:
        if active or not self._screensaver_pending_devices:
            return

        # Screen unlocked — show summary of devices inserted while locked
        pending_ids = list(self._screensaver_pending_devices)
        self._screensaver_pending_devices.clear()
        devices = self._client.list_devices()
        pending_devices = [d for d in devices if d.number in pending_ids and not d.is_allowed()]

        if not pending_devices:
            return

        count = len(pending_devices)
        names = "\n".join(f"  - {d.name or d.id} ({d.class_description_string()})" for d in pending_devices)
        self._tray.showMessage(
            f"{count} USB device(s) connected during absence",
            names,
            QSystemTrayIcon.MessageIcon.Information,
            10000,
        )

        # Show dialogs for each pending device
        for device in pending_devices:
            self._show_device_dialog(device)

    def _show_device_dialog(self, device: Device) -> None:
        # Don't open duplicate dialogs
        if device.number in self._open_dialogs:
            return

        # Tray notification
        self._tray.showMessage(
            "New USB device inserted",
            f"{device.name or '(unknown)'}\n{device.class_description_string()}",
            QSystemTrayIcon.MessageIcon.Information,
            5000,
        )

        dialog = DeviceActionDialog(device)
        self._open_dialogs[device.number] = dialog

        def on_finished(result: int) -> None:
            self._open_dialogs.pop(device.number, None)
            if dialog.result_target is not None:
                self._client.apply_device_policy(device.number, dialog.result_target, dialog.permanent)

        dialog.finished.connect(on_finished)
        dialog.show()

    def _show_device_list(self) -> None:
        if self._device_list_window is None:
            self._device_list_window = DeviceListWindow(self._client)
        self._device_list_window.show()
        self._device_list_window.raise_()
        self._device_list_window.activateWindow()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_device_list()

    def _quit(self) -> None:
        if self._device_list_window:
            self._device_list_window.close()
        for dialog in list(self._open_dialogs.values()):
            dialog.close()
        self._tray.hide()
        self._app.quit()


def main() -> None:
    """Entry point for the usbguard_gui application."""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Allow Ctrl+C to work
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    app = QApplication(sys.argv)
    app.setApplicationName("usbguard_gui")
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, "USBGuard GUI", "System tray is not available.")
        sys.exit(1)

    tray_app = USBGuardTrayApp(app)
    tray_app.start()

    sys.exit(app.exec())
