"""Tests for the decision engine — the HID lock-first flow, unlock cycles,
pending decisions and the lock gate."""

from __future__ import annotations

import os
import re
from unittest.mock import MagicMock, patch

import pytest
from conftest import IR_RULE, KEYBOARD_RULE
from fakes import _FakeSettings

from usbguard_gui.app import USBGuardTrayApp
from usbguard_gui.decision import MAX_PENDING_DECISIONS, dialog_identity
from usbguard_gui.device import Device, DeviceTarget, Persistence, PresenceEvent
from usbguard_gui.ui_strings import _LIVE_AUTHORIZE_PROMISES, HANDBACK_NOTICE_TITLE

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# ---------------------------------------------------------------------------
# Helpers for HID tests
# ---------------------------------------------------------------------------


def _make_hid_device(number: int = 1) -> Device:
    rule_str = (
        'block id 1234:abcd serial "" name "Test Keyboard" '
        'hash "abc123" parent-hash "" via-port "1-1" '
        "with-interface 03:00:00 with-connect-type hotplug"
    )
    return Device.from_dbus(number, rule_str)


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
        assert fake_client.apply_policy_calls == [(1, DeviceTarget.ALLOW, Persistence.UNCHANGED)]

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

        tray_app._engine._on_screensaver_locked(True)
        assert fake_client.apply_policy_calls == []


# ---------------------------------------------------------------------------
# HID allow on screen lock
# ---------------------------------------------------------------------------


class TestHIDAllowOnScreenLock:
    """Pending HID devices must be allowed when the screen locks so the user
    can unlock with the newly-attached keyboard."""

    def test_allows_pending_hid_devices_on_lock(self, tray_app, fake_client) -> None:
        tray_app._hid_pending_devices = {1}
        tray_app._engine._on_screensaver_locked(True)
        assert fake_client.apply_policy_calls == [(1, DeviceTarget.ALLOW, Persistence.UNCHANGED)]
        assert tray_app._hid_pending_devices == set()

    def test_allows_multiple_pending_devices(self, tray_app, fake_client) -> None:
        tray_app._hid_pending_devices = {1, 2, 3}
        tray_app._engine._on_screensaver_locked(True)
        assert len(fake_client.apply_policy_calls) == 3
        for device_id in (1, 2, 3):
            assert (device_id, DeviceTarget.ALLOW, Persistence.UNCHANGED) in fake_client.apply_policy_calls
        assert tray_app._hid_pending_devices == set()

    def test_no_pending_no_action(self, tray_app, fake_client) -> None:
        tray_app._hid_pending_devices = set()
        tray_app._engine._on_screensaver_locked(True)
        assert fake_client.apply_policy_calls == []

    def test_does_not_fire_on_unlock(self, tray_app, fake_client) -> None:
        tray_app._hid_pending_devices = {1}
        tray_app._engine._on_screensaver_locked(False)
        assert fake_client.apply_policy_calls == []
        assert tray_app._hid_pending_devices == {1}  # preserved for next lock

    def test_allows_only_matching_device_id(self, tray_app, fake_client) -> None:
        tray_app._hid_pending_devices = {1, 2}
        tray_app._engine._on_screensaver_locked(True)
        assert len(fake_client.apply_policy_calls) == 2
        assert all(call[1] == DeviceTarget.ALLOW for call in fake_client.apply_policy_calls)
        assert all(call[2] is Persistence.UNCHANGED for call in fake_client.apply_policy_calls)


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
        tray_app._engine._on_screensaver_unlocked(False)
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
        tray_app._engine._on_screensaver_locked(True)
        assert fake_client.apply_policy_calls == [(1, DeviceTarget.ALLOW, Persistence.UNCHANGED)]

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
        tray_app._engine._on_screensaver_unlocked(False)
        assert fake_client.fetch_devices_calls == [0]

        # 3. Screen locks again; device B inserted while locked -> deferred.
        #    Cycle 0's fetch is still "in flight" (its result has not arrived).
        tray_app._screensaver_pending_devices = {20}

        # 4. Screen unlocks again -> cycle 1 registered and fetched, before
        #    cycle 0 resolved.
        tray_app._engine._on_screensaver_unlocked(False)
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
        tray_app._engine._on_screensaver_unlocked(False)
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
        tray_app._engine._on_screensaver_unlocked(False)

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

        fake_client.device_presence_changed.emit(
            1, 1, int(DeviceTarget.BLOCK),
            'block id 1234:abcd serial "" name "Test Keyboard" '
            'hash "abc123" parent-hash "" via-port "1-1" '
            'with-interface 03:00:00 with-connect-type hotplug',
            {},
        )

        assert set(tray_app._open_dialogs) == {1}
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

        fake_client.device_presence_changed.emit(1, 1, int(DeviceTarget.BLOCK), self._HID_RULE, {})

        assert set(tray_app._open_dialogs) == {1}
        assert fake_client.apply_policy_calls == []
        assert fake_screensaver.lock_calls == 0
        assert tray_app._hid_pending_devices == set()

    def test_hid_lock_timer_skipped_when_lock_unavailable(self, tray_app, fake_client, fake_screensaver) -> None:
        """If availability drops while a deferred lock is in flight, the lock
        must not be claimed — the pending devices stay blocked."""
        tray_app._hid_pending_devices = {1}
        fake_screensaver.connection_changed.emit(False)

        tray_app._engine._lock_for_pending_hid()

        assert fake_screensaver.lock_calls == 0
        assert tray_app._hid_pending_devices == {1}

    def test_hid_pending_flow_still_works_when_available(self, tray_app, fake_client, fake_screensaver, qtbot) -> None:
        """Regression guard: with lock available the pending+lock flow is
        unchanged."""
        fake_client.device_presence_changed.emit(1, 1, int(DeviceTarget.BLOCK), self._HID_RULE, {})

        assert 1 in tray_app._hid_pending_devices
        qtbot.waitUntil(lambda: fake_screensaver.lock_calls == 1, timeout=8000)


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

        tray_app._engine._lock_for_pending_hid()
        assert fake_screensaver.lock_calls == 1

        tray_app._engine._on_screensaver_locked(True)
        assert sorted(fake_client.apply_policy_calls) == sorted(
            [(1, DeviceTarget.ALLOW, Persistence.UNCHANGED), (2, DeviceTarget.ALLOW, Persistence.UNCHANGED)]
        )


class TestUnlockQueueCap:
    """Queued unlock cycles are only consumed by a non-empty snapshot, so a
    daemon that stays down across many lock/unlock cycles must not grow the
    queue without bound."""

    def test_oldest_cycle_is_dropped_once_the_cap_is_reached(self, tray_app, monkeypatch) -> None:
        monkeypatch.setattr("usbguard_gui.decision.MAX_PENDING_UNLOCK_CYCLES", 3)

        for i in range(1, 7):
            tray_app._screensaver_pending_devices = {i}
            tray_app._engine._on_screensaver_unlocked(False)

        assert len(tray_app._pending_unlock_cycles) == 3
        assert tray_app._pending_unlock_cycles == {3: {4}, 4: {5}, 5: {6}}

    def test_nothing_dropped_below_the_cap(self, tray_app, monkeypatch) -> None:
        monkeypatch.setattr("usbguard_gui.decision.MAX_PENDING_UNLOCK_CYCLES", 10)

        for i in range(1, 4):
            tray_app._screensaver_pending_devices = {i}
            tray_app._engine._on_screensaver_unlocked(False)

        assert tray_app._pending_unlock_cycles == {0: {1}, 1: {2}, 2: {3}}


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
        tray_app._engine._on_screensaver_unlocked(False)
        tray_app._screensaver_pending_devices = {20}
        tray_app._engine._on_screensaver_unlocked(False)

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
        tray_app._engine._on_screensaver_unlocked(False)
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


