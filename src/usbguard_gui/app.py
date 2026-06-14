"""Main application: system tray icon, event routing, and lifecycle."""

from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path

from PyQt6.QtCore import QLockFile, QStandardPaths, QTimer
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from usbguard_gui.dbus_client import USBGuardClient
from usbguard_gui.device import Device, DeviceTarget, PresenceEvent, parse_device_rule
from usbguard_gui.device_dialog import DeviceActionDialog
from usbguard_gui.device_list import DeviceListWindow
from usbguard_gui.screensaver import ScreensaverMonitor
from usbguard_gui.settings import Settings

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


def _enum_name(enum: type, value: int, fallback: str = "?") -> str:
    """Return the name of an enum member by value, or a fallback string."""
    try:
        return enum(value).name
    except ValueError:
        return fallback


# Base seconds between reconnection attempts (will be exponentially increased)
RECONNECT_BASE_INTERVAL = 5
# Maximum seconds between reconnection attempts
RECONNECT_MAX_INTERVAL = 60
# Milliseconds between the HID warning notification and the actual screen lock.
# The device stays blocked by USBGuard's default policy during this window (it is
# only allowed after the screen has locked, in _on_screensaver_active_changed),
# so this delay does not reopen the keystroke-injection window — it just gives
# the tray notification time to appear before the screen blanks.
HID_LOCK_NOTIFY_DELAY_MS = 5000


