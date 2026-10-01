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
from usbguard_gui.device import Device, DeviceTarget, Persistence, rule_identity

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

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

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

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

        appended = thread._policy_iface.call_append_rule.await_args.args[0]
        assert 'parent-hash "xe96rjr8V53Jw+g7q/yi0C1czVxatehiq7r4gn2dH6s="' in appended
        assert 'via-port "3-3.1.1"' in appended

    def test_both_topologies_append_distinct_rules(self):
        """Allowing hub A then hub B must yield two rules, not one replaced twice."""
        thread = self._thread()

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, HUB_A.replace("allow ", "block ", 1)))
        _run(thread._do_apply_policy(56, DeviceTarget.ALLOW, Persistence.ALWAYS, HUB_B.replace("allow ", "block ", 1)))

        appended = [c.args[0] for c in thread._policy_iface.call_append_rule.await_args_list]
        assert appended == [HUB_A, HUB_B]
        assert len(set(appended)) == 2  # distinct: they coexist

    def test_temporary_uses_apply_device_policy_only(self):
        thread = self._thread()

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.UNCHANGED, BLOCKED_HUB))

        thread._devices_iface.call_apply_device_policy.assert_awaited_once_with(
            54, int(DeviceTarget.ALLOW), False
        )
        thread._policy_iface.call_append_rule.assert_not_awaited()

    def test_permanent_without_a_rule_falls_back_to_upsert(self):
        """No raw rule available (e.g. a stale device view) -- keep the old
        behaviour rather than silently doing nothing."""
        thread = self._thread()

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, None))

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

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))
        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

        assert _rules_conf(policy) == [HUB_A]

    def test_allow_block_reallow_cycle_leaves_one_rule_holding_the_last_decision(self):
        """The ping-pong the UI actually invites: allow, block, allow again."""
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))
        _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, Persistence.ALWAYS, HUB_A))
        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

        assert _rules_conf(policy) == [HUB_A]

    def test_repeated_permanent_decisions_do_not_grow_the_policy(self):
        """N permanent decisions on one device must not mean N rules."""
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        for _ in range(5):
            _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

        assert len(policy.rules) == 1

    def test_target_change_replaces_the_rule_instead_of_adding_one(self):
        """A changed mind updates the existing entry; it does not stack."""
        policy = _FakePolicy([(7, HUB_A)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, Persistence.ALWAYS, HUB_A))

        assert _rules_conf(policy) == [BLOCKED_HUB]

    def test_removal_precedes_append_so_a_new_block_is_never_shadowed(self):
        """USBGuard is first-match-wins, so the stale rule has to go *before*
        the new one lands.  Appending first would park a fresh ``block`` under
        an older ``allow`` and silently ignore the user."""
        policy = _FakePolicy([(7, HUB_A)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, Persistence.ALWAYS, HUB_A))

        assert policy.kinds() == ["list", "remove", "append"]
        assert [c[1] for c in policy.calls if c[0] == "remove"] == [7]

    def test_replacement_still_authorizes_the_live_device_temporarily(self):
        """Dedup must not turn the live authorization back into a permanent
        upsert — the temporary apply is what keeps the HID lock-first gate the
        only thing deciding when a device goes live."""
        policy = _FakePolicy([(7, HUB_A)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, Persistence.ALWAYS, HUB_A))

        thread._devices_iface.call_apply_device_policy.assert_awaited_once_with(
            54, int(DeviceTarget.BLOCK), False
        )

    def test_sibling_topology_is_not_a_duplicate(self):
        """The KVM's other hub differs in parent-hash, so it is a different
        insertion point and keeps its own rule — dedup must not collapse them
        back into the single-rule behaviour that caused the original bug."""
        policy = _FakePolicy([(7, HUB_B)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

        assert len(policy.rules) == 2
        assert "remove" not in policy.kinds()

    def test_unrelated_rules_are_left_untouched(self):
        """Only the matched device's rule moves; everything else keeps its
        place in the file."""
        webcam = ('allow id 04f2:b2ea serial "SN-CAM" name "Integrated Camera" '
                  'hash "camhash=" via-port "usb1" with-interface { 0e:01:00 }')
        policy = _FakePolicy([(3, webcam)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

        assert _rules_conf(policy) == [webcam, HUB_A]

    def test_rule_naming_no_device_is_never_treated_as_this_device_s(self):
        """A class-wide rule shares no identity with a specific device, so it
        is neither a duplicate nor a replacement target — hand-written policy
        stays exactly where the admin put it."""
        broad = 'allow with-interface { 03:00:00 }'
        policy = _FakePolicy([(3, broad)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

        assert _rules_conf(policy) == [broad, HUB_A]

    def test_whitespace_only_difference_is_not_a_new_rule(self):
        """Same rule, differently wrapped: nothing to write."""
        policy = _FakePolicy([(7, HUB_A.replace('name "USB2.0 Hub"', 'name  "USB2.0 Hub"'))])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

        assert policy.kinds() == ["list"]

    def test_preexisting_duplicates_are_replaced_with_one_effective_rule(self, caplog):
        """A retained duplicate allow must not shadow the new permanent block."""
        policy = _FakePolicy([(7, HUB_A), (9, HUB_A)])
        thread = _stub_thread(policy)

        with caplog.at_level(logging.INFO, logger="usbguard_gui.dbus_client"):
            _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, Persistence.ALWAYS, HUB_A))

        assert [c[1] for c in policy.calls if c[0] == "remove"] == [7, 9]
        assert _rules_conf(policy) == [BLOCKED_HUB]
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

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

        assert _rules_conf(policy) == [HUB_A]
        # An ordinary per-call failure must not flip the connection.
        assert thread._connected is True


class TestDuplicateRuleReplacement:
    """Always heals device-owned duplicates and restores removals on failure."""

    @pytest.mark.parametrize("target", [DeviceTarget.ALLOW, DeviceTarget.BLOCK])
    def test_same_or_mixed_targets_collapse_to_the_last_durable_choice(self, target):
        policy = _FakePolicy([(7, HUB_A), (8, BLOCKED_HUB), (9, HUB_A)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, target, Persistence.ALWAYS, BLOCKED_HUB))

        expected = _retarget_device_rule(BLOCKED_HUB, target)
        assert _rules_conf(policy) == [expected]
        assert [c[1] for c in policy.calls if c[0] == "remove"] == [7, 8, 9]

    def test_duplicate_replacement_preserves_sibling_and_class_policy(self):
        class_rule = "block with-interface all-of { 08:*:* }"
        policy = _FakePolicy([(7, HUB_A), (8, HUB_A), (9, HUB_B), (10, class_rule)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, Persistence.ALWAYS, BLOCKED_HUB))

        assert _rules_conf(policy) == [HUB_B, class_rule, BLOCKED_HUB]
        assert [c[1] for c in policy.calls if c[0] == "remove"] == [7, 8]

    def test_failure_on_a_later_removal_restores_the_rules_already_removed(self):
        from dbus_fast import DBusError

        policy = _FakePolicy([(7, HUB_A), (8, HUB_A)])
        remove = policy.call_remove_rule

        async def fail_second(rule_id):
            if rule_id == 8:
                raise DBusError("org.freedesktop.DBus.Error.AccessDenied", "Removal denied")
            await remove(rule_id)

        policy.call_remove_rule = fail_second
        thread = _stub_thread(policy)
        failures = []
        thread.permanent_write_failed.connect(lambda *args: failures.append(args))

        _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, Persistence.ALWAYS, BLOCKED_HUB))

        assert _rules_conf(policy) == [HUB_A, HUB_A]
        assert failures == [(54, "block", "Removal denied")]
        assert thread.is_connected

    def test_failed_append_restores_all_removed_duplicate_rules(self):
        from dbus_fast import DBusError

        stale = HUB_A.replace('name "USB2.0 Hub"', 'name "Old Hub"')
        policy = _FakePolicy([(7, HUB_A), (8, stale)])
        append = policy.call_append_rule

        async def fail_replacement(rule, parent_id, temporary):
            if rule.startswith("block "):
                raise DBusError("org.freedesktop.DBus.Error.AccessDenied", "Append denied")
            return await append(rule, parent_id, temporary)

        policy.call_append_rule = fail_replacement
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, Persistence.ALWAYS, BLOCKED_HUB))

        assert _rules_conf(policy) == [HUB_A, stale]


