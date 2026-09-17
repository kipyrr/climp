"""Enable or disable launching ClipSync when you log in.

Uses a shortcut in the user's Startup folder rather than a registry Run entry:
it is visible in Explorer, removable without a tool, and touches nothing
outside your own profile.

    python tools/autostart.py --status
    python tools/autostart.py --enable
    python tools/autostart.py --disable

Nothing here runs on its own. Autostart is only ever on because you asked.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTHONW = REPO / ".venv" / "Scripts" / "pythonw.exe"  # no console window
STARTUP = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
SHORTCUT = STARTUP / "ClipSync.lnk"


def create_shortcut() -> None:
    # PowerShell's WScript.Shell is the only dependency-free way to write a .lnk.
    script = f"""
$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{SHORTCUT}')
$s.TargetPath = '{PYTHONW}'
$s.Arguments = '-m clipsync.main'
$s.WorkingDirectory = '{REPO}'
$s.Description = 'ClipSync - upload game clips to Google Drive'
$s.Save()
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True, capture_output=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--enable", action="store_true")
    g.add_argument("--disable", action="store_true")
    g.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.status:
        if SHORTCUT.exists():
            print(f"ENABLED  -> {SHORTCUT}")
        else:
            print("DISABLED - ClipSync will not start at login")
        return 0

    if args.enable:
        if not PYTHONW.exists():
            print(f"Cannot find {PYTHONW}. Is the virtualenv set up?", file=sys.stderr)
            return 1
        STARTUP.mkdir(parents=True, exist_ok=True)
        create_shortcut()
        print(f"Enabled. ClipSync will start at login via:\n  {SHORTCUT}")
        print("\nIt launches with pythonw.exe, so there is no console window - look for")
        print("the tray icon near the clock.")
        return 0

    if SHORTCUT.exists():
        SHORTCUT.unlink()
        print("Disabled. ClipSync will no longer start at login.")
    else:
        print("Already disabled; nothing to remove.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
