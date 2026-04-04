# usbguard_gui

KDE/Qt system tray GUI for [USBGuard](https://usbguard.github.io/).

Monitors USB device insertions and lets you Allow, Block, or Reject devices
through desktop notifications and a device management window.

## Requirements

- Python 3.10+
- USBGuard daemon running with D-Bus interface enabled
- A desktop environment with system tray support (KDE Plasma, GNOME, etc.)

## Installation

```bash
uv tool install .
```

## Usage

```bash
usbguard_gui
```

The application runs as a system tray icon. When a new USB device is blocked
by USBGuard, a popup dialog appears with options to:

- **Allow (Permanent)** — allow and add a persistent rule
- **Allow (Temporary)** — allow until the device is removed
- **Block** — keep the device blocked
- **Reject** — reject the device (electrically disconnect)

Click the tray icon to open the device list showing all connected USB devices.

## Development

```bash
uv run pytest          # run tests
uv run ruff check src/ tests/  # lint
uv run ruff format src/ tests/ # format
```

## Credits

- Inspired by [usbguard-gnome](https://github.com/6E006B/usbguard-gnome) — a GNOME tray applet for USBGuard that pioneered several UX ideas adopted here (HID lock-screen behaviour, screensaver awareness, device dialog flow).
- Implemented with [Claude Sonnet](https://claude.ai/claude-code) (Anthropic).

## License

GPL-2.0-or-later
