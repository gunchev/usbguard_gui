# HID Unlock Quarantine — Design and Threat Model

**Status:** approved design, implementation pending (`TODO.md` → 1.0).
**Origin:** `docs/REVIEW-2026-11-15.md` finding 2 (High, 1.0 release blocker).
**User-facing contract:** `README.md` → *How It Works → HID Devices* (plain language, same
behaviour). This document is the full design: assumptions, use cases, corner cases, the threat
model, and why each decision is what it is.

---

## 1. The problem this design closes

A HID device must sometimes be allowed while the screen is locked: a newly-attached keyboard
may be the only way to type the unlock password (the anti-lockout property). Every such allow
is a hazard — the device was never user-confirmed. Without a re-check at unlock, the allow
lasts until the device is unplugged, and a keystroke injector planted on an unattended
machine is live the moment the owner unlocks (`docs/REVIEW-2026-11-15.md` finding 2, the A2
threat).

The design decides *when* such an allow may happen at all (§3.2 — only when the device might
be the machine's only keyboard), and what happens to it at unlock (§3.2/§3.3 — quarantine,
or exemption when the device provably carried the password).

## 2. Assumptions

| #       | Assumption                                                                                                                                                                                                       | Provenance                          | Consequence if wrong                                                                                       |
| ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| A1      | A deliberate interaction with the app's own controls — a click, or keyboard activation of a focused button — is proof a person is present. The app cannot and does not verify which physical device produced it. | maintainer's decision               | The manual-allow gate (finding 7) would then be justified; as it stands, manual allows are not lock-gated. |
| A2      | The planted-device attack — a physical injector on an unattended machine, waiting for the owner to unlock — is in scope.                                                                                         | maintainer's decision               | Finding 2 would be informational, not High.                                                                |
| A5      | The attacker is a physical device, not local code. Local malware can call `applyDevicePolicy` through the shipped polkit rule; the app's gates guard its own UI, not the system.                                 | maintainer-confirmed             | Nothing in this design is a boundary against local code.                                                   |
| A7      | Inserted devices arrive blocked (`ImplicitPolicyTarget=block`), and the daemon reports `target=BLOCK` identically for the implicit floor and for a user's block rule — only rule matching distinguishes them.    | from upstream source (A6)           | Suppression (§3.5) would need a different signal.                                                          |
| **A10** | **The unlock is done by typing a password.**                                                                                                                                                                     | **maintainer's decision, explicit** | See below.                                                                                                 |

**A10 in detail.** The unlock exemption (§3.2, third moment) reasons: *the armed device owns
the machine's only live keyboard, therefore the password was typed through it, therefore a
person drove it.* That inference holds only when unlocking required a password. Where the
desktop unlocks without one — KDE's grace period, blanking without lock, lock-after-delay —
the armed device's own keypress can end the lock, and the exemption would fire on the
attacker's device. Three things cover this:

- the exemption is denied for *unverified* cycles (enumeration failures, see §3.4);
- finding 3 ("screensaver active" ≠ "password required") is a separate 1.0 release blocker
  with per-desktop verification planned;
- post-1.0, PAM-lite (`docs/pam-lite.md`) supplies the "a password unlock happened" signal,
  which makes the exemption airtight.

Nothing else in this design depends on A10: arming, the confirm flow, rule-match suppression
and the quarantine warning all hold without it.

