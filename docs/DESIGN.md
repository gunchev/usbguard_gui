# Architecture: QThread + asyncio D-Bus Integration

usbguard_gui uses **dbus-fast** (pure-Python asyncio) for all D-Bus communication,
integrated with PyQt6 via dedicated QThreads that each own an asyncio event loop.
This avoids any GLib / PyGObject dependency.

## Why dbus-fast + QThread

The original implementation used `dasbus + PyGObject`, which dragged in GLib's type
introspection layer even though the application is pure Qt.  Replacing it with
dbus-fast eliminates that dependency while keeping the asyncio model isolated from
the Qt event loop — each D-Bus subsystem runs in its own thread.

## Dependencies

```toml
dependencies = [
    "PyQt6>=6.5",
    "dbus-fast>=1.0",
]
```

## Module Overview

| Module             | Class(es)                                   | Role                                       |
|--------------------|---------------------------------------------|--------------------------------------------|
| `dbus_common.py`   | `AsyncWorkerThread`, `stop_worker_thread()` | Shared QThread + asyncio worker base, introspection loading |
| `dbus_client.py`   | `_DBusThread`, `USBGuardClient`             | USBGuard system-bus D-Bus client           |
| `screensaver.py`   | `_ScreensaverThread`, `ScreensaverMonitor`  | ScreenSaver + logind session-bus monitor   |
| `device.py`        | `Device`, `DeviceTarget`, `parse_device_rule` | Device model and rule-string parsing    |
| `app.py`           | `USBGuardTrayApp`                           | Qt event loop, tray, routing               |
| `device_dialog.py` | `DeviceActionDialog`                        | Per-device Allow / Block / Reject prompt   |
| `device_list.py`   | `DeviceListWindow` (+ table models)         | Device list window                         |
| `settings.py`      | `Settings`                                  | QSettings wrapper                          |

## Architecture

Each subsystem follows the same two-layer pattern.  Both worker threads
subclass `AsyncWorkerThread` (`dbus_common.py`), which owns the event loop,
`_schedule()` and `stop()`.

```
Qt main thread                          Worker QThread
──────────────────────────────          ──────────────────────────────────────
USBGuardClient (QObject)                _DBusThread (AsyncWorkerThread)
 ├─ Public API methods                   ├─ asyncio event loop (run_until_complete)
 ├─ Forwards calls via _schedule()       ├─ dbus-fast MessageBus (system bus)
 └─ Re-emits signals to GUI             ├─ ProxyInterface for Devices + Policy
                                         ├─ Subscribes to D-Bus signals
                                         └─ Watches org.usbguard1 via NameOwnerChanged
ScreensaverMonitor (QObject)
 ├─ Public API methods
 ├─ Forwards calls via _schedule()      _ScreensaverThread (AsyncWorkerThread)
 └─ Re-emits signals to GUI             ├─ asyncio event loop (run_until_complete)
                                         ├─ dbus-fast MessageBus (session bus)
                                         ├─ ProxyInterface for ScreenSaver
                                         ├─ Subscribes to ActiveChanged signal
                                         ├─ Watches org.freedesktop.ScreenSaver via
                                            NameOwnerChanged
                                         ├─ MessageBus (system bus) for logind
                                         └─ Polls logind ListInhibitors for
                                            idle-block inhibitors
```

The `NameOwnerChanged` watches give proactive service-loss detection: a
daemon or screen-locker crash/restart flips the connection state immediately
instead of on the next call that happens to fail.

## Thread Communication

Commands flow Qt → worker via `loop.call_soon_threadsafe`:

```python
def _schedule(self, coro: Coroutine[Any, Any, Any]) -> None:
    if self._loop and self._running:
        self._loop.call_soon_threadsafe(asyncio.ensure_future, coro)
```

Results flow worker → Qt as Qt signals, connected in the public facade's
`__init__`.  No shared mutable state crosses the thread boundary.

