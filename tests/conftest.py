"""Fixtures shared across the test suite.

The fakes themselves live in `fakes.py` (the test files construct them with
arguments); everything buildable with defaults is provided here so no test
module defines its own wrapper.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fakes import _FakeClient, _FakeScreensaver, _FakeSettings

from usbguard_gui.device import Device, DeviceTarget, Persistence, PresenceEvent


@pytest.fixture()
def fake_client(qapp) -> _FakeClient:
    return _FakeClient()


@pytest.fixture()
def fake_screensaver() -> _FakeScreensaver:
    return _FakeScreensaver()


@pytest.fixture()
def fake_settings() -> _FakeSettings:
    return _FakeSettings()


KEYBOARD_RULE = (
    'block id 046d:c52b serial "KB1" name "Keyboard" hash "kbhash1" '
    'parent-hash "" via-port "1-3" with-interface 03:01:01 with-connect-type "hotplug"'
)
IR_RULE = (
    'block id 045c:0131 serial "IR1" name "Smart IR Blaster" hash "irhash1" '
    'parent-hash "" via-port "1-2" with-interface ff:00:00 with-connect-type "hotplug"'
)


@pytest.fixture()
def tray_app(qapp, fake_client, fake_screensaver, fake_settings, qtbot):
    from usbguard_gui.app import USBGuardTrayApp

    with (
        patch("usbguard_gui.app.USBGuardClient", return_value=fake_client),
        patch("usbguard_gui.app.ScreensaverMonitor", return_value=fake_screensaver),
    ):
        app = USBGuardTrayApp(qapp, settings=fake_settings)
    return app


@pytest.fixture()
def queued_decision(tray_app, mocker):
    """Leave a decision in the queue the way the user does: decide while away.

    A factory rather than a fixed state, because the tests vary the target, the
    persistence and the device, and each wants the tray mock back with its call
    history already cleared -- opening the dialog and the REMOVE that precedes
    the click raise notices of their own that would otherwise pollute the
    assertions about the insertion.
    """
    def _make(target: DeviceTarget, persistence: Persistence = Persistence.ONCE,
              rule: str = KEYBOARD_RULE) -> MagicMock:
        show = mocker.patch.object(tray_app._tray, "showMessage")
        tray_app._show_device_dialog(Device.from_dbus(292, rule))
        dialog = tray_app._open_dialogs[292]
        tray_app._on_device_presence_changed(292, int(PresenceEvent.REMOVE), int(DeviceTarget.BLOCK), rule, {})
        dialog._choose(target, persistence)
        assert tray_app._pending_decisions, "precondition: the decision is queued"
        show.reset_mock()
        return show

    return _make