class TestUnlockSnapshotFreshness:
    """New presence and user decisions invalidate older unlock-fetch snapshots."""

    def test_removed_incarnation_cannot_retarget_the_newer_live_dialog(self, tray_app, fake_client,
                                                                       fake_screensaver, mocker):
        show = mocker.patch.object(tray_app._tray, "showMessage")
        fake_screensaver._active = True
        tray_app._on_device_presence_changed(1, PresenceEvent.INSERT, DeviceTarget.BLOCK, IR_RULE, {})
        fake_screensaver._active = False
        tray_app._engine._on_screensaver_unlocked(False)
        cycle_id = fake_client.fetch_devices_calls[-1]
        tray_app._on_device_presence_changed(1, PresenceEvent.REMOVE, DeviceTarget.BLOCK, IR_RULE, {})
        current_rule = IR_RULE.replace('name "', 'name "Current ')
        tray_app._on_device_presence_changed(2, PresenceEvent.INSERT, DeviceTarget.BLOCK, current_rule, {})
        dialog = tray_app._open_dialogs[2]
        notices = show.call_count

        tray_app._engine._on_correlated_devices(cycle_id, [Device.from_dbus(1, IR_RULE)])

        assert dialog.device.number == 2
        assert dialog.device.raw_rule == current_rule
        assert dialog.device_present
        assert set(tray_app._open_dialogs) == {2}
        assert show.call_count == notices
        dialog._on_block_once()
        assert fake_client.apply_policy_calls == [(2, DeviceTarget.BLOCK, Persistence.ONCE)]

    def test_removal_preserves_other_devices_in_each_outstanding_cycle(self, tray_app):
        first = tray_app._engine._register_unlock_cycle({1, 3})
        second = tray_app._engine._register_unlock_cycle({1, 4})

        tray_app._on_device_presence_changed(1, PresenceEvent.REMOVE, DeviceTarget.BLOCK, IR_RULE, {})

        assert tray_app._pending_unlock_cycles == {first: {3}, second: {4}}
        other_rule = IR_RULE.replace('hash "irhash1"', 'hash "other"')
        tray_app._engine._on_correlated_devices(first, [Device.from_dbus(1, IR_RULE), Device.from_dbus(3, other_rule)])
        assert set(tray_app._open_dialogs) == {3}
        assert tray_app._pending_unlock_cycles == {second: {4}}

    def test_empty_late_reply_cannot_resurrect_a_cancelled_cycle(self, tray_app, fake_client):
        cycle_id = tray_app._engine._register_unlock_cycle({1})
        tray_app._on_device_presence_changed(1, PresenceEvent.REMOVE, DeviceTarget.BLOCK, IR_RULE, {})

        tray_app._engine._on_correlated_devices(cycle_id, [])
        tray_app._engine._retry_pending_unlock_cycles()

        assert tray_app._pending_unlock_cycles == {}
        assert fake_client.fetch_devices_calls == []

    def test_an_allowed_policy_change_prevents_an_old_blocked_snapshot_from_prompting(self, tray_app,
                                                                                      fake_client):
        cycle_id = tray_app._engine._register_unlock_cycle({1})
        allowed = IR_RULE.replace("block ", "allow ", 1)
        fake_client.device_policy_changed.emit(1, DeviceTarget.BLOCK, DeviceTarget.ALLOW, allowed, 7, {})

        tray_app._engine._on_correlated_devices(cycle_id, [Device.from_dbus(1, IR_RULE)])

        assert tray_app._open_dialogs == {}
        assert tray_app._pending_unlock_cycles == {}

    @pytest.mark.parametrize("target", [DeviceTarget.ALLOW, DeviceTarget.BLOCK])
    def test_a_fresh_decision_prevents_an_older_snapshot_from_reopening_the_prompt(self, tray_app,
                                                                                   fake_client, target):
        cycle_id = tray_app._engine._register_unlock_cycle({1})

        tray_app._apply_user_decision(Device.from_dbus(1, IR_RULE), target, Persistence.ONCE)
        tray_app._engine._on_correlated_devices(cycle_id, [Device.from_dbus(1, IR_RULE)])

        assert fake_client.apply_policy_calls == [(1, target, Persistence.ONCE)]
        assert tray_app._open_dialogs == {}
        assert tray_app._pending_unlock_cycles == {}


class TestAutomaticAllowsNeverClearPersistence:
    """Slice 5 -- the three non-user allow paths must never delete a standing rule.

    These fire with nobody clicking: the list-devices HID safety net, the
    anti-lockout branch, and the pending-unlock allow.  If any of them carried
    `Once` semantics it would erase an administrator's permanent `block` from
    a path with no user in the loop -- privilege escalation dressed up as a
    temporary allow.  They are `UNCHANGED`, and that is a security property,
    not a convenient default.
    """

    _RULE = ('block id 1234:abcd serial "" name "Keyboard" hash "aaa111" '
             'parent-hash "" via-port "1-1" with-interface 03:00:00 '
             'with-connect-type hotplug')

    def test_anti_lockout_branch_allows_without_clearing(self, tray_app, fake_client,
                                                         fake_screensaver) -> None:
        """HID inserted while the screen is already locked: allow, persistence untouched."""
        fake_screensaver._active = True

        fake_client.device_presence_changed.emit(
            1, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK), self._RULE, {})

        assert fake_client.apply_policy_calls == [(1, DeviceTarget.ALLOW, Persistence.UNCHANGED)]
        assert fake_client.remove_rule_calls == []

    def test_pending_unlock_allow_does_not_clear(self, tray_app, fake_client) -> None:
        tray_app._hid_pending_devices = {1}

        tray_app._engine._on_screensaver_locked(True)

        assert fake_client.apply_policy_calls == [(1, DeviceTarget.ALLOW, Persistence.UNCHANGED)]
        assert fake_client.remove_rule_calls == []

    def test_list_devices_hid_safety_net_does_not_clear(self, tray_app, fake_client,
                                                        fake_screensaver) -> None:
        fake_screensaver._active = True
        tray_app._hid_pending_devices = {1}

        tray_app._engine._on_list_devices_result([Device.from_dbus(1, self._RULE)])

        assert fake_client.apply_policy_calls == [(1, DeviceTarget.ALLOW, Persistence.UNCHANGED)]
        assert fake_client.remove_rule_calls == []


