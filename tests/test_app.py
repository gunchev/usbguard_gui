"""Tests for the main application module."""

from __future__ import annotations

import os
import signal
import time
from unittest.mock import MagicMock, patch

import pytest
from conftest import IR_RULE, KEYBOARD_RULE
from fakes import _FakeSettings

from usbguard_gui.app import PROMPT_COOLDOWN_SEC, USBGuardTrayApp
from usbguard_gui.decision import dialog_identity
from usbguard_gui.device import Device, DeviceTarget, Persistence, PresenceEvent

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
# Single-instance lock lifetime
# ---------------------------------------------------------------------------


class TestQuitUnlocksInstanceLock:
    """_quit() must explicitly unlock() the single-instance QLockFile rather
    than relying on process exit to release it — so a future refactor that
    moves _quit()'s callers out of the stack frame holding the lock can't
    silently skip releasing it."""

    def test_quit_unlocks_instance_lock(self, qapp, fake_client, fake_screensaver, fake_settings) -> None:

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

        with (
            patch("usbguard_gui.app.USBGuardClient", return_value=fake_client),
            patch("usbguard_gui.app.ScreensaverMonitor", return_value=fake_screensaver),
            patch("usbguard_gui.app.Settings") as real_settings_cls,
        ):
            USBGuardTrayApp(qapp, settings=fake_settings)

        real_settings_cls.assert_not_called()

    def test_default_falls_back_to_the_qsettings_singleton(self, qapp, fake_client, fake_screensaver) -> None:
        """Production (main()) injects nothing and must still get the real store."""
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

        with (
            patch("usbguard_gui.app.USBGuardClient", return_value=fake_client),
            patch("usbguard_gui.app.ScreensaverMonitor", return_value=fake_screensaver),
        ):
            app = USBGuardTrayApp(qapp, settings=_FakeSettings(disable_hid_treatment=True))

        fake_client.device_presence_changed.emit(1, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK), self._RULE, {})

        assert app._engine._hid_pending_devices == set()
        assert not app._hid_lock_timer.isActive()
        assert fake_client.apply_policy_calls == []
        app._quit()


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


class TestModuleEntryPoint:
    """python -m usbguard_gui must reach app.main() (the module was previously
    at 0% coverage, so nothing proved the entry point was wired up)."""

    def test_run_module_calls_main(self) -> None:
        import runpy

        with patch("usbguard_gui.app.main") as main:
            runpy.run_module("usbguard_gui.__main__", run_name="__main__")

        main.assert_called_once_with()


class TestBroaderRuleWarning:
    """A `Once` that leaves a wildcard rule in force must be announced.

    Nothing failed -- the device's own rule was cleared.  But a broader rule
    still governs the device, so it remains permanently allowed while the user
    believes they just made a temporary decision.  Silence here is the whole
    defect; the rule is deliberately left alone.
    """

    def test_the_tray_names_the_rule_that_remains(self, tray_app, mocker):
        show = mocker.patch.object(tray_app._tray, "showMessage")
        tray_app._on_permanent_rule_remains(54, "allow", "allow id 2109:2817")

        title, body = show.call_args[0][0], show.call_args[0][1]
        assert "Temporary decision incomplete" in title
        assert "allow id 2109:2817" in body, "the user must be able to find the rule"
        assert "permanently allow" in body

    def test_the_signal_is_wired_through_to_the_tray(self, tray_app, mocker):
        """Wiring, not just the handler -- an unconnected signal is silent."""
        show = mocker.patch.object(tray_app._tray, "showMessage")
        tray_app._client.permanent_rule_remains.emit(54, "block", "block id 2109:2817")

        assert show.called
        assert "block id 2109:2817" in show.call_args[0][1]


