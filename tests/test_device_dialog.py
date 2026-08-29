"""Tests for the device action dialog."""

from __future__ import annotations

import os

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QMessageBox

from usbguard_gui.device import Device, DeviceTarget
from usbguard_gui.device_dialog import DeviceActionDialog

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_RULE = (
    'block id 1234:abcd serial "" name "Test Device" '
    'hash "abc123" parent-hash "" via-port "1-1" '
    "with-interface 03:00:00 with-connect-type hotplug"
)


class _FakeClient:
    """Minimal stand-in for USBGuardClient that records policy calls."""

    def __init__(self, connected: bool = True) -> None:
        self._connected = connected
        self.apply_calls: list[tuple] = []

    @property
    def connected(self) -> bool:
        return self._connected

    def apply_device_policy(self, device_id: int, target: DeviceTarget, permanent: bool = False) -> None:
        self.apply_calls.append((device_id, target, permanent))


class _FakeScreensaver(QObject):
    """Minimal stand-in for ScreensaverMonitor (lock availability)."""

    connection_changed = pyqtSignal(bool)

    def __init__(self, connected: bool = True) -> None:
        super().__init__()
        self._connected = connected

    @property
    def connected(self) -> bool:
        return self._connected


class TestDialogLockUnavailable:
    """When screen locking is unavailable, every action button must be
    disabled — allowing (or blocking) a device without the lock-first
    guarantee breaks the app's security contract, so the UI refuses to
    touch the policy at all and says why."""

    @pytest.fixture()
    def buttons(self, dialog_with_screensaver):
        dialog = dialog_with_screensaver[0]
        return [dialog._btn_allow, dialog._btn_allow_temp, dialog._btn_block, dialog._btn_reject]

    @pytest.fixture()
    def dialog_with_screensaver(self, qapp, qtbot):
        screensaver = _FakeScreensaver(connected=False)
        dialog = DeviceActionDialog(_make_device(), _FakeClient(), screensaver=screensaver)
        qtbot.addWidget(dialog)
        return dialog, screensaver

    def test_buttons_disabled_when_lock_unavailable(self, dialog_with_screensaver, buttons) -> None:
        for btn in buttons:
            assert not btn.isEnabled()

    def test_buttons_enabled_when_lock_available(self, qapp, qtbot) -> None:
        screensaver = _FakeScreensaver(connected=True)
        dialog = DeviceActionDialog(_make_device(), _FakeClient(), screensaver=screensaver)
        qtbot.addWidget(dialog)

        for btn in (dialog._btn_allow, dialog._btn_allow_temp, dialog._btn_block, dialog._btn_reject):
            assert btn.isEnabled()

    def test_buttons_enabled_without_screensaver(self, qapp, qtbot) -> None:
        """Callers that do not pass a monitor are not gated (old behaviour)."""
        dialog = DeviceActionDialog(_make_device(), _FakeClient())
        qtbot.addWidget(dialog)

        for btn in (dialog._btn_allow, dialog._btn_allow_temp, dialog._btn_block, dialog._btn_reject):
            assert btn.isEnabled()

    def test_buttons_follow_lock_state_changes(self, dialog_with_screensaver, buttons) -> None:
        _, screensaver = dialog_with_screensaver
        assert all(not btn.isEnabled() for btn in buttons)

        # The monitor updates its property and then emits (like the real
        # _on_connected), so mirror that ordering in the fake.
        screensaver._connected = True
        screensaver.connection_changed.emit(True)
        assert all(btn.isEnabled() for btn in buttons)

        screensaver._connected = False
        screensaver.connection_changed.emit(False)
        assert all(not btn.isEnabled() for btn in buttons)

    @pytest.mark.parametrize("handler", ["_on_allow", "_on_allow_temp", "_on_block", "_on_reject"])
    def test_actions_warn_when_lock_unavailable(self, dialog_with_screensaver, mocker, handler: str) -> None:
        """If a handler runs while lock is unavailable (e.g. the state flips
        after the buttons were enabled), it must warn, not record the choice."""
        dialog, _ = dialog_with_screensaver
        warn = mocker.patch.object(QMessageBox, "warning")

        getattr(dialog, handler)()

        assert warn.called
        assert dialog.result_target is None
        dialog.close()


