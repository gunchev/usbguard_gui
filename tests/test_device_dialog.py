"""Tests for the device action dialog."""

from __future__ import annotations

import os

import pytest
from fakes import _FakeClient, _FakeScreensaver, _FakeSettings
from PyQt6.QtWidgets import QLabel, QMessageBox

from usbguard_gui.device import Device, DeviceTarget, Persistence
from usbguard_gui.device_dialog import DeviceActionDialog

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_RULE = (
    'block id 1234:abcd serial "" name "Test Device" '
    'hash "abc123" parent-hash "" via-port "1-1" '
    "with-interface 03:00:00 with-connect-type hotplug"
)


class TestDialogLockUnavailable:
    """When screen locking is unavailable only a lock-gated HID allow is
    refused.  Block, Reject and non-HID allows need no lock — denying a
    suspicious device must not depend on infrastructure that is itself
    broken — so they stay live while the locker is down."""

    _HUB_RULE = (
        'block id 1234:abcd serial "" name "Test Hub" '
        'hash "abc123" parent-hash "" via-port "1-1" '
        "with-interface ff:00:00 with-connect-type hotplug"
    )

    @pytest.fixture()
    def buttons(self, dialog_with_screensaver):
        dialog = dialog_with_screensaver[0]
        return [dialog._btn_allow_always, dialog._btn_allow_once, dialog._btn_block_once,
                dialog._btn_block_always, dialog._btn_close]

    @pytest.fixture()
    def allow_buttons(self, dialog_with_screensaver):
        dialog = dialog_with_screensaver[0]
        return [dialog._btn_allow_always, dialog._btn_allow_once]

    @pytest.fixture()
    def deny_buttons(self, dialog_with_screensaver):
        dialog = dialog_with_screensaver[0]
        return [dialog._btn_block_once, dialog._btn_block_always, dialog._btn_close]

    @pytest.fixture()
    def dialog_with_screensaver(self, qapp, qtbot):
        screensaver = _FakeScreensaver(connected=False)
        dialog = DeviceActionDialog(_make_device(), _FakeClient(), screensaver=screensaver,
                                    settings=_FakeSettings())
        qtbot.addWidget(dialog)
        return dialog, screensaver

    def test_allow_disabled_but_deny_live_when_lock_unavailable(self, allow_buttons, deny_buttons) -> None:
        assert all(not btn.isEnabled() for btn in allow_buttons)
        assert all(btn.isEnabled() for btn in deny_buttons)

    def test_buttons_enabled_when_lock_available(self, qapp, qtbot) -> None:
        screensaver = _FakeScreensaver(connected=True)
        dialog = DeviceActionDialog(_make_device(), _FakeClient(), screensaver=screensaver)
        qtbot.addWidget(dialog)

        for btn in (dialog._btn_allow_always, dialog._btn_allow_once, dialog._btn_block_once,
                    dialog._btn_block_always, dialog._btn_close):
            assert btn.isEnabled()

    def test_buttons_enabled_without_screensaver(self, qapp, qtbot) -> None:
        """Callers that do not pass a monitor are not gated (old behaviour)."""
        dialog = DeviceActionDialog(_make_device(), _FakeClient())
        qtbot.addWidget(dialog)

        for btn in (dialog._btn_allow_always, dialog._btn_allow_once, dialog._btn_block_once,
                    dialog._btn_block_always, dialog._btn_close):
            assert btn.isEnabled()

    def test_buttons_follow_lock_state_changes(self, dialog_with_screensaver, buttons, allow_buttons,
                                               deny_buttons) -> None:
        _, screensaver = dialog_with_screensaver
        assert all(not btn.isEnabled() for btn in allow_buttons)
        assert all(btn.isEnabled() for btn in deny_buttons)

        # The monitor updates its property and then emits (like the real
        # _on_connected), so mirror that ordering in the fake.
        screensaver._connected = True
        screensaver.connection_changed.emit(True)
        assert all(btn.isEnabled() for btn in buttons)

        screensaver._connected = False
        screensaver.connection_changed.emit(False)
        assert all(not btn.isEnabled() for btn in allow_buttons)
        assert all(btn.isEnabled() for btn in deny_buttons)

    @pytest.mark.parametrize("handler", ["_on_allow_always", "_on_allow_once"])
    def test_allow_handlers_warn_and_record_nothing(self, dialog_with_screensaver, mocker, handler: str) -> None:
        """If an allow handler runs while lock is unavailable (e.g. the state
        flips after the buttons were enabled), it must warn, not record."""
        dialog, _ = dialog_with_screensaver
        warn = mocker.patch.object(QMessageBox, "warning")

        getattr(dialog, handler)()

        assert warn.called
        assert dialog.result_target is None
        dialog.close()

    @pytest.mark.parametrize("handler", ["_on_block_once", "_on_block_always"])
    def test_block_handlers_apply_while_lock_unavailable(self, dialog_with_screensaver, mocker, handler: str) -> None:
        """Block needs no lock: the click records normally, no warning."""
        dialog, _ = dialog_with_screensaver
        warn = mocker.patch.object(QMessageBox, "warning")

        getattr(dialog, handler)()

        assert not warn.called
        assert dialog.result_target is DeviceTarget.BLOCK
        dialog.close()

    def test_non_hid_allow_stays_available_while_lock_unavailable(self, qapp, qtbot, mocker) -> None:
        """A non-HID device never relied on the lock, so Allow works."""
        screensaver = _FakeScreensaver(connected=False)
        dialog = DeviceActionDialog(Device.from_dbus(1, self._HUB_RULE), _FakeClient(),
                                    screensaver=screensaver, settings=_FakeSettings())
        qtbot.addWidget(dialog)
        warn = mocker.patch.object(QMessageBox, "warning")

        dialog._on_allow_once()

        assert not warn.called
        assert dialog.result_target is DeviceTarget.ALLOW

    def test_treatment_disabled_keeps_hid_allow_while_lock_unavailable(self, qapp, qtbot, mocker) -> None:
        """Special HID treatment off ⇒ the gate is disarmed entirely."""
        screensaver = _FakeScreensaver(connected=False)
        dialog = DeviceActionDialog(_make_device(), _FakeClient(), screensaver=screensaver,
                                    settings=_FakeSettings(disable_hid_treatment=True))
        qtbot.addWidget(dialog)
        warn = mocker.patch.object(QMessageBox, "warning")

        dialog._on_allow_once()

        assert not warn.called
        assert dialog.result_target is DeviceTarget.ALLOW
        assert dialog._btn_allow_once.isEnabled()

    def test_refresh_actions_enabled_follows_the_treatment_toggle(self, qapp, qtbot) -> None:
        """The tray menu toggle must re-arm/disarm an already-open dialog."""
        screensaver = _FakeScreensaver(connected=False)
        settings = _FakeSettings()
        dialog = DeviceActionDialog(_make_device(), _FakeClient(), screensaver=screensaver, settings=settings)
        qtbot.addWidget(dialog)
        assert not dialog._btn_allow_once.isEnabled()

        settings.set_disable_hid_treatment(True)
        dialog.refresh_actions_enabled()

        assert dialog._btn_allow_once.isEnabled()