class TestReappearingDeviceDoesNotStackDialogs:
    """A device that re-enumerates must not produce a notification storm.

    The daemon assigns a fresh device number on every insertion, so the old
    number-keyed dedup check let a flapping device stack one notification and
    one dialog per landing.  Observed live with an ELKSMART Smart IR Blaster,
    which resets when it is configured: three "New USB device inserted"
    notices deep for one physical device, none of them a new device.
    """

    _IR = (
        'block id 1234:5678 serial "IR1" name "Smart IR Blaster" hash "irhash1" '
        'parent-hash "" via-port "2-1" with-interface ff:00:00 with-connect-type "hotplug"'
    )

    def test_a_new_device_number_for_the_same_device_does_not_stack(self, tray_app, mocker) -> None:
        show = mocker.patch.object(tray_app._tray, "showMessage")
        tray_app._show_device_dialog(Device.from_dbus(174, self._IR))
        assert len(tray_app._open_dialogs) == 1
        assert show.call_count == 1

        tray_app._show_device_dialog(Device.from_dbus(179, self._IR))

        assert len(tray_app._open_dialogs) == 1, "re-enumeration must not stack a second dialog"
        assert show.call_count == 1, "and must not notify again"

    def test_a_different_device_still_gets_its_own_dialog(self, tray_app, mocker) -> None:
        mocker.patch.object(tray_app._tray, "showMessage")
        other = self._IR.replace('hash "irhash1"', 'hash "otherhash"')
        tray_app._show_device_dialog(Device.from_dbus(1, self._IR))
        tray_app._show_device_dialog(Device.from_dbus(2, other))
        assert len(tray_app._open_dialogs) == 2

    def test_closing_the_dialog_does_not_let_the_next_flap_prompt_immediately(self, tray_app, mocker) -> None:
        """An explicit dismissal keeps a flapping device quiet for the cooldown."""
        mocker.patch.object(tray_app._tray, "showMessage")
        tray_app._show_device_dialog(Device.from_dbus(1, self._IR))
        tray_app._open_dialogs[1].reject()

        tray_app._show_device_dialog(Device.from_dbus(2, self._IR))

        assert 2 not in tray_app._open_dialogs, "still inside the cooldown"

    @pytest.mark.parametrize("persistence", [Persistence.ONCE, Persistence.ALWAYS])
    @pytest.mark.parametrize("allow_confirmed", [False, True])
    def test_a_blocked_return_after_allow_prompts_immediately(self, tray_app, fake_client, mocker,
                                                              persistence, allow_confirmed) -> None:
        """Allow Once expires on disconnect; a failed Always must also be recoverable."""
        show = mocker.patch.object(tray_app._tray, "showMessage")
        mocker.patch("usbguard_gui.app.time.monotonic", return_value=100.0)
        tray_app._on_device_presence_changed(154, PresenceEvent.INSERT, DeviceTarget.BLOCK, self._IR, {})
        tray_app._open_dialogs[154]._choose(DeviceTarget.ALLOW, persistence)
        if allow_confirmed:
            allowed_rule = self._IR.replace("block ", "allow ", 1)
            fake_client.device_policy_changed.emit(154, DeviceTarget.BLOCK, DeviceTarget.ALLOW, allowed_rule, 0, {})

        tray_app._on_device_presence_changed(154, PresenceEvent.REMOVE, DeviceTarget.ALLOW, self._IR, {})
        tray_app._on_device_presence_changed(155, PresenceEvent.INSERT, DeviceTarget.BLOCK, self._IR, {})

        assert 155 in tray_app._open_dialogs, "a new blocked insertion needs a fresh decision, without waiting 30s"
        assert show.call_count == 2
        assert fake_client.apply_policy_calls == [(154, DeviceTarget.ALLOW, persistence)], "no automatic replay"
        dialog = tray_app._open_dialogs[155]
        tray_app._on_device_presence_changed(155, PresenceEvent.REMOVE, DeviceTarget.BLOCK, self._IR, {})
        tray_app._on_device_presence_changed(156, PresenceEvent.INSERT, DeviceTarget.BLOCK, self._IR, {})
        assert tray_app._open_dialogs == {156: dialog}, "further flaps reuse the new dialog"
        assert show.call_count == 2

    @pytest.mark.parametrize("persistence", [Persistence.ONCE, Persistence.ALWAYS])
    def test_a_block_choice_keeps_the_next_flap_quiet(self, tray_app, mocker, persistence) -> None:
        mocker.patch.object(tray_app._tray, "showMessage")
        mocker.patch("usbguard_gui.app.time.monotonic", return_value=100.0)
        tray_app._on_device_presence_changed(1, PresenceEvent.INSERT, DeviceTarget.BLOCK, self._IR, {})
        tray_app._open_dialogs[1]._choose(DeviceTarget.BLOCK, persistence)

        tray_app._on_device_presence_changed(1, PresenceEvent.REMOVE, DeviceTarget.BLOCK, self._IR, {})
        tray_app._on_device_presence_changed(2, PresenceEvent.INSERT, DeviceTarget.BLOCK, self._IR, {})

        assert tray_app._open_dialogs == {}, "the user asked to keep this device blocked"

    def test_an_external_allow_does_not_suppress_the_next_blocked_return(self, tray_app, fake_client, mocker) -> None:
        mocker.patch.object(tray_app._tray, "showMessage")
        mocker.patch("usbguard_gui.app.time.monotonic", return_value=100.0)
        tray_app._on_device_presence_changed(1, PresenceEvent.INSERT, DeviceTarget.BLOCK, self._IR, {})
        allowed_rule = self._IR.replace("block ", "allow ", 1)
        fake_client.device_policy_changed.emit(1, DeviceTarget.BLOCK, DeviceTarget.ALLOW, allowed_rule, 0, {})
        assert tray_app._open_dialogs == {}

        tray_app._on_device_presence_changed(1, PresenceEvent.REMOVE, DeviceTarget.ALLOW, allowed_rule, {})
        tray_app._on_device_presence_changed(2, PresenceEvent.INSERT, DeviceTarget.BLOCK, self._IR, {})

        assert 2 in tray_app._open_dialogs
        assert fake_client.apply_policy_calls == [], "observing an allow must not apply one"

    def test_after_the_cooldown_expires_the_next_landing_prompts_again(self, tray_app, mocker) -> None:
        """The cooldown is a quiet period, not a permanent silence -- a device
        that comes back later is still worth asking about."""
        mocker.patch.object(tray_app._tray, "showMessage")
        tray_app._show_device_dialog(Device.from_dbus(1, self._IR))
        tray_app._open_dialogs[1].reject()

        identity = dialog_identity(Device.from_dbus(1, self._IR))
        tray_app._engine._last_prompted_at[identity] = time.monotonic() - (PROMPT_COOLDOWN_SEC + 1)

        tray_app._show_device_dialog(Device.from_dbus(2, self._IR))

        assert 2 in tray_app._open_dialogs

    def test_devices_without_a_hash_dedup_on_id_and_port(self, tray_app, mocker) -> None:
        mocker.patch.object(tray_app._tray, "showMessage")
        no_hash = ('block id abcd:1234 serial "" name "Gadget" hash "" parent-hash "" '
                   'via-port "3-2" with-interface ff:00:00 with-connect-type "hotplug"')
        tray_app._show_device_dialog(Device.from_dbus(1, no_hash))
        tray_app._show_device_dialog(Device.from_dbus(2, no_hash))
        assert len(tray_app._open_dialogs) == 1

    def test_reenumeration_keeps_the_identity_key(self) -> None:
        device = Device.from_dbus(1, self._IR)
        reappeared = Device.from_dbus(2, self._IR)
        assert dialog_identity(device) == dialog_identity(reappeared)


