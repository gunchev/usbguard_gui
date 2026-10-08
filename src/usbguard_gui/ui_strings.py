"""User-facing strings shared by the tray, the dialog and the device list.

The refusal warnings were duplicated byte-for-byte between the two views; the
tray titles lived inline in ``app.py``.  One definition each here, with the
tests asserting on the literal text so a wording change stays a conscious,
visible one.
"""

from __future__ import annotations

# Title for QMessageBox boxes raised around the app.
MESSAGE_BOX_TITLE = "USBGuard GUI"

# Refusal bodies — identical wherever an action is turned away.
DAEMON_NOT_CONNECTED_WARNING = (
    "The USBGuard daemon is not connected.\nThe action was not applied — "
    "try again once the connection is restored."
)
LOCK_UNAVAILABLE_WARNING = (
    "Screen locking is unavailable — device actions are disabled.\n"
    "Devices remain blocked by USBGuard's policy."
)

# Tray notification titles (the first argument of showMessage).
LOCK_UNAVAILABLE_NOTICE_TITLE = "Screen locking unavailable"
LOCK_AVAILABLE_NOTICE_TITLE = "Screen locking available"
BROADER_RULE_NOTICE_TITLE = "Temporary decision incomplete"
TEMP_DECISION_PARTLY_CHANGED_NOTICE_TITLE = "Temporary decision not applied — policy partly changed"
TEMP_DECISION_NOT_APPLIED_NOTICE_TITLE = "Temporary decision not applied"
PERMANENT_RULE_NOT_SAVED_NOTICE_TITLE = "Permanent rule not saved"
HID_ATTACHED_NOTICE_TITLE = "New keyboard/HID attached"
DEVICE_INSERTED_NOTICE_TITLE = "New USB device inserted"