class USBGuardTrayApp:
    """System tray application for USBGuard."""

    def __init__(self, app: QApplication) -> None:
        self._app = app
        self._client = USBGuardClient()
        self._screensaver = ScreensaverMonitor()
        self._settings = Settings()
        self._device_list_window: DeviceListWindow | None = None
        self._open_dialogs: dict[int, DeviceActionDialog] = {}
        self._screensaver_pending_devices: set[int] = set()
        self._hid_pending_devices: set[int] = set()
        self._screensaver_pending_ids: list[int] | None = None
        self._permanent_allow_hashes: set[str] = set()

        # Reconnect timer with exponential backoff
        self._reconnect_timer = QTimer()
        self._reconnect_timer.setInterval(RECONNECT_BASE_INTERVAL * 1000)
        self._reconnect_timer.timeout.connect(self._try_connect)
        self._reconnect_attempts = 0

        # Deferred screen lock for HID inserts. Single-shot and cancellable so
        # that unplugging the triggering device before it fires aborts the lock
        # (see _on_device_presence_changed REMOVE handling).
        self._hid_lock_timer = QTimer()
        self._hid_lock_timer.setSingleShot(True)
        self._hid_lock_timer.timeout.connect(self._lock_for_pending_hid)

        self._setup_tray()
        self._connect_signals()
        self._connect_client_signals()

    def _setup_tray(self) -> None:
        self._tray = QSystemTrayIcon(_app_icon(), self._app)
        self._tray.setToolTip("USBGuard GUI — connecting...")

        menu = QMenu()
        self._action_show = QAction("Show Devices")
        self._action_show.triggered.connect(self._show_device_list)
        menu.addAction(self._action_show)

        menu.addSeparator()

        self._action_disable_hid = QAction("Disable special HID device treatment")
        self._action_disable_hid.setCheckable(True)
        self._action_disable_hid.setChecked(self._settings.disable_hid_treatment())
        self._action_disable_hid.toggled.connect(self._on_disable_hid_toggled)
        menu.addAction(self._action_disable_hid)

        menu.addSeparator()

        self._action_quit = QAction("Quit")
        self._action_quit.triggered.connect(self._quit)
        menu.addAction(self._action_quit)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_disable_hid_toggled(self, checked: bool) -> None:
        self._settings.set_disable_hid_treatment(checked)

    def _connect_signals(self) -> None:
        self._client.device_presence_changed.connect(self._on_device_presence_changed)
        self._client.device_policy_changed.connect(self._on_device_policy_changed)
        self._client.connection_changed.connect(self._on_connection_changed)
        self._screensaver.active_changed.connect(self._on_screensaver_changed)
        self._screensaver.active_changed.connect(self._on_screensaver_active_changed)

    def _connect_client_signals(self) -> None:
        self._client.list_devices_result.connect(self._on_list_devices_result)
        self._client.list_rules_result.connect(self._on_list_rules_result)

    def _on_list_rules_result(self, rules: list[tuple[int, str]]) -> None:
        self._permanent_allow_hashes.clear()
        for _, rule_str in rules:
            parsed = parse_device_rule(rule_str)
            if parsed["rule"] == "allow" and parsed["hash"]:
                self._permanent_allow_hashes.add(str(parsed["hash"]))

    def _on_list_devices_result(self, devices: list[Device]) -> None:
        if self._hid_pending_devices:
            pending_ids = self._hid_pending_devices
            self._hid_pending_devices = set()
            for device_number in pending_ids:
                if any(d.number == device_number for d in devices):
                    self._client.apply_device_policy(device_number, DeviceTarget.ALLOW, permanent=False)
        elif self._screensaver_pending_ids is not None:
            pending_ids = self._screensaver_pending_ids
            self._screensaver_pending_ids = None
            pending_devices = [d for d in devices if d.number in pending_ids and not d.is_allowed()]

            if not pending_devices:
                return

            count = len(pending_devices)
            names = "\n".join(f"  - {d.name or d.id} ({d.class_description_string()})" for d in pending_devices)
            info = QSystemTrayIcon.MessageIcon.Information
            self._tray.showMessage(f"{count} USB device(s) connected during absence", names, info, 10000)

            for device in pending_devices:
                self._show_device_dialog(device)

    def start(self) -> None:
        """Initialize D-Bus connections and start the application."""
        self._screensaver.connect()
        if not self._client.connect():
            log.warning("USBGuard daemon not available, will retry...")
            self._reconnect_timer.start()

    def _try_connect(self) -> None:
        if self._client.connect():
            self._reconnect_timer.stop()
            self._reconnect_attempts = 0  # Reset on successful connection
        else:
            # Exponential backoff: increase interval up to maximum
            self._reconnect_attempts += 1
            backoff = min(RECONNECT_BASE_INTERVAL * (2 ** (self._reconnect_attempts - 1)), RECONNECT_MAX_INTERVAL)
            self._reconnect_timer.setInterval(backoff * 1000)
            log.debug("Connection attempt %d failed, retrying in %d seconds", self._reconnect_attempts, backoff)

    def _on_connection_changed(self, connected: bool) -> None:
        if connected:
            self._tray.setToolTip("USBGuard GUI — connected")
            self._reconnect_timer.stop()
            self._reconnect_attempts = 0  # Reset on successful connection
            self._client.list_rules()
        else:
            self._tray.setToolTip("USBGuard GUI — disconnected (retrying...)")
            if not self._reconnect_timer.isActive():
                self._reconnect_timer.start()

    def _on_device_presence_changed(
        self, device_id: int, event: int, target: int, device_rule: str, attributes: dict
    ) -> None:
        try:
            log.debug(
                "DevicePresenceChanged: id=%d event=%d(%s) target=%d(%s) rule=%r attributes=%r",
                device_id,
                event,
                _enum_name(PresenceEvent, event),
                target,
                _enum_name(DeviceTarget, target),
                device_rule,
                attributes,
            )

            if event == PresenceEvent.REMOVE:
                # Close any open dialog for this device
                dialog = self._open_dialogs.pop(device_id, None)
                if dialog:
                    dialog.close()
                # Drop the device from any pending set — it is gone, so it must
                # neither be auto-allowed on lock nor prompted for on unlock.
                self._hid_pending_devices.discard(device_id)
                self._screensaver_pending_devices.discard(device_id)
                # If this was the last HID device awaiting the deferred lock,
                # cancel the lock: the user unplugged the device before it fired.
                if not self._hid_pending_devices and self._hid_lock_timer.isActive():
                    log.info("HID device %d removed before lock — cancelling scheduled lock", device_id)
                    self._hid_lock_timer.stop()
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

            device = Device.from_dbus(device_id, device_rule)

            # HID devices are handled before the screensaver check so that a
            # newly-attached keyboard can be used to unlock the screen.
            # has_hid_interface() catches composite devices (e.g. HID + MSC)
            # too — any HID interface can send keystrokes.
            #
            # The special-treatment path (auto-allow then lock) is skipped
            # entirely when the user has disabled it in settings OR when
            # screen locking is currently inhibited by a logind idle/block
            # inhibitor (dnf/rpm transaction, GNOME/KDE "Prevent screen
            # lock", systemd-inhibit --what=idle, ...). Auto-allowing a HID
            # device while lock is inhibited would hand an attached-keyboard
            # attacker typed input with no password prompt to gate it.
            is_hid = device.has_hid_interface()
            hid_treatment_enabled = not self._settings.disable_hid_treatment()
            lock_inhibited = self._screensaver.inhibited
            hid_special_treatment = is_hid and hid_treatment_enabled and not lock_inhibited
            if hid_special_treatment:
                if self._screensaver.active:
                    log.info(
                        "HID device %d inserted while screen locked, allowing temporarily so it can unlock",
                        device_id,
                    )
                    self._client.apply_device_policy(device_id, DeviceTarget.ALLOW, permanent=False)
                    return
                # Skip HID treatment for devices that are already whitelisted
                # (matching a permanent allow rule from the daemon's policy).
                if device.hash and device.hash in self._permanent_allow_hashes:
                    log.debug("DevicePresenceChanged: id=%d skipped (permanent allow hash match)", device_id)
                    return
                self._hid_pending_devices.add(device_id)
                self._tray.showMessage(
                    "New keyboard/HID attached",
                    "Locking screen. Enter your password to activate the device. "
                    "If you did not attach a keyboard, check for malicious devices.",
                    QSystemTrayIcon.MessageIcon.Warning,
                    5000,
                )
                self._hid_lock_timer.start(HID_LOCK_NOTIFY_DELAY_MS)
                return
            elif is_hid and hid_treatment_enabled:
                # lock_inhibited is True — fall through to normal prompt path
                log.info(
                    "HID device %d inserted while screen locking is inhibited — "
                    "falling back to prompt (will not auto-allow)",
                    device_id,
                )

            # Non-HID device: defer while screen is locked, otherwise prompt.
            if self._screensaver.active:
                self._screensaver_pending_devices.add(device_id)
                log.info("Device %d inserted while screen locked, deferring", device_id)
                return

            self._show_device_dialog(device)
        except Exception as e:
            log.exception("Error in _on_device_presence_changed for device %d: %s", device_id, e)

    def _on_device_policy_changed(
        self, device_id: int, target_old: int, target_new: int, device_rule: str, rule_id: int, attributes: dict
    ) -> None:
        try:
            log.debug(
                "DevicePolicyChanged: id=%d %s->%s",
                device_id,
                _enum_name(DeviceTarget, target_old, fallback=str(target_old)),
                _enum_name(DeviceTarget, target_new, fallback=str(target_new)),
            )
            if target_new == int(DeviceTarget.ALLOW):
                # Device was allowed by a permanent rule after the initial block —
                # dismiss any dialog that opened on the INSERT event.
                dialog = self._open_dialogs.pop(device_id, None)
                if dialog:
                    log.debug("DevicePolicyChanged: id=%d closing dialog (device now allowed)", device_id)
                    dialog.close()
                self._screensaver_pending_devices.discard(device_id)
                self._hid_pending_devices.discard(device_id)
                # Seed the permanent-allow cache so future re-insertions skip
                # HID treatment.
                if rule_id > 0:
                    d = Device.from_dbus(device_id, device_rule)
                    if d.hash:
                        self._permanent_allow_hashes.add(d.hash)
        except Exception as e:
            log.exception("Error in _on_device_policy_changed for device %d: %s", device_id, e)

    def _lock_for_pending_hid(self) -> None:
        """Lock the screen for a deferred HID insert, unless every triggering
        device was unplugged during the notification delay."""
        if not self._hid_pending_devices:
            log.debug("HID lock timer fired with no pending devices — skipping lock")
            return
        self._screensaver.lock()

    def _on_screensaver_changed(self, active: bool) -> None:
        if active or not self._screensaver_pending_devices:
            return

        self._screensaver_pending_ids = list(self._screensaver_pending_devices)
        self._screensaver_pending_devices.clear()
        self._client.list_devices()

    def _on_screensaver_active_changed(self, active: bool) -> None:
        if not active or not self._hid_pending_devices:
            return

        pending_ids = list(self._hid_pending_devices)
        self._hid_pending_devices = set()
        for device_number in pending_ids:
            self._client.apply_device_policy(device_number, DeviceTarget.ALLOW, permanent=False)

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

        def on_finished(result: int, device_number: int = device.number) -> None:
            target = dialog.result_target
            permanent = dialog.permanent
            self._open_dialogs.pop(device_number, None)
            if target is not None:
                self._client.apply_device_policy(device_number, target, permanent)

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
            if self._device_list_window is not None and self._device_list_window.isVisible():
                self._device_list_window.hide()
            else:
                self._show_device_list()

    def _quit(self) -> None:
        self._reconnect_timer.stop()
        if self._device_list_window:
            self._device_list_window.close()
        for dialog in list(self._open_dialogs.values()):
            dialog.close()
        self._tray.hide()
        self._client.stop()
        self._screensaver.stop()
        self._app.quit()


def main() -> None:
    """Entry point for the usbguard_gui application."""
    log_level = os.environ.get("USBGUARD_GUI_LOG", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    # Allow Ctrl+C to work
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # SIGUSR1: re-exec ourselves — sent by the RPM %posttrans scriptlet after an update
    # so the running instance picks up the new code without user intervention.
    def _restart() -> None:
        log.info("Received SIGUSR1 — restarting to apply package update")
        os.execv(sys.argv[0], sys.argv)

    signal.signal(signal.SIGUSR1, lambda *_: QTimer.singleShot(0, _restart))

    app = QApplication(sys.argv)
    app.setApplicationName("usbguard_gui")
    app.setWindowIcon(_app_icon())
    app.setQuitOnLastWindowClosed(False)

    runtime_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.RuntimeLocation)
    if not runtime_dir:
        runtime_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation)
    _lock = QLockFile(f"{runtime_dir}/usbguard_gui.lock")
    if not _lock.tryLock():
        QMessageBox.warning(None, "USBGuard GUI", "Another instance is already running.")
        sys.exit(0)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, "USBGuard GUI", "System tray is not available.")
        sys.exit(1)

    tray_app = USBGuardTrayApp(app)
    tray_app.start()

    sys.exit(app.exec())
