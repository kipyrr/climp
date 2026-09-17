"""Choosing the clips source folder from the tray popup.

The thing that matters here is not the menu -- it is that picking a folder
actually repoints the workflow at it, and does so in the order that keeps the
startup invariant true: scan to completion, then arm the watcher.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from climp import config as config_module
from climp.config import Config
from climp.db import CANDIDATE, Db
from climp.main import Application, SourceControl
from climp.tray import Tray


@pytest.fixture
def app_dir(tmp_path: Path) -> Path:
    d = tmp_path / "appdata"
    d.mkdir()
    return d


@pytest.fixture
def folder_a(tmp_path: Path) -> Path:
    d = tmp_path / "VideosA" / "Game"
    d.mkdir(parents=True)
    (d / "one.mp4").write_bytes(b"x" * 16)
    return d.parent


@pytest.fixture
def folder_b(tmp_path: Path) -> Path:
    d = tmp_path / "VideosB" / "Other Game"
    d.mkdir(parents=True)
    (d / "two.mp4").write_bytes(b"y" * 16)
    (d / "three.mp4").write_bytes(b"z" * 16)
    return d.parent


@pytest.fixture
def app(app_dir: Path, folder_a: Path) -> Application:
    cfg = Config(clips_root=folder_a, app_dir=app_dir, backfill_since=0.0)
    config_module.save(cfg, app_dir / "config.toml")
    a = Application(cfg)
    a.db = Db(tmp_db(app_dir))
    a.db.init_schema()
    a.source_control = SourceControl(a)
    return a


def tmp_db(app_dir: Path) -> Path:
    return app_dir / "clips.db"


# --- reading the current source ------------------------------------------


def test_reports_the_configured_folder(app: Application, folder_a: Path):
    assert app.source_control.clips_root == folder_a
    assert app.source_control.exists() is True


def test_counts_the_clips_in_it(app: Application, folder_b: Path):
    assert app.source_control.clip_count() == 1
    app.change_clips_root(folder_b)
    assert app.source_control.clip_count() == 2


def test_a_deleted_folder_is_reported_rather_than_crashing(app: Application, tmp_path: Path):
    app.cfg = app.cfg.__class__(**{**app.cfg.__dict__, "clips_root": tmp_path / "gone"})
    assert app.source_control.exists() is False
    assert app.source_control.clip_count() == 0


# --- changing it ----------------------------------------------------------


def test_changing_the_folder_updates_the_config(app: Application, folder_b: Path, app_dir: Path):
    assert app.change_clips_root(folder_b) is True

    assert app.cfg.clips_root == folder_b
    assert config_module.load(app_dir / "config.toml").clips_root == folder_b, (
        "the choice has to survive a restart"
    )


def test_the_new_folder_is_actually_scanned(app: Application, folder_b: Path):
    """The point of the feature: the clips in the new folder enter the queue."""
    app.change_clips_root(folder_b)

    queued = sorted(c.path.name for c in app.db.by_state(CANDIDATE))
    assert queued == ["three.mp4", "two.mp4"]


def test_the_watcher_ends_up_on_the_new_folder(app: Application, folder_b: Path):
    app.change_clips_root(folder_b)
    try:
        assert app.watcher is not None
        assert app.watcher.clips_root == folder_b
    finally:
        if app.watcher:
            app.watcher.stop()


def test_choosing_the_same_folder_is_a_no_op(app: Application, folder_a: Path):
    assert app.change_clips_root(folder_a) is False
    assert app.watcher is None, "nothing should have been restarted"


def test_a_different_spelling_of_the_same_folder_is_still_a_no_op(app: Application, folder_a: Path):
    assert app.change_clips_root(Path(str(folder_a).upper())) is False


def test_a_path_that_is_not_a_folder_is_refused(app: Application, tmp_path: Path, folder_a: Path):
    missing = tmp_path / "does-not-exist"
    assert app.change_clips_root(missing) is False
    assert app.cfg.clips_root == folder_a, "the working config must not be damaged"


def test_changing_the_folder_leaves_other_settings_alone(app: Application, folder_b: Path, app_dir: Path):
    before = config_module.load(app_dir / "config.toml")
    app.change_clips_root(folder_b)
    after = config_module.load(app_dir / "config.toml")

    assert after.backfill_since == before.backfill_since, "the cutoff must not move"
    assert after.drive_folder_id == before.drive_folder_id
    assert after.retention_keep_newest == before.retention_keep_newest


def test_already_queued_clips_survive_the_switch(app: Application, folder_a: Path, folder_b: Path):
    app.db.insert_candidate(folder_a / "Game" / "one.mp4")
    before = len(app.db.by_state(CANDIDATE))

    app.change_clips_root(folder_b)

    assert len(app.db.by_state(CANDIDATE)) == before + 2, "switching must not discard pending work"


# --- the menu -------------------------------------------------------------


@pytest.fixture
def tray(app: Application) -> Tray:
    return Tray(app.db, threading.Event(), source=app.source_control)


def test_menu_names_the_current_folder(tray: Tray, folder_a: Path):
    assert folder_a.name in tray._source_label()


def test_menu_shows_the_full_path_not_just_the_name(tray: Tray, folder_a: Path):
    labels = [i.text for i in tray._source_items() if getattr(i, "text", None)]
    assert str(folder_a) in labels, "two drives can both have a Videos folder"


def test_menu_shows_how_many_clips_are_there(tray: Tray):
    labels = " ".join(i.text for i in tray._source_items() if getattr(i, "text", None))
    assert "1 .mp4" in labels


def test_menu_warns_when_the_folder_has_gone(tray: Tray, app: Application, tmp_path: Path):
    app.cfg = app.cfg.__class__(**{**app.cfg.__dict__, "clips_root": tmp_path / "gone"})
    labels = " ".join(i.text for i in tray._source_items() if getattr(i, "text", None))
    assert "no longer exists" in labels


def test_menu_offers_a_way_to_change_it(tray: Tray):
    labels = " ".join(i.text for i in tray._source_items() if getattr(i, "text", None))
    assert "Choose a different folder" in labels


def test_picking_a_folder_applies_it(tray: Tray, app: Application, folder_b: Path, monkeypatch):
    monkeypatch.setattr("climp.tray.ask_for_folder", lambda initial=None: str(folder_b))

    tray._choose_source_blocking()

    assert app.cfg.clips_root == folder_b
    if app.watcher:
        app.watcher.stop()


def test_cancelling_the_picker_changes_nothing(tray: Tray, app: Application, folder_a: Path, monkeypatch):
    monkeypatch.setattr("climp.tray.ask_for_folder", lambda initial=None: None)

    tray._choose_source_blocking()

    assert app.cfg.clips_root == folder_a
    assert app.watcher is None


def test_a_failure_while_switching_does_not_take_the_tray_down(tray: Tray, monkeypatch):
    monkeypatch.setattr("climp.tray.ask_for_folder", lambda initial=None: "C:\\nope")

    def boom(_path):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(tray._source, "set_clips_root", boom)
    tray._choose_source_blocking()  # must not raise
