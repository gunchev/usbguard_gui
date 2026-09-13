# Roadmap


## Post-1.0

- [ ] Rename the dialog's **Close** button (it applies Reject — electrical disconnect, device
      forgotten). Label it **Reject** to match the device-list menu, or split into
      "Reject" + a real dismiss that applies nothing; README now has to explain the mismatch.
- [ ] Broader DE testing: GNOME (with systray extension), LXQT, LXDE, XFCE, others.
- [ ] UI to manage permanent rules (edit/disable).
- [ ] Warn when an action would disable the last keyboard/mouse HID.


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
      undecidable-by-design `None` verdict. Land this **before** the lock-gate refinement below — its
      tests are the baseline that change has to keep green — and its `rule_matches_device()` /
      `rule_persistence_problem()` are the primitives the catch-all detection should reuse.
- [x] Unlock-queue race: correlated per-call device fetch (Option 3) — `fetch_devices()` +
      `list_devices_correlated(id, devices)`, id→id-set dict in the app, device-list window
      untouched. Closes AUDIT follow-up items 2 & 3 (both reproduced red, then fixed;
      `TestUnlockQueueRaceReproductions`). Done 2026-09-12.
- [ ] Polkit policy subpackages: `usbguard_gui-policy-open` (recommended default, all local console sessions) +
      `usbguard_gui-policy-strict` (wheel-only), mutual Conflicts, README trade-off section, both rules kept in `rpm/`
      for non-RPM installs. No-policy mode = admin password per action (fail-closed, documented).
- [ ] Catch-all policy detection: bare `allow`/`reject` rule in the ruleset → persistent tray-tooltip warning +
      one notification per session + "dead" tray icon variant (second SVG in RPM/theme + dev-mode fallback).
- [ ] Lock-gate refinement: gate HID-capable devices only, and only while special HID treatment is enabled and
      screen locking is unavailable (DESIGN.md contract + dialog/device-list gating + notification text + tests).
      Treatment disabled ⇒ no gating at all; lock-availability changes are log-only then.
- [ ] Test on LXQT (and XFCE if that machine is reachable).
- [ ] Release v1.0 (`release.py`).


## 0.7.4

- [x] COPR integration (src-rpm upload, rebuild flow)
  - [x] Verify GitHub→COPR webhook fires a build on tag — verified on v0.7.4 (build 10928231, 2026-09-01)
- [x] Docs reorg: `DESIGN.md`, `AUDIT.md`, `REVIEW-2026-08-28.md`, `REVIEW-2026-08-30.md` → `docs/` (tracked), README pointer.


## 0.7.3

- [x] Any local users and a wheel-only users packages plus core (polkit) — the core/users split shipped.
      **Not** "packaging done": the open/strict subpackage split is still pending as the 1.0 item above.
- [x] Fix the screen locking inhibition.
- [x] Option to stop the special HID device handling.
- [x] Circuit Breaker Pattern for USBGuard Client.
