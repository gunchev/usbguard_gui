"""Tests for the D-Bus client (mocked)."""

from __future__ import annotations

import os
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from usbguard_gui.dbus_client import USBGuardClient, _DBusThread, _is_connection_error, _is_permission_error
from usbguard_gui.device import DeviceTarget

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


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
            list_devices_correlated = pyqtSignal(int, list)
            list_rules_result = pyqtSignal(list)
            remove_rule_result = pyqtSignal(bool)

            def __init__(self):
                super().__init__()
                self._is_connected = True
                self._start_called = False
                self._stop_called = False
                self._wait_called = False
                self._wait_timeout = None
                self._list_devices_calls = []
                self._fetch_devices_calls = []
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

            def wait(self, timeout=None):
                self._wait_called = True
                self._wait_timeout = timeout
                return True

            def list_devices(self, query="match"):
                self._list_devices_calls.append(query)

            def fetch_devices(self, request_id, query="match"):
                self._fetch_devices_calls.append((request_id, query))

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


class TestIsConnectionError:
    """Test _is_connection_error function: only errors that indicate the
    transport/session itself is broken (daemon gone, bus torn down) should
    be classified as connection errors — ordinary per-call failures (bad
    device id, unknown rule id) must not be."""

    def test_service_unknown_is_connection_error(self):
        from dbus_fast import DBusError, ErrorType

        e = DBusError(ErrorType.SERVICE_UNKNOWN, "The name is not owned")
        assert _is_connection_error(e) is True

    def test_name_has_no_owner_is_connection_error(self):
        from dbus_fast import DBusError, ErrorType

        e = DBusError(ErrorType.NAME_HAS_NO_OWNER, "no owner")
        assert _is_connection_error(e) is True

    def test_no_reply_is_connection_error(self):
        from dbus_fast import DBusError, ErrorType

        e = DBusError(ErrorType.NO_REPLY, "no reply")
        assert _is_connection_error(e) is True

    def test_disconnected_is_connection_error(self):
        from dbus_fast import DBusError, ErrorType

        e = DBusError(ErrorType.DISCONNECTED, "connection closed")
        assert _is_connection_error(e) is True

    def test_business_logic_error_is_not_connection_error(self):
        """A generic 'Failed' error for an ordinary per-call failure (e.g.
        applying policy to a device that was just unplugged) must not be
        mistaken for a broken transport."""
        from dbus_fast import DBusError, ErrorType

        e = DBusError(ErrorType.FAILED, "No such device")
        assert _is_connection_error(e) is False

    def test_permission_error_is_not_connection_error(self):
        from dbus_fast import DBusError, ErrorType

        e = DBusError(ErrorType.ACCESS_DENIED, "test")
        assert _is_connection_error(e) is False


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
        from usbguard_gui.dbus_client import _THREAD_STOP_TIMEOUT_MS

        client.connect()
        client.stop()
        assert mock_thread._stop_called is True
        # wait() must be called with the bounded timeout
        assert mock_thread._wait_called is True
        assert mock_thread._wait_timeout == _THREAD_STOP_TIMEOUT_MS
        assert client._thread is None

    def test_list_devices_no_thread(self, client):
        client.list_devices()

    def test_apply_device_policy_no_thread(self, client):
        client.apply_device_policy(1, DeviceTarget.ALLOW)

    def test_list_rules_no_thread(self, client):
        client.list_rules()

    def test_remove_rule_no_thread(self, client):
        client.remove_rule(1)


