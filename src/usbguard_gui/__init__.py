"""usbguard_gui — KDE/Qt system tray GUI for USBGuard."""

__version__ = "0.0.9"
__author__ = "Doncho Nikolaev Gunchev"
__license__ = "GPL-2.0-or-later"

from usbguard_gui.device import Device, DeviceTarget, PresenceEvent, parse_device_rule

__all__ = ["Device", "DeviceTarget", "PresenceEvent", "parse_device_rule"]
