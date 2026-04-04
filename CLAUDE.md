# usbguard-gui

KDE/Qt system tray GUI for USBGuard.

## Build & Run

```bash
uv run usbguard-gui          # run the app
uv run python -m usbguard_gui  # alternative
```

## Test

```bash
uv run pytest                 # run tests
uv run tox                    # run tests across Python versions
```

## Lint

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

## Style

- src-layout: sources in `src/usbguard_gui/`
- Line length: 120
- Formatter/linter: ruff
- Type hints on all public APIs
- Python >=3.10
