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
- adc75c8 Initial implementation of usbguard-gui

