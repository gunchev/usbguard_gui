"""Tests for the D-Bus client (mocked)."""

from unittest.mock import MagicMock, patch

import pytest

from usbguard_gui.dbus_client import USBGuardClient
from usbguard_gui.device import DeviceTarget


@pytest.fixture()
def mock_bus():
    with patch("usbguard_gui.dbus_client.SystemMessageBus") as mock_cls:
        bus_instance = MagicMock()
        mock_cls.return_value = bus_instance
        yield bus_instance


@pytest.fixture()
def client(mock_bus):
    return USBGuardClient()


class TestUSBGuardClient:
    def test_connect_success(self, client, mock_bus):
        mock_bus.get_proxy.return_value = MagicMock()
        assert client.connect() is True
        assert client.connected is True

    def test_connect_failure(self, client, mock_bus):
        from dasbus.error import DBusError

        mock_bus.get_proxy.side_effect = DBusError("not available")
        assert client.connect() is False
        assert client.connected is False

    def test_list_devices(self, client, mock_bus):
        devices_proxy = MagicMock()
        policy_proxy = MagicMock()
        mock_bus.get_proxy.side_effect = [devices_proxy, policy_proxy]
        client.connect()

        devices_proxy.listDevices.return_value = [
            (1, 'allow id 1d6b:0002 serial "0000:00:14.0" name "xHCI" with-interface 09:00:00'),
            (2, 'block id 04f2:b2ea name "Camera" with-interface 0e:01:00'),
        ]
        devices = client.list_devices()
        assert len(devices) == 2
        assert devices[0].number == 1
        assert devices[0].is_allowed()
        assert devices[1].number == 2
        assert devices[1].is_blocked()

    def test_apply_device_policy(self, client, mock_bus):
        devices_proxy = MagicMock()
        policy_proxy = MagicMock()
        mock_bus.get_proxy.side_effect = [devices_proxy, policy_proxy]
        client.connect()

        devices_proxy.applyDevicePolicy.return_value = 42
        result = client.apply_device_policy(1, DeviceTarget.ALLOW, permanent=True)
        assert result == 42
        devices_proxy.applyDevicePolicy.assert_called_once_with(1, 0, True)

    def test_list_rules(self, client, mock_bus):
        devices_proxy = MagicMock()
        policy_proxy = MagicMock()
        mock_bus.get_proxy.side_effect = [devices_proxy, policy_proxy]
        client.connect()

        policy_proxy.listRules.return_value = [
            (1, "allow id 1d6b:0002"),
            (2, "block id 04f2:b2ea"),
        ]
        rules = client.list_rules()
        assert len(rules) == 2
        assert rules[0] == (1, "allow id 1d6b:0002")

    def test_list_devices_not_connected(self, client):
        assert client.list_devices() == []

    def test_apply_policy_not_connected(self, client):
        assert client.apply_device_policy(1, DeviceTarget.ALLOW) is None
