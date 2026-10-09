"""Decision engine: the state and rules behind every policy reaction."""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, pyqtSignal

from usbguard_gui.dbus_client import USBGuardClient
from usbguard_gui.device import Device, DeviceTarget, Persistence, enum_name, parse_device_rule
from usbguard_gui.gate import lock_gate_open
from usbguard_gui.screensaver import ScreensaverMonitor
from usbguard_gui.settings import SettingsProtocol
from usbguard_gui.ui_strings import HANDBACK_NOTICE_TITLE, HID_ATTACHED_NOTICE_TITLE, LOCK_AVAILABLE_NOTICE_TITLE, \
    LOCK_UNAVAILABLE_NOTICE_TITLE

log = logging.getLogger(__name__)

# Semantic notification icons — the engine never names a tray enum; the app
# maps these onto QSystemTrayIcon.MessageIcon when it shows the message.
NOTIFY_INFO = "info"
NOTIFY_WARNING = "warning"

# Cap on queued screensaver-unlock cycles.  Entries are only consumed by a real
# fetch_devices() answer (a failed snapshot, emitted as None, deliberately does
# not consume one, so a transient daemon disconnect cannot drop a prompt), which means
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


def dialog_identity(device: Device) -> str:
    """A device/topology key that survives re-enumeration on the same port.

    Identical hubs can share a hash and even a serial. Include the parent
    and port so one live device cannot inherit another's dialog or queued
    choice. Tuple repr also keeps descriptor delimiters unambiguous.
    """
    return repr((device.id, device.serial, device.hash, device.parent_hash, device.via_port))


