"""Application settings persistence using QSettings."""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable

from PyQt6.QtCore import QSettings

_instance_lock = threading.Lock()


@runtime_checkable
class SettingsProtocol(Protocol):
    """The settings surface the tray application depends on.

    Injected into USBGuardTrayApp so tests can supply an in-memory fake.
    Without the seam, every test run reads (and the tray menu writes) the
    developer's real per-user config — ~/.config/usbguard_gui/general.conf —
    which lets a GUI preference toggled once in the app silently change what
    the suite asserts (e.g. disable_hid_treatment=true skips the whole HID
    pending/lock flow and fails the HID tests).
    """

    def disable_hid_treatment(self) -> bool:
        """Whether the special auto-allow-then-lock HID handling is disabled."""
        ...

    def set_disable_hid_treatment(self, value: bool) -> None:
        """Persist whether the special HID handling is disabled."""
        ...


class Settings:
    """Application settings with QSettings backend.

    Process-wide singleton over the user's persistent settings store; satisfies
    SettingsProtocol.  Prefer injecting a fake in tests rather than letting this
    be constructed there.
    """

    _instance: Settings | None = None
    _settings: QSettings

    def __new__(cls) -> Settings:
        if cls._instance is None:
            with _instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._settings = QSettings("usbguard_gui", "general")
        return cls._instance

    def disable_hid_treatment(self) -> bool:
        return self._settings.value("disable_hid_treatment", False, bool)

    def set_disable_hid_treatment(self, value: bool) -> None:
        self._settings.setValue("disable_hid_treatment", value)
