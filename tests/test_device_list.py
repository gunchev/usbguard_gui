"""Tests for DeviceListWindow refresh logic."""

from __future__ import annotations

import os
from typing import ClassVar

import pytest
from fakes import _FakeClient, _FakeScreensaver
from PyQt6.QtCore import QSettings, Qt

from usbguard_gui import device_list
from usbguard_gui.device import Device, DeviceTarget, Persistence
from usbguard_gui.device_list import COLUMNS, DeviceListWindow, DeviceTableModel

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


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
def screensaver(qapp):
    return _FakeScreensaver()


@pytest.fixture()
def settings_store(tmp_path) -> QSettings:
    """Temp-file-backed geometry store.

    Injected into DeviceListWindow so test runs never overwrite the developer's
    own ~/.config/usbguard_gui/device_list.conf with offscreen-Qt geometry.
    """
    return QSettings(str(tmp_path / "device_list.conf"), QSettings.Format.IniFormat)


@pytest.fixture()
def window(client, screensaver, settings_store, qtbot):
    w = DeviceListWindow(client, screensaver=screensaver, settings=settings_store)
    qtbot.addWidget(w)
    return w


class TestSettingsInjection:
    """The window's geometry store is injected, not hard-wired to the user's config."""

    def test_window_uses_the_injected_store(self, window, settings_store) -> None:
        assert window._settings is settings_store

    def test_store_resolves_under_tmp_not_the_user_config(self, window, tmp_path) -> None:
        assert str(window._settings.fileName()).startswith(str(tmp_path))

    def test_close_saves_geometry_to_the_injected_store(self, window, settings_store, tmp_path, qtbot) -> None:
        window.show()
        qtbot.waitExposed(window)
        window.close()
        settings_store.sync()

        saved = QSettings(str(tmp_path / "device_list.conf"), QSettings.Format.IniFormat)
        assert saved.value("geometry") is not None
        assert saved.value("header_state") is not None


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
    """The window delegates persistence and never removes a rule itself.

    Removal is the client's job and it is identity-keyed (`_rule_identity`),
    so `Once` can only ever reach a rule naming this device -- never a
    hand-written class rule.  The old invariant here said the GUI must never
    remove rules because permanent and temporary ones were indistinguishable
    from the rule string; that predates identity-keyed dedup.  `Once` must
    remove, or the old rule survives the click and silently re-asserts at
    the next reboot.  What still holds is that the window itself never calls
    `remove_rule()`."""

    _PERMANENT_RULE = (
        'allow id 1234:abcd serial "" name "Test Device" '
        'hash "abc123" parent-hash "" via-port "1-1" '
        "with-interface 03:00:00 with-connect-type hotplug"
    )

    def test_temporary_allow_keeps_permanent_rule(self, window, client):
        device = _make_device(1)  # hash "abc123", matches the rule below
        window._apply(device, DeviceTarget.ALLOW, persistence=Persistence.UNCHANGED)

        # Whatever the daemon reports must not be removed:
        client.list_rules_result.emit([(7, self._PERMANENT_RULE)])

        assert client.remove_rule_calls == []
        assert client.apply_policy_calls == [(1, DeviceTarget.ALLOW, Persistence.UNCHANGED)]

    def test_permanent_allow_applies_policy_directly(self, window, client):
        device = _make_device(1)
        window._apply(device, DeviceTarget.ALLOW, persistence=Persistence.ALWAYS)

        assert client.apply_policy_calls == [(1, DeviceTarget.ALLOW, Persistence.ALWAYS)]
        assert client.remove_rule_calls == []


class TestApplyConnectionWarning:
    """When the USBGuard daemon is not connected, _apply() must show a
    warning instead of silently dropping the action — the user must not
    believe their choice was applied."""

    def test_apply_warns_and_does_not_apply_when_disconnected(self, window, client, mocker):
        from PyQt6.QtWidgets import QMessageBox

        client._connected = False
        warn = mocker.patch.object(QMessageBox, "warning")

        window._apply(_make_device(1), DeviceTarget.ALLOW, persistence=Persistence.UNCHANGED)

        assert warn.called
        assert client.apply_policy_calls == []

    def test_apply_without_warning_when_connected(self, window, client, mocker):
        from PyQt6.QtWidgets import QMessageBox

        client._connected = True
        warn = mocker.patch.object(QMessageBox, "warning")

        window._apply(_make_device(1), DeviceTarget.ALLOW, persistence=Persistence.UNCHANGED)

        assert not warn.called
        assert client.apply_policy_calls == [(1, DeviceTarget.ALLOW, Persistence.UNCHANGED)]


