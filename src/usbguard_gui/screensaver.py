"""Freedesktop screensaver D-Bus integration for screen lock awareness."""

from __future__ import annotations

import asyncio
import logging
import os
import queue

from dbus_fast import BusType, DBusError
from dbus_fast.aio import MessageBus
from PyQt6.QtCore import QObject, QThread, pyqtSignal

log = logging.getLogger(__name__)

SCREENSAVER_BUS_NAME = "org.freedesktop.ScreenSaver"
SCREENSAVER_PATH = "/org/freedesktop/ScreenSaver"
SCREENSAVER_IFACE = "org.freedesktop.ScreenSaver"


def _get_introspection(filename: str) -> str:
    module_dir = os.path.dirname(__file__)
    path = os.path.join(module_dir, "introspection", filename)
    with open(path) as f:
        return f.read()


class _ScreensaverThread(QThread):
    finished = pyqtSignal()
    connected = pyqtSignal(bool)
    active_changed = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bus: MessageBus | None = None
        self._proxy = None
        self._running = True
        self._command_queue: queue.Queue = queue.Queue()

    @property
    def active(self) -> bool:
        return False

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:
            log.error("Screensaver thread error: %s", e)
            self.error_occurred.emit(str(e))
        finally:
            self._loop.close()
            self.finished.emit()

    async def _main(self) -> None:
        try:
            self._bus = await MessageBus(bus_type=BusType.SESSION).connect()
        except Exception as e:
            log.warning("Failed to connect to session D-Bus: %s", e)
            self.connected.emit(False)
            return

        try:
            introspection = _get_introspection("org.freedesktop.ScreenSaver.xml")
            proxy_obj = self._bus.get_proxy_object(SCREENSAVER_BUS_NAME, SCREENSAVER_PATH, introspection)
            self._proxy = proxy_obj.get_interface(SCREENSAVER_IFACE)
            self._proxy.on_active_changed(self._on_active_changed)
            self.connected.emit(True)
            log.info("Connected to freedesktop ScreenSaver D-Bus")

        except DBusError as e:
            log.warning("Could not connect to screensaver D-Bus: %s", e)
            self.connected.emit(False)
            return

        while self._running:
            try:
                cmd = self._command_queue.get(timeout=0.1)
                await cmd()
            except queue.Empty:
                continue

        if self._bus:
            self._bus.disconnect()

    def _on_active_changed(self, active: bool) -> None:
        log.debug("Screensaver active: %s", active)
        self.active_changed.emit(bool(active))

    def _schedule(self, coro: asyncio.coroutine) -> None:
        if self._loop and self._running:
            self._loop.call_soon_threadsafe(asyncio.ensure_future, coro)

    def stop(self) -> None:
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    async def _do_lock(self) -> None:
        if not self._proxy:
            log.warning("Cannot lock screen: screensaver proxy not available")
            return
        try:
            await self._proxy.call_lock()
        except DBusError as e:
            log.warning("Failed to lock screen: %s", e)

    def lock(self) -> None:
        if self._proxy and self._loop:
            self._schedule(self._do_lock())


class ScreensaverMonitor(QObject):
    """Monitor the freedesktop screensaver (works on KDE, GNOME, etc.)."""

    active_changed = pyqtSignal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: _ScreensaverThread | None = None
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def connect(self) -> bool:
        self._thread = _ScreensaverThread(self)
        self._thread.connected.connect(self._on_connected)
        self._thread.active_changed.connect(self._on_active_changed)
        self._thread.start()
        return True

    def _on_connected(self, connected: bool) -> None:
        if not connected:
            log.warning("Could not connect to screensaver D-Bus")

    def _on_active_changed(self, active: bool) -> None:
        self._active = active
        self.active_changed.emit(active)

    def stop(self) -> None:
        if self._thread:
            self._thread.stop()
            self._thread.wait()
            self._thread = None

    def lock(self) -> None:
        if self._thread:
            self._thread.lock()
