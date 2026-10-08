"""Single authority for whether a policy action may proceed, and if not, why.

The views keep the presentation — the QMessageBox text, the log lines — and
ask here for the decision, so one rule answers the dialog's buttons, the
device list's context menu and, as the engine grows, the tray's automatic
paths.  First pass: the connection and lock checks moved verbatim from the
views they were scattered across.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Protocol


class BlockReason(Enum):
    """Why an action may not proceed; the caller turns this into a message."""

    DAEMON_DISCONNECTED = auto()
    LOCK_UNAVAILABLE = auto()


class LockState(Protocol):
    """What the gate reads from a screensaver monitor."""

    @property
    def connected(self) -> bool: ...


def lock_gate_open(state: LockState | bool | None) -> bool:
    """Whether screen locking can guard an action.

    Accepts the three forms the state is carried in: a monitor object (the
    views), the tray's cached bool (`USBGuardTrayApp._lock_available`), or
    None — a dialog built without a monitor (unit tests, headless use) has
    no gate to refuse it, so it counts as open.
    """
    if isinstance(state, bool):
        return state
    return state is None or state.connected


def block_reason(daemon_connected: bool, lock_gate: bool) -> BlockReason | None:
    """Why a click may not proceed; None means it may.

    The daemon is checked first, in the order the views always did: a broken
    transport makes every action moot, whatever the lock says.  `lock_gate`
    is a bool — `lock_gate_open(...)` for the views, the cached field for the
    tray — so both feed the same decision.
    """
    if not daemon_connected:
        return BlockReason.DAEMON_DISCONNECTED
    if not lock_gate:
        return BlockReason.LOCK_UNAVAILABLE
    return None