class TestCloseIsDefaultButton:
    """The 'Close' button is the default: Enter dismisses the dialog safely
    (sends REJECT so USBGuard forgets the device) instead of triggering an
    allow action.  This eliminates the need to swallow key events."""

    def test_close_button_text(self, qapp, qtbot) -> None:
        dialog = DeviceActionDialog(_make_device(), _FakeClient())
        qtbot.addWidget(dialog)
        assert dialog._btn_close.text() == "Close"

    def test_close_button_is_default(self, qapp, qtbot) -> None:
        dialog = DeviceActionDialog(_make_device(), _FakeClient())
        qtbot.addWidget(dialog)
        dialog.show()
        qapp.processEvents()
        assert dialog._btn_close.isDefault()

    def test_enter_triggers_close(self, qapp, qtbot) -> None:
        """Pressing Enter triggers the default 'Close' button: records REJECT
        and closes the dialog."""
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest

        client = _FakeClient()
        dialog = DeviceActionDialog(_make_device(), client)
        qtbot.addWidget(dialog)
        dialog.show()
        qapp.processEvents()

        QTest.keyClick(dialog, Qt.Key.Key_Return)
        qapp.processEvents()

        assert dialog.result_target is None
        assert dialog.persistence is Persistence.UNCHANGED
        assert not dialog.isVisible()

    def test_enter_key_enter_triggers_close(self, qapp, qtbot) -> None:
        """Key_Enter (numpad Enter) also triggers Close."""
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
        assert dialog.persistence is Persistence.UNCHANGED
        assert not dialog.isVisible()

    def test_escape_closes_without_target(self, qapp, qtbot) -> None:
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
        assert client.apply_policy_calls == []
        assert not dialog.isVisible()

    def test_enter_does_not_trigger_allow(self, qapp, qtbot) -> None:
        """Enter must never trigger an allow action — the default is Close."""
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest

        client = _FakeClient()
        dialog = DeviceActionDialog(_make_device(), client)
        qtbot.addWidget(dialog)
        dialog.show()
        qapp.processEvents()

        QTest.keyClick(dialog, Qt.Key.Key_Return)
        qapp.processEvents()

        # The target must be REJECT (Close), never ALLOW.
        assert dialog.result_target is not DeviceTarget.ALLOW


