# Done

Completed roadmap items, moved out of `TODO.md` at every release so the roadmap
carries only open work. Entries keep their version section and their full text --
the reasoning behind a decision is why these are kept, not the checkbox. See
*Releasing* in `AGENTS.md`.


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
