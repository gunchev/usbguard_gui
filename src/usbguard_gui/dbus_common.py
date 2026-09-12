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

from PyQt6.QtCore import QObject, QThread, QTimer

# How long stop() waits for a worker thread to exit on its own before
# terminate() is used as a last resort.  A healthy worker notices
# _running=False within its 0.1 s keep-alive tick; a worker stuck in
# MessageBus.connect() never will, and an unbounded wait() would hang
# _quit() forever.
THREAD_STOP_TIMEOUT_MS = 3000

# How long a worker that is being *replaced* gets to exit on its own.  Much
# shorter than THREAD_STOP_TIMEOUT_MS because the recycle happens on the Qt
# main thread on every reconnect attempt: a healthy worker still exits
# inside its 0.1 s keep-alive tick, so the old bus connection and its
# signal subscriptions are gone before the replacement starts, while a
# wedged one costs 250 ms of UI instead of 3 s.
RECYCLE_GRACE_MS = 250

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


def recycle_worker_thread(thread: AsyncWorkerThread, label: str, log: logging.Logger) -> None:
    """Retire a worker that is being replaced, without stalling the Qt UI thread.

    Used by the connect()/reconnect path, which runs on the Qt main thread on
    every backoff retry.  Blocking there for the full THREAD_STOP_TIMEOUT_MS
    would freeze the tray for 3 s per attempt, so the worker is given only
    RECYCLE_GRACE_MS — enough for a healthy worker to leave its keep-alive
    loop, which is what guarantees the old D-Bus connection and its signal
    subscriptions are gone before the replacement starts (no device event is
    ever delivered twice).

    A worker still running after the grace period is wedged — typically inside
    MessageBus.connect(), i.e. holding no live bus connection and therefore no
    ability to emit device events — so finishing it off is deferred to a
    single-shot timer instead of being waited on here.
    """
    thread.stop()
    if thread.wait(RECYCLE_GRACE_MS):
        thread.deleteLater()
        return

    log.warning(
        "%s worker thread did not exit within %d ms — deferring teardown to keep the UI responsive",
        label,
        RECYCLE_GRACE_MS,
    )

    def _finish() -> None:
        if thread.isRunning():
            log.warning("%s worker thread never exited — terminating", label)
            thread.terminate()
        thread.deleteLater()

    QTimer.singleShot(THREAD_STOP_TIMEOUT_MS, _finish)
