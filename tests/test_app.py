"""Tests for the main application module."""

from __future__ import annotations

import os
import signal
from unittest.mock import MagicMock, patch

from PyQt6.QtCore import QObject, pyqtSignal

from usbguard_gui.device import Device, DeviceTarget, PresenceEvent

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestSignalHandlers:
    """Verify that main() wires SIGINT→SIG_DFL and SIGUSR1→deferred re-exec.

    These tests exercise the real main() code path (with everything after
    signal registration mocked out), not a copy of the logic.
    """

    def test_main_registers_sigint_default(self):
        """main() must reset SIGINT to SIG_DFL so Ctrl+C works."""
        captured: dict = {}

        def capture_signal(sig, handler):
            captured[sig] = handler

        with (
            patch("signal.signal", side_effect=capture_signal),
            patch("usbguard_gui.app.QApplication") as mock_app_cls,
            patch("usbguard_gui.app._app_icon"),
            patch("usbguard_gui.app.QStandardPaths") as mock_paths,
            patch("usbguard_gui.app.QLockFile") as mock_lock_cls,
            patch("usbguard_gui.app.QSystemTrayIcon.isSystemTrayAvailable", return_value=True),
            patch("usbguard_gui.app.USBGuardTrayApp"),
            patch("usbguard_gui.app.QTimer.singleShot"),
            patch("sys.argv", ["/usr/bin/usbguard_gui"]),
            patch("sys.exit"),
        ):
            mock_paths.writableLocation.return_value = "/tmp"
            mock_lock_cls.return_value.tryLock.return_value = True
            mock_app_cls.return_value.exec.return_value = 0

            from usbguard_gui.app import main
            main()

        assert captured[signal.SIGINT] == signal.SIG_DFL

    def test_main_registers_sigusr1_reexec_via_qtimer(self):
        """main() must register SIGUSR1 to defer os.execv through QTimer.singleShot
        (so the Qt event loop can finish the current tick before re-exec)."""
        captured: dict = {}
        qtimer_calls: list = []

        def capture_signal(sig, handler):
            captured[sig] = handler

        def mock_singleShot(delay, fn):
            qtimer_calls.append((delay, fn))

        with (
            patch("signal.signal", side_effect=capture_signal),
            patch("usbguard_gui.app.QApplication") as mock_app_cls,
            patch("usbguard_gui.app._app_icon"),
            patch("usbguard_gui.app.QStandardPaths") as mock_paths,
            patch("usbguard_gui.app.QLockFile") as mock_lock_cls,
            patch("usbguard_gui.app.QSystemTrayIcon.isSystemTrayAvailable", return_value=True),
            patch("usbguard_gui.app.USBGuardTrayApp"),
            patch("usbguard_gui.app.QTimer.singleShot", side_effect=mock_singleShot),
            patch("sys.argv", ["/usr/bin/usbguard_gui"]),
            patch("sys.exit"),
            patch("os.execv") as mock_execv,
        ):
            mock_paths.writableLocation.return_value = "/tmp"
            mock_lock_cls.return_value.tryLock.return_value = True
            mock_app_cls.return_value.exec.return_value = 0

            from usbguard_gui.app import main
            main()

            # SIGUSR1 handler must be registered
            assert signal.SIGUSR1 in captured
            handler = captured[signal.SIGUSR1]
            assert callable(handler)

            # Invoke the handler — it must schedule a restart via QTimer.singleShot
            handler(signal.SIGUSR1, None)
            assert len(qtimer_calls) == 1
            delay, restart_fn = qtimer_calls[0]
            assert delay == 0
            assert callable(restart_fn)

            # The restart function must call os.execv with the current argv
            restart_fn()
            mock_execv.assert_called_once_with("/usr/bin/usbguard_gui", ["/usr/bin/usbguard_gui"])


# ---------------------------------------------------------------------------
# Helpers for HID tests
# ---------------------------------------------------------------------------


class _FakeClient(QObject):
    device_presence_changed = pyqtSignal(int, int, int, str, dict)
    device_policy_changed = pyqtSignal(int, int, int, str, int, dict)
    connection_changed = pyqtSignal(bool)
    list_devices_result = pyqtSignal(list)
    list_devices_correlated = pyqtSignal(int, list)
    list_rules_result = pyqtSignal(list)
    remove_rule_result = pyqtSignal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.apply_policy_calls: list[tuple] = []
        self.list_devices_calls: int = 0
        self.fetch_devices_calls: list[int] = []
        self._connected = True

    @property
    def connected(self) -> bool:
        return self._connected

    def list_devices(self, query: str = "match") -> None:
        self.list_devices_calls += 1

    def fetch_devices(self, request_id: int, query: str = "match") -> None:
        self.fetch_devices_calls.append(request_id)

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
    connection_changed = pyqtSignal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.lock_calls: int = 0
        self._active: bool = False
        self._inhibited: bool = False
        self._connected: bool = True

    @property
    def active(self) -> bool:
        return self._active

    @property
    def inhibited(self) -> bool:
        return self._inhibited

    @property
    def connected(self) -> bool:
        return self._connected

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


class _FakeSettings:
    """In-memory SettingsProtocol implementation.

    Injected into USBGuardTrayApp so no test ever reads or writes the real
    per-user config (~/.config/usbguard_gui/general.conf).  A preference
    toggled in the running app used to flip HID test outcomes here; with the
    seam, the suite controls its own settings.
    """

    def __init__(self, disable_hid_treatment: bool = False) -> None:
        self._disable_hid: bool = disable_hid_treatment
        self.write_calls: list[bool] = []

    def disable_hid_treatment(self) -> bool:
        return self._disable_hid

    def set_disable_hid_treatment(self, value: bool) -> None:
        self.write_calls.append(value)
        self._disable_hid = value


@pytest.fixture()
def fake_client(qapp) -> _FakeClient:
    return _FakeClient()


@pytest.fixture()
def fake_screensaver() -> _FakeScreensaver:
    return _FakeScreensaver()


@pytest.fixture()
def fake_settings() -> _FakeSettings:
    return _FakeSettings()


