"""Config load/save/update.

The update() test exists because of a real incident: a one-off test run with
--clips-root wrote that temporary path into the config permanently, so the app
would have silently watched an empty folder on the next real start.
"""

from __future__ import annotations

from pathlib import Path

from climp import config as config_module
from climp.config import Config


def test_first_run_creates_a_config_with_a_cutoff(tmp_path: Path):
    path = tmp_path / "config.toml"
    cfg = config_module.load(path)

    assert path.exists()
    assert cfg.backfill_since > 0, "without a cutoff the first run would queue the whole library"


def test_round_trip_preserves_every_field(tmp_path: Path):
    path = tmp_path / "config.toml"
    original = Config(
        clips_root=Path(r"C:\Users\adria\Videos\NVIDIA"),
        drive_folder_id="folder123",
        backfill_since=1_758_000_000.5,
        concurrency=2,
        max_attempts=9,
    )
    config_module.save(original, path)
    loaded = config_module.load(path)

    assert loaded.clips_root == original.clips_root
    assert loaded.drive_folder_id == "folder123"
    assert loaded.backfill_since == original.backfill_since
    assert loaded.concurrency == 2
    assert loaded.max_attempts == 9


def test_windows_paths_survive_the_toml_round_trip(tmp_path: Path):
    """Backslashes are TOML escapes; an unescaped path silently corrupts."""
    path = tmp_path / "config.toml"
    weird = Path(r"C:\Users\adria\Videos\NVIDIA\Marvel Rivals")
    config_module.save(Config(clips_root=weird), path)
    assert config_module.load(path).clips_root == weird


def test_update_starts_from_disk_not_from_a_runtime_override(tmp_path: Path):
    """The regression. A --clips-root override must never be persisted."""
    path = tmp_path / "config.toml"
    on_disk = Config(clips_root=Path(r"C:\Users\adria\Videos\NVIDIA"))
    config_module.save(on_disk, path)

    # What main.py does: runtime config carries a CLI override...
    runtime = Config(clips_root=Path(r"C:\Temp\throwaway"), drive_folder_id=None)
    assert runtime.clips_root != on_disk.clips_root

    # ...and resolving the Drive folder writes only that field back.
    updated = config_module.update(path, drive_folder_id="newfolder")

    assert updated.drive_folder_id == "newfolder"
    assert updated.clips_root == on_disk.clips_root, "the override leaked into the config file"
    assert config_module.load(path).clips_root == on_disk.clips_root


def test_update_leaves_the_cutoff_alone(tmp_path: Path):
    """Resetting backfill_since would re-expose the whole back catalogue."""
    path = tmp_path / "config.toml"
    config_module.save(Config(backfill_since=1_758_000_000.0), path)
    updated = config_module.update(path, drive_folder_id="x")
    assert updated.backfill_since == 1_758_000_000.0