class TestApplyLockUnavailable:
    """When screen locking is unavailable, every policy action must be
    refused with a warning — the app cannot uphold its lock-first contract,
    so it does not touch the policy at all."""

    @pytest.mark.parametrize("target,persistence",
                             [(DeviceTarget.ALLOW, Persistence.ALWAYS), (DeviceTarget.ALLOW, Persistence.ONCE),
                              (DeviceTarget.BLOCK, Persistence.ONCE), (DeviceTarget.BLOCK, Persistence.ALWAYS)])
    def test_apply_warns_and_does_not_apply_when_lock_unavailable(self, window, client, screensaver, mocker,
                                                                  target: DeviceTarget,
                                                                  persistence: Persistence) -> None:
        from PyQt6.QtWidgets import QMessageBox

        screensaver._connected = False
        warn = mocker.patch.object(QMessageBox, "warning")

        window._apply(_make_device(1), target, persistence=persistence)

        assert warn.called
        assert client.apply_policy_calls == []

    def test_apply_without_warning_when_lock_available(self, window, client, screensaver, mocker):
        from PyQt6.QtWidgets import QMessageBox

        screensaver._connected = True
        warn = mocker.patch.object(QMessageBox, "warning")

        window._apply(_make_device(1), DeviceTarget.ALLOW, persistence=Persistence.UNCHANGED)

        assert not warn.called
        assert client.apply_policy_calls == [(1, DeviceTarget.ALLOW, Persistence.UNCHANGED)]


class TestDeviceListActionMenu:
    """Slice 8 -- the context menu is the same action set as the dialog.

    Two entry points, one contract.  If the labels or the pairs drift apart,
    the user learns the difference the hard way.
    """

    def test_menu_offers_exactly_the_four_actions(self, window) -> None:
        assert [label for label, _, _ in window._menu_actions(_make_device(1))] == [
            "Allow Always", "Allow Once", "Block Once", "Block Always",
        ]

    @pytest.mark.parametrize(
        ("label", "target", "persistence"),
        [
            ("Allow Always", DeviceTarget.ALLOW, Persistence.ALWAYS),
            ("Allow Once", DeviceTarget.ALLOW, Persistence.ONCE),
            ("Block Once", DeviceTarget.BLOCK, Persistence.ONCE),
            ("Block Always", DeviceTarget.BLOCK, Persistence.ALWAYS),
        ],
    )
    def test_each_item_carries_its_pair(self, window, label, target, persistence) -> None:
        pairs = {name: (tgt, persist) for name, tgt, persist in window._menu_actions(_make_device(1))}
        assert pairs[label] == (target, persistence)


class TestPersistenceColumn:
    """The list must say whether a device's state is durable, and *which kind* of
    durable.

    Before the action set only allows could be durable, so Status's "Allow" vs
    "Temporary" covered every case.  `Block Always` makes a deny durable too, and
    a device sitting on the implicit floor is a third case the old
    permanent-allow-hash set could not express at all.

    The `(wildcard)` suffix is the part that matters most: it marks a rule that
    covers this device without pinning it to this insertion point, and **`Once`
    cannot clear one**.  Without it, "Allow Once" reads as if it took effect while
    a wildcard allow underneath still governs the device -- the exact confusion
    this column exists to remove.
    """

    # The device's own full rule -- what the app actually writes for a permanent
    # decision, topology included.
    _HUB_RAW = ('allow id 2109:2817 serial "000000000" name "USB2.0 Hub" hash "aaa111" '
                'parent-hash "ppp111" via-port "3-3.1.1" with-interface { 09:00:01 } '
                'with-connect-type "unknown"')
    _KB_RAW = ('block id 04f2:b2ea serial "K1" name "Keyboard" hash "bbb222" '
               'parent-hash "ppp222" via-port "1-2" with-interface 03:00:00 '
               'with-connect-type "hotplug"')

    _HUB = Device.from_dbus(54, _HUB_RAW)
    _KB = Device.from_dbus(60, _KB_RAW)

    def _model(self, devices, rules):
        model = DeviceTableModel()
        model.set_devices(devices, rules)
        return model

    def _persistence(self, model, device):
        row = next(i for i in range(model.rowCount()) if model.device_at(i) is device)
        return model.index(row, COLUMNS.index("Persistence")).data()

    def test_persistence_column_sits_next_to_status(self):
        assert COLUMNS[1] == "Status"
        assert COLUMNS[2] == "Persistence"

    def test_device_exact_permanent_allow_is_not_marked_wildcard(self):
        model = self._model([self._HUB], [self._HUB_RAW])
        assert self._persistence(model, self._HUB) == "Permanent allow"

    def test_device_exact_permanent_block_is_not_marked_wildcard(self):
        model = self._model([self._KB], [self._KB_RAW])
        assert self._persistence(model, self._KB) == "Permanent block"

    def test_a_model_wide_rule_is_flagged_as_wildcard(self):
        """`allow id 2109:2817` covers every hub of the model.  Once cannot
        clear it, so the row must not read as a plain permanent allow."""
        model = self._model([self._HUB], ['allow id 2109:2817'])
        assert self._persistence(model, self._HUB) == "Permanent allow (wildcard)"

    def test_a_class_rule_is_flagged_as_wildcard(self):
        """Hand-written admin rules carry no device hash at all."""
        model = self._model([self._KB], ['block with-interface 03:00:00'])
        assert self._persistence(model, self._KB) == "Permanent block (wildcard)"

    def test_a_hash_only_rule_is_still_a_wildcard(self):
        """Pinning the hash names the device but not the insertion point, so it
        follows the device to every port.  Once keys on the full identity and
        cannot clear it either."""
        model = self._model([self._HUB], ['allow id 2109:2817 hash "aaa111"'])
        assert self._persistence(model, self._HUB) == "Permanent allow (wildcard)"

    def test_live_allow_with_no_matching_rule_is_temporary(self):
        model = self._model([self._HUB], ['allow id 1234:5678 hash "nope"'])
        assert self._persistence(model, self._HUB) == "Temporary"

    def test_no_rules_at_all_is_temporary(self):
        model = self._model([self._HUB], [])
        assert self._persistence(model, self._HUB) == "Temporary"

    def test_first_match_wins_in_rule_order(self):
        model = self._model([self._HUB], [self._HUB_RAW, 'block id 2109:2817'])
        assert self._persistence(model, self._HUB) == "Permanent allow"

    def test_an_undecidable_rule_earlier_in_order_reports_unknown(self):
        """We must not call a device temporary when an unreadable rule sits above
        the one we matched -- the daemon may have stopped there."""
        model = self._model([self._HUB], [
            'allow with-something-else "x"',
            self._HUB_RAW,
        ])
        assert self._persistence(model, self._HUB) == "Unknown"

    def test_wildcard_and_device_exact_are_distinguishable(self):
        """The two must never render alike: one is clearable by Once, one is not."""
        exact = self._persistence(self._model([self._HUB], [self._HUB_RAW]), self._HUB)
        wildcard = self._persistence(self._model([self._HUB], ['allow id 2109:2817']), self._HUB)
        assert exact != wildcard

    def test_permanent_and_temporary_block_get_different_colours(self):
        """The old palette had a single colour for block, so `Block Always` and an
        implicit block were indistinguishable at a glance."""
        permanent = self._model([self._KB], [self._KB_RAW])
        temporary = self._model([self._KB], [])
        assert permanent._bg_color(0) != temporary._bg_color(0)

    def test_permanent_and_temporary_allow_get_different_colours(self):
        permanent = self._model([self._HUB], [self._HUB_RAW])
        temporary = self._model([self._HUB], [])
        assert permanent._bg_color(0) != temporary._bg_color(0)