@pytest.fixture()
def tray_app(qapp, fake_client, fake_screensaver, fake_settings, qtbot):
    from usbguard_gui.app import USBGuardTrayApp

    with (
        patch("usbguard_gui.app.USBGuardClient", return_value=fake_client),
        patch("usbguard_gui.app.ScreensaverMonitor", return_value=fake_screensaver),
    ):
        app = USBGuardTrayApp(qapp, settings=fake_settings)
    return app


# ---------------------------------------------------------------------------
# Single-instance lock lifetime
# ---------------------------------------------------------------------------


class TestQuitUnlocksInstanceLock:
    """_quit() must explicitly unlock() the single-instance QLockFile rather
    than relying on process exit to release it — so a future refactor that
    moves _quit()'s callers out of the stack frame holding the lock can't
    silently skip releasing it."""

    def test_quit_unlocks_instance_lock(self, qapp, fake_client, fake_screensaver, fake_settings) -> None:
        from usbguard_gui.app import USBGuardTrayApp

        mock_lock = MagicMock()
        with (
            patch("usbguard_gui.app.USBGuardClient", return_value=fake_client),
            patch("usbguard_gui.app.ScreensaverMonitor", return_value=fake_screensaver),
        ):
            app = USBGuardTrayApp(qapp, lock_file=mock_lock, settings=fake_settings)

        app._quit()

        mock_lock.unlock.assert_called_once()

    def test_quit_without_lock_file_does_not_raise(self, tray_app) -> None:
        """Callers that don't pass a lock_file (e.g. existing tests) must
        still be able to call _quit() safely."""
        tray_app._quit()


# ---------------------------------------------------------------------------
# Settings injection (tests must not read/write the real user config)
# ---------------------------------------------------------------------------


class TestSettingsInjection:
    """The tray app takes its settings by injection.

    Regression guard: the QSettings-backed Settings singleton resolves to the
    developer's own ~/.config/usbguard_gui/general.conf, so a value toggled
    in the GUI (disable_hid_treatment=true) silently changed what the suite
    asserted — the HID pending/lock flow was skipped and the HID tests failed
    only on that machine.  Injecting a fake removes the coupling.
    """

    _RULE = (
        'block id 1234:abcd serial "" name "Test Keyboard" '
        'hash "abc123" parent-hash "" via-port "1-1" '
        'with-interface 03:00:00 with-connect-type hotplug'
    )

    def test_app_uses_the_injected_settings_object(self, tray_app, fake_settings) -> None:
        assert tray_app._settings is fake_settings

    def test_real_settings_class_is_never_constructed_when_injected(self, qapp, fake_client, fake_screensaver,
                                                                    fake_settings) -> None:
        from usbguard_gui.app import USBGuardTrayApp

        with (
            patch("usbguard_gui.app.USBGuardClient", return_value=fake_client),
            patch("usbguard_gui.app.ScreensaverMonitor", return_value=fake_screensaver),
            patch("usbguard_gui.app.Settings") as real_settings_cls,
        ):
            USBGuardTrayApp(qapp, settings=fake_settings)

        real_settings_cls.assert_not_called()

    def test_default_falls_back_to_the_qsettings_singleton(self, qapp, fake_client, fake_screensaver) -> None:
        """Production (main()) injects nothing and must still get the real store."""
        from usbguard_gui.app import USBGuardTrayApp
        from usbguard_gui.settings import Settings, SettingsProtocol

        with (
            patch("usbguard_gui.app.USBGuardClient", return_value=fake_client),
            patch("usbguard_gui.app.ScreensaverMonitor", return_value=fake_screensaver),
        ):
            app = USBGuardTrayApp(qapp)

        assert isinstance(app._settings, Settings)
        assert isinstance(app._settings, SettingsProtocol)

    def test_toggle_writes_through_to_the_injected_settings(self, tray_app, fake_settings) -> None:
        tray_app._on_disable_hid_toggled(True)

        assert fake_settings.write_calls == [True]
        assert fake_settings.disable_hid_treatment() is True

    def test_tray_menu_reflects_injected_initial_state(self, qapp, fake_client, fake_screensaver) -> None:
        from usbguard_gui.app import USBGuardTrayApp

        with (
            patch("usbguard_gui.app.USBGuardClient", return_value=fake_client),
            patch("usbguard_gui.app.ScreensaverMonitor", return_value=fake_screensaver),
        ):
            app = USBGuardTrayApp(qapp, settings=_FakeSettings(disable_hid_treatment=True))

        assert app._action_disable_hid.isChecked() is True

    def test_disabled_hid_treatment_comes_from_settings_not_user_config(self, qapp, fake_client,
                                                                        fake_screensaver) -> None:
        """With treatment disabled via the injected fake, a HID insert must skip
        the pending/lock flow — proving the flag drives behaviour from the
        injected object rather than from whatever the user's config holds."""
        from usbguard_gui.app import USBGuardTrayApp

        with (
            patch("usbguard_gui.app.USBGuardClient", return_value=fake_client),
            patch("usbguard_gui.app.ScreensaverMonitor", return_value=fake_screensaver),
        ):
            app = USBGuardTrayApp(qapp, settings=_FakeSettings(disable_hid_treatment=True))

        fake_client.device_presence_changed.emit(1, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK), self._RULE, {})

        assert app._hid_pending_devices == set()
        assert not app._hid_lock_timer.isActive()
        assert fake_client.apply_policy_calls == []
        app._quit()


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


