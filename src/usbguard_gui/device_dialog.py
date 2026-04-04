"""Popup dialog for responding to a new USB device insertion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from usbguard_gui.device import Device, DeviceTarget

if TYPE_CHECKING:
    from PyQt6.QtWidgets import QWidget

# Auto-close timeout in seconds (blocks device if no user response)
DEFAULT_TIMEOUT = 30


class DeviceActionDialog(QDialog):
    """Dialog shown when a new blocked USB device is inserted.

    The user can Allow (permanently), Allow Temporarily, Block, or Reject.
    If no action is taken within the timeout, the device remains blocked.
    """

    def __init__(self, device: Device, parent: QWidget | None = None, timeout: int = DEFAULT_TIMEOUT) -> None:
        super().__init__(parent)
        self.device = device
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
        self._timeout_label.setText(f"Auto-close in {self._remaining}s (device stays blocked)")

    def _on_allow(self) -> None:
        self._result_target = DeviceTarget.ALLOW
        self._permanent = True
        self.accept()

    def _on_allow_temp(self) -> None:
        self._result_target = DeviceTarget.ALLOW
        self._permanent = False
        self.accept()

    def _on_block(self) -> None:
        self._result_target = DeviceTarget.BLOCK
        self._permanent = False
        self.accept()

    def _on_reject(self) -> None:
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
