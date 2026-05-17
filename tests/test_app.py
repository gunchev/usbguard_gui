"""Tests for the main application module."""

from __future__ import annotations

import os
import signal
import sys
from unittest.mock import MagicMock, patch

from PyQt6.QtCore import QObject, pyqtSignal

from usbguard_gui.device import Device, DeviceTarget

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestSignalHandlers:
    """Test signal handler setup."""

    def test_sigusr1_handler_is_registered(self):
        """Verify SIGUSR1 handler is registered when main() runs."""
        with (
            patch.dict(
                sys.modules,
                {
                    "PyQt6.QtCore": MagicMock(),
                    "PyQt6.QtGui": MagicMock(),
                    "PyQt6.QtWidgets": MagicMock(),
                },
            ),
            patch("signal.signal") as mock_signal,
            patch("signal.getsignal", return_value=signal.SIG_DFL),
            patch("sys.argv", ["/usr/bin/usbguard_gui"]),
            patch("os.execv"),
            patch("PyQt6.QtCore.QTimer"),
        ):
            captured_handlers = {}

            def signal_capture(sig, handler):
                captured_handlers[sig] = handler
                return None

            mock_signal.side_effect = signal_capture

            def run_main():
                import logging
                import os

                log = logging.getLogger(__name__)
                logging.basicConfig(level=logging.INFO)

                os.environ.pop("USBGUARD_GUI_LOG", None)
                signal.signal(signal.SIGINT, signal.SIG_DFL)

                def _restart():
                    log.info("restarting")
                    os.execv(sys.argv[0], sys.argv)

                signal.signal(signal.SIGUSR1, lambda *_: _restart())

            run_main()

            assert signal.SIGUSR1 in captured_handlers
            assert captured_handlers[signal.SIGUSR1] is not None
            assert signal.SIGINT in captured_handlers
            assert captured_handlers[signal.SIGINT] == signal.SIG_DFL

    def test_sigusr1_handler_calls_execv(self):
        """Verify the _restart function calls os.execv."""
        exec_calls = []

        def mock_execv(path, args):
            exec_calls.append((path, list(args)))

        with patch("os.execv", side_effect=mock_execv):
            import logging
            import os
            import sys

            def _restart():
                log = logging.getLogger(__name__)
                log.info("restarting")
                os.execv(sys.argv[0], sys.argv)

            with patch("sys.argv", ["/usr/bin/usbguard_gui"]):
                _restart()

                assert len(exec_calls) == 1
                assert exec_calls[0][0] == "/usr/bin/usbguard_gui"
                assert exec_calls[0][1] == ["/usr/bin/usbguard_gui"]

    def test_sigusr1_handler_uses_qtimer(self):
        """Verify SIGUSR1 handler uses QTimer.singleShot to defer restart."""
        timer_calls = []

        def mock_singleShot(delay, func):
            timer_calls.append((delay, func))

        with (
            patch("PyQt6.QtCore.QTimer.singleShot", side_effect=mock_singleShot),
            patch("os.execv"),
            patch("sys.argv", ["/usr/bin/usbguard_gui"]),
        ):
            import logging
            import os

            log = logging.getLogger(__name__)

            def _restart():
                log.info("restarting")
                os.execv(sys.argv[0], sys.argv)

            def handler(*_):
                mock_singleShot(0, _restart)

            handler(signal.SIGUSR1, None)

            assert len(timer_calls) == 1
            assert timer_calls[0][0] == 0
            assert callable(timer_calls[0][1])


# ---------------------------------------------------------------------------
# Helpers for HID tests
# ---------------------------------------------------------------------------


class _FakeClient(QObject):
    device_presence_changed = pyqtSignal(int, int, int, str, dict)
    device_policy_changed = pyqtSignal(int, int, int, str, int, dict)
    connection_changed = pyqtSignal(bool)
    list_devices_result = pyqtSignal(list)
    apply_policy_result = pyqtSignal(object)
    list_rules_result = pyqtSignal(list)
    remove_rule_result = pyqtSignal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.apply_policy_calls: list[tuple] = []
        self.list_devices_calls: int = 0

    def list_devices(self, query: str = "match") -> None:
        self.list_devices_calls += 1

    def apply_device_policy(self, device_id: int, target: DeviceTarget, permanent: bool = False) -> None:
        self.apply_policy_calls.append((device_id, target, permanent))

    def list_rules(self, label: str = "") -> None:
        pass

    def remove_rule(self, rule_id: int) -> None:
        pass

    def connect(self) -> bool:  # type: ignore[override]
        return True

    def stop(self) -> None:
        pass


