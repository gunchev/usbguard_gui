# Roadmap


## Post-1.0

- [ ] Broader DE testing: GNOME (with systray extension), LXQT, LXDE, XFCE, others.
- [ ] UI to manage permanent rules (edit/disable).
- [ ] Warn when an action would disable the last keyboard/mouse HID.


## 1.0

- [ ] Unlock-queue race: correlated per-call device fetch (Option 3) — `fetch_devices()` + `list_devices_correlated(id, devices)`,
      id→id-set dict in the app, device-list window untouched. Closes AUDIT follow-up items 2 & 3.
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

- [x] Any local users and a wheel-only users packages plus core (polkit) — realized as the 1.0 subpackages above.
- [x] Fix the screen locking inhibition.
- [x] Option to stop the special HID device handling.
- [x] Circuit Breaker Pattern for USBGuard Client.
