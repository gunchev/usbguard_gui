"""Tests for the device model and rule parser."""

from usbguard_gui.device import (
    Device,
    DeviceTarget,
    interface_class,
    parse_device_rule,
)


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