class TestOnceClearsThePermanentRule:
    """`Once` deletes -- it is not merely a refusal to write.

    The invariant: a device's permanent rule always reflects the last *durable*
    decision, or there is none.  A temporary decision says nothing durable
    stands behind this device, so the standing rule has to go.  Merely declining
    to write would leave it in place to re-assert at the next boot -- the exact
    divergence between what the user clicked and what survives reboot that this
    change exists to remove.
    """

    def test_allow_once_removes_the_device_s_existing_permanent_allow(self):
        policy = _FakePolicy([(7, HUB_A)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ONCE, BLOCKED_HUB))

        assert _rules_conf(policy) == []
        assert ("remove", 7) in policy.calls
        assert "append" not in policy.kinds()
        thread._devices_iface.call_apply_device_policy.assert_called_once_with(
            54, int(DeviceTarget.ALLOW), False)

    def test_block_once_removes_the_same_rule_allow_once_would(self):
        """The identity carries no target verb, so the clear is verb-agnostic.

        Proved rather than assumed: `_RULE_IDENTITY_ATTRS` is device/topology
        only, so a `block` clears exactly what an `allow` clears.
        """
        policy = _FakePolicy([(7, HUB_A)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, Persistence.ONCE, BLOCKED_HUB))

        assert _rules_conf(policy) == []
        assert ("remove", 7) in policy.calls
        assert "append" not in policy.kinds()

    def test_once_with_no_standing_rule_writes_nothing(self):
        """A temporary decision about an unknown device leaves the policy alone."""
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ONCE, BLOCKED_HUB))

        assert _rules_conf(policy) == []
        # 'list' is the ruleset read; what matters is that nothing mutated it.
        assert [k for k in policy.kinds() if k != "list"] == []
        thread._devices_iface.call_apply_device_policy.assert_called_once_with(
            54, int(DeviceTarget.ALLOW), False)

    def test_once_never_touches_a_rule_that_names_no_device(self):
        """Hand-written class policy is not ours to remove.

        `reject with-interface all-of { ... }` carries no device identity, so
        `_rule_identity` returns None and it can never be picked up as "this
        device's rule".  This is the boundary that keeps a tray click from
        dismantling deliberate admin policy.
        """
        class_rule = 'reject with-interface all-of { 08:*:* 03:00:* }'
        policy = _FakePolicy([(3, class_rule), (7, HUB_A)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, Persistence.ONCE, BLOCKED_HUB))

        assert _rules_conf(policy) == [class_rule]


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

    def apply_device_policy(self, device_id: int, target: DeviceTarget,
                            persistence: Persistence = Persistence.UNCHANGED,
                            device_rule: str | None = None) -> None:
        self.applied.append((device_id, target, persistence, device_rule))


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

    def test_always_forwards_raw_rule(self, qtbot):
        window, client, device = self._window_and_device(qtbot)

        window._apply(device, DeviceTarget.ALLOW, persistence=Persistence.ALWAYS)

        assert client.applied == [(9, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB)]

    def test_once_forwards_raw_rule_so_the_clear_has_an_identity(self, qtbot):
        """`Once` deletes by device identity, and that identity is derived from
        the rule string.  Withhold it and the clear is a silent no-op."""
        window, client, device = self._window_and_device(qtbot)

        window._apply(device, DeviceTarget.BLOCK, persistence=Persistence.ONCE)

        assert client.applied == [(9, DeviceTarget.BLOCK, Persistence.ONCE, BLOCKED_HUB)]

    def test_unchanged_forwards_none(self, qtbot):
        window, client, device = self._window_and_device(qtbot)

        window._apply(device, DeviceTarget.ALLOW, persistence=Persistence.UNCHANGED)

        assert client.applied == [(9, DeviceTarget.ALLOW, Persistence.UNCHANGED, None)]


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

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

        # Placed after rule 5, i.e. directly above the shadowing rule 10.
        assert self._parent_of_append(policy) == 5

    def test_a_replacement_moves_above_the_shadow_too(self):
        """Our existing rule sits *below* something that shadows it; the
        replacement has to climb, or it stays dead where it is."""
        policy = _FakePolicy([(5, self.WEBCAM), (10, self.BROAD_BLOCK), (30, HUB_A)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, Persistence.ALWAYS, HUB_A))

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
            _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

        assert self._parent_of_append(policy) == _APPEND_RULE_AT_END
        assert "cannot place a rule before the first one" in caplog.text

    def test_no_shadow_appends_at_the_end(self):
        policy = _FakePolicy([(5, self.WEBCAM)])
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

        assert self._parent_of_append(policy) == _APPEND_RULE_AT_END

    def test_undecidable_rules_are_not_treated_as_shadows(self, caplog):
        """A rule we cannot read keeps its rank.  Moving a tray decision above
        administrator-written policy on the strength of a parsing gap is the
        wrong direction to guess in."""
        policy = _FakePolicy([(9, self.OPAQUE)])
        thread = _stub_thread(policy)

        with caplog.at_level(logging.INFO, logger="usbguard_gui.dbus_client"):
            _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

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
            _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, Persistence.ALWAYS, HUB_A))

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

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

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

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

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

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

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
            _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, Persistence.ALWAYS, HUB_A))

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
            _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, Persistence.ALWAYS, HUB_A))

        assert "could NOT be restored" in caplog.text
        assert _rules_conf(policy) == []

    def test_a_clean_write_reports_no_failure(self):
        policy = _FakePolicy()
        thread = _stub_thread(policy)
        failures = self._failures(thread)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

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

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, self.CRAFTED))

        assert "append" not in policy.kinds()

    def test_a_refused_rule_falls_back_to_the_daemon_upsert(self):
        """The decision is not dropped -- it goes through the path that
        generates the rule daemon-side instead of taking ours."""
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, self.CRAFTED))

        thread._devices_iface.call_apply_device_policy.assert_awaited_once_with(
            54, int(DeviceTarget.ALLOW), True
        )

    def test_a_refused_rule_does_not_reach_the_durable_ruleset(self):
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, self.CRAFTED))

        assert _rules_conf(policy) == []

    def test_a_rule_with_a_newline_is_refused(self):
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS,
                                     HUB_A + '\nreject with-interface { 03:00:00 }'))

        assert "append" not in policy.kinds()
        thread._devices_iface.call_apply_device_policy.assert_awaited_once_with(
            54, int(DeviceTarget.ALLOW), True
        )

    def test_a_wellformed_rule_is_still_persisted(self):
        """The check must not cost the normal path its behaviour."""
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ALWAYS, BLOCKED_HUB))

        assert [c for c in policy.calls if c[0] == "append"] == [
            ("append", HUB_A, _APPEND_RULE_AT_END, False)
        ]
        thread._devices_iface.call_apply_device_policy.assert_awaited_once_with(
            54, int(DeviceTarget.ALLOW), False
        )


