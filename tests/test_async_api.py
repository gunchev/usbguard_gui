"""Tests for the async signal-based D-Bus client API.

These tests verify that the new async signal-based API is correctly implemented
and that client methods return None (fire-and-forget) while results come via signals.
"""

from __future__ import annotations

import logging
from unittest.mock import ANY, MagicMock, patch

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

    def test_has_inhibit_changed_signal(self):
        """Monitor must expose an inhibit_changed signal so the app can react to it."""
        from usbguard_gui.screensaver import ScreensaverMonitor

        monitor = ScreensaverMonitor()
        assert hasattr(monitor, "inhibit_changed")

    def test_inhibited_property_defaults_false(self):
        from usbguard_gui.screensaver import ScreensaverMonitor

        monitor = ScreensaverMonitor()
        assert monitor.inhibited is False

    def test_inhibited_property_follows_thread_signal(self):
        from usbguard_gui.screensaver import ScreensaverMonitor

        with patch("usbguard_gui.screensaver._ScreensaverThread") as mock_cls:
            from PyQt6.QtCore import QObject, pyqtSignal

            class MockThread(QObject):
                connected = pyqtSignal(bool)
                active_changed = pyqtSignal(bool)
                inhibit_changed = pyqtSignal(bool)

                def start(self):
                    pass

                def stop(self):
                    pass

                def wait(self):
                    pass

                def lock(self):
                    pass

            mock_cls.return_value = MockThread()
            monitor = ScreensaverMonitor()
            monitor.connect()

            assert monitor.inhibited is False
            mock_cls.return_value.inhibit_changed.emit(True)
            assert monitor.inhibited is True
            mock_cls.return_value.inhibit_changed.emit(False)
            assert monitor.inhibited is False


class TestScreensaverStopBoundedWait:
    """ScreensaverMonitor.stop() must not wait forever for the worker thread:
    a worker stuck in MessageBus.connect() would hang _quit() indefinitely,
    so the wait is bounded with terminate() as the last-resort fallback."""

    def _monitor_with_thread(self, thread):
        from usbguard_gui.screensaver import ScreensaverMonitor

        monitor = ScreensaverMonitor()
        monitor._thread = thread
        return monitor

    def test_stop_waits_with_bounded_timeout(self):
        from usbguard_gui.screensaver import _THREAD_STOP_TIMEOUT_MS

        thread = MagicMock()
        thread.wait.return_value = True
        monitor = self._monitor_with_thread(thread)

        monitor.stop()

        thread.stop.assert_called_once_with()
        thread.wait.assert_called_once_with(_THREAD_STOP_TIMEOUT_MS)
        thread.terminate.assert_not_called()
        assert monitor._thread is None

    def test_stop_terminates_when_wait_times_out(self):
        from usbguard_gui.screensaver import _THREAD_STOP_TIMEOUT_MS

        thread = MagicMock()
        thread.wait.return_value = False
        monitor = self._monitor_with_thread(thread)

        monitor.stop()

        thread.stop.assert_called_once_with()
        thread.wait.assert_called_once_with(_THREAD_STOP_TIMEOUT_MS)
        thread.terminate.assert_called_once_with()
        assert monitor._thread is None


class TestScreensaverConnectionState:
    """The monitor must expose lock availability (connected /
    connection_changed) so the app can disable all allow/deny functionality
    when screen locking is unavailable instead of letting lock() no-op
    silently."""

    def test_has_connection_changed_signal(self):
        from usbguard_gui.screensaver import ScreensaverMonitor

        monitor = ScreensaverMonitor()
        assert hasattr(monitor, "connection_changed")

    def test_connected_defaults_false(self):
        from usbguard_gui.screensaver import ScreensaverMonitor

        monitor = ScreensaverMonitor()
        assert monitor.connected is False

    def test_connected_follows_thread_signal(self):
        from PyQt6.QtCore import QObject, pyqtSignal

        from usbguard_gui.screensaver import ScreensaverMonitor

        with patch("usbguard_gui.screensaver._ScreensaverThread") as mock_cls:

            class MockThread(QObject):
                connected = pyqtSignal(bool)
                active_changed = pyqtSignal(bool)
                inhibit_changed = pyqtSignal(bool)

                def start(self):
                    pass

                def stop(self):
                    pass

                def wait(self, timeout=None):
                    pass

                def lock(self):
                    pass

            mock_cls.return_value = MockThread()
            monitor = ScreensaverMonitor()
            monitor.connect()

            events: list[bool] = []
            monitor.connection_changed.connect(lambda v: events.append(v))

            mock_cls.return_value.connected.emit(True)
            assert monitor.connected is True
            assert events == [True]

            mock_cls.return_value.connected.emit(False)
            assert monitor.connected is False
            assert events == [True, False]


