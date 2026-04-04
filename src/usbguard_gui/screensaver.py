"""Freedesktop screensaver D-Bus integration for screen lock awareness."""

from __future__ import annotations

import contextlib
import logging

from dasbus.connection import SessionMessageBus
from dasbus.error import DBusError
from PyQt6.QtCore import QObject, pyqtSignal

log = logging.getLogger(__name__)

SCREENSAVER_BUS_NAME = "org.freedesktop.ScreenSaver"
SCREENSAVER_PATH = "/org/freedesktop/ScreenSaver"


class ScreensaverMonitor(QObject):
    """Monitor the freedesktop screensaver (works on KDE, GNOME, etc.)."""

    active_changed = pyqtSignal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._bus = SessionMessageBus()
        self._proxy = None
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def connect(self) -> bool:
        """Connect to the screensaver D-Bus service. Returns True on success."""
        try:
            self._proxy = self._bus.get_proxy(SCREENSAVER_BUS_NAME, SCREENSAVER_PATH)
            self._proxy.ActiveChanged.connect(self._on_active_changed)
            log.info("Connected to freedesktop ScreenSaver D-Bus")
            return True
        except DBusError as e:
            log.warning("Could not connect to screensaver D-Bus: %s", e)
            return False

    def lock(self) -> None:
        """Lock the screen."""
        if not self._proxy:
            return
        with contextlib.suppress(DBusError):
            self._proxy.Lock()

    def _on_active_changed(self, active: bool) -> None:
        self._active = bool(active)
        log.debug("Screensaver active: %s", self._active)
        self.active_changed.emit(self._active)