class TestAntiLockoutBranch:
    """The locked-screen allow must stand alone — REVIEW-2026-09-28 finding #1.

    A HID device inserted while the screen is already locked is allowed at
    once so it can type the password; that allow is the invariant this app
    exists to enforce.  What this class pins is everything the branch must
    NOT do around it: no dialog, no deferred state, no lock timer, no notice
    a locked user cannot read — and, crucially, that the allow runs before
    the permanent-hash cache skip, so a whitelisted keyboard is still let
    through while locked.
    """

    _RULE = ('block id 1234:abcd serial "" name "Keyboard" hash "aaa111" '
             'parent-hash "" via-port "1-1" with-interface 03:00:00 '
             'with-connect-type hotplug')

    def test_the_allow_fires_alone(self, tray_app, fake_client, fake_screensaver, mocker) -> None:
        show = mocker.patch.object(tray_app._tray, "showMessage")
        fake_screensaver._active = True

        fake_client.device_presence_changed.emit(
            1, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK), self._RULE, {})

        assert fake_client.apply_policy_calls == [(1, DeviceTarget.ALLOW, Persistence.UNCHANGED)]
        assert tray_app._open_dialogs == {}
        assert tray_app._hid_pending_devices == set()
        assert tray_app._screensaver_pending_devices == set()
        assert not tray_app._hid_lock_timer.isActive()
        assert not show.called

    def test_the_allow_precedes_the_permanent_cache_skip(self, tray_app, fake_client,
                                                         fake_screensaver) -> None:
        """Ordering pin: the hash-cache skip must not eat the locked-screen allow.

        The cache check sits after the anti-lockout branch in
        _on_device_presence_changed.  A refactor that moved it first would
        let a whitelisted keyboard be skipped while the screen is locked,
        leaving it unable to type the password that unlocks the session.
        """
        tray_app._permanent_allow_hashes.add("aaa111")
        fake_screensaver._active = True

        fake_client.device_presence_changed.emit(
            1, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK), self._RULE, {})

        assert fake_client.apply_policy_calls == [(1, DeviceTarget.ALLOW, Persistence.UNCHANGED)]

    def test_disabled_treatment_defers_while_locked_instead_of_allowing(
            self, qapp, fake_client, fake_screensaver) -> None:
        """Settings off: a locked screen defers the insert — it never auto-allows."""
        with (
            patch("usbguard_gui.app.USBGuardClient", return_value=fake_client),
            patch("usbguard_gui.app.ScreensaverMonitor", return_value=fake_screensaver),
        ):
            app = USBGuardTrayApp(qapp, settings=_FakeSettings(disable_hid_treatment=True))
        fake_screensaver._active = True

        fake_client.device_presence_changed.emit(
            1, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK), self._RULE, {})

        assert fake_client.apply_policy_calls == []
        assert app._screensaver_pending_devices == {1}
        assert app._hid_pending_devices == set()
        assert app._open_dialogs == {}
        app._quit()


class TestDecisionsSurviveAFlappingDevice:
    """A device dropping off the bus must not take the user's options with it.

    Observed: the dialog for a Smart IR Blaster closed in under three seconds
    -- REMOVE closed it, and the cooldown then suppressed the next one -- so
    there was nothing left to click at all.  The dialog now stays open, and a
    choice made while the device is away is held and applied on its next
    appearance.
    """

    _IR = (
        'block id 045c:0131 serial "IR1" name "Smart IR Blaster" hash "irhash1" '
        'parent-hash "" via-port "1-2" with-interface ff:00:00 with-connect-type "hotplug"'
    )

    def _open(self, tray_app, mocker, number: int = 292):
        mocker.patch.object(tray_app._tray, "showMessage")
        tray_app._show_device_dialog(Device.from_dbus(number, self._IR))
        return tray_app._open_dialogs[number]

    def _remove(self, tray_app, number: int = 292) -> None:
        tray_app._on_device_presence_changed(number, int(PresenceEvent.REMOVE),
                                             int(DeviceTarget.BLOCK), self._IR, {})

    @property
    def _ident(self) -> str:
        return dialog_identity(Device.from_dbus(292, self._IR))

    def test_removal_keeps_the_dialog_open_but_marks_the_device_gone(self, tray_app, mocker) -> None:
        dialog = self._open(tray_app, mocker)
        assert dialog.device_present is True

        self._remove(tray_app)

        assert tray_app._open_dialogs.get(292) is dialog, "the dialog must survive the REMOVE"
        assert dialog.device_present is False

    def test_always_while_absent_writes_the_rule_immediately(self, tray_app, fake_client, mocker) -> None:
        """A permanent rule is inert data -- appendRule needs no device.  There
        is nothing to defer, and nothing to authorize either."""
        dialog = self._open(tray_app, mocker)
        self._remove(tray_app)

        dialog._choose(DeviceTarget.BLOCK, Persistence.ALWAYS)

        assert fake_client.persist_rule_calls[-1] == (292, DeviceTarget.BLOCK, self._IR)
        assert fake_client.apply_policy_calls == [], "no live device to authorize"
        assert tray_app._pending_decisions == {}, "nothing queued -- it already landed"

    def test_once_while_absent_is_queued_not_applied(self, tray_app, fake_client, mocker) -> None:
        """`Once` is a live state that expires, so it genuinely needs the device."""
        dialog = self._open(tray_app, mocker)
        self._remove(tray_app)

        dialog._choose(DeviceTarget.BLOCK, Persistence.ONCE)

        assert fake_client.apply_policy_calls == []
        assert fake_client.persist_rule_calls == []
        assert self._ident in tray_app._pending_decisions

    def test_the_queued_decision_applies_on_the_next_appearance(self, tray_app, fake_client, mocker) -> None:
        dialog = self._open(tray_app, mocker)
        self._remove(tray_app)
        dialog._choose(DeviceTarget.ALLOW, Persistence.ONCE)

        tray_app._show_device_dialog(Device.from_dbus(301, self._IR))

        assert fake_client.apply_policy_calls[-1] == (301, DeviceTarget.ALLOW, Persistence.ONCE)
        assert tray_app._pending_decisions == {}

    def test_a_queued_decision_does_not_open_a_fresh_prompt(self, tray_app, mocker) -> None:
        dialog = self._open(tray_app, mocker)
        self._remove(tray_app)
        dialog._choose(DeviceTarget.BLOCK, Persistence.ONCE)
        before = tray_app._tray.showMessage.call_count

        tray_app._show_device_dialog(Device.from_dbus(302, self._IR))

        assert tray_app._tray.showMessage.call_count == before, "the user already decided"
        assert len(tray_app._open_dialogs) == 0, "the answered dialog closed; no new one opens"

    def test_a_held_allow_once_does_not_suppress_the_following_blocked_return(self, tray_app, fake_client,
                                                                              mocker) -> None:
        mocker.patch("usbguard_gui.app.time.monotonic", return_value=100.0)
        dialog = self._open(tray_app, mocker)
        self._remove(tray_app)
        dialog._choose(DeviceTarget.ALLOW, Persistence.ONCE)
        tray_app._on_device_presence_changed(301, PresenceEvent.INSERT, DeviceTarget.BLOCK, self._IR, {})
        assert tray_app._open_dialogs == {}, "this return consumes the held choice"

        self._remove(tray_app, 301)
        tray_app._on_device_presence_changed(302, PresenceEvent.INSERT, DeviceTarget.BLOCK, self._IR, {})

        assert 302 in tray_app._open_dialogs, "the held Once was consumed and expired on disconnect"
        assert fake_client.apply_policy_calls == [(301, DeviceTarget.ALLOW, Persistence.ONCE)]

    def test_a_choice_made_while_present_applies_immediately(self, tray_app, fake_client, mocker) -> None:
        dialog = self._open(tray_app, mocker)

        dialog._choose(DeviceTarget.BLOCK, Persistence.ONCE)

        assert fake_client.apply_policy_calls[-1] == (292, DeviceTarget.BLOCK, Persistence.ONCE)
        assert tray_app._pending_decisions == {}

    def test_the_pending_cap_drops_the_oldest_and_says_so(self, tray_app, mocker) -> None:
        for i in range(MAX_PENDING_DECISIONS):
            tray_app._pending_decisions[f"hash:filler{i}"] = (DeviceTarget.BLOCK, Persistence.ONCE)
        dropped = mocker.patch("usbguard_gui.decision.log.warning")

        dialog = self._open(tray_app, mocker)
        self._remove(tray_app)
        dialog._choose(DeviceTarget.BLOCK, Persistence.ONCE)

        assert len(tray_app._pending_decisions) == MAX_PENDING_DECISIONS
        assert self._ident in tray_app._pending_decisions
        assert dropped.called
        # Which one goes matters.  `dict.popitem()` is LIFO, so the cap used to
        # evict the *newest* queued decision and pin the 32 oldest forever --
        # keeping the stalest decisions and discarding the one the user made a
        # moment ago, which is the opposite of what the log line says.
        assert "hash:filler0" not in tray_app._pending_decisions, "the oldest queued decision goes first"
        assert f"hash:filler{MAX_PENDING_DECISIONS - 1}" in tray_app._pending_decisions, \
            "the newest queued decisions stay"
        assert dropped.call_args.args[-1] == "hash:filler0", "the log must name the entry it actually dropped"

    def test_a_retargeted_dialog_knows_the_device_is_back(self, tray_app, fake_client, mocker) -> None:
        """Retargeting must restore presence.

        Without it the dialog keeps the "away" flag set by the previous REMOVE,
        so a click made just after a re-appearance takes the no-live-device
        path and the live half of the decision is silently skipped.
        """
        dialog = self._open(tray_app, mocker)
        self._remove(tray_app)
        assert dialog.device_present is False

        tray_app._show_device_dialog(Device.from_dbus(301, self._IR))

        assert dialog.device_present is True, "the device we just re-targeted is on the bus"
        dialog._choose(DeviceTarget.ALLOW, Persistence.ONCE)
        assert fake_client.apply_policy_calls[-1] == (301, DeviceTarget.ALLOW, Persistence.ONCE)
        assert tray_app._pending_decisions == {}, "nothing to queue, it applied live"


