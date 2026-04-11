"""Tests for the D-Bus client (mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from usbguard_gui.dbus_client import USBGuardClient, _DBusThread, _is_permission_error
from usbguard_gui.device import DeviceTarget


@pytest.fixture()
def mock_thread():
    with patch("usbguard_gui.dbus_client._DBusThread") as mock_cls:
        from PyQt6.QtCore import QObject, pyqtSignal

        class MockThread(QObject):
            started = pyqtSignal()
            finished = pyqtSignal()
            connection_changed = pyqtSignal(bool)
            device_presence_changed = pyqtSignal(int, int, int, str, dict)
            device_policy_changed = pyqtSignal(int, int, int, str, int, dict)
            list_devices_result = pyqtSignal(list)
            apply_policy_result = pyqtSignal(object)
            list_rules_result = pyqtSignal(list)
            remove_rule_result = pyqtSignal(bool)

            def __init__(self):
                super().__init__()
                self._is_connected = True
                self._start_called = False
                self._stop_called = False
                self._list_devices_calls = []
                self._apply_policy_calls = []
                self._list_rules_calls = []
                self._remove_rule_calls = []

            @property
            def is_connected(self):
                return self._is_connected

            @is_connected.setter
            def is_connected(self, value):
                self._is_connected = value

            def start(self):
                self._start_called = True

            def stop(self):
                self._stop_called = True

            def wait(self):
                pass

            def list_devices(self, query="match"):
                self._list_devices_calls.append(query)

            def apply_device_policy(self, device_id, target, permanent=False):
                self._apply_policy_calls.append((device_id, target, permanent))

            def list_rules(self, label=""):
                self._list_rules_calls.append(label)

            def remove_rule(self, rule_id):
                self._remove_rule_calls.append(rule_id)

        thread_instance = MockThread()
        mock_cls.return_value = thread_instance
        yield thread_instance


@pytest.fixture()
def client(mock_thread):
    return USBGuardClient()


@pytest.fixture()
def connected_client(client, mock_thread):
    return client, mock_thread


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
    def test_connect_starts_thread(self, client, mock_thread):
        assert client.connect() is True
        assert mock_thread._start_called is True

    def test_connected_property_false_when_no_thread(self, client):
        assert client.connected is False

    def test_connected_property_true_when_thread_connected(self, client, mock_thread):
        mock_thread._is_connected = True
        client.connect()
        assert client.connected is True

    def test_connected_property_false_when_thread_disconnected(self, client, mock_thread):
        mock_thread._is_connected = False
        client.connect()
        assert client.connected is False

    def test_connection_changed_signal_propagates(self, client, mock_thread):
        emitted = []

        def capture(value):
            emitted.append(value)

        client.connection_changed.connect(capture)
        client.connect()
        mock_thread.connection_changed.emit(True)
        assert emitted == [True]

    def test_list_devices_calls_thread(self, client, mock_thread):
        client.connect()
        client.list_devices()
        assert mock_thread._list_devices_calls == ["match"]

    def test_list_devices_with_custom_query(self, client, mock_thread):
        client.connect()
        client.list_devices(query="match blocked")
        assert mock_thread._list_devices_calls == ["match blocked"]

    def test_list_devices_result_signal_propagates(self, client, mock_thread):
        client.connect()
        emitted = []

        def capture(value):
            emitted.append(value)

        client.list_devices_result.connect(capture)
        mock_thread.list_devices_result.emit([MagicMock()])
        assert len(emitted) == 1

    def test_apply_device_policy_calls_thread(self, client, mock_thread):
        client.connect()
        client.apply_device_policy(1, DeviceTarget.ALLOW)
        assert mock_thread._apply_policy_calls == [(1, DeviceTarget.ALLOW, False)]

    def test_apply_device_policy_with_permanent(self, client, mock_thread):
        client.connect()
        client.apply_device_policy(1, DeviceTarget.BLOCK, permanent=True)
        assert mock_thread._apply_policy_calls == [(1, DeviceTarget.BLOCK, True)]

    def test_apply_device_policy_result_signal_propagates(self, client, mock_thread):
        client.connect()
        emitted = []

        def capture(value):
            emitted.append(value)

        client.apply_policy_result.connect(capture)
        mock_thread.apply_policy_result.emit(42)
        assert emitted == [42]

    def test_list_rules_calls_thread(self, client, mock_thread):
        client.connect()
        client.list_rules()
        assert mock_thread._list_rules_calls == [""]

    def test_list_rules_with_label(self, client, mock_thread):
        client.connect()
        client.list_rules(label="my-label")
        assert mock_thread._list_rules_calls == ["my-label"]

    def test_list_rules_result_signal_propagates(self, client, mock_thread):
        client.connect()
        emitted = []

        def capture(value):
            emitted.append(value)

        client.list_rules_result.connect(capture)
        mock_thread.list_rules_result.emit([(1, "rule")])
        assert emitted == [[(1, "rule")]]

    def test_remove_rule_calls_thread(self, client, mock_thread):
        client.connect()
        client.remove_rule(42)
        assert mock_thread._remove_rule_calls == [42]

    def test_remove_rule_result_signal_propagates(self, client, mock_thread):
        client.connect()
        emitted = []

        def capture(value):
            emitted.append(value)

        client.remove_rule_result.connect(capture)
        mock_thread.remove_rule_result.emit(True)
        assert emitted == [True]

    def test_device_presence_changed_signal_propagates(self, client, mock_thread):
        client.connect()
        emitted = []

        def capture(*args):
            emitted.append(args)

        client.device_presence_changed.connect(capture)
        mock_thread.device_presence_changed.emit(1, 2, 3, "rule", {"key": "value"})
        assert emitted == [(1, 2, 3, "rule", {"key": "value"})]

    def test_device_policy_changed_signal_propagates(self, client, mock_thread):
        client.connect()
        emitted = []

        def capture(*args):
            emitted.append(args)

        client.device_policy_changed.connect(capture)
        mock_thread.device_policy_changed.emit(1, 2, 3, "rule", 4, {"key": "value"})
        assert emitted == [(1, 2, 3, "rule", 4, {"key": "value"})]

    def test_stop_calls_thread_stop_and_wait(self, client, mock_thread):
        client.connect()
        client.stop()
        assert mock_thread._stop_called is True

    def test_list_devices_no_thread(self, client):
        client.list_devices()

    def test_apply_device_policy_no_thread(self, client):
        client.apply_device_policy(1, DeviceTarget.ALLOW)

    def test_list_rules_no_thread(self, client):
        client.list_rules()

    def test_remove_rule_no_thread(self, client):
        client.remove_rule(1)


class TestDBusThread:
    def test_init_sets_defaults(self):
        thread = _DBusThread()
        assert thread._running is True
        assert thread._connected is False
        assert thread._bus is None
        assert thread._devices_iface is None
        assert thread._policy_iface is None

    def test_is_connected_property(self):
        thread = _DBusThread()
        assert thread.is_connected is False
        thread._connected = True
        assert thread.is_connected is True

    def test_schedule_does_nothing_when_not_running(self):
        thread = _DBusThread()
        thread._running = False
        thread._loop = MagicMock()
        thread._schedule(MagicMock())
        thread._loop.call_soon_threadsafe.assert_not_called()

    def test_schedule_calls_loop(self):
        thread = _DBusThread()
        thread._running = True
        thread._loop = MagicMock()
        coro = MagicMock()
        thread._schedule(coro)
        thread._loop.call_soon_threadsafe.assert_called_once()

    def test_no_command_queue(self):
        """_DBusThread must not have a blocking command queue — it caused the event loop to deadlock."""
        thread = _DBusThread()
        assert not hasattr(thread, "_command_queue")

    def test_event_loop_runs_scheduled_coroutines(self):
        """_schedule() coroutines must actually execute (i.e. the event loop is not blocked)."""
        import asyncio

        results: list[str] = []

        async def run_with_schedule():
            loop = asyncio.get_event_loop()
            thread = _DBusThread()
            thread._loop = loop
            thread._running = True

            async def work():
                results.append("ran")
                thread._running = False  # stop the keep-alive loop

            thread._schedule(work())
            # Simulate the keep-alive loop for a few iterations
            for _ in range(10):
                await asyncio.sleep(0)
                if not thread._running:
                    break

        asyncio.run(run_with_schedule())
        assert results == ["ran"], "Scheduled coroutine was never executed — event loop was blocked"


class TestDBusThreadFastFail:
    """Fast-fail circuit-breaker behavior: methods must not queue work when _connected is False."""

    def _build_thread(self, connected: bool) -> _DBusThread:
        thread = _DBusThread()
        thread._loop = MagicMock()
        thread._running = True
        thread._devices_iface = MagicMock()
        thread._policy_iface = MagicMock()
        thread._connected = connected
        return thread

    @staticmethod
    def _close_captured_coros(mock_loop: MagicMock) -> None:
        """Close any coroutines captured by the mocked call_soon_threadsafe so they don't leak as RuntimeWarnings."""
        import inspect

        for call in mock_loop.call_soon_threadsafe.call_args_list:
            for arg in call.args:
                if inspect.iscoroutine(arg):
                    arg.close()

    def test_list_devices_fast_fails_when_disconnected(self):
        thread = self._build_thread(connected=False)
        emitted: list[list] = []
        thread.list_devices_result.connect(lambda v: emitted.append(v))

        thread.list_devices()

        assert emitted == [[]]
        thread._loop.call_soon_threadsafe.assert_not_called()

    def test_list_devices_schedules_when_connected(self):
        thread = self._build_thread(connected=True)
        emitted: list[list] = []
        thread.list_devices_result.connect(lambda v: emitted.append(v))

        thread.list_devices()

        assert emitted == []
        thread._loop.call_soon_threadsafe.assert_called_once()
        self._close_captured_coros(thread._loop)

    def test_apply_device_policy_fast_fails_when_disconnected(self):
        thread = self._build_thread(connected=False)
        emitted: list = []
        thread.apply_policy_result.connect(lambda v: emitted.append(v))

        thread.apply_device_policy(1, DeviceTarget.ALLOW)

        assert emitted == [None]
        thread._loop.call_soon_threadsafe.assert_not_called()

    def test_apply_device_policy_schedules_when_connected(self):
        thread = self._build_thread(connected=True)
        emitted: list = []
        thread.apply_policy_result.connect(lambda v: emitted.append(v))

        thread.apply_device_policy(1, DeviceTarget.ALLOW)

        assert emitted == []
        thread._loop.call_soon_threadsafe.assert_called_once()
        self._close_captured_coros(thread._loop)

    def test_list_rules_fast_fails_when_disconnected(self):
        thread = self._build_thread(connected=False)
        emitted: list[list] = []
        thread.list_rules_result.connect(lambda v: emitted.append(v))

        thread.list_rules()

        assert emitted == [[]]
        thread._loop.call_soon_threadsafe.assert_not_called()

    def test_list_rules_schedules_when_connected(self):
        thread = self._build_thread(connected=True)
        emitted: list[list] = []
        thread.list_rules_result.connect(lambda v: emitted.append(v))

        thread.list_rules()

        assert emitted == []
        thread._loop.call_soon_threadsafe.assert_called_once()
        self._close_captured_coros(thread._loop)

    def test_remove_rule_fast_fails_when_disconnected(self):
        thread = self._build_thread(connected=False)
        emitted: list[bool] = []
        thread.remove_rule_result.connect(lambda v: emitted.append(v))

        thread.remove_rule(42)

        assert emitted == [False]
        thread._loop.call_soon_threadsafe.assert_not_called()

    def test_remove_rule_schedules_when_connected(self):
        thread = self._build_thread(connected=True)
        emitted: list[bool] = []
        thread.remove_rule_result.connect(lambda v: emitted.append(v))

        thread.remove_rule(42)

        assert emitted == []
        thread._loop.call_soon_threadsafe.assert_called_once()
        self._close_captured_coros(thread._loop)

    def test_breaker_recloses_on_reconnection(self):
        """After _connected flips back to True (simulating successful reconnect), calls schedule again."""
        thread = self._build_thread(connected=False)
        emitted: list[list] = []
        thread.list_devices_result.connect(lambda v: emitted.append(v))

        # Open breaker: fast-fail, no schedule.
        thread.list_devices()
        assert emitted == [[]]
        thread._loop.call_soon_threadsafe.assert_not_called()

        # Simulate successful reconnect (mirrors dbus_client.py:111 in _main()).
        thread._connected = True

        # Closed breaker: call is scheduled, no synthetic empty emit.
        thread.list_devices()
        assert emitted == [[]]  # still just the earlier fast-fail
        thread._loop.call_soon_threadsafe.assert_called_once()
        self._close_captured_coros(thread._loop)
