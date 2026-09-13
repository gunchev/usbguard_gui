"""Tests for permanent-allow rule handling across KVM/dock topologies.

USBGuard's applyDevicePolicy(..., permanent=True) *upserts* the rule it
generates for a device, keyed on the device hash. Two physically distinct
devices that hash identically -- chained hubs of the same model reporting the
same serial, as a KVM switch produces -- can therefore never both hold a
permanent rule: allowing one silently replaces the other's rule, so each
switch cycle prompts again. Observed live: rule id 16 removed and re-added
with a different parent-hash on every switch.

The app therefore applies the decision temporarily and appends the exact
device rule permanently, preserving parent-hash and via-port so each topology
gets its own coexisting rule.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from usbguard_gui.dbus_client import _APPEND_RULE_AT_END, _DBusThread, _retarget_device_rule
from usbguard_gui.device import Device, DeviceTarget

# The two same-hash hub instances seen on the KVM switch; they differ only in
# parent-hash, which is exactly what the upsert discards.
HUB_A = ('allow id 2109:2817 serial "000000000" name "USB2.0 Hub" '
         'hash "kj7MUN8qdDfj2pO0aUpZ2tOY7UIlzSNGG7bI9jnAeu4=" '
         'parent-hash "xe96rjr8V53Jw+g7q/yi0C1czVxatehiq7r4gn2dH6s=" '
         'via-port "3-3.1.1" with-interface { 09:00:01 09:00:02 } with-connect-type "unknown"')
HUB_B = ('allow id 2109:2817 serial "000000000" name "USB2.0 Hub" '
         'hash "kj7MUN8qdDfj2pO0aUpZ2tOY7UIlzSNGG7bI9jnAeu4=" '
         'parent-hash "kj7MUN8qdDfj2pO0aUpZ2tOY7UIlzSNGG7bI9jnAeu4=" '
         'via-port "3-3.1.1.4" with-interface { 09:00:01 09:00:02 } with-connect-type "unknown"')
BLOCKED_HUB = HUB_A.replace("allow ", "block ", 1)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestRetargetDeviceRule:
    """Only the target verb changes; every attribute is preserved verbatim."""

    def test_block_rule_becomes_allow(self):
        assert _retarget_device_rule(BLOCKED_HUB, DeviceTarget.ALLOW) == HUB_A

    def test_preserves_parent_hash_and_via_port(self):
        out = _retarget_device_rule(BLOCKED_HUB, DeviceTarget.ALLOW)
        assert 'parent-hash "xe96rjr8V53Jw+g7q/yi0C1czVxatehiq7r4gn2dH6s="' in out
        assert 'via-port "3-3.1.1"' in out

    @pytest.mark.parametrize(
        ("target", "verb"),
        [(DeviceTarget.ALLOW, "allow"), (DeviceTarget.BLOCK, "block"), (DeviceTarget.REJECT, "reject")],
    )
    def test_each_target_verb(self, target, verb):
        assert _retarget_device_rule(HUB_A, target).split()[0] == verb

    def test_rule_with_only_a_target(self):
        assert _retarget_device_rule("block", DeviceTarget.ALLOW) == "allow"

    def test_leading_whitespace_is_tolerated(self):
        assert _retarget_device_rule("  block id 1234:5678", DeviceTarget.ALLOW) == "allow id 1234:5678"


class TestPermanentAllowAppendsRule:
    """A permanent decision must append, never let USBGuard upsert."""

    def _thread(self):
        thread = _DBusThread()
        thread._connected = True
        thread._devices_iface = MagicMock()
        thread._devices_iface.call_apply_device_policy = AsyncMock(return_value=7)
        thread._policy_iface = MagicMock()
        thread._policy_iface.call_list_rules = AsyncMock(return_value=[])
        thread._policy_iface.call_append_rule = AsyncMock(return_value=23)
        return thread

    def test_permanent_applies_temporarily_then_appends(self):
        thread = self._thread()

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        # The live device is authorized without a permanent upsert...
        thread._devices_iface.call_apply_device_policy.assert_awaited_once_with(
            54, int(DeviceTarget.ALLOW), False
        )
        # ...and the durable rule is appended verbatim, at the end, non-temporary.
        thread._policy_iface.call_append_rule.assert_awaited_once_with(
            HUB_A, _APPEND_RULE_AT_END, False
        )

    def test_appended_rule_keeps_the_topology(self):
        """The whole point: parent-hash must survive, or the sibling hub's rule
        is the one USBGuard matches and the ping-pong continues."""
        thread = self._thread()

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        appended = thread._policy_iface.call_append_rule.await_args.args[0]
        assert 'parent-hash "xe96rjr8V53Jw+g7q/yi0C1czVxatehiq7r4gn2dH6s="' in appended
        assert 'via-port "3-3.1.1"' in appended

    def test_both_topologies_append_distinct_rules(self):
        """Allowing hub A then hub B must yield two rules, not one replaced twice."""
        thread = self._thread()

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, HUB_A.replace("allow ", "block ", 1)))
        _run(thread._do_apply_policy(56, DeviceTarget.ALLOW, True, HUB_B.replace("allow ", "block ", 1)))

        appended = [c.args[0] for c in thread._policy_iface.call_append_rule.await_args_list]
        assert appended == [HUB_A, HUB_B]
        assert len(set(appended)) == 2  # distinct: they coexist

    def test_temporary_uses_apply_device_policy_only(self):
        thread = self._thread()

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, False, BLOCKED_HUB))

        thread._devices_iface.call_apply_device_policy.assert_awaited_once_with(
            54, int(DeviceTarget.ALLOW), False
        )
        thread._policy_iface.call_append_rule.assert_not_awaited()

    def test_permanent_without_a_rule_falls_back_to_upsert(self):
        """No raw rule available (e.g. a stale device view) -- keep the old
        behaviour rather than silently doing nothing."""
        thread = self._thread()

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, None))

        thread._devices_iface.call_apply_device_policy.assert_awaited_once_with(
            54, int(DeviceTarget.ALLOW), True
        )
        thread._policy_iface.call_append_rule.assert_not_awaited()

    def test_append_at_end_sentinel_value(self):
        """UINT32_MAX-2; verified against the live daemon, which appended after
        the last rule and returned a fresh id."""
        assert _APPEND_RULE_AT_END == 4294967293


class _FakePolicy:
    """Stand-in for Policy1 holding a real permanent ruleset.

    appendRule always adds — the daemon never deduplicates, that is precisely
    the finding — removeRule deletes, listRules reports what is there now.
    Driving the client against state rather than bare mocks is what makes the
    accumulation tests mean something: they assert on the rules.conf the user
    ends up with, not on how many times a mock was poked.
    """

    def __init__(self, rules=None):
        self.rules = list(rules or [])
        self.calls = []
        self._next_id = max((rule_id for rule_id, _ in self.rules), default=0) + 1

    async def call_list_rules(self, label):
        self.calls.append(("list", label))
        return list(self.rules)

    async def call_append_rule(self, rule, parent_id, temporary):
        self.calls.append(("append", rule, parent_id, temporary))
        rule_id = self._next_id
        self._next_id += 1
        self.rules.append((rule_id, rule))
        return rule_id

    async def call_remove_rule(self, rule_id):
        self.calls.append(("remove", rule_id))
        self.rules = [(i, r) for i, r in self.rules if i != rule_id]

    def kinds(self):
        return [c[0] for c in self.calls]


def _stub_thread(policy=None):
    """_DBusThread wired to recording fakes for the daemon calls it makes."""
    thread = _DBusThread()
    thread._connected = True
    thread._devices_iface = MagicMock()
    thread._devices_iface.call_apply_device_policy = AsyncMock(return_value=7)
    thread._policy_iface = policy if policy is not None else _FakePolicy()
    return thread


def _rules_conf(policy):
    """The permanent ruleset as the user would find it in /etc/usbguard/rules.conf."""
    return [rule for _, rule in policy.rules]


class TestPermanentRuleDeduplication:
    """Finding B — repeated permanent decisions must not pile up rules.

    master's ``permanent=True`` *upserted* one rule per device hash, so
    re-deciding a device reused the entry that was already there.  Appending
    without looking first writes a fresh line every time: allow, block, then
    re-allow the same KVM port and three rules are left for one device, and
    the audit trail no longer records what the user decided.  The permanent
    path now reads the ruleset and updates the rule it finds.
    """

    def test_reallowing_same_topology_appends_only_one_rule(self):
        """Two permanent allows of one unchanged device == one rule."""
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))
        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        assert _rules_conf(policy) == [HUB_A]

    def test_allow_block_reallow_cycle_leaves_one_rule_holding_the_last_decision(self):
        """The ping-pong the UI actually invites: allow, block, allow again."""
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))
        _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, True, HUB_A))
        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        assert _rules_conf(policy) == [HUB_A]

    def test_repeated_permanent_decisions_do_not_grow_the_policy(self):
        """N permanent decisions on one device must not mean N rules."""
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        for _ in range(5):
            _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        assert len(policy.rules) == 1

    def test_target_change_replaces_the_rule_instead_of_adding_one(self):
        """A changed mind updates the existing entry; it does not stack."""
        policy = _FakePolicy([(7, HUB_A)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, True, HUB_A))

        assert _rules_conf(policy) == [BLOCKED_HUB]

    def test_removal_precedes_append_so_a_new_block_is_never_shadowed(self):
        """USBGuard is first-match-wins, so the stale rule has to go *before*
        the new one lands.  Appending first would park a fresh ``block`` under
        an older ``allow`` and silently ignore the user."""
        policy = _FakePolicy([(7, HUB_A)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, True, HUB_A))

        assert policy.kinds() == ["list", "remove", "append"]
        assert [c[1] for c in policy.calls if c[0] == "remove"] == [7]

    def test_replacement_still_authorizes_the_live_device_temporarily(self):
        """Dedup must not turn the live authorization back into a permanent
        upsert — the temporary apply is what keeps the HID lock-first gate the
        only thing deciding when a device goes live."""
        policy = _FakePolicy([(7, HUB_A)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, True, HUB_A))

        thread._devices_iface.call_apply_device_policy.assert_awaited_once_with(
            54, int(DeviceTarget.BLOCK), False
        )

    def test_sibling_topology_is_not_a_duplicate(self):
        """The KVM's other hub differs in parent-hash, so it is a different
        insertion point and keeps its own rule — dedup must not collapse them
        back into the single-rule behaviour that caused the original bug."""
        policy = _FakePolicy([(7, HUB_B)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        assert len(policy.rules) == 2
        assert "remove" not in policy.kinds()

    def test_unrelated_rules_are_left_untouched(self):
        """Only the matched device's rule moves; everything else keeps its
        place in the file."""
        webcam = ('allow id 04f2:b2ea serial "SN-CAM" name "Integrated Camera" '
                  'hash "camhash=" via-port "usb1" with-interface { 0e:01:00 }')
        policy = _FakePolicy([(3, webcam)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        assert _rules_conf(policy) == [webcam, HUB_A]

    def test_rule_naming_no_device_is_never_treated_as_this_device_s(self):
        """A class-wide rule shares no identity with a specific device, so it
        is neither a duplicate nor a replacement target — hand-written policy
        stays exactly where the admin put it."""
        broad = 'allow with-interface { 03:00:00 }'
        policy = _FakePolicy([(3, broad)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        assert _rules_conf(policy) == [broad, HUB_A]

    def test_whitespace_only_difference_is_not_a_new_rule(self):
        """Same rule, differently wrapped: nothing to write."""
        policy = _FakePolicy([(7, HUB_A.replace('name "USB2.0 Hub"', 'name  "USB2.0 Hub"'))])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        assert policy.kinds() == ["list"]

    def test_preexisting_duplicates_update_the_first_and_report_the_rest(self, caplog):
        """A policy already bloated by repeated decisions heals one rule at a
        time, and never silently: the extra copies are reported rather than
        deleted, because any of them could have been written by hand."""
        policy = _FakePolicy([(7, HUB_A), (9, HUB_A)])
        thread = _stub_thread(policy)

        with caplog.at_level(logging.WARNING, logger="usbguard_gui.dbus_client"):
            _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, True, HUB_A))

        assert [c[1] for c in policy.calls if c[0] == "remove"] == [7]
        assert _rules_conf(policy) == [HUB_A, BLOCKED_HUB]
        assert "9" in caplog.text

    def test_unreadable_ruleset_falls_back_to_appending(self):
        """Losing the user's permanent decision is worse than a possible
        duplicate, so an unreadable ruleset degrades to the old append."""
        from dbus_fast import DBusError, ErrorType

        policy = _FakePolicy()

        async def raise_error(label):
            raise DBusError(ErrorType.FAILED, "cannot read rules")

        policy.call_list_rules = raise_error
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        assert _rules_conf(policy) == [HUB_A]
        # An ordinary per-call failure must not flip the connection.
        assert thread._connected is True


class TestDeviceRawRule:
    """Device must carry the exact rule string USBGuard reported."""

    def test_from_dbus_preserves_raw_rule(self):
        device = Device.from_dbus(54, BLOCKED_HUB)
        assert device.raw_rule == BLOCKED_HUB

    def test_raw_rule_defaults_empty(self):
        assert Device(number=1, rule="block", id="1234:5678").raw_rule == ""

    def test_raw_rule_excluded_from_equality(self):
        """Two views of the same device must stay equal; raw_rule is carried
        data, not identity."""
        a = Device.from_dbus(54, BLOCKED_HUB)
        b = Device.from_dbus(54, BLOCKED_HUB.replace("block ", "block  ", 1))
        assert a == b

    def test_parsed_attributes_still_populated(self):
        device = Device.from_dbus(54, BLOCKED_HUB)
        assert device.id == "2109:2817"
        assert device.parent_hash == "xe96rjr8V53Jw+g7q/yi0C1czVxatehiq7r4gn2dH6s="
        assert device.via_port == "3-3.1.1"


class _RecordingClient(QObject):
    """Minimal USBGuardClient stand-in that records the device_rule argument."""

    connection_changed = pyqtSignal(bool)
    list_devices_result = pyqtSignal(list)
    list_rules_result = pyqtSignal(list)
    remove_rule_result = pyqtSignal(bool)
    device_presence_changed = pyqtSignal(int, int, str, dict)
    device_policy_changed = pyqtSignal(int, int, int, str, int, dict)

    def __init__(self) -> None:
        super().__init__()
        self.applied: list[tuple] = []

    @property
    def connected(self) -> bool:
        return True

    def list_devices(self, query: str = "match") -> None:
        pass

    def list_rules(self, label: str = "") -> None:
        pass

    def remove_rule(self, rule_id: int) -> None:
        pass

    def apply_device_policy(self, device_id: int, target: DeviceTarget, permanent: bool = False,
                            device_rule: str | None = None) -> None:
        self.applied.append((device_id, target, permanent, device_rule))


class _LockAvailable(QObject):
    """ScreensaverMonitor stand-in reporting that locking works."""

    connection_changed = pyqtSignal(bool)

    @property
    def connected(self) -> bool:
        return True


class TestCallSitesPassRawRule:
    """The device list must forward Device.raw_rule, or the client silently
    falls back to the upserting code path and the bug returns."""

    def _window_and_device(self, qtbot):
        from usbguard_gui.device_list import DeviceListWindow

        client = _RecordingClient()
        window = DeviceListWindow(client, screensaver=_LockAvailable())
        qtbot.addWidget(window)
        return window, client, Device.from_dbus(9, BLOCKED_HUB)

    def test_permanent_forwards_raw_rule(self, qtbot):
        window, client, device = self._window_and_device(qtbot)

        window._apply(device, DeviceTarget.ALLOW, permanent=True)

        assert client.applied == [(9, DeviceTarget.ALLOW, True, BLOCKED_HUB)]

    def test_temporary_forwards_none(self, qtbot):
        window, client, device = self._window_and_device(qtbot)

        window._apply(device, DeviceTarget.ALLOW, permanent=False)

        assert client.applied == [(9, DeviceTarget.ALLOW, False, None)]


class TestPermanentRulePlacement:
    """Finding A -- the rule must land where nothing above it shadows it.

    USBGuard evaluates top-down and stops at the first match, so position is
    not cosmetic: a permanent ``allow`` written below a rule that already
    matches the device is dead code, and nothing reports it.  The permanent
    path now walks the ruleset and places the rule above the first rule that
    provably matches the device.
    """

    # Matches both KVM hubs on vid:pid alone -- the classic silent shadow.
    BROAD_BLOCK = "block id 2109:2817"
    WEBCAM = ('allow id 04f2:b2ea serial "SN-CAM" name "Integrated Camera" '
              'hash "camhash=" via-port "usb1" with-interface { 0e:01:00 }')
    OPAQUE = 'allow if admin-condition("usbguard-gui cannot read this")'

    def _parent_of_append(self, policy):
        return [c[2] for c in policy.calls if c[0] == "append"][-1]

    def test_a_shadowing_rule_is_not_left_above_the_new_one(self):
        policy = _FakePolicy([(5, self.WEBCAM), (10, self.BROAD_BLOCK), (20, self.WEBCAM)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        # Placed after rule 5, i.e. directly above the shadowing rule 10.
        assert self._parent_of_append(policy) == 5

    def test_a_replacement_moves_above_the_shadow_too(self):
        """Our existing rule sits *below* something that shadows it; the
        replacement has to climb, or it stays dead where it is."""
        policy = _FakePolicy([(5, self.WEBCAM), (10, self.BROAD_BLOCK), (30, HUB_A)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, True, HUB_A))

        assert policy.kinds() == ["list", "remove", "append"]
        assert [c[1] for c in policy.calls if c[0] == "remove"] == [30]
        assert self._parent_of_append(policy) == 5

    def test_a_shadow_at_the_head_is_reported_as_unplaceable(self, caplog):
        """The daemon rejects parent_id 0 ("insert at the top") with
        `Invalid parent ID`, so when the shadow is the first rule there is
        nowhere better to go.  The decision is still written -- and said out
        loud -- rather than dropped in silence."""
        policy = _FakePolicy([(10, self.BROAD_BLOCK), (20, self.WEBCAM)])
        thread = _stub_thread(policy)

        with caplog.at_level(logging.INFO, logger="usbguard_gui.dbus_client"):
            _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        assert self._parent_of_append(policy) == _APPEND_RULE_AT_END
        assert "cannot place a rule before the first one" in caplog.text

    def test_no_shadow_appends_at_the_end(self):
        policy = _FakePolicy([(5, self.WEBCAM)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        assert self._parent_of_append(policy) == _APPEND_RULE_AT_END

    def test_undecidable_rules_are_not_treated_as_shadows(self, caplog):
        """A rule we cannot read keeps its rank.  Moving a tray decision above
        administrator-written policy on the strength of a parsing gap is the
        wrong direction to guess in."""
        policy = _FakePolicy([(9, self.OPAQUE)])
        thread = _stub_thread(policy)

        with caplog.at_level(logging.INFO, logger="usbguard_gui.dbus_client"):
            _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        assert self._parent_of_append(policy) == _APPEND_RULE_AT_END
        assert "could not be checked for shadowing" in caplog.text

    def test_the_device_s_own_rule_is_not_its_own_shadow(self, caplog):
        """The rule being replaced shares the device's identity, so it must not
        be counted as a shadow.  It sits first here, which is what makes the
        test bite: counting it would report the (false) "cannot place above
        the first rule" condition."""
        policy = _FakePolicy([(5, HUB_A), (6, self.WEBCAM)])
        thread = _stub_thread(policy)

        with caplog.at_level(logging.INFO, logger="usbguard_gui.dbus_client"):
            _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, True, HUB_A))

        assert [c[1] for c in policy.calls if c[0] == "remove"] == [5]
        assert self._parent_of_append(policy) == _APPEND_RULE_AT_END
        assert "cannot place a rule before the first one" not in caplog.text

    def test_placement_survives_an_unreadable_ruleset(self):
        """No ruleset, no shadow analysis -- but the decision still gets
        written rather than lost."""
        from dbus_fast import DBusError, ErrorType

        policy = _FakePolicy()

        async def raise_error(label):
            raise DBusError(ErrorType.FAILED, "cannot read rules")

        policy.call_list_rules = raise_error
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        assert self._parent_of_append(policy) == _APPEND_RULE_AT_END
        assert _rules_conf(policy) == [HUB_A]