class TestGhostGuard:
    """Slice 9 -- a persisted `reject` is a ghost, and the code refuses to write one.

    A permanent `reject` rule fires `remove=1` on every match, so the device is
    gone on sight: never in the device list, nothing to click, no UI path back
    until the persistent-rules editor exists.  Nothing in the action set
    produces that target -- but "nothing produces it" is exactly the kind of
    claim that rots the day someone wires a new button, so it is asserted.
    """

    def test_block_always_persists_a_block_verb(self):
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, Persistence.ALWAYS, BLOCKED_HUB))

        assert _rules_conf(policy) == [BLOCKED_HUB]

    def test_persisting_a_reject_is_refused(self):
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        with pytest.raises(ValueError, match="reject"):
            _run(thread._do_apply_policy(54, DeviceTarget.REJECT, Persistence.ALWAYS, BLOCKED_HUB))

        assert _rules_conf(policy) == []

    def test_no_action_set_target_ever_writes_a_reject_rule(self):
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        for target in (DeviceTarget.ALLOW, DeviceTarget.BLOCK):
            _run(thread._do_apply_policy(54, target, Persistence.ALWAYS, BLOCKED_HUB))

        assert not any(r.lstrip().startswith("reject") for r in _rules_conf(policy))


class TestFailedClearIsReported:
    """Slice 10 -- a `Once` whose removal failed must say so.

    The user clicked a deliberate action.  If the daemon refused the removal
    -- polkit, a hiccup -- the device keeps its standing rule and the dialog
    has already closed.  Silence here is the same defect
    `permanent_write_failed` exists for, one layer down.
    """

    def test_a_refused_removal_emits_a_clear_failure(self):
        from dbus_fast import DBusError, ErrorType

        policy = _FakePolicy([(7, HUB_A)])
        thread = _stub_thread(policy)

        async def deny(_rule_id):
            raise DBusError(ErrorType.FAILED, "Not authorized to remove rules")

        policy.call_remove_rule = deny
        received = []
        thread.permanent_clear_failed.connect(lambda *a: received.append(a))

        # The outer handler owns the DBusError (and the reconnect decision), so
        # what the caller can rely on is the signal, not a raise.
        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ONCE, BLOCKED_HUB))

        assert received == [(54, "allow", "Not authorized to remove rules", False)], \
            "the first removal failed, so rules.conf is untouched"
        # The clear comes first, so nothing was applied live either.
        thread._devices_iface.call_apply_device_policy.assert_not_called()


