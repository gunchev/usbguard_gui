# PR #8 — Handoff

**Status (updated 2026-09-13): reviewed, rebased, all four findings fixed, and pushed to the PR head
branch `beorn-:fix/permanent-rules-across-kvm` (`e7d1c15` → `f29d00e`; maintainer edits were enabled).
The PR went `CONFLICTING` → `MERGEABLE`/`CLEAN` and CI is green on Fedora 43/44 with the full tox gate
(run `34748783679`, approved off the first-time-contributor hold). NOT merged — handed back to the
author for KVM field confirmation of finding A's placement.**

| Finding | State | Commit |
|---|---|---|
| **A** — rule ordering / shadowing | ✅ fixed | `1a2feb2` |
| **B** — duplicate accumulation | ✅ fixed | `a19aff4` |
| **C** — non-atomic permanent | ✅ fixed | `437d17b` |
| **D** — trust boundary on `raw_rule` | ✅ fixed | `f29d00e` |
| lint gate (`autopep8` was advisory, not enforcing) | ✅ fixed & **pushed** | `7b0154a` |

Branch: `pr8-rebased` = `f29d00e` on top of `29f16ab` (the PR, rebased onto `7b0154a`).
Gate at HEAD: **358 passed**, `tox -e lint` clean, `tox -e typecheck` 0 errors.

