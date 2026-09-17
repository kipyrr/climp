"""Only one copy of climp may run at a time.

Written after a real incident: the app started fine from the Desktop shortcut,
but the tray icon was hidden in the Windows overflow area, so it looked like
nothing had happened. Double-clicking again silently started another copy.
Four processes ended up running, all watching one folder and sharing one
database.

Nothing corrupts -- the row claim is atomic -- but it wastes bandwidth and is
very hard to diagnose, and a second click that does nothing at all is
indistinguishable from the app being broken.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from climp.main import SINGLE_INSTANCE_MUTEX, acquire_single_instance

# Takes the mutex name as an argument. The tests must never use the real one:
# otherwise they fail whenever the actual app happens to be running, which is
# a property of the machine rather than of the code.
PROBE = (
    "from climp.main import acquire_single_instance as a;"
    "import sys,time;"
    "h=a(sys.argv[1]);"
    "sys.stdout.write(('LOCK' if h else 'BLOCKED')+chr(10));"
    "sys.stdout.flush();"
    "time.sleep(float(sys.argv[2]))"
)


def _python() -> str:
    return sys.executable


def _name(suffix: str) -> str:
    """A mutex name unique to this test run and this test."""
    return rf"Local\climp-test-{os.getpid()}-{suffix}"


def test_the_first_caller_gets_the_lock():
    handle = acquire_single_instance(_name("first"))
    assert handle is not None


def test_the_mutex_is_session_scoped():
    r"""Local\ rather than Global\: another Windows user's copy is their own."""
    assert SINGLE_INSTANCE_MUTEX.startswith("Local")


def test_a_second_process_is_blocked_while_the_first_lives():
    repo = Path(__file__).resolve().parents[1]

    first = subprocess.Popen(
        [_python(), "-c", PROBE, _name("blocked"), "6"], cwd=repo, stdout=subprocess.PIPE, text=True
    )
    try:
        assert first.stdout.readline().strip() == "LOCK"

        second = subprocess.run(
            [_python(), "-c", PROBE, _name("blocked"), "0"], cwd=repo, capture_output=True, text=True, timeout=30
        )
        assert second.stdout.strip() == "BLOCKED", second.stderr[-300:]
    finally:
        first.kill()
        first.wait(timeout=20)


def test_the_lock_is_released_when_the_holder_exits():
    """Otherwise a crash would lock the user out until they rebooted."""
    repo = Path(__file__).resolve().parents[1]

    name = _name("released")
    first = subprocess.run(
        [_python(), "-c", PROBE, name, "0"], cwd=repo, capture_output=True, text=True, timeout=30
    )
    assert first.stdout.strip() == "LOCK"

    second = subprocess.run(
        [_python(), "-c", PROBE, name, "0"], cwd=repo, capture_output=True, text=True, timeout=30
    )
    assert second.stdout.strip() == "LOCK", "the lock outlived its process"


def test_a_killed_holder_does_not_lock_the_app_out():
    """A hard kill is the realistic failure, not a clean exit."""
    repo = Path(__file__).resolve().parents[1]

    name = _name("killed")
    first = subprocess.Popen(
        [_python(), "-c", PROBE, name, "30"], cwd=repo, stdout=subprocess.PIPE, text=True
    )
    assert first.stdout.readline().strip() == "LOCK"
    first.kill()
    first.wait(timeout=20)

    after = subprocess.run(
        [_python(), "-c", PROBE, name, "0"], cwd=repo, capture_output=True, text=True, timeout=30
    )
    assert after.stdout.strip() == "LOCK", "a killed process left the mutex held"
