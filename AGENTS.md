# AGENTS.md

Instructions for agentic coding agents working in this repository.

## Read this first

1. **Preserve the HID lock-first contract.** A device exposing a HID interface may
   only be allowed while the screen is locked — that is the invariant this whole
   app exists to enforce, and the flow is specified in `README.md` → *How It
   Works → HID Devices*. Touch `app.py` without it in hand and you risk re-
   opening a security bug, not just a functional one.
2. **`make check` is the gate.** Lint + typecheck + tests pass, or the change is
   not done.
3. **Graft before grep** — but build it first. The `graft/` context graph (see
   the Graft section) answers "where does X live" and "what breaks if I change
   X" exactly, for free. It is **generated and local-only**: `graft/` is
   gitignored, so a fresh clone has none. Check with `ls graft/`; if it is
   missing (or `graft check` reports stale), run `graft build --deep` before
   relying on it. Never assume a hit is current just because the section below
   describes the tool.

## Project Overview

KDE/Qt system tray GUI for USBGuard — responds to USB device insertions with Allow, Block, or Reject actions.

- **Language**: Python >= 3.10
- **UI Framework**: PyQt6
- **IPC**: D-Bus (via dbus-fast)
- **Layout**: src-layout (sources in `src/usbguard_gui/`)

## Build & Run Commands

`make` carries the canonical verbs; `make help` describes every target (RPM/mock,
PyPI upload, clean/distclean).

```bash
make check         # lint + typecheck + tests — the gate
make lint          # isort --check-only + ruff + autopep8 --diff
make typecheck     # pyright
make test          # pytest -v
make format        # isort + autopep8 --in-place + ruff --fix
make coverage      # tests + term-missing coverage report
make run           # uv sync dev deps, then run the app
make build         # wheel + sdist into dist/
```

Ad-hoc work goes through uv directly:

```bash
uv run pytest tests/test_app.py      # one test file
uv run pytest -k test_name           # one test by name
uv run tox                          # every supported Python (CI runs this on Fedora 43/44)
```

## Code Style Guidelines

### Formatting

`.editorconfig` and `pyproject.toml` are the source of truth: 4-space indent,
LF, UTF-8, 120-column limit, trailing whitespace trimmed, final newline.
`make lint` checks all of it; `make format` applies it.

### Function Signature Wrapping

Prefer dense signatures: pack as many parameters as fit on the `def` line
(max 120 cols) rather than one parameter per line. Wrap the remainder on
continuation lines, visually aligned under the first parameter, keeping
the closing paren and return annotation on the last parameter line:

```python
def _on_device_policy_changed(self, device_id: int, target_old: int, target_new: int, device_rule: str,
                              rule_id: int, attributes: dict[str, str]) -> None:
```

autopep8 enforces the visual alignment (`make lint` flags it, `make
format` fixes it); the density preference is enforced by review.

### Linters in play

`ruff` (rule set lives in `[tool.ruff.lint]`, `pyproject.toml`), `isort`
(import order), `autopep8` (whitespace and continuation layout), `pyright`
(types). `make lint` runs the first three, `make typecheck` runs pyright.

### Imports

- Always use `from __future__ import annotations` for postponed annotations
- Group in order: stdlib, third-party, local; absolute imports only
  (`from usbguard_gui.device import ...`)
- **isort owns import order** — `make lint` runs `isort --check-only`, `make
  format` runs `isort`. Ruff's `I` rules are deliberately not enabled, so
  `ruff check --fix` will not sort imports for you.

### Type Hints

- Required on all public APIs (functions, classes, methods)
- Use Python 3.10+ syntax (`X | None` over `Optional[X]`)
- Return type annotations: always present on public methods

### Naming Conventions

- **Classes**: `PascalCase` (e.g., `USBGuardClient`, `DeviceActionDialog`)
- **Functions/methods**: `snake_case` (e.g., `apply_device_policy`, `_handle_disconnect`)
- **Constants**: `SCREAMING_SNAKE_CASE` (e.g., `USBGUARD_BUS_NAME`)
- **Private members**: leading underscore (e.g., `self._client`, `self._connected`)

