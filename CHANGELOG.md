## 0.1.0 — 2026-04-10

### Changes since v0.0.13

- e00caa3 feat: Toggle device list visibility on tray icon click
- 91f6cd9 OpenCode is no Anthropic ;-)
- ebaf3ac CI: add libglvnd-egl for libEGL.so.1 required by PyQt6
- 0c2d19c Return some needed deps.
- 4fad4fa Fix tox: add pytest-qt, tox-uv; run all Pythons in CI via Fedora packages
- 567e4da fix: Fix ImportError: libGL.so.1, CI

## 0.0.13 — 2026-04-10

### Changes since v0.0.12

- dff22be Fix clean exit and persist device list window state

## 0.0.12 — 2026-04-10

### Changes since v0.0.11

- bc80337 Enforce single application instance using QLockFile
- 34bedf1 Improve devices dialog: sortable, resizable, and reorderable columns
- 2cfd0f5 Add documentation on how the application works and its architecture
- cda1066 Clean up test_device_list.py: remove unused variables and noqa comments

## 0.0.11 — 2026-04-05

### Changes since v0.0.10

- 0d5f69e Fix device list never populated due to two independent bugs
- 2d15e00 Fix device list refresh by using refresh ID for signal correlation
- 7f02094 Fix device list not updating due to missing pending_devices storage
- b835a3a Add tests for async signal-based API
- 520b74b Fix app.py and device_list.py for async API
- dbf08f2 Add DESIGN.md documenting the PyGObject replacement approaches
- 3f83c62 Replace PyGObject dependency with dbus-fast (QThread+asyncio)
- db4f6db Update GitHub Actions workflow to use Podman

## 0.0.10 — 2026-04-05

### Changes since v0.0.9

- 3a94720 fix: release.py
- 5b72884 Configure ruff to prefer compact formatting: (by Qwen3.6 Plus Free)
- 059df0f Remove empty file.
- 7af2f9c Fix medium-priority issues: (by Qwen3.6 Plus Free)
- 025d8a1 Fix high-priority issues: (by Qwen3.6 Plus Free)
- 2d8f605 Fix critical bugs: by Qwen3.6 Plus Free - release.py build_impl crash - HID stale device reference - dialog WA_DeleteOnClose access
- ef68b4c Fix TODO.md: add trailing newline
- 3d586a3 Add TODO.md with circuit breaker pattern suggestion for future improvements
- 805d07d Improve error handling: Add logging, exponential backoff, and enhanced error context
- f4e668d Fix bug in device.py, add type hints, add __all__ exports, add pyqt6-stubs
- 4cd31a5 expand dbus_client tests to 100% coverage (by big-pickle/OpenCode)
- 3ce112f refactor release.py for testability, add comprehensive tests (by big-pickle/OpenCode)
- 81a1213 make CLAUDE.md a redirect to AGENTS.md

## 0.0.9 — 2026-04-05

### Changes since v0.0.8

- f604cac dynamic versioning from __init__.py (by big-pickle/OpenCode)

## 0.0.8 — 2026-04-05

### Changes since v0.0.7

- 6ac501c release.py: remove automatic dev version bump after release (by big-pickle/OpenCode)
- ad32718 release.py: fix version mismatch after release, format fixes, add AGENTS.md
- 797fb54 README: add credits section
- 69be8be Start 0.0.8-dev

## 0.0.7 — 2026-04-04

### Changes since v0.0.6

- 1e4fc6d rpm: install XDG autostart entry for session auto-start
- e983a43 Start 0.0.7-dev

## 0.0.6 — 2026-04-04

### Changes since v0.0.5

- 0be0a9c app: use usbguard_gui theme icon with fallback to source-tree SVG
- e41aa82 device_list: remove permanent rule when demoting to temporary allow
- 85808c5 device_list: refresh immediately after context-menu action
- b347e57 device_list: blue background for temporarily allowed devices
- ebd9daa app: fix spurious device dialogs for allowed/non-insert events
- dbb035c app: add detailed debug logging for device presence events
- e638935 Makefile: use python -m usbguard_gui in run target
- df30bd7 app: fix spurious device dialogs for allowed/non-insert events
- 713e99b Start 0.0.6-dev

## 0.0.5 — 2026-04-04

### Changes since v0.0.4

- 96bdf78 app: use usbguard_gui theme icon with fallback
- 8c2b9d7 Start 0.0.5-dev

## 0.0.4 — 2026-04-04

### Changes since v0.0.3

- 28396f0 Add whl_local/.gitignore
- 49fbc4d rpm: fix %preun, drop %postun and unused systemd macros
- 7b50b6f rpm: force-enable usbguard-dbus.service on install
- f40e501 Start 0.0.4-dev

## 0.0.3 — 2026-04-04

### Changes since v0.0.2

- 49c55d1 device_list: use dark row colors with white text
- ff03739 rpm: require usbguard-dbus, enable its service on install
- 552a629 Fix duplicate signals, auth errors, add polkit rule
- 58e907d Start 0.0.3-dev

## 0.0.2 — 2026-04-04

### Changes since v0.0.1

- b2408ca Add RPM packaging
- 430cd16 Start 0.0.2-dev

## 0.0.1 — 2026-04-04

### Changes since beginning

- a071a82 Rename to usbguard_gui, python things...
- 76f5233 Add release.py.
- 83838a4 Add Makefile
- adc75c8 Initial implementation of usbguard_gui