class TestHIDRemovalCancelsScheduledLock:
    """Unplugging a HID device during the notification delay must abort the
    deferred screen lock and drop the device from the pending set, so it is
    neither locked for nor auto-allowed afterwards."""

    _RULE = (
        'block id 1234:abcd serial "" name "Test Keyboard" '
        'hash "abc123" parent-hash "" via-port "1-1" '
        'with-interface 03:00:00 with-connect-type hotplug'
    )

    def test_remove_before_lock_cancels_lock(self, tray_app, fake_client, fake_screensaver, qtbot) -> None:
        fake_client.device_presence_changed.emit(1, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK), self._RULE, {})
        assert tray_app._hid_pending_devices == {1}
        assert tray_app._hid_lock_timer.isActive()

        fake_client.device_presence_changed.emit(1, int(PresenceEvent.REMOVE), int(DeviceTarget.BLOCK), self._RULE, {})
        assert tray_app._hid_pending_devices == set()
        # A stopped single-shot timer can never fire — the lock is aborted.
        assert not tray_app._hid_lock_timer.isActive()
        assert fake_screensaver.lock_calls == 0

    def test_remove_one_of_several_keeps_lock(self, tray_app, fake_client, fake_screensaver) -> None:
        """With two HID devices pending, removing one keeps the lock scheduled."""
        other = (
            'block id 5678:ef01 serial "" name "Other Keyboard" '
            'hash "def456" parent-hash "" via-port "2-1" '
            'with-interface 03:00:00 with-connect-type hotplug'
        )
        fake_client.device_presence_changed.emit(1, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK), self._RULE, {})
        fake_client.device_presence_changed.emit(2, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK), other, {})
        assert tray_app._hid_pending_devices == {1, 2}

        fake_client.device_presence_changed.emit(1, int(PresenceEvent.REMOVE), int(DeviceTarget.BLOCK), self._RULE, {})
        assert tray_app._hid_pending_devices == {2}
        assert tray_app._hid_lock_timer.isActive()
        tray_app._hid_lock_timer.stop()  # don't leak a 5 s timer into later tests

    def test_removed_device_not_allowed_on_later_lock(self, tray_app, fake_client, fake_screensaver) -> None:
        """A removed device must not be auto-allowed if the screen locks later."""
        fake_client.device_presence_changed.emit(1, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK), self._RULE, {})
        fake_client.device_presence_changed.emit(1, int(PresenceEvent.REMOVE), int(DeviceTarget.BLOCK), self._RULE, {})

        tray_app._on_screensaver_locked(True)
        assert fake_client.apply_policy_calls == []


# ---------------------------------------------------------------------------
# HID allow on screen lock
# ---------------------------------------------------------------------------


class TestHIDAllowOnScreenLock:
    """Pending HID devices must be allowed when the screen locks so the user
    can unlock with the newly-attached keyboard."""

    def test_allows_pending_hid_devices_on_lock(self, tray_app, fake_client) -> None:
        tray_app._hid_pending_devices = {1}
        tray_app._on_screensaver_locked(True)
        assert fake_client.apply_policy_calls == [(1, DeviceTarget.ALLOW, False)]
        assert tray_app._hid_pending_devices == set()

    def test_allows_multiple_pending_devices(self, tray_app, fake_client) -> None:
        tray_app._hid_pending_devices = {1, 2, 3}
        tray_app._on_screensaver_locked(True)
        assert len(fake_client.apply_policy_calls) == 3
        for device_id in (1, 2, 3):
            assert (device_id, DeviceTarget.ALLOW, False) in fake_client.apply_policy_calls
        assert tray_app._hid_pending_devices == set()

    def test_no_pending_no_action(self, tray_app, fake_client) -> None:
        tray_app._hid_pending_devices = set()
        tray_app._on_screensaver_locked(True)
        assert fake_client.apply_policy_calls == []

    def test_does_not_fire_on_unlock(self, tray_app, fake_client) -> None:
        tray_app._hid_pending_devices = {1}
        tray_app._on_screensaver_locked(False)
        assert fake_client.apply_policy_calls == []
        assert tray_app._hid_pending_devices == {1}  # preserved for next lock

    def test_allows_only_matching_device_id(self, tray_app, fake_client) -> None:
        tray_app._hid_pending_devices = {1, 2}
        tray_app._on_screensaver_locked(True)
        assert len(fake_client.apply_policy_calls) == 2
        assert all(call[1] == DeviceTarget.ALLOW for call in fake_client.apply_policy_calls)
        assert all(call[2] is False for call in fake_client.apply_policy_calls)


# ---------------------------------------------------------------------------
# HID auto-allow from list_devices_result requires a locked screen
# ---------------------------------------------------------------------------


class TestHIDAllowRequiresLockedScreen:
    """A pending HID device may only be auto-allowed from a list_devices
    result while the screen is actually locked — that is the moment a
    newly-attached keyboard is safe to activate (unlocking requires a
    password).  While the screen is still unlocked the device must stay
    pending for the deferred lock (_on_screensaver_locked).

    Regression: a list_devices_result arriving during the 5 s lock delay
    window (e.g. the unlock deferred-summary check) used to allow the
    keyboard while the screen was unlocked and then skip the lock entirely
    because the pending set was empty — handing an attacker's keyboard
    keystrokes on an unlocked session.
    """

    _RULE_NON_HID = (
        'block id 04f2:b2ea serial "" name "Test Camera" '
        'hash "cam123" parent-hash "" via-port "1-2" '
        "with-interface 0e:01:00 with-connect-type hotplug"
    )

    def test_does_not_allow_pending_hid_while_screen_unlocked(self, tray_app, fake_client, fake_screensaver) -> None:
        """Screen unlocked + list_devices_result: pending HID stays pending and blocked."""
        device = _make_hid_device(1)
        tray_app._hid_pending_devices = {1}
        fake_screensaver._active = False  # screen is unlocked

        fake_client.list_devices_result.emit([device])

        assert fake_client.apply_policy_calls == []
        assert tray_app._hid_pending_devices == {1}  # still pending for the lock

    def test_race_hid_inserted_during_unlock_check(self, tray_app, fake_client, fake_screensaver) -> None:
        """End-to-end repro of the unlock-window race:

        1. screen locked, non-HID device A inserted  -> deferred
        2. screen unlocks                            -> deferred-summary check starts (list_devices in flight)
        3. attacker's keyboard B inserted while that result is in flight
        4. the in-flight result arrives (screen still unlocked)

        B must stay blocked+pending; A gets its deferred prompt.  B is only
        allowed once the deferred lock actually happens.
        """
        # 1. Screen locked, non-HID device A inserted -> deferred.
        fake_screensaver._active = True
        fake_client.device_presence_changed.emit(
            10, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK), self._RULE_NON_HID, {}
        )
        assert tray_app._screensaver_pending_devices == {10}

        # 2. Screen unlocks -> unlock cycle registered, its fetch in flight.
        fake_screensaver._active = False
        tray_app._on_screensaver_unlocked(False)
        assert tray_app._pending_unlock_cycles == {0: {10}}

        # 3. Attacker's keyboard B inserted while the result is in flight.
        fake_client.device_presence_changed.emit(
            1, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK),
            'block id 1234:abcd serial "" name "Test Keyboard" '
            'hash "abc123" parent-hash "" via-port "1-1" '
            "with-interface 03:00:00 with-connect-type hotplug",
            {},
        )
        assert tray_app._hid_pending_devices == {1}
        tray_app._hid_lock_timer.stop()  # don't let the 5 s lock timer fire in-test

        # 4. In-flight result arrives; the screen is still unlocked.
        device_a = Device.from_dbus(10, self._RULE_NON_HID)
        device_b = _make_hid_device(1)
        fake_client.list_devices_correlated.emit(0, [device_a, device_b])

        # B must stay blocked and pending — the pre-fix code allowed it here.
        assert fake_client.apply_policy_calls == []
        assert tray_app._hid_pending_devices == {1}
        # A still gets its deferred prompt.
        assert 10 in tray_app._open_dialogs

        # 5. The deferred lock happens -> B is allowed (the safe path).
        fake_screensaver._active = True
        tray_app._on_screensaver_locked(True)
        assert fake_client.apply_policy_calls == [(1, DeviceTarget.ALLOW, False)]

        for dialog in list(tray_app._open_dialogs.values()):
            dialog.close()