**Hardware-verified enumeration facts (2026-11-16, this repo's dev machine).** Every input
device the kernel knows is listed under `/sys/class/input/` with world-readable `name`,
`id/bustype` and `capabilities` — read as uid 1000, no privileges, no D-Bus, no display
server (the evdev layer beneath both X11 and Wayland). Bus types observed: `0011` (i8042 —
the internal laptop keyboard), `0018` (I2C — internal touchpad), `0003` (USB — an external
2.4 GHz receiver), `0005` (Bluetooth), `0019` (host — power/sleep/lid buttons, WMI hotkeys;
not user-typable). HID devices symlink into the USB interface directories whose names match
USBGuard's `via-port`, so input nodes can be attributed to the USB device that owns them.
A blocked USB device never binds `usbhid`, so it has no input node — the enumeration counts
only *live* inputs for free.

## 3. The design

### 3.1 Decisions (condensed)

| Area | Decision |
|---|---|
| HID treatment default | ON (the shipped default). |
| Insert while unlocked, another live input present | Confirm prompt (~16 s countdown), no auto-lock, nothing armed; timeout applies nothing (fail-closed). |
| Insert while unlocked, might be the only input | Notice, 5 s, screen locks, allow behind the lock; armed at insert. |
| Insert while locked | Auto-allowed only if it might be the only live keyboard (kernel input check, §3.4); otherwise deferred and prompted at unlock. Enumeration failure: arm anyway (anti-lockout outranks verification), mark the cycle unverified. |
| At unlock | Block armed devices **first**, then: exempt if the device owns the only live keyboard, no baseline keyboard vanished, and the cycle is verified; otherwise the persistent warning. |
| Quarantine warning | Persistent window, no timeout, no Escape, no default Allow; Keep Once (default) / Keep Always / Block Always; buttons disabled until the block is confirmed; plain text for every device-supplied string; replug recovery with a port-change notice. |
| Block durability | Rule match: the flow consults the ruleset in rule order before running; a matching permanent block rule suppresses it — the device prompts instead, never silently. Requires the last-known-ruleset behaviour on failed reads (finding 10). |
| Manual allows | Not lock-gated (finding 7). The invariant is: *no automatic and no held allow of a HID device outside a working lock; a deliberate click is never lock-gated.* |
| Replug match key | Port-agnostic `(id, serial, hash)`; a tray notice when the returning port differs from the stored one. |
| PAM scope | Best-effort in 1.0 (the burst-at-unlock residual documented); PAM-lite is the post-1.0 upgrade, `docs/pam-lite.md`. |
| Deferred | Fullscreen attention splash (2.x). Not pursued: random-delay replug challenges, trusted-input detection, notices instead of blocking. |

### 3.2 The three moments

**Insert while the screen is unlocked** (`on_device_inserted`). The engine asks the input
inventory whether another *live* input device exists (any bus — internal keyboards and
touchpads count, because the kernel sees them even though USBGuard cannot). If one does,
the user is at the machine and can answer a prompt: the ordinary dialog appears with a
16-second countdown (shorter than the ordinary 30 s — the user is present), and Allow
applies directly. No notice, no scheduled lock, nothing armed — the click is the proof (A1).
If nothing else can click the prompt — the new device might be the user's only input —
the lock flow runs: a notice, five seconds, the screen locks, and only then is the device
temporarily allowed.

**Insert while the screen is locked.** The engine takes the union of the lock-time baseline
and the current inventory (§3.4). If any other live keyboard exists, the device is **not**
allowed behind the lock: it is deferred and prompted at unlock, together with the other
devices that connected during the absence. Only when the new device might be the only
keyboard is it auto-allowed immediately, so the user can type the password with it. Either
way the engine arms the allowed one at insert — the identity is only fully known there, and
a device that goes pending but is unplugged before the lock fires sends no allow and arms
nothing.

**Unlock** (`on_screensaver_unlocked`, ahead of the non-HID deferral handling). Every armed
device still present is blocked first, before any prompt or notice can be answered — until
the block lands, the armed device is itself a working input and could click its own Allow.
Then, for each armed device: if it owns the only live keyboard, no baseline keyboard
vanished, and the cycle is verified, it is **exempt** — the password went through it, so the
unlock itself is the proof; the device stays allowed and nothing is shown. Everything else
falls back to the persistent warning.

### 3.3 The quarantine state machine

Keyed by the port-agnostic identity `(id, serial, hash)` — never the USBGuard device number
(per-incarnation, changes on replug) and not `dialog_identity` (which is port-strict for
dialog retargeting and held decisions, where that strictness is right). Entries also store
the last number, name, id, port, parent hash and an `unplugged` flag.

| State | Entered when | Device | Exit |
|---|---|---|---|
| **Armed** | An auto-allow behind the lock: insert-while-locked (only-keyboard case), the lock-flow allow, or the snapshot safety net | Allowed while the screen is locked | Unlock → **Quarantined** or **exempt**; REMOVE before unlock → drop the entry |
| **Quarantined** | Unlock, after the block is confirmed, device still present, exemption not applicable | Blocked (`BLOCK/UNCHANGED`) | REMOVE → **Quarantined-absent**; Block click (Always only) → **Resolved (blocked)**; Keep click (Once/Always) → **Resolved (kept)** |
| **Quarantined-absent** | REMOVE after quarantine | Gone; entry survives | INSERT same identity while unlocked → **Replugged** (allowed, bypassing the five-second lock flow; tray notice if the port differs); INSERT while locked → normal flow, entry stays; Block click → resolved |
| **Resolved (blocked)** | Block click — always Block Always | Permanent block rule; from Quarantined-absent it is synthesized from the stored identity (no live device number — the durable-write path, not `apply_device_policy`) | — |
| **Resolved (kept)** | Keep click | Once: `ALLOW/UNCHANGED`, leaves no trace. Always: a permanent allow rule | Once: a later auto-allow → **Armed** again. Always: future insertions arrive already allowed and never arm |

The quarantine survives REMOVE: pending-decision state is per-incarnation
(`cancel_pending_device`), while quarantine entries live in the identity-keyed map and are
updated by a dedicated `on_device_removed` entry point — REMOVE arrives at the app's
presence handler with the device's rule string, from which the identity is recovered. A
replug while the screen is locked is not confirmation — the normal flow runs, the entry
stays, the next unlock re-evaluates.

### 3.4 The lock-time baseline and the union rule

Enumerate **before** the rogue can act: a snapshot of the input inventory is taken at the
top of `on_screensaver_locked`, before any behind-the-lock allow is issued. Every decision
trusts the **union of baseline and current inventory**:

- **arming** requires no other live keyboard *now or in the baseline*;
- the **unlock exemption** requires the armed device to own the only live keyboard **and**
  no baseline keyboard to have vanished.

A shrink — masking, the user's own unplug, a Bluetooth keyboard sleeping through the lock —
denies the exemption and falls back to block + warning. Fail-safe: the honest cases pay one
Keep click; the masking attack gets nothing. **Enumeration failure falls asymmetrically:**
at *arming*, arm anyway (anti-lockout outranks verification) but mark the cycle
*unverified*; at *unlock*, an unverified cycle cannot take the exemption — block + warn.
Anti-lockout fails open; the exemption fails closed.

The warning copy after a shrink or an unverified cycle should say the user may have typed
their password into the device — treat it as compromised.

### 3.5 Rule-match suppression (finding 10)

Before the HID flow runs, the engine matches the device against the ruleset in rule order
(the same matcher the device list uses); a matching permanent **block** rule suppresses the
flow — the device prompts (with the prompt cooldown), never silently. The suppression is
durable across restarts and enforced by the daemon even with the app dead.

A failed rules read keeps the last-known ruleset instead of reporting empty (finding 10):
the ruleset is load-bearing security state, and an empty-on-failure read would silently
erase every suppression. "Rules unknown" — before the first ruleset arrives — means no
suppression; arm + quarantine catches it.

### 3.6 Manual allows (finding 7)

Manual HID allows are not lock-gated: `block_reason` reports daemon connectivity only, and
the dialog's Allow is enabled whenever the daemon is connected. The lock-unavailable notice
explains that HID devices prompt instead of being auto-allowed. What the lock gates is the
*automatic* flow (`_hid_lock_flow_applies`, `lock_for_pending_hid`) and held allows — those
never authorize a HID device outside a working lock.

### 3.7 PAM scope

1.0 ships the best-effort signal block plus the quarantine; the burst-at-unlock residual is
documented (§7). PAM-lite (`docs/pam-lite.md`) is the post-1.0 upgrade, shipped only if
field data demands it. Upstream asks to pursue: KDE kscreenlocker `AboutToUnlock` + hold;
the freedesktop ScreenSaver spec; GNOME Shell; wayland-protocols `ext-session-lock-v1`.
The end-state worth driving toward: lockers report unlock early, and usbguard itself grows
lock-aware temporary allows — enforced app-independently, for every frontend.

## 4. What a rogue device can and cannot do

The enumeration trusts the kernel; the kernel trusts only the bus topology.

**A USB device cannot:**

- remove or alter *another* device's sysfs entries — `/sys/class/input/*` is kernel-owned
  and a USB device has no write path into it;
- impersonate an internal keyboard — `id/bustype` is set by the transport driver that
  registered the node (`usbhid` → always `0003`; the internal keyboard is
  `0011`/`0018`/`001c` no matter what descriptors the device claims);
- inject keystrokes without an evdev input node — injection into the session goes through
  the input subsystem — nor hide its own node while typing;
- create `BUS_VIRTUAL` input nodes — that requires userspace `uinput` (root), i.e. local
  code, which is A5 and already wins via polkit;
- forge its interface list past the tokenizer (finding 1), or forge the permanent-block
  check (rule match in rule order, §3.5).

**A USB device can — and the defence that catches it:**

- sit **upstream of the victim's device** (a rogue hub, an inline shim) and drop it off the
  bus → the lock-time baseline: a vanished keyboard denies the exemption and arming;
- present **fake extra keyboards** → fails safe: more warnings, never fewer exemptions;
- fake a **same-port replug** (self re-enumeration) → the replug recovery accepts it
  (residual); the persistent warning's Block Always is the remedy;
- **wait out the unlock edge** and fire a first burst of keystrokes → block-at-unlock
  removes persistent access; the burst window itself is the accepted best-effort residual
  (§7; PAM-lite post-1.0 would close it);
- be a **perfect inline proxy** of the user's own keyboard — forwarding the user's input
  and injecting alongside it — which is indistinguishable from the real device at any
  software layer. The baseline catches *vanishing*, not *proxying*. This is an inherent
  residual of every USB-layer defence, the quarantine and replug included (§7).

## 5. Use cases

1. **New keyboard while working** (a mouse or internal touchpad is available). The confirm
   prompt appears; the user clicks Allow; no lock, nothing armed. If they walk away mid-prompt,
   the countdown expires, nothing is applied, the device stays blocked.
2. **Wireless dongle re-enumerates behind the lock — the machine's only input.** It is
   allowed behind the lock (anti-lockout); the user types the password with it; at unlock it
   owns the only live keyboard, no baseline keyboard vanished, the cycle is verified →
   **exempt**. No warning, nothing to click. This is the case the whole baseline machinery
   exists to make painless.
3. **Injector planted on a laptop while locked** (internal keyboard present). The kernel
   input check sees the internal keyboard → the injector is *not* allowed behind the lock;
   it stays blocked and is prompted at unlock with the absence summary.
4. **Injector planted on a desktop whose USB keyboard is present and allowed.** Same: not
   allowed behind the lock, prompted at unlock.
5. **Keyboard plugged while locked, nothing else on the machine.** Allowed (anti-lockout),
   armed; the user types the password with it; at unlock → exempt, as use case 2.
6. **Same as 5, but a Bluetooth keyboard connected during the lock.** At unlock the armed
   device is not the only keyboard → blocked + persistent warning. The user who typed with
   the Bluetooth keyboard clicks Keep Once, or Keep Always if the arrangement is permanent.
7. **The warning itself.** Keep Once (allowed until unplugged; a later auto-allow re-arms),
   Keep Always (permanent rule; ends dock/KVM re-warn cycles for good), Block Always
   (permanent block; from Quarantined-absent the rule is synthesized from stored identity).
   Buttons stay disabled until the block is confirmed. If the device is the only input at
   all, unplug and replug — same port or another — and it works; the window says so.
8. **Permanently blocked keyboard replugged.** The rule match suppresses the flow; it
   prompts like any other device. A recorded Block is never silently overridden behind a
   lock.
9. **Locker-less session** (no screen-lock service). No automatic flow; HID inserts prompt;
   manual Allow works — the click is the proof (finding 7 removed).
10. **Daemon down at unlock.** The block cannot be sent; Allow stays disabled, the failure
    is surfaced, retried on reconnect (finding 13's silent return is not acceptable here).
11. **Dock/KVM re-enumeration each lock.** Re-arms each cycle → warning each unlock until
    the user clicks Keep Always; then insertions arrive already allowed and never arm again.

## 6. Corner cases

1. **Bluetooth keyboard sleeps during the lock** (in the baseline, gone now) → union rule:
   no exemption → warning, one Keep click. Fail-safe by design.
2. **The user unplugs their own keyboard during the lock and plugs a new one.** Baseline
   shrink → no exemption → warning; honest cost, one click.
3. **A touchscreen is present.** On-screen keyboards can carry a password, so touchscreens
   un-exempt: warning instead of exemption.
4. **Mid-lock lockout.** An armed device is the only live keyboard; the user plugs their own
   spare while locked → the spare sees "another keyboard exists" → deferred, blocked → the
   user cannot type. Recovery: unplug one device (the unfamiliar one), replug. Narrow,
   documented, inherent to any arming heuristic.
5. **Enumeration failure.** Arm anyway (anti-lockout), mark unverified → no exemption at
   unlock, block + warn (§3.4).
6. **Rogue hub masks the user's keyboard.** The baseline catches the disappearance → no
   exemption, warning.
7. **Identical unit / descriptor clone replugged on another port.** The port-agnostic key
   inherits the allow; the port-change notice leaves a trace. A same-port clone is
   indistinguishable under any keying scheme — inherent.
8. **App restart during a lock cycle.** Quarantine state is in-memory and lost; an
   allowed-armed HID is indistinguishable from a user Allow Once. Documented residual
   until PAM-lite forces armed-list persistence.
9. **Multiple HID inserted behind the lock with no other input.** The first arms (only
   keyboard); the second sees "another keyboard exists" → deferred. At most one unverified
   keyboard is live behind a lock.
10. **Passwordless unlock** (A10 broken). Unverified-cycle rules and the warning fallback
    apply; per-desktop verification is finding 3's release-blocker work.
11. **Held Allow on a HID device** (clicked while the device was off the bus). Still handed
    to the lock-first flow, never applied directly outside the lock — unchanged.
12. **Device-supplied strings.** Rendered as plain text everywhere in the warning and
    notices (finding 8); no rich text, no markup.
13. **The warning shows no USBGuard device number** — name, VID:PID and port only; the
    number is per-incarnation and meaningless to users.
14. **Replug while the screen is locked** is not confirmation: the normal flow runs, the
    entry stays, the next unlock re-evaluates.
15. **Non-HID deferrals** keep the correlated unlock summary; HID devices are never mixed
    into that set.

## 7. Residual risks (accepted, documented — not hidden)

- **Burst at the unlock edge.** The device is live until the block lands; a pre-programmed
  burst in that window can get keystrokes in. Persistent access is removed; the window is
  not. Shrunk by the arming rule (a machine with another keyboard never arms a stranger);
  open on only-keyboard machines and passwordless lockers. PAM-lite (post-1.0) closes it.
- **Inline hardware proxy.** §4 — undetectable at any software layer.
- **Passwordless/grace-period unlock** (A10, finding 3) — the exemption's inference breaks;
  the warning fallback and the unverified-cycle rule carry it until per-desktop semantics
  are verified.
- **App restart** — in-memory quarantine lost (§6.8).
- **Daemon down at unlock** — the block cannot be applied until reconnect; surfaced, not
  silent.
- **Same-port clone / self re-enumeration** — the replug recovery accepts it; Block Always
  is the remedy.
- **Previously allowed malicious HID, or local code** (A1/A5) — a device allowed before the
  lock, or malware running as the user, is outside this design's boundary entirely.

## 8. Not in 1.0

- **Fullscreen attention splash** — 2.x: best-effort on Wayland, and only meaningful after
  block-first.
- **PAM-lite** — post-1.0, only if field data demands it (`docs/pam-lite.md`).

## 9. References

- `docs/REVIEW-2026-11-15.md` — findings 1–18, assumptions A1–A9; finding 10 is High.
- `handover.md` — the working session record for this change (decisions with rationale,
  the state machine, implementation notes, residuals); retained until the work ships.
- `README.md` → *How It Works → HID Devices* — the user-facing contract.
- `docs/pam-lite.md` — the post-1.0 strong-unlock design note.
- `AGENTS.md` → *Read this first* — the agent-facing invariant.
