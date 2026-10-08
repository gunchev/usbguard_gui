"""Decision engine: the state and rules behind every policy reaction."""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, pyqtSignal

from usbguard_gui.dbus_client import USBGuardClient
from usbguard_gui.device import Device, DeviceTarget, Persistence
from usbguard_gui.gate import lock_gate_open
from usbguard_gui.screensaver import ScreensaverMonitor
from usbguard_gui.settings import SettingsProtocol

log = logging.getLogger(__name__)

# Semantic notification icons — the engine never names a tray enum; the app
# maps these onto QSystemTrayIcon.MessageIcon when it shows the message.
NOTIFY_INFO = "info"
NOTIFY_WARNING = "warning"

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
    notify = pyqtSignal(str, str, str, int)  # title, body, icon (NOTIFY_*), timeout
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

    def _register_unlock_cycle(self, device_ids: set[int]) -> int:
        """Record one deferred-unlock cycle and return the request id its fetch
        must carry.

        Past MAX_PENDING_UNLOCK_CYCLES the oldest cycle is dropped, so a daemon
        that stays down across many lock/unlock cycles cannot grow the map for
        the life of the process.  A dropped cycle loses only its prompt — the
        devices stay blocked by USBGuard's policy.
        """
        cycle_id = self._next_unlock_cycle_id
        self._next_unlock_cycle_id += 1
        self._pending_unlock_cycles[cycle_id] = set(device_ids)
        while len(self._pending_unlock_cycles) > MAX_PENDING_UNLOCK_CYCLES:
            oldest = next(iter(self._pending_unlock_cycles))
            dropped = self._pending_unlock_cycles.pop(oldest)
            log.warning(
                "Unlock-cycle queue full (%d) — dropping the oldest pending set (%d id(s)); "
                "those devices stay blocked",
                MAX_PENDING_UNLOCK_CYCLES,
                len(dropped),
            )
        return cycle_id

    def _retry_pending_unlock_cycles(self) -> None:
        """Re-fetch every cycle still outstanding once the daemon is back.

        A cycle keeps its id across retries, so a late answer to the failed
        attempt resolves it just as well, while an answer for a cycle that has
        already been resolved is ignored.
        """
        if not self._pending_unlock_cycles:
            return
        log.info("Reconnected — retrying %d outstanding unlock cycle(s)", len(self._pending_unlock_cycles))
        for cycle_id in list(self._pending_unlock_cycles):
            self._client.fetch_devices(cycle_id)

    def _on_correlated_devices(self, request_id: int, devices: list[Device]) -> None:
        """Resolve one unlock cycle against the snapshot fetched for it.

        Anything that is not a cycle still waiting — another caller's fetch, or a
        late answer for a cycle already resolved — is ignored.  That is the whole
        point of the correlation: the device-list window's refreshes can no
        longer consume an unlock cycle, and answers may arrive in any order.
        """
        pending_ids = self._pending_unlock_cycles.pop(request_id, None)
        if pending_ids is None:
            return

        if not devices:
            # An empty snapshot means the fetch fast-failed (daemon
            # disconnected) or hit a DBusError.  Put the cycle back: the
            # devices may still be present, and dropping the entry here is how a
            # transient disconnect silently lost the prompt.  It gets retried
            # when the daemon returns — see _retry_pending_unlock_cycles().
            self._pending_unlock_cycles[request_id] = pending_ids
            return

        pending_devices = [d for d in devices if d.number in pending_ids and not d.is_allowed()]
        if not pending_devices:
            return

        count = len(pending_devices)
        names = "\n".join(f"  - {d.name or d.id} ({d.class_description_string()})" for d in pending_devices)
        self.notify.emit(f"{count} USB device(s) connected during absence", names, NOTIFY_INFO, 10000)

        for device in pending_devices:
            self.show_dialog.emit(device)

    def _cancel_pending_device(self, device_id: int) -> None:
        """Invalidate deferred work and outstanding snapshots for one incarnation."""
        self._hid_pending_devices.discard(device_id)
        self._screensaver_pending_devices.discard(device_id)
        for cycle_id, pending_ids in list(self._pending_unlock_cycles.items()):
            pending_ids.discard(device_id)
            if not pending_ids:
                del self._pending_unlock_cycles[cycle_id]

    def _lock_for_pending_hid(self) -> None:
        """Lock the screen for a deferred HID insert, unless every triggering
        device was unplugged during the notification delay."""
        if not self._hid_pending_devices:
            log.debug("HID lock timer fired with no pending devices — skipping lock")
            return
        if not lock_gate_open(self._lock_available):
            # Locking became unavailable while the delay was running — do
            # not claim to lock.  The pending devices stay blocked (safe);
            # they are cleared on removal or the next lock.
            log.warning("HID lock timer fired but screen locking is unavailable — devices stay blocked")
            return
        self._screensaver.lock()

    def _on_screensaver_unlocked(self, active: bool) -> None:
        """Screen unlocked: collect the devices deferred while the screen was
        locked and show their summary prompts."""
        if active or not self._screensaver_pending_devices:
            return

        cycle_id = self._register_unlock_cycle(self._screensaver_pending_devices)
        self._screensaver_pending_devices.clear()
        self._client.fetch_devices(cycle_id)

    def _on_screensaver_locked(self, active: bool) -> None:
        """Screen locked: auto-allow pending HID devices so the newly-attached
        keyboard can be used to unlock."""
        if not active or not self._hid_pending_devices:
            return

        pending_ids = list(self._hid_pending_devices)
        self._hid_pending_devices = set()
        for device_number in pending_ids:
            self._client.apply_device_policy(device_number, DeviceTarget.ALLOW, persistence=Persistence.UNCHANGED)
