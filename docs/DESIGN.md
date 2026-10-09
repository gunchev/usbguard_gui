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
| `dbus_common.py`   | `AsyncWorkerThread`, `stop_worker_thread()`, `recycle_worker_thread()` | Shared QThread + asyncio worker base, introspection loading |
| `dbus_client.py`   | `_DBusThread`, `USBGuardClient`             | USBGuard system-bus D-Bus client           |
| `screensaver.py`   | `_ScreensaverThread`, `ScreensaverMonitor`  | ScreenSaver + logind session-bus monitor   |
| `device.py`        | `Device`, `DeviceTarget`, `parse_device_rule` | Device model and rule-string parsing    |
| `decision.py`      | `DecisionEngine`                           | Lock-first decision flow: HID pending set, unlock cycles, queued decisions, effect signals |
| `ui_strings.py`    | notice titles, warnings                     | Single home for user-visible strings       |
| `app.py`           | `USBGuardTrayApp`                           | Qt event loop, tray, dialogs, signal wiring |
| `device_dialog.py` | `DeviceActionDialog`                        | Per-device Allow/Block × Always/Once prompt |
| `device_list.py`   | `DeviceListWindow` (+ table models)         | Device list window                         |
| `settings.py`      | `SettingsProtocol`, `Settings`              | Settings seam (Protocol) + QSettings-backed singleton |
| `gate.py`          | `block_reason()`, `lock_gate_open()`, `hid_allow_gated()` | Single action-gate authority: daemon first, then the narrow lock rule (HID allow × special treatment × lock unavailable) |

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

Replacing a worker (the reconnect path) uses `recycle_worker_thread()`
instead, because `connect()` runs on the Qt main thread on every backoff
retry and must not freeze the tray for the full timeout.  It waits only
`RECYCLE_GRACE_MS` (250 ms) — enough for a healthy worker to leave its
0.1 s keep-alive loop, which is what guarantees the old bus connection and
its signal subscriptions are gone before the replacement starts, so no
device event is ever delivered twice.  A worker still running after the
grace period is wedged inside `MessageBus.connect()`, i.e. holding no live
bus connection and therefore no ability to emit events, so finishing it off
is deferred to a single-shot timer rather than blocked on.  Both facades
recycle: `USBGuardClient.connect()` always has, and `ScreensaverMonitor.connect()`
now does too — an un-retired screensaver thread keeps its own
`ActiveChanged` subscription and would deliver every lock/unlock twice.

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
| `list_devices_result`     | `list[Device] | None`                    | result of `list_devices()`; `None` = transient failure |
| `list_devices_correlated` | `int, list[Device] | None`              | result of `fetch_devices(request_id)`; `None` = transient failure |
| `list_rules_result`       | `list[tuple[int, str]]`                 | result of `list_rules()`     |
| `remove_rule_result`      | `bool`                                  | result of `remove_rule()`    |
| `permanent_write_failed`  | `int, str, str` (device_id, action, reason) | a permanent rule that did **not** reach `rules.conf` |
| `permanent_clear_failed`  | `int, str, str, bool` (device_id, action, reason, partial) | a failed `Once` clear, with whether any rules were removed |
| `temporary_apply_failed`  | `int, str, str, bool` (device_id, action, reason, policy_changed) | a failed live `Once` action after the clear succeeded |
| `device_bring_up_failed`  | `int, str, str` (device_id, action, explanation) | the device itself would not come up on a path `temporary_apply_failed` does not cover (`Always`, the lock-flow allow) |
| `permanent_rule_remains`  | `int, str, str` (device_id, action, rule) | a broader rule a `Once` choice deliberately leaves intact |