# ---------------------------------------------------------------------------
# Overlapping screensaver-unlock summary cycles
# ---------------------------------------------------------------------------


class TestOverlappingUnlockCycles:
    """The screensaver-unlock pending-id state must not be a single
    overwritable slot.

    Repro:
    1. Screen locked, device A inserted while away -> deferred.
    2. Screen unlocks -> list_devices() fired for A (R1 in flight).
    3. Before R1 returns, the screen locks again and device B is inserted
       while locked -> deferred.
    4. Screen unlocks again -> list_devices() fired for B (R2 in flight).
    5. R1 arrives (FIFO) -> must resolve against A, not whatever the latest
       pending set happens to be.
    6. R2 arrives -> must resolve against B, and must not have been dropped
       by R1 already consuming (and clearing) a shared single slot.
    """

    _RULE_A = (
        'block id 04f2:b2ea serial "" name "Device A" '
        'hash "aaa111" parent-hash "" via-port "1-2" '
        "with-interface 0e:01:00 with-connect-type hotplug"
    )
    _RULE_B = (
        'block id 04f2:b2eb serial "" name "Device B" '
        'hash "bbb222" parent-hash "" via-port "1-3" '
        "with-interface 0e:01:00 with-connect-type hotplug"
    )

    def test_two_overlapping_unlock_cycles_both_get_prompted(self, tray_app, fake_client, fake_screensaver) -> None:
        device_a = Device.from_dbus(10, self._RULE_A)
        device_b = Device.from_dbus(20, self._RULE_B)

        # 1. Screen locked, device A inserted while away -> deferred.
        tray_app._screensaver_pending_devices = {10}

        # 2. Screen unlocks -> cycle 0 registered, its fetch in flight.
        tray_app._on_screensaver_unlocked(False)
        assert fake_client.fetch_devices_calls == [0]

        # 3. Screen locks again; device B inserted while locked -> deferred.
        #    Cycle 0's fetch is still "in flight" (its result has not arrived).
        tray_app._screensaver_pending_devices = {20}

        # 4. Screen unlocks again -> cycle 1 registered and fetched, before
        #    cycle 0 resolved.
        tray_app._on_screensaver_unlocked(False)
        assert fake_client.fetch_devices_calls == [0, 1]

        # 5. Cycle 0's answer arrives first, carrying the snapshot taken for
        #    that request — must surface A's prompt.
        fake_client.list_devices_correlated.emit(0, [device_a])
        assert 10 in tray_app._open_dialogs, "A's deferred prompt must not be dropped"

        # 6. Cycle 1's answer arrives — must surface B's prompt too.
        fake_client.list_devices_correlated.emit(1, [device_a, device_b])
        assert 20 in tray_app._open_dialogs, "B's deferred prompt must not be dropped"

        for dialog in list(tray_app._open_dialogs.values()):
            dialog.close()

    def test_failed_result_does_not_consume_the_cycle(self, tray_app, fake_client, fake_screensaver) -> None:
        """An empty snapshot — the fetch fast-failed while the daemon was
        disconnected, or hit a DBusError — must not consume the registered
        cycle: it stays put so it can be retried."""
        device_a = Device.from_dbus(10, self._RULE_A)

        tray_app._screensaver_pending_devices = {10}
        tray_app._on_screensaver_unlocked(False)
        assert fake_client.fetch_devices_calls == [0]

        # The in-flight fetch fails (e.g. daemon briefly disconnected):
        # an empty snapshot arrives for cycle 0.
        fake_client.list_devices_correlated.emit(0, [])
        assert 10 not in tray_app._open_dialogs
        assert tray_app._pending_unlock_cycles == {0: {10}}, (
            "a failed result must not consume the registered cycle"
        )

        # The retry's answer surfaces A's prompt after all:
        fake_client.list_devices_correlated.emit(0, [device_a])
        assert 10 in tray_app._open_dialogs
        assert tray_app._pending_unlock_cycles == {}

        for dialog in list(tray_app._open_dialogs.values()):
            dialog.close()

    def test_stale_ids_dropped_when_device_gone(self, tray_app, fake_client, fake_screensaver) -> None:
        """If the deferred device is gone by the time the snapshot arrives
        (unplugged during the failed window), the stale id is resolved and
        dropped without prompting."""

        tray_app._screensaver_pending_devices = {10}
        tray_app._on_screensaver_unlocked(False)

        fake_client.list_devices_correlated.emit(0, [])
        # Next real snapshot does not contain A (it was unplugged):
        fake_client.list_devices_correlated.emit(0, [Device.from_dbus(30, self._RULE_B)])

        assert 10 not in tray_app._open_dialogs
        assert tray_app._pending_unlock_cycles == {}


