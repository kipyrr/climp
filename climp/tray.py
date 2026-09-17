"""Tray UI. A view over the queue, and deliberately dumb.

Reads counts. Writes exactly one thing: a retry reset on a failed row. It never
uploads, never deletes, never touches the filesystem. If it ever needs to know
something, the answer is a query against the state table, not a new channel
between components.

pystray wants the main thread on Windows, so main.py runs this last and blocks
on it while every other loop runs as a daemon thread.

It does write one more thing than the blueprint allowed: retention settings
(D19). That is a config write, not a state write -- the rule that the tray
never moves a clip between states still holds exactly.

See IMPLEMENTATION-PLAN.md section 2.9.
"""

from __future__ import annotations

import datetime
import logging
import math
import os
import subprocess
import threading
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from climp.db import CANDIDATE, DONE, FAILED, READY, UPLOADING, Db

log = logging.getLogger(__name__)

REFRESH_SECONDS = 3.0

# Offered in the menu. "Off" plus a handful of round numbers -- a spinner in a
# tray menu is miserable, and these cover the useful range at ~225 MB a clip.
KEEP_CHOICES = (10, 20, 40, 60, 100)

RED = (255, 0, 0)

# One full red -> black -> red cycle. Matched to REFRESH_SECONDS because that
# was the only cadence the icon previously had: before this, the icon did not
# animate at all, it swapped between three flat colours on each refresh.
ANIMATION_PERIOD_SECONDS = REFRESH_SECONDS
FRAME_SECONDS = 0.1
FRAME_COUNT = max(2, round(ANIMATION_PERIOD_SECONDS / FRAME_SECONDS))


def _icon_image(brightness: float = 1.0) -> Image.Image:
    """A solid circle. brightness 1.0 is red, 0.0 is black."""
    b = min(max(brightness, 0.0), 1.0)
    fill = (round(RED[0] * b), round(RED[1] * b), round(RED[2] * b), 255)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((4, 4, 60, 60), fill=fill)
    return img


def _build_frames() -> list[Image.Image]:
    """Pre-render the pulse once, so the animation loop only swaps images.

    Cosine rather than a triangle wave: a linear ramp reverses direction
    abruptly at both ends, which reads as a flinch. Cosine eases through red
    and through black, so there is no moment where the rate of change jumps.
    """
    frames = []
    for i in range(FRAME_COUNT):
        phase = 2 * math.pi * i / FRAME_COUNT
        frames.append(_icon_image((math.cos(phase) + 1) / 2))
    return frames


_FRAMES: list[Image.Image] | None = None


def _frames() -> list[Image.Image]:
    global _FRAMES
    if _FRAMES is None:
        _FRAMES = _build_frames()
    return _FRAMES


def _build_marker() -> str:
    """The build constant compiled into the loaded code."""
    try:
        from climp import BUILD

        return f"build {BUILD[-5:]}"
    except Exception:
        return "build ?"


def _wrap(text: str, width: int) -> list[str]:
    """Break a message across menu lines without losing the end of it."""
    import textwrap

    return textwrap.wrap(text, width=width, break_long_words=True) or [text]


def _human(n: int | None) -> str:
    return "unknown" if n is None else f"{n / 1024**3:.1f} GB"