`list_devices()` and `fetch_devices()` hit the same D-Bus call; they differ in
delivery.  `list_devices()` answers on the shared, untagged `list_devices_result`
— fine for a consumer that always wants "the latest snapshot" (the device-list
window).  `fetch_devices(request_id)` answers on `list_devices_correlated` with
the caller's id, which is what the screensaver-unlock path needs: each deferred
cycle resolves only against the snapshot it asked for, so another consumer's
refresh can never consume it and answers may arrive in any order.  Both always
terminate — a fast-fail or a `DBusError` emits `None` (with the request id on
the correlated path), a failed query rather than an empty-bus answer.  Consumers
keep their previous state instead of blanking, and an unlock cycle answered with
`None` is put back and retried on reconnect; an empty list is the daemon's real
answer and resolves the cycle.

Correlation identifies the request but does not make its snapshot current.
Removal, explicit decisions and Allow policy changes remove that incarnation's
ID from every outstanding unlock cycle; empty cycles are retired. Late replies
for retired cycles are ignored, including `None` failure replies, while other
devices in a partially invalidated cycle still get their prompts.

`apply_device_policy()` has no **success** signal — callers follow up with `list_rules()` to
confirm the new policy state. It does have **failure** signals, and that is deliberate rather
than an omission.

A permanent decision is two steps: make the device live, then append the durable rule. The
second step can be denied or fail on its own, leaving the device in the requested state only
until it is unplugged while the user believes they granted permanence. That gap is invisible
in a log file in a tray app nobody tails, so `_do_apply_policy` emits
`permanent_write_failed(device_id, action, reason)` and
`USBGuardTrayApp._on_permanent_write_failed` turns it into a tray warning. Consumers must
treat a permanent request as unconfirmed until either `list_rules()` shows the rule or this
signal fires.

An `Always` replacement removes every exact device/topology-identity duplicate
before appending one new rule. Removing just the first leaves another old rule
above the replacement, defeating first-match-wins. If a removal or append fails,
all rules already removed are restored best-effort and the write failure is
reported; sibling topologies and broader policy are never owned by this rewrite.

A worker-local `asyncio.Lock` serializes `Always`, `Once`, persist-only and
explicit rule-removal transactions, including live changes and rollback. Internal
rewrite helpers do not reacquire it. Read-only requests remain asynchronous.
Automatic `UNCHANGED` live authorizations do not acquire the policy lock: a
lock-screen keyboard must not wait behind a durable-write polkit prompt, and
those authorizations never mutate the permanent ruleset.

`Once` clears device-specific permanent rules before changing the live target. A
failed clear emits `permanent_clear_failed`; its flag distinguishes a total failure
from a partial deletion. If the clear succeeds but the live action fails,
`temporary_apply_failed` reports the live failure and whether the stored policy
changed, including the removed rules in the reason. These outcomes must not be
presented as a successful temporary decision. Permission and ordinary per-call
failures keep the connection alive; only a transport failure triggers reconnection.

Dialogs, cooldowns and held choices are keyed by device ID, serial, hash, parent
hash and port, not the daemon's per-insertion number. A same-topology re-enumeration
updates a retained dialog before any insertion early return. A different topology
needs its own decision even if the device hash is identical. A held choice always
uses the returning instance's raw rule, not a snapshot from the previous insertion.
Held HID Allows with special treatment enabled are handed back to the lock-first
flow or a fresh prompt even when locking is inhibited or unavailable. A fresh
choice also cancels pending automatic handling for that instance, so a delayed
HID allow cannot override an explicit Block.

The device-list window receives the tray's decision handler as a callback. Both
UI surfaces use it to supersede held choices, cancel automatic handling and
dismiss an older popup before dispatching the new choice. Standalone windows
without a handler retain the client-only API used by their isolated tests.

## ScreensaverMonitor Signals

| Signal              | Parameters | Emitted by                                        |
|---------------------|------------|---------------------------------------------------|
| `connection_changed`| `bool`     | ScreenSaver service reachability (connect / loss / re-appearance via NameOwnerChanged) |
| `active_changed`    | `bool`     | ScreenSaver ActiveChanged D-Bus signal            |
| `inhibit_changed`   | `bool`     | logind ListInhibitors poll (idle-block mode)      |

