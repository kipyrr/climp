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

import logging
import os
import subprocess
import threading
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from clipsync.db import CANDIDATE, DONE, FAILED, READY, UPLOADING, Db

log = logging.getLogger(__name__)

REFRESH_SECONDS = 3.0

# Offered in the menu. "Off" plus a handful of round numbers -- a spinner in a
# tray menu is miserable, and these cover the useful range at ~225 MB a clip.
KEEP_CHOICES = (10, 20, 40, 60, 100)

IDLE = (0x4C, 0xAF, 0x50)      # green
BUSY = (0x21, 0x96, 0xF3)      # blue
ATTENTION = (0xF4, 0x43, 0x36)  # red


def _icon_image(colour: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=colour)
    # A play triangle: this uploads recordings.
    d.polygon([(26, 20), (26, 44), (46, 32)], fill=(255, 255, 255, 255))
    return img


def _human(n: int | None) -> str:
    return "unknown" if n is None else f"{n / 1024**3:.1f} GB"


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
    ) -> None:
        self.db = db
        self.stop = stop_event
        self.log_path = log_path
        self.drive_folder_id = drive_folder_id
        self._quota_provider = quota_provider
        self._deferring_provider = deferring_provider or (lambda: False)
        self._retention = retention
        self._deferring = False
        self._counts = dict.fromkeys((CANDIDATE, READY, UPLOADING, DONE, FAILED), 0)
        self._quota: dict | None = None
        self._auth_message: str | None = None
        self.icon = pystray.Icon("clipsync", _icon_image(IDLE), "ClipSync", menu=self._menu())

    # --- what the menu shows ---------------------------------------------

    def _summary(self) -> str:
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
            if self._auth_message:
                yield pystray.MenuItem("!! Sign in again - see the log", None, enabled=False)
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

            if self._retention is not None:
                yield pystray.Menu.SEPARATOR
                yield pystray.MenuItem(self._retention_label(), pystray.Menu(*self._retention_items()))

            yield pystray.Menu.SEPARATOR
            yield pystray.MenuItem("Open Drive folder", self._open_drive)
            yield pystray.MenuItem("Open log file", self._open_log)
            yield pystray.Menu.SEPARATOR
            yield pystray.MenuItem("Quit", self._quit)

        return pystray.Menu(items)

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
        self.icon.stop()

    def auth_needed(self, detail: str) -> None:
        """Called by the upload worker when a human has to sign in (D13)."""
        self._auth_message = detail
        self.refresh()
        try:
            self.icon.notify("ClipSync needs you to sign in to Google again", "Sign in required")
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

        if self._counts[FAILED] or self._auth_message:
            colour = ATTENTION
        elif self._counts[UPLOADING] or self._counts[READY] or self._counts[CANDIDATE]:
            colour = BUSY
        else:
            colour = IDLE

        self.icon.icon = _icon_image(colour)
        self.icon.title = f"ClipSync - {self._summary()}"
        try:
            self.icon.update_menu()
        except Exception:
            log.debug("menu update failed", exc_info=True)

    def _refresh_loop(self) -> None:
        while not self.stop.is_set():
            self.refresh()
            self.stop.wait(REFRESH_SECONDS)
        self.icon.stop()

    def run(self) -> None:
        """Blocks on the main thread until Quit or the stop event."""
        threading.Thread(target=self._refresh_loop, name="tray-refresh", daemon=True).start()
        self.icon.run()