# ---------------------------------------------------------------------------
# HID handling when screen lock is inhibited
# ---------------------------------------------------------------------------


class TestHIDWhenLockInhibited:
    """When a logind idle/block inhibitor is active, HID devices must not get
    auto-allow+lock treatment — doing so would hand an attached-keyboard
    attacker typed input with no password prompt to gate it. Instead the
    device must go through the same prompt path as the 'Disable special HID
    device treatment' setting."""

    def test_hid_insert_triggers_prompt_when_inhibited(self, tray_app, fake_client, fake_screensaver, qtbot) -> None:
        fake_screensaver._inhibited = True

        with patch.object(tray_app, "_show_device_dialog") as show_dialog:
            fake_client.device_presence_changed.emit(
                1, 1, int(DeviceTarget.BLOCK),
                'block id 1234:abcd serial "" name "Test Keyboard" '
                'hash "abc123" parent-hash "" via-port "1-1" '
                'with-interface 03:00:00 with-connect-type hotplug',
                {},
            )

        show_dialog.assert_called_once()
        assert fake_client.apply_policy_calls == []
        assert fake_screensaver.lock_calls == 0
        assert tray_app._hid_pending_devices == set()

    def test_hid_insert_auto_allows_when_not_inhibited(self, tray_app, fake_client, fake_screensaver, qtbot) -> None:
        """Regression guard: the inhibit check must not break the default HID path."""
        fake_screensaver._inhibited = False

        fake_client.device_presence_changed.emit(
            1, 1, int(DeviceTarget.BLOCK),
            'block id 1234:abcd serial "" name "Test Keyboard" '
            'hash "abc123" parent-hash "" via-port "1-1" '
            'with-interface 03:00:00 with-connect-type hotplug',
            {},
        )

        # Device is not in _permanent_allow_hashes → lock is scheduled (after a
        # short delay so the warning notification can be read) but device stays
        # blocked until the lock completes.
        assert 1 in tray_app._hid_pending_devices
        assert fake_client.apply_policy_calls == []
        qtbot.waitUntil(lambda: fake_screensaver.lock_calls == 1, timeout=8000)

    def test_hid_insert_while_locked_and_inhibited_prompts(self, tray_app, fake_client, fake_screensaver,
                                                           qtbot) -> None:
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


# ---------------------------------------------------------------------------
# HID permanently allowed devices
# ---------------------------------------------------------------------------


class TestHIDPermanentlyAllowed:
    """Permanently allowed HID devices must NOT trigger screen lock.

    The app caches hashes of permanent allow rules from the daemon's
    policy (fetched on connect).  When a HID INSERT arrives, the device
    hash is checked against this cache — if it matches, HID treatment is
    skipped.
    """

    def test_allowed_hid_skipped_when_target_allow(self, tray_app, fake_client, fake_screensaver, qtbot) -> None:
        """When target=ALLOW, the device is skipped entirely (existing behaviour)."""
        fake_client.device_presence_changed.emit(
            1, 1, int(DeviceTarget.ALLOW),
            'block id 1234:abcd serial "" name "Test Keyboard" '
            'hash "abc123" parent-hash "" via-port "1-1" '
            'with-interface 03:00:00 with-connect-type hotplug',
            {},
        )

        assert tray_app._hid_pending_devices == set()
        assert fake_screensaver.lock_calls == 0

    def test_allowed_hid_skipped_when_hash_in_cache(self, tray_app, fake_client, fake_screensaver, qtbot) -> None:
        """A HID device whose hash matches _permanent_allow_hashes must not
        trigger the screen lock, even though target=BLOCK."""
        tray_app._permanent_allow_hashes.add("abc123")

        fake_client.device_presence_changed.emit(
            1, 1, int(DeviceTarget.BLOCK),
            'block id 1234:abcd serial "" name "Test Keyboard" '
            'hash "abc123" parent-hash "" via-port "1-1" '
            'with-interface 03:00:00 with-connect-type hotplug',
            {},
        )

        assert tray_app._hid_pending_devices == set()
        assert fake_screensaver.lock_calls == 0
        assert fake_client.apply_policy_calls == []

    def test_allowed_hid_skipped_when_hash_in_cache_multiple(self, tray_app, fake_client, fake_screensaver,
                                                             qtbot) -> None:
        """Multiple hashes in the cache: matching device is skipped,
        non-matching device still triggers lock."""
        tray_app._permanent_allow_hashes.add("abc123")
        tray_app._permanent_allow_hashes.add("xyz789")

        fake_client.device_presence_changed.emit(
            1, 1, int(DeviceTarget.BLOCK),
            'block id 1234:abcd serial "" name "Test Keyboard" '
            'hash "abc123" parent-hash "" via-port "1-1" '
            'with-interface 03:00:00 with-connect-type hotplug',
            {},
        )

        assert tray_app._hid_pending_devices == set()
        assert fake_screensaver.lock_calls == 0

    def test_allowed_hid_without_hash_in_cache_triggers_lock(self, tray_app, fake_client, fake_screensaver,
                                                             qtbot) -> None:
        """A HID device whose hash is NOT in the cache triggers the lock."""
        tray_app._permanent_allow_hashes.add("other_hash")

        fake_client.device_presence_changed.emit(
            1, 1, int(DeviceTarget.BLOCK),
            'block id 1234:abcd serial "" name "Test Keyboard" '
            'hash "abc123" parent-hash "" via-port "1-1" '
            'with-interface 03:00:00 with-connect-type hotplug',
            {},
        )

        assert 1 in tray_app._hid_pending_devices
        qtbot.waitUntil(lambda: fake_screensaver.lock_calls == 1, timeout=8000)

    def test_allowed_hid_empty_hash_not_matched(self, tray_app, fake_client, fake_screensaver, qtbot) -> None:
        """A device with an empty hash (malformed rule) must not be skipped
        even if the cache has entries."""
        tray_app._permanent_allow_hashes.add("abc123")

        fake_client.device_presence_changed.emit(
            1, 1, int(DeviceTarget.BLOCK),
            'block id 1234:abcd serial "" name "No Hash" '
            'hash "" parent-hash "" via-port "1-1" '
            'with-interface 03:00:00 with-connect-type hotplug',
            {},
        )

        assert 1 in tray_app._hid_pending_devices
        qtbot.waitUntil(lambda: fake_screensaver.lock_calls == 1, timeout=8000)

    def test_blocked_hid_device_still_triggers_lock(self, tray_app, fake_client, fake_screensaver, qtbot) -> None:
        """Regression guard: a blocked HID device (not in cache) triggers the
        screen lock immediately."""
        fake_client.device_presence_changed.emit(
            2, 1, int(DeviceTarget.BLOCK),
            'block id 5678:ef01 serial "" name "Unknown Keyboard" '
            'hash "def456" parent-hash "" via-port "2-1" '
            'with-interface 03:00:00 with-connect-type hotplug',
            {},
        )

        assert 2 in tray_app._hid_pending_devices
        qtbot.waitUntil(lambda: fake_screensaver.lock_calls == 1, timeout=8000)

    def test_cache_seeded_by_policy_changed_with_rule_id(self, tray_app, fake_client, fake_screensaver, qtbot) -> None:
        """When DevicePolicyChanged fires with ALLOW and rule_id>0, the
        device's hash is added to the cache for future insertions."""
        fake_client.device_policy_changed.emit(
            1,
            int(DeviceTarget.BLOCK),
            int(DeviceTarget.ALLOW),
            'allow id 1234:abcd serial "" name "Test Keyboard" '
            'hash "abc123" parent-hash "" via-port "1-1" '
            'with-interface 03:00:00 with-connect-type hotplug',
            5,
            {},
        )

        assert "abc123" in tray_app._permanent_allow_hashes

    def test_cache_not_seeded_by_policy_changed_without_rule_id(self, tray_app, fake_client, fake_screensaver,
                                                                qtbot) -> None:
        """PolicyChanged with ALLOW but rule_id==0 (temporary rule) must not
        seed the cache."""
        fake_client.device_policy_changed.emit(
            1,
            int(DeviceTarget.BLOCK),
            int(DeviceTarget.ALLOW),
            'allow id 1234:abcd serial "" name "Test Keyboard" '
            'hash "abc123" parent-hash "" via-port "1-1" '
            'with-interface 03:00:00 with-connect-type hotplug',
            0,
            {},
        )

        assert "abc123" not in tray_app._permanent_allow_hashes