While `connection_changed` is `False` (screen locking unavailable), the app
refuses the one action the lock exists to guard: allowing a HID device while
special HID treatment is enabled.  Block, Reject and non-HID allows keep
working — denying a suspicious device must not depend on the locker — and
with the treatment disabled nothing is gated (availability changes become
log-only).  See `gate.py` for the rule and `README.md` for the user-facing
contract.

ScreenSaver owner changes invalidate the cached lock state without emitting a
synthetic unlock event. Availability is restored only after `GetActive` succeeds
for the current owner. Owner generations reject replies from departed services,
and newer `ActiveChanged` signals outrank an in-flight query's snapshot.

## DecisionEngine Signals

`DecisionEngine` (`decision.py`) owns the lock-first decision flow and talks
to the GUI only through signals — it never touches QtWidgets:

| Signal            | Parameters                       | Emitted by                                      | Wired to (app side)          |
|-------------------|----------------------------------|--------------------------------------------------|------------------------------|
| `show_dialog`     | `Device`                         | a device needs a fresh prompt                    | `_show_device_dialog`        |
| `dialog_retarget` | `Device`                         | a returning instance supersedes an open dialog   | `_retarget_device_dialog`    |
| `notify`          | `str, str, str, int` (title, body, semantic icon, timeout) | lock-availability and handback notices | `_on_engine_notify` (maps the icon to `QSystemTrayIcon.MessageIcon`) |
| `schedule_lock`   | —                                | a HID device entered the pending set — start the deferred delay (never restarted) | `_schedule_hid_lock` (owns the single-shot `QTimer`) |

The app-side handlers own presentation (tray notices, dialog lifecycle, the
`QTimer`); the engine owns every piece of decision state behind
`tray_app._engine` — there are deliberately no property shims on
`USBGuardTrayApp`.  The app reaches the engine only through its public
methods: the `on_*` slots wired to the client and the screensaver monitor,
the decision entry points (`on_device_inserted`, `on_device_allowed`,
`apply_pending_decision`, `apply_user_decision`, `cancel_pending_device`,
`lock_for_pending_hid`, `retry_pending_unlock_cycles`,
`on_hid_treatment_changed`) and three accessors (`has_pending_hid`,
`last_prompted`, `mark_prompted`).  The underscore state is the engine's own.

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

## Testability seams

Two dependencies are injected rather than constructed in place, so the suite
never touches the developer's real per-user state:

- **Settings** — `USBGuardTrayApp(..., settings=SettingsProtocol)`. The
  protocol (`settings.py`) is the contract; tests pass an in-memory fake.
  Without the seam, a GUI preference toggled once in the running app
  (`disable_hid_treatment=true` in `~/.config/usbguard_gui/general.conf`)
  would silently change what the suite asserts — it can skip the entire HID
  pending/lock flow. Production passes nothing and gets the `Settings`
  singleton. Adding a setting means extending **both** the protocol and the
  fake.
- **Window-geometry store** — `DeviceListWindow(..., settings=QSettings)`, so
  each test brings a `tmp_path`-backed store instead of overwriting
  `device_list.conf` with offscreen geometry.

## Tooling

| Tool      | Purpose                        | Command                    |
|-----------|--------------------------------|----------------------------|
| isort     | Import sorting                 | `make lint` / `make format`|
| ruff      | Lint (E/F/W/UP/B/SIM/RUF)     | `make lint`                |
| autopep8  | Code formatting                | `make lint` / `make format`|
| pyright   | Static type checking           | `make typecheck`           |
| pytest    | Tests (`tests/`, headless via `QT_QPA_PLATFORM=offscreen`) | `make test`     |

**Import order is owned by isort, not ruff.** Ruff's `I` rules are deliberately
*not* enabled in `[tool.ruff.lint]` (see `pyproject.toml`), so `ruff check
--fix` will not sort imports — `make format` runs isort for that. AGENTS.md
carries the same rule.