class TestAQueuedDecisionDrainsOnEveryReturnPath:
    """A decision the user already made must not depend on how the device comes back.

    ``_show_device_dialog`` is the only place a queued decision was ever
    applied, and the INSERT handler returns before reaching it whenever the
    device needs no prompt -- it came back already allowed, or it is a HID
    device heading for the lock-first flow.  So the one case the queue exists
    for (a device that flaps) lost the decision precisely when the device's own
    policy disagreed with the user's choice.  The HID path is the sharp end: a
    queued `Block` became an auto-allow on the next unlock.
    """

    _IR = (
        'block id 045c:0131 serial "IR1" name "Smart IR Blaster" hash "irhash1" '
        'parent-hash "" via-port "1-2" with-interface ff:00:00 with-connect-type "hotplug"'
    )
    _KEYBOARD = (
        'block id 046d:c52b serial "KB1" name "Keyboard" hash "kbhash1" '
        'parent-hash "" via-port "1-3" with-interface 03:01:01 with-connect-type "hotplug"'
    )

    def _queue(self, tray_app, mocker, rule: str, target: DeviceTarget = DeviceTarget.BLOCK) -> None:
        """Leave a decision in the queue the way the user does: decide while away."""
        mocker.patch.object(tray_app._tray, "showMessage")
        tray_app._show_device_dialog(Device.from_dbus(292, rule))
        dialog = tray_app._open_dialogs[292]
        tray_app._on_device_presence_changed(292, int(PresenceEvent.REMOVE), int(DeviceTarget.BLOCK), rule, {})
        dialog._choose(target, Persistence.ONCE)
        assert tray_app._pending_decisions, "precondition: the decision is queued"

    def _insert(self, tray_app, rule: str, target: DeviceTarget, number: int = 301) -> None:
        tray_app._on_device_presence_changed(number, int(PresenceEvent.INSERT), int(target), rule, {})

    def test_a_device_that_returns_allowed_still_gets_the_queued_decision(self, tray_app, fake_client, mocker):
        """The INSERT handler skips allowed devices -- but not a decided one.

        A wildcard `allow id ...` underneath the device brings it back with
        target=ALLOW, and the queued `Block Once` was dropped without a word.
        """
        self._queue(tray_app, mocker, self._IR)

        self._insert(tray_app, self._IR, DeviceTarget.ALLOW)

        assert fake_client.apply_policy_calls == [(301, DeviceTarget.BLOCK, Persistence.ONCE)]
        assert tray_app._pending_decisions == {}, "the queue must not hold a decision it already applied"

    def test_a_queued_decision_wins_over_the_hid_lock_flow(self, tray_app, fake_client, mocker):
        """The queued choice is the user's; the lock-first flow is the default for undecided devices.

        Without this the keyboard lands in _hid_pending_devices and is
        auto-allowed on the next unlock -- the exact opposite of the `Block`
        the user clicked while it was off the bus.
        """
        self._queue(tray_app, mocker, self._KEYBOARD)

        self._insert(tray_app, self._KEYBOARD, DeviceTarget.BLOCK)

        assert fake_client.apply_policy_calls == [(301, DeviceTarget.BLOCK, Persistence.ONCE)]
        assert tray_app._hid_pending_devices == set(), "a decided device never enters the lock-first flow"
        assert not tray_app._hid_lock_timer.isActive(), "nothing to lock for"
        assert tray_app._pending_decisions == {}

    def test_a_queued_decision_applies_even_while_the_screen_is_locked(self, tray_app, fake_client,
                                                                       fake_screensaver, mocker):
        """Deferral is for devices nobody has decided about yet."""
        self._queue(tray_app, mocker, self._IR)
        fake_screensaver._active = True

        self._insert(tray_app, self._IR, DeviceTarget.BLOCK)

        assert fake_client.apply_policy_calls == [(301, DeviceTarget.BLOCK, Persistence.ONCE)]
        assert tray_app._screensaver_pending_devices == set(), "already decided -- nothing to prompt on unlock"

    def test_an_undecided_device_is_untouched_by_the_drain(self, tray_app, fake_client, mocker):
        """The drain must not swallow the normal paths it now runs ahead of."""
        mocker.patch.object(tray_app._tray, "showMessage")

        self._insert(tray_app, self._KEYBOARD, DeviceTarget.BLOCK)

        assert fake_client.apply_policy_calls == []
        assert tray_app._hid_pending_devices == {301}, "no queued decision, so the HID flow still owns it"