class TestConnectRecyclesPreviousThread:
    """USBGuardClient.connect() must stop the previous worker thread before
    starting a new one — the app calls connect() again on every reconnect
    attempt, and leaving the old thread running would leak a live QThread
    (with its D-Bus connection and signal subscriptions) that keeps
    delivering duplicate events after a daemon restart."""

    @staticmethod
    def _make_factory(threads: list):
        from PyQt6.QtCore import QObject, pyqtSignal

        class MockThread(QObject):
            connection_changed = pyqtSignal(bool)
            device_presence_changed = pyqtSignal(int, int, int, str, dict)
            device_policy_changed = pyqtSignal(int, int, int, str, int, dict)
            list_devices_result = pyqtSignal(list)
            list_devices_correlated = pyqtSignal(int, list)
            list_rules_result = pyqtSignal(list)
            remove_rule_result = pyqtSignal(bool)

            def __init__(self, parent=None):
                super().__init__(parent)
                self.events: list = []

            def start(self):
                self.events.append("start")

            def stop(self):
                self.events.append("stop")

            def wait(self, timeout=None):
                self.events.append("wait")
                return True

        def factory(parent=None):
            t = MockThread(parent)
            threads.append(t)
            return t

        return factory

    def test_connect_stops_previous_thread(self):
        threads: list = []
        with patch("usbguard_gui.dbus_client._DBusThread", side_effect=self._make_factory(threads)):
            client = USBGuardClient()
            client.connect()
            client.connect()  # e.g. _try_connect() on reconnect

            assert len(threads) == 2
            # The first worker must have been stopped and waited for, and
            # only then may the second one be started.
            assert threads[0].events == ["start", "stop", "wait"]
            assert threads[1].events == ["start"]
            assert client._thread is threads[1]

    def test_connect_without_previous_thread(self):
        threads: list = []
        with patch("usbguard_gui.dbus_client._DBusThread", side_effect=self._make_factory(threads)):
            client = USBGuardClient()
            client.connect()

            assert len(threads) == 1
            assert threads[0].events == ["start"]


class TestStopBoundedWait:
    """stop() must not wait forever for the worker thread: a worker stuck in
    MessageBus.connect() would hang _quit() indefinitely, so the wait is
    bounded with terminate() as the last-resort fallback."""

    def _client_with_thread(self, thread: MagicMock) -> USBGuardClient:
        client = USBGuardClient()
        client._thread = thread
        return client

    def test_stop_waits_with_bounded_timeout(self):
        from usbguard_gui.dbus_client import _THREAD_STOP_TIMEOUT_MS

        thread = MagicMock()
        thread.wait.return_value = True
        client = self._client_with_thread(thread)

        client.stop()

        thread.stop.assert_called_once_with()
        thread.wait.assert_called_once_with(_THREAD_STOP_TIMEOUT_MS)
        thread.terminate.assert_not_called()
        assert client._thread is None

    def test_stop_terminates_when_wait_times_out(self):
        from usbguard_gui.dbus_client import _THREAD_STOP_TIMEOUT_MS

        thread = MagicMock()
        thread.wait.return_value = False
        client = self._client_with_thread(thread)

        client.stop()

        thread.stop.assert_called_once_with()
        thread.wait.assert_called_once_with(_THREAD_STOP_TIMEOUT_MS)
        thread.terminate.assert_called_once_with()
        assert client._thread is None


class TestNameOwnerChangedHandler:
    """_on_name_owner_changed must flip _connected (and emit) only for the
    USBGuard bus name, on owner gain/loss — not for other names and not
    when ownership merely moves to a new unique name (daemon restart)."""

    def test_ignores_other_names(self):
        thread = _DBusThread()
        thread._connected = True
        emitted: list[bool] = []
        thread.connection_changed.connect(lambda v: emitted.append(v))

        thread._on_name_owner_changed("org.example.Other", "", ":1.42")

        assert emitted == []
        assert thread._connected is True

    def test_daemon_appearance_sets_connected(self):
        thread = _DBusThread()
        thread._connected = False
        emitted: list[bool] = []
        thread.connection_changed.connect(lambda v: emitted.append(v))

        thread._on_name_owner_changed("org.usbguard1", "", ":1.42")

        assert emitted == [True]
        assert thread._connected is True

    def test_daemon_disappearance_clears_connected(self):
        thread = _DBusThread()
        thread._connected = True
        emitted: list[bool] = []
        thread.connection_changed.connect(lambda v: emitted.append(v))

        thread._on_name_owner_changed("org.usbguard1", ":1.42", "")

        assert emitted == [False]
        assert thread._connected is False

    def test_no_emit_when_owner_changes_but_stays_owned(self):
        thread = _DBusThread()
        thread._connected = True
        emitted: list[bool] = []
        thread.connection_changed.connect(lambda v: emitted.append(v))

        # Daemon restart: the name is handed to a new unique name.
        thread._on_name_owner_changed("org.usbguard1", ":1.42", ":1.43")

        assert emitted == []
        assert thread._connected is True


