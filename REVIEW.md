# Project Review — usbguard_gui

Date: 2026-08-28
Reviewer: pi agent

Scope: full pass over all 10 source modules, all 6 test files, release tooling,
RPM spec, polkit rules, CI, and docs. Ran the full test suite (145 passed),
ruff, pyright, autopep8, coverage, and a wheel build; empirically verified a
couple of risky assumptions (QLockFile across `execv`, dbus-fast 5.x API
compatibility, wheel contents).

## Executive summary

The project is in **good shape overall**: clean, well-typed, well-documented
code with a sound QThread+asyncio architecture (DESIGN.md matches the code
accurately), 145 passing tests, pyright/ruff clean, 80% coverage, and polished
packaging. However, there are **3 real bugs** (one a security race in the app's
core HID defense flow), several robustness gaps, and some documentation /
test-quality drift.

---

## 🔴 Real bugs

### 1. HID security race: pending keyboard can be auto-allowed while the screen is *unlocked*

`src/usbguard_gui/app.py:144` — `_on_list_devices_result()`

The HID branch allows all pending HID devices on **any** `list_devices_result`,
without checking `self._screensaver.active`:

```python
if self._hid_pending_devices:
    ...
    for device_number in pending_ids:
        if any(d.number == device_number for d in devices):
            self._client.apply_device_policy(device_number, DeviceTarget.ALLOW, permanent=False)
```

Repro sequence (all normal code paths):

1. Screen locked → non-HID device A inserted → deferred (`screensaver_pending_devices={A}`).
2. Screen unlocks → `_on_screensaver_changed` (app.py:335) sets `_screensaver_pending_ids=[A]`
   and calls `list_devices()` (result R1 in flight).
3. Attacker plugs a malicious keyboard **during R1's in-flight window** → HID INSERT →
   added to `_hid_pending_devices`, 5 s lock timer started.
4. R1 arrives → HID branch matches → **keyboard allowed while the screen is still unlocked**.
5. Lock timer fires → `_hid_pending_devices` is now empty → `_lock_for_pending_hid` skips the
   lock (app.py:354).

Result: the attacker's keyboard gets keystrokes on an unlocked session with no password
prompt — exactly the attack this app exists to prevent. The window is small (one D-Bus
round trip) but this is a security product.

**Fix:** in the HID branch, only apply the allow if `self._screensaver.active` is true;
otherwise leave the device pending for the timer-driven lock. (Existing tests set
`_active = True` for this path, so they already encode the intended semantics — the check
is simply missing in the code.)

### 2. Reconnect leaks live `_DBusThread`s; the backoff logic is dead code

`src/usbguard_gui/dbus_client.py:264` (`USBGuardClient.connect()`) +
`src/usbguard_gui/app.py:170,174`

- `connect()` unconditionally creates a new `_DBusThread` and replaces `self._thread`
  **without stopping the old one**. If the USBGuard daemon crashes while the app is
  connected, the old thread stays in its keep-alive loop (`_main` in `dbus_client.py`)
  with its system-bus connection and D-Bus `AddMatch` signal subscriptions still
  registered. Every daemon crash leaks one live ghost thread (until quit). When the
  daemon returns, the match rules still route signals to the ghost connections →
  **duplicate `device_presence_changed` / `list_devices_result` deliveries** →
  `apply_device_policy` executed multiple times per event.
- `connect()` always returns `True`, so:
  - the `if not self._client.connect():` branch in `start()` (app.py:170) is dead code;
  - the exponential-backoff branch in `_try_connect()` (app.py:179-183, including
    `RECONNECT_MAX_INTERVAL`) is unreachable — reconnection retries at a fixed 5 s forever.