# ---------------------------------------------------------------------------
# Reconnect backoff
# ---------------------------------------------------------------------------


class TestReconnectBackoff:
    """The reconnection timer must use exponential backoff: each failed
    attempt doubles the interval up to RECONNECT_MAX_INTERVAL, and a
    successful connection resets it.

    Regression: connect() always returns True (it only starts the worker
    thread), so the backoff branch in _try_connect() was unreachable and
    the timer retried at a fixed 5 s interval forever."""

    def test_backoff_doubles_after_each_failure(self, tray_app, fake_client) -> None:
        fake_client.connection_changed.emit(False)
        assert tray_app._reconnect_timer.interval() == 5000
        assert tray_app._reconnect_timer.isActive()

        # The single-shot timer fires (stopping itself) and calls _try_connect.
        tray_app._reconnect_timer.stop()
        tray_app._try_connect()

        fake_client.connection_changed.emit(False)
        assert tray_app._reconnect_timer.interval() == 10000

        tray_app._reconnect_timer.stop()
        tray_app._try_connect()

        fake_client.connection_changed.emit(False)
        assert tray_app._reconnect_timer.interval() == 20000

    def test_backoff_caps_at_max_interval(self, tray_app, fake_client) -> None:
        from usbguard_gui.app import RECONNECT_MAX_INTERVAL

        tray_app._reconnect_attempts = 5  # 5 * 2^5 = 160 s > cap
        fake_client.connection_changed.emit(False)
        assert tray_app._reconnect_timer.interval() == RECONNECT_MAX_INTERVAL * 1000

    def test_success_resets_backoff(self, tray_app, fake_client) -> None:
        fake_client.connection_changed.emit(False)
        assert tray_app._reconnect_timer.interval() == 5000
        tray_app._reconnect_timer.stop()

        fake_client.connection_changed.emit(True)
        assert tray_app._reconnect_timer.isActive() is False
        assert tray_app._reconnect_attempts == 0

        fake_client.connection_changed.emit(False)
        assert tray_app._reconnect_timer.interval() == 5000  # backoff restarted


# ---------------------------------------------------------------------------
# Lock availability: all allow/deny functionality must be disabled when the
# screen cannot be locked — allowing a keyboard without the lock-first
# guarantee is exactly the attack this app exists to prevent.
# ---------------------------------------------------------------------------