def _make_device() -> Device:
    return Device.from_dbus(1, _RULE)


class TestDialogConnectionWarning:
    """When the USBGuard daemon is not connected, action buttons must show a
    warning instead of silently accepting the click — the user must not
    believe their choice was applied.  The dialog stays open so the user
    can retry once the daemon is back."""

    @pytest.mark.parametrize("handler", ["_on_allow_always", "_on_allow_once", "_on_block_once", "_on_block_always"])
    def test_all_actions_warn_when_disconnected(self, qapp, qtbot, mocker, handler: str) -> None:
        client = _FakeClient(connected=False)
        dialog = DeviceActionDialog(_make_device(), client)
        qtbot.addWidget(dialog)
        warn = mocker.patch.object(QMessageBox, "warning")

        getattr(dialog, handler)()

        assert warn.called, f"{handler} did not warn while disconnected"
        # No choice was recorded, so the app will not apply anything.
        assert dialog.result_target is None
        assert client.apply_policy_calls == []
        dialog.close()

    @pytest.mark.parametrize(
        ("handler", "target", "persistence"),
        [
            ("_on_allow_always", DeviceTarget.ALLOW, Persistence.ALWAYS),
            ("_on_allow_once", DeviceTarget.ALLOW, Persistence.ONCE),
            ("_on_block_once", DeviceTarget.BLOCK, Persistence.ONCE),
            ("_on_block_always", DeviceTarget.BLOCK, Persistence.ALWAYS),
        ],
    )
    def test_all_actions_record_choice_when_connected(self, qapp, qtbot, mocker, handler: str, target: DeviceTarget,
                                                      persistence: Persistence) -> None:
        client = _FakeClient(connected=True)
        dialog = DeviceActionDialog(_make_device(), client)
        qtbot.addWidget(dialog)
        warn = mocker.patch.object(QMessageBox, "warning")

        getattr(dialog, handler)()

        assert not warn.called
        assert dialog.result_target is target
        assert dialog.persistence is persistence

    def test_button_labels_are_the_action_set(self, qapp, qtbot) -> None:
        """The labels are the contract the user reads; keep them exact."""
        dialog = DeviceActionDialog(_make_device(), _FakeClient(connected=True))
        qtbot.addWidget(dialog)

        assert [b.text() for b in (dialog._btn_allow_always, dialog._btn_allow_once,
                                   dialog._btn_block_once, dialog._btn_block_always)] == [
            "Allow Always", "Allow Once", "Block Once", "Block Always",
        ]


