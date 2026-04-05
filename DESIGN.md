# Replacing PyGObject with dbus-fast

This document describes the migration from `dasbus + PyGObject` to `dbus-fast` to eliminate the PyGObject dependency.

## Motivation

dasbus uses PyGObject (gi) internally for GLib type system integration. Even though the application uses PyQt6 (not GTK), dasbus requires GLib's type introspection layer. PyGObject is a heavy dependency that many systems may not have pre-installed.

## Solution: dbus-fast

`dbus-fast` is a pure-Python asyncio D-Bus library with no GLib dependency. Two integration approaches were explored:

### Branch 1: `dev-glib`
Uses `dbus_fast.glib` which integrates with GLib's main loop. This is the simpler approach that works directly with Qt's event loop.

### Branch 2: `dev-qthread`
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

## Approach Comparison

### dev-glib: GLib Integration

**Pros:**
- Simpler implementation
- No additional threads needed
- Direct signal/callback integration with Qt

**Cons:**
- Still requires GLib dependency (though lighter than PyGObject)

### dev-qthread: QThread + asyncio

**Pros:**
- Pure asyncio, no GLib dependency
- Clear separation of async and sync code
- More control over event loop

**Cons:**
- More complex architecture
- Command queue pattern required for thread-safe communication
- Asynchronous methods become fire-and-forget (results via signals)

**Architecture:**
```
USBGuardClient (QObject)
├── Public methods schedule commands
└── Results returned via Qt signals

_DBusThread (QThread)
├── Runs asyncio event loop
├── Connects to D-Bus
└── Executes scheduled commands
```

---

## Files Modified

| File | Changes |
|------|---------|
| `pyproject.toml` | Replace dasbus + PyGObject with dbus-fast |
| `dbus_client.py` | Refactor D-Bus layer for dbus-fast |
| `screensaver.py` | Refactor D-Bus layer for dbus-fast |
| `tests/test_dbus_client.py` | Update mocks and tests |

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
