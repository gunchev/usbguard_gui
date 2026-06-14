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

| Module           | Class(es)                             | Role                                  |
|------------------|---------------------------------------|---------------------------------------|
| `dbus_client.py` | `_DBusThread`, `USBGuardClient`       | USBGuard system-bus D-Bus client      |
| `screensaver.py` | `_ScreensaverThread`, `ScreensaverMonitor` | Screensaver + logind session-bus |
| `app.py`         | `USBGuardTrayApp`                     | Qt event loop, tray, routing          |

## Architecture

Each subsystem follows the same two-layer pattern:

```
Qt main thread                          Worker QThread
──────────────────────────────          ──────────────────────────────────────
USBGuardClient (QObject)                _DBusThread (QThread)
 ├─ Public API methods                   ├─ asyncio event loop (run_until_complete)
 ├─ Forwards calls via _schedule()       ├─ dbus-fast MessageBus (system bus)
 └─ Re-emits signals to GUI             ├─ ProxyInterface for Devices + Policy
                                         ├─ Subscribes to D-Bus signals
ScreensaverMonitor (QObject)             └─ Emits Qt signals for results / events
 ├─ Public API methods
 ├─ Forwards calls via _schedule()      _ScreensaverThread (QThread)
 └─ Re-emits signals to GUI             ├─ asyncio event loop (run_until_complete)
                                         ├─ dbus-fast MessageBus (session bus)
                                         ├─ ProxyInterface for ScreenSaver
                                         ├─ Subscribes to ActiveChanged signal
                                         ├─ MessageBus (system bus) for logind
                                         └─ Polls logind ListInhibitors for
                                            idle-block inhibitors
```

## Thread Communication

Commands flow Qt → worker via `loop.call_soon_threadsafe`:

```python
def _schedule(self, coro: Coroutine[Any, Any, Any]) -> None:
    if self._loop and self._running:
        self._loop.call_soon_threadsafe(asyncio.ensure_future, coro)
```

Results flow worker → Qt as Qt signals, connected in the public facade's
`__init__`.  No shared mutable state crosses the thread boundary.

## Introspection XML

dbus-fast requires interface introspection XML to generate proxy objects.
The XMLs are bundled as package data and pre-loaded at import time so the
asyncio event loop never blocks on file I/O:

```
src/usbguard_gui/introspection/
├── org.usbguard.Devices1.xml
├── org.usbguard.Policy1.xml
└── org.freedesktop.ScreenSaver.xml
```

## USBGuardClient Signals

| Signal                    | Parameters                              | Emitted by                   |
|---------------------------|-----------------------------------------|------------------------------|
| `connection_changed`      | `bool`                                  | connect / disconnect events  |
| `device_presence_changed` | `int, int, int, str, dict`              | D-Bus DevicePresenceChanged  |
| `device_policy_changed`   | `int, int, int, str, int, dict`         | D-Bus DevicePolicyChanged    |
| `list_devices_result`     | `list[Device]`                          | result of `list_devices()`   |
| `list_rules_result`       | `list[tuple[int, str]]`                 | result of `list_rules()`     |
| `remove_rule_result`      | `bool`                                  | result of `remove_rule()`    |

`apply_device_policy()` is fire-and-forget (no result signal); callers follow
up with `list_rules()` to confirm the new policy state.

## ScreensaverMonitor Signals

| Signal           | Parameters | Emitted by                                  |
|------------------|------------|---------------------------------------------|
| `active_changed` | `bool`     | ScreenSaver ActiveChanged D-Bus signal      |
| `inhibit_changed`| `bool`     | logind ListInhibitors poll (idle-block mode)|

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

## Tooling

| Tool      | Purpose                        | Command                    |
|-----------|--------------------------------|----------------------------|
| isort     | Import sorting                 | `make lint` / `make format`|
| ruff      | Lint (E/F/W/UP/B/SIM/RUF)      | `make lint`                |
| autopep8  | Code formatting                | `make lint` / `make format`|
| pyright   | Static type checking           | `make typecheck`           |
| pytest    | Tests (145 tests, ~80% cov)    | `make test`                |
