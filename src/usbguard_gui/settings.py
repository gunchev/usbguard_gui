"""Application settings persistence using QSettings."""

from __future__ import annotations

import threading

from PyQt6.QtCore import QSettings

_instance_lock = threading.Lock()


class Settings:
    """Application settings with QSettings backend."""

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