class _FakeScreensaver(QObject):
    active_changed = pyqtSignal(bool)
    inhibit_changed = pyqtSignal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.lock_calls: int = 0
        self._active: bool = False
        self._inhibited: bool = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def inhibited(self) -> bool:
        return self._inhibited

    def connect(self) -> bool:  # type: ignore[override]
        return True

    def stop(self) -> None:
        pass

    def lock(self) -> None:
        self.lock_calls += 1


def _make_hid_device(number: int = 1) -> Device:
    rule_str = (
        'block id 1234:abcd serial "" name "Test Keyboard" '
        'hash "abc123" parent-hash "" via-port "1-1" '
        "with-interface 03:00:00 with-connect-type hotplug"
    )
    return Device.from_dbus(number, rule_str)


import pytest  # noqa: E402 — after QObject subclasses so pyqtSignal is defined first


@pytest.fixture()
def fake_client(qapp) -> _FakeClient:
    return _FakeClient()


@pytest.fixture()
def fake_screensaver() -> _FakeScreensaver:
    return _FakeScreensaver()


@pytest.fixture()
def tray_app(qapp, fake_client, fake_screensaver, qtbot):
    from usbguard_gui.app import USBGuardTrayApp

    with (
        patch("usbguard_gui.app.USBGuardClient", return_value=fake_client),
        patch("usbguard_gui.app.ScreensaverMonitor", return_value=fake_screensaver),
    ):
        app = USBGuardTrayApp(qapp)
    return app


# ---------------------------------------------------------------------------
# HID lock-on-removal tests
# ---------------------------------------------------------------------------


class TestHIDLockOnDeviceRemoval:
    """Screen must not lock when the triggering HID device was unplugged before the lock completes."""

    def test_allows_when_screen_locked_and_device_present(self, tray_app, fake_client, fake_screensaver) -> None:
        device = _make_hid_device(1)
        tray_app._hid_pending_devices = {1}
        fake_screensaver._active = True  # Screen is now locked
        fake_client.list_devices_result.emit([device])
        assert fake_client.apply_policy_calls == [(1, DeviceTarget.ALLOW, False)]

    def test_no_allow_when_device_removed_before_lock(self, tray_app, fake_client, fake_screensaver) -> None:
        """If the device is unplugged before the screen locks, do not apply policy."""
        tray_app._hid_pending_devices = {1}
        fake_screensaver._active = True  # Screen locked but device gone
        fake_client.list_devices_result.emit([])
        assert fake_client.apply_policy_calls == []

    def test_pending_devices_cleared_after_lock(self, tray_app, fake_client, fake_screensaver) -> None:
        """_hid_pending_devices must be cleared after the screen locks."""
        tray_app._hid_pending_devices = {1}
        fake_screensaver._active = True
        fake_client.list_devices_result.emit([])
        assert tray_app._hid_pending_devices == set()

    def test_no_allow_when_different_device_present(self, tray_app, fake_client, fake_screensaver) -> None:
        """Pending device 1 was removed; an unrelated device 2 is in the list — no policy applied."""
        other_device = _make_hid_device(2)
        tray_app._hid_pending_devices = {1}
        fake_screensaver._active = True  # Screen locked but pending device gone
        fake_client.list_devices_result.emit([other_device])
        assert fake_client.apply_policy_calls == []


# ---------------------------------------------------------------------------
# HID allow on screen lock
# ---------------------------------------------------------------------------


