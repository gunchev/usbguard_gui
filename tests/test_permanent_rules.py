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