class TestLockAvailability:
    """The app must track whether screen locking is available and, when it
    is not: (a) not schedule the HID lock flow (the 'Locking screen…' notice
    would be a lie and the pending devices could never be auto-allowed),
    (b) tell the user, and (c) leave every policy action to the disabled UI."""

    _HID_RULE = (
        'block id 1234:abcd serial "" name "Test Keyboard" '
        'hash "abc123" parent-hash "" via-port "1-1" '
        "with-interface 03:00:00 with-connect-type hotplug"
    )

    def test_tracks_lock_availability(self, tray_app, fake_screensaver) -> None:
        assert tray_app._lock_available is True

        fake_screensaver.connection_changed.emit(False)
        assert tray_app._lock_available is False

        fake_screensaver.connection_changed.emit(True)
        assert tray_app._lock_available is True

    def test_first_unavailable_report_notifies(self, tray_app, fake_screensaver, mocker) -> None:
        """When lock availability is confirmed down (first report), the user
        must be told — the actions are being disabled under their feet."""
        notify = mocker.patch.object(tray_app._tray, "showMessage")
        fake_screensaver._connected = False
        fake_screensaver.connection_changed.emit(False)

        assert notify.called

    def test_repeated_same_state_does_not_notify(self, tray_app, fake_screensaver, mocker) -> None:
        notify = mocker.patch.object(tray_app._tray, "showMessage")
        fake_screensaver.connection_changed.emit(False)
        fake_screensaver.connection_changed.emit(False)

        assert notify.call_count == 1

    def test_hid_insert_prompts_when_lock_unavailable(self, tray_app, fake_client, fake_screensaver) -> None:
        """No lock flow: the HID device goes through the normal prompt path
        (whose actions are disabled) instead of the deferred lock."""
        fake_screensaver.connection_changed.emit(False)

        with patch.object(tray_app, "_show_device_dialog") as show_dialog:
            fake_client.device_presence_changed.emit(1, 1, int(DeviceTarget.BLOCK), self._HID_RULE, {})

        show_dialog.assert_called_once()
        assert fake_client.apply_policy_calls == []
        assert fake_screensaver.lock_calls == 0
        assert tray_app._hid_pending_devices == set()

    def test_hid_lock_timer_skipped_when_lock_unavailable(self, tray_app, fake_client, fake_screensaver) -> None:
        """If availability drops while a deferred lock is in flight, the lock
        must not be claimed — the pending devices stay blocked."""
        tray_app._hid_pending_devices = {1}
        fake_screensaver.connection_changed.emit(False)

        tray_app._lock_for_pending_hid()

        assert fake_screensaver.lock_calls == 0
        assert tray_app._hid_pending_devices == {1}

    def test_hid_pending_flow_still_works_when_available(self, tray_app, fake_client, fake_screensaver, qtbot) -> None:
        """Regression guard: with lock available the pending+lock flow is
        unchanged."""
        fake_client.device_presence_changed.emit(1, 1, int(DeviceTarget.BLOCK), self._HID_RULE, {})

        assert 1 in tray_app._hid_pending_devices
        qtbot.waitUntil(lambda: fake_screensaver.lock_calls == 1, timeout=8000)


# ---------------------------------------------------------------------------
# About dialog: QMessageBox.about() expects a QWidget parent, not a
# QSystemTrayIcon.  Passing the tray icon raises a TypeError inside the
# slot, which PyQt6 escalates to qFatal → abort on the second invocation.
# ---------------------------------------------------------------------------


class TestShowAbout:
    """The About dialog must not pass a non-QWidget as parent to
    QMessageBox.about() — QSystemTrayIcon is a QObject, not a QWidget.
    On the first click the TypeError is printed to stderr (dialog never
    appears); on the second click PyQt6's error handler itself hits a
    deleted object and aborts the process."""

    def test_about_parent_is_widget_or_none(self, tray_app, mocker) -> None:
        """QMessageBox.about must be called with None or a QWidget as parent.
        Calling it twice matches the user's crash repro (first is a no-op,
        second aborts)."""
        from PyQt6.QtWidgets import QWidget

        mock_about = mocker.patch("usbguard_gui.app.QMessageBox.about")

        # Trigger the About action twice — the second call is what aborts
        # in production.
        tray_app._show_about()
        tray_app._show_about()

        assert mock_about.call_count == 2
        for call in mock_about.call_args_list:
            parent = call.args[0]
            assert parent is None or isinstance(parent, QWidget), (
                f"QMessageBox.about called with {type(parent).__name__} as parent, "
                "expected None or QWidget"
            )

    def test_about_dialog_shows(self, tray_app, mocker) -> None:
        """The About action must actually show a message box (not silently
        fail due to a type error)."""
        mock_about = mocker.patch("usbguard_gui.app.QMessageBox.about", return_value=0)

        tray_app._show_about()

        mock_about.assert_called_once()


class TestHIDLockTimerNotExtendedByLaterInserts:
    """The deferred lock is one shared single-shot timer.  A second HID insert
    must not restart it, or every additional keyboard would push the first
    device's lock back by another full HID_LOCK_NOTIFY_DELAY_MS."""

    _RULE_A = (
        'block id 1234:abcd serial "" name "Keyboard A" '
        'hash "aaa111" parent-hash "" via-port "1-1" '
        'with-interface 03:00:00 with-connect-type hotplug'
    )
    _RULE_B = (
        'block id 5678:ef01 serial "" name "Keyboard B" '
        'hash "bbb222" parent-hash "" via-port "2-1" '
        'with-interface 03:00:00 with-connect-type hotplug'
    )

    def _insert(self, fake_client, number: int, rule: str) -> None:
        fake_client.device_presence_changed.emit(number, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK), rule, {})

    def test_second_insert_does_not_push_the_lock_back(self, tray_app, fake_client) -> None:
        self._insert(fake_client, 1, self._RULE_A)
        first_remaining = tray_app._hid_lock_timer.remainingTime()
        assert first_remaining > 0

        self._insert(fake_client, 2, self._RULE_B)

        assert tray_app._hid_pending_devices == {1, 2}
        assert tray_app._hid_lock_timer.remainingTime() <= first_remaining, (
            "a later HID insert must not extend the first device's lock delay"
        )
        tray_app._hid_lock_timer.stop()  # don't leak a 5 s timer into later tests

    def test_the_earliest_scheduled_lock_covers_every_pending_device(self, tray_app, fake_client,
                                                                  fake_screensaver) -> None:
        self._insert(fake_client, 1, self._RULE_A)
        self._insert(fake_client, 2, self._RULE_B)
        tray_app._hid_lock_timer.stop()

        tray_app._lock_for_pending_hid()
        assert fake_screensaver.lock_calls == 1

        tray_app._on_screensaver_locked(True)
        assert sorted(fake_client.apply_policy_calls) == sorted(
            [(1, DeviceTarget.ALLOW, False), (2, DeviceTarget.ALLOW, False)]
        )


