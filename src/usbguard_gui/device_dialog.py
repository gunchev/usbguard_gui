"""Popup dialog for responding to a new USB device insertion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from usbguard_gui.device import Device, DeviceTarget, Persistence

if TYPE_CHECKING:
    from PyQt6.QtWidgets import QWidget

    from usbguard_gui.dbus_client import USBGuardClient
    from usbguard_gui.screensaver import ScreensaverMonitor

# Auto-close timeout in seconds (blocks device if no user response)
DEFAULT_TIMEOUT = 30


class DeviceActionDialog(QDialog):
    """Dialog shown when a new blocked USB device is inserted.

    The user can Allow or Block, Always or Once, or dismiss without deciding.
    'Close' is the default button: pressing Enter dismisses the dialog safely.
    If no action is taken within the timeout, no policy change is applied.
    """

    def __init__(self, device: Device, client: USBGuardClient, parent: QWidget | None = None,
                 timeout: int = DEFAULT_TIMEOUT, screensaver: ScreensaverMonitor | None = None) -> None:
        super().__init__(parent)
        self.device = device
        self._client = client
        self._screensaver = screensaver
        self._result_target: DeviceTarget | None = None
        self._persistence: Persistence = Persistence.UNCHANGED
        self._remaining = timeout
        self._cleanup_done = False
        self._device_present = True

        self.setWindowTitle("New USB Device")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setMinimumWidth(400)

        self._build_ui()
        self._start_timeout()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Title
        title = QLabel("<big><b>New USB Device Inserted</b></big>")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        # Device info form
        self._device_form = QFormLayout()
        self._update_device_details()
        layout.addLayout(self._device_form)

        # Timeout label
        self._timeout_label = QLabel()
        self._timeout_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._update_timeout_label()
        layout.addWidget(self._timeout_label)

        # Action buttons
        btn_layout = QDialogButtonBox()

        self._btn_allow_always = QPushButton("Allow Always")
        self._btn_allow_always.clicked.connect(self._on_allow_always)
        btn_layout.addButton(self._btn_allow_always, QDialogButtonBox.ButtonRole.AcceptRole)

        self._btn_allow_once = QPushButton("Allow Once")
        self._btn_allow_once.clicked.connect(self._on_allow_once)
        btn_layout.addButton(self._btn_allow_once, QDialogButtonBox.ButtonRole.AcceptRole)

        self._btn_block_once = QPushButton("Block Once")
        self._btn_block_once.clicked.connect(self._on_block_once)
        btn_layout.addButton(self._btn_block_once, QDialogButtonBox.ButtonRole.RejectRole)

        self._btn_block_always = QPushButton("Block Always")
        self._btn_block_always.clicked.connect(self._on_block_always)
        btn_layout.addButton(self._btn_block_always, QDialogButtonBox.ButtonRole.RejectRole)

        self._btn_close = QPushButton("Close")
        self._btn_close.clicked.connect(self._on_close)
        btn_layout.addButton(self._btn_close, QDialogButtonBox.ButtonRole.AcceptRole)

        layout.addWidget(btn_layout)

        # Make 'Close' the default button so Enter dismisses the dialog without
        # deciding, instead of triggering an allow action.
        self._btn_close.setDefault(True)

        # Track lock availability so the buttons follow it while the dialog
        # is open.  Without the monitor (old callers) no gating happens.
        # Connected as a bound method (not a lambda) and explicitly torn
        # down in _on_finished_cleanup: a lambda closing over `self` gives
        # PyQt no way to auto-disconnect when the dialog is destroyed, which
        # would otherwise leak every dialog for the life of the long-running
        # tray process (connection_changed lives on the app's single
        # long-lived ScreensaverMonitor).
        if self._screensaver is not None:
            self._screensaver.connection_changed.connect(self._on_connection_changed)
        self.finished.connect(self._on_finished_cleanup)
        self._update_actions_enabled()

    def _actions_enabled(self) -> bool:
        """Whether any policy action may be applied from this dialog.

        All allow/deny functionality is disabled while screen locking is
        unavailable: without the ability to lock first, allowing a
        keyboard would hand an attached-device attacker an unlocked
        session — exactly what this app exists to prevent.  The app
        therefore refuses to touch the policy at all, not just allows.
        """
        return self._screensaver is None or self._screensaver.connected

    def _update_actions_enabled(self) -> None:
        enabled = self._actions_enabled()
        for button in (self._btn_allow_always, self._btn_allow_once, self._btn_block_once,
                       self._btn_block_always, self._btn_close):
            button.setEnabled(enabled)

    def _update_device_details(self) -> None:
        while self._device_form.rowCount():
            self._device_form.removeRow(0)
        self._device_form.addRow("Name:", QLabel(self.device.name or "(unknown)"))
        self._device_form.addRow("USB ID:", QLabel(self.device.id))
        self._device_form.addRow("Type:", QLabel(self.device.class_description_string() or "(unknown)"))
        if self.device.serial:
            self._device_form.addRow("Serial:", QLabel(self.device.serial))
        self._device_form.addRow("Port:", QLabel(self.device.via_port or "(unknown)"))
        self._device_form.addRow("Connection:", QLabel(self.device.with_connect_type or "(unknown)"))

    def set_device(self, device: Device) -> None:
        """Retarget the dialog and its displayed details to a present instance."""
        self.device = device
        self._update_device_details()
        self.set_device_present(True)

    def set_device_present(self, present: bool) -> None:
        """Reflect whether the device is on the bus right now.

        The buttons deliberately stay live.  A flapping device is worth deciding
        about even in the gap between one incarnation and the next: the choice is
        recorded and applied the moment it reappears, rather than being lost
        because the user was a second too slow.
        """
        if self._device_present == present:
            return
        self._device_present = present
        self._update_timeout_label()

    @property
    def device_present(self) -> bool:
        return self._device_present

    def _on_connection_changed(self, _available: bool) -> None:
        self._update_actions_enabled()

    def _on_finished_cleanup(self) -> None:
        """Detach from the long-lived screensaver monitor, exactly once.

        ``finished`` can arrive more than once: closing the dialog and then
        rejecting it -- or a button click racing a programmatic close from the
        device-removed handler -- emits it again.  A second ``disconnect`` on an
        already-detached signal raises ``TypeError`` inside a Qt slot, and PyQt
        turns that into an abort: the whole tray process dies, not just the
        dialog.  Idempotency here is load-bearing, not tidiness.
        """
        if self._cleanup_done:
            return
        self._cleanup_done = True
        if self._screensaver is not None:
            self._screensaver.connection_changed.disconnect(self._on_connection_changed)
        self.deleteLater()

    def _start_timeout(self) -> None:
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        self._remaining -= 1
        self._update_timeout_label()
        if self._remaining <= 0:
            self._timer.stop()
            self.close()

    def _update_timeout_label(self) -> None:
        """Render the one status line, for whichever state the device is in.

        The countdown and the away-notice share a label, and `_tick` runs once a
        second, so the notice has to be rendered *here* rather than written over
        the top of it -- otherwise it survived less than a second and the user
        was told "device stays blocked" while their choice was in fact still
        live and waiting for the device to come back.
        """
        if not self._device_present:
            # The notice is held, but the clock is not hidden with it: the user
            # can still click while the device is away, so they need to see how
            # long they have before the dialog closes on them.
            self._timeout_label.setText("Device disconnected - your choice applies when it returns "
                                        f"(auto-close in {self._remaining}s)")
            return
        self._timeout_label.setText(f"Auto-close in {self._remaining}s (no action is applied)")

    def _action_blocked(self) -> bool:
        """Warn and return True if the action cannot be applied.

        The dialog stays open so the user can retry once the connection is
        back — silently accepting the click would make them believe the
        action was applied.
        """
        if not self._client.connected:
            QMessageBox.warning(
                self,
                "USBGuard GUI",
                "The USBGuard daemon is not connected.\nThe action was not applied — "
                "try again once the tray icon shows 'connected'.",
            )
            return True
        if self._screensaver is not None and not self._screensaver.connected:
            QMessageBox.warning(
                self,
                "USBGuard GUI",
                "Screen locking is unavailable — device actions are disabled.\n"
                "Devices remain blocked by USBGuard's policy.",
            )
            return True
        return False

    def _choose(self, target: DeviceTarget, persistence: Persistence) -> None:
        """Record the decision and close, unless the action cannot be applied."""
        if self._action_blocked():
            return
        self._result_target = target
        self._persistence = persistence
        self.accept()

    def _on_allow_always(self) -> None:
        self._choose(DeviceTarget.ALLOW, Persistence.ALWAYS)

    def _on_allow_once(self) -> None:
        self._choose(DeviceTarget.ALLOW, Persistence.ONCE)

    def _on_block_once(self) -> None:
        self._choose(DeviceTarget.BLOCK, Persistence.ONCE)

    def _on_block_always(self) -> None:
        self._choose(DeviceTarget.BLOCK, Persistence.ALWAYS)

    def _on_close(self) -> None:
        """Dismiss without deciding -- no target recorded, nothing applied.

        Deliberately **not** gated on `_action_blocked()`: there is no action
        here that could fail, and gating it meant a dropped connection left the
        user unable to close the dialog at all.  The device stays where
        USBGuard's implicit policy put it -- blocked -- and nothing durable is
        touched by a dismiss nobody chose.
        """
        self._result_target = None
        self._persistence = Persistence.UNCHANGED
        self.reject()

    @property
    def result_target(self) -> DeviceTarget | None:
        """The target chosen by the user, or None if dialog timed out / was closed."""
        return self._result_target

    @property
    def persistence(self) -> Persistence:
        """What the chosen action does to the device's permanent rule."""
        return self._persistence
