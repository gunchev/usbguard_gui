# AGENTS.md

Instructions for agentic coding agents working in this repository.

## Project Overview

KDE/Qt system tray GUI for USBGuard — responds to USB device insertions with Allow, Block, or Reject actions.

- **Language**: Python >= 3.10
- **UI Framework**: PyQt6
- **IPC**: D-Bus (via dbus-fast)
- **Layout**: src-layout (sources in `src/usbguard_gui/`)

## Build & Run Commands

### Using uv (recommended)

```bash
uv run usbguard_gui                                 # run the app
uv run python -m usbguard_gui                       # alternative entry point
uv run pytest                                       # run all tests
uv run pytest tests/test_file.py                    # run single test file
uv run pytest -k test_name                          # run single test by name
uv run pytest -v --cov . --cov-report=term-missing  # coverage
uv run ruff check src/ tests/                       # lint
uv run autopep8 --in-place --recursive src/ tests/  # format
uv run tox                                          # test across Python versions
```

### Using Make

```bash
make test          # run all tests (uv run pytest -v)
make check         # lint + test
make lint          # ruff check + autopep8 format check
make format        # auto-format with autopep8
make coverage      # test with coverage report
make build         # build wheel package
make clean         # clean build artifacts
make run           # sync dev deps and run app
```

## Code Style Guidelines

### Formatting

- **Line length**: 120 characters maximum
- **Indentation**: 4 spaces for Python files
- **Line endings**: LF
- **Formatter**: autopep8
- **Charset**: UTF-8
- **Trailing whitespace**: trimmed
- **Final newline**: required
- See `.editorconfig` for additional editor-specific settings

### Linter/Formatter Configuration (pyproject.toml)

```toml
[tool.ruff]
line-length = 120
target-version = "py310"

[tool.ruff.lint]
select = ["E", "F", "W", "UP", "B", "SIM", "RUF"]

[tool.autopep8]
max_line_length = 120
```

### Imports

- Always use `from __future__ import annotations` for postponed annotations
- Group imports in order: stdlib, third-party, local
- Use absolute imports: `from usbguard_gui.device import ...`
- Sort imports with ruff (I001)

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
- D-Bus errors: catch `DBusError` from `dbus_fast.error`
- Permission errors: check error names via `_is_permission_error()` pattern
- Never expose secrets or credentials in logs

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

## Project Structure

```
src/usbguard_gui/
    __init__.py       # Package init (can export VERSION)
    __main__.py       # python -m entry point
    app.py            # Main tray application
    dbus_client.py    # D-Bus client for USBGuard daemon
    device.py         # Device model and rule parsing
    device_dialog.py  # Device action dialog window
    device_list.py    # Device list window
    screensaver.py    # Screensaver state monitoring
    settings.py       # Application settings

tests/
    test_app.py          # Tests for main tray application
    test_device.py       # Tests for device model
    test_device_list.py  # Tests for device list window
    test_dbus_client.py  # Tests for D-Bus client
    test_async_api.py    # Tests for async signal-based API
    test_release.py      # Tests for release scripts
```

## Testing Conventions

- Use `pytest` with `pytest-mock` and `pytest-cov`
- Test files: `tests/test_<module>.py`
- Test classes: `TestClassName` with descriptive methods
- Test methods: `test_<what_is_tested>`
- Use `pytest.mark.parametrize` for multiple test cases
- Private helper methods in tests prefixed with `_`
- Use sentinel pattern (`object()`) for special default values

### Example Test Structure

```python
"""Tests for the device model and rule parser."""

from usbguard_gui.device import Device, DeviceTarget, parse_device_rule


class TestParseDeviceRule:
  """Test rule string parsing."""

  RULE_ALLOW = 'allow id 1d6b:0002 serial "..." name "..." ...'

  def test_allow_rule(self):
    result = parse_device_rule(self.RULE_ALLOW)
    assert result["rule"] == "allow"
```

## Pre-commit Checklist

Before submitting changes:

1. Run `make check` (lint + tests)
2. Ensure all new public APIs have type hints
3. Add tests for new functionality
4. Update docstrings for user-facing APIs
5. No commented-out code in final submissions
