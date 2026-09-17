"""What the Desktop and Startup shortcuts actually run.

Exists because of a real failure that was impossible to diagnose: double
clicking the shortcut did nothing at all -- no window, no tray icon, no log
line, no error. Anything that goes wrong before logging is configured happens
in total silence under pythonw.exe, which has no console to print to.

Two things fix that:

  * sys.path is set from this file's own location, so the app does not depend
    on the shortcut's "Start in" directory being honoured. `python -m climp.main`
    needs the working directory to be the repo; this does not.
  * Everything is wrapped. Any failure is written to launch.log and shown in a
    message box, so a silent death is no longer possible.

Run directly for the same behaviour with a console:  python climp_launcher.pyw
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))

# Before anything else: make the package importable regardless of where the
# process was started from.
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _ensure_streams() -> None:
    """Give pythonw.exe somewhere to print to.

    Launched from Explorer, pythonw has no console and sys.stdout/stderr are
    None. Any library that prints then raises AttributeError -- which is
    exactly what killed the Google sign-in flow, since run_local_server prints
    the authorisation URL before waiting for the redirect. The thread died
    silently and sign-in simply never happened.
    """
    devnull = None
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            if devnull is None:
                devnull = open(os.devnull, "w", encoding="utf-8")
            setattr(sys, name, devnull)


_ensure_streams()


def _log_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "climp", "logs")
    os.makedirs(path, exist_ok=True)
    return path


def _note(message: str) -> None:
    """Append to launch.log. Deliberately dependency-free and failure-tolerant.

    This has to work before climp is importable, so it cannot use the app's
    own logging.
    """
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(_log_dir(), "launch.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:
        pass


def _show_error(title: str, body: str) -> None:
    # The dialog is modal and waits for a click, which is right for a person
    # and wrong for anything unattended -- a test or a CI run would hang on it.
    if os.environ.get("CLIMP_NO_DIALOG"):
        _note(f"dialog suppressed: {title}: {body.splitlines()[0]}")
        return
    try:
        import ctypes

        # ICONERROR | SETFOREGROUND | TOPMOST -- without the last two it opens
        # behind a fullscreen game, which is exactly when people launch this.
        ctypes.windll.user32.MessageBoxW(None, body, title, 0x10 | 0x10000 | 0x40000)
    except Exception:
        pass


def main() -> int:
    _note(f"launch: exe={sys.executable} cwd={os.getcwd()} dir={HERE}")
    try:
        from climp.main import main as climp_main
    except BaseException:
        tb = traceback.format_exc()
        _note("IMPORT FAILED\n" + tb)
        _show_error(
            "climp could not start",
            "climp failed while loading.\n\n"
            f"{tb.strip().splitlines()[-1]}\n\n"
            f"Full details: {os.path.join(_log_dir(), 'launch.log')}",
        )
        return 1

    try:
        climp_main()
    except SystemExit:
        raise
    except BaseException:
        tb = traceback.format_exc()
        _note("CRASHED\n" + tb)
        _show_error(
            "climp stopped unexpectedly",
            "climp hit an error and closed.\n\n"
            f"{tb.strip().splitlines()[-1]}\n\n"
            f"Full details: {os.path.join(_log_dir(), 'launch.log')}",
        )
        return 1

    _note("exited normally")
    return 0


if __name__ == "__main__":
    sys.exit(main())