class TestFailedLiveOnceAction:
    """A failed live action is announced after a successful permanent-rule clear."""

    @pytest.mark.parametrize("target", [DeviceTarget.ALLOW, DeviceTarget.BLOCK])
    @pytest.mark.parametrize("has_own_rule", [False, True])
    @pytest.mark.parametrize("error_name, stays_connected", [
        ("org.freedesktop.DBus.Error.AccessDenied", True),
        ("org.freedesktop.DBus.Error.Failed", True),
        ("org.freedesktop.DBus.Error.NoReply", False),
    ])
    def test_live_failure_reports_what_was_removed_and_preserves_connection_classification(
            self, target, has_own_rule, error_name, stays_connected):
        from dbus_fast import DBusError

        rules = [(8, HUB_B)]
        if has_own_rule:
            rules.insert(0, (7, HUB_A))
        policy = _FakePolicy(rules)
        thread = _stub_thread(policy)
        failures = []
        clear_failures = []
        thread.temporary_apply_failed.connect(lambda *args: failures.append(args))
        thread.permanent_clear_failed.connect(lambda *args: clear_failures.append(args))
        thread._devices_iface.call_apply_device_policy.side_effect = DBusError(error_name, "Live action failed")

        _run(thread._do_apply_policy(54, target, Persistence.ONCE, BLOCKED_HUB))

        assert policy.rules == [(8, HUB_B)], "The clear succeeded and must not remove the sibling's rule"
        assert clear_failures == [], "This is a live failure, not a clear failure"
        assert len(failures) == 1
        device_id, action, reason, policy_changed = failures[0]
        assert (device_id, action, policy_changed) == (54, target.name.lower(), has_own_rule)
        assert "Live action failed" in reason
        assert (HUB_A in reason) is has_own_rule
        assert HUB_B not in reason
        assert thread.is_connected is stays_connected

    def test_successful_once_does_not_emit_a_live_failure(self):
        policy = _FakePolicy([(7, HUB_A)])
        thread = _stub_thread(policy)
        failures = []
        thread.temporary_apply_failed.connect(lambda *args: failures.append(args))

        _run(thread._do_apply_policy(54, DeviceTarget.BLOCK, Persistence.ONCE, BLOCKED_HUB))

        assert policy.rules == []
        assert failures == []


