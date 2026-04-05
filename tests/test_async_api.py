"""Tests for the async signal-based D-Bus client API.

These tests verify that the new async signal-based API is correctly implemented
and that client methods return None (fire-and-forget) while results come via signals.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from usbguard_gui.device import DeviceTarget


class TestUSBGuardClientAsyncAPI:
    """Verify the async signal-based API is correctly implemented."""

    def test_client_methods_return_none(self):
        """Test that client methods return None (fire-and-forget pattern)."""
        with patch("usbguard_gui.dbus_client._DBusThread"):
            from usbguard_gui.dbus_client import USBGuardClient

            client = USBGuardClient()

            assert client.list_devices() is None
            assert client.apply_device_policy(1, DeviceTarget.ALLOW) is None
            assert client.list_rules() is None
            assert client.remove_rule(1) is None

    def test_client_has_result_signals(self):
        """Test that client has result signals for async operations."""
        from usbguard_gui.dbus_client import USBGuardClient

        client = USBGuardClient()

        assert hasattr(client, "list_devices_result")
        assert hasattr(client, "apply_policy_result")
        assert hasattr(client, "list_rules_result")
        assert hasattr(client, "remove_rule_result")

    def test_client_has_device_signals(self):
        """Test that client has device event signals."""
        from usbguard_gui.dbus_client import USBGuardClient

        client = USBGuardClient()

        assert hasattr(client, "device_presence_changed")
        assert hasattr(client, "device_policy_changed")
        assert hasattr(client, "connection_changed")

    def test_connect_returns_true(self):
        """Test that connect() returns True (starts thread)."""
        with patch("usbguard_gui.dbus_client._DBusThread") as mock_thread:
            from usbguard_gui.dbus_client import USBGuardClient

            mock_thread.return_value = MagicMock()
            client = USBGuardClient()

            result = client.connect()

            assert result is True
            mock_thread.return_value.start.assert_called_once()

    def test_stop_method_exists(self):
        """Test that stop() method exists for cleanup."""
        with patch("usbguard_gui.dbus_client._DBusThread") as mock_thread:
            from usbguard_gui.dbus_client import USBGuardClient

            mock_thread.return_value = MagicMock()
            client = USBGuardClient()
            client.connect()

            client.stop()

            mock_thread.return_value.stop.assert_called_once()
            mock_thread.return_value.wait.assert_called_once()

    def test_connected_property_deferred_to_thread(self):
        """Test that connected property is deferred to thread."""
        with patch("usbguard_gui.dbus_client._DBusThread") as mock_thread:
            from usbguard_gui.dbus_client import USBGuardClient

            mock_instance = MagicMock()
            mock_instance.is_connected = True
            mock_thread.return_value = mock_instance

            client = USBGuardClient()
            client.connect()

            assert client.connected is True
            mock_instance.is_connected = False
            assert client.connected is False


class TestScreensaverMonitorAsyncAPI:
    """Verify the screensaver monitor async API."""

    def test_lock_method_exists(self):
        """Test that lock() method exists."""
        with patch("usbguard_gui.screensaver._ScreensaverThread"):
            from usbguard_gui.screensaver import ScreensaverMonitor

            monitor = ScreensaverMonitor()
            assert hasattr(monitor, "lock")

    def test_stop_method_exists(self):
        """Test that stop() method exists for cleanup."""
        with patch("usbguard_gui.screensaver._ScreensaverThread") as mock_thread:
            from usbguard_gui.screensaver import ScreensaverMonitor

            mock_thread.return_value = MagicMock()
            monitor = ScreensaverMonitor()
            monitor.connect()

            monitor.stop()

            mock_thread.return_value.stop.assert_called_once()
            mock_thread.return_value.wait.assert_called_once()

    def test_has_active_changed_signal(self):
        """Test that monitor has active_changed signal."""
        from usbguard_gui.screensaver import ScreensaverMonitor

        monitor = ScreensaverMonitor()
        assert hasattr(monitor, "active_changed")
