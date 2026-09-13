"""Tests for the device model and rule parser."""

import pytest

from usbguard_gui.device import Device, DeviceTarget, interface_class, parse_device_rule, parse_rule_predicates, \
    rule_matches_device, rule_persistence_problem


class TestParseDeviceRule:
    """Test rule string parsing."""

    RULE_ALLOW = (
        'allow id 1d6b:0002 serial "0000:00:14.0" name "xHCI Host Controller"'
        ' hash "Miigb8mx72Z0q6L+YMai0mDZSlYC8qiSMctoUjByF2o="'
        ' parent-hash "G1ehGQdrl3dJ9HvW9w2HdC//pk87pKzFE1WY25bq8k4="'
        ' via-port "usb1" with-interface 09:00:00 with-connect-type "hardwired"'
    )

    RULE_BLOCK_MULTI_IFACE = (
        'block id 04f2:b2ea serial "" name "Integrated Camera"'
        ' hash "18xYrZpFsIyYEyw3SqedfmQFkrnVcPmbyLZIVLeFPPs="'
        " with-interface { 0e:01:00 0e:02:00 0e:02:00 }"
    )

    RULE_REJECT = 'reject id 1234:5678 name "Bad Device" with-interface 03:01:01'

    def test_allow_rule(self):
        result = parse_device_rule(self.RULE_ALLOW)
        assert result["rule"] == "allow"
        assert result["id"] == "1d6b:0002"
        assert result["serial"] == "0000:00:14.0"
        assert result["name"] == "xHCI Host Controller"
        assert result["hash"] == "Miigb8mx72Z0q6L+YMai0mDZSlYC8qiSMctoUjByF2o="
        assert result["parent_hash"] == "G1ehGQdrl3dJ9HvW9w2HdC//pk87pKzFE1WY25bq8k4="
        assert result["via_port"] == "usb1"
        assert result["with_interface"] == ["09:00:00"]
        assert result["with_connect_type"] == "hardwired"

    def test_block_multi_interface(self):
        result = parse_device_rule(self.RULE_BLOCK_MULTI_IFACE)
        assert result["rule"] == "block"
        assert result["id"] == "04f2:b2ea"
        assert result["name"] == "Integrated Camera"
        assert result["serial"] == ""
        assert result["with_interface"] == ["0e:01:00", "0e:02:00", "0e:02:00"]

    def test_reject_rule(self):
        result = parse_device_rule(self.RULE_REJECT)
        assert result["rule"] == "reject"
        assert result["id"] == "1234:5678"
        assert result["name"] == "Bad Device"
        assert result["with_interface"] == ["03:01:01"]