Shutdown goes through `stop_worker_thread()` (`dbus_common.py`): bounded
`wait(THREAD_STOP_TIMEOUT_MS)` with a `QThread.terminate()` fallback, so a
worker stuck in `MessageBus.connect()` cannot hang `_quit()`.

## Introspection XML

dbus-fast requires interface introspection XML to generate proxy objects.
The XMLs are bundled as package data and pre-loaded at import time (via
`dbus_common.get_introspection()`) so the asyncio event loop never blocks
on file I/O:

```
src/usbguard_gui/introspection/
├── org.usbguard.Devices1.xml
├── org.usbguard.Policy1.xml
├── org.freedesktop.ScreenSaver.xml
└── org.freedesktop.DBus.xml   (NameOwnerChanged watches)
```

## USBGuardClient Signals

| Signal                    | Parameters                              | Emitted by                   |
|---------------------------|-----------------------------------------|------------------------------|
| `connection_changed`      | `bool`                                  | connect / disconnect / `org.usbguard1` NameOwnerChanged |
| `device_presence_changed` | `int, int, int, str, dict`              | D-Bus DevicePresenceChanged  |
| `device_policy_changed`   | `int, int, int, str, int, dict`         | D-Bus DevicePolicyChanged    |
| `list_devices_result`     | `list[Device]`                          | result of `list_devices()`   |
| `list_rules_result`       | `list[tuple[int, str]]`                 | result of `list_rules()`     |
| `remove_rule_result`      | `bool`                                  | result of `remove_rule()`    |

`apply_device_policy()` is fire-and-forget (no result signal); callers follow
up with `list_rules()` to confirm the new policy state.

## ScreensaverMonitor Signals

| Signal              | Parameters | Emitted by                                        |
|---------------------|------------|---------------------------------------------------|
| `connection_changed`| `bool`     | ScreenSaver service reachability (connect / loss / re-appearance via NameOwnerChanged) |
| `active_changed`    | `bool`     | ScreenSaver ActiveChanged D-Bus signal            |
| `inhibit_changed`   | `bool`     | logind ListInhibitors poll (idle-block mode)      |

While `connection_changed` is `False` (screen locking unavailable), the app
disables **all** allow/deny actions: allowing a keyboard without the ability
to lock first would hand an attached-device attacker an unlocked session.

## dasbus → dbus-fast API mapping

| dasbus                        | dbus-fast                                                      |
|-------------------------------|----------------------------------------------------------------|
| `SystemMessageBus()`          | `MessageBus(bus_type=BusType.SYSTEM)`                          |
| `bus.get_proxy(name, path)`   | `bus.get_proxy_object(name, path, xml).get_interface(iface)`   |
| `proxy.listDevices()`         | `proxy.call_list_devices()`                                    |
| `proxy.Signal.connect(cb)`    | `proxy.on_signal(cb)`                                          |
| `proxy.Signal.disconnect(cb)` | `proxy.off_signal(cb)`                                         |
| `DBusError.error_name`        | `DBusError.type`                                               |

## Error Handling

`_is_permission_error` inspects `DBusError.type` for polkit authorization
errors and logs them with a specific hint about installing a polkit rule,
rather than treating them as connection failures.

`_is_connection_error` narrows which failures flip the connection state:
only errors that indicate a broken transport/session (`ServiceUnknown`,
`NameHasNoOwner`, `NoReply`, `Disconnected`, `Timeout`, `IOError`,
`NoServer`, `NoNetwork`) mark the daemon as lost and trigger a full
reconnect (worker-thread teardown + rebuild).  Ordinary per-call failures
(e.g. applying a policy to a device that was unplugged a moment earlier) are
logged and swallowed without touching connection state.

## Tooling

| Tool      | Purpose                        | Command                    |
|-----------|--------------------------------|----------------------------|
| isort     | Import sorting                 | `make lint` / `make format`|
| ruff      | Lint (E/F/W/UP/B/SIM/RUF/I)   | `make lint`                |
| autopep8  | Code formatting                | `make lint` / `make format`|
| pyright   | Static type checking           | `make typecheck`           |
| pytest    | Tests (226 tests)              | `make test`                |
