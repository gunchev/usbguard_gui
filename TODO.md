# Roadmap


## Post-1.0

- [ ] **Disconnect tier** — `[Disconnect Once]` / `[Disconnect Always]`: rename today's Reject to
      match what the kernel call actually is (`DeviceManagerBase::sysfsRemoveDevice` → `remove=1`).
      **Hard-coupled to the persistent-rules editor below.** A permanent `reject` rule fires on
      every match, so the device is removed on sight and never appears in the device list — there
      is nothing to click to undo it. Shipping `Disconnect Always` without the editor creates
      unrecoverable ghost devices. `Disconnect Once` is safe alone (live-only, no persistent trace)
      but ships with its sibling so the pair stays symmetric.
      Escalate the confirmation when `with_connect_type == "hardwired"`: *"This device cannot be
      reconnected without a reboot."* That sentence is the reason the word changed — it reads as
      nonsense next to "Reject".
- [ ] **Persistent-rules editor** (read + delete). **Promote — see finding 12.** `remove_rule()`
      is implemented on the client and has **no UI caller**, so there is no way to revoke a
      permanent allow, no way to inspect a permanent block, and no way to reach a hand-written
      rule from inside the app. Scope it read + delete: rule id, target, predicate, and **whether
      it currently matches a present device**; searchable by vid:pid / serial / name.
      Read-only + delete deliberately avoids the ordering footgun — `RuleSet::getFirstMatchingRule`
      returns the *first* match in `_rules` order, so any editor that lets users add or reorder
      without exposing position will silently produce shadowed rules that never fire.
- [ ] **Layout choice: 6 buttons vs 3 buttons + checkbox.** Two equivalent expressions of the same
      2×2 (authorization × durability):
      `[Allow Always] [Allow Once] [Block Once] [Block Always] [Disconnect Once] [Disconnect Always]`
      — or — `[Allow] [Block] [Disconnect]` + `[ ] Make this permanent`.
      The checkbox form matches the orthogonal model exactly, keeps every button to one word, and
      can *display* current state ("Currently permanently allowed — unchecking removes the rule"),
      which makes the persistent layer visible at the moment of decision. Ship behind a setting so
      users pick. Note the tray context menu may have to stay button-only (no room for a checkbox).
- [ ] **Configurable rule identity** — let the user choose which predicates a permanent rule
      carries: `hash`, `parent_hash`, `via_port`, `with_interface`, `with_connect_type`
      (`_RULE_IDENTITY_ATTRS` is a fixed tuple today). This is a **specificity ↔ portability** knob:
      dropping `via_port` lets a rule follow a device across dock/KVM topology; dropping `serial`
      widens it to a whole model line.
      **Caveat: those same attrs are the dedup key.** Widening a rule widens what `_rule_identity()`
      treats as "the one permanent rule for this device", so a broad rule can silently own several
      devices' decisions. Enforce a minimum — a rule naming no device returns `None` from
      `_rule_identity()` and is unmanageable by design, which is exactly what protects hand-written
      `all-of { ... }` policy from tray clicks.
