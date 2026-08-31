"""USBGuard D-Bus client using dbus-fast with QThread asyncio integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from dbus_fast import BusType, DBusError
from dbus_fast.aio import MessageBus
from PyQt6.QtCore import QObject, pyqtSignal

from usbguard_gui.dbus_common import DBUS_BUS_NAME, DBUS_BUS_PATH, DBUS_IFACE, THREAD_STOP_TIMEOUT_MS, \
    AsyncWorkerThread, get_introspection, stop_worker_thread
from usbguard_gui.device import Device, DeviceTarget

log = logging.getLogger(__name__)

# Kept as a module-level alias: existing tests import this name directly.
_THREAD_STOP_TIMEOUT_MS = THREAD_STOP_TIMEOUT_MS

USBGUARD_BUS_NAME = "org.usbguard1"
USBGUARD_DEVICES_PATH = "/org/usbguard1/Devices"
USBGUARD_POLICY_PATH = "/org/usbguard1/Policy"
USBGUARD_DEVICES_IFACE = "org.usbguard.Devices1"
USBGUARD_POLICY_IFACE = "org.usbguard.Policy1"

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
_DEVICES_INTROSPECTION = get_introspection("org.usbguard.Devices1.xml")
_POLICY_INTROSPECTION = get_introspection("org.usbguard.Policy1.xml")
_DBUS_INTROSPECTION = get_introspection("org.freedesktop.DBus.xml")


class _DBusThread(AsyncWorkerThread):
    finished = pyqtSignal()
    connection_changed = pyqtSignal(bool)
    device_presence_changed = pyqtSignal(int, int, int, str, dict)
    device_policy_changed = pyqtSignal(int, int, int, str, int, dict)
    list_devices_result = pyqtSignal(list)
    list_rules_result = pyqtSignal(list)
    remove_rule_result = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)

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

    async def _do_apply_policy(self, device_id: int, target: DeviceTarget, permanent: bool) -> None:
        try:
            rule_id = await self._devices_iface.call_apply_device_policy(device_id, int(target), permanent)
            log.info("Applied %s to device %d (permanent=%s) → rule %d", target.name, device_id, permanent, rule_id)
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

    def apply_device_policy(self, device_id: int, target: DeviceTarget, permanent: bool = False) -> None:
        if not self._connected:
            return
        if self._devices_iface and self._loop:
            self._schedule(self._do_apply_policy(device_id, target, permanent))

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
    list_rules_result = pyqtSignal(list)
    remove_rule_result = pyqtSignal(bool)

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
        self.stop()
        self._thread = _DBusThread(self)
        self._thread.connection_changed.connect(self.connection_changed)
        self._thread.device_presence_changed.connect(self.device_presence_changed)
        self._thread.device_policy_changed.connect(self.device_policy_changed)
        self._thread.list_devices_result.connect(self.list_devices_result)
        self._thread.list_rules_result.connect(self.list_rules_result)
        self._thread.remove_rule_result.connect(self.remove_rule_result)
        self._thread.start()
        return True

    def stop(self) -> None:
        if self._thread:
            stop_worker_thread(self._thread, "D-Bus", log)
            self._thread = None

    def list_devices(self, query: str = "match") -> None:
        if self._thread:
            self._thread.list_devices(query)

    def apply_device_policy(self, device_id: int, target: DeviceTarget, permanent: bool = False) -> None:
        if self._thread:
            self._thread.apply_device_policy(device_id, target, permanent)

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
