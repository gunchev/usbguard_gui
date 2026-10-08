"""Tests for the action-gate authority."""

from __future__ import annotations

from types import SimpleNamespace

from usbguard_gui.gate import BlockReason, block_reason, lock_gate_open


class TestLockGateOpen:
    """`lock_gate_open` answers the lock question for every form the state takes."""

    def test_no_monitor_counts_as_open(self) -> None:
        assert lock_gate_open(None) is True

    def test_a_connected_monitor_is_open(self) -> None:
        assert lock_gate_open(SimpleNamespace(connected=True)) is True

    def test_a_disconnected_monitor_is_closed(self) -> None:
        assert lock_gate_open(SimpleNamespace(connected=False)) is False

    def test_the_trays_cached_bool_passes_through(self) -> None:
        assert lock_gate_open(True) is True
        assert lock_gate_open(False) is False


class TestBlockReason:
    """`block_reason` decides, in the order the views always checked."""

    def test_a_disconnected_daemon_blocks_first(self) -> None:
        """Even with the lock down too, the transport is the answer the user gets."""
        assert block_reason(daemon_connected=False, lock_gate=False) is BlockReason.DAEMON_DISCONNECTED

    def test_a_down_lock_blocks_an_otherwise_healthy_click(self) -> None:
        assert block_reason(daemon_connected=True, lock_gate=False) is BlockReason.LOCK_UNAVAILABLE

    def test_a_healthy_setup_blocks_nothing(self) -> None:
        assert block_reason(daemon_connected=True, lock_gate=True) is None

    def test_a_down_lock_never_masks_a_disconnected_daemon(self) -> None:
        assert block_reason(daemon_connected=False, lock_gate=True) is BlockReason.DAEMON_DISCONNECTED