class TestDevice:
    """Test Device dataclass methods."""

    _SENTINEL = object()

    def _make_device(self, rule: str = "block", interfaces: list[str] | object = _SENTINEL) -> Device:
        if interfaces is self._SENTINEL:
            interfaces = ["03:00:01"]
        return Device(
            number=1,
            rule=rule,
            id="04f2:b2ea",
            serial="",
            name="Test Device",
            hash="abc=",
            parent_hash="def=",
            via_port="usb1",
            with_interface=interfaces,
            with_connect_type="hotplug",
        )

    def test_is_allowed(self):
        assert self._make_device("allow").is_allowed()
        assert not self._make_device("block").is_allowed()

    def test_is_blocked(self):
        assert self._make_device("block").is_blocked()
        assert not self._make_device("allow").is_blocked()

    def test_is_rejected(self):
        assert self._make_device("reject").is_rejected()

    def test_target(self):
        assert self._make_device("allow").target() == DeviceTarget.ALLOW
        assert self._make_device("block").target() == DeviceTarget.BLOCK
        assert self._make_device("reject").target() == DeviceTarget.REJECT

    def test_is_hid_single_hid_interface(self):
        device = self._make_device(interfaces=["03:00:01"])
        assert device.is_hid()

    def test_is_hid_multiple_hid_interfaces(self):
        device = self._make_device(interfaces=["03:00:01", "03:01:01"])
        assert device.is_hid()

    def test_is_hid_mixed_interfaces(self):
        device = self._make_device(interfaces=["03:00:01", "08:06:50"])
        assert not device.is_hid()

    def test_is_hid_no_interfaces(self):
        device = self._make_device(interfaces=[])
        assert not device.is_hid()

    def test_has_hid_interface_pure_hid(self):
        device = self._make_device(interfaces=["03:00:01"])
        assert device.has_hid_interface()

    def test_has_hid_interface_composite_hid_msc(self):
        # Composite device: HID + Mass Storage — must trigger HID security path
        device = self._make_device(interfaces=["03:00:01", "08:06:50"])
        assert device.has_hid_interface()

    def test_has_hid_interface_non_hid(self):
        device = self._make_device(interfaces=["08:06:50"])
        assert not device.has_hid_interface()

    def test_has_hid_interface_no_interfaces(self):
        device = self._make_device(interfaces=[])
        assert not device.has_hid_interface()

    def test_class_descriptions(self):
        device = self._make_device(interfaces=["03:00:01", "08:06:50"])
        descs = device.class_descriptions()
        assert "Human Interface Device (HID)" in descs
        assert "Mass Storage" in descs

    def test_class_description_string(self):
        device = self._make_device(interfaces=["09:00:00"])
        assert device.class_description_string() == "USB Hub"

    def test_vendor_product_id(self):
        device = self._make_device()
        assert device.vendor_id == "04f2"
        assert device.product_id == "b2ea"

    def test_from_dbus(self):
        rule_str = (
            'block id 04f2:b2ea serial "" name "Integrated Camera"'
            ' hash "abc=" parent-hash "def="'
            ' via-port "usb1" with-interface { 0e:01:00 0e:02:00 }'
            ' with-connect-type "hotplug"'
        )
        device = Device.from_dbus(5, rule_str)
        assert device.number == 5
        assert device.rule == "block"
        assert device.id == "04f2:b2ea"
        assert device.name == "Integrated Camera"
        assert len(device.with_interface) == 2


class TestInterfaceClass:
    def test_hid_class(self):
        assert interface_class("03:00:01") == 0x03

    def test_mass_storage_class(self):
        assert interface_class("08:06:50") == 0x08

    def test_hub_class(self):
        assert interface_class("09:00:00") == 0x09


class TestRuleMatchesDevice:
    """Would one rule match the device another rule describes?

    The permanent path places its rule by asking this of every rule sitting
    above it, so the three verdicts have to mean what they say: True means
    "that rule already covers this device" and moves the new rule above it,
    False means the rule provably cannot match, and None means we do not
    know -- which must never be read as True, or a parsing gap becomes a
    user decision ranked above an administrator's policy.
    """

    DEVICE_RULE = ('allow id 2109:2817 serial "000000000" name "USB2.0 Hub" '
                   'hash "kj7MUN8qdDfj2pO0aUpZ2tOY7UIlzSNGG7bI9jnAeu4=" '
                   'parent-hash "xe96rjr8V53Jw+g7q/yi0C1czVxatehiq7r4gn2dH6s=" '
                   'via-port "3-3.1.1" with-interface { 09:00:01 09:00:02 } '
                   'with-connect-type "unknown"')

    def _device(self):
        return Device.from_dbus(54, self.DEVICE_RULE)

    def _verdict(self, rule):
        return rule_matches_device(rule, self._device())

    @pytest.mark.parametrize("rule", [
        "block",                                              # no predicates: matches everything
        "reject",
        'block id 2109:2817',
        'block id 2109:2817 serial "000000000"',
        'block via-port "3-3.1.1"',
        'block with-connect-type "unknown"',
        'block with-interface all-of { 09:00:01 }',
        'block with-interface all-of { 09:*:* }',              # wildcard byte
        'block with-interface one-of { 08:00:00 09:00:02 }',
        'block with-interface none-of { 08:00:00 }',          # device has none of those
        'block with-interface match-all { 09:00:01 09:00:02 }',
        # The device's own rule, retargeted: every predicate is satisfied.
        'block id 2109:2817 serial "000000000" name "USB2.0 Hub" '
        'hash "kj7MUN8qdDfj2pO0aUpZ2tOY7UIlzSNGG7bI9jnAeu4=" '
        'parent-hash "xe96rjr8V53Jw+g7q/yi0C1czVxatehiq7r4gn2dH6s=" '
        'via-port "3-3.1.1" with-interface { 09:00:01 09:00:02 } with-connect-type "unknown"',
    ])
    def test_matches(self, rule):
        assert self._verdict(rule) is True, rule

    @pytest.mark.parametrize("rule", [
        'block id 04f2:b2ea',
        'block serial "someone-elses-drive"',
        'block via-port "usb1"',
        'block with-connect-type "hardwired"',
        'block with-interface { 08:00:00 }',                  # equals: wrong size and values
        'block with-interface all-of { 08:00:00 }',
        'block with-interface equals-ordered { 09:00:02 09:00:01 }',
        'block with-interface match-all { 09:00:01 }',        # 09:00:02 left uncovered
        'block with-interface none-of { 09:00:01 }',          # device does have it
        'block id 04f2:b2ea with-interface all-of { 09:00:01 }',
    ])
    def test_cannot_match(self, rule):
        assert self._verdict(rule) is False, rule

    @pytest.mark.parametrize("rule", [
        'allow label "trusted-lab-machine"',                   # attribute we do not model
        'allow if usbguard-condition("admin")',
        'block name-hash "abc"',
        'block with-interface equals { 09:*:* 09:00:02 }',     # wildcard pairing is ambiguous
        'allow if admin-only("yes") id 2109:2817',           # unreadable predicate *before* a matching one
    ])
    def test_undecidable(self, rule):
        assert self._verdict(rule) is None, rule

    def test_a_predicate_we_cannot_read_never_becomes_a_match(self):
        """The undecidable verdict must not collapse into True."""
        assert self._verdict('allow label "x" id 2109:2817') is None

    def test_undecidable_predicate_after_a_non_match_still_says_no(self):
        """A rule that already failed to match stays failed; the unreadable
        predicate later in the rule cannot rescue it."""
        assert self._verdict('block id 04f2:b2ea label "x"') is False