class TestUnlockQueueCap:
    """Queued unlock cycles are only consumed by a non-empty snapshot, so a
    daemon that stays down across many lock/unlock cycles must not grow the
    queue without bound."""

    def test_oldest_cycle_is_dropped_once_the_cap_is_reached(self, tray_app, monkeypatch) -> None:
        monkeypatch.setattr("usbguard_gui.app.MAX_PENDING_UNLOCK_CYCLES", 3)

        for i in range(1, 7):
            tray_app._screensaver_pending_devices = {i}
            tray_app._on_screensaver_unlocked(False)

        assert len(tray_app._pending_unlock_cycles) == 3
        assert tray_app._pending_unlock_cycles == {3: {4}, 4: {5}, 5: {6}}

    def test_nothing_dropped_below_the_cap(self, tray_app, monkeypatch) -> None:
        monkeypatch.setattr("usbguard_gui.app.MAX_PENDING_UNLOCK_CYCLES", 10)

        for i in range(1, 4):
            tray_app._screensaver_pending_devices = {i}
            tray_app._on_screensaver_unlocked(False)

        assert tray_app._pending_unlock_cycles == {0: {1}, 1: {2}, 2: {3}}


class TestModuleEntryPoint:
    """python -m usbguard_gui must reach app.main() (the module was previously
    at 0% coverage, so nothing proved the entry point was wired up)."""

    def test_run_module_calls_main(self) -> None:
        import runpy

        with patch("usbguard_gui.app.main") as main:
            runpy.run_module("usbguard_gui.__main__", run_name="__main__")

        main.assert_called_once_with()


class TestUnlockQueueRaceReproductions:
    """AUDIT follow-ups #2 and #3 — regression guards for the correlated fetch.

    Both were verified red against the old FIFO unlock queue, which attributed
    the k-th queued id-set to the k-th ``list_devices_result`` arriving while it
    was non-empty.  That held only if every result on that signal belonged to an
    unlock cycle and answers arrived in call order; the device-list window issues
    its own ``list_devices()`` on the same signal, and D-Bus gives no ordering
    guarantee, so prompts were silently dropped.

    Fixed by giving each unlock cycle its own ``fetch_devices(request_id)`` and
    resolving answers from ``list_devices_correlated(request_id, devices)``.
    """

    _RULE_A = (
        'block id 04f2:b2ea serial "" name "Device A" hash "aaa111" '
        'parent-hash "" via-port "1-2" with-interface 0e:01:00 with-connect-type hotplug'
    )
    _RULE_B = (
        'block id 04f2:b2eb serial "" name "Device B" hash "bbb222" '
        'parent-hash "" via-port "1-3" with-interface 0e:01:00 with-connect-type hotplug'
    )

    def _queue_two_cycles(self, tray_app) -> None:
        """Cycle 1 defers device 10 and unlocks; then cycle 2 defers device 20
        and unlocks, before cycle 1's result has arrived."""
        tray_app._screensaver_pending_devices = {10}
        tray_app._on_screensaver_unlocked(False)
        tray_app._screensaver_pending_devices = {20}
        tray_app._on_screensaver_unlocked(False)

    def test_foreign_refresh_must_not_consume_a_queued_cycle(self, tray_app, fake_client) -> None:
        """AUDIT follow-up #2: a foreign device-list refresh must not be taken
        as the answer to a queued unlock cycle."""
        a = Device.from_dbus(10, self._RULE_A)
        b = Device.from_dbus(20, self._RULE_B)
        self._queue_two_cycles(tray_app)

        # Cycle 0's own answer arrives and resolves A.
        fake_client.list_devices_correlated.emit(0, [a])
        assert 10 in tray_app._open_dialogs

        # A foreign refresh, snapshotted before B was ever plugged in, lands
        # between the two cycle answers.  It arrives on the uncorrelated signal
        # the device-list window uses, so it cannot touch the unlock cycles.
        fake_client.list_devices_result.emit([a])

        # Cycle 1's answer arrives, and it does contain B.
        fake_client.list_devices_correlated.emit(1, [a, b])
        assert 20 in tray_app._open_dialogs, "B's prompt must survive a foreign snapshot"

    def test_out_of_order_results_must_not_cross_cycles(self, tray_app, fake_client) -> None:
        """AUDIT follow-up #3: answers may arrive in any order and still resolve
        their own cycle."""
        a = Device.from_dbus(10, self._RULE_A)
        b = Device.from_dbus(20, self._RULE_B)
        self._queue_two_cycles(tray_app)

        # Cycle 1's answer arrives first.  Matching is by request id, so cycle 0
        # is never matched against a snapshot that only holds B, or vice versa.
        fake_client.list_devices_correlated.emit(1, [b])
        fake_client.list_devices_correlated.emit(0, [a])

        assert 10 in tray_app._open_dialogs, "A (cycle 0) must be prompted"
        assert 20 in tray_app._open_dialogs, "B (cycle 1) must be prompted"

    def test_unknown_request_id_is_ignored(self, tray_app, fake_client) -> None:
        """A correlated answer for a request nobody registered must be dropped,
        not guessed at."""
        a = Device.from_dbus(10, self._RULE_A)

        fake_client.list_devices_correlated.emit(99, [a])

        assert tray_app._open_dialogs == {}
        assert tray_app._pending_unlock_cycles == {}

    def test_outstanding_cycles_are_retried_on_reconnect(self, tray_app, fake_client) -> None:
        """A cycle whose fetch failed while the daemon was down is re-fetched when
        the connection returns, so the prompt is not lost with the outage."""
        device_a = Device.from_dbus(10, self._RULE_A)

        tray_app._screensaver_pending_devices = {10}
        tray_app._on_screensaver_unlocked(False)
        fake_client.list_devices_correlated.emit(0, [])  # daemon was down
        assert tray_app._pending_unlock_cycles == {0: {10}}

        fake_client.connection_changed.emit(True)
        assert fake_client.fetch_devices_calls == [0, 0], "the outstanding cycle must be re-fetched"

        fake_client.list_devices_correlated.emit(0, [device_a])
        assert 10 in tray_app._open_dialogs

        for dialog in list(tray_app._open_dialogs.values()):
            dialog.close()

    def test_reconnect_with_no_outstanding_cycles_fetches_nothing(self, tray_app, fake_client) -> None:
        fake_client.connection_changed.emit(True)
        assert fake_client.fetch_devices_calls == []