class TestScreensaverNameOwnerChangedHandler:
    """_on_name_owner_changed must react only to the ScreenSaver bus name,
    emitting connected(False) on owner loss and connected(True) on owner
    gain — so a screen-locker crash/restart is detected immediately instead
    of only on the next lock()/GetActive call that happens to fail."""

    def test_ignores_other_names(self):
        from usbguard_gui.screensaver import _ScreensaverThread

        thread = _ScreensaverThread()
        emitted: list[bool] = []
        thread.connected.connect(lambda v: emitted.append(v))

        thread._on_name_owner_changed("org.example.Other", "", ":1.42")

        assert emitted == []

    def test_service_appearance_emits_connected_true(self):
        from usbguard_gui.screensaver import _ScreensaverThread

        thread = _ScreensaverThread()
        emitted: list[bool] = []
        thread.connected.connect(lambda v: emitted.append(v))

        thread._on_name_owner_changed("org.freedesktop.ScreenSaver", "", ":1.42")

        assert emitted == [True]

    def test_service_disappearance_emits_connected_false(self):
        from usbguard_gui.screensaver import _ScreensaverThread

        thread = _ScreensaverThread()
        emitted: list[bool] = []
        thread.connected.connect(lambda v: emitted.append(v))

        thread._on_name_owner_changed("org.freedesktop.ScreenSaver", ":1.42", "")

        assert emitted == [False]


class TestScreensaverReappearanceReseedsActive:
    """When the ScreenSaver name re-appears on the bus, the cached active
    state must be re-seeded via GetActive: no ActiveChanged signals reach
    the app while the service is down, so the screen could have
    locked/unlocked in the meantime and _active would otherwise stay
    stale (e.g. the app would keep believing the screen is unlocked
    after a locker restart that happened while it was locked)."""

    def test_reappearance_refetches_active(self, qapp, qtbot, mocker) -> None:
        import usbguard_gui.screensaver as sa

        state: dict = {
            "active": False,
            "get_active_calls": 0,
            "name_owner_handler": None,
        }

        class FakeProxy:
            def on_active_changed(self, handler):
                pass

            async def call_get_active(self):
                state["get_active_calls"] += 1
                return state["active"]

            async def call_lock(self):
                pass

        class FakeDBusProxy:
            def on_name_owner_changed(self, handler):
                state["name_owner_handler"] = handler

        class FakeLogindProxy:
            async def call_list_inhibitors(self):
                return []

        class FakeProxyObject:
            def __init__(self, bus_name: str) -> None:
                self._bus_name = bus_name

            def get_interface(self, name: str):
                if self._bus_name == sa.DBUS_BUS_NAME:
                    return FakeDBusProxy()
                if self._bus_name == sa.SCREENSAVER_BUS_NAME:
                    return FakeProxy()
                return FakeLogindProxy()

        class FakeBus:
            def get_proxy_object(self, bus_name, path, introspection):
                return FakeProxyObject(bus_name)

            async def introspect(self, bus_name, path):
                return "<node/>"

            def disconnect(self):
                pass

        class FakeMessageBus:
            def __init__(self, bus_type=None, **kwargs):
                self._bus_type = bus_type

            async def connect(self):
                return FakeBus()

        mocker.patch.object(sa, "MessageBus", FakeMessageBus)

        from usbguard_gui.screensaver import _ScreensaverThread

        thread = _ScreensaverThread()
        active_events: list[bool] = []
        thread.active_changed.connect(lambda v: active_events.append(v))
        thread.start()

        # Initial connect seeds active=False from the first GetActive.
        qtbot.waitUntil(lambda: state["name_owner_handler"] is not None, timeout=5000)
        qtbot.waitUntil(lambda: state["get_active_calls"] >= 1, timeout=5000)
        assert active_events == [False]

        # The screen locks while the service is down: no ActiveChanged
        # signal reaches the app.
        state["active"] = True

        # The service disappears... the cached state must be re-fetched
        # when it comes back, not left at the stale False.
        state["name_owner_handler"](sa.SCREENSAVER_BUS_NAME, ":1.42", "")
        state["name_owner_handler"](sa.SCREENSAVER_BUS_NAME, "", ":1.43")

        qtbot.waitUntil(lambda: len(active_events) >= 2, timeout=5000)
        thread.stop()
        assert thread.wait(2000)

        assert state["get_active_calls"] >= 2
        assert active_events[-1] is True
        assert thread._active is True


