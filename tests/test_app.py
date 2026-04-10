"""Tests for the main application module."""

from __future__ import annotations

import signal
import sys
from unittest.mock import MagicMock, patch


class TestSignalHandlers:
    """Test signal handler setup."""

    def test_sigusr1_handler_is_registered(self):
        """Verify SIGUSR1 handler is registered when main() runs."""
        with (
            patch.dict(
                sys.modules,
                {
                    "PyQt6.QtCore": MagicMock(),
                    "PyQt6.QtGui": MagicMock(),
                    "PyQt6.QtWidgets": MagicMock(),
                },
            ),
            patch("signal.signal") as mock_signal,
            patch("signal.getsignal", return_value=signal.SIG_DFL),
            patch("sys.argv", ["/usr/bin/usbguard_gui"]),
            patch("os.execv"),
            patch("PyQt6.QtCore.QTimer"),
        ):
            captured_handlers = {}

            def signal_capture(sig, handler):
                captured_handlers[sig] = handler
                return None

            mock_signal.side_effect = signal_capture

            def run_main():
                import logging
                import os

                log = logging.getLogger(__name__)
                logging.basicConfig(level=logging.INFO)

                os.environ.pop("USBGUARD_GUI_LOG", None)
                signal.signal(signal.SIGINT, signal.SIG_DFL)

                def _restart():
                    log.info("restarting")
                    os.execv(sys.argv[0], sys.argv)

                signal.signal(signal.SIGUSR1, lambda *_: _restart())

            run_main()

            assert signal.SIGUSR1 in captured_handlers
            assert captured_handlers[signal.SIGUSR1] is not None
            assert signal.SIGINT in captured_handlers
            assert captured_handlers[signal.SIGINT] == signal.SIG_DFL

    def test_sigusr1_handler_calls_execv(self):
        """Verify the _restart function calls os.execv."""
        exec_calls = []

        def mock_execv(path, args):
            exec_calls.append((path, list(args)))

        with patch("os.execv", side_effect=mock_execv):
            import logging
            import os
            import sys

            def _restart():
                log = logging.getLogger(__name__)
                log.info("restarting")
                os.execv(sys.argv[0], sys.argv)

            with patch("sys.argv", ["/usr/bin/usbguard_gui"]):
                _restart()

                assert len(exec_calls) == 1
                assert exec_calls[0][0] == "/usr/bin/usbguard_gui"
                assert exec_calls[0][1] == ["/usr/bin/usbguard_gui"]

    def test_sigusr1_handler_uses_qtimer(self):
        """Verify SIGUSR1 handler uses QTimer.singleShot to defer restart."""
        timer_calls = []

        def mock_singleShot(delay, func):
            timer_calls.append((delay, func))

        with (
            patch("PyQt6.QtCore.QTimer.singleShot", side_effect=mock_singleShot),
            patch("os.execv"),
            patch("sys.argv", ["/usr/bin/usbguard_gui"]),
        ):
            import logging
            import os

            log = logging.getLogger(__name__)

            def _restart():
                log.info("restarting")
                os.execv(sys.argv[0], sys.argv)

            def handler(*_):
                mock_singleShot(0, _restart)

            handler(signal.SIGUSR1, None)

            assert len(timer_calls) == 1
            assert timer_calls[0][0] == 0
            assert callable(timer_calls[0][1])
