"""Recording stand-ins shared by the test suite.

The tray app, the action dialog and the device list window all talk to a
USBGuard client, a screensaver monitor and the settings store; each test
module used to keep its own private copy of those fakes.  These are the union
of the three variants — the full signal set and recorders from the tray tests,
the `connected=` constructor flag from the dialog tests — so one import serves
every file.  The classes live here rather than in `conftest.py` because the
test files construct them directly with arguments (`_FakeClient(connected=False)`),
which a fixture alone cannot carry.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from usbguard_gui.device import DeviceTarget, Persistence


class _FakeClient(QObject):
    """Minimal stand-in for USBGuardClient that records policy calls."""

    device_presence_changed = pyqtSignal(int, int, int, str, dict)
    device_policy_changed = pyqtSignal(int, int, int, str, int, dict)
    connection_changed = pyqtSignal(bool)
    list_devices_result = pyqtSignal(object)
    list_devices_correlated = pyqtSignal(int, list)
    list_rules_result = pyqtSignal(list)
    remove_rule_result = pyqtSignal(bool)
    permanent_write_failed = pyqtSignal(int, str, str)
    permanent_clear_failed = pyqtSignal(int, str, str, bool)
    temporary_apply_failed = pyqtSignal(int, str, str, bool)
    permanent_rule_remains = pyqtSignal(int, str, str)

    def __init__(self, connected: bool = True) -> None:
        super().__init__()
        self._connected = connected
        self.apply_policy_calls: list[tuple] = []
        self.persist_rule_calls: list[tuple] = []
        self.apply_policy_rules: list[str | None] = []
        self.remove_rule_calls: list[int] = []
        self.list_devices_calls: int = 0
        self.list_rules_calls: int = 0
        self.fetch_devices_calls: list[int] = []

    @property
    def connected(self) -> bool:
        return self._connected

    def list_devices(self, query: str = "match") -> None:
        self.list_devices_calls += 1

    def fetch_devices(self, request_id: int, query: str = "match") -> None:
        self.fetch_devices_calls.append(request_id)

    def list_rules(self, label: str = "") -> None:
        self.list_rules_calls += 1

    def apply_device_policy(self, device_id: int, target: DeviceTarget,
                            persistence: Persistence = Persistence.UNCHANGED,
                            device_rule: str | None = None) -> None:
        self.apply_policy_calls.append((device_id, target, persistence))
        self.apply_policy_rules.append(device_rule)

    def persist_rule(self, device_id: int, target: DeviceTarget, device_rule: str) -> None:
        self.persist_rule_calls.append((device_id, target, device_rule))

    def remove_rule(self, rule_id: int) -> None:
        self.remove_rule_calls.append(rule_id)

    def connect(self) -> bool:
        return True

    def stop(self) -> None:
        pass


class _FakeScreensaver(QObject):
    """Minimal stand-in for ScreensaverMonitor (lock availability)."""

    active_changed = pyqtSignal(bool)
    inhibit_changed = pyqtSignal(bool)
    connection_changed = pyqtSignal(bool)

    def __init__(self, connected: bool = True) -> None:
        super().__init__()
        self.lock_calls: int = 0
        self._active: bool = False
        self._inhibited: bool = False
        self._connected: bool = connected

    @property
    def active(self) -> bool:
        return self._active

    @property
    def inhibited(self) -> bool:
        return self._inhibited

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        return True

    def stop(self) -> None:
        pass

    def lock(self) -> None:
        self.lock_calls += 1


class _FakeSettings:
    """In-memory SettingsProtocol implementation.

    Injected into USBGuardTrayApp so no test ever reads or writes the real
    per-user config (~/.config/usbguard_gui/general.conf).  A preference
    toggled in the running app used to flip HID test outcomes here; with the
    seam, the suite controls its own settings.
    """

    def __init__(self, disable_hid_treatment: bool = False) -> None:
        self._disable_hid: bool = disable_hid_treatment
        self.write_calls: list[bool] = []

    def disable_hid_treatment(self) -> bool:
        return self._disable_hid

    def set_disable_hid_treatment(self, value: bool) -> None:
        self.write_calls.append(value)
        self._disable_hid = value
