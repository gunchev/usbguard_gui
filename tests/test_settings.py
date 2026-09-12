"""Tests for the settings seam (SettingsProtocol / Settings)."""

from __future__ import annotations

import os

from usbguard_gui.settings import Settings, SettingsProtocol

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestSettingsProtocol:
    """The protocol is what the tray app is typed against; Settings must satisfy it."""

    def test_settings_satisfies_the_protocol(self) -> None:
        assert isinstance(Settings(), SettingsProtocol)

    def test_settings_is_a_process_singleton(self) -> None:
        assert Settings() is Settings()

    def test_non_conforming_object_is_rejected(self) -> None:
        class _NotSettings:
            pass

        assert not isinstance(_NotSettings(), SettingsProtocol)
