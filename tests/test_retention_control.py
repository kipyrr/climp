"""Changing retention from the tray.

The point of these tests is that a setting changed in the menu survives a
restart AND takes effect without one, and that the tray still cannot delete
anything itself.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from climp import config as config_module
from climp.db import CANDIDATE, DONE, Db
from climp.main import RetentionControl
from climp.tray import KEEP_CHOICES, Tray


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    config_module.save(config_module.Config(), path)
    return path


@pytest.fixture
def db(tmp_path: Path) -> Db:
    d = Db(tmp_path / "clips.db")
    d.init_schema()
    return d


@pytest.fixture
def control(db: Db, config_path: Path) -> RetentionControl:
    return RetentionControl(db, config_path, config_module.load(config_path))


def uploaded(db: Db, name: str) -> int:
    path = "C:\\clips\\G\\" + name + ".mp4"
    db.insert_candidate(path)
    clip = [c for c in db.by_state(CANDIDATE) if str(c.path) == path][0]
    db.mark_ready(clip.id)
    db.claim_ready()
    db.mark_done(clip.id, "drive-" + name, "link")
    return clip.id


# --- the setting round-trips ---------------------------------------------


def test_retention_starts_off(control: RetentionControl):
    assert control.enabled is False


def test_choosing_a_keep_count_turns_retention_on(control: RetentionControl):
    """Picking a number is the whole gesture; no separate enable step."""
    control.set_keep(40)
    assert control.enabled is True
    assert control.keep_newest == 40


def test_the_choice_survives_a_restart(control: RetentionControl, db: Db, config_path: Path):
    control.set_keep(20)

    fresh = RetentionControl(db, config_path, config_module.load(config_path))
    assert fresh.enabled is True
    assert fresh.keep_newest == 20


def test_keep_everything_turns_it_off_without_losing_the_number(control: RetentionControl):
    control.set_keep(60)
    control.set_enabled(False)

    assert control.enabled is False
    assert control.keep_newest == 60, "re-enabling should not forget the previous choice"


def test_changing_the_setting_does_not_disturb_anything_else(control: RetentionControl, config_path: Path):
    before = config_module.load(config_path)
    control.set_keep(100)
    after = config_module.load(config_path)

    assert after.clips_root == before.clips_root
    assert after.backfill_since == before.backfill_since, "the cutoff must not move"
    assert after.drive_folder_id == before.drive_folder_id


def test_reload_picks_up_an_external_edit(control: RetentionControl, config_path: Path):
    """Editing config.toml by hand must work while the app is running."""
    config_module.update(config_path, retention_enabled=True, retention_keep_newest=15)
    control.reload()
    assert control.keep_newest == 15


def test_a_corrupt_config_keeps_the_previous_settings(control: RetentionControl, config_path: Path):
    control.set_keep(40)
    config_path.write_text("this is not valid toml {{{", encoding="utf-8")

    control.reload()  # must not raise
    assert control.keep_newest == 40


# --- the trigger ----------------------------------------------------------


def test_run_now_only_signals(control: RetentionControl, db: Db):
    """The tray must not delete anything itself; the loop owns that."""
    uploaded(db, "clip1")
    control.run_now()

    assert control.trigger.is_set()
    assert len(db.by_state(DONE)) == 1, "nothing was deleted by the signal alone"


def test_in_drive_count_ignores_retired_clips(control: RetentionControl, db: Db):
    a = uploaded(db, "a")
    uploaded(db, "b")
    assert control.in_drive_count() == 2

    db.mark_retired(a)
    assert control.in_drive_count() == 1


# --- the menu -------------------------------------------------------------


@pytest.fixture
def tray(db: Db, control: RetentionControl) -> Tray:
    return Tray(db, threading.Event(), retention=control)


def test_menu_label_reflects_the_current_setting(tray: Tray, control: RetentionControl):
    assert "everything" in tray._retention_label()

    control.set_keep(40)
    assert "40" in tray._retention_label()


def test_menu_offers_every_keep_choice_plus_off(tray: Tray):
    labels = [item.text for item in tray._retention_items() if getattr(item, "text", None)]
    joined = " ".join(labels)

    assert "Keep everything" in joined
    for n in KEEP_CHOICES:
        assert f"Keep newest {n}" in joined


def test_menu_shows_the_size_each_choice_implies(tray: Tray):
    """A clip count means nothing without the GB it represents."""
    labels = " ".join(item.text for item in tray._retention_items() if getattr(item, "text", None))
    assert "GB" in labels


def test_menu_states_the_age_floor(tray: Tray, control: RetentionControl):
    control.set_keep(10)
    labels = " ".join(item.text for item in tray._retention_items() if getattr(item, "text", None))
    assert "24h" in labels, "the safety floor should be visible, not hidden in a config file"


def test_selecting_a_choice_from_the_menu_applies_it(tray: Tray, control: RetentionControl):
    tray._set_keep(20)()
    assert control.enabled is True
    assert control.keep_newest == 20

    tray._set_keep(None)()
    assert control.enabled is False


def test_the_current_choice_shows_as_selected(tray: Tray, control: RetentionControl):
    control.set_keep(40)
    assert tray._is_keep(40)(None) is True
    assert tray._is_keep(20)(None) is False


def test_tidy_up_now_appears_only_when_retention_is_on(tray: Tray, control: RetentionControl):
    off = " ".join(i.text for i in tray._retention_items() if getattr(i, "text", None))
    assert "Tidy up now" not in off

    control.set_keep(40)
    on = " ".join(i.text for i in tray._retention_items() if getattr(i, "text", None))
    assert "Tidy up now" in on