- [ ] **Root-hub (`1d6b`) collapse — detect the stale PCI-addressed rules and offer to fix.**
      The kernel's own root hubs carry the Linux Foundation VID `1d6b`, and a permanent rule
      recorded from one is **PCI-addressed twice over**: `serial` *is* the host controller's PCI
      address (`0000:06:00.3`) and `parent-hash` hashes the parent PCI device. Any renumbering —
      kernel update, BIOS, module order, USB4/Thunderbolt state — moves both, the rule stops
      matching, and the controller falls to the implicit floor (`ImplicitPolicyTarget=block`),
      which the device list reports as `Temporary`. Observed on `dg-lp2`: nine root hubs, seven
      `Temporary`; the eight `1d6b` rules in `rules.conf` were recorded at `05:00.x`/`06:00.x`
      while the controllers now sit at `06:00.x`/`07:00.x`, so that whole block matched nothing —
      two devices failed on `parent-hash` alone with `serial` and `hash` both still correct. Only
      the two controllers re-recorded after the shift held.
      The fix is two VID-level rules above the stale ones:

      ```
      allow id 1d6b:0002 with-interface 09:00:00
      allow id 1d6b:0003 with-interface 09:00:00
      ```

      Safe **only** because `1d6b` is the kernel's root-hub VID and no unpluggable peripheral can
      present it. That is why the app must hard-code the VID instead of generalising to "drop the
      serial from a rule" — the same widening against a real vendor VID is the indefensible rule
      this project exists to prevent.
      **Detect, don't wait for the user to notice:** root hubs present as `with_interface 09:00:00`
      with no matching permanent rule, and/or permanent `1d6b` rules matching no present device.
      Offer one action — *collapse to VID-level rules* — that inserts the two lines above the
      stale ones (first-match-wins) and deletes what they replaced; the leftovers are dead weight
      otherwise, and the manual path belongs to the persistent-rules editor above.
      Reuse `rule_matches_device()` / `rule_identity()` / `rule_is_broader_than_device()`, and keep
      the `None` verdict load-bearing: a rule above the match that cannot be read must **suppress**
      the offer, not trigger it.
      Two constraints:
      **Never route this through the per-device `Always` path.** A `1d6b` rule *names* a device, so
      `RULE_IDENTITY_ATTRS` makes it the single permanent rule for **every** root hub — one click
      on one controller would own all nine. That is the `Configurable rule identity` caveat above,
      arriving on real hardware: write it as an explicit admin-level rule with its own identity,
      or refuse per-device `Always` on root hubs and point the click at the collapse action.
      **Root hubs are never HID** (`09:00:00`, hub class), so the collapse touches none of the HID
      lock-first contract — but it must not be able to shadow a HID rule placed above it, which is
      the ordering check the insert needs anyway (`Policy::appendRule` rejects `parent_id = 0`, so
      landing a rule at position #1 is its own special case).
- [ ] Broader DE testing: GNOME (with systray extension), LXQT, LXDE, XFCE, others.
- [ ] Warn when an action would leave no local input device. **Warn, never forbid** — the app
      cannot see BMC/IPMI/serial console/SSH, so any hard prohibition is wrong for a real share
      of deployments (a network-only server with all local input permanently denied is a
      legitimate, arguably stronger posture). Escalate the confirmation on what the app *can*
      see: target is HID; `with_connect_type == "hardwired"` (cannot be unplugged); no other
      allowed HID would remain. Always name the recovery path. `Device.with_connect_type` is
      already parsed and displayed but read by no decision.
      See `docs/REVIEW-2026-09-28.md` findings 2, 3 and 12.


## 1.0

- [ ] Polkit policy subpackages: `usbguard_gui-policy-open` (recommended default, all local console sessions) +
      `usbguard_gui-policy-strict` (wheel-only), mutual Conflicts, README trade-off section, both rules kept in `rpm/`
      for non-RPM installs. No-policy mode = admin password per action (fail-closed, documented).
- [ ] Catch-all policy detection: bare `allow`/`reject` rule in the ruleset → persistent tray-tooltip warning +
      one notification per session + "dead" tray icon variant (second SVG in RPM/theme + dev-mode fallback).
- [ ] Test on LXQT (and XFCE if that machine is reachable).
- [ ] **TUI front end over the same core (proof, not a product).** A
      [textual](https://textual.textualize.io/) app sharing `decision.py`, `gate.py`, `device.py`
      and `dbus_client.py` with the tray GUI. **The point is to demonstrate the core/UI split is
      real** — the security contract must be provable from one implementation, not two that drift.
      What is Qt-free today and reusable as-is: `device.py`, `gate.py`, `ui_strings.py`, and the
      pure functions inside `decision.py`. What is not, and is the actual work:
      `DecisionEngine` is a `QObject` that publishes via `pyqtSignal`, `AsyncWorkerThread` is a
      `QThread`, and `ScreensaverMonitor` is a `QObject` — so the threading model has to be
      abstracted (asyncio + a plain callback/signal seam) before a non-Qt host can drive it. Keep the
      tray GUI as the shipped product; the TUI is a second consumer, not a rewrite.
      **Constraints:** the HID lock-first flow (`README.md` → *How It Works → HID Devices*) must be
      identical in both hosts — same `gate.py` calls, same ordering — and the shared test suite has to
      run against both, or the "proof" only proves the copy. Terminal-only hosts also lose the
      polkit-agent path and any notification surface, so scope it to the device list + action dialog.
- [ ] Release v1.0 (`release.py`).
