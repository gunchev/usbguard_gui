"""Tests for the D-Bus client (mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from usbguard_gui.dbus_client import USBGuardClient, _is_permission_error
from usbguard_gui.device import DeviceTarget


@pytest.fixture()
def mock_bus():
    with patch("usbguard_gui.dbus_client.MessageBus") as mock_cls:
        bus_instance = MagicMock()
        mock_cls.return_value = bus_instance
        yield bus_instance


@pytest.fixture()
def client(mock_bus):
    return USBGuardClient()


@pytest.fixture()
def connected_client(client, mock_bus):
    devices_obj = MagicMock()
    policy_obj = MagicMock()
    devices_iface = MagicMock()
    policy_iface = MagicMock()
    devices_obj.get_interface.return_value = devices_iface
    policy_obj.get_interface.return_value = policy_iface
    mock_bus.get_proxy_object.side_effect = [devices_obj, policy_obj]
    client.connect()
    return client, devices_iface, policy_iface


class TestIsPermissionError:
    """Test _is_permission_error function."""

    def test_access_denied_error_name(self):
        from dbus_fast import DBusError, ErrorType

        e = DBusError(ErrorType.ACCESS_DENIED, "test")
        assert _is_permission_error(e) is True

    def test_policykit_error_name(self):
        from dbus_fast import DBusError

        e = DBusError("org.freedesktop.PolicyKit1.Error.NotAuthorized", "test")
        assert _is_permission_error(e) is True

    def test_usbguard_permission_error_name(self):
        from dbus_fast import DBusError

        e = DBusError("org.usbguard.Error.PermissionDenied", "test")
        assert _is_permission_error(e) is True

    def test_not_authorized_in_message(self):
        from dbus_fast import DBusError

        e = DBusError("org.freedesktop.DBus.Error.ServiceUnknown", "Not authorized to perform action")
        assert _is_permission_error(e) is True

    def test_access_denied_in_message(self):
        from dbus_fast import DBusError

        e = DBusError("org.freedesktop.DBus.Error.ServiceUnknown", "AccessDenied error")
        assert _is_permission_error(e) is True

    def test_not_permission_error(self):
        from dbus_fast import DBusError, ErrorType

        e = DBusError(ErrorType.SERVICE_UNKNOWN, "Service unavailable")
        assert _is_permission_error(e) is False


class TestUSBGuardClient:
    def test_connect_success(self, client, mock_bus):
        devices_obj = MagicMock()
        policy_obj = MagicMock()
        mock_bus.get_proxy_object.side_effect = [devices_obj, policy_obj]
        assert client.connect() is True
        assert client.connected is True

    def test_connect_failure(self, client, mock_bus):
        from dbus_fast import DBusError, ErrorType

        mock_bus.connect.side_effect = DBusError(ErrorType.SERVICE_UNKNOWN, "not available")
        assert client.connect() is False
        assert client.connected is False

    def test_connect_emits_signal(self, client, mock_bus):
        devices_obj = MagicMock()
        policy_obj = MagicMock()
        mock_bus.get_proxy_object.side_effect = [devices_obj, policy_obj]
        emitted = []

        def capture(value):
            emitted.append(value)

        client.connection_changed.connect(capture)
        client.connect()
        assert emitted == [True]

    def test_list_devices(self, client, mock_bus):
        devices_obj = MagicMock()
        policy_obj = MagicMock()
        devices_iface = MagicMock()
        devices_obj.get_interface.return_value = devices_iface
        policy_obj.get_interface.return_value = MagicMock()
        mock_bus.get_proxy_object.side_effect = [devices_obj, policy_obj]
        client.connect()

        devices_iface.call_list_devices.return_value = [
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
        client, devices_iface, _ = connected_client
        devices_iface.call_list_devices.return_value = [(1, "allow id 1d6b:0002")]

        devices = client.list_devices(query="match blocked")
        devices_iface.call_list_devices.assert_called_with("match blocked")
        assert len(devices) == 1

    def test_list_devices_dbus_error(self, connected_client):
        from dbus_fast import DBusError, ErrorType

        client, devices_iface, _ = connected_client
        devices_iface.call_list_devices.side_effect = DBusError(ErrorType.SERVICE_UNKNOWN, "Service unavailable")

        result = client.list_devices()
        assert result == []

    def test_list_devices_permission_error_no_disconnect(self, connected_client):
        from dbus_fast import DBusError, ErrorType

        client, devices_iface, _ = connected_client
        e = DBusError(ErrorType.ACCESS_DENIED, "Not authorized")
        devices_iface.call_list_devices.side_effect = e

        result = client.list_devices()
        assert result == []
        assert client.connected is True

    def test_apply_device_policy(self, client, mock_bus):
        devices_obj = MagicMock()
        policy_obj = MagicMock()
        devices_iface = MagicMock()
        devices_obj.get_interface.return_value = devices_iface
        policy_obj.get_interface.return_value = MagicMock()
        mock_bus.get_proxy_object.side_effect = [devices_obj, policy_obj]
        client.connect()

        devices_iface.call_apply_device_policy.return_value = 42
        result = client.apply_device_policy(1, DeviceTarget.ALLOW, permanent=True)
        assert result == 42
        devices_iface.call_apply_device_policy.assert_called_once_with(1, 0, True)

    def test_apply_device_policy_not_connected(self, client):
        assert client.apply_device_policy(1, DeviceTarget.ALLOW) is None

    def test_apply_device_policy_dbus_error(self, connected_client):
        from dbus_fast import DBusError, ErrorType

        client, devices_iface, _ = connected_client
        devices_iface.call_apply_device_policy.side_effect = DBusError(ErrorType.SERVICE_UNKNOWN, "Service unavailable")

        result = client.apply_device_policy(1, DeviceTarget.ALLOW)
        assert result is None
        assert client.connected is False

    def test_apply_device_policy_permission_error(self, connected_client):
        from dbus_fast import DBusError, ErrorType

        client, devices_iface, _ = connected_client
        e = DBusError(ErrorType.ACCESS_DENIED, "Not authorized")
        devices_iface.call_apply_device_policy.side_effect = e

        result = client.apply_device_policy(1, DeviceTarget.ALLOW)
        assert result is None
        assert client.connected is True

    def test_list_rules(self, client, mock_bus):
        devices_obj = MagicMock()
        policy_obj = MagicMock()
        devices_obj.get_interface.return_value = MagicMock()
        policy_iface = MagicMock()
        policy_obj.get_interface.return_value = policy_iface
        mock_bus.get_proxy_object.side_effect = [devices_obj, policy_obj]
        client.connect()

        policy_iface.call_list_rules.return_value = [(1, "allow id 1d6b:0002"), (2, "block id 04f2:b2ea")]
        rules = client.list_rules()
        assert len(rules) == 2
        assert rules[0] == (1, "allow id 1d6b:0002")

    def test_list_rules_not_connected(self, client):
        assert client.list_rules() == []

    def test_list_rules_with_label(self, connected_client):
        client, _, policy_iface = connected_client
        policy_iface.call_list_rules.return_value = [(1, "allow id 1d6b:0002")]

        rules = client.list_rules(label="my-label")
        policy_iface.call_list_rules.assert_called_with("my-label")
        assert len(rules) == 1

    def test_list_rules_dbus_error(self, connected_client):
        from dbus_fast import DBusError, ErrorType

        client, _, policy_iface = connected_client
        policy_iface.call_list_rules.side_effect = DBusError(ErrorType.SERVICE_UNKNOWN, "Service unavailable")

        result = client.list_rules()
        assert result == []
        assert client.connected is False

    def test_remove_rule(self, connected_client):
        client, _, policy_iface = connected_client
        policy_iface.call_remove_rule.return_value = None

        result = client.remove_rule(1)
        assert result is True
        policy_iface.call_remove_rule.assert_called_once_with(1)

    def test_remove_rule_not_connected(self, client):
        assert client.remove_rule(1) is False

    def test_remove_rule_dbus_error(self, connected_client):
        from dbus_fast import DBusError, ErrorType

        client, _, policy_iface = connected_client
        policy_iface.call_remove_rule.side_effect = DBusError(ErrorType.SERVICE_UNKNOWN, "Service unavailable")

        result = client.remove_rule(1)
        assert result is False
        assert client.connected is False

    def test_remove_rule_permission_error(self, connected_client):
        from dbus_fast import DBusError, ErrorType

        client, _, policy_iface = connected_client
        e = DBusError(ErrorType.ACCESS_DENIED, "Not authorized")
        policy_iface.call_remove_rule.side_effect = e

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
        _, devices_iface, _ = connected_client
        assert devices_iface.on_device_presence_changed.called
        assert devices_iface.on_device_policy_changed.called

    def test_unsubscribe_signals_on_reconnect(self, connected_client):
        client, devices_iface, _ = connected_client
        devices_iface.off_device_presence_changed.reset_mock()
        devices_iface.off_device_policy_changed.reset_mock()

        mock_bus = client._bus
        devices_obj = MagicMock()
        policy_obj = MagicMock()
        devices_obj.get_interface.return_value = devices_iface
        policy_obj.get_interface.return_value = MagicMock()
        mock_bus.get_proxy_object.side_effect = [devices_obj, policy_obj]

        client.connect()
        assert devices_iface.off_device_presence_changed.called
        assert devices_iface.off_device_policy_changed.called

    def test_subscribe_signals_no_proxy(self, client):
        client._devices_iface = None
        client._disconnect()

    def test_unsubscribe_signals_exception(self, connected_client):
        client, devices_iface, _ = connected_client
        devices_iface.off_device_presence_changed.side_effect = Exception("fail")
        devices_iface.off_device_policy_changed.side_effect = Exception("fail")
        client._disconnect()