class TestPersistenceIsResolvedOncePerRow:
    """Painting a row must not re-walk the ruleset.

    `set_devices` resolves persistence once per device and stores it -- and then
    `_bg_color` went and called `_resolve_persistence` again, for every cell, for
    both the background and the foreground role.  A 30-device list against a
    100-rule policy cost 60 000 rule matches per repaint instead of the 3 000 the
    precomputation had already paid for, all of it on the GUI thread.
    """

    _RULES: ClassVar[list[str]] = [f"allow id 1234:{n:04x}" for n in range(100)]

    def _devices(self, count: int) -> list[Device]:
        return [Device.from_dbus(n, f'block id 9999:{n:04x} serial "S{n}" name "d{n}" hash "h{n}" '
                                 f'parent-hash "" via-port "1-{n}" with-interface 08:06:50 '
                                 f'with-connect-type "hotplug"')
                for n in range(count)]

    def _repaint(self, model: DeviceTableModel) -> None:
        for row in range(model.rowCount()):
            for col in range(len(COLUMNS)):
                index = model.index(row, col)
                for role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.BackgroundRole,
                             Qt.ItemDataRole.ForegroundRole):
                    model.data(index, role)

    def test_a_repaint_matches_no_rules_at_all(self, mocker) -> None:
        model = DeviceTableModel()
        model.set_devices(self._devices(30), self._RULES)
        spy = mocker.spy(device_list, "rule_matches_device")

        self._repaint(model)

        assert spy.call_count == 0, "every row was resolved in set_devices; painting must be a lookup"

    def test_set_devices_resolves_each_device_exactly_once(self, mocker) -> None:
        model = DeviceTableModel()
        spy = mocker.spy(device_list, "rule_matches_device")

        model.set_devices(self._devices(30), self._RULES)

        assert spy.call_count == 30 * len(self._RULES), "one pass over the ruleset per device, no more"

    def test_the_colours_survive_the_precomputation(self) -> None:
        """The cache must not change what a row looks like."""
        hub_raw = ('allow id 2109:2817 serial "000000000" name "USB2.0 Hub" hash "aaa111" '
                   'parent-hash "ppp111" via-port "3-3.1.1" with-interface { 09:00:01 } '
                   'with-connect-type "unknown"')
        hub = Device.from_dbus(54, hub_raw)
        permanent = DeviceTableModel()
        permanent.set_devices([hub], [hub_raw])
        temporary = DeviceTableModel()
        temporary.set_devices([hub], [])

        assert permanent.index(0, 0).data(Qt.ItemDataRole.BackgroundRole) is not None
        assert (permanent.index(0, 0).data(Qt.ItemDataRole.BackgroundRole)
                != temporary.index(0, 0).data(Qt.ItemDataRole.BackgroundRole))

    def test_a_row_past_the_end_has_no_colour(self) -> None:
        model = DeviceTableModel()
        model.set_devices(self._devices(1), [])

        assert model._bg_color(5) is None