Working notes for resuming review of [gunchev/usbguard_gui#8][pr]. Written 2026-09-13,
updated the same day after all four findings were fixed.

| | |
|---|---|
| **PR** | [#8 — fix: keep permanent allow rules across KVM and dock topology changes][pr] |
| **Author** | `beorn-` (Aurélien "beorn" Rougemont, a.rougemont@criteo.com) — **external contributor** |
| **Base** | `master` |
| **Original commit** | `e7d1c15` (single commit) — preserved untouched on local branch `pr8-review` |
| **PR rebased** | `29f16ab` — the PR's own commit, rebased onto `7b0154a` |
| **Fixes on top** | `a19aff4` (B) → `1a2feb2` (A) → `437d17b` (C) → `f29d00e` (D) |
| **Branch HEAD** | `pr8-rebased` = `f29d00e` — **pushed to the PR head branch 2026-09-13** |
| **Worktree** | `/tmp/pr8-wt` (branch `pr8-rebased`) |
| **Verdict** | Right idea, right mechanism, HID contract intact. With the four fixes it is mergeable in mechanism; **the one thing still outstanding is field confirmation on `beorn-`'s KVM hardware** (finding A). |

---

## 1. What the PR does

Solves a real problem: `applyDevicePolicy(..., permanent=True)` makes USBGuard **upsert**
the rule it generates for a device, keyed on the **device hash**. Chained identical hubs —
what a KVM switch produces — hash alike, so allowing one silently replaces the other's
rule and every switch cycle prompts again. Author observed rule id 16 removed and re-added
with a different `parent-hash`, back and forth.

The fix stops letting USBGuard regenerate the rule, and appends the **exact reported rule**
instead, so each topology keeps its own coexisting rule.

Mechanism, four parts:

1. **`Device.raw_rule`** (`device.py`) — carries the exact rule string USBGuard reported.
   Declared `field(default="", repr=False, compare=False)`: carried data, not identity.
2. **`_retarget_device_rule(rule, target)`** (`dbus_client.py`) — replaces **only** the
   leading target verb, preserving every attribute verbatim.
3. **`_APPEND_RULE_AT_END = (1 << 32) - 3`** (`dbus_client.py`) — `UINT32_MAX-2`, the
   "append at end" sentinel for `Policy1.appendRule`'s `parent_id`.
4. **The permanent path** (`_DBusThread._do_apply_policy`) — when `permanent and device_rule`:
   authorize the live device **temporarily**, then `appendRule(retargeted, AT_END, temporary=False)`.
   Otherwise falls back to the original upsert.

Call sites updated to pass `device.raw_rule if permanent else None`: `app.py:542`,
`device_list.py:300`. Introspection XML gained the `appendRule` method.

---

## 2. What is GOOD here — keep these

- **The HID lock-first contract is intact.** Traced specifically because `AGENTS.md` flags
  it. The gate lives upstream in `_on_device_presence_changed`
  (`hid_special_treatment = is_hid and enabled and not lock_inhibited and lock_available`).
  This PR changes *how a rule is written*, not *when a device may be allowed*. Permanent
  allow already bypassed HID treatment by design via `_permanent_allow_hashes`; the PR
  inherits that and does **not** widen it.
- **`raw_rule` excluded from equality and `repr`** — the correct call. Two views of the same
  device stay equal.
- **Graceful fallback** — when `raw_rule` is missing (stale device view) it keeps the old
  upsert behaviour rather than silently doing nothing.
- **Introspection XML is correct.** Verified against the **live daemon on this machine**
  (usbguard 1.1.4): `appendRule method sub → u` matches exactly.
- **The tests test the actual invariant**, not just that a call happened — `test_both_topologies_append_distinct_rules`
  and `test_appended_rule_keeps_the_topology` assert `parent-hash` survives, which is the
  whole point. 19 focused tests in `tests/test_permanent_rules.py` as authored; that file now
  carries 49 across six classes.

---

## 3. Findings — all four resolved

Each finding below keeps its original review text, with the resolution appended. One commit
per finding; see the table at the top for SHAs.

### A. Rule ordering / shadowing — ✅ FIXED (`1a2feb2`); field confirmation still wanted

USBGuard evaluates rules **top-down, first match wins**. This PR appends at the **end** of
the list. If any earlier rule matches the device, the new permanent `allow` is **dead code**.
The old upsert let the daemon place the rule.

The author verified the rule "landed after the last existing one" — but *last* is exactly
the position where an earlier broad rule shadows it. Needs checking against a realistic
policy with a catch-all (e.g. `usbguard-generate` output), not a near-empty one.

> **Ask `beorn-`:** does the appended allow actually take effect on your real
> `/etc/usbguard/rules.conf`, or is it shadowed? Should it insert at a position other
> than last?

**✅ FIXED — `1a2feb2`.** The daemon does support positional insert; verified against the
live daemon with temporary, unmatchable rules (removed immediately afterwards):

| `appendRule` `parent_id` | Result |
|---|---|
| `LastID` (`UINT32_MAX-2`) | accepted — appended at the end |
| an existing rule id | accepted — lands immediately after it |
| `0` (`Rule::RootID`) | **`Policy append: rule: Invalid parent ID`** |

The last row is upstream's doing: `RuleSet` honours `0` by inserting at the head, but
`Policy::appendRule` looks the parent up first and no rule has id 0 — so **nothing can be
placed above the first rule**. That is the one case still needing a human.

The permanent path now reads the ruleset and places the new rule **above the first rule that
provably matches the device**. Matching is `rule_matches_device(rule, device)` in `device.py`,
which returns a **three-way verdict**: `True` (matches — move above), `False` (provably
cannot match), `None` (undecidable). The third value is the design: it must never read as
`True`, or a parsing gap becomes a tray decision ranked above administrator-written policy.
It covers the predicate vocabulary USBGuard emits for devices, the six
`usbguard::Rule::SetOperator` operators (default `equals`) and `*` wildcards on interface
specs; `label`, `if` conditions and `equals`-against-wildcards are `None`, not guesses. When
the shadow is the first rule the write still happens and the log says it may be shadowed and
to reorder `rules.conf`.

### B. Duplicate accumulation — ✅ FIXED (`a19aff4`)

The append was **unconditional, with no dedup check**. Master's `permanent=True` *upserted*
one rule per device hash. Now **every** permanent decision added a fresh rule to
`/etc/usbguard/rules.conf`. Re-allow the same topology after a block → duplicate. Over
months: policy bloat and a confusing audit trail.

**Confirmed against the live policy on this machine** (`usbguard list-rules`, read-only, 135
rules) — the accumulation is not hypothetical:

```
20: allow id 05e3:0610 ... hash "6wd5…" with-interface 09:00:00 with-connect-type "unknown"
21: allow id 05e3:0610 ... hash "6wd5…" with-interface 09:00:00 with-connect-type "hotplug"
22: allow id 05e3:0612 ... hash "k74K…" with-interface 09:00:00
23: allow id 05e3:0612 ... hash "k74K…" with-interface 09:00:00     ← byte-identical to 22
```

**The fix** (`dbus_client.py`): the permanent path now reads the ruleset and updates what is
already there instead of piling on.

- `_rule_identity(rule)` — `(id, serial, hash, parent_hash, via_port)`, i.e. *one device in
  one topology*. `parent-hash`/`via-port` stay in the key so KVM siblings are never
  collapsed. Returns `None` for a rule naming no device (`allow with-interface { … }`), so
  class-wide, hand-written policy is never matched or replaced.
- `_persist_device_rule(device_id, rule)` — three outcomes:
  - **identical rule present** → do nothing (whitespace-normalised compare).
  - **same device, different target** → `removeRule(old)` **then** `appendRule(new)`.
    Remove-first is deliberate: append-first would park the new `block` *under* the stale
    `allow`, and USBGuard is first-match-wins — the user's block would be silently ignored
    until the removal landed, and forever if it didn't. Removing first can only ever leave
    the device more restricted, never less.
  - **not present** → append at the end, as before.
- **Unreadable ruleset** → falls back to the plain append (losing the user's decision is
  worse than a possible duplicate); an ordinary failure there still does not flip
  `_connected`.
- **Pre-existing bloat** (several rules sharing one identity) → the first is updated and the
  rest are **logged, not deleted** — any of them could have been hand-written.

Cost: one extra `listRules` round-trip per permanent decision, which is user-driven and rare.
No new D-Bus methods needed — `listRules`/`removeRule` were already in the introspection XML.

**Tests** — `tests/test_permanent_rules.py::TestPermanentRuleDeduplication` (12 tests, all
passing), driven by a `_FakePolicy` that holds a real ruleset rather than bare mocks, so they
assert on the `rules.conf` the user ends up with: re-allow ×2 → one rule; allow→block→allow
cycle → one rule holding the last decision; 5 decisions → one rule; target change replaces;
removal precedes append; the live device is still authorized *temporarily* (HID gate
untouched); KVM sibling topology is **not** a duplicate; unrelated and class-wide rules are
left in place; whitespace-only difference writes nothing; unreadable ruleset still persists
the decision without dropping the connection; pre-existing duplicates update the first and
report the rest.

### C. Non-atomic permanent

Two calls: temporary allow, then permanent append. If the append hits a polkit denial, the
device is left **live-allowed but not persisted** — the user believes they granted
permanence. An error *is* emitted, so there's some signal, but the temporary allow survives
until daemon restart.

> **Ask / fix:** roll back the temporary on append failure, or explicitly report
> "applied temporarily, permanent write failed".

**✅ FIXED — `437d17b`.** The report was the missing piece, and there was a worse fact behind
it: `_DBusThread.error_occurred` is **never connected to `USBGuardClient`**, so a polkit
denial on this path produced only a log line in a tray app nobody tails — the user had no
channel to learn anything.

- New `permanent_write_failed(device_id, action, reason)` on the thread **and** the client,
  forwarded in `connect()`. Distinct from `error_occurred` deliberately: the caller needs to
  know the decision is *temporary*, not merely that something failed.
- `app.py` surfaces it as a tray warning naming the action, the reason, and that it holds only
  until the device is unplugged.
- The temporary authorization is **kept**, not rolled back — reverting to block would fight
  what the user asked for; the honest move is to leave it standing and label it.
- `_restore_permanent_rule()`: the remove-then-append pair is not atomic, so if the append
  fails after the remove landed the policy holds *less* than before the click. Restore
  best-effort; if that fails too, log at error level — the operator needs to know the rule is
  gone.

### D. Trust boundary — *minor, defence-in-depth, not proven exploitable*

`raw_rule` is device-derived and now gets persisted into the permanent policy. Probed
`_retarget_device_rule` with a crafted rule carrying embedded rule tokens:

```
IN : block id 1234:5678 name "hub" block with-interface { 03:01:01 }" serial "x" reject
OUT: allow id 1234:5678 name "hub" block with-interface { 03:01:01 }" serial "x" reject
```

Only the first token is rewritten; **everything after passes through verbatim.** Probably
safe because `raw_rule` originates from the daemon, which escapes quotes on the way out —
but the app now persists a device-influenced string without re-validating that it parses as
one rule matching the intended device. Cheap to add.

**✅ FIXED — `f29d00e`.** `rule_persistence_problem(rule)` in `device.py` returns a reason,
or `None`: control characters (a newline would split the policy file), a missing target verb,
a string that does not tokenise as one rule, or any predicate that is not a device attribute —
which is what catches a second rule or a stray directive riding along.

A refused rule is **not dropped**: the decision falls back to `applyDevicePolicy(permanent=True)`,
the daemon's own upsert, which never takes a device-supplied string. The user still gets
permanence, just not one assembled from a suspect rule, and the refusal is logged with its
reason.

---

## 4. Merge state

The PR was based on merge-base `42bfdd4`, **19 commits behind** master at review time.
`app.py` alone had diverged 114+/47−, and the intervening commits included three security
fixes in this exact path:

- `955632b` correlate unlock-cycle fetches so foreign/out-of-order results can't drop prompts
- `2367c99` bound the HID lock delay and the screensaver unlock queue
- `dc4ea90` recycle replaced D-Bus workers without blocking the Qt UI thread

**First rebase conflicted** in `dbus_client.py` (its own core), `tests/test_app.py`,
`tests/test_dbus_client.py` — all the same mechanical pattern: master added
`fetch_devices`/`_do_fetch_devices` directly above the method whose *signature* the PR
changed. Resolved by keeping both sides. **No semantic clash** with the HID / correlated-fetch
work, which is reassuring.

**Second rebase (onto the CI-fixed `b4d3e7c`) was clean** — no conflicts.

**Third rebase (onto `7b0154a`, the lint-gate fix) was also clean** — that commit touched only
`Makefile`, `tox.ini` and `AGENTS.md`, nothing in the PR's path. The four fix commits sit on
top of it without conflict.

---

## 5. Verification already done (don't redo)

| Check | Where | Result |
|---|---|---|
| PR at its own base | `pr8-review` | 245 passed |
| PR rebased, before fixes | `29f16ab` | 281 passed |
| **PR + all four fixes** | `pr8-rebased` @ `f29d00e` | **358 passed** |
| master alone | `py313` | 262 passed |
| `tox -e lint` at branch HEAD | local | ✅ isort + ruff + autopep8 clean — **and the gate now fails on a diff** (`--exit-code`, `7b0154a`) |
| `tox -e typecheck` at branch HEAD | local | ✅ pyright 0 errors |
| Live daemon API shape | this machine, usbguard 1.1.4 | ✅ `appendRule sub → u` matches XML |
| **Live `appendRule` placement** | this machine, temporary unmatchable rules | ✅ `LastID` and an existing id accepted; `parent_id=0` → `Invalid parent ID` |
| **Live `rules.conf` for duplication** | `usbguard list-rules`, read-only, 135 rules | ⚠️ pre-existing duplicates confirmed (20/21 same identity; 22/23 byte-identical) |
| HID lock-first contract | traced in `app.py` | ✅ intact, not widened |

The live-daemon probes used a `block id dead:beef` rule with `temporary=True` — never written
to `rules.conf` — and each returned rule was removed immediately. Nothing was left behind and
no real device was affected.

**Note on running tests in the worktree** — `uv run` stalls on downloads there. Use the main
venv instead:

```bash
cd /tmp/pr8-wt && QT_QPA_PLATFORM=offscreen PYTHONPATH=/tmp/pr8-wt/src \
  /home/dgunchev/github/gunchev/usbguard_gui/.venv/bin/python -m pytest tests/ -q
```

---

## 6. Side quest completed (pushed, unrelated to #8's findings)

While reviewing, a gap surfaced: `AGENTS.md` claimed "`make check` is the gate" but CI ran
plain `tox`, and `tox.ini` defined **only** the `py310`–`py314` test envs — so lint and
typecheck **never ran in CI**. That's why master carried a live autopep8 violation and still
showed green.

Fixed and pushed as `b4d3e7c` (`2893815..b4d3e7c`):

- `tox.ini` — added `lint` and `typecheck` envs, **leading the envlist** so plain `tox`
  covers the whole gate. Separate envs, not extra `[testenv]` commands, so the linters run
  once instead of once per interpreter.
- `.github/workflows/fedora.yml` — added `nodejs` to the dnf line (pyright is a Node tool
  shipped through pip; avoids a runtime download).
- `tests/test_app.py` — fixed the pre-existing autopep8 continuation-alignment violation.
- `AGENTS.md` — recorded that CI now enforces the gate.

**Proven in CI** — run [`34734685447`][ci]: green on Fedora 43 **and** 44, all 7 envs
(`lint`, `typecheck`, `py310`–`py314`), with `ruff` "All checks passed!" and pyright
"0 errors" visible in the logs. `nodejs-22.22.2-2.fc43` came from Fedora `updates`, so
pyright used the system Node.

**The gate costs ~8 seconds** (lint 1.2s + typecheck 6.6s). Total run 4m18s vs 4m40s
before — *faster* than the pytest-only baseline. No excuse to skip it now.

### 6b. The gate was still soft — fixed and pushed (`7b0154a`)

While working the fixes it surfaced that `b4d3e7c` had put lint in CI but left one step
advisory: `autopep8 --diff` **prints the patch and still exits 0**, so `make lint` and the
tox `lint` env *reported* formatting drift while passing. With the flag autopep8 exits 2 when
a diff exists, so the step actually rejects unformatted code the way ruff and isort already
did. Applied to both `Makefile` and `tox.ini`, documented in `AGENTS.md`.

Proven both ways: `make check` green on a clean tree, and a probe file with a misindented
continuation now fails the env with exit 2 — the exact class of violation that used to slip
through silently (it is literally what `b4d3e7c` had to fix).

---

## 7. Next steps

All four findings are fixed and committed on `pr8-rebased` (`f29d00e`), each as its own
commit with its own tests. What is left is coordination, not code:

1. ✅ **Handed to `beorn-`** (2026-09-13) — [`#issuecomment-5652347501`][review-comment] frames A as a
   question, not a verdict, and asks for the three field checks. The full diff is regenerable rather
   than committed: `git diff master..pr8-rebased > PR-8-fixes.patch`. Note that it contains the PR's
   **own** changes too — the fixes-only diff is `git diff 29f16ab..pr8-rebased` (8 files, +1078/−6),
   so applying the full patch after merging the PR would re-apply the PR on top of itself.
2. ✅ **PR branch updated** (2026-09-13) — `pr8-rebased` was pushed to
   `beorn-:fix/permanent-rules-across-kvm` with `--force-with-lease` pinned to `e7d1c15`
   (maintainer edits were enabled on the PR). PR went `CONFLICTING` → `MERGEABLE` / `CLEAN`.
3. ✅ **Approval gate cleared** (2026-09-13) — the run for the new head, [`34748783679`][ci-pr], was
   held `action_required` (first-time contributor); approved via the REST API and it went green on
   Fedora 43 **and** 44 with the enforcing gate (`autopep8 --exit-code`, pyright 0 errors,
   `py310`–`py314` all executed). The earlier `34648577186` predates the gate change.
4. **One case is knowingly unfixable client-side:** a shadow sitting on the very first rule.
   `Policy::appendRule` rejects `parent_id = 0`, so the app writes the rule anyway and logs
   that it may be shadowed. Worth a line in the README if users hit it.

### Resume commands

```bash
cd /home/dgunchev/github/gunchev/usbguard_gui

git log --oneline -1 master          # expect 7b0154a (pushed)
git log --oneline -1 pr8-rebased    # expect f29d00e (also the PR head on the author's fork)
git log --oneline -1 pr8-review     # expect e7d1c15 (original, untouched)

# PR state — head should be f29d00e, mergeable MERGEABLE / mergeStateStatus CLEAN
gh pr view 8 --repo gunchev/usbguard_gui --json state,mergeable,mergeStateStatus,headRefOid

# The four fixes, oldest first
git log --oneline master..pr8-rebased

# The PR's own new tests (pre-fix, for comparison)
git show pr8-review:tests/test_permanent_rules.py

# The core logic under review now
git show pr8-rebased:src/usbguard_gui/dbus_client.py | sed -n '290,470p'
git show pr8-rebased:src/usbguard_gui/device.py | sed -n '190,420p'   # matcher + validator

# Re-run the gate in the worktree
cd /tmp/pr8-wt && QT_QPA_PLATFORM=offscreen PYTHONPATH=/tmp/pr8-wt/src \
  /home/dgunchev/github/gunchev/usbguard_gui/.venv/bin/python -m pytest tests/ -q
tox -e lint -e typecheck

# Cleanup when finished
cd /home/dgunchev/github/gunchev/usbguard_gui
git worktree remove /tmp/pr8-wt && git branch -D pr8-rebased
```

> ⚠️ `/tmp/pr8-wt` is a temp dir — **it will not survive a reboot.** If this handoff sits
> for a while, recreate it:
> `git worktree add /tmp/pr8-wt pr8-rebased`

---

## 8. Files touched

`master..pr8-rebased` — the PR's own changes plus the four fixes (11 files, +1385/−16).
Full diff: `git diff master..pr8-rebased` (regenerate; no stale copy is committed).

```
src/usbguard_gui/app.py                                 |  19 +-  # raw_rule call site + permanent_write_failed tray warning
src/usbguard_gui/dbus_client.py                         | 254 ++   # dedup, placement, restore, validation call, signals
src/usbguard_gui/device.py                              | 237 ++   # raw_rule field + rule matcher + persistence validator
src/usbguard_gui/device_list.py                         |   3 +-  # raw_rule call site
src/usbguard_gui/introspection/org.usbguard.Policy1.xml |   6 +   # appendRule method
tests/test_app.py                                       |   4 +-  # fake client gained the new signal
tests/test_dbus_client.py                               |   4 +-  # fake thread/client stand-ins gained the signal
tests/test_device.py                                    | 140 ++   # matcher + validator: 47 new cases
tests/test_device_dialog.py                             |   3 +-
tests/test_device_list.py                               |   6 +-
tests/test_permanent_rules.py                           | 725 ++   # NEW — 49 tests across 6 classes
```

Test classes in `tests/test_permanent_rules.py`:

| Class | Covers |
|---|---|
| `TestRetargetDeviceRule` | the PR's verb-only rewrite (original) |
| `TestPermanentAllowAppendsRule` | the PR's append path (original) |
| `TestPermanentRuleDeduplication` | **B** — 12 tests, driven by a `_FakePolicy` holding a real ruleset |
| `TestPermanentRulePlacement` | **A** — 7 tests, incl. the unplaceable-first-rule case |
| `TestPermanentWriteFailure` | **C** — 6 tests, report + restore |
| `TestUntrustedRuleIsNotPersisted` | **D** — 5 tests, refuse + fall back to upsert |
| `TestDeviceRawRule` / `TestCallSitesPassRawRule` | the model and its call sites (original) |

[pr]: https://github.com/gunchev/usbguard_gui/pull/8
[ci]: https://github.com/gunchev/usbguard_gui/actions/runs/34734685447
[ci-pr]: https://github.com/gunchev/usbguard_gui/actions/runs/34748783679
[review-comment]: https://github.com/gunchev/usbguard_gui/pull/8#issuecomment-5652347501