### Docstrings

- Module-level: `"""Module description."""`
- Classes: `"""Short description.\n\nExtended explanation if needed.\n"""`
- Methods: `"""One-line description."""` or multi-line for complex methods

### Error Handling

- Use logging (`log = logging.getLogger(__name__)`) for errors and warnings
- D-Bus errors: `from dbus_fast import DBusError` (there is no `dbus_fast.error`
  module) alongside `from dbus_fast.aio import MessageBus`
- Two D-Bus error classes, and the split matters: `_is_permission_error()` is a
  polkit denial, `_is_connection_error()` is a broken transport. Only the second
  may flip `_connected` and trigger a reconnect — an ordinary per-call failure
  (device unplugged a moment before the action) must leave the connection alone.
- Keep credentials out of log output.

### Dataclasses & Enums

- Use `@dataclass` for data models with `field(default_factory=...)` for mutable defaults
- Use `IntEnum` for integer-backed enums (e.g., `DeviceTarget`, `PresenceEvent`)
- Prefer `.name` over string comparison when available

### Qt/PyQt6 Patterns

- Subclass `QObject` for classes that emit signals
- Use `pyqtSignal` for typed signals
- Parent parameter in `__init__(self, parent: QObject | None = None)`
- Connect signals in `_connect_signals()` method
- Use `QTimer.singleShot()` for deferred actions

### Settings & Test Isolation

- App settings go through `SettingsProtocol` (`settings.py`), injected as
  `USBGuardTrayApp(..., settings=...)`; `DeviceListWindow(..., settings=...)`
  takes its `QSettings` geometry store the same way. Production injects nothing
  and gets the real `Settings` singleton.
- Adding a setting means extending **both** `SettingsProtocol` and `_FakeSettings`
  (`tests/test_app.py`): the protocol is the contract, the fake is what the suite
  runs against. The device list gets a `tmp_path`-backed `QSettings` for the same
  reason — every test brings its own store, so a preference toggled in the running
  app can never change what the suite asserts.

## Project Structure

```
src/usbguard_gui/
    __init__.py       # Package init (exports __version__)
    __main__.py       # python -m entry point
    app.py            # Main tray application (HID lock-first flow lives here)
    dbus_client.py    # D-Bus client for the USBGuard daemon
    dbus_common.py    # Shared worker-thread base: AsyncWorkerThread, get_introspection
    device.py         # Device model and rule parsing
    device_dialog.py  # Device action dialog window
    device_list.py    # Device list window
    screensaver.py    # Screensaver / lock-state monitoring
    settings.py       # SettingsProtocol + the QSettings-backed Settings singleton
    introspection/    # Bundled D-Bus introspection XML (must ship in the wheel)

tests/
    test_app.py             # Main tray application
    test_device.py          # Device model
    test_device_dialog.py   # Action dialog
    test_device_list.py     # Device list window
    test_dbus_client.py     # D-Bus client
    test_async_api.py       # Async signal-based API
    test_settings.py        # SettingsProtocol conformance
    test_release.py         # Release scripts
```

## Where things are documented

| Location | What it owns |
|---|---|
| `README.md` → *How It Works* | user-facing behaviour, **the HID lock-first security contract**, polkit, install |
| `docs/DESIGN.md` | QThread + asyncio architecture, signal contracts, introspection XML, dasbus→dbus-fast mapping |
| `docs/AUDIT-*.md`, `docs/REVIEW-*.md` | past audit/review findings and how each was resolved |
| `TODO.md` | versioned roadmap, including deferred race fixes for 1.0 |
| `graft/` | generated repo context graph — **local-only, gitignored**; build with `graft build --deep` (see the Graft section) |

## Testing Conventions

- Use `pytest` with `pytest-mock` and `pytest-cov`
- Test files: `tests/test_<module>.py`
- Test classes: `TestClassName` with descriptive methods
- Test methods: `test_<what_is_tested>`
- Use `pytest.mark.parametrize` for multiple test cases
- Private helper methods in tests prefixed with `_`
- Use sentinel pattern (`object()`) for special default values
- Headless: each test module sets `QT_QPA_PLATFORM=offscreen` at import, and CI
  sets it too, so the suite needs no display server

