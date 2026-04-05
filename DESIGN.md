# Replacing PyGObject with dbus-fast

This document describes the migration from `dasbus + PyGObject` to `dbus-fast` to eliminate the PyGObject dependency.

## Motivation

dasbus uses PyGObject (gi) internally for GLib type system integration. Even though the application uses PyQt6 (not GTK), dasbus requires GLib's type introspection layer. PyGObject is a heavy dependency that many systems may not have pre-installed.

## Solution: dbus-fast

`dbus-fast` is a pure-Python asyncio D-Bus library with no GLib dependency. Two integration approaches were explored:

### Branch 1: `dev-glib`
Uses `dbus_fast.glib` which integrates with GLib's main loop.

### Branch 2: `dev-qthread` (This branch)
Uses `dbus_fast.aio` in a dedicated QThread with its own asyncio event loop.

---

## Implementation Details

### Dependencies Changed

**Before:**
```toml
dependencies = [
    "PyQt6>=6.5",
    "dasbus>=1.7",
    "PyGObject>=3.42",
]
```

**After:**
```toml
dependencies = [
    "PyQt6>=6.5",
    "dbus-fast>=1.0",
]
```

### New Files

```
src/usbguard_gui/introspection/
├── org.usbguard.Devices1.xml    # USBGuard Devices interface
├── org.usbguard.Policy1.xml     # USBGuard Policy interface
└── org.freedesktop.ScreenSaver.xml  # ScreenSaver interface
```

### API Changes

| dasbus | dbus-fast |
|--------|-----------|
| `SystemMessageBus()` | `MessageBus(bus_type=BusType.SYSTEM)` |
| `bus.get_proxy(name, path)` | `bus.get_proxy_object(name, path, xml).get_interface(iface)` |
| `proxy.listDevices()` | `proxy.call_list_devices()` |
| `proxy.Signal.connect(cb)` | `proxy.on_signal(cb)` |
| `proxy.Signal.disconnect(cb)` | `proxy.off_signal(cb)` |
| `DBusError.error_name` | `DBusError.type` |

### Error Handling

```python
# dasbus
def _is_permission_error(e: DBusError) -> bool:
    name = getattr(e, "error_name", "") or ""

# dbus-fast
def _is_permission_error(e: DBusError) -> bool:
    error_name = getattr(e, "type", "") or ""
```

---

## This Branch: QThread + asyncio

This implementation uses a dedicated QThread running an asyncio event loop to handle D-Bus communication.

### Architecture

```
USBGuardClient (QObject)                    _DBusThread (QThread)
├── Public API (unchanged)                  ├── Runs asyncio event loop
├── Receives results via signals            ├── Connects to D-Bus
└── Forwards signals to GUI                ├── Schedules commands from queue
                                            └── Emits results as Qt signals

ScreensaverMonitor (QObject)                _ScreensaverThread (QThread)
├── Public API (unchanged)                  ├── Runs asyncio event loop
├── Receives active_changed via signal      └── Connects to session D-Bus
```

### Key Components

**`_DBusThread`**: QThread subclass that:
- Creates a new asyncio event loop
- Connects to system D-Bus using `dbus_fast.aio.MessageBus`
- Maintains proxy objects for USBGuard interfaces
- Subscribes to D-Bus signals (DevicePresenceChanged, DevicePolicyChanged)
- Processes commands from a queue (thread-safe communication)
- Emits Qt signals for device events and method results

**Command Queue Pattern**:
```python
def list_devices(self, query: str = "match") -> None:
    if self._devices_iface and self._loop:
        self._schedule(self._do_list_devices(query))

def _schedule(self, coro: asyncio.coroutine) -> None:
    if self._loop and self._running:
        self._loop.call_soon_threadsafe(asyncio.ensure_future, coro)
```

### API Changes in This Branch

Methods are now **asynchronous fire-and-forget**:
- `client.list_devices()` → returns via `list_devices_result` signal
- `client.apply_device_policy()` → returns via `apply_policy_result` signal
- `client.list_rules()` → returns via `list_rules_result` signal
- `client.remove_rule()` → returns via `remove_rule_result` signal

### New Signals Added

| Signal | Parameters | Description |
|--------|------------|-------------|
| `list_devices_result` | `list[Device]` | Result of list_devices() |
| `apply_policy_result` | `int \| None` | Result of apply_device_policy() |
| `list_rules_result` | `list[tuple[int, str]]` | Result of list_rules() |
| `remove_rule_result` | `bool` | Result of remove_rule() |

---

## Approach Comparison

### dev-glib: GLib Integration

**Pros:**
- Simpler implementation
- No additional threads needed
- Direct signal/callback integration with Qt
- Synchronous methods (return values directly)

**Cons:**
- Still requires GLib dependency

### dev-qthread: QThread + asyncio (This branch)

**Pros:**
- Pure asyncio, no GLib dependency
- Clear separation of async and sync code
- More control over event loop

**Cons:**
- More complex architecture
- Command queue pattern required for thread-safe communication
- Asynchronous methods become fire-and-forget (results via signals)
- Additional Qt signals needed for results

---

## Files Modified

| File | Changes |
|------|---------|
| `pyproject.toml` | Replace dasbus + PyGObject with dbus-fast |
| `dbus_client.py` | Add `_DBusThread` class, refactor to async pattern |
| `screensaver.py` | Add `_ScreensaverThread` class, refactor to async pattern |
| `tests/test_dbus_client.py` | Update mocks for new architecture |

## Verification

Both branches pass:
- All unit tests (75 tests)
- Lint checks (ruff)
- Format checks (ruff)

## Recommendation

**Use `dev-glib`** for production. It's simpler, has fewer components, and the GLib dependency is already present on most Linux systems that would run USBGuard.

Use `dev-qthread` only if:
- GLib dependency must be completely avoided
- Maximum isolation of async code is required
- Debugging async behavior is easier with a separate thread