class TestAQueuedAllowStillObeysTheLockContract:
    """A queued decision must not become a way around the lock-first flow.

    Draining the queue ahead of everything is right for `Block` -- it is
    strictly safer than the default.  For `Allow` on a device with a HID
    interface it is not: the whole point of the lock-first flow is that a
    keyboard is only ever authorized behind a password prompt, and a decision
    the user made minutes ago while the device was off the bus is no substitute
    for that.  So the live authorize goes back to the lock flow.  The `Once`
    clear that goes with it is not performed either -- see
    `TestAHandbackQueuedAllowSaysWhatWasLost` for why, and for the warning
    that keeps the loss from being silent.
    """

    _KEYBOARD = (
        'block id 046d:c52b serial "KB1" name "Keyboard" hash "kbhash1" '
        'parent-hash "" via-port "1-3" with-interface 03:01:01 with-connect-type "hotplug"'
    )
    _IR = (
        'block id 045c:0131 serial "IR1" name "Smart IR Blaster" hash "irhash1" '
        'parent-hash "" via-port "1-2" with-interface ff:00:00 with-connect-type "hotplug"'
    )

    def _queue(self, tray_app, mocker, rule: str, target: DeviceTarget, persistence: Persistence) -> None:
        mocker.patch.object(tray_app._tray, "showMessage")
        tray_app._show_device_dialog(Device.from_dbus(292, rule))
        dialog = tray_app._open_dialogs[292]
        tray_app._on_device_presence_changed(292, int(PresenceEvent.REMOVE), int(DeviceTarget.BLOCK), rule, {})
        dialog._choose(target, persistence)

    def _insert(self, tray_app, rule: str) -> None:
        tray_app._on_device_presence_changed(301, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK), rule, {})

    def test_a_queued_allow_on_a_hid_device_goes_through_the_lock(self, tray_app, fake_client, mocker):
        self._queue(tray_app, mocker, self._KEYBOARD, DeviceTarget.ALLOW, Persistence.ONCE)

        self._insert(tray_app, self._KEYBOARD)

        assert fake_client.apply_policy_calls == [], "no live allow without the lock gate"
        assert tray_app._hid_pending_devices == {301}, "the lock-first flow owns the authorize"
        assert tray_app._hid_lock_timer.isActive()

    def test_an_always_never_reaches_the_queue_at_all(self, tray_app, fake_client, mocker):
        """So the lock gate costs the user nothing durable.

        A permanent rule needs no live device, so `Always` chosen while the
        device is away is written at click time rather than queued -- which is
        why handing a queued `Allow` back to the lock flow can only ever defer
        a `Once`, never discard an `Always`.
        """
        self._queue(tray_app, mocker, self._KEYBOARD, DeviceTarget.ALLOW, Persistence.ALWAYS)

        assert tray_app._pending_decisions == {}, "written, not queued"
        assert fake_client.persist_rule_calls == [(292, DeviceTarget.ALLOW, self._KEYBOARD)]
        assert fake_client.apply_policy_calls == [], "no live device to authorize"

    def test_a_once_queue_writes_no_rule(self, tray_app, fake_client, mocker):
        self._queue(tray_app, mocker, self._KEYBOARD, DeviceTarget.ALLOW, Persistence.ONCE)

        self._insert(tray_app, self._KEYBOARD)

        assert fake_client.persist_rule_calls == []

    def test_a_queued_block_on_a_hid_device_applies_at_once(self, tray_app, fake_client, mocker):
        """Block is strictly safer than the flow it replaces, so it needs no gate."""
        self._queue(tray_app, mocker, self._KEYBOARD, DeviceTarget.BLOCK, Persistence.ONCE)

        self._insert(tray_app, self._KEYBOARD)

        assert fake_client.apply_policy_calls == [(301, DeviceTarget.BLOCK, Persistence.ONCE)]
        assert tray_app._hid_pending_devices == set()

    def test_a_queued_allow_on_a_non_hid_device_applies_at_once(self, tray_app, fake_client, mocker):
        """The contract is about HID; nothing else is gated."""
        self._queue(tray_app, mocker, self._IR, DeviceTarget.ALLOW, Persistence.ONCE)

        self._insert(tray_app, self._IR)

        assert fake_client.apply_policy_calls == [(301, DeviceTarget.ALLOW, Persistence.ONCE)]

    def test_a_queued_allow_applies_at_once_when_hid_treatment_is_off(self, tray_app, fake_client,
                                                                      fake_settings, mocker):
        """There is no lock-first flow to defer to once the user has disabled it."""
        self._queue(tray_app, mocker, self._KEYBOARD, DeviceTarget.ALLOW, Persistence.ONCE)
        fake_settings.set_disable_hid_treatment(True)

        self._insert(tray_app, self._KEYBOARD)

        assert fake_client.apply_policy_calls == [(301, DeviceTarget.ALLOW, Persistence.ONCE)]
        assert tray_app._hid_pending_devices == set()


class TestQueuedDecisionSafetyAndIdentity:
    """Held choices obey the lock gate and cannot migrate to a sibling hub."""

    @pytest.mark.parametrize("lock_state", ["unavailable", "inhibited"])
    @pytest.mark.parametrize("interfaces", ["03:01:01", "{ 03:01:01 08:06:50 }"])
    def test_held_hid_allow_requires_a_fresh_choice_when_lock_cannot_run(self, tray_app, fake_client,
                                                                         fake_screensaver, queued_decision,
                                                                         lock_state, interfaces):
        rule = KEYBOARD_RULE.replace("with-interface 03:01:01", f"with-interface {interfaces}")
        show = queued_decision(DeviceTarget.ALLOW, rule=rule)
        if lock_state == "unavailable":
            fake_screensaver._connected = False
            fake_screensaver.connection_changed.emit(False)
        else:
            fake_screensaver._inhibited = True

        tray_app._on_device_presence_changed(301, PresenceEvent.INSERT, DeviceTarget.BLOCK, rule, {})

        assert not fake_screensaver.active
        assert fake_client.apply_policy_calls == []
        assert fake_client.persist_rule_calls == []
        assert tray_app._pending_decisions == {}
        assert any(c.args[0] == HANDBACK_NOTICE_TITLE for c in show.call_args_list)
        assert tray_app._open_dialogs[301].device_present
        assert tray_app._hid_pending_devices == set()
        assert not tray_app._hid_lock_timer.isActive()
        if lock_state == "inhibited":
            # A fresh click with the device in hand is the normal inhibited flow.
            tray_app._open_dialogs[301]._on_allow_once()
            assert fake_client.apply_policy_calls == [(301, DeviceTarget.ALLOW, Persistence.ONCE)]

    def test_same_hash_hubs_keep_separate_dialogs_and_click_targets(self, tray_app, fake_client):
        first = IR_RULE
        sibling = first.replace('via-port "1-2"', 'via-port "1-2.1"')
        tray_app._on_device_presence_changed(1, PresenceEvent.INSERT, DeviceTarget.BLOCK, first, {})
        first_dialog = tray_app._open_dialogs[1]
        tray_app._on_device_presence_changed(2, PresenceEvent.INSERT, DeviceTarget.BLOCK, sibling, {})

        assert set(tray_app._open_dialogs) == {1, 2}
        assert tray_app._open_dialogs[1] is first_dialog
        first_dialog._on_allow_once()
        assert fake_client.apply_policy_calls == [(1, DeviceTarget.ALLOW, Persistence.ONCE)]
        assert fake_client.apply_policy_rules == [first]
        assert 2 in tray_app._open_dialogs

    @pytest.mark.parametrize("attribute", ["via-port", "parent-hash"])
    def test_sibling_cannot_consume_a_queued_choice_or_its_cooldown(self, tray_app, fake_client,
                                                                    queued_decision, attribute):
        queued_decision(DeviceTarget.BLOCK, rule=IR_RULE)
        original = Device.from_dbus(292, IR_RULE)
        old_value = original.via_port if attribute == "via-port" else original.parent_hash
        sibling = IR_RULE.replace(f'{attribute} "{old_value}"', f'{attribute} "different"')

        tray_app._on_device_presence_changed(301, PresenceEvent.INSERT, DeviceTarget.BLOCK, sibling, {})

        assert fake_client.apply_policy_calls == []
        assert tray_app._pending_decisions
        assert 301 in tray_app._open_dialogs, "A sibling needs its own prompt, even during the cooldown"
        tray_app._on_device_presence_changed(302, PresenceEvent.INSERT, DeviceTarget.BLOCK, IR_RULE, {})
        assert fake_client.apply_policy_calls == [(302, DeviceTarget.BLOCK, Persistence.ONCE)]
        assert tray_app._pending_decisions == {}

    def test_queued_once_forwards_the_returning_instances_rule(self, tray_app, fake_client, queued_decision):
        queued_decision(DeviceTarget.BLOCK, rule=IR_RULE)
        updated = IR_RULE.replace('with-connect-type "hotplug"', 'with-connect-type "unknown"')

        tray_app._on_device_presence_changed(301, PresenceEvent.INSERT, DeviceTarget.ALLOW, updated, {})

        assert fake_client.apply_policy_calls == [(301, DeviceTarget.BLOCK, Persistence.ONCE)]
        assert fake_client.apply_policy_rules == [updated]
        assert tray_app._pending_decisions == {}

    def test_queued_choice_waits_for_lock_availability_and_a_fresh_click_supersedes_it(
            self, tray_app, fake_client, fake_screensaver, queued_decision):
        queued_decision(DeviceTarget.BLOCK, rule=IR_RULE)
        fake_screensaver._connected = False
        fake_screensaver.connection_changed.emit(False)
        tray_app._on_device_presence_changed(301, PresenceEvent.INSERT, DeviceTarget.BLOCK, IR_RULE, {})
        assert fake_client.apply_policy_calls == []
        assert tray_app._pending_decisions

        fake_screensaver._connected = True
        fake_screensaver.connection_changed.emit(True)
        tray_app._open_dialogs[301]._on_allow_once()

        assert fake_client.apply_policy_calls == [(301, DeviceTarget.ALLOW, Persistence.ONCE)]
        assert tray_app._pending_decisions == {}, "The superseded block must never replay later"