class TestDialogTracksTheNewestInstance:
    """A click must act on the device that exists *now*.

    Observed live: Allow Once on a flapping Smart IR Blaster failed with
    "Device lookup: device id: id doesn't exist".  The dialog held the device
    number captured when it opened, and that incarnation was long gone by the
    time the user clicked -- so the click failed even though the user did
    everything right.
    """

    _IR = (
        'block id 045c:0131 serial "IR1" name "Smart IR Blaster" hash "irhash1" '
        'parent-hash "" via-port "1-2" with-interface ff:00:00 with-connect-type "hotplug"'
    )

    def test_a_reappearance_retargets_the_open_dialog(self, tray_app, mocker) -> None:
        mocker.patch.object(tray_app._tray, "showMessage")
        tray_app._show_device_dialog(Device.from_dbus(247, self._IR))
        dialog = tray_app._open_dialogs[247]

        tray_app._show_device_dialog(Device.from_dbus(264, self._IR))

        assert dialog.device.number == 264
        assert 247 not in tray_app._open_dialogs, "the stale key must not linger"
        assert tray_app._open_dialogs[264] is dialog

    def test_the_click_applies_to_the_current_number(self, tray_app, fake_client, mocker) -> None:
        mocker.patch.object(tray_app._tray, "showMessage")
        tray_app._show_device_dialog(Device.from_dbus(247, self._IR))
        dialog = tray_app._open_dialogs[247]
        tray_app._show_device_dialog(Device.from_dbus(264, self._IR))

        dialog._choose(DeviceTarget.ALLOW, Persistence.ONCE)

        assert fake_client.apply_policy_calls[-1][0] == 264, "must not act on the stale 247"

    def test_the_click_uses_the_current_raw_rule(self, tray_app, fake_client, mocker) -> None:
        """`Once` derives the deletion identity from the rule string, so a stale
        rule would clear the wrong thing."""
        mocker.patch.object(tray_app._tray, "showMessage")
        updated = self._IR.replace('with-connect-type "hotplug"', 'with-connect-type "unknown"')
        tray_app._show_device_dialog(Device.from_dbus(1, self._IR))
        dialog = tray_app._open_dialogs[1]
        tray_app._show_device_dialog(Device.from_dbus(2, updated))

        dialog._choose(DeviceTarget.ALLOW, Persistence.ONCE)

        assert fake_client.apply_policy_rules[-1] == updated

    def test_no_reappearance_still_uses_the_original(self, tray_app, fake_client, mocker) -> None:
        mocker.patch.object(tray_app._tray, "showMessage")
        tray_app._show_device_dialog(Device.from_dbus(7, self._IR))
        dialog = tray_app._open_dialogs[7]

        dialog._choose(DeviceTarget.BLOCK, Persistence.ALWAYS)

        assert fake_client.apply_policy_calls[-1][0] == 7