class TestScreensaverThreadRetry:
    """_ScreensaverThread must retry connecting while the session bus /
    ScreenSaver service is unavailable instead of giving up forever:
    the screen locker may start after the app does."""

    def _fake_stack(self, mocker, fail_times: int):
        """Patch MessageBus in the screensaver module with a fake that fails
        the first `fail_times` connect() calls, then succeeds and serves a
        working ScreenSaver + logind proxy."""
        import usbguard_gui.screensaver as sa

        state = {"attempts": 0}

        class FakeProxy:
            def on_active_changed(self, handler):
                pass

            async def call_get_active(self):
                return False

            async def call_lock(self):
                pass

        class FakeDBusProxy:
            def on_name_owner_changed(self, handler):
                pass

        class FakeLogindProxy:
            async def call_list_inhibitors(self):
                return []

        class FakeProxyObject:
            def __init__(self, bus_name: str) -> None:
                self._bus_name = bus_name

            def get_interface(self, name: str):
                if self._bus_name == sa.DBUS_BUS_NAME:
                    return FakeDBusProxy()
                if self._bus_name == sa.SCREENSAVER_BUS_NAME:
                    return FakeProxy()
                return FakeLogindProxy()

        class FakeBus:
            def __init__(self, kind: str) -> None:
                self._kind = kind
                self.disconnected = False

            def get_proxy_object(self, bus_name, path, introspection):
                return FakeProxyObject(bus_name)

            async def introspect(self, bus_name, path):
                return "<node/>"

            def disconnect(self):
                self.disconnected = True

        class FakeMessageBus:
            def __init__(self, bus_type=None, **kwargs):
                self._bus_type = bus_type

            async def connect(self):
                state["attempts"] += 1
                if state["attempts"] <= fail_times:
                    raise OSError("no session bus")
                # SYSTEM bus (logind) is served with kind "logind".
                kind = "logind" if self._bus_type == sa.BusType.SYSTEM else "screensaver"
                return FakeBus(kind)

        mocker.patch.object(sa, "MessageBus", FakeMessageBus)
        mocker.patch.object(sa, "_CONNECT_RETRY_INTERVAL", 0.05)
        return state

    def test_retries_until_service_available(self, qapp, qtbot, mocker) -> None:
        self._fake_stack(mocker, fail_times=2)
        from usbguard_gui.screensaver import _ScreensaverThread

        thread = _ScreensaverThread()
        events: list[bool] = []
        thread.connected.connect(lambda v: events.append(v))
        thread.start()

        # Wait for the actual connected(True) emission, not just the attempt
        # count: attempts is incremented before connect() resolves, and
        # there's a `self._running` check between the successful connect and
        # the emit — stopping the thread as soon as attempts hits 3 can race
        # ahead of that emit and make it never happen.
        qtbot.waitUntil(lambda: True in events, timeout=5000)
        thread.stop()
        assert thread.wait(2000)

        # Failed probes reported disconnected, success reported connected.
        assert events.count(False) >= 2
        assert events[-1] is True

    def test_stop_during_retry_exits_thread(self, qapp, qtbot, mocker) -> None:
        """stop() must be honoured while the thread is stuck in the retry loop."""
        self._fake_stack(mocker, fail_times=10_000)
        from usbguard_gui.screensaver import _ScreensaverThread

        thread = _ScreensaverThread()
        thread.start()
        # Give it a moment to enter the retry loop, then stop it.
        qtbot.waitUntil(lambda: thread.isRunning(), timeout=5000)
        thread.stop()
        assert thread.wait(2000)


class TestHasIdleBlockInhibitor:
    """Unit tests for the logind inhibitor filter."""

    def _f(self, inhibitors):
        from usbguard_gui.screensaver import _ScreensaverThread

        return _ScreensaverThread._has_idle_block_inhibitor(inhibitors)

    def test_empty_list(self):
        assert self._f([]) is False

    def test_none_input(self):
        assert self._f(None) is False

    def test_single_idle_block(self):
        assert self._f([("idle", "dnf", "updating system", "block", 0, 1234)]) is True

    def test_single_idle_delay(self):
        """delay mode does not hard-block, so should be ignored."""
        assert self._f([("idle", "gnome-session", "reason", "delay", 1000, 4321)]) is False

    def test_sleep_block_without_idle(self):
        assert self._f([("sleep:shutdown", "packagekit", "txn", "block", 0, 100)]) is False

    def test_composite_what_with_idle(self):
        assert self._f([("idle:sleep:shutdown", "dnf", "txn", "block", 0, 1)]) is True

    def test_multiple_inhibitors_one_matches(self):
        rows = [
            ("sleep", "who1", "w1", "block", 0, 1),
            ("handle-lid-switch", "who2", "w2", "block", 0, 2),
            ("idle", "who3", "w3", "block", 0, 3),
        ]
        assert self._f(rows) is True

    def test_multiple_inhibitors_none_match(self):
        rows = [
            ("sleep", "who1", "w1", "block", 0, 1),
            ("handle-lid-switch", "who2", "w2", "delay", 0, 2),
            ("shutdown", "who3", "w3", "block", 0, 3),
        ]
        assert self._f(rows) is False

    def test_malformed_row_skipped(self):
        rows = [
            (),  # too short
            ("idle", "ok", "ok", "block", 0, 1),
        ]
        assert self._f(rows) is True


