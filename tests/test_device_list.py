"""Tests for DeviceListWindow refresh logic."""

from __future__ import annotations

import os

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from usbguard_gui.device import Device, DeviceTarget
from usbguard_gui.device_list import DeviceListWindow

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class _FakeClient(QObject):
    """Minimal stand-in for USBGuardClient that records calls and emits results on demand."""

    device_presence_changed = pyqtSignal(int, int, int, str, dict)
    device_policy_changed = pyqtSignal(int, int, int, str, int, dict)
    connection_changed = pyqtSignal(bool)
    list_devices_result = pyqtSignal(list)
    apply_policy_result = pyqtSignal(object)
    list_rules_result = pyqtSignal(list)
    remove_rule_result = pyqtSignal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._connected: bool = True
        self.list_devices_calls: int = 0
        self.list_rules_calls: int = 0
        self.apply_policy_calls: list[tuple] = []
        self.remove_rule_calls: list[int] = []

    @property
    def connected(self) -> bool:
        return self._connected

    def list_devices(self, query: str = "match") -> None:
        self.list_devices_calls += 1

    def list_rules(self, label: str = "") -> None:
        self.list_rules_calls += 1

    def apply_device_policy(self, device_id: int, target, permanent: bool = False) -> None:
        self.apply_policy_calls.append((device_id, target, permanent))

    def remove_rule(self, rule_id: int) -> None:
        self.remove_rule_calls.append(rule_id)


def _make_device(number: int = 1, rule: str = "block") -> Device:
    rule_str = (
        f'{rule} id 1234:abcd serial "" name "Test Device" '
        'hash "abc123" parent-hash "" via-port "1-1" '
        "with-interface 03:00:00 with-connect-type hotplug"
    )
    return Device.from_dbus(number, rule_str)


class _FakeScreensaver(QObject):
    """Minimal stand-in for ScreensaverMonitor (lock availability)."""

    connection_changed = pyqtSignal(bool)

    def __init__(self, connected: bool = True) -> None:
        super().__init__()
        self._connected = connected

    @property
    def connected(self) -> bool:
        return self._connected


@pytest.fixture()
def client(qapp):
    return _FakeClient()


@pytest.fixture()
def screensaver(qapp):
    return _FakeScreensaver()


@pytest.fixture()
def window(client, screensaver, qtbot):
    w = DeviceListWindow(client, screensaver=screensaver)
    qtbot.addWidget(w)
    return w


class TestDoRefreshSetsFlag:
    """_do_refresh() must always mark a refresh as pending."""

    def test_sets_refresh_pending_on_first_call(self, window):
        window._refresh_pending = False
        window._do_refresh()
        assert window._refresh_pending is True

    def test_sets_refresh_pending_even_if_cleared(self, window, client):
        # Simulate a completed refresh clearing the flag
        window._refresh_pending = False
        window._do_refresh()
        assert window._refresh_pending is True

    def test_calls_list_devices(self, window, client):
        before = client.list_devices_calls
        window._do_refresh()
        assert client.list_devices_calls == before + 1


class TestRefreshFlow:
    """Full refresh flow: list_devices_result → list_rules_result → model update."""

    def test_model_populated_after_signals(self, window, client):
        devices = [_make_device(1), _make_device(2)]
        rules: list[tuple[int, str]] = []

        window._request_refresh()

        # Simulate async results arriving
        client.list_devices_result.emit(devices)
        client.list_rules_result.emit(rules)

        assert window._model.rowCount() == 2

    def test_model_empty_when_no_devices(self, window, client):
        window._request_refresh()
        client.list_devices_result.emit([])
        client.list_rules_result.emit([])
        assert window._model.rowCount() == 0

    def test_timer_triggered_refresh_updates_model(self, window, client):
        """Refresh triggered by _schedule_refresh() (timer path) must update model.

        Regression: _do_refresh() didn't set _refresh_pending=True, so a timer-fired
        refresh after the initial refresh completed left the model stale.
        """
        devices = [_make_device(3)]

        # Simulate: initial refresh already completed, flag is cleared
        window._refresh_pending = False

        # Device event triggers schedule_refresh → timer → _do_refresh
        window._schedule_refresh()  # sets _refresh_pending=True, starts timer
        # Simulate timer firing directly
        window._do_refresh()

        client.list_devices_result.emit(devices)
        client.list_rules_result.emit([])

        assert window._model.rowCount() == 1

    def test_rapid_refreshes_still_update_model(self, window, client):
        """Repeated _do_refresh() calls must still update the model when results arrive."""
        window._request_refresh()
        window._do_refresh()  # superseding refresh

        devices = [_make_device(99)]
        client.list_devices_result.emit(devices)
        client.list_rules_result.emit([])

        assert window._model.rowCount() == 1

    def test_do_refresh_does_not_leak_signal_connections(self, window, client):
        """Repeated refreshes must not add new signal connections.

        Previously each _do_refresh() connected a fresh lambda to
        list_devices_result / list_rules_result and never disconnected it,
        leaking one connection per refresh. Connections should be made once
        in __init__ and stay at 1 no matter how many times we refresh.
        """
        before_devices = client.receivers(client.list_devices_result)
        before_rules = client.receivers(client.list_rules_result)

        for _ in range(5):
            window._do_refresh()

        assert client.receivers(client.list_devices_result) == before_devices
        assert client.receivers(client.list_rules_result) == before_rules