class TestOnceNeverRemovesABroaderRule:
    """Option A: a `Once` decision clears the device's own rule and nothing else.

    A rule that merely *covers* the device -- `allow id 2109:2817`, or a class
    rule -- is usually hand-written admin policy, and a tray click has no
    business erasing it.  But the user clicked a temporary action, so the app
    has to say out loud that a permanent rule is still in force; otherwise
    "Allow Once" reads as if it took effect.
    """

    def test_a_broader_rule_survives_and_is_reported(self):
        policy = _FakePolicy([(1, 'allow id 2109:2817')])
        thread = _stub_thread(policy)
        received = []
        thread.permanent_rule_remains.connect(lambda *a: received.append(a))

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ONCE, BLOCKED_HUB))

        assert received == [(54, "allow", "allow id 2109:2817")]
        assert "allow id 2109:2817" in _rules_conf(policy), "broader rule must NOT be removed"

    def test_a_class_rule_covering_the_device_is_reported_not_removed(self):
        policy = _FakePolicy([(1, 'allow with-interface { 09:00:01 09:00:02 }')])
        thread = _stub_thread(policy)
        received = []
        thread.permanent_rule_remains.connect(lambda *a: received.append(a))

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ONCE, BLOCKED_HUB))

        assert received == [(54, "allow", "allow with-interface { 09:00:01 09:00:02 }")]
        assert 'allow with-interface { 09:00:01 09:00:02 }' in _rules_conf(policy)

    def test_a_serial_only_rule_is_reported_as_broader(self):
        """A serial pins the unit but not the insertion point, and carries no
        identity for `Once` to match on -- so it is broader in the sense that
        matters: this app cannot clear it."""
        policy = _FakePolicy([(1, 'allow serial "000000000"')])
        thread = _stub_thread(policy)
        received = []
        thread.permanent_rule_remains.connect(lambda *a: received.append(a))

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ONCE, BLOCKED_HUB))

        assert received == [(54, "allow", 'allow serial "000000000"')]
        assert 'allow serial "000000000"' in _rules_conf(policy)

    def test_a_device_exact_rule_is_cleared_and_nothing_is_reported(self):
        """The clean case: the only rule is the device's own, so `Once` finishes
        and there is no broader rule to warn about."""
        policy = _FakePolicy([(1, HUB_A)])
        thread = _stub_thread(policy)
        received = []
        thread.permanent_rule_remains.connect(lambda *a: received.append(a))

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ONCE, HUB_A))

        assert received == []
        assert _rules_conf(policy) == []

    def test_no_rules_means_no_warning(self):
        policy = _FakePolicy()
        thread = _stub_thread(policy)
        received = []
        thread.permanent_rule_remains.connect(lambda *a: received.append(a))

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ONCE, HUB_A))

        assert received == []

    def test_a_rule_that_does_not_cover_the_device_is_not_reported(self):
        """An unrelated device's permanent rule is not this device's business."""
        policy = _FakePolicy([(1, HUB_B.replace("3-3.1.1.4", "9-9.9.9"))])
        thread = _stub_thread(policy)
        received = []
        thread.permanent_rule_remains.connect(lambda *a: received.append(a))

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ONCE, HUB_A))

        assert received == []

    def test_the_reported_rule_text_is_the_real_rule(self):
        """The warning has to name the rule the user must go edit -- not a
        paraphrase they cannot find in rules.conf."""
        rule = 'allow id 2109:2817 serial "000000000"'
        policy = _FakePolicy([(7, rule)])
        thread = _stub_thread(policy)
        received = []
        thread.permanent_rule_remains.connect(lambda *a: received.append(a))

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ONCE, BLOCKED_HUB))

        assert received[0][2] == rule