### Example Test Structure

```python
"""Tests for the device model and rule parser."""

from __future__ import annotations

from usbguard_gui.device import parse_device_rule


class TestParseDeviceRule:
    """Test rule string parsing."""

    RULE_ALLOW = 'allow id 1d6b:0002 serial "0000:00:14.0" name "xHCI Host Controller"'

    def test_allow_rule(self) -> None:
        result = parse_device_rule(self.RULE_ALLOW)

        assert result["rule"] == "allow"
```

## Pre-commit Checklist

Before submitting changes:

1. Run `make check` (lint + typecheck + tests)
2. Ensure all new public APIs have type hints
3. Add tests for new functionality
4. Update docstrings for user-facing APIs
5. Update `README.md` when user-facing behaviour changes — the HID flow, the
   lock delay, polkit rules and install steps all live there
6. Run `graft build` after structural changes so the local context graph matches
   the code (the graph is not committed — this refreshes it for the next agent on
   this machine)
7. No commented-out code in final submissions

## Releasing

`make V=X.Y.Z release` runs `release.py`: bumps `__init__.py`, rebuilds the
CHANGELOG section from the git log, commits, and tags `vX.Y.Z`. The tag triggers
the GitHub → COPR webhook (verified on v0.7.4, build 10928231). Release through
that target rather than hand-editing `__version__` or the CHANGELOG.

<!-- graft:start -->

> **The graph itself is not committed.** `graft/` and `/.graft/` are gitignored,
> so the nodes described below exist only on the machine that built them. On a
> fresh clone there is no graph: run `graft build --deep` before using any graft
> command, and `graft check` to see whether an existing local graph has drifted
> from the code. If graft is unavailable, fall back to reading the source — do
> not treat a missing or stale graph as an answer.

## Graft — repo context graph

This repo is indexed in `graft/`: small linked markdown nodes that explain each
system and carry exact file:line spans, kept in sync with the code by rebuilding
with `graft build` (generated locally, never committed).

For ANY task here — understanding how something works, finding where code lives,
or scoping a change — get context from the graph before grepping or opening
source files. Re-ask freely (it's cheap) and reuse literal identifiers you
already have (symbol, error string, file name) as the query. New to this repo?
Run `graft map` first — a token-budgeted orientation (dir clusters, hubs,
hotspots), no LLM, no key.

- Run `graft ask "<your question>" --source` → ranked nodes with the relevant
  code spans inlined (each hit's ≤8-line crux by default; `--full` for whole
  definitions when the crux isn't enough). Match the tool to the task shape:
  for understanding or editing, the top node IS the answer — cite its
  `covers:` file:line spans and edit straight from `--source`. For
  exhaustive tasks ("every occurrence / every caller of this pattern"), ranked
  results are top-N, not complete — run `graft grep "<literal>"` instead
  (exhaustive over indexed files, grouped by enclosing symbol), falling back
  to raw `grep -rn` only for unindexed files.
- `graft skeleton <file>` → every definition's signature + span, ~10× cheaper
  than reading the file; use it to skim an API surface.
- `graft callers <symbol>` gives precomputed, exact edges — who calls this.
  Add `--direction out` for what it calls, or `--depth N` to walk
  transitively for the full blast radius. For structural questions, skip
  ranking and use this directly.
- Or browse: `graft/INDEX.md` lists every node; follow the links.
- Monorepos and folders of multiple repos rank fairly across sub-projects —
  hits carry `[scope/]` labels naming which one they're from. Narrow with
  `graft ask "<task>" --in <scope>/` once you know where you're working.

If a returned span is truncated ("+N more lines"), open the file at that exact
range before finalizing. Only open source files when a node genuinely lacks a
needed detail, and then at the exact file:line the node points to — never
re-read whole files.

After big code changes, refresh the graph with `graft build` (deterministic,
no API key, $0).
<!-- graft:end -->
