"""Is the user doing something that an upload would spoil?

Roadblock 4: a 400 MB upload saturating a home connection adds latency to the
game the app exists to record. The fix the blueprint chose is to defer, and it
placed that as a condition on the upload worker's claim rather than as a new
component. This module answers the question; it decides nothing.

Holds no state and reads no rows, like drive.py. Windows-only by nature, and
returns False everywhere else so the rest of the app needs no branches.
"""

from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger(__name__)

# SHQueryUserNotificationState, the purpose-built "should I interrupt?" API.
QUNS_NOT_PRESENT = 1
QUNS_BUSY = 2                    # a fullscreen application is running
QUNS_RUNNING_D3D_FULL_SCREEN = 3  # a fullscreen Direct3D game
QUNS_PRESENTATION_MODE = 4
QUNS_ACCEPTS_NOTIFICATIONS = 5   # normal desktop use
QUNS_QUIET_TIME = 6
QUNS_APP = 7                     # a fullscreen store app

BUSY_STATES = {QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE, QUNS_APP}

STATE_NAMES = {
    QUNS_NOT_PRESENT: "screen off or locked",
    QUNS_BUSY: "fullscreen app",
    QUNS_RUNNING_D3D_FULL_SCREEN: "fullscreen game",
    QUNS_PRESENTATION_MODE: "presentation mode",
    QUNS_ACCEPTS_NOTIFICATIONS: "normal desktop",
    QUNS_QUIET_TIME: "quiet hours",
    QUNS_APP: "fullscreen app",
}


def notification_state() -> int | None:
    """Raw Windows answer, or None if it cannot be asked."""
    if sys.platform != "win32":
        return None
    try:
        state = ctypes.c_int()
        # S_OK is 0; anything else means the answer is unusable.
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return None
        return state.value
    except Exception:
        log.debug("SHQueryUserNotificationState unavailable", exc_info=True)
        return None


def is_busy() -> bool:
    """True when a fullscreen app or game has the screen.

    Deliberately conservative: if the state cannot be determined, the answer is
    False. A missed deferral costs some latency once; a stuck False would stop
    uploading forever, which is worse and much harder to notice.
    """
    state = notification_state()
    if state is None:
        return False
    return state in BUSY_STATES


def describe() -> str:
    state = notification_state()
    if state is None:
        return "unknown"
    return STATE_NAMES.get(state, f"state {state}")
