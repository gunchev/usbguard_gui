"""USBGuard D-Bus client using dbus-fast with QThread asyncio integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from dbus_fast import BusType, DBusError
from dbus_fast.aio import MessageBus
from PyQt6.QtCore import QObject, pyqtSignal

from usbguard_gui.dbus_common import DBUS_BUS_NAME, DBUS_BUS_PATH, DBUS_IFACE, THREAD_STOP_TIMEOUT_MS, \
    AsyncWorkerThread, get_introspection, recycle_worker_thread, stop_worker_thread
from usbguard_gui.device import Device, DeviceTarget, parse_device_rule, rule_matches_device, rule_persistence_problem

log = logging.getLogger(__name__)

# Kept as a module-level alias: existing tests import this name directly.
_THREAD_STOP_TIMEOUT_MS = THREAD_STOP_TIMEOUT_MS

USBGUARD_BUS_NAME = "org.usbguard1"
USBGUARD_DEVICES_PATH = "/org/usbguard1/Devices"
USBGUARD_POLICY_PATH = "/org/usbguard1/Policy"
USBGUARD_DEVICES_IFACE = "org.usbguard.Devices1"
USBGUARD_POLICY_IFACE = "org.usbguard.Policy1"

# Policy1.appendRule() takes the id of the rule to insert after; UINT32_MAX-2
# means "append at the end".  Verified against usbguard 1.1.4: the rule landed
# after the last existing one and came back with a fresh id.
_APPEND_RULE_AT_END = (1 << 32) - 3

_PERMISSION_ERRORS = frozenset(
    [
        "org.freedesktop.DBus.Error.AccessDenied",
        "org.freedesktop.PolicyKit1.Error.NotAuthorized",
        "org.usbguard.Error.PermissionDenied",
    ]
)


def _is_permission_error(e: DBusError) -> bool:
    error_name = getattr(e, "type", "") or ""
    if error_name in _PERMISSION_ERRORS:
        return True
    msg = str(e)
    return "Not authorized" in msg or "AccessDenied" in msg


# D-Bus error types that indicate the transport/session itself is broken
# (daemon gone, bus torn down) rather than an ordinary per-call failure
# (bad device id, unknown rule id, ...). Only these should flip the
# connection state and trigger a full reconnect.
_CONNECTION_ERRORS = frozenset(
    [
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.NameHasNoOwner",
        "org.freedesktop.DBus.Error.NoReply",
        "org.freedesktop.DBus.Error.Disconnected",
        "org.freedesktop.DBus.Error.Timeout",
        "org.freedesktop.DBus.Error.IOError",
        "org.freedesktop.DBus.Error.NoServer",
        "org.freedesktop.DBus.Error.NoNetwork",
    ]
)


def _is_connection_error(e: DBusError) -> bool:
    return (getattr(e, "type", "") or "") in _CONNECTION_ERRORS


# Pre-load introspection XML at import time so the async event loop never
# blocks on file I/O.
def _retarget_device_rule(rule: str, target: DeviceTarget) -> str:
    """Return `rule` with only its target verb replaced.

    Every attribute is preserved verbatim — crucially parent-hash and via-port,
    which identify *which* instance of an identical device this is.  A KVM or
    dock presents the same hardware under different topologies, and those must
    coexist as separate permanent rules rather than be collapsed into one rule
    broad enough to match them all.
    """
    parts = rule.strip().split(None, 1)
    return target.name.lower() + (f" {parts[1]}" if len(parts) == 2 else "")


# Attributes that pin a rule to one physical device in one topology.  Two rules
# agreeing on all of them describe the same insertion point, so a permanent
# decision must update the rule already there instead of adding another.
# parent-hash and via-port are what tell the KVM's sibling hubs apart; leaving
# them out of the identity is the collapse this whole path exists to avoid.
_RULE_IDENTITY_ATTRS = ("id", "serial", "hash", "parent_hash", "via_port")


def _rule_identity(rule: str) -> tuple[str, ...] | None:
    """Return the device/topology identity a permanent rule is keyed on.

    ``None`` when the rule names no device — ``allow with-interface { ... }``,
    or anything unparseable.  Such a rule covers a *class* of devices rather
    than one, so claiming it as "this device's rule" would let a permanent
    decision clobber hand-written policy.  Rules without an identity are never
    matched, and the permanent path simply appends alongside them.
    """
    try:
        parsed = parse_device_rule(rule)
    except Exception as e:  # a rule we cannot read is simply not a match
        log.debug("Could not parse rule for deduplication: %s", e)
        return None
    identity = tuple(str(parsed.get(attr) or "") for attr in _RULE_IDENTITY_ATTRS)
    return identity if identity[0] else None


def _normalize_rule(rule: str) -> str:
    """Collapse whitespace so two spellings of one rule compare equal."""
    return " ".join(rule.split())


_DEVICES_INTROSPECTION = get_introspection("org.usbguard.Devices1.xml")
_POLICY_INTROSPECTION = get_introspection("org.usbguard.Policy1.xml")
_DBUS_INTROSPECTION = get_introspection("org.freedesktop.DBus.xml")


class _DBusThread(AsyncWorkerThread):
    finished = pyqtSignal()
    connection_changed = pyqtSignal(bool)
    device_presence_changed = pyqtSignal(int, int, int, str, dict)
    device_policy_changed = pyqtSignal(int, int, int, str, int, dict)
    list_devices_result = pyqtSignal(list)
    list_devices_correlated = pyqtSignal(int, list)
    list_rules_result = pyqtSignal(list)
    remove_rule_result = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)
    # Emitted when the device was authorized live but the durable rule did
    # not land.  Distinct from error_occurred on purpose: the caller needs to
    # know the decision is *temporary*, not merely that something failed.
    permanent_write_failed = pyqtSignal(int, str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._bus: MessageBus | None = None
        self._bus_iface: Any = None     # ProxyInterface — dbus-fast dynamic API
        self._devices_iface: Any = None  # ProxyInterface — dbus-fast dynamic API
        self._policy_iface: Any = None   # ProxyInterface — dbus-fast dynamic API
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _set_connected(self, connected: bool) -> None:
        """Update the connection state, emitting connection_changed on change."""
        if connected == self._connected:
            return
        self._connected = connected
        self.connection_changed.emit(connected)

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:
            log.error("DBus thread error: %s", e)
            self.error_occurred.emit(str(e))
            # Report the failure too, so the app schedules a reconnect —
            # without this the app would sit in "connecting..." forever.
            self.connection_changed.emit(False)
        finally:
            self._loop.close()
            self.finished.emit()

    async def _main(self) -> None:
        try:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception as e:
            log.error("Failed to connect to system D-Bus: %s", e)
            self.connection_changed.emit(False)
            return

        try:
            # Watch the bus for ownership of the USBGuard name so the daemon
            # dying or (re)appearing is detected proactively via
            # NameOwnerChanged, instead of staying 'connected' until the next
            # D-Bus call happens to fail.
            dbus_obj = self._bus.get_proxy_object(
                DBUS_BUS_NAME, DBUS_BUS_PATH, _DBUS_INTROSPECTION  # pyright: ignore[reportArgumentType]
            )
            self._bus_iface = dbus_obj.get_interface(DBUS_IFACE)
            self._bus_iface.on_name_owner_changed(self._on_name_owner_changed)

            devices_obj = self._bus.get_proxy_object(
                USBGUARD_BUS_NAME, USBGUARD_DEVICES_PATH, _DEVICES_INTROSPECTION  # pyright: ignore[reportArgumentType]
            )
            self._devices_iface = devices_obj.get_interface(USBGUARD_DEVICES_IFACE)

            policy_obj = self._bus.get_proxy_object(
                USBGUARD_BUS_NAME, USBGUARD_POLICY_PATH, _POLICY_INTROSPECTION  # pyright: ignore[reportArgumentType]
            )
            self._policy_iface = policy_obj.get_interface(USBGUARD_POLICY_IFACE)

            self._devices_iface.on_device_presence_changed(self._on_device_presence_changed)
            self._devices_iface.on_device_policy_changed(self._on_device_policy_changed)

            # Initial state: the proxies above are valid even while the daemon
            # is absent (they target the well-known name), so report
            # 'connected' only when the name is actually owned.  The initial
            # report is unconditional so the app learns the probe happened.
            # An absent daemon must not kill the thread — the
            # NameOwnerChanged subscription flips the state when it appears.
            try:
                owner = await self._bus_iface.call_get_name_owner(USBGUARD_BUS_NAME)
            except DBusError as e:
                # dbus-daemon answers a never-registered name with
                # NameHasNoOwner rather than the spec's empty string.
                if getattr(e, "type", "") != "org.freedesktop.DBus.Error.NameHasNoOwner":
                    raise
                owner = ""
            self._connected = bool(owner)
            self.connection_changed.emit(self._connected)
            if owner:
                log.info("Connected to USBGuard D-Bus service")
            else:
                log.warning("USBGuard daemon not present on the bus — waiting for it to appear")

        except DBusError as e:
            log.error("Failed to connect to USBGuard: %s", e)
            self._set_connected(False)
            return

        while self._running:
            await asyncio.sleep(0.1)

        if self._bus:
            self._bus.disconnect()

    def _on_name_owner_changed(self, name: str, old_owner: str, new_owner: str) -> None:
        """React to the USBGuard daemon taking or releasing its bus name.

        Owner gain (old_owner empty) is the daemon starting, owner loss
        (new_owner empty) is it dying — both flip the connection state
        immediately.  An ownership handover (daemon restart, both non-empty)
        needs no action: the proxies target the well-known name, so calls
        and signal subscriptions keep working against the new owner.
        """
        if name != USBGUARD_BUS_NAME:
            return
        if bool(new_owner) == self._connected:
            return
        if new_owner:
            log.info("USBGuard daemon appeared on the bus (%s)", new_owner)
        else:
            log.warning("USBGuard daemon left the bus (was %s)", old_owner)
        self._set_connected(bool(new_owner))

    def _on_device_presence_changed(self, device_id: int, event: int, target: int, device_rule: str,
                                    attributes: dict[str, str]) -> None:
        self.device_presence_changed.emit(device_id, event, target, device_rule, attributes)

    def _on_device_policy_changed(self, device_id: int, target_old: int, target_new: int, device_rule: str,
                                  rule_id: int, attributes: dict[str, str]) -> None:
        self.device_policy_changed.emit(device_id, target_old, target_new, device_rule, rule_id, attributes)

    async def _do_list_devices(self, query: str) -> None:
        try:
            raw = await self._devices_iface.call_list_devices(query)
            devices = [Device.from_dbus(int(dev_id), str(rule_str)) for dev_id, rule_str in raw]
            self.list_devices_result.emit(devices)
        except DBusError as e:
            log.error("Failed to list devices (query=%s): %s", query, e)
            if _is_connection_error(e):
                self._set_connected(False)
            self.list_devices_result.emit([])

    async def _do_fetch_devices(self, request_id: int, query: str) -> None:
        """List devices for one specific caller and hand the snapshot back tagged
        with that caller's request id, so concurrent fetches can never be
        mistaken for each other (see fetch_devices())."""
        try:
            raw = await self._devices_iface.call_list_devices(query)
            devices = [Device.from_dbus(int(dev_id), str(rule_str)) for dev_id, rule_str in raw]
        except DBusError as e:
            log.error("Failed to fetch devices (request=%d, query=%s): %s", request_id, query, e)
            if _is_connection_error(e):
                self._set_connected(False)
            self.list_devices_correlated.emit(request_id, [])
            return
        self.list_devices_correlated.emit(request_id, devices)

    async def _do_apply_policy(self, device_id: int, target: DeviceTarget, permanent: bool,
                               device_rule: str | None = None) -> None:
        try:
            if permanent and device_rule and self._policy_iface is not None:
                # applyDevicePolicy(permanent=True) makes USBGuard *upsert* the
                # rule it generates for this device, keyed on the device hash.
                # Chained identical hubs — what a KVM switch produces — hash
                # alike, so allowing one silently replaces the other's rule and
                # every switch cycle prompts again (observed: rule id 16 removed
                # and re-added with a different parent-hash, back and forth).
                #
                # Authorize the connected instance temporarily instead, then
                # append the exact topology-specific rule permanently.  Once each
                # topology has been seen, all of them stay valid at the same time.
                rule = _retarget_device_rule(device_rule, target)
                problem = rule_persistence_problem(rule)
                if problem is not None:
                    # Defence in depth.  This string started life as a
                    # device's own descriptors, and it is about to become
                    # permanent policy.  Anything that is not one
                    # well-formed rule of known device attributes is not
                    # written; the daemon's own upsert is used instead,
                    # which never takes a device-supplied string, so the
                    # user still gets the permanent decision they asked
                    # for -- just not one assembled from a suspect rule.
                    log.warning("Refusing to persist the device-reported rule for device %d: %s; "
                                "falling back to the daemon's upsert", device_id, problem)
                    await self._devices_iface.call_apply_device_policy(device_id, int(target), True)
                    return

                await self._devices_iface.call_apply_device_policy(device_id, int(target), False)
                try:
                    await self._persist_device_rule(device_id, rule)
                except DBusError as e:
                    # The device is live-allowed (or live-blocked) right now
                    # and the durable half never landed.  Silence here is the
                    # actual defect: the denial is a log line nobody reads,
                    # the user believes they granted permanence, and the
                    # decision quietly expires the next time the device is
                    # unplugged.
                    self.permanent_write_failed.emit(device_id, target.name.lower(), str(e))
                    raise
            else:
                rule_id = await self._devices_iface.call_apply_device_policy(device_id, int(target), permanent)
                log.info("Applied %s to device %d (permanent=%s) → rule %d",
                         target.name, device_id, permanent, rule_id)
        except DBusError as e:
            if _is_permission_error(e):
                log.error(
                    "Not authorized to apply policy to device %d (target=%s, permanent=%s) — install polkit rule",
                    device_id,
                    target.name,
                    permanent,
                )
            else:
                log.error(
                    "Failed to apply policy to device %d (target=%s, permanent=%s): %s",
                    device_id,
                    target.name,
                    permanent,
                    e,
                )
                if _is_connection_error(e):
                    self._set_connected(False)

    async def _list_permanent_rules(self) -> list[tuple[int, str]]:
        """The permanent ruleset, in the order the daemon evaluates it."""
        raw = await self._policy_iface.call_list_rules("")
        return [(int(rule_id), str(rule_str)) for rule_id, rule_str in raw]

    @staticmethod
    def _placement_for(others: list[tuple[int, str]], device: Device) -> tuple[int, str | None]:
        """Where to write the new rule so an earlier rule cannot shadow it.

        Walk the ruleset in evaluation order and stop at the first rule that
        provably matches this device.  The new rule belongs directly above
        it: USBGuard is first-match-wins, so anything above that already
        matches turns the new rule into dead code.

        Returns the ``parent_id`` to append after, plus a note for the log.
        Two limits shape it, both verified against the live daemon:

        * The daemon cannot insert above the *first* rule, so when the
          shadowing rule sits there there is nowhere better to go.  The write
          still happens -- dropping the user's decision silently would be
          worse -- but the note says the rule may be shadowed.
        * Rules this matcher cannot decide are skipped rather than counted as
          matches, so an unreadable rule keeps its rank.  Promoting a tray
          decision above administrator-written policy needs better evidence
          than a parsing gap.
        """
        undecidable: list[int] = []

        for index, (rule_id, text) in enumerate(others):
            verdict = rule_matches_device(text, device)
            if verdict is True:
                if index == 0:
                    return _APPEND_RULE_AT_END, (
                        f"rule {rule_id} already matches this device and nothing sits above it to "
                        "insert past; the daemon cannot place a rule before the first one, so this "
                        "rule may be shadowed -- reorder rules.conf if the decision does not hold")
                return others[index - 1][0], f"inserted above rule {rule_id}, which already matches this device"
            if verdict is None:
                undecidable.append(rule_id)

        if undecidable:
            return _APPEND_RULE_AT_END, (f"{len(undecidable)} existing rule(s) could not be checked for "
                                         f"shadowing (ids {undecidable})")
        return _APPEND_RULE_AT_END, None

    async def _persist_device_rule(self, device_id: int, rule: str) -> None:
        """Keep exactly one permanent rule for this device, where it will apply.

        Two things go wrong when writing a permanent rule, and the daemon
        guards against neither:

        * **Accumulation.** ``appendRule`` has no upsert semantics, so
          appending unconditionally grows ``/etc/usbguard/rules.conf`` on
          every decision -- allow, block and re-allow the same KVM port and
          three rules are left behind for one device, and the audit trail
          stops saying what the user chose.
        * **Shadowing.** Rules are evaluated top-down and the first match
          wins, so a rule appended at the end is dead code whenever a rule
          above it already matches the device.

        So read the ruleset, drop the device's previous rule if it has one,
        and put the new one above the first rule that would shadow it.
        """
        identity = _rule_identity(rule)

        try:
            rules = await self._list_permanent_rules()
        except DBusError as e:
            # Unreadable ruleset: fall through to a plain append.  Losing
            # the user's permanent decision is worse than a possible
            # duplicate, and a later decision can heal it.  A broken
            # transport still surfaces -- the append below raises, and
            # _do_apply_policy owns the reconnect decision.
            log.warning("Cannot read permanent rules for device %d, appending without a duplicate "
                        "or shadow check: %s", device_id, e)
            rules = []

        if identity is None:
            # A rule naming no device cannot be deduplicated or reasoned
            # about positionally; just write it.
            own, others = [], rules
        else:
            own = [(rule_id, text) for rule_id, text in rules if _rule_identity(text) == identity]
            others = [(rule_id, text) for rule_id, text in rules if _rule_identity(text) != identity]

        if len(own) > 1:
            # Already-bloated policy: earlier builds of this path left one
            # rule per decision.  Update the first and say so out loud --
            # deleting the rest is not ours to decide, since any of them
            # could have been written by hand.
            log.warning("%d permanent rules share device %d's identity (ids %s) -- updating the "
                        "first, prune the rest by hand",
                        len(own), device_id, [rule_id for rule_id, _ in own])
        existing = own[0] if own else None

        if existing is not None and _normalize_rule(existing[1]) == _normalize_rule(rule):
            log.info("Permanent rule %d already covers device %d -- not appending a duplicate",
                     existing[0], device_id)
            return

        parent_id, placement_note = (self._placement_for(others, Device.from_dbus(device_id, rule))
                                     if identity is not None else (_APPEND_RULE_AT_END, None))

        if existing is not None:
            # Remove before appending, never after: appending first would
            # leave the older rule above the new one, and first-match-wins
            # would go on honouring it -- silently ignoring a fresh `block`
            # until the removal landed, and forever if it did not.
            await self._policy_iface.call_remove_rule(existing[0])

        try:
            rule_id = await self._policy_iface.call_append_rule(rule, parent_id, False)
        except DBusError:
            if existing is not None:
                await self._restore_permanent_rule(device_id, existing)
            raise

        log.info("Stored permanent rule %d for device %d%s", rule_id, device_id,
                 f" -- {placement_note}" if placement_note else "")

    async def _restore_permanent_rule(self, device_id: int, previous: tuple[int, str]) -> None:
        """Put back a permanent rule we removed but could not replace.

        Remove-then-append is not atomic.  If the append fails -- a polkit
        denial, a daemon hiccup -- the policy is left holding *less* than it
        did before the user clicked, while the device runs on a temporary
        state nobody chose to make temporary.  Restore best-effort, and if
        that fails too say so loudly: the operator is looking at a policy
        that changed under them and needs to know it.
        """
        rule_id, text = previous
        try:
            await self._policy_iface.call_append_rule(text, _APPEND_RULE_AT_END, False)
            log.warning("Restored permanent rule %d for device %d after the replacement failed",
                        rule_id, device_id)
        except DBusError as e:
            log.error("Permanent rule %d for device %d was removed and could NOT be restored: %s",
                      rule_id, device_id, e)

    async def _do_list_rules(self, label: str) -> None:
        try:
            raw = await self._policy_iface.call_list_rules(label)
            rules = [(int(rule_id), str(rule_str)) for rule_id, rule_str in raw]
            self.list_rules_result.emit(rules)
        except DBusError as e:
            log.error("Failed to list rules (label='%s'): %s", label, e)
            if _is_connection_error(e):
                self._set_connected(False)
            self.list_rules_result.emit([])

    async def _do_remove_rule(self, rule_id: int) -> None:
        try:
            await self._policy_iface.call_remove_rule(rule_id)
            log.info("Removed rule %d", rule_id)
            self.remove_rule_result.emit(True)
        except DBusError as e:
            if _is_permission_error(e):
                log.error("Not authorized to remove rule %d", rule_id)
            else:
                log.error("Failed to remove rule %d: %s", rule_id, e)
                if _is_connection_error(e):
                    self._set_connected(False)
            self.remove_rule_result.emit(False)

    def list_devices(self, query: str = "match") -> None:
        if not self._connected:
            self.list_devices_result.emit([])
            return
        if self._devices_iface and self._loop:
            self._schedule(self._do_list_devices(query))

    def fetch_devices(self, request_id: int, query: str = "match") -> None:
        """Request a device snapshot tagged with ``request_id``.

        Identical to list_devices() except that the result comes back on
        list_devices_correlated with the same id, so the caller can tell its own
        answer from anybody else's and does not care what order the answers
        arrive in.  Always terminates: a fast-fail or a D-Bus error still emits
        ``(request_id, [])`` rather than leaving the caller waiting.
        """
        if not self._connected:
            self.list_devices_correlated.emit(request_id, [])
            return
        if self._devices_iface and self._loop:
            self._schedule(self._do_fetch_devices(request_id, query))

    def apply_device_policy(self, device_id: int, target: DeviceTarget, permanent: bool = False,
                            device_rule: str | None = None) -> None:
        if not self._connected:
            return
        if self._devices_iface and self._loop:
            self._schedule(self._do_apply_policy(device_id, target, permanent, device_rule))

    def list_rules(self, label: str = "") -> None:
        if not self._connected:
            self.list_rules_result.emit([])
            return
        if self._policy_iface and self._loop:
            self._schedule(self._do_list_rules(label))

    def remove_rule(self, rule_id: int) -> None:
        if not self._connected:
            self.remove_rule_result.emit(False)
            return
        if self._policy_iface and self._loop:
            self._schedule(self._do_remove_rule(rule_id))


class USBGuardClient(QObject):
    """D-Bus client for the USBGuard daemon.

    Emits Qt signals when device events occur, so the GUI can react
    without direct D-Bus coupling.
    """

    device_presence_changed = pyqtSignal(int, int, int, str, dict)
    device_policy_changed = pyqtSignal(int, int, int, str, int, dict)
    connection_changed = pyqtSignal(bool)
    list_devices_result = pyqtSignal(list)
    list_devices_correlated = pyqtSignal(int, list)
    list_rules_result = pyqtSignal(list)
    remove_rule_result = pyqtSignal(bool)
    permanent_write_failed = pyqtSignal(int, str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: _DBusThread | None = None

    @property
    def connected(self) -> bool:
        return self._thread.is_connected if self._thread else False

    def connect(self) -> bool:
        # Recycle any previous worker: the app calls connect() again on every
        # reconnect attempt, and leaving the old thread running would leak a
        # live QThread (with its D-Bus connection and signal subscriptions)
        # that keeps delivering duplicate events after a daemon restart.
        # The non-blocking recycle variant is used deliberately: this runs on
        # the Qt main thread on every backoff retry, so a wedged worker must
        # cost a short grace period, not the full THREAD_STOP_TIMEOUT_MS.
        old, self._thread = self._thread, None
        if old is not None:
            recycle_worker_thread(old, "D-Bus", log)
        self._thread = _DBusThread(self)
        self._thread.connection_changed.connect(self.connection_changed)
        self._thread.device_presence_changed.connect(self.device_presence_changed)
        self._thread.device_policy_changed.connect(self.device_policy_changed)
        self._thread.list_devices_result.connect(self.list_devices_result)
        self._thread.list_devices_correlated.connect(self.list_devices_correlated)
        self._thread.list_rules_result.connect(self.list_rules_result)
        self._thread.remove_rule_result.connect(self.remove_rule_result)
        self._thread.permanent_write_failed.connect(self.permanent_write_failed)
        self._thread.start()
        return True

    def stop(self) -> None:
        if self._thread:
            stop_worker_thread(self._thread, "D-Bus", log)
            self._thread = None

    def list_devices(self, query: str = "match") -> None:
        if self._thread:
            self._thread.list_devices(query)

    def fetch_devices(self, request_id: int, query: str = "match") -> None:
        """Correlated variant of list_devices(); the answer arrives on
        list_devices_correlated(request_id, devices).

        With no worker thread there is nothing to ask, but the caller still gets
        its (request_id, []) so no request can be left permanently outstanding.
        """
        if self._thread:
            self._thread.fetch_devices(request_id, query)
        else:
            self.list_devices_correlated.emit(request_id, [])

    def apply_device_policy(self, device_id: int, target: DeviceTarget, permanent: bool = False,
                            device_rule: str | None = None) -> None:
        if self._thread:
            self._thread.apply_device_policy(device_id, target, permanent, device_rule)

    def list_rules(self, label: str = "") -> None:
        if self._thread:
            self._thread.list_rules(label)

    # Not currently called from application code — the GUI deliberately never
    # removes rules (see DeviceListWindow._apply: permanent and temporary
    # rules are indistinguishable from the rule string, so removing one could
    # silently revoke persistent authorization).  Kept for interface
    # completeness: it may get used in the future (e.g. an explicit, user-
    # initiated "Remove rule" action in a rules view), so do not treat it as
    # dead code.
    def remove_rule(self, rule_id: int) -> None:
        if self._thread:
            self._thread.remove_rule(rule_id)
