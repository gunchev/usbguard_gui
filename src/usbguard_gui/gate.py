"""Single authority for whether a policy action may proceed, and if not, why.

The views keep the presentation — the QMessageBox text, the log lines — and
ask here for the decision, so one rule answers the dialog's buttons, the
device list's context menu and the engine's queued-decision drain.

The lock half of the gate is narrow on purpose: when screen locking is
unavailable, only *allowing* a HID device while special HID treatment is
enabled is refused — that is the click the lock-first contract exists to
police.  Block and Reject need no lock (they make the machine safer, not
less safe), non-HID allows never relied on one, and with the HID treatment
disabled nothing is lock-gated at all.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Protocol

from usbguard_gui.device import DeviceTarget


class BlockReason(Enum):
    """Why an action may not proceed; the caller turns this into a message."""

    DAEMON_DISCONNECTED = auto()
    #: Only ever raised for a lock-gated HID allow (see `hid_allow_gated`).
    LOCK_UNAVAILABLE = auto()


class LockState(Protocol):
    """What the gate reads from a screensaver monitor."""

    @property
    def connected(self) -> bool: ...


def lock_gate_open(state: LockState | bool | None) -> bool:
    """Whether screen locking can guard an action.

    Accepts the three forms the state is carried in: a monitor object (the
    views), the engine's cached bool (`DecisionEngine._lock_available`), or
    None — a dialog built without a monitor (unit tests, headless use) has
    no gate to refuse it, so it counts as open.
    """
    if isinstance(state, bool):
        return state
    return state is None or state.connected


def hid_allow_gated(lock_gate: bool, *, is_hid: bool, hid_treatment_enabled: bool) -> bool:
    """Is an ALLOW on this device refused because the screen cannot be locked?

    True only for the one combination the lock-first contract polices: a HID
    device, special HID treatment on, and no working screen lock.  Block and
    Reject always work, non-HID allows never needed the lock, and treatment
    off means the flow is gone and nothing is gated.
    """
    return not lock_gate and is_hid and hid_treatment_enabled


def block_reason(daemon_connected: bool, lock_gate: bool, *, target: DeviceTarget, is_hid: bool,
                 hid_treatment_enabled: bool) -> BlockReason | None:
    """Why a click may not proceed; None means it may.

    The daemon is checked first, in the order the views always did: a broken
    transport makes every action moot, whatever the lock says.  The lock half
    consults `hid_allow_gated` — fed `lock_gate_open(...)` by the views or
    the engine's cached field — so every caller answers to the same rule.
    """
    if not daemon_connected:
        return BlockReason.DAEMON_DISCONNECTED
    if target is DeviceTarget.ALLOW and hid_allow_gated(lock_gate, is_hid=is_hid,
                                                        hid_treatment_enabled=hid_treatment_enabled):
        return BlockReason.LOCK_UNAVAILABLE
    return None