def _handback_notice(show) -> tuple[str, str]:
    """The handback notice the tray was shown, selected by identity not prose."""
    for call in show.call_args_list:
        if call.args[0] == HANDBACK_NOTICE_TITLE:
            return call.args[0], call.args[1]
    raise AssertionError(f"no handback notice was raised; got {[c.args[0] for c in show.call_args_list]}")


def _says_clear_did_not_happen(body: str) -> bool:
    """Does the wording report that no permanent rule was cleared?

    Keyed on the claim, not on one sentence.  The wording is user-facing and
    gets improved; an assertion pinned to a literal turns every rewording into
    a test failure that has nothing to do with whether the claim is being made.
    """
    return bool(_NEGATED_CLEAR.search(body.lower()))


def _asserts_a_rule_the_app_never_read(body: str) -> bool:
    """Does the wording state as fact that the device *has* a standing rule?

    `_apply_pending_decision` never reads the ruleset -- the app tracks
    permanent *allow* hashes only -- so it cannot say one is there.  "cleared
    no permanent rule" and "any rule it has" are both fine; a definite
    possessive is not, because it asserts existence the code never checked.
    """
    return bool(_EXISTENCE_ASSERTION.search(body.lower()))


# Any way of saying the clear did not happen.  Apostrophes are matched both
# ways because user-facing strings drift between ASCII and typographic.
_NEGATED_CLEAR = re.compile(r"cleared no|n[o'\u2019]t cleared|n[o'\u2019]t clear|no permanent rule")

# A definite, unhedged reference to a standing permanent rule.
_EXISTENCE_ASSERTION = re.compile(r"\b(?:its|the|this)\s+standing permanent rule\b")


class TestAHandbackQueuedAllowSaysWhatWasLost:
    """F3 -- handing a queued `Allow` back to the lock flow drops the `Once`
    clear, and that has to be said out loud.

    The handback is right for the *live* half: a click made minutes ago while
    the device was off the bus does not prove anybody is at the machine, so
    the authorize waits for the lock.  But `Once` is two halves, and the
    durable one -- "clear the standing permanent rule" -- is not performed by
    the lock-first flow, which authorizes with `UNCHANGED`.  Before this the
    decision was popped and dropped with one log line, so a user who clicked
    *Allow Once* on a permanently blocked keyboard kept the permanent rule
    and was never told.

    The clear is deliberately *not* performed outside the lock: dropping a
    standing `block` widens what the *next* insertion does, and acting on a
    stale click is exactly what the lock contract refuses to do.  So the fix
    is honesty rather than action -- the user is told the standing rule
    survived, and can decide again with the device in hand.
    """

    def test_a_handback_warns_that_the_standing_rule_was_kept(self, tray_app, fake_client, queued_decision):
        show = queued_decision(DeviceTarget.ALLOW, Persistence.ONCE, KEYBOARD_RULE)

        tray_app._on_device_presence_changed(301, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK),
                                             KEYBOARD_RULE, {})

        assert tray_app._hid_pending_devices == {301}, "the lock-first flow owns the authorize"
        titles = [c.args[0] for c in show.call_args_list]
        assert any(t == HANDBACK_NOTICE_TITLE for t in titles), \
            f"the user must be told the clear did not happen; got {titles}"

    def test_the_handback_warning_says_the_clear_was_not_made(self, tray_app, fake_client, queued_decision):
        show = queued_decision(DeviceTarget.ALLOW, Persistence.ONCE, KEYBOARD_RULE)

        tray_app._on_device_presence_changed(301, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK),
                                             KEYBOARD_RULE, {})

        _title, body = _handback_notice(show)
        assert _says_clear_did_not_happen(body), \
            f"the durable half of the click is what did not happen: {body!r}"
        assert "did not take effect" not in body, \
            "the live half DOES still take effect behind the lock -- do not claim otherwise"

    def test_the_notice_title_claims_no_lock_screen(self, tray_app, fake_client, queued_decision):
        """The title is the one part with no room to hedge, so it must not claim.

        Whether a lock screen ever arrives is decided after
        `_apply_pending_decision` returns -- a device that comes back already
        allowed never enters the lock flow at all.  A title naming the lock is
        therefore a forecast, and forecasts are what this notice got wrong.
        """
        show = queued_decision(DeviceTarget.ALLOW, Persistence.ONCE, KEYBOARD_RULE)

        tray_app._on_device_presence_changed(301, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK),
                                             KEYBOARD_RULE, {})

        title, _body = _handback_notice(show)
        assert "lock" not in title.lower(), f"the title forecasts a lock screen: {title!r}"

    def test_the_notice_bodies_never_promise_a_live_authorize(self, tray_app, fake_client, queued_decision):
        """Checked against the whole promise list, not one string that was once wrong."""
        show = queued_decision(DeviceTarget.ALLOW, Persistence.ONCE, KEYBOARD_RULE)

        tray_app._on_device_presence_changed(301, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK),
                                             KEYBOARD_RULE, {})

        _title, body = _handback_notice(show)
        promised = [p for p in _LIVE_AUTHORIZE_PROMISES if p in body.lower()]
        assert not promised, f"the notice forecasts the live half: {promised} in {body!r}"

    def test_the_handback_warning_names_the_device(self, tray_app, fake_client, queued_decision):
        show = queued_decision(DeviceTarget.ALLOW, Persistence.ONCE, KEYBOARD_RULE)

        tray_app._on_device_presence_changed(301, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK),
                                             KEYBOARD_RULE, {})

        body = next(c.args[1] for c in show.call_args_list if c.args[0] == HANDBACK_NOTICE_TITLE)
        assert "301" in body, "the user needs to know which device still carries the rule"

    def test_a_queued_allow_that_applied_live_raises_no_handback_warning(self, tray_app, fake_client, queued_decision):
        """Nothing was handed back on a non-HID device, so nothing was lost."""
        show = queued_decision(DeviceTarget.ALLOW, Persistence.ONCE, IR_RULE)

        tray_app._on_device_presence_changed(301, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK),
                                             IR_RULE, {})

        assert fake_client.apply_policy_calls == [(301, DeviceTarget.ALLOW, Persistence.ONCE)]
        assert not any(c.args[0] == HANDBACK_NOTICE_TITLE for c in show.call_args_list)

    def test_a_queued_block_raises_no_handback_warning(self, tray_app, fake_client, queued_decision):
        """A queued Block applies whole -- no handback, no lost half."""
        show = queued_decision(DeviceTarget.BLOCK, Persistence.ONCE, KEYBOARD_RULE)

        tray_app._on_device_presence_changed(301, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK),
                                             KEYBOARD_RULE, {})

        assert fake_client.apply_policy_calls == [(301, DeviceTarget.BLOCK, Persistence.ONCE)]
        assert not any(c.args[0] == HANDBACK_NOTICE_TITLE for c in show.call_args_list)

    def test_the_handback_still_leaves_no_queued_decision_behind(self, tray_app, fake_client, queued_decision):
        """The warning is not a re-queue: the entry must not linger and fire twice."""
        queued_decision(DeviceTarget.ALLOW, Persistence.ONCE, KEYBOARD_RULE)

        tray_app._on_device_presence_changed(301, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK),
                                             KEYBOARD_RULE, {})

        assert tray_app._pending_decisions == {}