class TestDismissAppliesNothing:
    """Slice 7 -- Close, Escape and the timeout all mean "not deciding".

    With four explicit Always/Once buttons, a dismiss that silently applies
    something is the anomaly: the label says Close, not Block.  The device
    stays where USBGuard's implicit policy put it -- blocked -- and nothing
    durable is touched by a decision nobody made.  Verified against the
    insert path: a device matching a permanent allow never reaches a dialog
    at all (`app.py:327` returns on target=ALLOW), so a dismiss could not
    have wiped a grant even if it tried.
    """

    def test_close_button_applies_nothing(self, qapp, qtbot) -> None:
        client = _FakeClient(connected=True)
        dialog = DeviceActionDialog(_make_device(), client)
        qtbot.addWidget(dialog)

        dialog._on_close()

        assert dialog.result_target is None
        assert client.apply_policy_calls == []

    def test_close_is_never_blocked(self, qapp, qtbot, mocker) -> None:
        """Closing has no action that could fail, so it must never warn or stick."""
        client = _FakeClient(connected=False)
        dialog = DeviceActionDialog(_make_device(), client)
        qtbot.addWidget(dialog)
        warn = mocker.patch.object(QMessageBox, "warning")

        dialog._on_close()

        assert not warn.called
        assert dialog.result_target is None

    def test_timeout_applies_nothing(self, qapp, qtbot) -> None:
        client = _FakeClient(connected=True)
        dialog = DeviceActionDialog(_make_device(), client, timeout=1)
        qtbot.addWidget(dialog)

        dialog._tick()

        assert dialog.result_target is None
        assert client.apply_policy_calls == []


class TestDialogRetargeting:
    """Device details and presence follow the instance the action will target."""

    def test_retarget_updates_the_visible_details_and_presence(self, qapp, qtbot) -> None:
        dialog = DeviceActionDialog(Device.from_dbus(1, _RULE), _FakeClient())
        qtbot.addWidget(dialog)
        dialog.set_device_present(False)
        rule = _RULE.replace('name "Test Device"', 'name "Current Device"').replace('serial ""', 'serial "S1"')
        rule = rule.replace("with-connect-type hotplug", "with-connect-type unknown")
        current = Device.from_dbus(2, rule)

        dialog.set_device(current)

        assert dialog.device is current
        assert dialog.device_present
        labels = [label.text() for label in dialog.findChildren(QLabel)]
        assert "Current Device" in labels
        assert "S1" in labels
        assert "unknown" in labels
        assert "Test Device" not in labels
        assert "hotplug" not in labels
        assert "disconnected" not in dialog._timeout_label.text()
        assert "no action is applied" in dialog._timeout_label.text()


class TestCleanupIsIdempotent:
    """`finished` can arrive more than once; the second cleanup must not raise.

    A TypeError raised inside a Qt slot aborts the tray process.  Observed as
    the whole app dying with SIGABRT after `close()` followed by `reject()`:
    the second `finished` ran cleanup again and the second `disconnect` hit an
    already-detached signal.
    """

    def test_close_then_reject_survives_the_second_finished(self, qapp) -> None:
        screensaver = _FakeScreensaver()
        dialog = DeviceActionDialog(_make_device(), _FakeClient(), screensaver=screensaver)
        dialog.show()
        qapp.processEvents()

        dialog.close()
        dialog.reject()
        qapp.processEvents()

        assert dialog._cleanup_done is True

    def test_the_monitor_is_detached_exactly_once(self, qapp) -> None:
        screensaver = _FakeScreensaver()
        dialog = DeviceActionDialog(_make_device(), _FakeClient(), screensaver=screensaver)
        dialog.show()
        qapp.processEvents()
        assert screensaver.receivers(screensaver.connection_changed) == 1

        dialog.close()
        dialog.reject()
        qapp.processEvents()

        assert screensaver.receivers(screensaver.connection_changed) == 0

    def test_cleanup_without_a_screensaver_is_still_safe(self, qapp) -> None:
        dialog = DeviceActionDialog(_make_device(), _FakeClient())
        dialog.show()
        qapp.processEvents()
        dialog.close()
        dialog.reject()
        qapp.processEvents()
        assert dialog._cleanup_done is True


