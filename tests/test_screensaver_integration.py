"""Integration tests for the screen-locker registry on a private session bus.

These tests run the real ``ScreensaverMonitor`` against stub services exported
on a throw-away ``dbus-daemon`` session bus, so the D-Bus names, paths,
introspection XML and ``Lock()`` signatures are exercised end to end instead of
being mocked.  They are skipped when ``dbus-daemon`` is unavailable.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest
from dbus_fast import BusType, DBusError
from dbus_fast.aio import MessageBus
from dbus_fast.annotations import DBusBool, DBusStr
from dbus_fast.service import ServiceInterface, method
from dbus_fast.service import signal as dbus_signal

from usbguard_gui.screensaver import ScreensaverMonitor

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.skipif(
    shutil.which("dbus-daemon") is None,
    reason="dbus-daemon is required for the private session bus",
)


@dataclass(frozen=True)
class _LockerSpec:
    """The D-Bus coordinates and lock signature of one stub locker."""

    bus_name: str
    path: str
    interface: str
    lock_takes_message: bool = False
    not_supported: bool = False


_FREEDESKTOP = _LockerSpec("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver",
                           "org.freedesktop.ScreenSaver")
_FREEDESKTOP_NOT_SUPPORTED = _LockerSpec("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver",
                                         "org.freedesktop.ScreenSaver", not_supported=True)
_GNOME = _LockerSpec("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver")
_CINNAMON = _LockerSpec("org.cinnamon.ScreenSaver", "/org/cinnamon/ScreenSaver", "org.cinnamon.ScreenSaver",
                        lock_takes_message=True)
_MATE = _LockerSpec("org.mate.ScreenSaver", "/org/mate/ScreenSaver", "org.mate.ScreenSaver")
_XFCE = _LockerSpec("org.xfce.ScreenSaver", "/org/xfce/ScreenSaver", "org.xfce.ScreenSaver")

_NATIVE_LOCKERS = [_GNOME, _CINNAMON, _MATE, _XFCE]


class _StubBase(ServiceInterface):
    """The Lock/GetActive/ActiveChanged API shared by the stub lockers."""

    def __init__(self, interface: str, owner: _StubLocker) -> None:
        super().__init__(interface)
        self._owner = owner

    @method()
    def GetActive(self) -> DBusBool:
        return self._owner.active

    @dbus_signal()
    def ActiveChanged(self, active: DBusBool) -> DBusBool:
        return active


class _NoArgLockStub(_StubBase):
    @method()
    def Lock(self):
        self._owner.lock_args = ()
        self._owner.lock_called.set()


class _MessageLockStub(_StubBase):
    @method()
    def Lock(self, message: DBusStr):
        self._owner.lock_args = (message,)
        self._owner.lock_called.set()


class _NotSupportedStub(ServiceInterface):
    """A desktop proxy that owns the name but answers NotSupported."""

    def __init__(self, interface: str) -> None:
        super().__init__(interface)

    @method()
    def GetActive(self) -> DBusBool:
        raise DBusError("org.freedesktop.DBus.Error.NotSupported", "not implemented")

    @method()
    def Lock(self):
        raise DBusError("org.freedesktop.DBus.Error.NotSupported", "not implemented")

    @dbus_signal()
    def ActiveChanged(self, active: DBusBool) -> DBusBool:
        return active


class _StubLocker(threading.Thread):
    """Export one stub locker on its own asyncio loop until stopped."""

    def __init__(self, spec: _LockerSpec) -> None:
        super().__init__(name=f"stub-{spec.bus_name}", daemon=True)
        self.spec = spec
        self.ready = threading.Event()
        self.lock_called = threading.Event()
        self.lock_args: tuple[str, ...] = ()
        self.active = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._stub: ServiceInterface | None = None
        self._error: BaseException | None = None

    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as e:  # surfaced by start()
            self._error = e
            self.ready.set()

    async def _main(self) -> None:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        if self.spec.not_supported:
            stub: ServiceInterface = _NotSupportedStub(self.spec.interface)
        elif self.spec.lock_takes_message:
            stub = _MessageLockStub(self.spec.interface, self)
        else:
            stub = _NoArgLockStub(self.spec.interface, self)
        bus.export(self.spec.path, stub)
        await bus.request_name(self.spec.bus_name)
        self._stub = stub
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self.ready.set()
        await self._stop_event.wait()
        bus.disconnect()

    def start(self) -> None:
        super().start()
        if not self.ready.wait(timeout=5):
            self.stop()
            raise AssertionError(f"{self.spec.bus_name} stub did not start")
        if self._error is not None:
            error, self._error = self._error, None
            raise error

    def emit_active(self, active: bool) -> None:
        self.active = active
        if self._loop is None or self._stub is None:
            raise AssertionError("stub is not running")
        self._loop.call_soon_threadsafe(self._stub.ActiveChanged, active)

    def stop(self) -> None:
        if self.is_alive() and self._loop is not None and self._stop_event is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._stop_event.set)
        self.join(timeout=5)


@contextmanager
def _running_lockers(specs: list[_LockerSpec]) -> Iterator[list[_StubLocker]]:
    stubs = [_StubLocker(spec) for spec in specs]
    try:
        for stub in stubs:
            stub.start()
        yield stubs
    finally:
        for stub in stubs:
            stub.stop()


@pytest.fixture()
def private_session_bus(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Start a throw-away session bus and point dbus-fast at it."""
    result = subprocess.run(
        ["dbus-daemon", "--session", "--fork", "--print-address=1", "--print-pid=1"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    address, pid_text = result.stdout.strip().splitlines()[:2]
    pid = int(pid_text)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)
    try:
        yield address
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)


