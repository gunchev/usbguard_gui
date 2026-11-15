# Done

Completed roadmap items, moved out of `TODO.md` at every release so the roadmap
carries only open work. Entries keep their version section and their full text --
the reasoning behind a decision is why these are kept, not the checkbox. See
*Releasing* in `AGENTS.md`.

Entries below cite `docs/REVIEW-2026-08-28.md` through `docs/REVIEW-2026-09-28.md`,
`docs/AUDIT-2026-06-15.md` and their finding numbers. Those reports were superseded
by `docs/REVIEW-2026-11-15.md` and removed; they are in git history
(`git log --diff-filter=D -- docs/`).


## 1.0

- [x] Merge PR #8 — permanent allow rules across KVM and dock topology changes (`beorn-`). **Merged 2026-09-13**
      as `2e5eea4` (merge commit, authorship preserved) and shipped in **v0.8.0**. Rebased onto
      master with four review fixes: place the rule above the first one that provably matches the device,
      dedup instead of append-only, report a permanent write that only applied temporarily
      (`permanent_write_failed`), and validate the device-reported `raw_rule` before persisting it
      (fall back to the daemon's own upsert if it looks wrong). HID lock-first contract untouched.
      Green under the full tox gate on Fedora 43/44. Merged without waiting on the KVM field
      confirmation — see `docs/REVIEW-2026-09-13-pr8-permanent-rules.md` for the review trail.
      Leaves two README lines behind: the unplaceable-first-rule case
      (`Policy::appendRule` rejects `parent_id = 0`, so nothing lands above rule #1) and the matcher's
      undecidable-by-design `None` verdict. (Sequenced before the lock-gate refinement,
      which has since landed — the gate's tests are the baseline this race fix must keep
      green.) Its `rule_matches_device()` /
      `rule_persistence_problem()` are the primitives the catch-all detection should reuse.
- [x] Unlock-queue race: correlated per-call device fetch (Option 3) — `fetch_devices()` +
      `list_devices_correlated(id, devices)`, id→id-set dict in the app, device-list window
      untouched. Closes AUDIT follow-up items 2 & 3 (both reproduced red, then fixed;
      `TestUnlockQueueRaceReproductions`). Done 2026-09-12.
- [x] **Action set v1: `[Allow Always] [Allow Once] [Block Once] [Block Always]`.** Shipped
      in both the dialog and the device-list menu (one shared `_ACTION_SET`), and
      establishes the invariant the rest of the design rests on:

      **A device's permanent rule always reflects the last durable decision — or there is none.**
      `Always` upserts the rule; `Once` **deletes** it. `Once` is not "decline to write", it is an
      explicit removal — otherwise the old rule survives the click and silently re-asserts at the
      next reboot, which is the whole finding-12 confusion in a different costume.

      The machinery is already there: `_rule_identity()` keys on device/topology with **no target
      verb**, and `_persist_device_rule` already removes-before-appends (never the reverse —
      appending first leaves the older rule on top and first-match-wins keeps honouring it) and
      restores on a failed append. What is missing is a **remove-without-append** path for the
      `Once` verbs. Hand-written class rules stay safe: `_rule_identity()` returns `None` for rules
      naming no device, so a tray click cannot clobber `reject with-interface all-of { ... }`.

      **`Block Always` must write verb `block`, never `reject`.** `_retarget_device_rule()` will
      happily write any verb, and `reject` would build the ghost-device trap — a deny with no UI
      path back until the rules editor lands. Worth an assert, not just a comment.

      **Close, Escape and the timeout all apply nothing** (as decided — the plan said
      `Block Once`, which was wrong twice over). The timeout never applied Reject in the first
      place: `_tick()` closes without setting a target, so an expired dialog already applied
      nothing. And `Once` is a *deletion*, so a timed-out `Block Once` would mutate persistent
      policy on no decision. Dismiss now records nothing and the device stays where the implicit
      floor put it. Close is also no longer gated on `_action_blocked()`, which used to leave it
      stuck open when the connection was down.
      Related: the dialog returns `QDialog.Accepted` for Block too (it calls `accept()`), so
      `result()` cannot be trusted as an outcome signal — see finding 11.

      **Supersedes** the *Permanent Block* item and **both** "warn when blocking a device that has
      a permanent allow rule" items: under this invariant a `Once` action clears the rule, so the
      silent divergence those items warned about no longer exists.
- [x] Lock-gate refinement: gate HID-capable devices only, and only while special HID treatment is enabled and
      screen locking is unavailable (DESIGN.md contract + dialog/device-list gating + notification text + tests).
      Treatment disabled ⇒ no gating at all; lock-availability changes are log-only then.
      **Also narrow the gate to `ALLOW` only** — Block and Reject need no lock capability and
      make the situation strictly safer, yet today a ScreenSaver outage disables them too, so
      you cannot deny a suspicious device while the locker is down. Resolve together with
      `docs/REVIEW-2026-09-28.md` finding 10.
      (Done — `gate.py` is the single authority; dialog/device-list gate only HID
      allows with treatment on; notices narrowed; table-driven tests in
      `tests/test_gate.py`. Commit `3d218e9`.)
- [x] Stop the device list blanking to "no devices" on a transient D-Bus error.
      `_do_list_devices` emits `[]` on `DBusError` and `DeviceListWindow` cannot tell that
      from a genuinely empty bus, so the table clears. Fix at the **signal** level (emit
      `None`, or add a success flag) rather than in the window, so no future consumer has to
      guess. The correlated path already proves the failure mode is real —
      `_on_correlated_devices` re-queues on an empty snapshot for exactly this reason.
      `docs/REVIEW-2026-09-28.md` finding 9.
      (Done — `list_devices_result` emits `None` on failure; both consumers keep
      their previous state. Correlated path unchanged on purpose: its re-queue
      retry still relies on `[]`. Commit `41f6324`.)
- [x] Test the anti-lockout HID branch (HID inserted while the screen is
      already locked → immediate temporary allow). It is the only auto-allow path with no
      test, and a regression there locks the user out of their own machine rather than
      opening a hole. `docs/REVIEW-2026-09-28.md` finding 1.
      (Done — `TestAntiLockoutBranch` in `tests/test_decision_engine.py`, added
      in commit `747afae` before the split; the branch now lives in
      `DecisionEngine._on_device_inserted`.)
- [x] Delete or rename `Device.is_hid()` (all-interfaces-HID). Public, tested, used by
      nothing but its own tests; the security-correct check is `has_hid_interface()` (any
      interface). The name invites the wrong call and would silently exclude composite
      HID+MSC devices. `docs/REVIEW-2026-09-28.md` finding 4.
      (Done — `is_hid` is gone; `tests/test_device.py` asserts it no longer exists.)


## Core refactor (v0.10.0) — GUI / decision-engine separation

Branch: `core_refactor`; the plan was never committed, so its steps are recorded here.
Landed in **v0.10.0** — Phases 0–5 pure refactor, Phase 6 behaviour changes.

Four of these steps were also roadmap-level 1.0 items and are recorded in `## 1.0`
above; the full plan is kept intact here so the phase record stays whole. Open items
spun out of the plan are in `TODO.md`.

Goal: extract the decision/state machine behind the HID lock-first contract out of
`USBGuardTrayApp` into a headless engine, so the security core is testable without
widgets and the action gate has a single authority.

**Rules for every step**

- `make check` green before marking a step done (lint + typecheck + 535 tests).
- The HID lock-first contract in `README.md` → *How It Works → HID Devices* must hold
  after every step — no step may change which devices get auto-allowed.
- One concern per commit; keep the branch shippable at every commit.
- Behavior-preserving steps first; behavior changes last and clearly separated.

Reference: `docs/REVIEW-2026-09-28.md` findings #1, #9, #10; `TODO.md` 1.0 items
"Lock-gate refinement" and "Stop the device list blanking".

---

### Phase 0 — Safety net before touching the security path

- [x] **0.1 Test the anti-lockout HID branch** (REVIEW finding #1)
      `app.py` HID inserted while screen already locked → immediate temporary allow
      (`_on_device_presence_changed`, the `self._screensaver.active` branch inside
      `hid_special_treatment`). Only auto-allow path with no test; a regression here
      locks the user out. Reproduce first, then green.
      *(Done — basic test already landed in 9bc3433; added `TestAntiLockoutBranch`:
      allow fires alone — no dialog/pending/timer/notice — allow precedes the
      permanent-cache skip, treatment-off defers while locked. Commit 747afae.)*
- [x] **0.2 Pin the queued-decision + HID handback behavior**
      Tests asserting `_apply_pending_decision` refuses a held HID ALLOW and keeps
      the permanent rule — currently covered only indirectly; make them explicit so
      the engine extraction has a tripwire.
      *(Done — `TestApplyPendingDecisionReturnContract` pins the return contract
      directly; `TestDecisionStateSources` covers the previously untested
      `_on_list_rules_result` seeding and `_on_permanent_write_failed`;
      `TestInsertionEntryGuards` covers the PRESENT/UPDATE skip, empty-lock-timer
      guard and stale-identity retarget. Commit cb33ea8.)*
- [x] **0.3 Run full gate + record baseline**
      `make check`, note test count; `graft build --deep` so the graph reflects HEAD.
      *(Done — baseline **549 passed** (was 535); app.py coverage 93% → 95%,
      missing lines reduced to GUI/main()/backoff paths only.)*

### Phase 1 — Test scaffolding (mechanical, no src changes)

- [x] **1.1 Create `tests/conftest.py`** with shared fixtures: `_FakeClient`,
      `_FakeScreensaver`, `_FakeSettings` (currently duplicated across
      `test_app.py`, `test_device_dialog.py`, `test_device_list.py`,
      `test_permanent_rules.py`). Keep per-file private subclasses where behavior
      genuinely differs; share only the identical copies.
      *(Done — classes went to `tests/fakes.py`, fixtures to `tests/conftest.py`,
      because the files construct fakes with arguments (`_FakeClient(connected=False)`)
      which fixtures alone cannot carry. Note: `test_permanent_rules.py` has no
      duplicates — it only defines `_FakePolicy`, a different thing; plan was stale.)*
- [x] **1.2 Point all four test files at the shared fixtures**; delete the local
      duplicates.
      *(Done — test_app, test_device_dialog, test_device_list share the union fake;
      local copies deleted, `apply_calls` renamed `apply_policy_calls` in dialog tests.)*
- [x] **1.3 `make check`** — expect identical test count.
      *(Done — **549 passed**, lint + typecheck green. Commit 6bfb301, net −55 lines.)*

### Phase 2 — Leaf extractions (still no decision-logic moves)

- [x] **2.1 Create `src/usbguard_gui/ui_strings.py`** (or `notifications.py`) with
      the duplicated user-facing messages: the "daemon is not connected" warning and
      the "Screen locking is unavailable — device actions are disabled" warning
      (identical in `device_dialog.py` and `device_list.py`), plus tray notification
      titles currently inlined in `app.py`.
      *(Done — note: the daemon warning tails differed ("tray icon shows 'connected'"
      vs "connection is restored"); unified to the device-list wording. The dialog's
      version had the more specific promise the tray icon can't always keep. Also
      inlined: MESSAGE_BOX_TITLE at the 4 view sites + 2 in main().)*
- [x] **2.2 Switch `device_dialog.py` + `device_list.py` to the shared strings.**
      *(Done, together with app.py's showMessage titles — extracting titles nobody
      reads would leave dead constants. Commit 8feb726.)*
- [x] **2.3 Create `src/usbguard_gui/gate.py` — single action-gate authority**
      Headless function/object answering "may an action proceed, and if not, why?":
      inputs = daemon connected, lock available (`ScreensaverMonitor` state),
      settings (`disable_hid_treatment`), device (for HID checks later).
      First pass: move the *existing* logic verbatim (connection + lock checks) so
      behavior is unchanged.
      *(Done — `BlockReason` enum, `lock_gate_open(state)` accepting monitor object /
      cached bool / None, `block_reason(daemon_connected, lock_gate)`. Settings and
      device inputs wait for a caller that needs them; 8 unit tests in test_gate.py.)*
- [x] **2.4 Rewire `DeviceActionDialog._action_blocked` / `_actions_enabled`**
      (`device_dialog.py:110,212`) and `DeviceListWindow._apply` (`device_list.py:339`)
      to call the gate; keep `QMessageBox` presentation in the views, decision in
      the gate.
      *(Done — commit 40ad20d.)*
- [x] **2.5 Rewire `app.py`'s `_lock_available` reads** used for action gating to go
      through the same gate (keep the cached field if needed for the engine — the
      gate consumes it).
      *(Done — the three decision reads (lock timer, HID flow, queued drain) call
      `lock_gate_open(self._lock_available)`; the field's own writes stay. Commit
      557ce91.)*
- [x] **2.6 `make check`** + manual smoke: dialog buttons disable when lock service
      is down; device-list context menu shows the warning.
      *(Done — **557 passed** (+8 gate tests), lint + typecheck green. Manual smoke
      is headless here; `TestDialogLockUnavailable` and `TestApplyLockUnavailable`
      assert exactly those two behaviors.)*

### Phase 3 — Create the engine skeleton (moves state, not yet handlers)

- [x] **3.1 Create `src/usbguard_gui/decision.py`** with
      `class DecisionEngine(QObject)`:
      - constructor takes `USBGuardClient`, `ScreensaverMonitor`, `SettingsProtocol`
        (no `QApplication`, no widgets);
      - effect signals (names final at implementation time):
        `show_dialog(Device)`, `notify(title, body, icon, timeout)`,
        `lock_now()` (deferred-lock trigger), `dialog_retarget(Device)` — whatever
        the moved handlers actually need; declared empty first.
      *(Done — all four signals declared; MAX_PENDING_* constants moved with their
      state, app re-imports them so the existing monkeypatch targets keep working.)*
- [x] **3.2 Move pure state fields** from `USBGuardTrayApp.__init__` into the
      engine: `_pending_decisions`, `_last_prompted_at`, `_hid_pending_devices`,
      `_screensaver_pending_devices`, `_pending_unlock_cycles`,
      `_next_unlock_cycle_id`, `_permanent_allow_hashes`, `_lock_available`,
      `_lock_state_confirmed`, plus constants `MAX_PENDING_*`.
      `USBGuardTrayApp` keeps a reference: `self._engine = DecisionEngine(...)`.
      *(Done — engine inits `_lock_available` from the monitor at construction.)*
- [x] **3.3 Keep both sides in sync trivially for now** — app property shims or
      direct `self._engine._x` access in the still-unmoved handlers (tests keep
      passing unmodified or with a one-line path change each).
      *(Done — ten property shims (get + set) on `USBGuardTrayApp`; zero test
      changes needed; 5 seam tests added. Commit 94ec5aa.)*
- [x] **3.4 `make check`.**
      *(Done — **562 passed**, lint + typecheck green.)*

### Phase 4 — Move handlers (the security-sensitive core)

Order chosen so each move is independently reviewable against the contract:

- [x] **4.1 Move `_register_unlock_cycle` / `_retry_pending_unlock_cycles` /
      `_on_correlated_devices`** (unlock-queue race machinery) + their tests.
      *(Done — pure move to engine; client signal now connects straight to the
      engine slot; tray notify + dialogs flow through the `notify`/`show_dialog`
      signals with a semantic icon (`NOTIFY_INFO`/`NOTIFY_WARNING`) mapped to
      `MessageIcon` in the app; test call sites retargeted to
      `tray_app._engine.*`; cap-monkeypatch target → `usbguard_gui.decision`.
      Commit 75d2eed, **562 passed**.)*
- [x] **4.2 Move `_cancel_pending_device`, `_lock_for_pending_hid`,
      `_on_screensaver_locked`, `_on_screensaver_unlocked`** — the auto-allow-then-lock
      flow. Engine emits `lock_now()`; app keeps the `QTimer` (`_hid_lock_timer`) and
      calls `engine.allow_pending_hid()` when it fires, **or** engine owns a
      callback-injected scheduler — decide at implementation: prefer the app keeping
      QTimer and engine exposing `pending_hid_ids()` / `allow_pending_hid()` so the
      engine stays free of timers on first pass.
      *(Decided: app keeps the QTimer; `timeout` connects straight to
      `engine._lock_for_pending_hid`, and `_cancel_pending_device` stays as an app
      wrapper that does the timer stop while the engine owns all state — no new
      signals needed. `lock_now()` stays declared but unused until 4.4 (the INSERT
      flow will need to ask the app to start the timer). A dead duplicate of
      `_on_screensaver_locked` left in app.py was found and removed in the 4.3
      pass — connections already pointed at the engine. Commit bb991df, **562
      passed**.)*
- [x] **4.3 Move `_apply_pending_decision` + `_hid_lock_flow_applies`** — the
      contract's heart. Notifications become `notify(...)` signals; the app connects
      them to `_tray.showMessage`.
      *(Done — handback notice emitted as `notify(HANDBACK_NOTICE_TITLE, ...,
      NOTIFY_WARNING, 15000)`; `dialog_identity` became a module-level function in
      `decision.py` (single source for both sides); `HANDBACK_NOTICE_TITLE` +
      `_LIVE_AUTHORIZE_PROMISES` moved to `ui_strings.py`; test imports and the
      `tray_app._apply_pending_decision` / `USBGuardTrayApp._dialog_identity`
      call sites retargeted; dropped the now-unused `lock_gate_open` import from
      app.py. README HID section read against the diff — logic verbatim, only the
      notice path changed shape. **562 passed**, lint + typecheck green.)*
- [x] **4.4 Move the INSERT/REMOVE decision flow** out of
      `_on_device_presence_changed` (`app.py:390-521`): the app slot becomes
      parse-`Device`-from-signal → `engine.on_device_presence(device, event, target)`
      → react to emitted effects (show dialog, retarget, notify).
      *(Done — but split, not unified: `engine._on_device_inserted(device,
      target)` takes the INSERT flow; REMOVE stays in the app (dialog-keep +
      the `_cancel_pending_device` wrapper that owns the timer stop) so no
      `Device` parse was added to a path that never parsed one. Effects emit
      `dialog_retarget`, `notify(HID_ATTACHED…)` and the renamed
      `schedule_lock()` — renamed from `lock_now()` because it means "start the
      deferred lock", not "the lock is due"; the app slot `_schedule_hid_lock`
      owns the `isActive()` never-restart guard. `_enum_name` moved to
      `device.enum_name` (one source for app logs and the engine's INSERT
      log). Two tests that patched `tray_app._show_device_dialog` were
      rewritten to assert `_open_dialogs` — the signal connects at init, so a
      later instance-attribute patch could no longer intercept it; the new
      assertion pins the same behavior at a higher level. **562 passed**,
      lint + typecheck green.)*
- [x] **4.5 Move `_on_device_policy_changed`'s cache-seeding** of
      `_permanent_allow_hashes` (keep UI-sync parts — list refresh, dialog retarget —
      in the app; engine emits what the UI needs).
      *(Done — engine `_on_device_allowed(device, rule_id)` also takes the
      cooldown pop (both are pure decision state); dialog dismissal and the
      `_cancel_pending_device` wrapper stay app-side, in that order — the
      containers are disjoint so the reorder is unobservable. Commit 6cdb913,
      **562 passed**.)*
- [x] **4.6 Move `_on_list_devices_result`'s opportunistic HID safety net**
      (`app.py:338-354`) — the `screensaver.active` snapshot allow.
      *(Done — moved with `_on_list_rules_result` (the pure cache rebuild
      sitting right above it); both client signals now connect straight to the
      engine; `parse_device_rule` dropped from app imports. Commit 74f14c7,
      **562 passed**.)*
- [x] **4.7 Move `_apply_user_decision` + `_on_lock_availability_changed`
      decision halves**; keep window/dialog lifecycle in the app.
      *(Done — app keeps dialog lookup/close, then the `_cancel_pending_device`
      wrapper (timer glue), then delegates to the engine; engine
      `_on_lock_availability_changed` owns the state transition + notices via
      `notify` and the screensaver connection now points straight at it.
      Follow-up fixes: `MAX_PENDING_DECISIONS` and the two lock-notice titles
      dropped from app imports (tests import the cap from `decision`), one test
      re-targeted the cap-warning patch to `usbguard_gui.decision.log`, and the
      locker-restart test connects its replacement monitor to the engine slot.
      **562 passed**, lint + typecheck green.)*
- [x] **4.8 After each of 4.1–4.7: `make check` + read README HID section against
      the diff.** Nothing here may change behavior — flag any temptation to "fix
      while moving"; park it in `TODO.md` instead.
      *(Done — `make check` green after every step (562 passed each time). Final
      pass: README *How It Works* → *HID Devices* → *When Screen Locking Is Not
      Possible* → *Screensaver Integration* read line-by-line against the moved
      code: lock-then-allow ordering, 5 s delay, unplug-skips-lock, locked-
      insert immediate allow, treatment-off prompt fallback, inhibited/unavailable
      refusal, unlock-cycle notification + dialogs, handback notice — all present
      and byte-identical (the only shape change is tray notices routed through
      `notify` → `_on_engine_notify`, same title/body/icon/timeout, asserted by
      the existing tests). Nothing parked in `TODO.md`. `graft build --deep`
      refreshed after the phase.)*

### Phase 5 — Thin the tray app

- [x] **5.1 Connect all engine effect signals** in `_connect_signals()`:
      `show_dialog` → `_show_device_dialog`, `notify` → `_tray.showMessage`,
      `lock_now` → start `_hid_lock_timer`, etc.
      *(Already true incrementally: `show_dialog`+`notify` (4.1),
      `dialog_retarget`+`schedule_lock` (4.4); the plan's `lock_now` became
      `schedule_lock` → `_schedule_hid_lock`. All four connected in
      `_connect_signals()`; no orphaned effects.)*
- [x] **5.2 Delete leftover dead state/handlers in `USBGuardTrayApp`**; class
      should be: tray setup, menus, dialog/window lifecycle, reconnect timer,
      signal wiring, `main()`.
      *(Audit: zero dead methods — every `def` has a connect/call site — and
      `__init__` holds only app-native state (tray, timers, dialogs, window).
      The ten shim property pairs remain: nine are tests-only (deleted in 5.4's
      rewire) and `_last_prompted_at` backs the live cooldown read/write in
      `_show_device_dialog` (switched to `engine._last_prompted_at` then).
      Stale Phase-3 comments updated.)*
- [x] **5.3 Split `tests/test_app.py`** (2,504 lines) into:
      `test_decision_engine.py` (HID flow, unlock cycles, pending decisions,
      correlated fetch) and keep `test_app.py` for wiring/lifecycle (tray, quit,
      reconnect backoff, settings injection, signal connections).
      *(Done — exact line-range split: 25 decision classes → new file (1,852
      lines), 13 wiring classes stay (739 lines). Shared `tray_app` +
      `queued_decision` fixtures and the `KEYBOARD_RULE`/`IR_RULE` constants
      moved to `tests/conftest.py` (needed by both halves). Commit `1acff6a`;
      `make check` green, **562 passed**.)*
- [x] **5.4 Rewire tests** that poke `tray_app._hid_pending_devices` etc. to target
      the engine; keep a thin set of end-to-end tests that drive
      `tray_app._on_device_presence_changed` and assert via the fake client —
      the contract deserves at least a few full-stack tests.
      *(Done — 131 bare shim reads/assignments across conftest + both suites
      rewired to `tray_app._engine.*`; `_show_device_dialog` cooldown now
      stamps `engine._last_prompted_at` (app.py:422,428); all 9 shim
      property/setter pairs deleted (77 lines). Seam test became
      `TestDecisionEngineStateOwnership` (5 tests: empty state, effect
      signals, no-shims guard, engine-stamped cooldown, monitor state) — the
      old "shim transparency" tests were tautologies once the shims were gone.
      End-to-end coverage kept: the flapping/handback/guards classes still
      drive `_on_device_presence_changed` and assert via the fake client.
      Commit `1ce3b10`; `make check` green, **562 passed**.)*
- [x] **5.5 `make check`**; coverage must not drop (baseline ≈ 89–91%).
      *(Done — `make check` green, **562 passed**, TOTAL coverage **92%**
      (1878 statements, 143 missed) — above the baseline.)*

### Phase 6 — Behavior changes (separate commits, each with its own tests)

- [x] **6.1 Narrow the gate: ALLOW-only** (REVIEW finding #10, TODO "Lock-gate
      refinement"): Block/Reject stay available while the locker is down — they make
      the machine safer, not less safe. Gate HID-allow only; dialogs/list menus
      reflect it. Update `README.md` (polkit/lock section) + DESIGN.md.
      *(Done — `gate.hid_allow_gated()` + `block_reason(..., target, is_hid,
      hid_treatment_enabled)`; dialog disables only the two Allow buttons (and
      only for a gated HID device), list menu disables only its Allow entries;
      notices narrowed ("Block and Reject still work"), drain-gate deleted
      (only HID allows were ever lock-gated). README lock section + treatment
      section rewritten, DESIGN module table gained `gate.py`.
      Commit `3d218e9`, **586 passed**.)*
- [x] **6.2 Gate HID devices only, and only while special HID treatment is enabled**
      (same TODO item — do together with 6.1 if the diff stays small).
      *(Done in the same commit `3d218e9`: `hid_allow_gated` requires
      `is_hid and hid_treatment_enabled`; treatment off ⇒ nothing gated and
      lock-availability changes are log-only (notices skipped, state still
      tracked); the tray's treatment toggle re-arms open dialogs via
      `refresh_actions_enabled()`.)*
- [x] **6.3 Fix device-list blanking on transient D-Bus error** (REVIEW finding #9):
      `list_devices_result` emits `None` (or a success flag) instead of `[]` on
      `DBusError`; `DeviceListWindow` keeps the previous table on failure. Correlated
      path already shows the pattern (`_on_correlated_devices` re-queues).
      *(Done — `pyqtSignal(object)` on both thread and client declarations;
      DBusError + disconnected fast-fail emit `None`; the window ends the
      refresh cycle and keeps its table; the engine's HID safety net no-ops on
      `None`. DESIGN signal table/prose updated. Tests: payload asserted on
      both error paths + fast-fail, table-keeps-previous, pending-set-untouched.
      Commit `41f6324`, **588 passed**.)*
- [x] **6.4 Update `README.md`, `docs/DESIGN.md` (module overview + new signal
      contracts), `AGENTS.md` project structure, and tick the stale
      `TODO.md` item "Delete or rename `Device.is_hid()`" (already done —
      `tests/test_device.py:93` asserts it's gone).**
      *(Done — README lock/treatment sections were updated with 6.1/6.2;
      this pass added `decision.py`/`ui_strings.py` module rows, the
      DecisionEngine signal table and the narrowed-gate prose to DESIGN,
      rewrote AGENTS point 1 + project structure + settings paragraph, and
      ticked the is_hid TODO item. Commit `00bd0c7`, 588 passed.)*
- [x] **6.5 Add the gate unit tests** (table-driven: connected/locked/HID/settings
      combinations → allowed/denied + reason).
      *(Done within `3d218e9` — `tests/test_gate.py` now carries
      `TestHidAllowGated` (6 rows) and `TestBlockReason` (10 rows across
      daemon/lock/HID/treatment/target), plus view-level matrix tests in
      `test_device_dialog.py` and `test_device_list.py`.)*

### Phase 7 — Wrap-up

- [x] **7.1 Final `make check` + `make coverage`** — no coverage regression.
      *(588 passed, TOTAL 92% — same as Phase 5 end; also re-run after the
      7.3 comment fixes: green.)*
- [x] **7.2 `graft build --deep`** so the local graph matches the new structure.
      *(Wiring graph rebuilt: 1088 nodes / 2221 edges, `graft check` OK. The
      deep concept tier needs GRAFT_API_KEY — not set here, so the structural
      layer is current and the meaning tier stays pending.)*
- [x] **7.3 Full review pass of the diff against the HID contract**
      (`git diff master...core_refactor` read end-to-end).
      *(Read all 6672 diff lines. Found and fixed: 4 shim-era comments in
      src (app `__init__`, DecisionEngine docstring, `_lock_available` init,
      "whose actions are disabled"), DESIGN's `schedule_lock` row described
      the emission backwards, AGENTS "Touch either" with three referents.
      Verified on purpose: correlated path still emits `[]` (its re-queue
      retry needs it), `_LIVE_AUTHORIZE_PROMISES` still referenced by
      tests, no secrets / commented-out code. Commit `77530d4`.)*
- [x] **7.4 Update `TODO.md`**: remove/adjust items now done or changed by the
      refactor (lock-gate refinement, device-list blanking, is_hid).
      *(is_hid ticked at 6.4; lock-gate (`3d218e9`) and blanking (`41f6324`)
      ticked here; the anti-lockout test item was also stale — covered by
      `TestAntiLockoutBranch` since `747afae` — and the race-fix item's
      "land before the lock-gate" sequencing note reworded to past tense.
      Commit `77530d4`.)*
- [x] **7.5 Decide merge strategy**: Phases 0–5 as one PR (pure refactor),
      Phase 6 as follow-up PR(s) (behavior). Or land sequentially on master —
      either way, do not squash phase 6 into the refactor commits.
      *(Decided: two stacked PRs. `core_refactor_pr1` @ `1ce3b10` (Phases 0–5)
      → master; `core_refactor` @ `77530d4` (Phase 6) → base
      `core_refactor_pr1`. Branches created locally; push blocked in this
      environment — GitHub rejects the SSH key (`Permission denied` for
      `~/.ssh/id_ed25519`), so the push + PR open is a manual step.)*


## 0.7.4

- [x] COPR integration (src-rpm upload, rebuild flow)
  - [x] Verify GitHub→COPR webhook fires a build on tag — verified on v0.7.4 (build 10928231, 2026-09-01)
- [x] Docs reorg: `DESIGN.md`, `AUDIT.md`, `REVIEW-2026-08-28.md`, `REVIEW-2026-08-30.md` → `docs/` (tracked), README pointer.


## 0.7.3

- [x] Any local users and a wheel-only users packages plus core (polkit) — the core/users split shipped.
      **Not** "packaging done": the open/strict subpackage split is still pending as the open 1.0 item in `TODO.md`.
- [x] Fix the screen locking inhibition.
- [x] Option to stop the special HID device handling.
- [x] Circuit Breaker Pattern for USBGuard Client.
