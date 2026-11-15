---
title: A deliberate click is enough to allow a keyboard; the lock-first rule covers only automatic and held allows
author: Doncho
date: 2026-11-15
branch: core_refactor
cost: { minutes: 24, tokens: 322621 }
id: l-c5719539
scope: repo
status: active
src: agent
touches:
  - docs/REVIEW-2026-11-15.md
  - TODO.md
  - AGENTS.md
  - README.md
  - DONE.md
  - src/usbguard_gui/gate.py
  - src/usbguard_gui/device_dialog.py
  - src/usbguard_gui/decision.py
  - src/usbguard_gui/app.py
  - src/usbguard_gui/dbus_client.py
---

## Decided

The maintainer settled four points during the 2026-11-15 review:

- A manual Allow on a HID device needs no screen lock. The click itself shows a person is at the machine. The lock-first rule governs automatic allows and held (queued) allows only.
- An attacker who plants a keystroke injector on an unattended machine and waits for the owner to unlock is inside the threat model. A HID device auto-allowed behind the lock must not simply stay live after unlock with no word to the owner.
- KDE Plasma plus GNOME, LXQt and XFCE are 1.0 targets, so the lock signal has to be checked on each of them before release.
- The KVM/dock rule-placement fix has not been confirmed on real hardware; it stays an open verification item.

## Tried and ruled out

- "A manual HID Allow should lock the screen first" was offered and not chosen: the click is treated as proof of presence.
- "Keep the lock-unavailable refusal as deliberate friction" was offered and not chosen. With a click being enough, refusing that click when no locker is reachable blocks a legitimate action for no security gain. Whether to remove the gate or keep it is still an open code decision; the wording that justifies it as "cannot lock first" is wrong either way.
- For the planted-device attack, "out of scope" and "a notice at unlock is enough" were both offered and not chosen.
- "KDE only for 1.0" was offered and not chosen.

## Watch out

- Three texts state a stronger invariant than the one decided ("may only be allowed while the screen is locked"). State it as: no automatic and no held allow of a HID device outside the lock.
- Do not read this decision as licence to loosen the automatic paths. They must still refuse to run without a working lock, and a held Allow must still never authorise a HID device.
- Every one of these rules depends on the app telling HID from non-HID correctly, and the review found the interface list can be forged through a device's own name. Fix that before relying on any of the above.

In the code:
- Review with the full reasoning and the recorded answers: docs/REVIEW-2026-11-15.md (assumptions A1-A4, findings 1, 2, 3, 7).
- Click gate: src/usbguard_gui/gate.py (hid_allow_gated, block_reason).
- Stronger wording to restate: AGENTS.md "Read this first" item 1; README.md "When Screen Locking Is Not Possible"; the _allow_enabled docstring in src/usbguard_gui/device_dialog.py.
- Automatic and held paths that must stay lock-bound: src/usbguard_gui/decision.py (on_device_inserted, on_screensaver_locked, apply_pending_decision).
