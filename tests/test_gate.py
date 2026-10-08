"""Tests for the action-gate authority."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from usbguard_gui.device import DeviceTarget
from usbguard_gui.gate import BlockReason, block_reason, hid_allow_gated, lock_gate_open


class TestLockGateOpen:
    """`lock_gate_open` answers the lock question for every form the state takes."""

    def test_no_monitor_counts_as_open(self) -> None:
        assert lock_gate_open(None) is True

    def test_a_connected_monitor_is_open(self) -> None:
        assert lock_gate_open(SimpleNamespace(connected=True)) is True

    def test_a_disconnected_monitor_is_closed(self) -> None:
        assert lock_gate_open(SimpleNamespace(connected=False)) is False

    def test_the_engines_cached_bool_passes_through(self) -> None:
        assert lock_gate_open(True) is True
        assert lock_gate_open(False) is False


class TestHidAllowGated:
    """The narrow lock rule: HID allow, treatment on, lock down — and nothing else."""

    @pytest.mark.parametrize(
        "lock_gate,is_hid,treatment,expected",
        [
            (False, True, True, True),    # the one refused click
            (False, True, False, False),  # treatment off ⇒ nothing gated
            (False, False, True, False),  # non-HID never needed the lock
            (True, True, True, False),    # lock up ⇒ nothing gated
            (True, False, False, False),
            (False, False, False, False),
        ],
    )
    def test_table(self, lock_gate: bool, is_hid: bool, treatment: bool, expected: bool) -> None:
        assert hid_allow_gated(lock_gate, is_hid=is_hid, hid_treatment_enabled=treatment) is expected


class TestBlockReason:
    """The full decision table: connected / locked / HID / settings / target."""

    @pytest.mark.parametrize(
        "daemon,lock,target,is_hid,treatment,expected",
        [
            # The daemon is checked first, in the order the views always did.
            (False, True, DeviceTarget.ALLOW, True, True, BlockReason.DAEMON_DISCONNECTED),
            (False, False, DeviceTarget.BLOCK, True, True, BlockReason.DAEMON_DISCONNECTED),
            (False, False, DeviceTarget.ALLOW, False, False, BlockReason.DAEMON_DISCONNECTED),
            # The lock refuses exactly one click: HID allow, treatment on, lock down.
            (True, False, DeviceTarget.ALLOW, True, True, BlockReason.LOCK_UNAVAILABLE),
            # Every other combination clears — Block/Reject need no lock,
            # non-HID allows never relied on one, treatment off disarms all of it.
            (True, False, DeviceTarget.ALLOW, True, False, None),
            (True, False, DeviceTarget.ALLOW, False, True, None),
            (True, False, DeviceTarget.BLOCK, True, True, None),
            (True, False, DeviceTarget.REJECT, True, True, None),
            (True, True, DeviceTarget.ALLOW, True, True, None),
            (True, False, DeviceTarget.ALLOW, False, False, None),
        ],
    )
    def test_table(self, daemon: bool, lock: bool, target: DeviceTarget, is_hid: bool, treatment: bool,
                   expected: BlockReason | None) -> None:
        assert block_reason(daemon, lock, target=target, is_hid=is_hid,
                            hid_treatment_enabled=treatment) is expected