class DecisionEngine(QObject):
    """Owns every piece of state a policy decision is made from.

    The effect signals below are the engine's only UI surface — dialogs,
    tray notices and the deferred lock are requests, never actions it
    performs itself.  The HID lock-first contract is specified in
    `README.md`; this class and `gate.py` are where it is enforced, and the
    app reads the fields it still needs directly through `_engine`.
    """

    show_dialog = pyqtSignal(object)  # Device to prompt for
    dialog_retarget = pyqtSignal(object)  # Device whose open dialog must follow it
    notify = pyqtSignal(str, str, str, int)  # title, body, icon (NOTIFY_*), timeout
    schedule_lock = pyqtSignal()  # the deferred HID lock may be scheduled

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
        # While False, a lock-gated HID allow cannot proceed (gate.hid_allow_gated)
        # and the user is noticed — see _on_lock_availability_changed.  Block,
        # Reject and non-HID allows are never gated on it.
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

    def _on_correlated_devices(self, request_id: int, devices: list[Device] | None) -> None:
        """Resolve one unlock cycle against the snapshot fetched for it.

        Anything that is not a cycle still waiting — another caller's fetch, or a
        late answer for a cycle already resolved — is ignored.  That is the whole
        point of the correlation: the device-list window's refreshes can no
        longer consume an unlock cycle, and answers may arrive in any order.
        """
        pending_ids = self._pending_unlock_cycles.pop(request_id, None)
        if pending_ids is None:
            return

        if devices is None:
            # None means the fetch fast-failed (daemon disconnected) or hit a
            # DBusError.  Put the cycle back: the devices may still be present,
            # and dropping the entry here is how a transient disconnect
            # silently lost the prompt.  It gets retried when the daemon
            # returns — see _retry_pending_unlock_cycles().  An empty list is
            # the daemon's real answer (nothing on the bus) and resolves the
            # cycle.
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

    def _on_list_rules_result(self, rules: list[tuple[int, str]]) -> None:
        """Rebuild the permanent-allow cache from a fresh ruleset snapshot."""
        self._permanent_allow_hashes.clear()
        for _, rule_str in rules:
            parsed = parse_device_rule(rule_str)
            if parsed["rule"] == "allow" and parsed["hash"]:
                self._permanent_allow_hashes.add(str(parsed["hash"]))

    def _on_list_devices_result(self, devices: list[Device] | None) -> None:
        # Opportunistic HID safety net: a fresh snapshot taken while the screen
        # is actually locked lets any pending HID device in — that is the moment
        # a newly-attached keyboard is safe to activate (unlocking requires a
        # password).  The primary path is _on_screensaver_locked(); this only
        # matters if that signal was missed.  Allowing them while the screen is
        # still unlocked would hand a just-plugged keyboard keystrokes on an
        # unlocked session, so the active check stays.  Deferred-unlock cycles
        # are NOT resolved here — they need the snapshot fetched specifically
        # for them, see _on_correlated_devices.
        if devices is None:
            # A failed query is not an empty bus: keep the pending set and let
            # the next snapshot take the safety net.
            log.debug("list_devices failed transiently — HID safety net unchanged")
            return
        if self._hid_pending_devices and self._screensaver.active:
            pending_ids = self._hid_pending_devices
            self._hid_pending_devices = set()
            for device_number in pending_ids:
                if any(d.number == device_number for d in devices):
                    self._client.apply_device_policy(device_number, DeviceTarget.ALLOW,
                                                     persistence=Persistence.UNCHANGED)

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

    def _on_lock_availability_changed(self, available: bool) -> None:
        """Track lock availability; notice the user on the first confirmed
        report of a problem or on an actual transition.

        With special HID treatment disabled nothing is lock-gated, so the
        changes are log-only then — a tray notice about actions being
        unavailable would describe a gate that is not armed.
        """
        # The monitor reports its state repeatedly (initial report plus
        # every failed retry), so notify only on the first confirmed report
        # or an actual transition.
        changed = available != self._lock_available
        first = not self._lock_state_confirmed
        self._lock_available = available
        self._lock_state_confirmed = True
        if self._settings.disable_hid_treatment():
            if changed or first:
                log.info("Screen locking is %s (special HID treatment disabled — nothing is gated)",
                         "available" if available else "unavailable")
            return
        if not available and (changed or first):
            self.notify.emit(
                LOCK_UNAVAILABLE_NOTICE_TITLE,
                "Screen locking is unavailable, so HID devices cannot be allowed. "
                "Block and Reject still work. Devices remain blocked by USBGuard's policy.",
                NOTIFY_WARNING,
                10000,
            )
        elif available and changed and not first:
            self.notify.emit(
                LOCK_AVAILABLE_NOTICE_TITLE,
                "Screen locking is available again — HID devices can be allowed.",
                NOTIFY_INFO,
                5000,
            )

    def _hid_lock_flow_applies(self, device: Device) -> bool:
        """Would this device take the auto-allow-then-lock path?

        `has_hid_interface()` catches composite devices (HID + MSC) too — any
        HID interface can send keystrokes.  The flow is off when the user
        disabled it, and skipped when locking is inhibited or unavailable,
        because a deferred lock that never fires would auto-allow a keyboard
        with no password prompt to gate it.
        """
        return (device.has_hid_interface()
                and not self._settings.disable_hid_treatment()
                and not self._screensaver.inhibited
                and lock_gate_open(self._lock_available))

    def _apply_pending_decision(self, device: Device) -> bool:
        """Replay a held decision; return True if it consumes this insertion.

        Called before every other reaction to an insertion, because none of them
        apply to a device the user has already decided about.  Draining only
        from the prompt path meant the returns that never reach a prompt --
        the device came back already allowed, or it is a HID device heading for
        the lock-first flow -- silently dropped the decision and did the default
        instead.  For a queued `Block` on a keyboard that default was an
        auto-allow: the exact opposite of the click.

        The one thing a queued decision may not do is authorize a HID device
        outside the lock.  That contract is about proving a person is at the
        machine *now*, and a click made minutes ago while the device was off the
        bus does not prove it, so a queued `Allow` on such a device is handed
        back: the lock-first flow authorizes it behind the password prompt, or
        -- when the device returns already allowed -- there was never a live
        change left to make.  Which of the two happens is decided after this
        method returns, so the notice speaks of neither as a promise.

        What the handback does not carry is the other half of `Once`: the
        standing permanent rule stays.  It is not deferred to the lock either --
        dropping a permanent `block` widens what the *next* insertion is
        allowed to do, which is the same stale-click problem one layer down,
        and the lock-first flow authorizes with `UNCHANGED` so nothing else
        makes the change.  Rather than lose it silently, the user is told the
        rule survived and can decide again with the device in hand.
        """
        identity = dialog_identity(device)
        pending = self._pending_decisions.get(identity)
        if pending is None:
            return False
        target, persistence = pending

        # The HID contract does not disappear when the automatic lock flow
        # cannot run. Inhibited/unavailable locking requires a fresh decision,
        # never an unattended allow from a click made while the device was away.
        if target is DeviceTarget.ALLOW and device.has_hid_interface() and not self._settings.disable_hid_treatment():
            del self._pending_decisions[identity]
            self._last_prompted_at.pop(identity, None)
            log.info("Device %s is back as id %d with a queued ALLOW, but it is a HID device -- "
                     "refusing the held authorize and the %s clear is not made",
                     identity, device.number, persistence.name)
            self.notify.emit(
                HANDBACK_NOTICE_TITLE,
                f"Device {device.number}'s held Allow cleared no permanent rule: a device with a "
                f"keyboard interface is authorized behind the lock screen, never from a click made "
                f"while it was away.\n"
                f"Decide again with the device connected if you meant to change it.",
                NOTIFY_WARNING,
                15000,
            )
            return False

        # Nothing else is lock-gated on a drain: a queued Block or Reject
        # needs no lock, a non-HID allow never relied on one, and a HID allow
        # with treatment on was handed back above (treatment off means no
        # gating at all).
        del self._pending_decisions[identity]
        if target is DeviceTarget.ALLOW:
            self._last_prompted_at.pop(identity, None)
        log.info("Device %s is back as id %d -- applying the queued %s / %s",
                 identity, device.number, target.name, persistence.name)
        self._client.apply_device_policy(device.number, target, persistence,
                                         device.raw_rule if persistence is not Persistence.UNCHANGED else None)
        return True

    def _apply_user_decision(self, device: Device, target: DeviceTarget, persistence: Persistence,
                             device_present: bool = True) -> None:
        """Supersede pending work and dispatch a fresh choice.

        The app calls this after its dialog bookkeeping and the
        `_cancel_pending_device` wrapper (which also stops the deferred
        lock); everything from here is decision state and client calls.
        """
        identity = dialog_identity(device)
        self._pending_decisions.pop(identity, None)
        if target is DeviceTarget.ALLOW:
            # The user wanted this device usable, not quietly blocked on return.
            # Clear at dispatch too: an unsuccessful apply or durable write must
            # not suppress the next chance to decide, waiting for an ALLOW signal.
            self._last_prompted_at.pop(identity, None)
        if not device_present and persistence is Persistence.ALWAYS:
            log.info("Device %s is off the bus -- writing the permanent %s rule now", identity, target.name.lower())
            self._client.persist_rule(device.number, target, device.raw_rule)
            return
        if not device_present:
            log.info("Device %s is off the bus -- queuing %s / %s until it returns",
                     identity, target.name, persistence.name)
            if len(self._pending_decisions) >= MAX_PENDING_DECISIONS:
                dropped_ident = next(iter(self._pending_decisions))
                del self._pending_decisions[dropped_ident]
                log.warning("Pending-decision cap (%d) reached -- dropping the oldest queued decision for %s",
                            MAX_PENDING_DECISIONS, dropped_ident)
            self._pending_decisions[identity] = (target, persistence)
            return
        self._client.apply_device_policy(device.number, target, persistence,
                                         device.raw_rule if persistence is not Persistence.UNCHANGED else None)

    def _on_device_allowed(self, device: Device, rule_id: int) -> None:
        """React to a device becoming allowed by policy.

        Clears the prompt cooldown — an allow lets a later blocked insertion
        prompt again — and seeds the permanent-allow cache so future
        re-insertions skip HID treatment.  Cancelling pending work stays in
        the app's `_cancel_pending_device` wrapper: only it knows when the
        deferred-lock timer may stop.
        """
        self._last_prompted_at.pop(dialog_identity(device), None)
        # Seed the permanent-allow cache so future re-insertions skip
        # HID treatment.
        if rule_id > 0 and device.hash:
            self._permanent_allow_hashes.add(device.hash)

    def _on_device_inserted(self, device: Device, target: int) -> None:
        """React to a new insertion: replay held decisions, run the HID
        lock-first flow, defer while locked, or emit the prompt.

        Removals never reach here — the app handles them alongside its dialog
        bookkeeping and calls `_cancel_pending_device` directly.
        """
        # Synchronize retained dialogs even when this insertion will not
        # prompt (already allowed, HID lock flow, or screensaver deferral).
        self.dialog_retarget.emit(device)

        # Before anything else: a decision the user already made about this
        # device outranks every reaction below, including the ones that never
        # reach a prompt.  A device that comes back already allowed, or a HID
        # device heading for the lock-first flow, used to skip straight past
        # the queue and do the default instead.
        if self._apply_pending_decision(device):
            return

        # Use the target from the signal directly — more reliable than
        # parsing the rule string, which may not reflect the applied target.
        if target == int(DeviceTarget.ALLOW):
            log.debug("DevicePresenceChanged: id=%d skipped (target=ALLOW)", device.number)
            return

        log.info("INSERT device %d %s %r identity=%s target=%s", device.number, device.id,
                 device.name or "", dialog_identity(device), enum_name(DeviceTarget, target))

        # HID devices are handled before the screensaver check so that a
        # newly-attached keyboard can be used to unlock the screen.
        # has_hid_interface() catches composite devices (e.g. HID + MSC)
        # too — any HID interface can send keystrokes.
        #
        # The special-treatment path (auto-allow then lock) is skipped
        # entirely when the user has disabled it in settings OR when
        # screen locking is currently inhibited by a logind idle/block
        # inhibitor (dnf/rpm transaction, GNOME/KDE "Prevent screen
        # lock", systemd-inhibit --what=idle, ...). Auto-allowing a HID
        # device while lock is inhibited would hand an attached-keyboard
        # attacker typed input with no password prompt to gate it.
        has_hid = device.has_hid_interface()
        hid_treatment_enabled = not self._settings.disable_hid_treatment()
        lock_inhibited = self._screensaver.inhibited
        # The auto-allow-then-lock flow additionally requires that
        # locking is actually available: without it the deferred lock
        # would no-op, the 'Locking screen…' notice would be a lie, and
        # the pending device could never be auto-allowed.  Fall back to
        # the normal prompt path (whose HID Allow buttons stay disabled
        # while the lock stays down) instead.
        # The automatic-flow conjunction lives in _hid_lock_flow_applies.
        # A held HID Allow must obey the contract even when that flow cannot
        # run; _apply_pending_decision then requires a fresh choice instead.
        hid_special_treatment = self._hid_lock_flow_applies(device)
        if hid_special_treatment:
            if self._screensaver.active:
                log.info(
                    "HID device %d inserted while screen locked, allowing temporarily so it can unlock",
                    device.number,
                )
                self._client.apply_device_policy(device.number, DeviceTarget.ALLOW, persistence=Persistence.UNCHANGED)
                return
            # Skip HID treatment for devices that are already whitelisted
            # (matching a permanent allow rule from the daemon's policy).
            if device.hash and device.hash in self._permanent_allow_hashes:
                log.debug("DevicePresenceChanged: id=%d skipped (permanent allow hash match)", device.number)
                return
            self._hid_pending_devices.add(device.number)
            self.notify.emit(
                HID_ATTACHED_NOTICE_TITLE,
                "Locking screen. Enter your password to activate the device. "
                "If you did not attach a keyboard, check for malicious devices.",
                NOTIFY_WARNING,
                5000,
            )
            # Never restart an already-running lock timer: a second HID insert
            # would push the first device's lock back by another full delay.
            # _hid_pending_devices already covers every waiting device, so the
            # earliest scheduled lock serves them all.  The QTimer lives
            # app-side; its schedule_lock slot owns the isActive guard.
            self.schedule_lock.emit()
            return
        elif has_hid and hid_treatment_enabled:
            # lock_inhibited or not lock_available — fall through to the
            # normal prompt path
            reason = "screen locking is inhibited" if lock_inhibited else "screen locking is unavailable"
            log.info(
                "HID device %d inserted while %s — falling back to prompt (will not auto-allow)",
                device.number,
                reason,
            )

        # Non-HID device: defer while screen is locked, otherwise prompt.
        if self._screensaver.active:
            self._screensaver_pending_devices.add(device.number)
            log.info("Device %d inserted while screen locked, deferring", device.number)
            return

        self.show_dialog.emit(device)