class TestTheAbsentDeviceNoticeSurvivesTheCountdown:
    """The one label that tells the user a click still counts must not be erased.

    `set_device_present(False)` writes the notice into the same label the
    countdown ticks into, and `_tick` rewrote it a second later with
    "Auto-close in Ns (device stays blocked)" -- which not only loses the
    notice, it says the opposite of what is true: the choice *is* still live,
    and it applies when the device returns.
    """

    @pytest.fixture()
    def dialog(self, qapp, qtbot):
        dialog = DeviceActionDialog(_make_device(), _FakeClient(), timeout=30)
        qtbot.addWidget(dialog)
        return dialog

    def test_the_notice_survives_a_tick(self, dialog) -> None:
        dialog.set_device_present(False)

        dialog._tick()

        # The notice is what must survive, not one exact string -- the away
        # line keeps its own countdown in it, so the whole text moves each tick.
        text = dialog._timeout_label.text()
        assert "disconnected" in text.lower(), "the countdown must not overwrite the notice"
        assert "returns" in text

    def test_the_countdown_still_runs_while_the_device_is_away(self, dialog) -> None:
        """Only the text is held back -- the dialog still auto-closes."""
        dialog.set_device_present(False)
        remaining = dialog._remaining

        dialog._tick()

        assert dialog._remaining == remaining - 1

    def test_the_countdown_comes_back_when_the_device_does(self, dialog) -> None:
        dialog.set_device_present(False)
        dialog._tick()

        dialog.set_device_present(True)

        assert "Auto-close in" in dialog._timeout_label.text()

    def test_a_present_device_still_shows_the_countdown(self, dialog) -> None:
        dialog._tick()

        assert dialog._timeout_label.text() == "Auto-close in 29s (no action is applied)"


class TestTheAwayNoticeKeepsTheClockVisible:
    """F5 -- holding back the countdown text also hid the clock.

    The away notice replaces the whole status line, so for the entire window
    in which the user can still click there is no indication of how long is
    left -- while the dialog keeps counting down and closes behind them.  The
    notice is worth keeping; suppressing the timer alongside it is not.
    """

    @pytest.fixture()
    def dialog(self, qapp, qtbot):
        dialog = DeviceActionDialog(_make_device(), _FakeClient(), timeout=30)
        qtbot.addWidget(dialog)
        return dialog

    def test_the_time_left_is_visible_while_the_device_is_away(self, dialog) -> None:
        dialog.set_device_present(False)

        dialog._tick()

        assert f"{dialog._remaining}s" in dialog._timeout_label.text(), \
            f"the clock must stay readable; got {dialog._timeout_label.text()!r}"

    def test_the_clock_still_moves_while_the_device_is_away(self, dialog) -> None:
        dialog.set_device_present(False)
        dialog._tick()
        first = dialog._timeout_label.text()

        dialog._tick()

        assert dialog._timeout_label.text() != first, "a frozen clock is not a countdown"

    def test_the_away_notice_is_still_there_alongside_the_clock(self, dialog) -> None:
        dialog.set_device_present(False)

        dialog._tick()

        text = dialog._timeout_label.text()
        assert "disconnected" in text.lower(), "the away state must not be lost either"
        assert "stays blocked" not in text, "the old wording says the opposite of what is true"