class TestRetainedDialogsOnEarlyReturnPaths:
    """Every insertion updates a retained dialog before choosing a default flow."""

    @pytest.mark.parametrize("return_path", ["allowed", "hid_lock", "locked_session"])
    def test_current_instance_is_used_and_a_click_cancels_pending_defaults(self, tray_app, fake_client,
                                                                           fake_screensaver, return_path):
        rule = KEYBOARD_RULE if return_path == "hid_lock" else IR_RULE
        fake_screensaver._inhibited = return_path == "hid_lock"
        tray_app._on_device_presence_changed(1, PresenceEvent.INSERT, DeviceTarget.BLOCK, rule, {})
        dialog = tray_app._open_dialogs[1]
        tray_app._on_device_presence_changed(1, PresenceEvent.REMOVE, DeviceTarget.BLOCK, rule, {})
        updated = rule.replace('name "', 'name "Current ')
        fake_screensaver._inhibited = False
        fake_screensaver._active = return_path == "locked_session"
        target = DeviceTarget.ALLOW if return_path == "allowed" else DeviceTarget.BLOCK

        tray_app._on_device_presence_changed(2, PresenceEvent.INSERT, target, updated, {})

        assert dialog.device.number == 2
        assert dialog.device_present
        assert dialog.device.raw_rule == updated
        assert set(tray_app._open_dialogs) == {2}
        dialog._on_block_once()
        assert fake_client.apply_policy_calls == [(2, DeviceTarget.BLOCK, Persistence.ONCE)]
        assert fake_client.apply_policy_rules == [updated]
        assert tray_app._engine._pending_decisions == {}
        assert tray_app._engine._hid_pending_devices == set()
        assert tray_app._engine._screensaver_pending_devices == set()
        assert not tray_app._hid_lock_timer.isActive()
        tray_app._engine._on_screensaver_locked(True)
        assert fake_client.apply_policy_calls == [(2, DeviceTarget.BLOCK, Persistence.ONCE)]

    def test_a_fresh_block_preserves_the_lock_flow_for_other_pending_hid_devices(self, tray_app, fake_client,
                                                                                 fake_screensaver):
        fake_screensaver._inhibited = True
        tray_app._on_device_presence_changed(1, PresenceEvent.INSERT, DeviceTarget.BLOCK, KEYBOARD_RULE, {})
        dialog = tray_app._open_dialogs[1]
        tray_app._on_device_presence_changed(1, PresenceEvent.REMOVE, DeviceTarget.BLOCK, KEYBOARD_RULE, {})
        fake_screensaver._inhibited = False
        tray_app._on_device_presence_changed(2, PresenceEvent.INSERT, DeviceTarget.BLOCK, KEYBOARD_RULE, {})
        other = KEYBOARD_RULE.replace('hash "kbhash1"', 'hash "other"').replace('via-port "1-3"', 'via-port "1-4"')
        tray_app._on_device_presence_changed(3, PresenceEvent.INSERT, DeviceTarget.BLOCK, other, {})

        dialog._on_block_once()

        assert tray_app._engine._hid_pending_devices == {3}
        assert tray_app._hid_lock_timer.isActive()
        fake_screensaver._active = True
        tray_app._engine._on_screensaver_locked(True)
        assert fake_client.apply_policy_calls == [(2, DeviceTarget.BLOCK, Persistence.ONCE),
                                                  (3, DeviceTarget.ALLOW, Persistence.UNCHANGED)]