class TestApplyDoesNotRemoveRules:
    """Regression: applying 'Allow (Temporary)' used to remove every allow
    rule matching the device hash — including the user's permanent rule —
    silently revoking persistent authorization (after the next replug or
    reboot the device would be blocked again).  Permanent and temporary
    rules are indistinguishable from the rule string, so the GUI must not
    remove rules at all: re-applying a temporary allow is harmless because
    USBGuard prepends it, so it wins evaluation order."""

    _PERMANENT_RULE = (
        'allow id 1234:abcd serial "" name "Test Device" '
        'hash "abc123" parent-hash "" via-port "1-1" '
        "with-interface 03:00:00 with-connect-type hotplug"
    )

    def test_temporary_allow_keeps_permanent_rule(self, window, client):
        device = _make_device(1)  # hash "abc123", matches the rule below
        window._apply(device, DeviceTarget.ALLOW, permanent=False)

        # Whatever the daemon reports must not be removed:
        client.list_rules_result.emit([(7, self._PERMANENT_RULE)])

        assert client.remove_rule_calls == []
        assert client.apply_policy_calls == [(1, DeviceTarget.ALLOW, False)]

    def test_permanent_allow_applies_policy_directly(self, window, client):
        device = _make_device(1)
        window._apply(device, DeviceTarget.ALLOW, permanent=True)

        assert client.apply_policy_calls == [(1, DeviceTarget.ALLOW, True)]
        assert client.remove_rule_calls == []


class TestApplyConnectionWarning:
    """When the USBGuard daemon is not connected, _apply() must show a
    warning instead of silently dropping the action — the user must not
    believe their choice was applied."""

    def test_apply_warns_and_does_not_apply_when_disconnected(self, window, client, mocker):
        from PyQt6.QtWidgets import QMessageBox

        client._connected = False
        warn = mocker.patch.object(QMessageBox, "warning")

        window._apply(_make_device(1), DeviceTarget.ALLOW, permanent=False)

        assert warn.called
        assert client.apply_policy_calls == []

    def test_apply_without_warning_when_connected(self, window, client, mocker):
        from PyQt6.QtWidgets import QMessageBox

        client._connected = True
        warn = mocker.patch.object(QMessageBox, "warning")

        window._apply(_make_device(1), DeviceTarget.ALLOW, permanent=False)

        assert not warn.called
        assert client.apply_policy_calls == [(1, DeviceTarget.ALLOW, False)]


class TestApplyLockUnavailable:
    """When screen locking is unavailable, every policy action must be
    refused with a warning — the app cannot uphold its lock-first contract,
    so it does not touch the policy at all."""

    @pytest.mark.parametrize("target,permanent", [(DeviceTarget.ALLOW, True), (DeviceTarget.ALLOW, False),
                                                  (DeviceTarget.BLOCK, False), (DeviceTarget.REJECT, False)])
    def test_apply_warns_and_does_not_apply_when_lock_unavailable(
        self, window, client, screensaver, mocker, target: DeviceTarget, permanent: bool
    ) -> None:
        from PyQt6.QtWidgets import QMessageBox

        screensaver._connected = False
        warn = mocker.patch.object(QMessageBox, "warning")

        window._apply(_make_device(1), target, permanent=permanent)

        assert warn.called
        assert client.apply_policy_calls == []

    def test_apply_without_warning_when_lock_available(self, window, client, screensaver, mocker):
        from PyQt6.QtWidgets import QMessageBox

        screensaver._connected = True
        warn = mocker.patch.object(QMessageBox, "warning")

        window._apply(_make_device(1), DeviceTarget.ALLOW, permanent=False)

        assert not warn.called
        assert client.apply_policy_calls == [(1, DeviceTarget.ALLOW, False)]