**Fix:** make `connect()` idempotent — stop+wait the existing thread before spawning a
new one (or better, do reconnection *inside* `_DBusThread` so there's ever only one).
Then either make `connect()` report real state or drive retries purely from
`connection_changed`.

### 3. "Allow (Temporary)" from the device list can delete the user's *permanent* rule

`src/usbguard_gui/device_list.py` — `_do_apply_with_rules()`

```python
if target == DeviceTarget.ALLOW and not permanent and device.hash:
    for rule_id, rule_str in rules:
        parsed = parse_device_rule(rule_str)
        if parsed["rule"] == "allow" and parsed["hash"] == device.hash:
            self._client.remove_rule(rule_id)
```

This removes **every** `allow` rule matching the device's hash — permanent ones included
(permanent vs. temporary can't be told apart from the rule string). Right-click a
permanently-allowed keyboard → "Allow (Temporary)" → its permanent rule is silently
removed; after the next replug/reboot the device is blocked again.

**Fix:** don't remove rules at all — re-applying a temporary allow is harmless (USBGuard
prepends it, so it just wins evaluation order); or track rule IDs the GUI added itself
and only remove those.

---

## 🟠 Robustness gaps

4. **No user feedback when actions are silently dropped.** When the daemon is
   disconnected, dialog buttons and the device-list context menu no-op
   (`USBGuardClient` methods fast-fail in the thread). The user clicks "Allow
   (Permanent)" believing it took effect. At minimum, disable the buttons or show a
   warning when `client.connected` is `False`; that property exists but is never
   consulted by the UI.
5. **No proactive daemon-loss detection.** The tray tooltip stays "connected" until the
   next D-Bus call happens to fail (e.g., the user opens the device list). Watching
   `NameOwnerChanged` for `org.usbguard1` would flip state immediately.
6. **`_quit()` can hang.** `USBGuardClient.stop()` → `_thread.stop()` →
   `_thread.wait()` is unbounded; a worker stuck in `await MessageBus.connect()` blocks
   quit forever. Use a bounded `wait(timeout)` with a `terminate()` fallback.
7. **Enter key = "Allow (Permanent)".** In `device_dialog.py:67-74`, both allow buttons
   carry `AcceptRole`; the first one (`Allow (Permanent)`) is the default, so a stray
   Enter press on the dialog creates a persistent rule. Make "Allow (Temporary)" the
   default (or set no default).
8. **Screensaver monitor never retries.** If the session bus / screensaver service isn't
   available at startup, `lock()` is a no-op for the app's lifetime: the "Locking
   screen…" notification fires, nothing locks, and pending HIDs are never auto-allowed
   (device stuck blocked until replug).
9. **Misleadingly similar slot names.** `_on_screensaver_changed` (app.py:335, unlock →
   defer summary) and `_on_screensaver_active_changed` (app.py:343, lock → HID
   auto-allow) are two slots on the *same* signal doing unrelated things. Rename to e.g.
   `_on_screensaver_unlocked` / `_on_screensaver_locked`.

## 🟡 Minor / cosmetic

10. **Docs drift:** README (line 44) and the spec file say the HID lock delay is
    "4 seconds"; the code is `HID_LOCK_NOTIFY_DELAY_MS = 5000` (app.py:61). AGENTS.md
    says `make check` is "lint + test" but it's lint + typecheck + test.
    `release.py:main()` docstring is a fragment ("The main and only"). The 0.6.0
    CHANGELOG contains "Will this run on fedora?" as a commit line (issue title used as
    commit message).
11. **Polkit rule** (`rpm/70-usbguard_gui.rules`): `subject.active == true` appears
    twice in the condition.
12. **Format not idempotent-clean:** `uv run autopep8 --diff` flags `screensaver.py:88`
    (pyright-ignore comment placement). `make lint` still passes (autopep8 exits 0), but
    `make format` would move the comment — the tree should be committed format-clean.
13. **Dead code:** `Device.vendor_id` / `Device.product_id` are unused everywhere;
    `RECONNECT_MAX_INTERVAL` is unreachable (see #2); the `apply_policy_result` signal
    still exists in both test fakes (`test_app.py:133`, `test_device_list.py:23`)
    although it was removed from the real client in 0.5.0.
14. **`release.py`** `build_changelog`: the displayed commit count
    (`len(section.splitlines()) - 4`) is section-line arithmetic, not a commit count
    (display-only).

## Test suite assessment

Strong at the unit level — regression tests explicitly document past bugs (circuit
breaker, signal-connection leak, timer refresh staleness), and the HID / inhibit /
permission-error logic is well covered. Weaknesses:

- **`TestSignalHandlers` (test_app.py:17-100) tests a copy, not the code.** Each test
  re-implements `main()`'s signal-setup logic inline; they'd pass even if `app.main()`
  were deleted. That's false confidence in the SIGUSR1 re-exec path (the QLockFile +
  execv assumption *was* verified to hold — Qt treats a same-PID lock as stale — but
  the *code* itself isn't what's being tested).
- `test_hid_insert_triggers_prompt_when_inhibited` uses
  `device.to_rule() if hasattr(device, "to_rule") else <literal>` — `Device` has no
  `to_rule`, so it's dead defensive code.
- `test_stop_calls_thread_stop_and_wait` doesn't actually assert `wait()` was called.
- Coverage gaps: `app.py` 58% (reconnect logic, tray handling, `main()` untested),
  `device_dialog.py` 24% (the whole dialog untested).

## Packaging & CI (verified)

- ✅ Wheel build works and **includes the introspection XMLs** (required at runtime —
  easy to break silently).
- ✅ dbus-fast 5.0.22 (installed in the dev venv, far newer than the `>=1.0` pin) is
  API-compatible with everything used: `connect()`, `introspect()`,
  `get_proxy_object()`, `get_interface()`, `on_*`/`call_*` dynamic methods. Note:
  dbus-fast **validates signal signatures against the introspection XML** and drops
  mismatching signals with a warning — the bundled `a{ss}` attributes type should be
  re-verified against a live `gdbus introspect org.usbguard1` on a Fedora host
  (usbguard isn't installed on this machine).
- ✅ `SIGUSR1` re-exec across `QLockFile` verified working empirically (same-PID lock is
  treated as stale, so the new process after `execv` can re-lock).
- ⚠️ CI installs `python3.10…3.13` but `tox.ini` envlist includes `py314` — silently
  skipped via `skip_missing_interpreters`; either add the package or drop the env.
- ⚠️ `%post`'s `usbguard generate-policy` writes the initial policy, but nothing
  verifies the daemon's default policy is permissive enough for a fresh install to be
  usable — probably fine, just noting.

## Suggested priority order

1. Fix the HID allow race (#1) — it's in the app's core security contract.
2. Fix reconnect thread lifecycle + backoff dead code (#2).
3. Stop deleting permanent rules on temporary allow (#3).
4. Add disconnected-state feedback in dialogs / device list (#4).
5. Rename the two screensaver slots, fix the default dialog button (#7, #9), sync the
   4 s / 5 s docs (#10).
6. Replace the `TestSignalHandlers` copies with tests that exercise the real `main()`
   (or at least the handler wiring), and add a dialog test.