class TestNoDefaultAction:
    """Enter must not silently pick an action: the dialog has no default
    button, and a stray Enter used to trigger Qt's auto-assigned default
    ('Allow (Permanent)') — creating a persistent rule from an accidental
    keypress."""

    def test_enter_does_not_trigger_any_action(self, qapp, qtbot) -> None:
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest

        client = _FakeClient()
        dialog = DeviceActionDialog(_make_device(), client)
        qtbot.addWidget(dialog)
        dialog.show()
        qapp.processEvents()

        for _ in range(3):
            QTest.keyClick(dialog, Qt.Key.Key_Return)
            qapp.processEvents()

        assert dialog.result_target is None
        assert client.apply_calls == []
        assert dialog.isVisible()

    def test_enter_on_both_keys(self, qapp, qtbot) -> None:
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest

        client = _FakeClient()
        dialog = DeviceActionDialog(_make_device(), client)
        qtbot.addWidget(dialog)
        dialog.show()
        qapp.processEvents()

        QTest.keyClick(dialog, Qt.Key.Key_Enter)
        qapp.processEvents()

        assert dialog.result_target is None
        assert dialog.isVisible()

    def test_escape_still_closes_without_action(self, qapp, qtbot) -> None:
        """Esc keeps its cancel semantics: close the dialog, record nothing."""
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest

        client = _FakeClient()
        dialog = DeviceActionDialog(_make_device(), client)
        qtbot.addWidget(dialog)
        dialog.show()
        qapp.processEvents()

        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        qapp.processEvents()

        assert dialog.result_target is None
        assert client.apply_calls == []
        assert not dialog.isVisible()


def _make_device() -> Device:
    return Device.from_dbus(1, _RULE)


class TestDialogConnectionWarning:
    """When the USBGuard daemon is not connected, action buttons must show a
    warning instead of silently accepting the click — the user must not
    believe their choice was applied.  The dialog stays open so the user
    can retry once the daemon is back."""

    @pytest.mark.parametrize("handler", ["_on_allow", "_on_allow_temp", "_on_block", "_on_reject"])
    def test_all_actions_warn_when_disconnected(self, qapp, qtbot, mocker, handler: str) -> None:
        client = _FakeClient(connected=False)
        dialog = DeviceActionDialog(_make_device(), client)
        qtbot.addWidget(dialog)
        warn = mocker.patch.object(QMessageBox, "warning")

        getattr(dialog, handler)()

        assert warn.called, f"{handler} did not warn while disconnected"
        # No choice was recorded, so the app will not apply anything.
        assert dialog.result_target is None
        assert client.apply_calls == []
        dialog.close()

    @pytest.mark.parametrize(
        ("handler", "target", "permanent"),
        [
            ("_on_allow", DeviceTarget.ALLOW, True),
            ("_on_allow_temp", DeviceTarget.ALLOW, False),
            ("_on_block", DeviceTarget.BLOCK, False),
            ("_on_reject", DeviceTarget.REJECT, False),
        ],
    )
    def test_all_actions_record_choice_when_connected(
        self, qapp, qtbot, mocker, handler: str, target: DeviceTarget, permanent: bool
    ) -> None:
        client = _FakeClient(connected=True)
        dialog = DeviceActionDialog(_make_device(), client)
        qtbot.addWidget(dialog)
        warn = mocker.patch.object(QMessageBox, "warning")

        getattr(dialog, handler)()

        assert not warn.called
        assert dialog.result_target is target
        assert dialog.permanent is permanent
