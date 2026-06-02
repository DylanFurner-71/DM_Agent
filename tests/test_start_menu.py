"""Tests for the start-menu model — no API, no TTY.

The interactive selector (raw-mode key reading and rich rendering) is left to
manual verification; here we cover the pure helpers that build the menu from the
filesystem: adventure discovery and the conditional landing rows.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import start_menu
from src.start_menu import (
    _build_landing,
    _load_adventures,
    _other_saves,
    _prettify,
    _OTHER_SAVES,
)


# --- adventure discovery ------------------------------------------------------

def test_load_shipped_adventures_have_titles_and_blurbs():
    advs = _load_adventures()  # reads the real data/adventures
    assert advs, "expected shipped adventures under data/adventures"
    by_name = {os.path.basename(p): (title, blurb) for p, title, blurb in advs}
    assert by_name["emberdeep_mine.json"][0] == "Emberdeep Mine"
    # every shipped adventure carries an author title and a non-empty blurb
    for _, title, blurb in advs:
        assert title and blurb


def test_load_adventures_falls_back_for_missing_fields(tmp_path):
    (tmp_path / "lost_caverns.json").write_text(json.dumps({"current_scene": "a"}))
    advs = _load_adventures(tmp_path)
    assert advs == [(str(tmp_path / "lost_caverns.json"), "Lost Caverns", "")]


def test_load_adventures_tolerates_unparseable_file(tmp_path):
    (tmp_path / "broken.json").write_text("{not valid json")
    advs = _load_adventures(tmp_path)
    assert advs == [(str(tmp_path / "broken.json"), "Broken", "")]


def test_load_adventures_missing_dir_is_empty(tmp_path):
    assert _load_adventures(tmp_path / "nope") == []


def test_prettify():
    assert _prettify("tomb_of_the_sunken_king") == "Tomb Of The Sunken King"


# --- landing rows -------------------------------------------------------------

_ADVS = [("data/adventures/x.json", "X", "an x"), ("data/adventures/y.json", "Y", "")]


def test_landing_lists_adventures_with_and_without_blurb(tmp_path):
    rows = _build_landing(_ADVS, save_dir=tmp_path)
    labels = [label for label, _ in rows]
    actions = [action for _, action in rows]
    assert "X  —  an x" in labels   # blurb shown
    assert "Y" in labels            # bare title when no blurb
    assert "data/adventures/x.json" in actions


def test_resume_row_only_when_autosave_exists(tmp_path):
    # no autosave yet
    rows = _build_landing(_ADVS, save_dir=tmp_path)
    assert not any("Resume" in label for label, _ in rows)
    # create the autosave
    (tmp_path / "autosave.json").write_text("{}")
    rows = _build_landing(_ADVS, save_dir=tmp_path)
    assert rows[0][0].startswith("↩")
    assert rows[0][1] == str(tmp_path / "autosave.json")


def test_load_other_saves_row_only_when_other_saves_exist(tmp_path):
    rows = _build_landing(_ADVS, save_dir=tmp_path)
    assert not any(action == _OTHER_SAVES for _, action in rows)
    (tmp_path / "my_run.json").write_text("{}")
    rows = _build_landing(_ADVS, save_dir=tmp_path)
    assert any(action == _OTHER_SAVES for _, action in rows)


def test_other_saves_excludes_autosave_and_stats_sidecars(tmp_path):
    (tmp_path / "autosave.json").write_text("{}")
    (tmp_path / "my_run.json").write_text("{}")
    (tmp_path / "my_run_stats_trace.json").write_text("[]")
    names = [p.name for p in _other_saves(tmp_path)]
    assert names == ["my_run.json"]