class TestPersistWithoutADevice:
    """`Always` needs no live device -- a permanent rule is inert data.

    appendRule takes a rule string and nothing else.  Only the live
    authorize/deauthorize call needs a device, so a durable decision about a
    device that has already left the bus can land immediately rather than
    waiting for it to come back.
    """

    def test_the_rule_lands_without_touching_the_devices_interface(self):
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        _run(thread._do_persist_only(54, DeviceTarget.ALLOW, BLOCKED_HUB))

        assert _rules_conf(policy)[0].startswith("allow ")
        thread._devices_iface.call_apply_device_policy.assert_not_called()

    def test_a_permanent_block_lands_the_same_way(self):
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        _run(thread._do_persist_only(54, DeviceTarget.BLOCK, HUB_A))

        assert _rules_conf(policy)[0].startswith("block ")
        thread._devices_iface.call_apply_device_policy.assert_not_called()

    def test_the_ghost_guard_still_refuses_a_permanent_reject(self):
        policy = _FakePolicy()
        thread = _stub_thread(policy)

        with pytest.raises(ValueError):
            _run(thread._do_persist_only(54, DeviceTarget.REJECT, BLOCKED_HUB))

        assert _rules_conf(policy) == []

    def test_an_unpersistable_rule_is_reported_not_falled_back(self):
        """The usual fallback is the daemon's own upsert, and that needs a live
        device we do not have -- so it must be reported, not attempted."""
        policy = _FakePolicy()
        thread = _stub_thread(policy)
        received = []
        thread.permanent_write_failed.connect(lambda *a: received.append(a))

        _run(thread._do_persist_only(54, DeviceTarget.ALLOW, 'allow with-bogus-thing "x"'))

        assert received, "the user must be told the durable half did not land"
        assert _rules_conf(policy) == []
        thread._devices_iface.call_apply_device_policy.assert_not_called()


