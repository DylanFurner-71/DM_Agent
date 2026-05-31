"""Tests for /save helpers — no API, no input() needed."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.main import _resolve_save_path, _do_save
from src.game_state import Character, GameState


def _simple_state():
    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric")
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


# --- trace sidecar -----------------------------------------------------------

def test_save_writes_trace_sidecar(tmp_path):
    """M trace records → M lines in <name>.trace.jsonl, each valid JSON."""
    gs = _simple_state()
    trace = [
        {"turn": 1, "name": "attack",    "input": {"attacker": "Aldric"}, "result": {"ok": True}},
        {"turn": 1, "name": "get_state", "input": {},                     "result": {"ok": True}},
        {"turn": 2, "name": "cast_spell","input": {"caster": "Wisp"},     "result": {"ok": False}},
    ]
    _do_save(gs, "slot1", base_dir=tmp_path, trace=trace)

    sidecar = tmp_path / "slot1.trace.jsonl"
    assert sidecar.exists(), "sidecar must be created alongside the save file"
    lines = sidecar.read_text().splitlines()
    assert len(lines) == len(trace), f"expected {len(trace)} lines, got {len(lines)}"
    for line in lines:
        json.loads(line)  # every line must be independently parseable JSON


def test_save_game_json_has_no_trace(tmp_path):
    """The game-state JSON must contain only game state — no trace keys."""
    gs = _simple_state()
    trace = [{"turn": 1, "name": "attack", "input": {}, "result": {}}]
    _do_save(gs, "slot1", base_dir=tmp_path, trace=trace)

    data = json.loads((tmp_path / "slot1.json").read_text())
    for key in ("trace", "tool_trace", "full_trace", "calls"):
        assert key not in data, f"save-game JSON must not contain {key!r}"


def test_sidecar_failure_does_not_affect_save(tmp_path, monkeypatch):
    """A write error on the sidecar must leave the game file intact and return 'saved'."""
    import builtins
    gs = _simple_state()
    original_open = builtins.open

    def bad_open(path, *args, **kwargs):
        if str(path).endswith(".trace.jsonl"):
            raise OSError("no space left on device")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", bad_open)
    trace = [{"turn": 1, "name": "roll_dice", "input": {}, "result": {}}]
    status, val = _do_save(gs, "slot1", base_dir=tmp_path, trace=trace)

    assert status == "saved"           # game save succeeded despite sidecar failure
    assert (tmp_path / "slot1.json").exists()


def test_no_sidecar_when_trace_is_none(tmp_path):
    """When trace=None (default), no sidecar file must be created."""
    gs = _simple_state()
    _do_save(gs, "slot1", base_dir=tmp_path)  # trace omitted
    assert not (tmp_path / "slot1.trace.jsonl").exists()


def test_do_save_empty_name_returns_error(tmp_path):
    gs = _simple_state()
    status, val = _do_save(gs, "", base_dir=tmp_path)
    assert status == "error"
    assert isinstance(val, str)
    assert list(tmp_path.iterdir()) == []  # nothing written