class TestDeviceListDecisionCoordination:
    """Device-list choices supersede held choices and automatic tray handling."""

    @staticmethod
    def _window(tray_app, fake_client, qtbot, tmp_path):
        from PyQt6.QtCore import QSettings

        from usbguard_gui.device_list import DeviceListWindow

        settings = QSettings(str(tmp_path / "device-list.ini"), QSettings.Format.IniFormat)
        window = DeviceListWindow(fake_client, screensaver=tray_app._screensaver, settings=settings,
                                  decision_handler=tray_app._apply_user_decision)
        qtbot.addWidget(window)
        return window

    @pytest.mark.parametrize("persistence", [Persistence.ONCE, Persistence.ALWAYS])
    def test_a_device_list_block_is_not_overridden_by_the_pending_hid_allow(self, tray_app, fake_client,
                                                                            fake_screensaver, qtbot, tmp_path,
                                                                            persistence):
        tray_app._on_device_presence_changed(1, PresenceEvent.INSERT, DeviceTarget.BLOCK, KEYBOARD_RULE, {})
        window = self._window(tray_app, fake_client, qtbot, tmp_path)

        window._apply(Device.from_dbus(1, KEYBOARD_RULE), DeviceTarget.BLOCK, persistence)
        fake_client.device_policy_changed.emit(1, DeviceTarget.BLOCK, DeviceTarget.BLOCK, KEYBOARD_RULE, 0, {})
        fake_screensaver._active = True
        fake_screensaver.active_changed.emit(True)

        assert fake_client.apply_policy_calls == [(1, DeviceTarget.BLOCK, persistence)]
        assert tray_app._engine._hid_pending_devices == set()
        assert not tray_app._hid_lock_timer.isActive()

    def test_a_new_allow_always_supersedes_a_held_block_once(self, tray_app, fake_client, fake_screensaver,
                                                             queued_decision, qtbot, tmp_path):
        queued_decision(DeviceTarget.BLOCK, rule=IR_RULE)
        fake_screensaver._connected = False
        fake_screensaver.connection_changed.emit(False)
        tray_app._on_device_presence_changed(301, PresenceEvent.INSERT, DeviceTarget.BLOCK, IR_RULE, {})
        fake_screensaver._connected = True
        fake_screensaver.connection_changed.emit(True)
        window = self._window(tray_app, fake_client, qtbot, tmp_path)

        window._apply(Device.from_dbus(301, IR_RULE), DeviceTarget.ALLOW, Persistence.ALWAYS)

        assert tray_app._engine._pending_decisions == {}
        assert tray_app._open_dialogs == {}
        fake_client.apply_policy_calls.clear()
        tray_app._on_device_presence_changed(301, PresenceEvent.REMOVE, DeviceTarget.ALLOW, IR_RULE, {})
        tray_app._on_device_presence_changed(302, PresenceEvent.INSERT, DeviceTarget.ALLOW, IR_RULE, {})
        assert fake_client.apply_policy_calls == []

    def test_device_list_uses_the_retained_dialogs_newest_instance(self, tray_app, fake_client, qtbot, tmp_path):
        tray_app._on_device_presence_changed(1, PresenceEvent.INSERT, DeviceTarget.BLOCK, IR_RULE, {})
        old_row = Device.from_dbus(1, IR_RULE)
        tray_app._on_device_presence_changed(1, PresenceEvent.REMOVE, DeviceTarget.BLOCK, IR_RULE, {})
        tray_app._on_device_presence_changed(2, PresenceEvent.INSERT, DeviceTarget.BLOCK, IR_RULE, {})
        window = self._window(tray_app, fake_client, qtbot, tmp_path)

        window._apply(old_row, DeviceTarget.BLOCK, Persistence.ONCE)

        assert fake_client.apply_policy_calls == [(2, DeviceTarget.BLOCK, Persistence.ONCE)]
        assert tray_app._open_dialogs == {}

    @pytest.mark.parametrize("persistence", [Persistence.ONCE, Persistence.ALWAYS])
    def test_a_device_list_allow_does_not_suppress_the_next_blocked_return(self, tray_app, fake_client, qtbot,
                                                                           tmp_path, mocker, persistence) -> None:
        mocker.patch.object(tray_app._tray, "showMessage")
        mocker.patch("usbguard_gui.app.time.monotonic", return_value=100.0)
        tray_app._on_device_presence_changed(1, PresenceEvent.INSERT, DeviceTarget.BLOCK, IR_RULE, {})
        window = self._window(tray_app, fake_client, qtbot, tmp_path)
        window._apply(Device.from_dbus(1, IR_RULE), DeviceTarget.ALLOW, persistence)
        assert tray_app._open_dialogs == {}

        tray_app._on_device_presence_changed(1, PresenceEvent.REMOVE, DeviceTarget.ALLOW, IR_RULE, {})
        tray_app._on_device_presence_changed(2, PresenceEvent.INSERT, DeviceTarget.BLOCK, IR_RULE, {})

        assert 2 in tray_app._open_dialogs
        assert fake_client.apply_policy_calls == [(1, DeviceTarget.ALLOW, persistence)]

    def test_tray_wires_the_common_decision_handler_into_the_window(self, tray_app, mocker):
        window_class = mocker.patch("usbguard_gui.app.DeviceListWindow")

        tray_app._show_device_list()

        assert window_class.call_args.kwargs["decision_handler"] == tray_app._apply_user_decision