class TestPermanentWriteFailure:
    """Finding C -- a permanent decision that failed must not look like it worked.

    The permanent path is two calls: authorize the live device, then write the
    durable rule.  When the second one is denied the device is left in the
    requested state with nothing persisted, so the user believes they granted
    permanence and finds out otherwise at the next boot.  The path now says
    so, and puts back anything it took away.
    """

    def _failures(self, thread):
        seen = []
        thread.permanent_write_failed.connect(lambda dev, action, reason: seen.append((dev, action, reason)))
        return seen

    def _deny(self, reason="Not authorized"):
        from dbus_fast import DBusError, ErrorType
        return DBusError(ErrorType.FAILED, reason)

    def test_a_denied_permanent_write_reports_that_only_temporary_applied(self):
        policy = _FakePolicy()
        policy.call_append_rule = AsyncMock(side_effect=self._deny())
        thread = _stub_thread(policy)
        failures = self._failures(thread)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        assert len(failures) == 1
        device_id, action, reason = failures[0]
        assert (device_id, action) == (54, "allow")
        assert "Not authorized" in reason

    def test_the_temporary_allow_is_kept_and_said_to_be_temporary(self):
        """Rolling the live allow back would fight what the user asked for; the
        honest move is to leave it standing and label it temporary."""
        policy = _FakePolicy()
        policy.call_append_rule = AsyncMock(side_effect=self._deny())
        thread = _stub_thread(policy)
        self._failures(thread)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        thread._devices_iface.call_apply_device_policy.assert_awaited_once_with(
            54, int(DeviceTarget.ALLOW), False
        )

    def test_a_failed_replacement_puts_the_removed_rule_back(self, caplog):
        """The remove landed and the append did not: the policy would be left
        with less than it had before the click."""
        policy = _FakePolicy([(7, HUB_A)])
        original_append = policy.call_append_rule
        attempts = {"n": 0}

        async def flaky_append(rule, parent_id, temporary):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise self._deny()
            return await original_append(rule, parent_id, temporary)

        policy.call_append_rule = flaky_append
        thread = _stub_thread(policy)
        self._failures(thread)

        with caplog.at_level(logging.WARNING, logger="usbguard_gui.dbus_client"):
            _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, True, HUB_A))

        assert [c[1] for c in policy.calls if c[0] == "remove"] == [7]
        assert _rules_conf(policy) == [HUB_A]
        assert "Restored permanent rule 7" in caplog.text

    def test_a_restore_that_also_fails_is_said_loudly(self, caplog):
        """Nothing left to undo -- the operator needs to know the rule is gone."""
        policy = _FakePolicy([(7, HUB_A)])
        policy.call_append_rule = AsyncMock(side_effect=self._deny())
        thread = _stub_thread(policy)
        self._failures(thread)

        with caplog.at_level(logging.ERROR, logger="usbguard_gui.dbus_client"):
            _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, True, HUB_A))

        assert "could NOT be restored" in caplog.text
        assert _rules_conf(policy) == []

    def test_a_clean_write_reports_no_failure(self):
        policy = _FakePolicy()
        thread = _stub_thread(policy)
        failures = self._failures(thread)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        assert failures == []

    def test_both_classes_expose_the_signal(self):
        """The thread's signal is only useful because the client forwards it --
        error_occurred never reached the GUI, which is how this stayed hidden."""
        from usbguard_gui.dbus_client import USBGuardClient

        assert hasattr(_DBusThread, "permanent_write_failed")
        assert hasattr(USBGuardClient, "permanent_write_failed")