class TestAPartialClearSaysSo:
    """A clear that removed some rules and then failed is not "nothing happened".

    `_clear_device_rule` removes every rule sharing the device's identity, and a
    bloated policy really does carry more than one (`_persist_device_rule` warns
    about exactly that state and leaves it alone).  The loop had no per-removal
    guard, so a failure partway through emitted the same signal as a failure on
    the first rule -- and the tray said "the existing permanent rule could not be
    removed", while a rule had in fact already been deleted from rules.conf.
    """

    @staticmethod
    def _flaky_policy(rules, fail_after: int):
        from dbus_fast import DBusError, ErrorType

        policy = _FakePolicy(rules)
        real_remove = policy.call_remove_rule
        attempts: list[int] = []

        async def flaky(rule_id):
            attempts.append(rule_id)
            if len(attempts) > fail_after:
                raise DBusError(ErrorType.FAILED, "transient failure")
            return await real_remove(rule_id)

        policy.call_remove_rule = flaky
        return policy, attempts

    def test_a_failure_partway_through_is_reported_as_partial(self):
        policy, attempts = self._flaky_policy([(7, HUB_A), (8, HUB_A)], fail_after=1)
        thread = _stub_thread(policy)
        received = []
        thread.permanent_clear_failed.connect(lambda *a: received.append(a))

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ONCE, BLOCKED_HUB))

        assert len(attempts) == 2, "the failure must not be silently swallowed mid-loop"
        assert len(received) == 1
        device_id, action, reason, partial = received[0]
        assert (device_id, action) == (54, "allow")
        assert partial is True, "policy was modified -- the user must not be told nothing happened"
        assert "7" in reason, "the reason must name the rule that was already removed"

    def test_a_failure_on_the_first_rule_is_not_partial(self):
        policy, _attempts = self._flaky_policy([(7, HUB_A), (8, HUB_A)], fail_after=0)
        thread = _stub_thread(policy)
        received = []
        thread.permanent_clear_failed.connect(lambda *a: received.append(a))

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ONCE, BLOCKED_HUB))

        assert received == [(54, "allow", "transient failure", False)], "nothing was removed"

    def test_a_partial_clear_still_withholds_the_live_change(self):
        """Clear-first is fail-closed: the device keeps USBGuard's implicit block
        rather than running under a policy half the user asked for."""
        policy, _attempts = self._flaky_policy([(7, HUB_A), (8, HUB_A)], fail_after=1)
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ONCE, BLOCKED_HUB))

        thread._devices_iface.call_apply_device_policy.assert_not_called()