class TestLockerRestartInvalidatesLockState:
    """A replacement locker cannot authorize HID using the departed owner's state."""

    def test_unlocked_replacement_does_not_auto_allow_before_its_state_arrives(self, tray_app, fake_client):
        import asyncio
        from unittest.mock import AsyncMock

        from usbguard_gui.screensaver import SCREENSAVER_BUS_NAME, ScreensaverMonitor, _ScreensaverThread

        monitor = ScreensaverMonitor()
        tray_app._screensaver = monitor
        monitor.connection_changed.connect(tray_app._engine._on_lock_availability_changed)
        monitor.active_changed.connect(tray_app._engine._on_screensaver_locked)
        worker = _ScreensaverThread()
        worker.connected.connect(monitor._on_connected)
        worker.active_changed.connect(monitor._on_active_changed)

        async def scenario():
            worker._loop = asyncio.get_running_loop()
            worker._proxy = MagicMock(call_get_active=AsyncMock(return_value=False))
            worker._on_active_changed(True)
            worker._on_name_owner_changed(SCREENSAVER_BUS_NAME, ":old", "")
            worker._on_name_owner_changed(SCREENSAVER_BUS_NAME, "", ":new")
            tray_app._on_device_presence_changed(1, PresenceEvent.INSERT, DeviceTarget.BLOCK, KEYBOARD_RULE, {})
            assert not monitor.connected
            assert not monitor.active
            for _ in range(3):
                await asyncio.sleep(0)

        asyncio.run(scenario())
        assert monitor.connected
        assert not monitor.active
        assert fake_client.apply_policy_calls == []


class TestTheHandbackWarningPromisesNothingItCannotKeep:
    """F6 -- the warning fires *before* the already-allowed early return.

    `_apply_pending_decision` runs ahead of every other reaction to an
    insertion; that ordering is the whole point of it.  But one of the
    reactions it runs ahead of is "the device came back already allowed, do
    nothing", and a HID device reaches that when a permanent allow rule put it
    there.  The lock-first flow then never runs, so a notice telling the user
    their Allow "will be authorized only behind the lock screen" described
    something that was not going to happen -- harmless to the device, which is
    allowed either way, but the user is being told about a password prompt
    they will never see.

    The notice itself still belongs on this path: its load-bearing half is that
    no permanent rule was cleared, and that is true however the insertion is
    handled.  Whether a rule *exists* is not something this method knows -- the
    app tracks permanent *allow* hashes only, and a device returning already
    allowed may be governed by a broader wildcard rule (which a `Once` never
    removes) or by the daemon's default policy with no device-specific rule at
    all.  So the notice states only what holds either way, and says nothing
    about the live half, which it does not get to decide.
    """

    def test_the_notice_still_fires_when_the_device_returns_allowed(self, tray_app, fake_client, queued_decision):
        show = queued_decision(DeviceTarget.ALLOW, Persistence.ONCE, KEYBOARD_RULE)

        tray_app._on_device_presence_changed(301, int(PresenceEvent.INSERT), int(DeviceTarget.ALLOW),
                                             KEYBOARD_RULE, {})

        assert any(c.args[0] == HANDBACK_NOTICE_TITLE for c in show.call_args_list), \
            "the kept permanent rule is real however the insertion is handled"

    def test_the_notice_does_not_promise_an_authorize_that_never_happens(self, tray_app, fake_client, queued_decision):
        show = queued_decision(DeviceTarget.ALLOW, Persistence.ONCE, KEYBOARD_RULE)

        tray_app._on_device_presence_changed(301, int(PresenceEvent.INSERT), int(DeviceTarget.ALLOW),
                                             KEYBOARD_RULE, {})

        assert tray_app._hid_pending_devices == set(), "an already-allowed device never enters the lock flow"
        title, body = _handback_notice(show)
        promised = [p for p in _LIVE_AUTHORIZE_PROMISES if p in body.lower()]
        assert not promised, \
            f"no lock screen is coming on this path; do not promise one: {promised} in {body!r}"
        assert "lock" not in title.lower(), f"nor in the title: {title!r}"

    def test_the_notice_does_not_assert_a_permanent_rule_the_app_never_looked_for(self, tray_app,
                                                                                  fake_client,
                                                                                  queued_decision):
        """The app tracks permanent *allow* hashes only -- it cannot know.

        A keyboard prompted during a lock inhibitor (a dnf transaction, "prevent
        screen lock"), flapped, clicked `Allow Once` and returned once the
        inhibitor lifted has usually no standing rule at all, and the clear
        would have been a no-op.  Stating flatly that "its standing permanent
        rule was not cleared" sends that user looking through rules.conf for a
        rule that was never there.
        """
        show = queued_decision(DeviceTarget.ALLOW, Persistence.ONCE, KEYBOARD_RULE)

        tray_app._on_device_presence_changed(301, int(PresenceEvent.INSERT), int(DeviceTarget.BLOCK),
                                             KEYBOARD_RULE, {})

        _title, body = _handback_notice(show)
        assert _says_clear_did_not_happen(body), \
            f"the durable half of the click is still what did not happen: {body!r}"
        assert not _asserts_a_rule_the_app_never_read(body), \
            f"the app never read the ruleset -- it must not assert the rule exists: {body!r}"

    def test_a_queued_block_on_an_allowed_return_still_applies(self, tray_app, fake_client, queued_decision):
        """The handback is the ALLOW-only exception; a Block is not softened.

        A device coming back already allowed is precisely when a queued `Block`
        matters most, and it runs before the already-allowed return that would
        otherwise leave the keyboard live.
        """
        show = queued_decision(DeviceTarget.BLOCK, Persistence.ONCE, KEYBOARD_RULE)

        tray_app._on_device_presence_changed(301, int(PresenceEvent.INSERT), int(DeviceTarget.ALLOW),
                                             KEYBOARD_RULE, {})

        assert fake_client.apply_policy_calls == [(301, DeviceTarget.BLOCK, Persistence.ONCE)]
        assert not any(c.args[0] == HANDBACK_NOTICE_TITLE for c in show.call_args_list)


class TestDecisionStateSources:
    """The two signal feeds that decide before any prompt runs.

    `_permanent_allow_hashes` decides whether a returning HID device is
    skipped as already-whitelisted (app.py:485), and the permanent-write
    failure is the only thing that tells the user a "permanent" choice
    lasted until unplug.  Both arrive over client signals that no test had
    ever emitted — the seeding path especially, since the whole cache was
    being maintained untested.
    """

    _ALLOW = ('allow id 1234:abcd serial "" name "Camera" hash "camallow" '
              'parent-hash "" via-port "1-1" with-interface 0e:01:00 with-connect-type "hotplug"')
    _BLOCK = ('block id 1234:abcd serial "" name "Keyboard" hash "kbhash9" '
              'parent-hash "" via-port "1-1" with-interface 03:00:00 with-connect-type "hotplug"')
    _ALLOW_NO_HASH = 'allow id 1234:abcd serial "" name "NoHash" with-connect-type "hotplug"'

    def test_allow_rule_hashes_seed_the_cache(self, tray_app, fake_client) -> None:
        fake_client.list_rules_result.emit([(1, self._ALLOW), (2, self._BLOCK), (3, self._ALLOW_NO_HASH)])

        assert tray_app._permanent_allow_hashes == {"camallow"}

    def test_a_fresh_result_replaces_the_stale_cache(self, tray_app, fake_client) -> None:
        tray_app._permanent_allow_hashes.add("gone")

        fake_client.list_rules_result.emit([(1, self._ALLOW)])

        assert tray_app._permanent_allow_hashes == {"camallow"}

    def test_a_permanent_write_failure_announces_itself(self, tray_app, fake_client, mocker) -> None:
        show = mocker.patch.object(tray_app._tray, "showMessage")

        fake_client.permanent_write_failed.emit(54, "allow", "Not authorized")

        assert len(show.call_args_list) == 1
        title, body, _icon, _ms = show.call_args.args
        assert title == "Permanent rule not saved"
        assert "54" in body and "allow" in body, f"the notice must name device and action: {body!r}"
        assert "Not authorized" in body, "the daemon's reason is the only diagnosis the user gets"
        assert "only until the device is unplugged" in body, \
            "without this the user reads a failed permanent rule as done"


