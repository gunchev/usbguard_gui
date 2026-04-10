"""USBGuard D-Bus client using dbus-fast."""

from __future__ import annotations

import contextlib
import logging
import os

from dbus_fast import BusType, DBusError
from dbus_fast.glib import MessageBus
from dbus_fast.glib.proxy_object import ProxyInterface, ProxyObject
from PyQt6.QtCore import QObject, pyqtSignal

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


class USBGuardClient(QObject):
    """D-Bus client for the USBGuard daemon.

    Emits Qt signals when device events occur, so the GUI can react
    without direct D-Bus coupling.
    """

    device_presence_changed = pyqtSignal(int, int, int, str, dict)
    device_policy_changed = pyqtSignal(int, int, int, str, int, dict)
    connection_changed = pyqtSignal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._bus: MessageBus | None = None
        self._devices_iface: ProxyInterface | None = None
        self._policy_iface: ProxyInterface | None = None
        self._connected = False
        self._devices_obj: ProxyObject | None = None
        self._policy_obj: ProxyObject | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        """Connect to the USBGuard D-Bus service. Returns True on success."""
        self._disconnect()
        try:
            self._bus = MessageBus(bus_type=BusType.SYSTEM)
            self._bus.connect_sync()
        except Exception as e:
            log.error("Failed to connect to system D-Bus: %s", e)
            self._connected = False
            self.connection_changed.emit(False)
            return False

        try:
            devices_introspection = _get_introspection("org.usbguard.Devices1.xml")
            self._devices_obj = self._bus.get_proxy_object(
                USBGUARD_BUS_NAME, USBGUARD_DEVICES_PATH, devices_introspection
            )
            self._devices_iface = self._devices_obj.get_interface(USBGUARD_DEVICES_IFACE)

            policy_introspection = _get_introspection("org.usbguard.Policy1.xml")
            self._policy_obj = self._bus.get_proxy_object(USBGUARD_BUS_NAME, USBGUARD_POLICY_PATH, policy_introspection)
            self._policy_iface = self._policy_obj.get_interface(USBGUARD_POLICY_IFACE)

            self._devices_iface.on_device_presence_changed(self._on_device_presence_changed)
            self._devices_iface.on_device_policy_changed(self._on_device_policy_changed)

            self._connected = True
            self.connection_changed.emit(True)
            log.info("Connected to USBGuard D-Bus service")
            return True

        except DBusError as e:
            log.error("Failed to connect to USBGuard: %s", e)
            self._disconnect()
            self.connection_changed.emit(False)
            return False
        except Exception as e:
            log.error("Unexpected error connecting to USBGuard: %s", e)
            self._disconnect()
            self.connection_changed.emit(False)
            return False

    def _disconnect(self) -> None:
        if self._devices_iface:
            try:
                self._devices_iface.off_device_presence_changed(self._on_device_presence_changed)
                self._devices_iface.off_device_policy_changed(self._on_device_policy_changed)
            except Exception as e:
                log.debug("Failed to unsubscribe from signals: %s", e)
        self._devices_iface = None
        self._policy_iface = None
        self._devices_obj = None
        self._policy_obj = None
        if self._bus:
            with contextlib.suppress(Exception):
                self._bus.disconnect()
            self._bus = None

    def _on_device_presence_changed(
        self, device_id: int, event: int, target: int, device_rule: str, attributes: dict
    ) -> None:
        log.debug("DevicePresenceChanged: id=%d event=%d target=%d", device_id, event, target)
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
        log.debug("DevicePolicyChanged: id=%d %d->%d", device_id, target_old, target_new)
        self.device_policy_changed.emit(device_id, target_old, target_new, device_rule, rule_id, attributes)

    def list_devices(self, query: str = "match") -> list[Device]:
        """List devices matching the given query."""
        if not self._devices_iface:
            return []
        try:
            raw = self._devices_iface.call_list_devices_sync(query)
            return [Device.from_dbus(int(dev_id), str(rule_str)) for dev_id, rule_str in raw]
        except DBusError as e:
            log.error("Failed to list devices (query=%s): %s", query, e)
            if not _is_permission_error(e):
                self._handle_disconnect()
            return []

    def apply_device_policy(self, device_id: int, target: DeviceTarget, permanent: bool = False) -> int | None:
        """Apply an authorization policy to a device. Returns rule_id or None on error."""
        if not self._devices_iface:
            return None
        try:
            rule_id = self._devices_iface.call_apply_device_policy_sync(device_id, int(target), permanent)
            log.info("Applied %s to device %d (permanent=%s) → rule %d", target.name, device_id, permanent, rule_id)
            return int(rule_id)
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
                self._handle_disconnect()
            return None

    def list_rules(self, label: str = "") -> list[tuple[int, str]]:
        """List policy rules."""
        if not self._policy_iface:
            return []
        try:
            raw = self._policy_iface.call_list_rules_sync(label)
            return [(int(rule_id), str(rule_str)) for rule_id, rule_str in raw]
        except DBusError as e:
            log.error("Failed to list rules (label='%s'): %s", label, e)
            if not _is_permission_error(e):
                self._handle_disconnect()
            return []

    def remove_rule(self, rule_id: int) -> bool:
        """Remove a policy rule by ID. Returns True on success."""
        if not self._policy_iface:
            return False
        try:
            self._policy_iface.call_remove_rule_sync(rule_id)
            log.info("Removed rule %d", rule_id)
            return True
        except DBusError as e:
            if _is_permission_error(e):
                log.error("Not authorized to remove rule %d", rule_id)
            else:
                log.error("Failed to remove rule %d: %s", rule_id, e)
                self._handle_disconnect()
            return False

    def _handle_disconnect(self) -> None:
        """Handle a D-Bus disconnection."""
        if self._connected:
            self._connected = False
            self.connection_changed.emit(False)
            log.warning("Lost connection to USBGuard daemon")
