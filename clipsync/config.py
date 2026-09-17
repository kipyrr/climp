"""Configuration. One file, one dataclass, no reads scattered through the app.

Lives in %LOCALAPPDATA%\\ClipSync alongside the database and the token (D12) --
outside any repo, and outside a PyInstaller bundle, which is the blueprint's
one stated constraint on the token path.
"""

from __future__ import annotations

import os
import time
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ClipSync"
CONFIG_PATH = APP_DIR / "config.toml"

DEFAULT_CLIPS_ROOT = Path(os.environ.get("USERPROFILE", Path.home())) / "Videos" / "NVIDIA"
DEFAULT_FOLDER_NAME = "Game Clips"


@dataclass(frozen=True)
class Config:
    clips_root: Path = DEFAULT_CLIPS_ROOT
    drive_folder_id: str | None = None
    drive_folder_name: str = DEFAULT_FOLDER_NAME

    # D14: files older than this are never enqueued. Written once on first run.
    backfill_since: float = field(default_factory=time.time)

    concurrency: int = 1              # D10
    max_attempts: int = 6             # D8
    settle_timeout_seconds: float = 30 * 60   # D6
    stale_upload_seconds: float = 600         # roadblock 7

    app_dir: Path = APP_DIR

    @property
    def db_path(self) -> Path:
        return self.app_dir / "clips.db"

    @property
    def token_path(self) -> Path:
        return self.app_dir / "token.json"

    @property
    def client_secret_path(self) -> Path:
        return self.app_dir / "client_secret.json"


def load(path: Path = CONFIG_PATH) -> Config:
    """Read config, creating it with defaults on first run.

    Writing backfill_since on first run is what pins the cutoff to "the moment
    ClipSync was first started here", so the existing library is never queued.
    """
    if not path.exists():
        cfg = Config()
        save(cfg, path)
        return cfg

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    return Config(
        clips_root=Path(raw.get("clips_root", DEFAULT_CLIPS_ROOT)),
        drive_folder_id=raw.get("drive_folder_id") or None,
        drive_folder_name=raw.get("drive_folder_name", DEFAULT_FOLDER_NAME),
        backfill_since=float(raw.get("backfill_since", time.time())),
        concurrency=int(raw.get("concurrency", 1)),
        max_attempts=int(raw.get("max_attempts", 6)),
        settle_timeout_seconds=float(raw.get("settle_timeout_seconds", 30 * 60)),
        stale_upload_seconds=float(raw.get("stale_upload_seconds", 600)),
        app_dir=Path(raw.get("app_dir", APP_DIR)),
    )


def update(path: Path = CONFIG_PATH, **changes) -> Config:
    """Apply changes to the config file, starting from what is on disk.

    Deliberately does NOT take the running Config. A runtime override such as
    a --clips-root flag lives only in memory, and writing it back would make a
    one-off test run permanently repoint the app. That happened once; this
    function exists so it cannot happen again.
    """
    updated = replace(load(path), **changes)
    save(updated, path)
    return updated


def save(cfg: Config, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f'''# ClipSync configuration.
# Regenerated only if deleted. See IMPLEMENTATION-PLAN.md section 7.

clips_root = {_toml_str(cfg.clips_root)}
drive_folder_id = {_toml_str(cfg.drive_folder_id or "")}
drive_folder_name = {_toml_str(cfg.drive_folder_name)}

# Clips older than this are never enqueued (D14). Lower it to pull in older
# clips; the next startup scan will find them.
backfill_since = {cfg.backfill_since!r}

concurrency = {cfg.concurrency}
max_attempts = {cfg.max_attempts}
settle_timeout_seconds = {cfg.settle_timeout_seconds!r}
stale_upload_seconds = {cfg.stale_upload_seconds!r}
'''
    path.write_text(body, encoding="utf-8")


def _toml_str(value) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