def ask_for_folder(initial: str | None = None) -> str | None:
    """Show a folder picker. Returns the chosen path, or None if cancelled.

    tkinter rather than a native shell dialog: it ships with Python, needs no
    extra dependency, and the root window is created and destroyed here so
    nothing lingers.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        log.error("tkinter is unavailable, so the folder picker cannot open")
        return None

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        chosen = filedialog.askdirectory(
            title="Choose the folder climp should watch for clips",
            initialdir=initial or "",
            mustexist=True,
        )
    finally:
        root.destroy()

    return chosen or None


class Tray:
    def __init__(
        self,
        db: Db,
        stop_event: threading.Event,
        log_path: Path | None = None,
        drive_folder_id: str | None = None,
        quota_provider=None,
        deferring_provider=None,
        retention=None,
        source=None,
        auth=None,
    ) -> None:
        self.db = db
        self.stop = stop_event
        self.log_path = log_path
        self.drive_folder_id = drive_folder_id
        self._quota_provider = quota_provider
        self._deferring_provider = deferring_provider or (lambda: False)
        self._retention = retention
        self._source = source
        self._auth = auth
        self._menu_state: tuple | None = None
        # Shown in the menu. Several confusing sessions came from looking at
        # an instance started before a fix landed; this makes that visible.
        self.started_at = datetime.datetime.now()
        self.build = _build_marker()
        self._deferring = False
        self._counts = dict.fromkeys((CANDIDATE, READY, UPLOADING, DONE, FAILED), 0)
        self._quota: dict | None = None
        self._auth_message: str | None = None
        self._animating = False

        # The image the icon should be showing. Kept separately so the tray's
        # visual state can be read without a live Win32 window -- pystray
        # registers a window class per Icon, so constructing one per test
        # eventually fails with "class already exists".
        self.current_image = _icon_image(1.0)
        self.current_title = "climp"
        self.icon: pystray.Icon | None = None

    # --- what the menu shows ---------------------------------------------

    def _auth_problem(self) -> str | None:
        if self._auth_message:
            return self._auth_message
        if self._auth is not None and getattr(self._auth, "auth_problem", None):
            return self._auth.auth_problem
        return None

    def _summary(self) -> str:
        if self._auth_problem():
            # Takes priority over everything: nothing uploads until it is fixed,
            # and clips silently piling up looks like the app working.
            return "Not signed in to Google - clips are waiting"

        c = self._counts
        pending = c[CANDIDATE] + c[READY] + c[UPLOADING]
        if c[FAILED]:
            return f"{c[FAILED]} failed, {c[DONE]} uploaded"
        if self._deferring and pending:
            # Say why, or this reads as the app being broken mid-game.
            return f"{pending} waiting - paused while you play"
        if pending:
            return f"{pending} in progress, {c[DONE]} uploaded"
        return f"{c[DONE]} uploaded, idle"

    def _quota_text(self) -> str:
        if not self._quota:
            return "Drive: checking..."
        free, limit = self._quota.get("free"), self._quota.get("limit")
        if free is None:
            return "Drive: no stated limit"
        clips = int(free // (225 * 1024 * 1024))
        return f"Drive: {_human(free)} free of {_human(limit)}  (~{clips} clips)"

    def _failure_items(self):
        items = []
        for clip in self.db.recent_failures(limit=8):
            error = (clip.last_error or "unknown error").split("\n")[0][:60]
            items.append(
                pystray.MenuItem(
                    f"Retry: {clip.path.name[:40]}  -  {error}",
                    self._make_retry(clip.id),
                )
            )
        return items

    def _make_retry(self, clip_id: int):
        def action(_icon=None, _item=None):
            # D4: back to candidate, so a settle-timeout row is re-verified
            # before it can upload, and the session URI is preserved.
            self.db.request_retry(clip_id)
            log.info("retry requested for clip %d", clip_id)
            self.refresh()

        return action

    def _menu(self) -> pystray.Menu:
        def items():
            yield pystray.MenuItem(self._summary(), None, enabled=False)
            yield pystray.MenuItem(
                f"running since {self.started_at:%H:%M:%S}  ({self.build})", None, enabled=False
            )
            problem = self._auth_problem()
            if problem:
                # Wrapped rather than truncated: the useful part of these
                # messages is usually the path at the end, and cutting at a
                # fixed width removed precisely that.
                for line in _wrap(problem, 60):
                    yield pystray.MenuItem(line, None, enabled=False)
                if self._auth is not None:
                    yield pystray.MenuItem("Sign in to Google...", self._sign_in)
            else:
                yield pystray.MenuItem("Signed in to Google", None, enabled=False)
            yield pystray.MenuItem(self._quota_text(), None, enabled=False)
            yield pystray.Menu.SEPARATOR

            c = self._counts
            yield pystray.MenuItem(
                f"waiting {c[CANDIDATE]}   ready {c[READY]}   uploading {c[UPLOADING]}",
                None, enabled=False,
            )

            failures = self._failure_items()
            if failures:
                yield pystray.Menu.SEPARATOR
                yield pystray.MenuItem("Failed clips", pystray.Menu(*failures))

            if self._source is not None:
                yield pystray.Menu.SEPARATOR
                yield pystray.MenuItem(self._source_label(), pystray.Menu(*self._source_items()))

            if self._retention is not None:
                if self._source is None:
                    yield pystray.Menu.SEPARATOR
                yield pystray.MenuItem(self._retention_label(), pystray.Menu(*self._retention_items()))

            yield pystray.Menu.SEPARATOR
            yield pystray.MenuItem("Open Drive folder", self._open_drive)
            yield pystray.MenuItem("Open log file", self._open_log)
            yield pystray.Menu.SEPARATOR
            yield pystray.MenuItem("Quit", self._quit)

        return pystray.Menu(items)

    def _sign_in(self, _icon=None, _item=None) -> None:
        """Opens a browser and blocks, so it cannot run on the menu's thread."""
        threading.Thread(target=self._sign_in_blocking, name="sign-in", daemon=True).start()

    def _sign_in_blocking(self) -> None:
        try:
            if self._auth.sign_in():
                self._auth_message = None
                log.info("signed in; uploads can resume")
            else:
                log.warning("sign-in did not complete")
        except Exception:
            log.exception("sign-in failed")
        self.refresh()

    # --- clips source folder ----------------------------------------------

    def _source_label(self) -> str:
        return f"Clips from: {self._source.clips_root.name}"

    def _source_items(self):
        src = self._source
        root = src.clips_root

        # Full path on its own line: the folder name alone is ambiguous when
        # two drives both have a Videos folder.
        yield pystray.MenuItem(str(root), None, enabled=False)
        if src.exists():
            yield pystray.MenuItem(f"{src.clip_count()} .mp4 files in it", None, enabled=False)
        else:
            yield pystray.MenuItem("!! this folder no longer exists", None, enabled=False)

        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Choose a different folder...", self._choose_source)
        yield pystray.MenuItem("Open this folder", self._open_source)

    def _choose_source(self, _icon=None, _item=None) -> None:
        """Ask for a folder, then repoint the watcher at it.

        Runs on its own thread: the dialog blocks, and blocking a pystray menu
        callback freezes the whole tray icon until it closes.
        """
        threading.Thread(target=self._choose_source_blocking, name="pick-folder", daemon=True).start()

    def _choose_source_blocking(self) -> None:
        chosen = ask_for_folder(str(self._source.clips_root))
        if not chosen:
            return
        try:
            if self._source.set_clips_root(Path(chosen)):
                log.info("clips folder set from the tray: %s", chosen)
            else:
                log.info("clips folder unchanged")
        except Exception:
            log.exception("could not change the clips folder")
        self.refresh()

    def _open_source(self, _icon=None, _item=None) -> None:
        root = self._source.clips_root
        if root.is_dir():
            os.startfile(root)  # noqa: S606 - the user's own folder

    # --- retention (D18/D19) ----------------------------------------------

    def _retention_label(self) -> str:
        r = self._retention
        if not r.enabled:
            return "Keep in Drive: everything"
        return f"Keep in Drive: newest {r.keep_newest}"

    def _retention_items(self):
        r = self._retention
        in_drive = r.in_drive_count()

        yield pystray.MenuItem(f"{in_drive} clips in Drive now", None, enabled=False)
        yield pystray.Menu.SEPARATOR

        yield pystray.MenuItem(
            "Keep everything (no deleting)",
            self._set_keep(None),
            checked=lambda _item: not self._retention.enabled,
            radio=True,
        )
        for n in KEEP_CHOICES:
            gb = n * 225 / 1024
            yield pystray.MenuItem(
                f"Keep newest {n}  (~{gb:.1f} GB)",
                self._set_keep(n),
                checked=self._is_keep(n),
                radio=True,
            )

        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem(
            f"Never delete anything under {r.min_age_hours:.0f}h old", None, enabled=False
        )
        if r.enabled:
            yield pystray.MenuItem("Tidy up now", self._run_retention)

    def _is_keep(self, n: int):
        def checked(_item) -> bool:
            return self._retention.enabled and self._retention.keep_newest == n

        return checked

    def _set_keep(self, n: int | None):
        def action(_icon=None, _item=None):
            if n is None:
                self._retention.set_enabled(False)
                log.info("retention turned off from the tray")
            else:
                self._retention.set_keep(n)
                log.info("retention set to keep the newest %d clips", n)
            self.refresh()

        return action

    def _run_retention(self, _icon=None, _item=None) -> None:
        self._retention.run_now()
        log.info("retention sweep requested from the tray")

    # --- actions ----------------------------------------------------------

    def _open_drive(self, _icon=None, _item=None) -> None:
        if self.drive_folder_id:
            webbrowser.open(f"https://drive.google.com/drive/folders/{self.drive_folder_id}")

    def _open_log(self, _icon=None, _item=None) -> None:
        if self.log_path and self.log_path.exists():
            os.startfile(self.log_path)  # noqa: S606 - opening the user's own log
        elif self.log_path:
            subprocess.Popen(["explorer", str(self.log_path.parent)])

    def _quit(self, _icon=None, _item=None) -> None:
        log.info("quit requested from the tray")
        self.stop.set()
        if self.icon is not None:
            self.icon.stop()

    def auth_needed(self, detail: str) -> None:
        """Called by the upload worker when a human has to sign in (D13)."""
        self._auth_message = detail
        self.refresh()
        if self.icon is None:
            return
        try:
            self.icon.notify("climp needs you to sign in to Google again", "Sign in required")
        except Exception:
            log.debug("tray notification unavailable", exc_info=True)

    # --- refresh ----------------------------------------------------------

    def refresh(self) -> None:
        try:
            self._counts = self.db.counts_by_state()
        except Exception:
            log.debug("count query failed", exc_info=True)
            return

        try:
            self._deferring = bool(self._deferring_provider())
        except Exception:
            log.debug("defer state unavailable", exc_info=True)

        if self._quota_provider is not None:
            try:
                self._quota = self._quota_provider()
            except Exception:
                log.debug("quota read failed", exc_info=True)

        # Animate only while work can actually progress. Clips queued behind a
        # pause -- a fullscreen game, or a broken sign-in -- are not an
        # operation running, and pulsing for them both misleads and costs a
        # tray icon update ten times a second for as long as the pause lasts.
        work_pending = bool(
            self._counts[UPLOADING] or self._counts[READY] or self._counts[CANDIDATE]
        )
        self._animating = work_pending and not self._deferring and not self._auth_problem()
        if not self._animating:
            self._show(_icon_image(1.0))

        self.current_title = f"climp - {self._summary()}"
        if self.icon is None:
            return

        self.icon.title = self.current_title

        # Rebuilding the menu is not free: it queries failures, reads retention
        # settings and counts files in the clips folder. Doing that every few
        # seconds when nothing has changed made the menu visibly lag.
        state = self._menu_signature()
        if state == self._menu_state:
            return
        self._menu_state = state
        try:
            self.icon.update_menu()
        except Exception:
            log.debug("menu update failed", exc_info=True)

    def _menu_signature(self) -> tuple:
        """Everything the menu renders. Equal signature means equal menu."""
        retention = None
        if self._retention is not None:
            retention = (self._retention.enabled, self._retention.keep_newest)
        source = None
        if self._source is not None:
            source = str(self._source.clips_root)
        return (
            tuple(sorted(self._counts.items())),
            self._quota_text(),
            self._auth_problem(),
            retention,
            source,
            self._deferring,
        )

    def _show(self, image) -> None:
        self.current_image = image
        if self.icon is not None:
            self.icon.icon = image

    def _refresh_loop(self) -> None:
        while not self.stop.is_set():
            self.refresh()
            self.stop.wait(REFRESH_SECONDS)
        if self.icon is not None:
            self.icon.stop()

    def _animate_loop(self) -> None:
        """Pulse red to black and back while there is work in the queue.

        Separate from the refresh loop on purpose: refreshing queries the
        database and rebuilds the menu, which is far too expensive to do at
        frame rate. This only swaps a pre-rendered image.
        """
        frames = _frames()
        i = 0
        was_animating = False

        while not self.stop.is_set():
            if self._animating:
                self._show(frames[i % len(frames)])
                i += 1
                was_animating = True
            elif was_animating:
                # Work finished: settle back on solid red, at full brightness
                # rather than wherever the fade happened to be.
                self._show(_icon_image(1.0))
                was_animating = False
                i = 0
            self.stop.wait(FRAME_SECONDS)

    def run(self) -> None:
        """Blocks on the main thread until Quit or the stop event.

        The pystray Icon is built here rather than in __init__, because
        creating one registers a Win32 window class and there should only ever
        be one of those per process.
        """
        self.icon = pystray.Icon("climp", self.current_image, self.current_title, menu=self._menu())
        threading.Thread(target=self._refresh_loop, name="tray-refresh", daemon=True).start()
        threading.Thread(target=self._animate_loop, name="tray-animate", daemon=True).start()
        self.icon.run()
