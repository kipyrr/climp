"""The launcher the shortcuts run.

Written after a failure that took three rounds to find. Double-clicking the
Desktop shortcut did nothing whatsoever: no window, no tray icon, no log line,
no error dialog. The shortcut, its target, its arguments and its working
directory were all correct when inspected.

The cause was that `pythonw.exe -m climp.main` only works when the process
starts in the repo directory, because -m resolves the package against the
current working directory. Explorer did not give it that directory, so Python
exited with ModuleNotFoundError -- into a process with no console, before
logging existed. Perfectly silent.

`--help` is used throughout: it exercises the whole import and startup path and
then exits, without authenticating or touching Drive.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "climp_launcher.pyw"


@pytest.fixture
def elsewhere(tmp_path: Path) -> Path:
    """Any directory that is not the repo."""
    return tmp_path


def test_the_launcher_exists_where_the_shortcuts_expect_it():
    assert LAUNCHER.is_file(), "tools/autostart.py points every shortcut at this file"


def test_the_old_form_really_does_fail_from_another_directory(elsewhere: Path):
    """The regression itself. If this ever passes, the bug is back in scope."""
    result = subprocess.run(
        [sys.executable, "-m", "climp.main", "--help"],
        cwd=elsewhere, capture_output=True, text=True, timeout=60,
    )
    assert "No module named 'climp'" in (result.stderr + result.stdout), (
        "expected -m to fail outside the repo; if it now succeeds, "
        "something else is putting the repo on sys.path"
    )


def test_the_launcher_works_from_another_directory(elsewhere: Path):
    """The fix: it sets sys.path from its own location, not from the cwd."""
    result = subprocess.run(
        [sys.executable, str(LAUNCHER), "--help"],
        cwd=elsewhere, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr[-500:]
    assert "climp" in result.stdout.lower()


def test_the_launcher_works_from_the_repo_too(elsewhere: Path):
    result = subprocess.run(
        [sys.executable, str(LAUNCHER), "--help"],
        cwd=REPO, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr[-500:]


def test_it_records_every_launch_before_anything_can_fail(elsewhere: Path, tmp_path: Path):
    """launch.log is written first, so even an instant death leaves a trace."""
    fake_appdata = tmp_path / "appdata"
    env = dict(os.environ, LOCALAPPDATA=str(fake_appdata))

    subprocess.run(
        [sys.executable, str(LAUNCHER), "--help"],
        cwd=elsewhere, capture_output=True, text=True, timeout=60, env=env,
    )

    log = fake_appdata / "climp" / "logs" / "launch.log"
    assert log.is_file(), "a launch that leaves no trace is undiagnosable"
    body = log.read_text(encoding="utf-8")
    assert "launch:" in body
    assert "cwd=" in body, "the working directory is the thing that broke; record it"


def test_an_import_failure_is_reported_not_swallowed(tmp_path: Path):
    """The whole point: no silent deaths.

    climp is made unimportable by pointing the launcher at a copy of itself in
    a directory with no package beside it.
    """
    fake_appdata = tmp_path / "appdata"
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    copy = lonely / "climp_launcher.pyw"
    copy.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")

    # CLIMP_NO_DIALOG: the error box is modal and would hang this test forever.
    env = dict(os.environ, LOCALAPPDATA=str(fake_appdata), PYTHONPATH="", CLIMP_NO_DIALOG="1")
    result = subprocess.run(
        [sys.executable, str(copy), "--help"],
        cwd=lonely, capture_output=True, text=True, timeout=60, env=env,
    )

    assert result.returncode == 1, "a failed launch must not report success"
    log = (fake_appdata / "climp" / "logs" / "launch.log").read_text(encoding="utf-8")
    assert "IMPORT FAILED" in log
    assert "ModuleNotFoundError" in log, "the log has to name the actual cause"
    assert "dialog suppressed" in log, "the user would have been shown this"