class TestAnUnreadableRulesetIsStillReported:
    """F1 -- the clear reads the ruleset *before* it removes anything, and a
    failure there must be announced just like a failure inside the loop.

    ``_clear_device_rule`` opens with ``listRules``.  Narrowing the ``Once``
    handler to ``except _PartialClear`` caught only the removal loop and let
    everything else escape unreported: the click did nothing, the live change
    was withheld, and the tray said nothing at all.  That is the exact silence
    ``permanent_clear_failed`` exists to kill, and it is not theoretical --
    polkit gates ``listRules`` and ``removeRule`` separately, so a policy that
    permits one and refuses the other lands here.
    """

    def test_a_refused_ruleset_read_emits_a_clear_failure(self):
        from dbus_fast import DBusError, ErrorType

        policy = _FakePolicy([(7, HUB_A)])

        async def deny_list(_label):
            raise DBusError(ErrorType.FAILED, "Not authorized to list rules")

        policy.call_list_rules = deny_list
        thread = _stub_thread(policy)
        received = []
        thread.permanent_clear_failed.connect(lambda *a: received.append(a))

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ONCE, BLOCKED_HUB))

        assert received == [(54, "allow", "Not authorized to list rules", False)], \
            "nothing was removed, so it is a total failure -- but it must still be announced"

    def test_a_refused_ruleset_read_still_withholds_the_live_change(self):
        """Fail-closed is unchanged: no live apply behind an unreadable policy."""
        from dbus_fast import DBusError, ErrorType

        policy = _FakePolicy([(7, HUB_A)])

        async def deny_list(_label):
            raise DBusError(ErrorType.FAILED, "Not authorized to list rules")

        policy.call_list_rules = deny_list
        thread = _stub_thread(policy)

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ONCE, BLOCKED_HUB))

        thread._devices_iface.call_apply_device_policy.assert_not_called()
        assert policy.rules == [(7, HUB_A)], "the stored policy must not have moved"


class TestAPartialClearNamesTheRuleText:
    """F2 -- the user is sent to rules.conf, which carries no rule ids.

    Naming the ids that were already removed is useless against a file of rule
    strings; the ids are good for the log, the text is what the user can find.
    """

    def test_the_reason_carries_the_removed_rule_text(self):
        policy, _attempts = TestAPartialClearSaysSo._flaky_policy([(7, HUB_A), (8, HUB_A)], fail_after=1)
        thread = _stub_thread(policy)
        received = []
        thread.permanent_clear_failed.connect(lambda *a: received.append(a))

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ONCE, BLOCKED_HUB))

        reason = received[0][2]
        assert "allow id 2109:2817" in reason, "the text must be findable in rules.conf"

    def test_the_rule_that_was_not_removed_is_not_named(self):
        """Only what actually went is listed, or the file is misdescribed.

        The surviving rule shares the removed one's identity -- same id, serial,
        hash, parent-hash and port -- and differs only in `name`, so the two are
        indistinguishable to the clear and must be distinguishable in the
        message.
        """
        stale = HUB_A.replace('name "USB2.0 Hub"', 'name "USB2.0 Hub (stale)"')
        assert rule_identity(stale) == rule_identity(HUB_A), "same device, same topology"
        policy, _attempts = TestAPartialClearSaysSo._flaky_policy([(7, HUB_A), (8, stale)], fail_after=1)
        thread = _stub_thread(policy)
        received = []
        thread.permanent_clear_failed.connect(lambda *a: received.append(a))

        _run(thread._do_apply_policy(54, DeviceTarget.ALLOW, Persistence.ONCE, BLOCKED_HUB))

        reason = received[0][2]
        assert 'name "USB2.0 Hub"' in reason, "rule 7 went, so its text must be named"
        assert "(stale)" not in reason, "rule 8 is still in the file and must not be named"
