"""Freedesktop screensaver D-Bus integration for screen lock awareness."""

from __future__ import annotations

import logging
import os

from dbus_fast import BusType, DBusError
from dbus_fast.glib import MessageBus
from dbus_fast.glib.proxy_object import ProxyInterface, ProxyObject
from PyQt6.QtCore import QObject, pyqtSignal

log = logging.getLogger(__name__)

SCREENSAVER_BUS_NAME = "org.freedesktop.ScreenSaver"
SCREENSAVER_PATH = "/org/freedesktop/ScreenSaver"
SCREENSAVER_IFACE = "org.freedesktop.ScreenSaver"


def _get_introspection(filename: str) -> str:
    module_dir = os.path.dirname(__file__)
    path = os.path.join(module_dir, "introspection", filename)
    with open(path) as f:
        return f.read()


class ScreensaverMonitor(QObject):
    """Monitor the freedesktop screensaver (works on KDE, GNOME, etc.)."""

    active_changed = pyqtSignal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._bus: MessageBus | None = None
        self._proxy: ProxyInterface | None = None
        self._proxy_obj: ProxyObject | None = None
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def connect(self) -> bool:
        """Connect to the screensaver D-Bus service. Returns True on success."""
        try:
            self._bus = MessageBus(bus_type=BusType.SESSION)
            self._bus.connect()

            introspection = _get_introspection("org.freedesktop.ScreenSaver.xml")
            self._proxy_obj = self._bus.get_proxy_object(SCREENSAVER_BUS_NAME, SCREENSAVER_PATH, introspection)
            self._proxy = self._proxy_obj.get_interface(SCREENSAVER_IFACE)
            self._proxy.on_active_changed(self._on_active_changed)
            log.info("Connected to freedesktop ScreenSaver D-Bus")
            return True
        except DBusError as e:
            log.warning("Could not connect to screensaver D-Bus: %s", e)
            return False
        except Exception as e:
            log.warning("Unexpected error connecting to screensaver D-Bus: %s", e)
            return False

    def lock(self) -> None:
        """Lock the screen."""
        if not self._proxy:
            log.warning("Cannot lock screen: screensaver proxy not available")
            return
        try:
            self._proxy.call_lock()
        except DBusError as e:
            log.warning("Failed to lock screen: %s", e)

    def _on_active_changed(self, active: bool) -> None:
        self._active = bool(active)
        log.debug("Screensaver active: %s", self._active)
        self.active_changed.emit(self._active)
