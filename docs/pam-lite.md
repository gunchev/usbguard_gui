# PAM-lite — the optional strong-unlock hook (design note, not shipped)

**Status:** design note, not shipped. Ships **post-1.0 at the earliest**, and only if field
data shows the burst-at-unlock residual (`docs/HID-QUARANTINE.md` §7) matters to real users.
Nothing here is installed, generated or edited automatically — a wrong PAM edit can lock the
user out of their machine.

## The problem it solves

At unlock, an armed HID device is live until the app's block lands (signal → app → daemon →
sysfs). A pre-programmed device can fire a burst of keystrokes in that window. The only hook
that runs *after the password is accepted and before the locker releases input* is the
locker's PAM stack — that timing is the entire prize.

## The design

A `pam_exec` script, shipped and documented but **user-installed** into the locker's PAM
service. Steps:

1. The locker's PAM auth succeeds and the stack reaches the script (placement — below).
2. The script (running as root, `PAM_USER` known) reads that user's **armed list** —
   `~/.local/state/usbguard_gui/armed.json`, a format fixed by this note (see *Upgrade
   path*). No file / empty list → exit 0 immediately (the common case costs milliseconds).
3. `usbguard list-devices`; match devices against the armed identities by
   `(id, serial, hash)`. The parsing is the app's own tokenizer
   (`usbguard_gui.device.parse_device_rule`) — one implementation, no drift.
4. For each match: apply a **live** block via the `usbguard` CLI (`apply-device-policy`,
   not permanent — the app's quarantine owns the durable decision).
5. Verify by re-listing (target `block`, ids matching); retry once; give up after a hard
   timeout (~2 s) and exit 0 — **fail-open by default**, fail-closed as a documented
   configuration choice. A hung script must never hang the unlock.
6. Exit 0; the stack returns success; the locker releases input — now with the armed
   devices blocked.

The script is stateless and self-contained: no root helper, no socket, no app-lifetime
dependency — it works with the app dead, at boot, in any session, and `PAM_USER` hands it
the right user, so multi-session targeting is free.

What it additionally buys once it exists: the **A10 signal** — a hook that ran is a
password unlock that happened — which makes the HID quarantine's only-keyboard exemption
airtight (`docs/HID-QUARANTINE.md` §2, A10).

## The irreducible costs (shared by every PAM variant)

- **Placement is per-distro stack surgery.** On Fedora-style authselect stacks `pam_unix`
  is `sufficient`, so a line appended after `auth include system-auth` **never runs on
  success** — the stack short-circuits past it. Correct placement sits inside the stack,
  above the deny, with the right control flag. Wrong in the other direction, it runs on
  *failed* attempts and blocks the armed keyboard mid-retry while the user is typo-ing
  their password with it. This is why the script is user-installed and per-locker
  documented, never auto-edited.
- **Per-locker service names**: KDE `kde`; GNOME `gdm-password` (shared with login — the
  script no-ops harmlessly at login, since a fresh boot's floor already blocks armed
  devices); Xfce/light-locker/xscreensaver have their own services.
- **Latency** on every auth of that service (milliseconds when no armed list exists; the
  CLI round trip when it does).
- **No help for passwordless unlock** — there is no authentication event to hook; finding 3
  remains its own work.

## Upgrade path from 1.0

The quarantine state is in memory in 1.0. When PAM-lite ships, the app persists the armed
list on every arm, in the format above — a drop-in addition, not a rework: `armed.json` is
append-on-arm, prune-on-resolve, and owner-readable. Note the A5 boundary honestly: the file
is user-writable,
so local code can plant entries — but local code can already block devices through polkit,
and a fake armed list can only cause blocks, never allows.

## Upstream asks to pursue in parallel (the real fix)

KDE kscreenlocker `AboutToUnlock` + hold; the freedesktop ScreenSaver spec; GNOME Shell;
wayland-protocols `ext-session-lock-v1`. The end-state worth driving toward: lockers
report unlock early, and usbguard itself grows lock-aware temporary allows (watch logind
`LockedHint`, revert on unlock) — enforced app-independently, for every frontend, with no
PAM anywhere.
