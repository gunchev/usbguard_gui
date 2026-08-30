"""Shared helpers for the USBGuard and screensaver D-Bus worker threads.

Both dbus_client.py and screensaver.py run their own asyncio event loop on
a QThread and talk to a D-Bus service; this module holds the bits that
would otherwise be duplicated between them.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Coroutine
from typing import Any

from PyQt6.QtCore import QObject, QThread

# How long stop() waits for a worker thread to exit on its own before
# terminate() is used as a last resort.  A healthy worker notices
# _running=False within its 0.1 s keep-alive tick; a worker stuck in
# MessageBus.connect() never will, and an unbounded wait() would hang
# _quit() forever.
THREAD_STOP_TIMEOUT_MS = 3000

# The standard freedesktop D-Bus interface, used to watch NameOwnerChanged
# for proactive service-loss detection (both the USBGuard daemon on the
# system bus and the ScreenSaver service on the session bus).
DBUS_BUS_NAME = "org.freedesktop.DBus"
DBUS_BUS_PATH = "/org/freedesktop/DBus"
DBUS_IFACE = "org.freedesktop.DBus"


def get_introspection(filename: str) -> str:
    """Load bundled introspection XML from usbguard_gui/introspection/."""
    module_dir = os.path.dirname(__file__)
    path = os.path.join(module_dir, "introspection", filename)
    with open(path, encoding="utf-8") as f:
        return f.read()


class AsyncWorkerThread(QThread):
    """QThread running its own asyncio event loop, schedulable via
    call_soon_threadsafe from the Qt thread. Subclasses implement run()
    (typically `self._loop.run_until_complete(self._main())`) and use
    _schedule() from Qt-thread methods to hand fire-and-forget coroutines
    to the worker's event loop.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = True

    def _schedule(self, coro: Coroutine[Any, Any, Any]) -> None:
        if self._loop and self._running:
            self._loop.call_soon_threadsafe(asyncio.ensure_future, coro)

    def stop(self) -> None:
        # Flip the flag and let the subclass's keep-alive/retry loop exit on
        # its next iteration so any trailing bus.disconnect() can run. Do
        # NOT call loop.stop() here — that kills the loop mid-await and
        # skips cleanup.
        self._running = False


def stop_worker_thread(thread: AsyncWorkerThread, label: str, log: logging.Logger) -> None:
    """Ask a worker thread to stop, with a bounded wait + terminate() fallback."""
    thread.stop()
    if not thread.wait(THREAD_STOP_TIMEOUT_MS):
        log.warning("%s worker thread did not exit within %d ms — terminating", label, THREAD_STOP_TIMEOUT_MS)
        thread.terminate()
