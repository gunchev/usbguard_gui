"""USBGuard D-Bus client using dbus-fast with QThread asyncio integration."""

from __future__ import annotations

import asyncio
import logging
import os

from dbus_fast import BusType, DBusError
from dbus_fast.aio import MessageBus
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from usbguard_gui.device import Device, DeviceTarget

log = logging.getLogger(__name__)

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


def _get_introspection(filename: str) -> str:
    module_dir = os.path.dirname(__file__)
    path = os.path.join(module_dir, "introspection", filename)
    with open(path) as f:
        return f.read()


class _DBusThread(QThread):
    finished = pyqtSignal()
    connection_changed = pyqtSignal(bool)
    device_presence_changed = pyqtSignal(int, int, int, str, dict)
    device_policy_changed = pyqtSignal(int, int, int, str, int, dict)
    list_devices_result = pyqtSignal(list)
    apply_policy_result = pyqtSignal(object)
    list_rules_result = pyqtSignal(list)
    remove_rule_result = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bus: MessageBus | None = None
        self._devices_iface = None
        self._policy_iface = None
        self._running = True
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except RuntimeError as e:
            # "Event loop stopped before Future completed" is expected when stop() calls loop.stop()
            if "Event loop stopped" not in str(e):
                log.error("DBus thread error: %s", e)
                self.error_occurred.emit(str(e))
        except Exception as e:
            log.error("DBus thread error: %s", e)
            self.error_occurred.emit(str(e))
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
            devices_introspection = _get_introspection("org.usbguard.Devices1.xml")
            devices_obj = self._bus.get_proxy_object(USBGUARD_BUS_NAME, USBGUARD_DEVICES_PATH, devices_introspection)
            self._devices_iface = devices_obj.get_interface(USBGUARD_DEVICES_IFACE)

            policy_introspection = _get_introspection("org.usbguard.Policy1.xml")
            policy_obj = self._bus.get_proxy_object(USBGUARD_BUS_NAME, USBGUARD_POLICY_PATH, policy_introspection)
            self._policy_iface = policy_obj.get_interface(USBGUARD_POLICY_IFACE)

            self._devices_iface.on_device_presence_changed(self._on_device_presence_changed)
            self._devices_iface.on_device_policy_changed(self._on_device_policy_changed)

            self._connected = True
            self.connection_changed.emit(True)
            log.info("Connected to USBGuard D-Bus service")

        except DBusError as e:
            log.error("Failed to connect to USBGuard: %s", e)
            self.connection_changed.emit(False)
            return

        while self._running:
            await asyncio.sleep(0.1)

        if self._bus:
            self._bus.disconnect()

    def _on_device_presence_changed(
        self, device_id: int, event: int, target: int, device_rule: str, attributes: dict
    ) -> None:
        self.device_presence_changed.emit(device_id, event, target, device_rule, attributes)

    def _on_device_policy_changed(
        self,
        device_id: int,
        target_old: int,
        target_new: int,
        device_rule: str,
        rule_id: int,
        attributes: dict,
    ) -> None:
        self.device_policy_changed.emit(device_id, target_old, target_new, device_rule, rule_id, attributes)

    def _schedule(self, coro: asyncio.coroutine) -> None:
        if self._loop and self._running:
            self._loop.call_soon_threadsafe(asyncio.ensure_future, coro)

    def stop(self) -> None:
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    async def _do_list_devices(self, query: str) -> None:
        try:
            raw = await self._devices_iface.call_list_devices(query)
            devices = [Device.from_dbus(int(dev_id), str(rule_str)) for dev_id, rule_str in raw]
            self.list_devices_result.emit(devices)
        except DBusError as e:
            log.error("Failed to list devices (query=%s): %s", query, e)
            if not _is_permission_error(e):
                self._connected = False
                self.connection_changed.emit(False)
            self.list_devices_result.emit([])

    async def _do_apply_policy(self, device_id: int, target: DeviceTarget, permanent: bool) -> None:
        try:
            rule_id = await self._devices_iface.call_apply_device_policy(device_id, int(target), permanent)
            log.info("Applied %s to device %d (permanent=%s) → rule %d", target.name, device_id, permanent, rule_id)
            self.apply_policy_result.emit(int(rule_id))
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
                self._connected = False
                self.connection_changed.emit(False)
            self.apply_policy_result.emit(None)

    async def _do_list_rules(self, label: str) -> None:
        try:
            raw = await self._policy_iface.call_list_rules(label)
            rules = [(int(rule_id), str(rule_str)) for rule_id, rule_str in raw]
            self.list_rules_result.emit(rules)
        except DBusError as e:
            log.error("Failed to list rules (label='%s'): %s", label, e)
            if not _is_permission_error(e):
                self._connected = False
                self.connection_changed.emit(False)
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
                self._connected = False
                self.connection_changed.emit(False)
            self.remove_rule_result.emit(False)

    def list_devices(self, query: str = "match") -> None:
        if self._devices_iface and self._loop:
            self._schedule(self._do_list_devices(query))

    def apply_device_policy(self, device_id: int, target: DeviceTarget, permanent: bool = False) -> None:
        if self._devices_iface and self._loop:
            self._schedule(self._do_apply_policy(device_id, target, permanent))

    def list_rules(self, label: str = "") -> None:
        if self._policy_iface and self._loop:
            self._schedule(self._do_list_rules(label))

    def remove_rule(self, rule_id: int) -> None:
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
    apply_policy_result = pyqtSignal(object)
    list_rules_result = pyqtSignal(list)
    remove_rule_result = pyqtSignal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: _DBusThread | None = None

    @property
    def connected(self) -> bool:
        return self._thread.is_connected if self._thread else False

    def connect(self) -> bool:
        self._thread = _DBusThread(self)
        self._thread.connection_changed.connect(self.connection_changed)
        self._thread.device_presence_changed.connect(self.device_presence_changed)
        self._thread.device_policy_changed.connect(self.device_policy_changed)
        self._thread.list_devices_result.connect(self.list_devices_result)
        self._thread.apply_policy_result.connect(self.apply_policy_result)
        self._thread.list_rules_result.connect(self.list_rules_result)
        self._thread.remove_rule_result.connect(self.remove_rule_result)
        self._thread.start()
        return True

    def stop(self) -> None:
        if self._thread:
            self._thread.stop()
            self._thread.wait()
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

    def remove_rule(self, rule_id: int) -> None:
        if self._thread:
            self._thread.remove_rule(rule_id)