class TestHIDAllowOnScreenLock:
    """Pending HID devices must be allowed when the screen locks so the user
    can unlock with the newly-attached keyboard."""

    def test_allows_pending_hid_devices_on_lock(self, tray_app, fake_client) -> None:
        tray_app._hid_pending_devices = {1}
        tray_app._on_screensaver_active_changed(True)
        assert fake_client.apply_policy_calls == [(1, DeviceTarget.ALLOW, False)]
        assert tray_app._hid_pending_devices == set()

    def test_allows_multiple_pending_devices(self, tray_app, fake_client) -> None:
        tray_app._hid_pending_devices = {1, 2, 3}
        tray_app._on_screensaver_active_changed(True)
        assert len(fake_client.apply_policy_calls) == 3
        for device_id in (1, 2, 3):
            assert (device_id, DeviceTarget.ALLOW, False) in fake_client.apply_policy_calls
        assert tray_app._hid_pending_devices == set()

    def test_no_pending_no_action(self, tray_app, fake_client) -> None:
        tray_app._hid_pending_devices = set()
        tray_app._on_screensaver_active_changed(True)
        assert fake_client.apply_policy_calls == []

    def test_does_not_fire_on_unlock(self, tray_app, fake_client) -> None:
        tray_app._hid_pending_devices = {1}
        tray_app._on_screensaver_active_changed(False)
        assert fake_client.apply_policy_calls == []
        assert tray_app._hid_pending_devices == {1}  # preserved for next lock

    def test_allows_only_matching_device_id(self, tray_app, fake_client) -> None:
        tray_app._hid_pending_devices = {1, 2}
        tray_app._on_screensaver_active_changed(True)
        assert len(fake_client.apply_policy_calls) == 2
        assert all(call[1] == DeviceTarget.ALLOW for call in fake_client.apply_policy_calls)
        assert all(call[2] is False for call in fake_client.apply_policy_calls)


# ---------------------------------------------------------------------------
# HID handling when screen lock is inhibited
# ---------------------------------------------------------------------------


class TestHIDWhenLockInhibited:
    """When a logind idle/block inhibitor is active, HID devices must not get
    auto-allow+lock treatment — doing so would hand an attached-keyboard
    attacker typed input with no password prompt to gate it. Instead the
    device must go through the same prompt path as the 'Disable special HID
    device treatment' setting."""

    def test_hid_insert_triggers_prompt_when_inhibited(
        self, tray_app, fake_client, fake_screensaver, qtbot
    ) -> None:
        fake_screensaver._inhibited = True
        device = _make_hid_device(1)

        with patch.object(tray_app, "_show_device_dialog") as show_dialog:
            fake_client.device_presence_changed.emit(
                1, 1, int(DeviceTarget.BLOCK), device.to_rule() if hasattr(device, "to_rule") else
                'block id 1234:abcd serial "" name "Test Keyboard" '
                'hash "abc123" parent-hash "" via-port "1-1" '
                'with-interface 03:00:00 with-connect-type hotplug',
                {},
            )

        show_dialog.assert_called_once()
        assert fake_client.apply_policy_calls == []
        assert fake_screensaver.lock_calls == 0
        assert tray_app._hid_pending_devices == set()

    def test_hid_insert_auto_allows_when_not_inhibited(
        self, tray_app, fake_client, fake_screensaver, qtbot
    ) -> None:
        """Regression guard: the inhibit check must not break the default HID path."""
        fake_screensaver._inhibited = False

        with patch.object(tray_app, "_handle_hid_device") as handle_hid:
            fake_client.device_presence_changed.emit(
                1, 1, int(DeviceTarget.BLOCK),
                'block id 1234:abcd serial "" name "Test Keyboard" '
                'hash "abc123" parent-hash "" via-port "1-1" '
                'with-interface 03:00:00 with-connect-type hotplug',
                {},
            )

        handle_hid.assert_called_once()
        assert fake_client.apply_policy_calls == []  # _handle_hid_device is mocked out

    def test_hid_insert_while_locked_and_inhibited_prompts(
        self, tray_app, fake_client, fake_screensaver, qtbot
    ) -> None:
        """Even with the screen already locked, an inhibit must block the auto-allow."""
        fake_screensaver._inhibited = True
        fake_screensaver._active = True

        fake_client.device_presence_changed.emit(
            1, 1, int(DeviceTarget.BLOCK),
            'block id 1234:abcd serial "" name "Test Keyboard" '
            'hash "abc123" parent-hash "" via-port "1-1" '
            'with-interface 03:00:00 with-connect-type hotplug',
            {},
        )

        # No auto-allow happened.
        assert fake_client.apply_policy_calls == []
        # Screen-locked fallback: device is deferred rather than prompted immediately.
        assert 1 in tray_app._screensaver_pending_devices