class TestParseRulePredicates:
    """Tokenising a rule into (attribute, set_operator, values)."""

    def test_bare_target_carries_no_predicates(self):
        assert parse_rule_predicates("block") == []

    def test_default_operator_is_equals(self):
        assert parse_rule_predicates('allow id 1234:5678') == [("id", "equals", ["1234:5678"])]

    def test_explicit_set_operator(self):
        assert parse_rule_predicates('block with-interface all-of { 03:00:00 03:01:01 }') == [
            ("with-interface", "all-of", ["03:00:00", "03:01:01"])
        ]

    def test_quoted_values_keep_spaces(self):
        assert parse_rule_predicates('allow name "USB 2.0 Hub"') == [("name", "equals", ["USB 2.0 Hub"])]

    def test_several_predicates_in_order(self):
        assert parse_rule_predicates('block id 1234:5678 serial "s1"') == [
            ("id", "equals", ["1234:5678"]),
            ("serial", "equals", ["s1"]),
        ]

    @pytest.mark.parametrize("rule", ["", "   ", 'block with-interface all-of { 03:00:00'])
    def test_unreadable_rule_is_none_not_an_empty_match(self, rule):
        """Empty and unreadable are different things: the first matches
        everything, the second says nothing."""
        assert parse_rule_predicates(rule) is None


class TestRulePersistenceValidation:
    """A device-derived rule must look like one rule before it is persisted."""

    @pytest.mark.parametrize("rule,reason", [
        ('allow id 2109:2817 serial "s" name "n" with-interface { 09:00:01 }', None),
        ('block id 2109:2817', None),
        ('reject id 2109:2817 with-connect-type "unknown"', None),
        ('allow id 1234:5678 name "hub" block with-interface { 03:01:01 }" serial "x" reject',
         "carries directives that are not device attributes"),
        ('allow id 1:2\nreject with-interface { 03:00:00 }', "contains a control character"),
        ('allow id 1:2\rblock', "contains a control character"),
        ('id 1:2', "does not start with a target verb"),
        ('allow with-interface all-of { 03:00:00', "does not parse as a single rule"),
        ('allow label "trusted"', "carries directives that are not device attributes"),
    ])
    def test_reasons(self, rule, reason):
        result = rule_persistence_problem(rule)
        if reason is None:
            assert result is None, rule
        else:
            assert result is not None and result.startswith(reason), (rule, result)

    def test_a_bare_verb_is_a_whole_rule(self):
        assert rule_persistence_problem("block") is None
