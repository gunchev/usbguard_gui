"""Popup dialog for responding to a new USB device insertion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import QEvent, Qt, QTimer
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from usbguard_gui.device import Device, DeviceTarget

if TYPE_CHECKING:
    from PyQt6.QtWidgets import QWidget

    from usbguard_gui.dbus_client import USBGuardClient
    from usbguard_gui.screensaver import ScreensaverMonitor

# Auto-close timeout in seconds (blocks device if no user response)
DEFAULT_TIMEOUT = 30


class DeviceActionDialog(QDialog):
    """Dialog shown when a new blocked USB device is inserted.

    The user can Allow (permanently), Allow Temporarily, Block, or Reject.
    If no action is taken within the timeout, the device remains blocked.
    """

    def __init__(
        self,
        device: Device,
        client: USBGuardClient,
        parent: QWidget | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        screensaver: ScreensaverMonitor | None = None,
    ) -> None:
        super().__init__(parent)
        self.device = device
        self._client = client
        self._screensaver = screensaver
        self._result_target: DeviceTarget | None = None
        self._permanent = False
        self._remaining = timeout

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
        form = QFormLayout()
        form.addRow("Name:", QLabel(self.device.name or "(unknown)"))
        form.addRow("USB ID:", QLabel(self.device.id))
        form.addRow("Type:", QLabel(self.device.class_description_string() or "(unknown)"))
        if self.device.serial:
            form.addRow("Serial:", QLabel(self.device.serial))
        form.addRow("Port:", QLabel(self.device.via_port or "(unknown)"))
        form.addRow("Connection:", QLabel(self.device.with_connect_type or "(unknown)"))
        layout.addLayout(form)

        # Timeout label
        self._timeout_label = QLabel()
        self._timeout_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._update_timeout_label()
        layout.addWidget(self._timeout_label)

        # Action buttons
        btn_layout = QDialogButtonBox()

        self._btn_allow = QPushButton("Allow (Permanent)")
        self._btn_allow.clicked.connect(self._on_allow)
        btn_layout.addButton(self._btn_allow, QDialogButtonBox.ButtonRole.AcceptRole)

        self._btn_allow_temp = QPushButton("Allow (Temporary)")
        self._btn_allow_temp.clicked.connect(self._on_allow_temp)
        btn_layout.addButton(self._btn_allow_temp, QDialogButtonBox.ButtonRole.AcceptRole)

        self._btn_block = QPushButton("Block")
        self._btn_block.clicked.connect(self._on_block)
        btn_layout.addButton(self._btn_block, QDialogButtonBox.ButtonRole.RejectRole)

        self._btn_reject = QPushButton("Reject")
        self._btn_reject.clicked.connect(self._on_reject)
        btn_layout.addButton(self._btn_reject, QDialogButtonBox.ButtonRole.RejectRole)

        layout.addWidget(btn_layout)

        # Track lock availability so the buttons follow it while the dialog
        # is open.  Without the monitor (old callers) no gating happens.
        if self._screensaver is not None:
            self._screensaver.connection_changed.connect(lambda _available: self._update_actions_enabled())
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
        for button in (self._btn_allow, self._btn_allow_temp, self._btn_block, self._btn_reject):
            button.setEnabled(enabled)

    def _start_timeout(self) -> None:
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def event(self, event: QEvent) -> bool:
        # This dialog intentionally has no default button.  Qt assigns the
        # first AcceptRole button as the default when the dialog is shown
        # (during the post-show polish wave, where it cannot be reliably
        # cleared), so swallow Return/Enter here to guarantee that a stray
        # Enter cannot silently apply "Allow (Permanent)" — a persistent
        # rule from an accidental keypress.  Esc keeps its cancel semantics.
        if (
            isinstance(event, QKeyEvent)
            and event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        ):
            return True
        return super().event(event)

    def _tick(self) -> None:
        self._remaining -= 1
        self._update_timeout_label()
        if self._remaining <= 0:
            self._timer.stop()
            self.close()

    def _update_timeout_label(self) -> None:
        self._timeout_label.setText(f"Auto-close in {self._remaining}s (device stays blocked)")

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

    def _on_allow(self) -> None:
        if self._action_blocked():
            return
        self._result_target = DeviceTarget.ALLOW
        self._permanent = True
        self.accept()

    def _on_allow_temp(self) -> None:
        if self._action_blocked():
            return
        self._result_target = DeviceTarget.ALLOW
        self._permanent = False
        self.accept()

    def _on_block(self) -> None:
        if self._action_blocked():
            return
        self._result_target = DeviceTarget.BLOCK
        self._permanent = False
        self.accept()

    def _on_reject(self) -> None:
        if self._action_blocked():
            return
        self._result_target = DeviceTarget.REJECT
        self._permanent = False
        self.accept()

    @property
    def result_target(self) -> DeviceTarget | None:
        """The target chosen by the user, or None if dialog timed out / was closed."""
        return self._result_target

    @property
    def permanent(self) -> bool:
        """Whether the policy should be stored permanently."""
        return self._permanent