class TestRecycleWorkerThread:
    """connect() recycles the previous worker on the Qt main thread, so the
    teardown must not block for the full THREAD_STOP_TIMEOUT_MS — a wedged worker
    would otherwise freeze the tray for 3 s on every backoff retry."""

    @staticmethod
    def _thread(*, exited_in_grace: bool, still_running: bool = False) -> MagicMock:
        thread = MagicMock()
        thread.wait.return_value = exited_in_grace
        thread.isRunning.return_value = still_running
        return thread

    def test_grace_period_is_shorter_than_the_quit_timeout(self) -> None:
        from usbguard_gui.dbus_common import RECYCLE_GRACE_MS, THREAD_STOP_TIMEOUT_MS

        assert RECYCLE_GRACE_MS < THREAD_STOP_TIMEOUT_MS

    def test_worker_that_exits_in_grace_is_deleted_without_a_timer(self) -> None:
        from usbguard_gui.dbus_common import RECYCLE_GRACE_MS, recycle_worker_thread

        thread = self._thread(exited_in_grace=True)
        with patch("usbguard_gui.dbus_common.QTimer.singleShot") as single_shot:
            recycle_worker_thread(thread, "Test", logging.getLogger(__name__))

        thread.stop.assert_called_once_with()
        thread.wait.assert_called_once_with(RECYCLE_GRACE_MS)
        thread.deleteLater.assert_called_once_with()
        single_shot.assert_not_called()
        thread.terminate.assert_not_called()

    def test_wedged_worker_is_deferred_instead_of_blocked_on(self) -> None:
        from usbguard_gui.dbus_common import RECYCLE_GRACE_MS, THREAD_STOP_TIMEOUT_MS, recycle_worker_thread

        thread = self._thread(exited_in_grace=False, still_running=True)
        with patch("usbguard_gui.dbus_common.QTimer.singleShot") as single_shot:
            recycle_worker_thread(thread, "Test", logging.getLogger(__name__))

        # Only the short grace wait happens inline — never the full quit timeout.
        thread.wait.assert_called_once_with(RECYCLE_GRACE_MS)
        assert THREAD_STOP_TIMEOUT_MS not in [c.args[0] for c in thread.wait.call_args_list]
        # Nothing is terminated while the UI thread is still inside recycle().
        thread.terminate.assert_not_called()
        thread.deleteLater.assert_not_called()

        single_shot.assert_called_once()
        assert single_shot.call_args.args[0] == THREAD_STOP_TIMEOUT_MS
        single_shot.call_args.args[1]()  # deferred teardown fires
        thread.terminate.assert_called_once_with()
        thread.deleteLater.assert_called_once_with()

    def test_deferred_teardown_does_not_terminate_a_worker_that_recovered(self) -> None:
        from usbguard_gui.dbus_common import recycle_worker_thread

        thread = self._thread(exited_in_grace=False, still_running=False)
        with patch("usbguard_gui.dbus_common.QTimer.singleShot") as single_shot:
            recycle_worker_thread(thread, "Test", logging.getLogger(__name__))

        single_shot.call_args.args[1]()
        thread.terminate.assert_not_called()
        thread.deleteLater.assert_called_once_with()


class TestConnectRecyclesWithoutBlocking:
    """Both facades must retire a previous worker through the non-blocking path,
    and ScreensaverMonitor must do it too — an un-retired screensaver thread keeps
    its own ActiveChanged subscription, so every lock/unlock would be delivered
    twice."""

    def test_client_connect_recycles_the_previous_worker(self) -> None:
        from usbguard_gui.dbus_client import USBGuardClient

        first, second = MagicMock(), MagicMock()
        with patch("usbguard_gui.dbus_client._DBusThread", side_effect=[first, second]), \
                patch("usbguard_gui.dbus_client.recycle_worker_thread") as recycle:
            client = USBGuardClient()
            client.connect()
            assert recycle.call_count == 0  # nothing to retire yet
            client.connect()

        recycle.assert_called_once_with(first, "D-Bus", ANY)
        assert client._thread is second

    def test_screensaver_monitor_connect_recycles_the_previous_worker(self) -> None:
        from usbguard_gui.screensaver import ScreensaverMonitor

        first, second = MagicMock(), MagicMock()
        with patch("usbguard_gui.screensaver._ScreensaverThread", side_effect=[first, second]), \
                patch("usbguard_gui.screensaver.recycle_worker_thread") as recycle:
            monitor = ScreensaverMonitor()
            monitor.connect()
            assert recycle.call_count == 0
            monitor.connect()

        recycle.assert_called_once_with(first, "Screensaver", ANY)
        assert monitor._thread is second
