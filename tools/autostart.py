"""Enable or disable launching climp when you log in.

Uses a shortcut in the user's Startup folder rather than a registry Run entry:
it is visible in Explorer, removable without a tool, and touches nothing
outside your own profile.

    python tools/autostart.py --status
    python tools/autostart.py --enable          # start at login
    python tools/autostart.py --disable
    python tools/autostart.py --desktop         # add a Desktop icon
    python tools/autostart.py --remove-desktop

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
SHORTCUT = STARTUP / "climp.lnk"
DESKTOP = Path(os.environ["USERPROFILE"]) / "Desktop"
DESKTOP_SHORTCUT = DESKTOP / "climp.lnk"


def create_shortcut(target: Path) -> None:
    """Write a .lnk via WScript.Shell -- the dependency-free way on Windows.

    Points at pythonw.exe rather than python.exe, so double-clicking gives you
    the tray icon and no console window.
    """
    script = f"""
$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{target}')
$s.TargetPath = '{PYTHONW}'
$s.Arguments = '-m climp.main'
$s.WorkingDirectory = '{REPO}'
$s.IconLocation = '{REPO / "climp.ico"},0'
$s.Description = 'climp - upload game clips to Google Drive'
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
    g.add_argument("--desktop", action="store_true", help="Put a climp icon on the Desktop.")
    g.add_argument("--remove-desktop", action="store_true")
    args = ap.parse_args()

    if args.status:
        if SHORTCUT.exists():
            print(f"Start at login : ENABLED  -> {SHORTCUT}")
        else:
            print("Start at login : disabled")
        if DESKTOP_SHORTCUT.exists():
            print(f"Desktop icon   : present  -> {DESKTOP_SHORTCUT}")
        else:
            print("Desktop icon   : not present")
        return 0

    if args.desktop:
        if not PYTHONW.exists():
            print(f"Cannot find {PYTHONW}. Is the virtualenv set up?", file=sys.stderr)
            return 1
        create_shortcut(DESKTOP_SHORTCUT)
        print(f"Added: {DESKTOP_SHORTCUT}")
        print()
        print("Double-click it to start climp. No console window appears -")
        print("look for the icon near your clock instead.")
        return 0

    if args.remove_desktop:
        if DESKTOP_SHORTCUT.exists():
            DESKTOP_SHORTCUT.unlink()
            print("Desktop icon removed.")
        else:
            print("No Desktop icon to remove.")
        return 0

    if args.enable:
        if not PYTHONW.exists():
            print(f"Cannot find {PYTHONW}. Is the virtualenv set up?", file=sys.stderr)
            return 1
        STARTUP.mkdir(parents=True, exist_ok=True)
        create_shortcut(SHORTCUT)
        print(f"Enabled. climp will start at login via:\n  {SHORTCUT}")
        print("\nIt launches with pythonw.exe, so there is no console window - look for")
        print("the tray icon near the clock.")
        return 0

    if SHORTCUT.exists():
        SHORTCUT.unlink()
        print("Disabled. climp will no longer start at login.")
    else:
        print("Already disabled; nothing to remove.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
