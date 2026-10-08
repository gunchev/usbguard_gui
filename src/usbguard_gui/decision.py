"""Decision engine: the state and rules behind every policy reaction."""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from usbguard_gui.dbus_client import USBGuardClient
from usbguard_gui.device import DeviceTarget, Persistence
from usbguard_gui.screensaver import ScreensaverMonitor
from usbguard_gui.settings import SettingsProtocol

# Cap on queued screensaver-unlock cycles.  Entries are only consumed by a
# non-empty list_devices() result (a failed/empty snapshot deliberately does not
# consume one, so a transient daemon disconnect cannot drop a prompt), which means
# a daemon that stays down while the user keeps locking/unlocking would otherwise
# grow the queue for the life of the process.  Dropping the oldest cycle loses only
# a prompt; the devices stay blocked.
MAX_PENDING_UNLOCK_CYCLES = 32

# Cap on decisions held for devices that are off the bus.  They normally drain
# within a second or two of the next appearance, but a device the user decided
# about and then threw away would otherwise leave the entry for the life of the
# process.  Dropping the oldest loses a queued decision; nothing is applied
# that nobody asked for.
MAX_PENDING_DECISIONS = 32


class DecisionEngine(QObject):
    """Owns every piece of state a policy decision is made from.

    Phase 3 of the core refactor: storage only.  The tray app still runs the
    handlers and reaches this state through property shims on itself, so no
    call path changes yet; the effect signals below are declared for the
    handlers that move over in Phase 4, one reviewable step at a time
    against the HID lock-first contract.
    """

    show_dialog = pyqtSignal(object)  # Device to prompt for
    dialog_retarget = pyqtSignal(object)  # Device whose open dialog must follow it
    notify = pyqtSignal(str, str, object, int)  # title, body, icon, timeout
    lock_now = pyqtSignal()  # deferred HID lock is due

    def __init__(self, client: USBGuardClient, screensaver: ScreensaverMonitor, settings: SettingsProtocol,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._client = client
        self._screensaver = screensaver
        self._settings = settings

        self._last_prompted_at: dict[str, float] = {}
        # Decisions made while the device was off the bus, keyed by identity:
        # a flapping device is worth deciding about in the gap between one
        # incarnation and the next.  They stand until that device shows up
        # again, which is exactly what "block this even though it was only
        # plugged in for a second" means.
        self._pending_decisions: dict[str, tuple[DeviceTarget, Persistence]] = {}
        self._screensaver_pending_devices: set[int] = set()
        self._hid_pending_devices: set[int] = set()
        # Outstanding screensaver-unlock cycles, keyed by the request id of the
        # fetch_devices() call whose snapshot answers that cycle.  A FIFO list
        # here had to assume that every list_devices_result on the shared signal
        # belonged to an unlock cycle, and that answers arrived in call order.
        # Neither holds: the device-list window issues its own list_devices()
        # calls on that same signal, and D-Bus makes no ordering promise, so a
        # cycle could be matched against somebody else's (or a later cycle's)
        # snapshot and its prompt silently dropped.  Keyed by id, a cycle is
        # resolved only by its own snapshot, arriving in any order.
        self._pending_unlock_cycles: dict[int, set[int]] = {}
        self._next_unlock_cycle_id: int = 0
        self._permanent_allow_hashes: set[str] = set()
        # Whether screen locking is available (ScreenSaver service reachable).
        # While False, the HID lock-first flow cannot work and the UI must
        # refuse all allow/deny actions — see _on_lock_availability_changed.
        self._lock_available: bool = screensaver.connected
        self._lock_state_confirmed: bool = False
