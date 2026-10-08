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
    "Screen locking is unavailable — allowing this HID device is disabled.\n"
    "Block and Reject still work; devices remain blocked by USBGuard's policy."
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

# Title for the notice raised when a queued `Allow` is handed back to the
# lock-first flow.  Named so the tests can select the message by identity rather
# than by matching prose: the wording is user-facing and will be reworded, and
# the tests that police *what it is allowed to claim* should not have to move
# every time somebody improves a sentence.  What it must never claim is a lock
# screen -- see `TestTheHandbackWarningPromisesNothingItCannotKeep`.
HANDBACK_NOTICE_TITLE = "Held Allow cleared no permanent rule"

# Phrasings that would promise the user a live authorization this code path does
# not control.  Whether the lock screen ever arrives is decided after
# `_apply_pending_decision` returns, so the notice may describe the policy and
# must not forecast the event.
_LIVE_AUTHORIZE_PROMISES = (
    "will be authorized",
    "will authorize",
    "will be allowed",
    "goes through the lock",
    "is authorized behind the lock screen for you",
)