@pytest.mark.parametrize("spec", _NATIVE_LOCKERS, ids=lambda s: s.bus_name)
def test_native_locker_connects_locks_and_tracks_active(qtbot, private_session_bus, spec):
    with _running_lockers([spec]) as stubs:
        stub = stubs[0]
        monitor = ScreensaverMonitor()
        try:
            monitor.connect()
            qtbot.waitUntil(lambda: monitor.connected, timeout=5000)
            assert monitor._thread is not None
            assert monitor._thread._service_name == spec.bus_name

            monitor.lock()
            assert stub.lock_called.wait(timeout=5)
            assert stub.lock_args == (("",) if spec.lock_takes_message else ())

            stub.emit_active(True)
            qtbot.waitUntil(lambda: monitor.active, timeout=5000)
            stub.emit_active(False)
            qtbot.waitUntil(lambda: not monitor.active, timeout=5000)
        finally:
            monitor.stop()


def test_prefers_freedesktop_when_both_answer(qtbot, private_session_bus):
    with _running_lockers([_FREEDESKTOP, _GNOME]):
        monitor = ScreensaverMonitor()
        try:
            monitor.connect()
            qtbot.waitUntil(lambda: monitor.connected, timeout=5000)
            assert monitor._thread is not None
            assert monitor._thread._service_name == _FREEDESKTOP.bus_name
        finally:
            monitor.stop()


def test_skips_not_supported_freedesktop_proxy(qtbot, private_session_bus):
    with _running_lockers([_FREEDESKTOP_NOT_SUPPORTED, _GNOME]):
        monitor = ScreensaverMonitor()
        try:
            monitor.connect()
            qtbot.waitUntil(lambda: monitor.connected, timeout=5000)
            assert monitor._thread is not None
            assert monitor._thread._service_name == _GNOME.bus_name
        finally:
            monitor.stop()


def test_initial_get_active_seeds_the_cache(qtbot, private_session_bus):
    with _running_lockers([_GNOME]) as stubs:
        stubs[0].active = True
        monitor = ScreensaverMonitor()
        try:
            monitor.connect()
            qtbot.waitUntil(lambda: monitor.connected and monitor.active, timeout=5000)
        finally:
            monitor.stop()


def test_no_service_reports_disconnected(qtbot, private_session_bus):
    monitor = ScreensaverMonitor()
    events: list[bool] = []
    monitor.connection_changed.connect(events.append)
    try:
        monitor.connect()
        qtbot.waitUntil(lambda: False in events, timeout=5000)
        assert monitor.connected is False
    finally:
        monitor.stop()