@pytest.mark.skipif(
    shutil.which("dbus-daemon") is None or shutil.which("gdbus") is None,
    reason="dbus-daemon and gdbus are required",
)
class TestProactiveOwnerDetection:
    """_DBusThread must notice the USBGuard daemon vanishing/returning on
    the bus (NameOwnerChanged) instead of staying 'connected' until the
    next call happens to fail, and must not claim 'connected' while the
    daemon is absent.  Runs against a private dbus-daemon."""

    _BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
"http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>custom</type>
  <listen>unix:tmpdir=/tmp</listen>
  <policy context="default">
    <allow user="*"/>
    <allow own="*"/>
    <allow send_destination="*"/>
    <allow receive_sender="*"/>
  </policy>
</busconfig>
"""

    @staticmethod
    def _gdbus(addr: str, method: str, *args: str) -> None:
        subprocess.run(
            [
                "gdbus",
                "call",
                "--address",
                addr,
                "--dest",
                "org.freedesktop.DBus",
                "--object-path",
                "/org/freedesktop/DBus",
                "--method",
                method,
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_detects_owner_loss_and_reappearance(self, qtbot, tmp_path, monkeypatch) -> None:
        config = tmp_path / "bus.conf"
        config.write_text(self._BUS_CONFIG)
        daemon = subprocess.Popen(
            ["dbus-daemon", "--config-file", str(config), "--print-address", "--nofork"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            assert daemon.stdout is not None
            addr = daemon.stdout.readline().strip()
            assert addr, "dbus-daemon did not print a bus address"
            monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", addr)

            thread = _DBusThread()
            events: list[bool] = []
            thread.connection_changed.connect(lambda v: events.append(v))
            thread.start()

            # 1. Bus is up, daemon absent -> reported disconnected.
            qtbot.waitUntil(lambda: len(events) >= 1 and events[0] is False, timeout=10000)

            # 2. Daemon appears (some process takes the bus name).
            self._gdbus(addr, "org.freedesktop.DBus.RequestName", "org.usbguard1", "0")
            qtbot.waitUntil(lambda: len(events) >= 2 and events[1] is True, timeout=10000)

            # 3. Daemon disappears again.
            self._gdbus(addr, "org.freedesktop.DBus.ReleaseName", "org.usbguard1")
            qtbot.waitUntil(lambda: len(events) >= 3 and events[2] is False, timeout=10000)

            thread.stop()
            assert thread.wait(5000)
        finally:
            daemon.terminate()
            daemon.wait()


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

        thread.apply_device_policy(1, DeviceTarget.ALLOW)

        thread._loop.call_soon_threadsafe.assert_not_called()

    def test_apply_device_policy_schedules_when_connected(self):
        thread = self._build_thread(connected=True)

        thread.apply_device_policy(1, DeviceTarget.ALLOW)

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


class TestConnectionDropNarrowing:
    """_do_apply_policy/_do_list_rules/_do_remove_rule/_do_list_devices must
    only flip _connected on errors that indicate a broken transport/session
    — an ordinary per-call failure (e.g. applying policy to a device that
    was unplugged a moment earlier, or a malformed list_devices query) must
    not trigger a full reconnect."""

    def _thread(self) -> _DBusThread:
        thread = _DBusThread()
        thread._connected = True
        thread._devices_iface = MagicMock()
        thread._policy_iface = MagicMock()
        return thread

    def _events(self, thread: _DBusThread) -> list[bool]:
        events: list[bool] = []
        thread.connection_changed.connect(lambda v: events.append(v))
        return events

    # -- apply_device_policy --------------------------------------------

    def test_apply_policy_business_error_does_not_disconnect(self):
        import asyncio

        from dbus_fast import DBusError, ErrorType

        thread = self._thread()
        events = self._events(thread)

        async def raise_error(*args, **kwargs):
            raise DBusError(ErrorType.FAILED, "No such device")

        thread._devices_iface.call_apply_device_policy = raise_error

        asyncio.run(thread._do_apply_policy(1, DeviceTarget.ALLOW, False))

        assert thread._connected is True
        assert events == []

    def test_apply_policy_connection_error_disconnects(self):
        import asyncio

        from dbus_fast import DBusError, ErrorType

        thread = self._thread()
        events = self._events(thread)

        async def raise_error(*args, **kwargs):
            raise DBusError(ErrorType.SERVICE_UNKNOWN, "gone")

        thread._devices_iface.call_apply_device_policy = raise_error

        asyncio.run(thread._do_apply_policy(1, DeviceTarget.ALLOW, False))

        assert thread._connected is False
        assert events == [False]

    # -- list_rules -------------------------------------------------------

    def test_list_rules_business_error_does_not_disconnect(self):
        import asyncio

        from dbus_fast import DBusError, ErrorType

        thread = self._thread()
        events = self._events(thread)

        async def raise_error(*args, **kwargs):
            raise DBusError(ErrorType.FAILED, "bad label")

        thread._policy_iface.call_list_rules = raise_error

        asyncio.run(thread._do_list_rules(""))

        assert thread._connected is True
        assert events == []

    def test_list_rules_connection_error_disconnects(self):
        import asyncio

        from dbus_fast import DBusError, ErrorType

        thread = self._thread()
        events = self._events(thread)

        async def raise_error(*args, **kwargs):
            raise DBusError(ErrorType.NO_REPLY, "gone")

        thread._policy_iface.call_list_rules = raise_error

        asyncio.run(thread._do_list_rules(""))

        assert thread._connected is False
        assert events == [False]

    # -- remove_rule --------------------------------------------------------

    def test_remove_rule_business_error_does_not_disconnect(self):
        import asyncio

        from dbus_fast import DBusError, ErrorType

        thread = self._thread()
        events = self._events(thread)

        async def raise_error(*args, **kwargs):
            raise DBusError(ErrorType.FAILED, "unknown rule id")

        thread._policy_iface.call_remove_rule = raise_error

        asyncio.run(thread._do_remove_rule(42))

        assert thread._connected is True
        assert events == []

    def test_remove_rule_connection_error_disconnects(self):
        import asyncio

        from dbus_fast import DBusError, ErrorType

        thread = self._thread()
        events = self._events(thread)

        async def raise_error(*args, **kwargs):
            raise DBusError(ErrorType.DISCONNECTED, "gone")

        thread._policy_iface.call_remove_rule = raise_error

        asyncio.run(thread._do_remove_rule(42))

        assert thread._connected is False
        assert events == [False]

    # -- list_devices -------------------------------------------------------

    def test_list_devices_business_error_does_not_disconnect(self):
        import asyncio

        from dbus_fast import DBusError, ErrorType

        thread = self._thread()
        events = self._events(thread)

        async def raise_error(*args, **kwargs):
            raise DBusError(ErrorType.FAILED, "malformed query")

        thread._devices_iface.call_list_devices = raise_error

        asyncio.run(thread._do_list_devices("match"))

        assert thread._connected is True
        assert events == []

    def test_list_devices_connection_error_disconnects(self):
        import asyncio

        from dbus_fast import DBusError, ErrorType

        thread = self._thread()
        events = self._events(thread)

        async def raise_error(*args, **kwargs):
            raise DBusError(ErrorType.SERVICE_UNKNOWN, "gone")

        thread._devices_iface.call_list_devices = raise_error

        asyncio.run(thread._do_list_devices("match"))

        assert thread._connected is False
        assert events == [False]


class TestCorrelatedFetchDevices:
    """fetch_devices() returns the snapshot tagged with the caller's request id on
    list_devices_correlated, so concurrent fetches can never be mistaken for each
    other and answers may arrive in any order.  Transport semantics are the same
    as list_devices(); only the delivery differs."""

    def _thread(self) -> _DBusThread:
        thread = _DBusThread()
        thread._connected = True
        thread._devices_iface = MagicMock()
        thread._policy_iface = MagicMock()
        return thread

    def _emitted(self, thread: _DBusThread) -> list:
        emitted: list = []
        thread.list_devices_correlated.connect(lambda rid, devs: emitted.append((rid, devs)))
        return emitted

    def test_success_emits_the_request_id_with_the_snapshot(self):
        import asyncio

        thread = self._thread()
        emitted = self._emitted(thread)

        async def fake_list(query):
            return [("10", 'allow id 1234:abcd serial "" name "A" hash "h1" with-interface 03:00:00')]

        thread._devices_iface.call_list_devices = fake_list
        asyncio.run(thread._do_fetch_devices(7, "match"))

        assert len(emitted) == 1
        assert emitted[0][0] == 7
        assert emitted[0][1][0].number == 10

    def test_business_error_emits_empty_for_that_id_and_stays_connected(self):
        import asyncio

        from dbus_fast import DBusError, ErrorType

        thread = self._thread()
        emitted = self._emitted(thread)

        async def raise_error(*args, **kwargs):
            raise DBusError(ErrorType.FAILED, "malformed query")

        thread._devices_iface.call_list_devices = raise_error
        asyncio.run(thread._do_fetch_devices(5, "match"))

        assert emitted == [(5, [])]
        assert thread._connected is True

    def test_connection_error_emits_empty_and_disconnects(self):
        import asyncio

        from dbus_fast import DBusError, ErrorType

        thread = self._thread()
        emitted = self._emitted(thread)

        async def raise_error(*args, **kwargs):
            raise DBusError(ErrorType.SERVICE_UNKNOWN, "gone")

        thread._devices_iface.call_list_devices = raise_error
        asyncio.run(thread._do_fetch_devices(5, "match"))

        assert emitted == [(5, [])]
        assert thread._connected is False

    def test_thread_fast_fails_when_disconnected_but_still_answers(self):
        thread = self._thread()
        thread._connected = False
        emitted = self._emitted(thread)

        thread.fetch_devices(9)

        assert emitted == [(9, [])]

    def test_client_delegates_with_the_request_id(self, client, mock_thread):
        client.connect()
        client.fetch_devices(7)
        assert mock_thread._fetch_devices_calls == [(7, "match")]

    def test_client_honours_a_custom_query(self, client, mock_thread):
        client.connect()
        client.fetch_devices(3, query="match blocked")
        assert mock_thread._fetch_devices_calls == [(3, "match blocked")]

    def test_client_without_a_thread_still_terminates_the_request(self, client):
        """No worker thread means nothing to ask, but the caller must still get its
        (request_id, []) so no request is left permanently outstanding."""
        emitted = []
        client.list_devices_correlated.connect(lambda rid, devs: emitted.append((rid, devs)))

        client.fetch_devices(11)

        assert emitted == [(11, [])]

    def test_correlated_signal_is_wired_from_the_thread(self, client, mock_thread):
        client.connect()
        emitted = []
        client.list_devices_correlated.connect(lambda rid, devs: emitted.append((rid, devs)))

        mock_thread.list_devices_correlated.emit(4, [MagicMock()])

        assert len(emitted) == 1
        assert emitted[0][0] == 4
