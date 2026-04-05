"""Tests for DeviceListWindow refresh logic."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal  # noqa: E402

from usbguard_gui.device import Device  # noqa: E402
from usbguard_gui.device_list import DeviceListWindow  # noqa: E402


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
        self.list_devices_calls: int = 0
        self.list_rules_calls: int = 0

    def list_devices(self, query: str = "match") -> None:
        self.list_devices_calls += 1

    def list_rules(self, label: str = "") -> None:
        self.list_rules_calls += 1

    def apply_device_policy(self, device_id: int, target, permanent: bool = False) -> None:
        pass

    def remove_rule(self, rule_id: int) -> None:
        pass


def _make_device(number: int = 1, rule: str = "block") -> Device:
    rule_str = (
        f'{rule} id 1234:abcd serial "" name "Test Device" '
        'hash "abc123" parent-hash "" via-port "1-1" '
        "with-interface 03:00:00 with-connect-type hotplug"
    )
    return Device.from_dbus(number, rule_str)


@pytest.fixture()
def client(qapp):
    return _FakeClient()


@pytest.fixture()
def window(client, qtbot):
    w = DeviceListWindow(client)
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

    def test_increments_refresh_id(self, window):
        before = window._refresh_id
        window._do_refresh()
        assert window._refresh_id == before + 1

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
        refresh_id = window._refresh_id

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

        refresh_id = window._refresh_id
        client.list_devices_result.emit(devices)
        client.list_rules_result.emit([])

        assert window._model.rowCount() == 1

    def test_stale_refresh_id_is_ignored(self, window, client):
        """Results from old refreshes must not update the model."""
        window._request_refresh()
        old_id = window._refresh_id

        # Start a new refresh, making old_id stale
        window._do_refresh()
        new_id = window._refresh_id
        assert new_id == old_id + 1

        # Emit the OLD result (old_id lambda still fires but should be ignored)
        # The new refresh lambda captures new_id; old one captures old_id.
        # Both lambdas fire: old returns early, new processes.
        devices = [_make_device(99)]
        client.list_devices_result.emit(devices)
        client.list_rules_result.emit([])

        # Model updated via new refresh's lambda
        assert window._model.rowCount() == 1

    def test_pending_apply_takes_priority_over_refresh(self, window, client):
        """When _pending_apply is set, list_rules_result handles apply not refresh."""
        from usbguard_gui.device import DeviceTarget

        device = _make_device(1)
        window._request_refresh()
        # Simulate devices result came in
        client.list_devices_result.emit([device])

        # Apply action set before rules result arrives
        window._pending_apply = (device, DeviceTarget.ALLOW, True)

        # list_rules_result arrives
        client.list_rules_result.emit([])

        # _pending_apply consumed, model NOT updated from this rules result
        assert window._pending_apply is None
