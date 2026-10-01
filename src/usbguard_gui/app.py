"""Main application: system tray icon, event routing, and lifecycle."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from enum import Enum
from pathlib import Path

from PyQt6.QtCore import QLockFile, QStandardPaths, QTimer
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

import usbguard_gui
from usbguard_gui.dbus_client import USBGuardClient
from usbguard_gui.device import Device, DeviceTarget, Persistence, PresenceEvent, parse_device_rule
from usbguard_gui.device_dialog import DeviceActionDialog
from usbguard_gui.device_list import DeviceListWindow
from usbguard_gui.screensaver import ScreensaverMonitor
from usbguard_gui.settings import Settings, SettingsProtocol

log = logging.getLogger(__name__)

# SVG bundled in the source tree for dev-mode runs (make run).
_DEV_SVG = Path(__file__).parent.parent.parent / "rpm" / "usbguard_gui.svg"


def _app_icon() -> QIcon:
    """Return the application icon.

    Installed: picked up from the hicolor theme (put there by the RPM).
    Dev (make run): loaded directly from rpm/usbguard_gui.svg in the source tree.
    Fallback: generic drive-removable-media theme icon.
    """
    icon = QIcon.fromTheme("usbguard_gui")
    if not icon.isNull():
        return icon
    if _DEV_SVG.exists():
        return QIcon(str(_DEV_SVG))
    return QIcon.fromTheme("drive-removable-media")


def _enum_name(enum: type[Enum], value: int, fallback: str = "?") -> str:
    """Return the name of an enum member by value, or a fallback string."""
    try:
        return enum(value).name
    except ValueError:
        return fallback


# Base seconds between reconnection attempts (will be exponentially increased)
RECONNECT_BASE_INTERVAL = 5
# Maximum seconds between reconnection attempts
RECONNECT_MAX_INTERVAL = 60
# Milliseconds between the HID warning notification and the actual screen lock.
# The device stays blocked by USBGuard's default policy during this window (it is
# only allowed after the screen has locked, in _on_screensaver_locked),
# so this delay does not reopen the keystroke-injection window — it just gives
# the tray notification time to appear before the screen blanks.
HID_LOCK_NOTIFY_DELAY_MS = 5000

# Cap on queued screensaver-unlock cycles.  Entries are only consumed by a
# non-empty list_devices() result (a failed/empty snapshot deliberately does not
# consume one, so a transient daemon disconnect cannot drop a prompt), which means
# a daemon that stays down while the user keeps locking/unlocking would otherwise
# grow the queue for the life of the process.  Dropping the oldest cycle loses only
# a prompt; the devices stay blocked.
MAX_PENDING_UNLOCK_CYCLES = 32

# How long to stay quiet about a device identity that has already been prompted
# for, even after the user dismisses its dialog. A flapping device can otherwise
# prompt again on each landing once the previous dialog is gone. This is the
# backstop. The device is never silently
# allowed during the cooldown, it simply is not re-announced; it stays wherever
# USBGuard's policy put it.  Override with USBGUARD_GUI_PROMPT_COOLDOWN.
PROMPT_COOLDOWN_SEC = int(os.environ.get("USBGUARD_GUI_PROMPT_COOLDOWN", "30"))

# Cap on decisions held for devices that are off the bus.  They normally drain
# within a second or two of the next appearance, but a device the user decided
# about and then threw away would otherwise leave the entry for the life of the
# process.  Dropping the oldest loses a queued decision; nothing is applied
# that nobody asked for.
MAX_PENDING_DECISIONS = 32

# Tray title for the notice raised when a queued `Allow` is handed back to the
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


class USBGuardTrayApp:
    """System tray application for USBGuard."""

    def __init__(self, app: QApplication, lock_file: QLockFile | None = None,
                 settings: SettingsProtocol | None = None) -> None:
        self._app = app
        # Single-instance QLockFile, if any: held here (rather than left as
        # a local in main(), kept alive only by app.exec() blocking in the
        # same stack frame) so _quit() can explicitly release it instead of
        # relying on process exit.
        self._instance_lock = lock_file
        self._client = USBGuardClient()
        self._screensaver = ScreensaverMonitor()
        # Settings are injected so tests never touch the real per-user config
        # file; production (main()) passes nothing and gets the QSettings
        # singleton.  See SettingsProtocol for why this seam exists.
        self._settings: SettingsProtocol = settings if settings is not None else Settings()
        self._device_list_window: DeviceListWindow | None = None
        self._open_dialogs: dict[int, DeviceActionDialog] = {}
        self._open_dialog_identities: dict[str, int] = {}
        self._last_prompted_at: dict[str, float] = {}
        # Decisions made while the device was off the bus, keyed by identity:
        # a flapping device is worth deciding about in the gap between one
        # incarnation and the next.  They stand until that device shows up
        # again, which is exactly what "block this even though it was only
        # plugged in for a second" means.
        self._pending_decisions: dict[str, tuple[DeviceTarget, Persistence]] = {}
        self._screensaver_pending_devices: set[int] = set()
        self._hid_pending_devices: set[int] = set()
        # Outstanding screensaver-unlock cycles, keyed by the request id of the
        # fetch_devices() call whose snapshot answers that cycle.  A FIFO list
        # here had to assume that every list_devices_result on the shared signal
        # belonged to an unlock cycle, and that answers arrived in call order.
        # Neither holds: the device-list window issues its own list_devices()
        # calls on that same signal, and D-Bus makes no ordering promise, so a
        # cycle could be matched against somebody else's (or a later cycle's)
        # snapshot and its prompt silently dropped.  Keyed by id, a cycle is
        # resolved only by its own snapshot, arriving in any order.
        self._pending_unlock_cycles: dict[int, set[int]] = {}
        self._next_unlock_cycle_id: int = 0
        self._permanent_allow_hashes: set[str] = set()
        # Whether screen locking is available (ScreenSaver service reachable).
        # While False, the HID lock-first flow cannot work and the UI must
        # refuse all allow/deny actions — see _on_lock_availability_changed.
        self._lock_available = self._screensaver.connected
        self._lock_state_confirmed = False

        # Reconnect timer with exponential backoff. Single-shot: each failed
        # attempt (connection_changed(False)) reschedules it with a doubled
        # interval, capped at RECONNECT_MAX_INTERVAL.
        self._reconnect_timer = QTimer()
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.setInterval(RECONNECT_BASE_INTERVAL * 1000)
        self._reconnect_timer.timeout.connect(self._try_connect)
        self._reconnect_attempts = 0

        # Deferred screen lock for HID inserts. Single-shot and cancellable so
        # that unplugging the triggering device before it fires aborts the lock
        # (see _on_device_presence_changed REMOVE handling).
        self._hid_lock_timer = QTimer()
        self._hid_lock_timer.setSingleShot(True)
        self._hid_lock_timer.timeout.connect(self._lock_for_pending_hid)

        self._setup_tray()
        self._connect_signals()
        self._connect_client_signals()

    def _setup_tray(self) -> None:
        self._tray = QSystemTrayIcon(_app_icon(), self._app)
        self._tray.setToolTip("USBGuard GUI — connecting...")

        menu = QMenu()
        self._action_show = QAction("Show Devices")
        self._action_show.triggered.connect(self._show_device_list)
        menu.addAction(self._action_show)

        menu.addSeparator()

        self._action_disable_hid = QAction("Disable special HID device treatment")
        self._action_disable_hid.setCheckable(True)
        self._action_disable_hid.setChecked(self._settings.disable_hid_treatment())
        self._action_disable_hid.toggled.connect(self._on_disable_hid_toggled)
        menu.addAction(self._action_disable_hid)

        menu.addSeparator()

        self._action_about = QAction("About")
        self._action_about.triggered.connect(self._show_about)
        menu.addAction(self._action_about)

        menu.addSeparator()

        self._action_quit = QAction("Quit")
        self._action_quit.triggered.connect(self._quit)
        menu.addAction(self._action_quit)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_disable_hid_toggled(self, checked: bool) -> None:
        self._settings.set_disable_hid_treatment(checked)

    def _show_about(self) -> None:
        QMessageBox.about(
            None,
            "About USBGuard GUI",
            f"<b>USBGuard GUI</b> v{usbguard_gui.__version__}<br>"
            "KDE/Qt system tray GUI for USBGuard.<br><br>"
            f"Author: {usbguard_gui.__author__}<br>"
            f"License: {usbguard_gui.__license__}",
        )

    def _connect_signals(self) -> None:
        self._client.device_presence_changed.connect(self._on_device_presence_changed)
        self._client.device_policy_changed.connect(self._on_device_policy_changed)
        self._client.connection_changed.connect(self._on_connection_changed)
        self._screensaver.active_changed.connect(self._on_screensaver_unlocked)
        self._screensaver.active_changed.connect(self._on_screensaver_locked)
        self._screensaver.connection_changed.connect(self._on_lock_availability_changed)

    def _on_lock_availability_changed(self, available: bool) -> None:
        # The monitor reports its state repeatedly (initial report plus
        # every failed retry), so notify only on the first confirmed report
        # or an actual transition.
        changed = available != self._lock_available
        first = not self._lock_state_confirmed
        self._lock_available = available
        self._lock_state_confirmed = True
        if not available and (changed or first):
            self._tray.showMessage(
                "Screen locking unavailable",
                "The screen cannot be locked, so device actions are disabled. "
                "Devices remain blocked by USBGuard's policy.",
                QSystemTrayIcon.MessageIcon.Warning,
                10000,
            )
        elif available and changed and not first:
            self._tray.showMessage(
                "Screen locking available",
                "USBGuard GUI device actions re-enabled.",
                QSystemTrayIcon.MessageIcon.Information,
                5000,
            )

    def _connect_client_signals(self) -> None:
        self._client.list_devices_result.connect(self._on_list_devices_result)
        self._client.list_devices_correlated.connect(self._on_correlated_devices)
        self._client.list_rules_result.connect(self._on_list_rules_result)
        self._client.permanent_write_failed.connect(self._on_permanent_write_failed)
        self._client.permanent_clear_failed.connect(self._on_permanent_clear_failed)
        self._client.temporary_apply_failed.connect(self._on_temporary_apply_failed)
        self._client.permanent_rule_remains.connect(self._on_permanent_rule_remains)

    def _on_permanent_rule_remains(self, device_id: int, action: str, rule: str) -> None:
        # The device's own rule was cleared, but a broader rule still covers it
        # and we will not delete hand-written admin policy on a tray click.
        # Say so -- otherwise "Allow Once" reads as if it took effect while the
        # device is in fact permanently allowed.
        self._tray.showMessage(
            "Temporary decision incomplete",
            f"A broader permanent rule still covers device {device_id} and was not removed:\n"
            f"{rule}\n"
            f"The device stays permanently {action}. Edit /etc/usbguard/rules.conf to revoke "
            f"that rule deliberately.",
            QSystemTrayIcon.MessageIcon.Warning,
            15000,
        )

    def _on_permanent_clear_failed(self, device_id: int, action: str, reason: str, partial: bool) -> None:
        # The clear comes before the live change, so a refused removal means the
        # "Once" decision never happened at all and the device keeps its standing
        # rule -- which the user believes they just cleared.
        #
        # `partial` is the case where the device owned several permanent rules
        # and the clear died partway: the decision still did not take effect, but
        # rules.conf *was* rewritten, and calling that "could not be removed"
        # sends the user looking for a file that no longer matches.
        if partial:
            self._tray.showMessage(
                "Temporary decision not applied — policy partly changed",
                f"Device {device_id}'s permanent rules were only partly removed, so the temporary "
                f"{action} did not take effect and the stored policy is no longer what it was.\n"
                f"{reason}\nCheck /etc/usbguard/rules.conf before deciding again.",
                QSystemTrayIcon.MessageIcon.Warning,
                15000,
            )
            return
        self._tray.showMessage(
            "Temporary decision not applied",
            f"The existing permanent rule for device {device_id} could not be removed, so the "
            f"temporary {action} did not take effect.\n{reason}",
            QSystemTrayIcon.MessageIcon.Warning,
            10000,
        )

    def _on_temporary_apply_failed(self, device_id: int, action: str, reason: str, policy_changed: bool) -> None:
        title = "Temporary decision not applied"
        detail = ""
        if policy_changed:
            title += " — permanent rules removed"
            detail = ("\nPermanent rules were removed before the live action failed. "
                      "The stored policy has changed; check /etc/usbguard/rules.conf before deciding again.")
        self._tray.showMessage(
            title,
            f"The temporary {action} for device {device_id} did not take effect.\n{reason}{detail}",
            QSystemTrayIcon.MessageIcon.Warning,
            15000,
        )

    def _on_permanent_write_failed(self, device_id: int, action: str, reason: str) -> None:
        # The device is in the requested state right now, but only until it is
        # unplugged.  Without this the user reads "Allow (Permanent)" as done
        # and finds out otherwise at the next boot -- the failure is otherwise
        # a log line in a tray app nobody tails.
        self._tray.showMessage(
            "Permanent rule not saved",
            f"The permanent {action} for device {device_id} could not be written; it applies only "
            f"until the device is unplugged.\n{reason}",
            QSystemTrayIcon.MessageIcon.Warning,
            10000,
        )

    def _on_list_rules_result(self, rules: list[tuple[int, str]]) -> None:
        self._permanent_allow_hashes.clear()
        for _, rule_str in rules:
            parsed = parse_device_rule(rule_str)
            if parsed["rule"] == "allow" and parsed["hash"]:
                self._permanent_allow_hashes.add(str(parsed["hash"]))

    def _on_list_devices_result(self, devices: list[Device]) -> None:
        # Opportunistic HID safety net: a fresh snapshot taken while the screen
        # is actually locked lets any pending HID device in — that is the moment
        # a newly-attached keyboard is safe to activate (unlocking requires a
        # password).  The primary path is _on_screensaver_locked(); this only
        # matters if that signal was missed.  Allowing them while the screen is
        # still unlocked would hand a just-plugged keyboard keystrokes on an
        # unlocked session, so the active check stays.  Deferred-unlock cycles
        # are NOT resolved here — they need the snapshot fetched specifically
        # for them, see _on_correlated_devices.
        if self._hid_pending_devices and self._screensaver.active:
            pending_ids = self._hid_pending_devices
            self._hid_pending_devices = set()
            for device_number in pending_ids:
                if any(d.number == device_number for d in devices):
                    self._client.apply_device_policy(device_number, DeviceTarget.ALLOW,
                                                     persistence=Persistence.UNCHANGED)

    def start(self) -> None:
        """Initialize D-Bus connections and start the application.

        connect() only starts the worker thread and always returns True;
        the actual connection result arrives asynchronously via
        connection_changed, which schedules reconnection with backoff.
        """
        self._screensaver.connect()
        self._client.connect()

    def _try_connect(self) -> None:
        # connect() recycles any previous worker thread; success/failure
        # arrives asynchronously via connection_changed, which reschedules
        # the next attempt with exponential backoff.
        self._client.connect()

    def _on_connection_changed(self, connected: bool) -> None:
        if connected:
            self._tray.setToolTip("USBGuard GUI — connected")
            self._reconnect_timer.stop()
            self._reconnect_attempts = 0  # Reset on successful connection
            self._client.list_rules()
            self._retry_pending_unlock_cycles()
            return

        self._tray.setToolTip("USBGuard GUI — disconnected (retrying...)")
        if self._reconnect_timer.isActive():
            return
        backoff = min(RECONNECT_BASE_INTERVAL * (2 ** self._reconnect_attempts), RECONNECT_MAX_INTERVAL)
        self._reconnect_attempts += 1
        self._reconnect_timer.setInterval(backoff * 1000)
        log.debug("Connection attempt failed, retrying in %d seconds", backoff)
        self._reconnect_timer.start()

    def _on_device_presence_changed(self, device_id: int, event: int, target: int, device_rule: str,
                                    attributes: dict[str, str]) -> None:
        try:
            log.debug(
                "DevicePresenceChanged: id=%d event=%d(%s) target=%d(%s) rule=%r attributes=%r",
                device_id,
                event,
                _enum_name(PresenceEvent, event),
                target,
                _enum_name(DeviceTarget, target),
                device_rule,
                attributes,
            )

            if event == PresenceEvent.REMOVE:
                # Keep the dialog open.  A flapping device vanishes moments
                # after appearing, and closing the dialog on every REMOVE left
                # the user with no way to decide at all -- it vanished in under
                # three seconds and the cooldown then suppressed the next one.
                # The decision is worth keeping on screen: if the device is back
                # when the user clicks it applies immediately, and if not it is
                # queued for the next appearance.
                dialog = self._open_dialogs.get(device_id)
                if dialog:
                    log.info("Device %d dropped -- keeping its dialog open; a choice made now "
                             "applies when it returns", device_id)
                    dialog.set_device_present(False)
                # Drop the device from any pending set — it is gone, so it must
                # neither be auto-allowed on lock nor prompted for on unlock.
                self._hid_pending_devices.discard(device_id)
                self._screensaver_pending_devices.discard(device_id)
                # If this was the last HID device awaiting the deferred lock,
                # cancel the lock: the user unplugged the device before it fired.
                if not self._hid_pending_devices and self._hid_lock_timer.isActive():
                    log.info("HID device %d removed before lock — cancelling scheduled lock", device_id)
                    self._hid_lock_timer.stop()
                return

            # Only react to new insertions — PRESENT fires for devices already
            # connected at daemon start, UPDATE fires on policy changes; neither
            # should spawn a dialog.
            if event != PresenceEvent.INSERT:
                log.debug("DevicePresenceChanged: id=%d skipped (not INSERT)", device_id)
                return

            device = Device.from_dbus(device_id, device_rule)
            # Synchronize retained dialogs even when this insertion will not
            # prompt (already allowed, HID lock flow, or screensaver deferral).
            self._retarget_device_dialog(device)

            # Before anything else: a decision the user already made about this
            # device outranks every reaction below, including the ones that never
            # reach a prompt.  A device that comes back already allowed, or a HID
            # device heading for the lock-first flow, used to skip straight past
            # the queue and do the default instead.
            if self._apply_pending_decision(device):
                return

            # Use the target from the signal directly — more reliable than
            # parsing the rule string, which may not reflect the applied target.
            if target == int(DeviceTarget.ALLOW):
                log.debug("DevicePresenceChanged: id=%d skipped (target=ALLOW)", device_id)
                return

            log.info("INSERT device %d %s %r identity=%s target=%s", device_id, device.id,
                     device.name or "", self._dialog_identity(device), _enum_name(DeviceTarget, target))

            # HID devices are handled before the screensaver check so that a
            # newly-attached keyboard can be used to unlock the screen.
            # has_hid_interface() catches composite devices (e.g. HID + MSC)
            # too — any HID interface can send keystrokes.
            #
            # The special-treatment path (auto-allow then lock) is skipped
            # entirely when the user has disabled it in settings OR when
            # screen locking is currently inhibited by a logind idle/block
            # inhibitor (dnf/rpm transaction, GNOME/KDE "Prevent screen
            # lock", systemd-inhibit --what=idle, ...). Auto-allowing a HID
            # device while lock is inhibited would hand an attached-keyboard
            # attacker typed input with no password prompt to gate it.
            has_hid = device.has_hid_interface()
            hid_treatment_enabled = not self._settings.disable_hid_treatment()
            lock_inhibited = self._screensaver.inhibited
            # The auto-allow-then-lock flow additionally requires that
            # locking is actually available: without it the deferred lock
            # would no-op, the 'Locking screen…' notice would be a lie, and
            # the pending device could never be auto-allowed.  Fall back to
            # the normal prompt path (whose actions are disabled) instead.
            # The automatic-flow conjunction lives in _hid_lock_flow_applies.
            # A held HID Allow must obey the contract even when that flow cannot
            # run; _apply_pending_decision then requires a fresh choice instead.
            hid_special_treatment = self._hid_lock_flow_applies(device)
            if hid_special_treatment:
                if self._screensaver.active:
                    log.info(
                        "HID device %d inserted while screen locked, allowing temporarily so it can unlock",
                        device_id,
                    )
                    self._client.apply_device_policy(device_id, DeviceTarget.ALLOW, persistence=Persistence.UNCHANGED)
                    return
                # Skip HID treatment for devices that are already whitelisted
                # (matching a permanent allow rule from the daemon's policy).
                if device.hash and device.hash in self._permanent_allow_hashes:
                    log.debug("DevicePresenceChanged: id=%d skipped (permanent allow hash match)", device_id)
                    return
                self._hid_pending_devices.add(device_id)
                self._tray.showMessage(
                    "New keyboard/HID attached",
                    "Locking screen. Enter your password to activate the device. "
                    "If you did not attach a keyboard, check for malicious devices.",
                    QSystemTrayIcon.MessageIcon.Warning,
                    5000,
                )
                # Never restart an already-running lock timer: a second HID insert
                # would push the first device's lock back by another full delay.
                # _hid_pending_devices already covers every waiting device, so the
                # earliest scheduled lock serves them all.
                if not self._hid_lock_timer.isActive():
                    self._hid_lock_timer.start(HID_LOCK_NOTIFY_DELAY_MS)
                return
            elif has_hid and hid_treatment_enabled:
                # lock_inhibited or not lock_available — fall through to the
                # normal prompt path
                reason = "screen locking is inhibited" if lock_inhibited else "screen locking is unavailable"
                log.info(
                    "HID device %d inserted while %s — falling back to prompt (will not auto-allow)",
                    device_id,
                    reason,
                )

            # Non-HID device: defer while screen is locked, otherwise prompt.
            if self._screensaver.active:
                self._screensaver_pending_devices.add(device_id)
                log.info("Device %d inserted while screen locked, deferring", device_id)
                return

            self._show_device_dialog(device)
        except Exception as e:
            log.exception("Error in _on_device_presence_changed for device %d: %s", device_id, e)

    def _on_device_policy_changed(self, device_id: int, target_old: int, target_new: int, device_rule: str,
                                  rule_id: int, attributes: dict[str, str]) -> None:
        try:
            log.debug(
                "DevicePolicyChanged: id=%d %s->%s",
                device_id,
                _enum_name(DeviceTarget, target_old, fallback=str(target_old)),
                _enum_name(DeviceTarget, target_new, fallback=str(target_new)),
            )
            if target_new == int(DeviceTarget.ALLOW):
                # Device was allowed by a permanent rule after the initial block —
                # dismiss any dialog that opened on the INSERT event.
                dialog = self._open_dialogs.pop(device_id, None)
                if dialog:
                    log.debug("DevicePolicyChanged: id=%d closing dialog (device now allowed)", device_id)
                    dialog.close()
                self._screensaver_pending_devices.discard(device_id)
                self._hid_pending_devices.discard(device_id)
                # Seed the permanent-allow cache so future re-insertions skip
                # HID treatment.
                if rule_id > 0:
                    d = Device.from_dbus(device_id, device_rule)
                    if d.hash:
                        self._permanent_allow_hashes.add(d.hash)
        except Exception as e:
            log.exception("Error in _on_device_policy_changed for device %d: %s", device_id, e)

    def _lock_for_pending_hid(self) -> None:
        """Lock the screen for a deferred HID insert, unless every triggering
        device was unplugged during the notification delay."""
        if not self._hid_pending_devices:
            log.debug("HID lock timer fired with no pending devices — skipping lock")
            return
        if not self._lock_available:
            # Locking became unavailable while the delay was running — do
            # not claim to lock.  The pending devices stay blocked (safe);
            # they are cleared on removal or the next lock.
            log.warning("HID lock timer fired but screen locking is unavailable — devices stay blocked")
            return
        self._screensaver.lock()

    def _on_screensaver_unlocked(self, active: bool) -> None:
        """Screen unlocked: collect the devices deferred while the screen was
        locked and show their summary prompts."""
        if active or not self._screensaver_pending_devices:
            return

        cycle_id = self._register_unlock_cycle(self._screensaver_pending_devices)
        self._screensaver_pending_devices.clear()
        self._client.fetch_devices(cycle_id)

    def _register_unlock_cycle(self, device_ids: set[int]) -> int:
        """Record one deferred-unlock cycle and return the request id its fetch
        must carry.

        Past MAX_PENDING_UNLOCK_CYCLES the oldest cycle is dropped, so a daemon
        that stays down across many lock/unlock cycles cannot grow the map for
        the life of the process.  A dropped cycle loses only its prompt — the
        devices stay blocked by USBGuard's policy.
        """
        cycle_id = self._next_unlock_cycle_id
        self._next_unlock_cycle_id += 1
        self._pending_unlock_cycles[cycle_id] = set(device_ids)
        while len(self._pending_unlock_cycles) > MAX_PENDING_UNLOCK_CYCLES:
            oldest = next(iter(self._pending_unlock_cycles))
            dropped = self._pending_unlock_cycles.pop(oldest)
            log.warning(
                "Unlock-cycle queue full (%d) — dropping the oldest pending set (%d id(s)); "
                "those devices stay blocked",
                MAX_PENDING_UNLOCK_CYCLES,
                len(dropped),
            )
        return cycle_id

    def _retry_pending_unlock_cycles(self) -> None:
        """Re-fetch every cycle still outstanding once the daemon is back.

        A cycle keeps its id across retries, so a late answer to the failed
        attempt resolves it just as well, while an answer for a cycle that has
        already been resolved is ignored.
        """
        if not self._pending_unlock_cycles:
            return
        log.info("Reconnected — retrying %d outstanding unlock cycle(s)", len(self._pending_unlock_cycles))
        for cycle_id in list(self._pending_unlock_cycles):
            self._client.fetch_devices(cycle_id)

    def _on_correlated_devices(self, request_id: int, devices: list[Device]) -> None:
        """Resolve one unlock cycle against the snapshot fetched for it.

        Anything that is not a cycle still waiting — another caller's fetch, or a
        late answer for a cycle already resolved — is ignored.  That is the whole
        point of the correlation: the device-list window's refreshes can no
        longer consume an unlock cycle, and answers may arrive in any order.
        """
        pending_ids = self._pending_unlock_cycles.pop(request_id, None)
        if pending_ids is None:
            return

        if not devices:
            # An empty snapshot means the fetch fast-failed (daemon
            # disconnected) or hit a DBusError.  Put the cycle back: the
            # devices may still be present, and dropping the entry here is how a
            # transient disconnect silently lost the prompt.  It gets retried
            # when the daemon returns — see _retry_pending_unlock_cycles().
            self._pending_unlock_cycles[request_id] = pending_ids
            return

        pending_devices = [d for d in devices if d.number in pending_ids and not d.is_allowed()]
        if not pending_devices:
            return

        count = len(pending_devices)
        names = "\n".join(f"  - {d.name or d.id} ({d.class_description_string()})" for d in pending_devices)
        info = QSystemTrayIcon.MessageIcon.Information
        self._tray.showMessage(f"{count} USB device(s) connected during absence", names, info, 10000)

        for device in pending_devices:
            self._show_device_dialog(device)

    def _on_screensaver_locked(self, active: bool) -> None:
        """Screen locked: auto-allow pending HID devices so the newly-attached
        keyboard can be used to unlock."""
        if not active or not self._hid_pending_devices:
            return

        pending_ids = list(self._hid_pending_devices)
        self._hid_pending_devices = set()
        for device_number in pending_ids:
            self._client.apply_device_policy(device_number, DeviceTarget.ALLOW, persistence=Persistence.UNCHANGED)

    @staticmethod
    def _dialog_identity(device: Device) -> str:
        """A device/topology key that survives re-enumeration on the same port.

        Identical hubs can share a hash and even a serial. Include the parent
        and port so one live device cannot inherit another's dialog or queued
        choice. Tuple repr also keeps descriptor delimiters unambiguous.
        """
        return repr((device.id, device.serial, device.hash, device.parent_hash, device.via_port))

    def _retarget_device_dialog(self, device: Device) -> bool:
        """Synchronize an existing dialog with the current device instance."""
        identity = self._dialog_identity(device)
        open_number = self._open_dialog_identities.get(identity)
        if open_number is None:
            return False
        dialog = self._open_dialogs.get(open_number)
        if dialog is None:
            return False
        self._open_dialogs.pop(open_number, None)
        dialog.set_device(device)
        self._open_dialogs[device.number] = dialog
        self._open_dialog_identities[identity] = device.number
        log.info("Device %s re-appeared as id %d -- the open dialog now targets it", identity, device.number)
        return True

    def _hid_lock_flow_applies(self, device: Device) -> bool:
        """Would this device take the auto-allow-then-lock path?

        `has_hid_interface()` catches composite devices (HID + MSC) too — any
        HID interface can send keystrokes.  The flow is off when the user
        disabled it, and skipped when locking is inhibited or unavailable,
        because a deferred lock that never fires would auto-allow a keyboard
        with no password prompt to gate it.
        """
        return (device.has_hid_interface()
                and not self._settings.disable_hid_treatment()
                and not self._screensaver.inhibited
                and self._lock_available)

    def _apply_pending_decision(self, device: Device) -> bool:
        """Replay a held decision; return True if it consumes this insertion.

        Called before every other reaction to an insertion, because none of them
        apply to a device the user has already decided about.  Draining only
        from the prompt path meant the returns that never reach a prompt --
        the device came back already allowed, or it is a HID device heading for
        the lock-first flow -- silently dropped the decision and did the default
        instead.  For a queued `Block` on a keyboard that default was an
        auto-allow: the exact opposite of the click.

        The one thing a queued decision may not do is authorize a HID device
        outside the lock.  That contract is about proving a person is at the
        machine *now*, and a click made minutes ago while the device was off the
        bus does not prove it, so a queued `Allow` on such a device is handed
        back: the lock-first flow authorizes it behind the password prompt, or
        -- when the device returns already allowed -- there was never a live
        change left to make.  Which of the two happens is decided after this
        method returns, so the notice speaks of neither as a promise.

        What the handback does not carry is the other half of `Once`: the
        standing permanent rule stays.  It is not deferred to the lock either --
        dropping a permanent `block` widens what the *next* insertion is
        allowed to do, which is the same stale-click problem one layer down,
        and the lock-first flow authorizes with `UNCHANGED` so nothing else
        makes the change.  Rather than lose it silently, the user is told the
        rule survived and can decide again with the device in hand.
        """
        identity = self._dialog_identity(device)
        pending = self._pending_decisions.get(identity)
        if pending is None:
            return False
        target, persistence = pending

        # The HID contract does not disappear when the automatic lock flow
        # cannot run. Inhibited/unavailable locking requires a fresh decision,
        # never an unattended allow from a click made while the device was away.
        if target is DeviceTarget.ALLOW and device.has_hid_interface() and not self._settings.disable_hid_treatment():
            del self._pending_decisions[identity]
            self._last_prompted_at.pop(identity, None)
            log.info("Device %s is back as id %d with a queued ALLOW, but it is a HID device -- "
                     "refusing the held authorize and the %s clear is not made",
                     identity, device.number, persistence.name)
            self._tray.showMessage(
                HANDBACK_NOTICE_TITLE,
                f"Device {device.number}'s held Allow cleared no permanent rule: a device with a "
                f"keyboard interface is authorized behind the lock screen, never from a click made "
                f"while it was away.\n"
                f"Decide again with the device connected if you meant to change it.",
                QSystemTrayIcon.MessageIcon.Warning,
                15000,
            )
            return False

        if not self._lock_available:
            self._last_prompted_at.pop(identity, None)
            log.info("Device %s is back, but screen locking is unavailable -- keeping its queued decision", identity)
            return False

        del self._pending_decisions[identity]
        log.info("Device %s is back as id %d -- applying the queued %s / %s",
                 identity, device.number, target.name, persistence.name)
        self._client.apply_device_policy(device.number, target, persistence,
                                         device.raw_rule if persistence is not Persistence.UNCHANGED else None)
        return True

    def _show_device_dialog(self, device: Device) -> None:
        # Don't open duplicate dialogs -- and don't re-notify either.  Keyed on
        # the device's identity rather than its daemon number: a device that
        # re-enumerates gets a fresh number every time, so the number-keyed
        # check let a flapping Smart IR Blaster stack notification after
        # notification with a dialog behind each one.
        identity = self._dialog_identity(device)

        # A decision made while this device was off the bus lands here the moment
        # it comes back, before any dedup or cooldown: the user already chose, so
        # there is nothing left to prompt for.  The INSERT handler drains the
        # queue too, for the returns that never reach a dialog at all.
        if self._apply_pending_decision(device):
            return

        if self._retarget_device_dialog(device):
            return

        # Cooldown also applies after the user dismisses a dialog. The device
        # is not silently allowed: it stays wherever USBGuard's policy put it.
        now = time.monotonic()
        last = self._last_prompted_at.get(identity)
        if last is not None and now - last < PROMPT_COOLDOWN_SEC:
            log.info("Device %s re-appeared as id %d %.1fs after the last prompt (cooldown %ds) -- "
                     "staying quiet; it remains blocked by policy",
                     identity, device.number, now - last, PROMPT_COOLDOWN_SEC)
            return
        self._last_prompted_at[identity] = now
        log.info("Prompting for device %d (identity %s)", device.number, identity)

        # Tray notification
        self._tray.showMessage(
            "New USB device inserted",
            f"{device.name or '(unknown)'}\n{device.class_description_string()}",
            QSystemTrayIcon.MessageIcon.Information,
            5000,
        )

        dialog = DeviceActionDialog(device, client=self._client, screensaver=self._screensaver)
        self._open_dialogs[device.number] = dialog
        self._open_dialog_identities[identity] = device.number

        def on_finished(result: int, ident: str = identity) -> None:
            target = dialog.result_target
            persistence = dialog.persistence
            # Resolved at click time, not at dialog-creation time: the device may
            # have re-enumerated several times while the dialog sat there.
            current = dialog.device
            self._open_dialogs.pop(current.number, None)
            if self._open_dialog_identities.get(ident) == current.number:
                self._open_dialog_identities.pop(ident, None)
            if target is not None:
                self._apply_user_decision(current, target, persistence, dialog.device_present)

        dialog.finished.connect(on_finished)
        dialog.show()

    def _apply_user_decision(self, device: Device, target: DeviceTarget, persistence: Persistence,
                             device_present: bool = True) -> None:
        """Dispatch a fresh choice from either UI surface, superseding pending work."""
        identity = self._dialog_identity(device)
        open_number = self._open_dialog_identities.get(identity)
        dialog = self._open_dialogs.get(open_number) if open_number is not None else None
        if dialog is not None:
            # A device-list row/menu can be older than the retained dialog.
            device, device_present = dialog.device, dialog.device_present
            dialog.close()
        self._pending_decisions.pop(identity, None)
        self._hid_pending_devices.discard(device.number)
        self._screensaver_pending_devices.discard(device.number)
        if not self._hid_pending_devices:
            self._hid_lock_timer.stop()
        if not device_present and persistence is Persistence.ALWAYS:
            log.info("Device %s is off the bus -- writing the permanent %s rule now", identity, target.name.lower())
            self._client.persist_rule(device.number, target, device.raw_rule)
            return
        if not device_present:
            log.info("Device %s is off the bus -- queuing %s / %s until it returns",
                     identity, target.name, persistence.name)
            if len(self._pending_decisions) >= MAX_PENDING_DECISIONS:
                dropped_ident = next(iter(self._pending_decisions))
                del self._pending_decisions[dropped_ident]
                log.warning("Pending-decision cap (%d) reached -- dropping the oldest queued decision for %s",
                            MAX_PENDING_DECISIONS, dropped_ident)
            self._pending_decisions[identity] = (target, persistence)
            return
        self._client.apply_device_policy(device.number, target, persistence,
                                         device.raw_rule if persistence is not Persistence.UNCHANGED else None)

    def _show_device_list(self) -> None:
        if self._device_list_window is None:
            self._device_list_window = DeviceListWindow(self._client, screensaver=self._screensaver,
                                                        decision_handler=self._apply_user_decision)
        self._device_list_window.show()
        self._device_list_window.raise_()
        self._device_list_window.activateWindow()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self._device_list_window is not None and self._device_list_window.isVisible():
                self._device_list_window.hide()
            else:
                self._show_device_list()

    def _quit(self) -> None:
        self._reconnect_timer.stop()
        if self._device_list_window:
            self._device_list_window.close()
        for dialog in list(self._open_dialogs.values()):
            dialog.close()
        self._tray.hide()
        self._client.stop()
        self._screensaver.stop()
        if self._instance_lock is not None:
            self._instance_lock.unlock()
        self._app.quit()


def main() -> None:
    """Entry point for the usbguard_gui application."""
    log_level = os.environ.get("USBGUARD_GUI_LOG", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    # Allow Ctrl+C to work
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # SIGUSR1: re-exec ourselves — sent by the RPM %posttrans scriptlet after an update
    # so the running instance picks up the new code without user intervention.
    def _restart() -> None:
        log.info("Received SIGUSR1 — restarting to apply package update")
        os.execv(sys.argv[0], sys.argv)

    signal.signal(signal.SIGUSR1, lambda *_: QTimer.singleShot(0, _restart))

    app = QApplication(sys.argv)
    app.setApplicationName("usbguard_gui")
    app.setWindowIcon(_app_icon())
    app.setQuitOnLastWindowClosed(False)

    runtime_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.RuntimeLocation)
    if not runtime_dir:
        runtime_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation)
    lock_file = QLockFile(f"{runtime_dir}/usbguard_gui.lock")
    if not lock_file.tryLock():
        QMessageBox.warning(None, "USBGuard GUI", "Another instance is already running.")
        sys.exit(0)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, "USBGuard GUI", "System tray is not available.")
        sys.exit(1)

    tray_app = USBGuardTrayApp(app, lock_file=lock_file)
    tray_app.start()

    sys.exit(app.exec())