class TestUntrustedRuleIsNotPersisted:
    """Finding D -- a device-derived string is not written verbatim.

    raw_rule reaches the app from the daemon, but it is built from the
    device's own descriptors and this path writes it into
    /etc/usbguard/rules.conf.  A string that is not one well-formed rule of
    known device attributes is refused, and the daemon's own upsert -- which
    never accepts a device-supplied string -- is used instead, so the user
    still gets a permanent decision rather than a silent no-op.
    """

    # The handoff's smuggling probe: a second rule and stray tokens riding
    # along behind a legitimate-looking prefix.
    CRAFTED = 'block id 2109:2817 name "hub" block with-interface { 03:01:01 }" serial "x" reject'

    def test_a_smuggled_directive_is_never_written_to_the_policy(self):
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, self.CRAFTED))

        assert "append" not in policy.kinds()

    def test_a_refused_rule_falls_back_to_the_daemon_upsert(self):
        """The decision is not dropped -- it goes through the path that
        generates the rule daemon-side instead of taking ours."""
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, self.CRAFTED))

        thread._devices_iface.call_apply_device_policy.assert_awaited_once_with(
            54, int(DeviceTarget.ALLOW), True
        )

    def test_a_refused_rule_does_not_reach_the_durable_ruleset(self):
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, self.CRAFTED))

        assert _rules_conf(policy) == []

    def test_a_rule_with_a_newline_is_refused(self):
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True,
                                     HUB_A + '\nreject with-interface { 03:00:00 }'))

        assert "append" not in policy.kinds()
        thread._devices_iface.call_apply_device_policy.assert_awaited_once_with(
            54, int(DeviceTarget.ALLOW), True
        )

    def test_a_wellformed_rule_is_still_persisted(self):
        """The check must not cost the normal path its behaviour."""
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, True, BLOCKED_HUB))

        assert [c for c in policy.calls if c[0] == "append"] == [
            ("append", HUB_A, _APPEND_RULE_AT_END, False)
        ]
        thread._devices_iface.call_apply_device_policy.assert_awaited_once_with(
            54, int(DeviceTarget.ALLOW), False
        )