class TestInsertionEntryGuards:
    """The skips in front of the flow: only a fresh INSERT starts a reaction.

    PRESENT fires for devices already connected at daemon start and UPDATE
    on policy changes; treating either as an insertion would spawn dialogs
    for every device at boot.  The empty-set guard on the HID lock timer
    covers the mirror image: every pending device was unplugged during the
    notification delay, and claiming a lock then would show a lie.
    """

    @pytest.mark.parametrize("event", [PresenceEvent.PRESENT, PresenceEvent.UPDATE])
    def test_non_insert_events_open_nothing_and_apply_nothing(self, tray_app, fake_client, event) -> None:
        fake_client.device_presence_changed.emit(1, int(event), int(DeviceTarget.BLOCK), KEYBOARD_RULE, {})

        assert tray_app._open_dialogs == {}
        assert tray_app._hid_pending_devices == set()
        assert tray_app._screensaver_pending_devices == set()
        assert fake_client.apply_policy_calls == []

    def test_the_lock_timer_with_no_pending_devices_claims_no_lock(self, tray_app, fake_screensaver) -> None:
        tray_app._hid_pending_devices.clear()

        tray_app._engine._lock_for_pending_hid()

        assert fake_screensaver.lock_calls == 0

    def test_a_stale_identity_entry_cannot_retarget_a_dialog_that_is_gone(self, tray_app) -> None:
        """Identity remembers a dialog the map no longer holds — return False, keep both maps intact."""
        device = Device.from_dbus(7, IR_RULE)
        tray_app._open_dialog_identities[dialog_identity(device)] = 99

        assert tray_app._retarget_device_dialog(device) is False
        assert tray_app._open_dialogs == {}


class TestApplyPendingDecisionReturnContract:
    """The drain's return value decides whether an insertion is consumed.

    `_apply_pending_decision` runs before every other reaction at two call
    sites (the INSERT handler and the prompt path): True means the user
    already decided and nothing else may react; False means fall through to
    the default flow.  The outcomes are reachable end-to-end through an
    INSERT, but the method itself is what the engine extraction moves
    wholesale — these pin it directly so a reordered branch fails here
    before the flows that wrap it go green.
    """

    def test_nothing_queued_falls_through(self, tray_app) -> None:
        assert tray_app._engine._apply_pending_decision(Device.from_dbus(1, IR_RULE)) is False

    def test_a_queued_block_is_consumed(self, tray_app, fake_client) -> None:
        device = Device.from_dbus(301, IR_RULE)
        tray_app._pending_decisions[dialog_identity(device)] = (DeviceTarget.BLOCK,
                                                                Persistence.ONCE)

        assert tray_app._engine._apply_pending_decision(device) is True
        assert fake_client.apply_policy_calls == [(301, DeviceTarget.BLOCK, Persistence.ONCE)]
        assert tray_app._pending_decisions == {}

    def test_a_queued_hid_allow_is_handed_back_not_consumed(self, tray_app, fake_client, mocker) -> None:
        show = mocker.patch.object(tray_app._tray, "showMessage")
        device = Device.from_dbus(301, KEYBOARD_RULE)
        tray_app._pending_decisions[dialog_identity(device)] = (DeviceTarget.ALLOW,
                                                                Persistence.ONCE)

        assert tray_app._engine._apply_pending_decision(device) is False
        assert fake_client.apply_policy_calls == [], "a stale click may never authorize a HID device"
        assert tray_app._pending_decisions == {}, "the handback is not a re-queue"
        assert any(c.args[0] == HANDBACK_NOTICE_TITLE for c in show.call_args_list)

    def test_lock_unavailable_keeps_the_decision_pending(self, tray_app, fake_client, fake_screensaver,
                                                         mocker) -> None:
        mocker.patch.object(tray_app._tray, "showMessage")
        fake_screensaver._connected = False
        fake_screensaver.connection_changed.emit(False)
        device = Device.from_dbus(301, IR_RULE)
        identity = dialog_identity(device)
        tray_app._pending_decisions[identity] = (DeviceTarget.BLOCK, Persistence.ONCE)

        assert tray_app._engine._apply_pending_decision(device) is False
        assert tray_app._pending_decisions == {identity: (DeviceTarget.BLOCK, Persistence.ONCE)}, \
            "the choice must survive until locking can run"
        assert fake_client.apply_policy_calls == []


class TestDecisionEngineStateSeam:
    """Phase 3: the tray's decision state is storage owned by DecisionEngine.

    The shims on `USBGuardTrayApp` must be transparent — assignment through
    the tray reaches the engine and engine-side changes show through the
    tray — or the handlers (still on their old paths) and the tests poking
    `tray_app._x` would be reading a private copy instead of the truth.
    """

    def test_the_engine_starts_with_an_empty_decision_state(self, tray_app) -> None:
        engine = tray_app._engine

        assert engine._pending_decisions == {}
        assert engine._last_prompted_at == {}
        assert engine._hid_pending_devices == set()
        assert engine._screensaver_pending_devices == set()
        assert engine._pending_unlock_cycles == {}
        assert engine._next_unlock_cycle_id == 0
        assert engine._permanent_allow_hashes == set()
        assert engine._lock_available is True
        assert engine._lock_state_confirmed is False

    def test_the_effect_signals_are_declared_for_phase_4(self, tray_app) -> None:
        for name in ("show_dialog", "dialog_retarget", "notify", "schedule_lock"):
            assert hasattr(tray_app._engine, name), f"the engine must expose {name}"

    def test_assignment_through_the_tray_reaches_the_engine(self, tray_app) -> None:
        tray_app._hid_pending_devices = {7}
        tray_app._lock_available = False
        tray_app._next_unlock_cycle_id = 3

        assert tray_app._engine._hid_pending_devices == {7}
        assert tray_app._engine._lock_available is False
        assert tray_app._engine._next_unlock_cycle_id == 3

    def test_engine_side_changes_show_through_the_tray(self, tray_app) -> None:
        tray_app._engine._pending_decisions["x"] = (DeviceTarget.BLOCK, Persistence.ONCE)

        assert tray_app._pending_decisions["x"] == (DeviceTarget.BLOCK, Persistence.ONCE)

    def test_the_engine_reads_the_monitors_state_at_construction(
            self, qapp, fake_client, fake_screensaver, fake_settings) -> None:
        fake_screensaver._connected = False

        with (
            patch("usbguard_gui.app.USBGuardClient", return_value=fake_client),
            patch("usbguard_gui.app.ScreensaverMonitor", return_value=fake_screensaver),
        ):
            app = USBGuardTrayApp(qapp, settings=fake_settings)

        assert app._engine._lock_available is False
        app._quit()
