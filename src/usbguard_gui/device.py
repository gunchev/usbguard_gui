"""USB device model and rule parsing for USBGuard."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum


class DeviceTarget(IntEnum):
    """USBGuard device authorization target."""

    ALLOW = 0
    BLOCK = 1
    REJECT = 2


class PresenceEvent(IntEnum):
    """USBGuard device presence change event type."""

    PRESENT = 0
    INSERT = 1
    UPDATE = 2
    REMOVE = 3


# USB device class codes → human-readable descriptions.
# See https://www.usb.org/defined-class-codes
USB_CLASS_DESCRIPTIONS: dict[int, str] = {
    0x00: "Device Unspecified",
    0x01: "Audio",
    0x02: "Communications / CDC Control",
    0x03: "Human Interface Device (HID)",
    0x05: "Physical Interface Device",
    0x06: "Image (PTP/MTP)",
    0x07: "Printer",
    0x08: "Mass Storage",
    0x09: "USB Hub",
    0x0A: "CDC Data",
    0x0B: "Smart Card",
    0x0D: "Content Security",
    0x0E: "Video",
    0x0F: "Personal Healthcare",
    0x10: "Audio/Video",
    0x11: "Billboard",
    0x12: "USB Type-C Bridge",
    0xDC: "Diagnostic Device",
    0xE0: "Wireless Controller",
    0xEF: "Miscellaneous",
    0xFE: "Application Specific",
    0xFF: "Vendor Specific",
}


@dataclass
class Device:
    """Represents a USB device as reported by USBGuard."""

    number: int
    rule: str  # "allow", "block", or "reject"
    id: str  # VID:PID e.g. "04f2:b2ea"
    serial: str = ""
    name: str = ""
    hash: str = ""
    parent_hash: str = ""
    via_port: str = ""
    with_interface: list[str] = field(default_factory=list)
    with_connect_type: str = ""

    @classmethod
    def from_dbus(cls, device_id: int, rule_string: str) -> Device:
        """Create a Device from a D-Bus (id, rule_string) tuple."""
        parsed = parse_device_rule(rule_string)
        return cls(number=device_id, **parsed)

    def is_allowed(self) -> bool:
        return self.rule.lower() == "allow"

    def is_blocked(self) -> bool:
        return self.rule.lower() == "block"

    def is_rejected(self) -> bool:
        return self.rule.lower() == "reject"

    def target(self) -> DeviceTarget:
        rule_lower = self.rule.lower()
        if rule_lower == "allow":
            return DeviceTarget.ALLOW
        if rule_lower == "reject":
            return DeviceTarget.REJECT
        return DeviceTarget.BLOCK

    def is_hid(self) -> bool:
        """Return True if all interfaces are HID (class 0x03)."""
        if not self.with_interface:
            return False
        return all(interface_class(iface) == 0x03 for iface in self.with_interface)

    def class_descriptions(self) -> set[str]:
        """Return human-readable descriptions for all interface classes."""
        descriptions = set()
        for iface in self.with_interface:
            cls_code = interface_class(iface)
            descriptions.add(USB_CLASS_DESCRIPTIONS.get(cls_code, f"Unknown (0x{cls_code:02X})"))
        return descriptions

    def class_description_string(self) -> str:
        return ", ".join(sorted(self.class_descriptions()))

    @property
    def vendor_id(self) -> str | None:
        parts = self.id.split(":")
        return parts[0] if len(parts) >= 1 else None

    @property
    def product_id(self) -> str | None:
        parts = self.id.split(":")
        return parts[1] if len(parts) >= 2 else None


def interface_class(interface_str: str) -> int:
    """Extract the base class byte from an interface string like '03:00:01'."""
    return int(interface_str[:2], 16)


# Regex patterns for parsing USBGuard rule strings.
_QUOTED_VALUE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_INTERFACE_BLOCK = re.compile(r"with-interface\s*\{([^}]+)\}")
_INTERFACE_SINGLE = re.compile(r"with-interface\s+([0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2})")


def _extract_field(rule: str, field_name: str) -> str:
    """Extract a quoted or unquoted field value from a rule string."""
    # Try quoted value first: field "value"
    pattern = re.compile(rf'{field_name}\s+"((?:[^"\\]|\\.)*)"')
    m = pattern.search(rule)
    if m:
        return m.group(1)
    # Try unquoted value: field value
    pattern = re.compile(rf"{field_name}\s+(\S+)")
    m = pattern.search(rule)
    if m:
        return m.group(1)
    return ""


def _extract_interfaces(rule: str) -> list[str]:
    """Extract interface list from a rule string."""
    # Multi-interface: with-interface { 0e:01:00 0e:02:00 }
    m = _INTERFACE_BLOCK.search(rule)
    if m:
        return m.group(1).split()
    # Single interface: with-interface 09:00:00
    m = _INTERFACE_SINGLE.search(rule)
    if m:
        return [m.group(1)]
    return []


def parse_device_rule(rule_string: str) -> dict:
    """Parse a USBGuard rule string into a dict suitable for Device construction.

    Example rule:
        allow id 1d6b:0002 serial "0000:00:14.0" name "xHCI Host Controller"
        hash "abc=" parent-hash "def=" via-port "usb1" with-interface 09:00:00
        with-connect-type "hardwired"
    """
    # The first word is the target (allow/block/reject)
    parts = rule_string.strip().split(None, 1)
    rule_target = parts[0] if parts else "block"
    rest = parts[1] if len(parts) > 1 else ""

    return {
        "rule": rule_target,
        "id": _extract_field(rest, "id") or _extract_field(rest, "id"),
        "serial": _extract_field(rest, "serial"),
        "name": _extract_field(rest, "name"),
        "hash": _extract_field(rest, "hash"),
        "parent_hash": _extract_field(rest, "parent-hash"),
        "via_port": _extract_field(rest, "via-port"),
        "with_interface": _extract_interfaces(rest),
        "with_connect_type": _extract_field(rest, "with-connect-type"),
    }