class TestFailedTemporaryActionWarning:
    """A failed live action distinguishes a successful clear from an unchanged policy."""

    @pytest.mark.parametrize("policy_changed", [False, True])
    def test_signal_reports_live_failure_and_the_actual_policy_outcome(self, tray_app, fake_client,
                                                                       mocker, policy_changed):
        show = mocker.patch.object(tray_app._tray, "showMessage")

        fake_client.temporary_apply_failed.emit(54, "block", "Not authorized", policy_changed)

        show.assert_called_once()
        title, body = show.call_args.args[:2]
        assert "Temporary decision not applied" in title
        assert "54" in body
        assert "block" in body
        assert "did not take effect" in body
        assert "Not authorized" in body
        if policy_changed:
            assert "permanent rules removed" in title
            assert "stored policy has changed" in body
            assert "/etc/usbguard/rules.conf" in body
        else:
            assert "removed" not in title
            assert "stored policy has changed" not in body


class TestPartialClearIsAnnouncedDifferently:
    """A half-done clear must not read like a no-op.

    The `Once` path is fail-closed: the clear runs first, so a refusal means the
    decision never happened and the device keeps its rule.  That message is
    correct only while rules.conf is untouched.  When the device owned several
    permanent rules and the clear died partway, the stored policy *did* change,
    and telling the user "the existing permanent rule could not be removed"
    points them at a file that no longer says what they think it says.
    """

    def test_a_total_failure_keeps_the_plain_message(self, tray_app, mocker):
        show = mocker.patch.object(tray_app._tray, "showMessage")
        tray_app._on_permanent_clear_failed(54, "allow", "Not authorized", False)

        title, body = show.call_args[0][0], show.call_args[0][1]
        assert title == "Temporary decision not applied"
        assert "could not be removed" in body
        assert "partly" not in body

    def test_a_partial_failure_says_the_policy_changed(self, tray_app, mocker):
        show = mocker.patch.object(tray_app._tray, "showMessage")
        tray_app._on_permanent_clear_failed(54, "allow", "transient failure", True)

        title, body = show.call_args[0][0], show.call_args[0][1]
        assert "partly changed" in title
        assert "did not take effect" in body, "the decision still did not land"
        assert "no longer what it was" in body, "and the stored policy moved"
        assert "rules.conf" in body, "send the user somewhere they can check"

    def test_the_signal_is_wired_through_to_the_tray(self, tray_app, mocker):
        """Wiring, not just the handler -- an unconnected signal is silent."""
        show = mocker.patch.object(tray_app._tray, "showMessage")
        tray_app._client.permanent_clear_failed.emit(54, "allow", "transient failure", True)

        assert show.called
        assert "partly changed" in show.call_args[0][0]
