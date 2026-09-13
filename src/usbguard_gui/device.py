"""USB device model and rule parsing for USBGuard."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TypedDict


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
    # The exact rule string USBGuard reported, kept so a permanent decision can
    # append it verbatim instead of letting USBGuard regenerate (and upsert)
    # its own.  Carried data, not identity — excluded from equality and repr.
    raw_rule: str = field(default="", repr=False, compare=False)

    @classmethod
    def from_dbus(cls, device_id: int, rule_string: str) -> Device:
        """Create a Device from a D-Bus (id, rule_string) tuple."""
        return cls(number=device_id, raw_rule=rule_string, **parse_device_rule(rule_string))

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

    def has_hid_interface(self) -> bool:
        """Return True if any interface is HID (class 0x03).

        Use this for security decisions: a composite device (e.g. HID + MSC)
        can send keystrokes and must be treated as a HID device.
        """
        return any(interface_class(iface) == 0x03 for iface in self.with_interface)

    def class_descriptions(self) -> set[str]:
        """Return human-readable descriptions for all interface classes."""
        descriptions = set()
        for iface in self.with_interface:
            cls_code = interface_class(iface)
            descriptions.add(USB_CLASS_DESCRIPTIONS.get(cls_code, f"Unknown (0x{cls_code:02X})"))
        return descriptions

    def class_description_string(self) -> str:
        return ", ".join(sorted(self.class_descriptions()))

    # Not currently called from application code beyond their own test —
    # kept intentionally as public accessors for vendor/product id (e.g. for
    # a future device-list column or USB-ID-database lookups), not dead code.
    @property
    def vendor_id(self) -> str | None:
        parts = self.id.split(":")
        return parts[0] if parts[0] else None

    @property
    def product_id(self) -> str | None:
        parts = self.id.split(":")
        return parts[1] if len(parts) >= 2 else None


def interface_class(interface_str: str) -> int:
    """Extract the base class byte from an interface string like '03:00:01'."""
    if len(interface_str) < 2:
        return 0
    try:
        return int(interface_str[:2], 16)
    except ValueError:
        return 0


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


# ---------------------------------------------------------------------------
# Rule matching
#
# USBGuard evaluates its rules top-down and stops at the first match, so a
# rule's position only matters relative to the rules above it.  Answering
# "would this rule match that device?" is what lets the permanent path place
# a rule where it cannot be silently shadowed.
#
# Every rule attribute is a multiset compared against the device's attribute
# set with one of these operators (usbguard::Rule::SetOperator).  A rule
# that spells none uses `equals`.
# ---------------------------------------------------------------------------
SET_OPERATORS: frozenset[str] = frozenset(
    {"all-of", "one-of", "none-of", "equals", "equals-ordered", "match-all"}
)
_DEFAULT_SET_OPERATOR = "equals"

# A rule token is a quoted string, a brace, or a bare word.
_RULE_TOKEN_RE = re.compile(r'"(?:\\.|[^"\\])*"|\{|\}|[^\s{}]+')

# Rule attribute -> the Device field holding the device's value for it.
# Anything outside this table -- `label`, `if` conditions, operators we do
# not model -- is deliberately *not* evaluated.  Guessing about a predicate
# we cannot read is how a user's decision ends up ranked above an
# administrator's policy by accident, so such rules are reported as
# undecidable instead.
_MATCHABLE_ATTRS: dict[str, str] = {
    "id": "id",
    "serial": "serial",
    "name": "name",
    "hash": "hash",
    "parent-hash": "parent_hash",
    "via-port": "via_port",
    "with-connect-type": "with_connect_type",
    "with-interface": "with_interface",
}


def _unquote(token: str) -> str:
    """Strip the surrounding quotes from a rule token, if it has any."""
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return token[1:-1]
    return token


def parse_rule_predicates(rule: str) -> list[tuple[str, str, list[str]]] | None:
    """Split a rule into ``(attribute, set_operator, values)`` triples.

    ``None`` means the rule could not be tokenised, which keeps "carries no
    predicates at all" -- a bare ``allow``, matching every device -- apart
    from "unreadable".  The two must not be confused: the first is a real
    and very broad match, the second is no verdict at all.
    """
    tokens = _RULE_TOKEN_RE.findall(rule.strip())
    if not tokens:
        return None

    predicates: list[tuple[str, str, list[str]]] = []
    index = 1  # token 0 is the target verb
    while index < len(tokens):
        attribute = tokens[index]
        index += 1
        operator = _DEFAULT_SET_OPERATOR
        if index < len(tokens) and tokens[index] in SET_OPERATORS:
            operator = tokens[index]
            index += 1
        values: list[str] = []
        if index < len(tokens) and tokens[index] == "{":
            index += 1
            while index < len(tokens) and tokens[index] != "}":
                values.append(_unquote(tokens[index]))
                index += 1
            if index >= len(tokens):
                return None  # unterminated set
            index += 1  # consume the closing brace
        elif index < len(tokens):
            values.append(_unquote(tokens[index]))
            index += 1
        else:
            return None  # attribute with no value
        predicates.append((attribute, operator, values))
    return predicates


def _interface_spec_matches(spec: str, actual: str) -> bool | None:
    """Match one interface spec against a concrete class/subclass/protocol.

    Any byte of a spec may be ``*``, so ``03:01:*`` covers ``03:01:00`` and
    ``03:01:01``.  A side that is not a three-part spec is left undecided
    rather than guessed at.
    """
    spec_parts, actual_parts = spec.split(":"), actual.split(":")
    if len(spec_parts) != 3 or len(actual_parts) != 3:
        return None
    return all(expected == "*" or expected == found
               for expected, found in zip(spec_parts, actual_parts, strict=True))


def _exact_match(expected: str, found: str) -> bool | None:
    return expected == found


def _matched_by_any(expected: str, candidates: list[str], compare) -> bool | None:
    """Does `expected` match at least one candidate?

    None when nothing matched outright but some comparison could not be
    decided -- which is not the same as having failed to match.
    """
    undecided = False
    for candidate in candidates:
        verdict = compare(expected, candidate)
        if verdict is True:
            return True
        if verdict is None:
            undecided = True
    return None if undecided else False


def _combine_any(verdicts: list[bool | None]) -> bool | None:
    if True in verdicts:
        return True
    return None if None in verdicts else False


def _combine_all(verdicts: list[bool | None]) -> bool | None:
    if False in verdicts:
        return False
    return None if None in verdicts else True


def _combine_none(verdicts: list[bool | None]) -> bool | None:
    if True in verdicts:
        return False
    return None if None in verdicts else True


def _set_matches(operator: str, rule_values: list[str], device_values: list[str], compare) -> bool | None:
    """Apply one set operator. None means it could not be decided."""
    if operator == "one-of":
        return _combine_any([_matched_by_any(value, device_values, compare) for value in rule_values])
    if operator == "all-of":
        return _combine_all([_matched_by_any(value, device_values, compare) for value in rule_values])
    if operator == "none-of":
        return _combine_none([_matched_by_any(value, device_values, compare) for value in rule_values])
    if operator == "match-all":
        # Every interface the device *has* must be covered by the rule.
        return _combine_all([_matched_by_any(found, rule_values, compare) for found in device_values])
    if operator == "equals-ordered":
        if len(rule_values) != len(device_values):
            return False
        return _combine_all([compare(expected, found)
                             for expected, found in zip(rule_values, device_values, strict=True)])
    if operator == "equals":
        if len(rule_values) != len(device_values):
            return False
        if any("*" in value for value in rule_values):
            # Wildcards make the pairing ambiguous: which device value each
            # spec stands for is not knowable from the rule alone.
            return None
        return sorted(rule_values) == sorted(device_values)
    return None


def rule_persistence_problem(rule: str) -> str | None:
    """Why `rule` must not be written to the permanent policy, or None if it may.

    ``raw_rule`` arrives from the daemon, but it is ultimately built from a
    device's own descriptors, and this app writes it verbatim into
    ``/etc/usbguard/rules.conf``. Before that happens the string has to look
    like what it claims to be: one rule, with a target verb in front and
    nothing in it but device attributes.

    Without this, a string that carries a second rule, a stray directive or a
    newline is persisted on the strength of having had its first word
    rewritten. Probed against the retarget helper::

        in:  block id 1234:5678 name "hub" block with-interface { 03:01:01 }" serial "x" reject
        out: allow id 1234:5678 name "hub" block with-interface { 03:01:01 }" serial "x" reject

    Only the leading verb changed; the rest passed through untouched. The
    daemon escapes quotes on the way out, so this is not a proven exploit --
    it is the cheap check that makes it someone else's problem to ever prove.
    """
    if any(ch in rule for ch in ("\n", "\r", "\x00")):
        return "contains a control character"

    parts = rule.strip().split(None, 1)
    if not parts or parts[0] not in {"allow", "block", "reject"}:
        return "does not start with a target verb"

    predicates = parse_rule_predicates(rule)
    if predicates is None:
        return "does not parse as a single rule"

    unknown = sorted({attribute for attribute, _, _ in predicates if attribute not in _MATCHABLE_ATTRS})
    if unknown:
        return f"carries directives that are not device attributes: {', '.join(unknown)}"

    return None


def rule_matches_device(rule: str, device: Device) -> bool | None:
    """Would `rule` match `device`?

    ``True`` -- it matches.  ``False`` -- it cannot: some predicate the rule
    requires is not satisfied by this device.  ``None`` -- we cannot tell,
    because the rule uses an attribute or operator outside what this matcher
    models.

    The three-way answer is the point.  A caller choosing where to place a
    rule must treat "does not match" and "unknown" differently: promoting a
    user's decision above a rule we merely failed to read would override an
    administrator on nothing better than a parsing gap.
    """
    predicates = parse_rule_predicates(rule)
    if predicates is None:
        return None

    for attribute, operator, values in predicates:
        field = _MATCHABLE_ATTRS.get(attribute)
        if field is None:
            return None
        device_value = getattr(device, field)
        device_values = list(device_value) if isinstance(device_value, list) else [device_value]
        compare = _interface_spec_matches if attribute == "with-interface" else _exact_match
        verdict = _set_matches(operator, values, device_values, compare)
        if verdict is not True:
            return verdict
    return True


class _ParsedRule(TypedDict):
    rule: str
    id: str
    serial: str
    name: str
    hash: str
    parent_hash: str
    via_port: str
    with_interface: list[str]
    with_connect_type: str


def parse_device_rule(rule_string: str) -> _ParsedRule:
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
        "id": _extract_field(rest, "id"),
        "serial": _extract_field(rest, "serial"),
        "name": _extract_field(rest, "name"),
        "hash": _extract_field(rest, "hash"),
        "parent_hash": _extract_field(rest, "parent-hash"),
        "via_port": _extract_field(rest, "via-port"),
        "with_interface": _extract_interfaces(rest),
        "with_connect_type": _extract_field(rest, "with-connect-type"),
    }
