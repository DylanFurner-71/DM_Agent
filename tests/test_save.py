"""Tests for /save helpers — no API, no input() needed."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.main import _resolve_save_path, _do_save, _do_export
from src.game_state import Character, GameState


def _simple_state():
    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric")
    return gs


def _played_state():
    gs = _simple_state()
    gs.transcript = [
        {"kind": "player", "text": "Aldric opens the door."},
        {"kind": "dm", "text": "It groans wide onto darkness."},
    ]
    return gs


# --- _resolve_save_path -------------------------------------------------------

def test_resolve_appends_json(tmp_path):
    p = _resolve_save_path("mygame", base_dir=tmp_path)
    assert p == tmp_path / "mygame.json"


def test_resolve_no_double_extension_lowercase(tmp_path):
    p = _resolve_save_path("mygame.json", base_dir=tmp_path)
    assert p == tmp_path / "mygame.json"


def test_resolve_no_double_extension_uppercase(tmp_path):
    p = _resolve_save_path("Game.JSON", base_dir=tmp_path)
    assert p == tmp_path / "Game.JSON"
    assert p.name.count(".") == 1


def test_resolve_strips_directories(tmp_path):
    p = _resolve_save_path("../../etc/passwd", base_dir=tmp_path)
    assert p.parent == tmp_path
    assert p.name == "passwd.json"


def test_resolve_rejects_empty(tmp_path):
    with pytest.raises(ValueError):
        _resolve_save_path("", base_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_resolve_rejects_whitespace_only(tmp_path):
    with pytest.raises(ValueError):
        _resolve_save_path("   ", base_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_resolve_creates_base_dir(tmp_path):
    subdir = tmp_path / "nested" / "saves"
    assert not subdir.exists()
    _resolve_save_path("slot1", base_dir=subdir)
    assert subdir.is_dir()


# --- _do_save -----------------------------------------------------------------

def test_do_save_writes_chosen_path(tmp_path):
    gs = _simple_state()
    status, path = _do_save(gs, "slot1", base_dir=tmp_path)
    assert status == "saved"
    assert path.exists()
    data = json.loads(path.read_text())
    assert "party" in data
    assert "current_scene" in data
    assert "turn" in data


def test_do_save_no_clobber_without_flag(tmp_path):
    gs = _simple_state()
    target = tmp_path / "slot1.json"
    original = b'{"original": true}'
    target.write_bytes(original)
    status, val = _do_save(gs, "slot1", base_dir=tmp_path, overwrite=False)
    assert status == "exists"
    assert val == target
    assert target.read_bytes() == original  # file untouched


def test_do_save_overwrites_when_flag_set(tmp_path):
    gs = _simple_state()
    target = tmp_path / "slot1.json"
    target.write_bytes(b'{"stale": true}')
    status, path = _do_save(gs, "slot1", base_dir=tmp_path, overwrite=True)
    assert status == "saved"
    data = json.loads(path.read_text())
    assert "party" in data  # new content, not the stale stub


def test_do_save_error_does_not_raise(tmp_path, monkeypatch):
    gs = _simple_state()

    def raise_disk_full(path):
        raise OSError("disk full")

    monkeypatch.setattr(gs, "save", raise_disk_full)
    status, val = _do_save(gs, "slot1", base_dir=tmp_path)
    assert status == "error"
    assert isinstance(val, str)
    assert "disk full" in val


# --- stats-trace sidecar ------------------------------------------------------

def test_save_writes_stats_trace_sidecar(tmp_path):
    """A stats_trace of N turn records → <name>_stats_trace.json holding that list."""
    gs = _simple_state()
    stats = [
        {"turn": 1, "player_input": "swing",
         "tool_calls": [{"name": "attack", "input": {"attacker": "Aldric"}, "result": {"ok": True}}],
         "api_calls": [{"phase": "thinking", "elapsed": 1.2, "usage": {"input": 10, "output": 5}}]},
        {"turn": 2, "player_input": "cast",
         "tool_calls": [{"name": "cast_spell", "input": {"caster": "Wisp"}, "result": {"ok": False}}],
         "api_calls": []},
    ]
    _do_save(gs, "slot1", base_dir=tmp_path, stats_trace=stats)

    sidecar = tmp_path / "slot1_stats_trace.json"
    assert sidecar.exists(), "stats-trace sidecar must be created alongside the save file"
    assert json.loads(sidecar.read_text()) == stats  # round-trips the full per-turn record


def test_save_game_json_has_no_trace(tmp_path):
    """The game-state JSON must contain only game state — no trace keys."""
    gs = _simple_state()
    stats = [{"turn": 1, "player_input": "x", "tool_calls": [], "api_calls": []}]
    _do_save(gs, "slot1", base_dir=tmp_path, stats_trace=stats)

    data = json.loads((tmp_path / "slot1.json").read_text())
    for key in ("trace", "tool_trace", "full_trace", "calls", "api_calls"):
        assert key not in data, f"save-game JSON must not contain {key!r}"


def test_sidecar_failure_does_not_affect_save(tmp_path, monkeypatch):
    """A write error on the sidecar must leave the game file intact and return 'saved'."""
    import builtins
    gs = _simple_state()
    original_open = builtins.open

    def bad_open(path, *args, **kwargs):
        if str(path).endswith("_stats_trace.json"):
            raise OSError("no space left on device")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", bad_open)
    stats = [{"turn": 1, "player_input": "x", "tool_calls": [], "api_calls": []}]
    status, val = _do_save(gs, "slot1", base_dir=tmp_path, stats_trace=stats)

    assert status == "saved"           # game save succeeded despite sidecar failure
    assert (tmp_path / "slot1.json").exists()


def test_no_stats_sidecar_when_omitted(tmp_path):
    """With no stats_trace, only the game-state JSON is written — no sidecar."""
    gs = _simple_state()
    _do_save(gs, "slot1", base_dir=tmp_path)
    assert not (tmp_path / "slot1_stats_trace.json").exists()


def test_do_save_empty_name_returns_error(tmp_path):
    gs = _simple_state()
    status, val = _do_save(gs, "", base_dir=tmp_path)
    assert status == "error"
    assert isinstance(val, str)
    assert list(tmp_path.iterdir()) == []  # nothing written


# --- _resolve_save_path with a custom extension ------------------------------

def test_resolve_appends_md_extension(tmp_path):
    p = _resolve_save_path("log", base_dir=tmp_path, ext=".md")
    assert p == tmp_path / "log.md"


def test_resolve_no_double_md_extension(tmp_path):
    p = _resolve_save_path("log.md", base_dir=tmp_path, ext=".md")
    assert p == tmp_path / "log.md"


# --- _do_export --------------------------------------------------------------

def test_do_export_writes_markdown(tmp_path):
    status, path = _do_export(_played_state(), "story", base_dir=tmp_path)
    assert status == "saved"
    assert path == tmp_path / "story.md"
    text = path.read_text()
    assert text.startswith("# DM Agent — Session Log")
    assert "**You:** Aldric opens the door." in text
    assert "It groans wide onto darkness." in text


def test_do_export_empty_transcript_writes_nothing(tmp_path):
    status, val = _do_export(_simple_state(), "story", base_dir=tmp_path)
    assert status == "empty"
    assert isinstance(val, str)
    assert list(tmp_path.iterdir()) == []  # no file created for an empty session


def test_do_export_no_clobber_without_flag(tmp_path):
    target = tmp_path / "story.md"
    target.write_text("ORIGINAL")
    status, val = _do_export(_played_state(), "story", base_dir=tmp_path, overwrite=False)
    assert status == "exists"
    assert val == target
    assert target.read_text() == "ORIGINAL"  # untouched


def test_do_export_overwrites_when_flag_set(tmp_path):
    target = tmp_path / "story.md"
    target.write_text("STALE")
    status, path = _do_export(_played_state(), "story", base_dir=tmp_path, overwrite=True)
    assert status == "saved"
    assert "Session Log" in path.read_text()


def test_do_export_empty_name_returns_error(tmp_path):
    status, val = _do_export(_played_state(), "", base_dir=tmp_path)
    assert status == "error"
    assert isinstance(val, str)
    assert not list(tmp_path.iterdir())  # nothing written
