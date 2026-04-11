"""Freedesktop screensaver D-Bus integration for screen lock awareness."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from dbus_fast import BusType, DBusError
from dbus_fast.aio import MessageBus
from PyQt6.QtCore import QObject, QThread, pyqtSignal

log = logging.getLogger(__name__)

SCREENSAVER_BUS_NAME = "org.freedesktop.ScreenSaver"
SCREENSAVER_PATH = "/org/freedesktop/ScreenSaver"
SCREENSAVER_IFACE = "org.freedesktop.ScreenSaver"

LOGIN1_BUS_NAME = "org.freedesktop.login1"
LOGIN1_PATH = "/org/freedesktop/login1"
LOGIN1_MANAGER_IFACE = "org.freedesktop.login1.Manager"

# Seconds between polls of logind's inhibitor list. Short enough that a
# user-initiated "prevent screen lock" toggle is picked up before the next
# HID insertion, long enough that we are not hammering the system bus.
_INHIBIT_POLL_INTERVAL = 2.0


def _get_introspection(filename: str) -> str:
    module_dir = os.path.dirname(__file__)
    path = os.path.join(module_dir, "introspection", filename)
    with open(path) as f:
        return f.read()


# Pre-load introspection XML at import time so the async event loop never
# blocks on file I/O.
_SCREENSAVER_INTROSPECTION = _get_introspection("org.freedesktop.ScreenSaver.xml")


class _ScreensaverThread(QThread):
    finished = pyqtSignal()
    connected = pyqtSignal(bool)
    active_changed = pyqtSignal(bool)
    inhibit_changed = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bus: MessageBus | None = None
        self._system_bus: MessageBus | None = None
        self._proxy = None
        self._logind = None
        self._running = True
        self._inhibited = False

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
            proxy_obj = self._bus.get_proxy_object(
                SCREENSAVER_BUS_NAME, SCREENSAVER_PATH, _SCREENSAVER_INTROSPECTION
            )
            self._proxy = proxy_obj.get_interface(SCREENSAVER_IFACE)
            self._proxy.on_active_changed(self._on_active_changed)
            self.connected.emit(True)
            log.info("Connected to freedesktop ScreenSaver D-Bus")

        except DBusError as e:
            log.warning("Could not connect to screensaver D-Bus: %s", e)
            self.connected.emit(False)
            return

        # Connect to logind on the system bus so we can poll screen-lock
        # inhibitors. Non-fatal: absent logind just means we treat the
        # system as never-inhibited.
        await self._setup_logind()

        # Keep the event loop alive and yielding so that call_soon_threadsafe
        # callbacks (e.g. screen-lock requests) are processed promptly, and
        # periodically poll logind for inhibitor state changes.
        last_inhibit_poll = time.monotonic()
        while self._running:
            if self._logind and time.monotonic() - last_inhibit_poll >= _INHIBIT_POLL_INTERVAL:
                last_inhibit_poll = time.monotonic()
                await self._check_inhibited()
            await asyncio.sleep(0.1)

        if self._bus:
            self._bus.disconnect()
        if self._system_bus:
            self._system_bus.disconnect()

    async def _setup_logind(self) -> None:
        """Connect to logind for screen-lock inhibitor detection. Non-fatal."""
        try:
            self._system_bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            intro = await self._system_bus.introspect(LOGIN1_BUS_NAME, LOGIN1_PATH)
            logind_obj = self._system_bus.get_proxy_object(LOGIN1_BUS_NAME, LOGIN1_PATH, intro)
            self._logind = logind_obj.get_interface(LOGIN1_MANAGER_IFACE)
            log.info("Connected to logind for screen-lock inhibitor detection")
            await self._check_inhibited()  # seed the cache
        except Exception as e:
            log.warning("Logind inhibit detection unavailable: %s", e)
            self._logind = None

    async def _check_inhibited(self) -> None:
        """Poll logind and emit inhibit_changed on transitions."""
        if not self._logind:
            return
        try:
            inhibitors = await self._logind.call_list_inhibitors()
        except DBusError as e:
            log.debug("ListInhibitors failed: %s", e)
            return
        new_state = self._has_idle_block_inhibitor(inhibitors)
        if new_state != self._inhibited:
            self._inhibited = new_state
            log.info(
                "Screen-lock inhibit state changed: %s",
                "active (idle/block inhibitor present)" if new_state else "cleared",
            )
            self.inhibit_changed.emit(new_state)

    @staticmethod
    def _has_idle_block_inhibitor(inhibitors) -> bool:
        """Return True if any logind inhibitor blocks idle/screen-lock.

        `ListInhibitors` returns an array of (what, who, why, mode, uid, pid)
        tuples. `what` is a colon-separated list of inhibition classes; we
        care about `idle` (prevents the session from transitioning to idle,
        which is what GNOME/KDE "Prevent screen lock" and dnf/rpm transactions
        acquire). `mode == "block"` means a hard block, while `delay` would
        only delay transitions — we only honor hard blocks.
        """
        for row in inhibitors or ():
            try:
                what = str(row[0])
                mode = str(row[3])
            except (IndexError, TypeError):
                continue
            if mode != "block":
                continue
            if "idle" in what.split(":"):
                return True
        return False

    def _on_active_changed(self, active: bool) -> None:
        log.debug("Screensaver active: %s", active)
        self.active_changed.emit(bool(active))

    def _schedule(self, coro: asyncio.coroutine) -> None:
        if self._loop and self._running:
            self._loop.call_soon_threadsafe(asyncio.ensure_future, coro)

    def stop(self) -> None:
        # Flip the flag and let _main()'s keep-alive loop exit on its next
        # iteration so the trailing bus.disconnect() can run. Do NOT call
        # loop.stop() here — that kills the loop mid-await and skips cleanup.
        self._running = False

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
    inhibit_changed = pyqtSignal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: _ScreensaverThread | None = None
        self._active = False
        self._inhibited = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def inhibited(self) -> bool:
        """Whether a logind idle/block inhibitor is currently preventing screen lock."""
        return self._inhibited

    def connect(self) -> bool:
        self._thread = _ScreensaverThread(self)
        self._thread.connected.connect(self._on_connected)
        self._thread.active_changed.connect(self._on_active_changed)
        self._thread.inhibit_changed.connect(self._on_inhibit_changed)
        self._thread.start()
        return True

    def _on_connected(self, connected: bool) -> None:
        if not connected:
            log.warning("Could not connect to screensaver D-Bus")

    def _on_active_changed(self, active: bool) -> None:
        self._active = active
        self.active_changed.emit(active)

    def _on_inhibit_changed(self, inhibited: bool) -> None:
        self._inhibited = inhibited
        self.inhibit_changed.emit(inhibited)

    def stop(self) -> None:
        if self._thread:
            self._thread.stop()
            self._thread.wait()
            self._thread = None

    def lock(self) -> None:
        if self._thread:
            self._thread.lock()
