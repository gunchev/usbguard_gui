# usbguard_gui

A vibe coded KDE/Qt system tray GUI for [USBGuard](https://usbguard.github.io/).

Monitors USB device insertions and lets you Allow, Block, or Reject devices
through desktop notifications and a device management window.

## Requirements

- Python 3.10+
- USBGuard daemon running with D-Bus interface enabled
- A desktop environment with system tray support (KDE Plasma, GNOME, etc.)

## Installation

### Fedora

On Fedora installing the RPM package will start the app on session start in KDE.

```bash
# USB Guard itself, install and enable
sudo dnf -y install usbguard usbguard-dbus
systemctl enable --now usbguard.service usbguard-dbus.service

# Development tools
sudo dnf -y install usbguard usbguard-dbus rpmdevtools make uv
rpmdev-setuptree

# Build and install the RPM package
make rpm
sudo dnf install -y ~/rpmbuild/RPMS/noarch/usbguard_gui-*.noarch.rpm
```
At this point either run `usbguard_gui` or logout and login to get it.

### Generic

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

## How It Works

### Startup
On launch, the application connects to the USBGuard daemon via D-Bus and monitors the desktop screensaver state via session D-Bus. The app runs as a system tray icon with tooltip showing connection status.

### Device Detection Flow
1. USBGuard daemon blocks an unknown USB device
2. App receives a `device_presence_changed` signal from D-Bus
3. A popup dialog appears with options:
   - **Allow (Permanent)** — allow device and create persistent rule
   - **Allow (Temporary)** — allow device until it's disconnected
   - **Block** — keep device blocked
   - **Reject** — electrically disconnect the device

### HID Devices
Human Interface Devices (keyboards, mice, etc.) are handled specially:
- When the screen is locked and an HID device connects, it's automatically allowed temporarily
- When the screen unlocks, the device is allowed and the screen locks again automatically

### Screensaver Integration
- When the screen locks: the app tracks all device insertions that occur while locked
- When the screen unlocks: displays a notification listing all devices that connected during absence
- Opens an action dialog for each pending device so you can decide what to do

### Device List Window
- Shows all currently connected USB devices with their status
- Displays device name, ID, hash, and class
- Live updates via D-Bus signals (refreshes on device events)
- Supports applying policy actions directly
- Indicates which devices have permanent allow rules

## Architecture

- **UI Framework**: PyQt6
- **D-Bus Integration**: dbus-fast with QThread-based asyncio event loop
- **Communication Pattern**: Asynchronous operations return results via Qt signals
- **Key Components**:
  - `app.py` — Main tray application and signal handlers
  - `dbus_client.py` — USBGuard daemon communication
  - `screensaver.py` — Screensaver state monitoring
  - `device_list.py` — Device management window
  - `device_dialog.py` — Device action dialog

## Development

```bash
uv run pytest          # run tests
uv run ruff check src/ tests/  # lint
uv run ruff format src/ tests/ # format
```

## Credits

- Inspired by [usbguard-gnome](https://github.com/6E006B/usbguard-gnome) — a GNOME tray applet for USBGuard that pioneered several UX ideas adopted here (HID lock-screen behaviour, screensaver awareness, device dialog flow).
- Planned with [Claude Opus](https://claude.ai/claude-code) (Anthropic).
- Implemented with [Claude Sonnet](https://claude.ai/claude-code) (Anthropic).
- Infrastructure improvements by [big-pickle/OpenCode](https://opencode.ai).

## License

GPL-2.0-or-later
