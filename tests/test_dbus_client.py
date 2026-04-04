"""Tests for the D-Bus client (mocked)."""

from unittest.mock import MagicMock, patch

import pytest

from usbguard_gui.dbus_client import USBGuardClient, _is_permission_error
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


@pytest.fixture()
def connected_client(client, mock_bus):
    devices_proxy = MagicMock()
    policy_proxy = MagicMock()
    mock_bus.get_proxy.side_effect = [devices_proxy, policy_proxy]
    client.connect()
    return client, devices_proxy, policy_proxy


class TestIsPermissionError:
    """Test _is_permission_error function."""

    def test_access_denied_error_name(self):
        from dasbus.error import DBusError

        e = DBusError("test")
        e.error_name = "org.freedesktop.DBus.Error.AccessDenied"
        assert _is_permission_error(e) is True

    def test_policykit_error_name(self):
        from dasbus.error import DBusError

        e = DBusError("test")
        e.error_name = "org.freedesktop.PolicyKit1.Error.NotAuthorized"
        assert _is_permission_error(e) is True

    def test_usbguard_permission_error_name(self):
        from dasbus.error import DBusError

        e = DBusError("test")
        e.error_name = "org.usbguard.Error.PermissionDenied"
        assert _is_permission_error(e) is True

    def test_not_authorized_in_message(self):
        from dasbus.error import DBusError

        e = DBusError("Not authorized to perform action")
        e.error_name = ""
        assert _is_permission_error(e) is True

    def test_access_denied_in_message(self):
        from dasbus.error import DBusError

        e = DBusError("AccessDenied error")
        e.error_name = ""
        assert _is_permission_error(e) is True

    def test_not_permission_error(self):
        from dasbus.error import DBusError

        e = DBusError("Service unavailable")
        e.error_name = "org.freedesktop.DBus.Error.ServiceUnknown"
        assert _is_permission_error(e) is False


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

    def test_connect_emits_signal(self, client, mock_bus):
        mock_bus.get_proxy.return_value = MagicMock()
        emitted = []

        def capture(value):
            emitted.append(value)

        client.connection_changed.connect(capture)
        client.connect()
        assert emitted == [True]

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

    def test_list_devices_with_custom_query(self, connected_client):
        client, devices_proxy, _ = connected_client
        devices_proxy.listDevices.return_value = [(1, "allow id 1d6b:0002")]

        devices = client.list_devices(query="match blocked")
        devices_proxy.listDevices.assert_called_with("match blocked")
        assert len(devices) == 1

    def test_list_devices_dbus_error(self, connected_client):
        from dasbus.error import DBusError

        client, devices_proxy, _ = connected_client
        devices_proxy.listDevices.side_effect = DBusError("Service unavailable")

        result = client.list_devices()
        assert result == []

    def test_list_devices_permission_error_no_disconnect(self, connected_client):
        from dasbus.error import DBusError

        client, devices_proxy, _ = connected_client
        e = DBusError("Not authorized")
        e.error_name = "org.freedesktop.DBus.Error.AccessDenied"
        devices_proxy.listDevices.side_effect = e

        result = client.list_devices()
        assert result == []
        assert client.connected is True

    def test_apply_device_policy(self, client, mock_bus):
        devices_proxy = MagicMock()
        policy_proxy = MagicMock()
        mock_bus.get_proxy.side_effect = [devices_proxy, policy_proxy]
        client.connect()

        devices_proxy.applyDevicePolicy.return_value = 42
        result = client.apply_device_policy(1, DeviceTarget.ALLOW, permanent=True)
        assert result == 42
        devices_proxy.applyDevicePolicy.assert_called_once_with(1, 0, True)

    def test_apply_device_policy_not_connected(self, client):
        assert client.apply_device_policy(1, DeviceTarget.ALLOW) is None

    def test_apply_device_policy_dbus_error(self, connected_client):
        from dasbus.error import DBusError

        client, devices_proxy, _ = connected_client
        devices_proxy.applyDevicePolicy.side_effect = DBusError("Service unavailable")

        result = client.apply_device_policy(1, DeviceTarget.ALLOW)
        assert result is None
        assert client.connected is False

    def test_apply_device_policy_permission_error(self, connected_client):
        from dasbus.error import DBusError

        client, devices_proxy, _ = connected_client
        e = DBusError("Not authorized")
        e.error_name = "org.freedesktop.DBus.Error.AccessDenied"
        devices_proxy.applyDevicePolicy.side_effect = e

        result = client.apply_device_policy(1, DeviceTarget.ALLOW)
        assert result is None
        assert client.connected is True

    def test_list_rules(self, client, mock_bus):
        devices_proxy = MagicMock()
        policy_proxy = MagicMock()
        mock_bus.get_proxy.side_effect = [devices_proxy, policy_proxy]
        client.connect()

        policy_proxy.listRules.return_value = [(1, "allow id 1d6b:0002"), (2, "block id 04f2:b2ea")]
        rules = client.list_rules()
        assert len(rules) == 2
        assert rules[0] == (1, "allow id 1d6b:0002")

    def test_list_rules_not_connected(self, client):
        assert client.list_rules() == []

    def test_list_rules_with_label(self, connected_client):
        client, _, policy_proxy = connected_client
        policy_proxy.listRules.return_value = [(1, "allow id 1d6b:0002")]

        rules = client.list_rules(label="my-label")
        policy_proxy.listRules.assert_called_with("my-label")
        assert len(rules) == 1

    def test_list_rules_dbus_error(self, connected_client):
        from dasbus.error import DBusError

        client, _, policy_proxy = connected_client
        policy_proxy.listRules.side_effect = DBusError("Service unavailable")

        result = client.list_rules()
        assert result == []
        assert client.connected is False

    def test_remove_rule(self, connected_client):
        client, _, policy_proxy = connected_client
        policy_proxy.removeRule.return_value = None

        result = client.remove_rule(1)
        assert result is True
        policy_proxy.removeRule.assert_called_once_with(1)

    def test_remove_rule_not_connected(self, client):
        assert client.remove_rule(1) is False

    def test_remove_rule_dbus_error(self, connected_client):
        from dasbus.error import DBusError

        client, _, policy_proxy = connected_client
        policy_proxy.removeRule.side_effect = DBusError("Service unavailable")

        result = client.remove_rule(1)
        assert result is False
        assert client.connected is False

    def test_remove_rule_permission_error(self, connected_client):
        from dasbus.error import DBusError

        client, _, policy_proxy = connected_client
        e = DBusError("Not authorized")
        e.error_name = "org.freedesktop.DBus.Error.AccessDenied"
        policy_proxy.removeRule.side_effect = e

        result = client.remove_rule(1)
        assert result is False
        assert client.connected is True

    def test_list_devices_not_connected(self, client):
        assert client.list_devices() == []

    def test_apply_policy_not_connected(self, client):
        assert client.apply_device_policy(1, DeviceTarget.ALLOW) is None

    def test_handle_disconnect_emits_signal(self, connected_client):
        client, _, _ = connected_client
        emitted = []

        def capture(value):
            emitted.append(value)

        client.connection_changed.connect(capture)
        client._handle_disconnect()
        assert emitted == [False]
        assert client.connected is False

    def test_handle_disconnect_already_disconnected(self, client):
        emitted = []

        def capture(value):
            emitted.append(value)

        client.connection_changed.connect(capture)
        client._handle_disconnect()
        assert emitted == []
        assert client.connected is False

    def test_on_device_presence_changed(self, client):
        emitted = []

        def capture(*args):
            emitted.append(args)

        client.device_presence_changed.connect(capture)
        client._on_device_presence_changed(1, 2, 3, "rule", {"key": "value"})
        assert emitted == [(1, 2, 3, "rule", {"key": "value"})]

    def test_on_device_policy_changed(self, client):
        emitted = []

        def capture(*args):
            emitted.append(args)

        client.device_policy_changed.connect(capture)
        client._on_device_policy_changed(1, 2, 3, "rule", 4, {"key": "value"})
        assert emitted == [(1, 2, 3, "rule", 4, {"key": "value"})]

    def test_subscribe_signals(self, connected_client):
        _, devices_proxy, _ = connected_client
        assert devices_proxy.DevicePresenceChanged.connect.called
        assert devices_proxy.DevicePolicyChanged.connect.called

    def test_unsubscribe_signals_on_reconnect(self, connected_client):
        client, devices_proxy, _ = connected_client
        devices_proxy.DevicePresenceChanged.reset_mock()
        devices_proxy.DevicePolicyChanged.reset_mock()

        mock_bus = client._bus
        mock_bus.get_proxy.side_effect = [MagicMock(), MagicMock()]

        client.connect()
        assert devices_proxy.DevicePresenceChanged.disconnect.called
        assert devices_proxy.DevicePolicyChanged.disconnect.called

    def test_subscribe_signals_no_proxy(self, client):
        client._devices_proxy = None
        client._subscribe_signals()

    def test_unsubscribe_signals_exception(self, connected_client):
        client, devices_proxy, _ = connected_client
        devices_proxy.DevicePresenceChanged.disconnect.side_effect = Exception("fail")
        client._unsubscribe_signals()
