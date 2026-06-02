"""Tests for trigger_hazard traps and available_hazards redaction. Extracted from test_rules.py; the enforcement core
stays there. Run:  python -m pytest -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import rules, tools
from src.game_state import Character, NPC, GameState
from tests._helpers import _make_combat_state


def _hazard_gs(hazards: dict) -> GameState:
    gs = GameState(location="Hall", current_scene="hall")
    gs.scenes = {"hall": {"location": "Hall", "exits": {}, "hazards": hazards}}
    gs.party["kael"] = Character(name="Kael", max_hp=20, hp=20,
                                 ability_modifiers={"dex": 3}, proficiency_bonus=2,
                                 save_proficiencies=["dex"])
    gs.party["wisp"] = Character(name="Wisp", max_hp=16, hp=16,
                                 ability_modifiers={"dex": 0}, proficiency_bonus=2)
    return gs


def test_trigger_hazard_failed_save_takes_full_damage():
    gs = _hazard_gs({"dart_trap": {"name": "dart trap", "ability": "dex", "dc": 13, "damage": "2d6"}})
    rules.force_rolls([4, 4, 2])  # damage 2d6=8; Kael save d20=2 (+5=7) -> fail
    res = tools.dispatch("trigger_hazard", {"hazard_id": "dart_trap", "characters": ["Kael"]}, gs)
    assert res["ok"] is True and res["dc"] == 13 and res["ability"] == "dex"
    r = res["results"][0]
    assert r["success"] is False and r["damage"] == 8
    assert gs.party["kael"].hp == 12


def test_trigger_hazard_successful_save_no_damage_by_default():
    gs = _hazard_gs({"dart_trap": {"name": "dart trap", "ability": "dex", "dc": 13, "damage": "2d6"}})
    rules.force_rolls([6, 6, 20])  # damage 12; Kael save nat 20 -> success; on_success defaults "none"
    res = tools.dispatch("trigger_hazard", {"hazard_id": "dart_trap", "characters": ["Kael"]}, gs)
    assert res["results"][0]["success"] is True and res["results"][0]["damage"] == 0
    assert gs.party["kael"].hp == 20


def test_trigger_hazard_save_for_half():
    gs = _hazard_gs({"gas": {"name": "gas", "ability": "dex", "dc": 25, "damage": "2d6", "on_success": "half"}})
    rules.force_rolls([5, 5, 20])  # damage 10; Wisp dex+0 nat 20 = 20 < 25 -> FAIL -> full
    res = tools.dispatch("trigger_hazard", {"hazard_id": "gas", "characters": ["Wisp"]}, gs)
    assert res["results"][0]["success"] is False and res["results"][0]["damage"] == 10
    # Now a success → half of the same-size roll.
    gs.party["wisp"].hp = 16
    rules.force_rolls([6, 6, 20])  # damage 12; give Kael (dex+5) a save vs low... use dc small
    gs2 = _hazard_gs({"gas": {"name": "gas", "ability": "dex", "dc": 5, "damage": "2d6", "on_success": "half"}})
    rules.force_rolls([6, 6, 10])  # damage 12; Kael d20=10 (+5=15) >= 5 -> success -> half=6
    res2 = tools.dispatch("trigger_hazard", {"hazard_id": "gas", "characters": ["Kael"]}, gs2)
    assert res2["results"][0]["success"] is True and res2["results"][0]["damage"] == 6


def test_trigger_hazard_area_one_roll_shared():
    gs = _hazard_gs({"blast": {"name": "blast", "ability": "dex", "dc": 13, "damage": "2d6"}})
    # damage rolled ONCE (8); then saves in target order: Kael fail, Wisp fail.
    rules.force_rolls([4, 4, 2, 3])
    res = tools.dispatch("trigger_hazard", {"hazard_id": "blast"}, gs)  # omit characters -> all conscious party
    dmgs = {r["character"]: r["damage"] for r in res["results"]}
    assert dmgs == {"Kael": 8, "Wisp": 8}  # same single roll applied to both failers
    assert res["damage_roll"] == 8


def test_trigger_hazard_once_then_already_sprung():
    gs = _hazard_gs({"dart_trap": {"name": "dart trap", "ability": "dex", "dc": 13, "damage": "1d6", "once": True}})
    rules.force_rolls([3, 5])
    first = tools.dispatch("trigger_hazard", {"hazard_id": "dart_trap", "characters": ["Kael"]}, gs)
    assert first["ok"] is True
    assert gs.sprung_hazards == ["hall:dart_trap"]
    again = tools.dispatch("trigger_hazard", {"hazard_id": "dart_trap", "characters": ["Kael"]}, gs)
    assert again["ok"] is False and again["reason"] == "already_sprung"


def test_trigger_hazard_once_false_repeats():
    gs = _hazard_gs({"lava": {"name": "lava", "ability": "dex", "dc": 13, "damage": "1d6", "once": False}})
    rules.force_rolls([3, 5])
    assert tools.dispatch("trigger_hazard", {"hazard_id": "lava", "characters": ["Kael"]}, gs)["ok"] is True
    rules.force_rolls([3, 5])
    assert tools.dispatch("trigger_hazard", {"hazard_id": "lava", "characters": ["Kael"]}, gs)["ok"] is True
    assert gs.sprung_hazards == []  # non-one-shot hazards are not tracked


def test_trigger_hazard_requires_flag_gate():
    gs = _hazard_gs({"rune": {"name": "rune", "ability": "wis", "dc": 10, "damage": "1d6", "requires": "rune_armed"}})
    locked = tools.dispatch("trigger_hazard", {"hazard_id": "rune", "characters": ["Kael"]}, gs)
    assert locked["ok"] is False and locked["reason"] == "locked"
    # Hidden from the snapshot until armed.
    assert tools._available_hazards(gs.scenes["hall"], gs.quest_flags, set(), "hall") == []
    gs.quest_flags["rune_armed"] = True
    rules.force_rolls([3, 12])
    assert tools.dispatch("trigger_hazard", {"hazard_id": "rune", "characters": ["Kael"]}, gs)["ok"] is True


def test_trigger_hazard_undeclared_rejected():
    gs = _hazard_gs({"dart_trap": {"name": "dart trap", "ability": "dex", "dc": 13, "damage": "1d6"}})
    res = tools.dispatch("trigger_hazard", {"hazard_id": "fireball_trap", "characters": ["Kael"]}, gs)
    assert res["ok"] is False and res["reason"] == "not_declared"


def test_trigger_hazard_can_down_a_pc():
    gs = _hazard_gs({"spikes": {"name": "spikes", "ability": "dex", "dc": 30, "damage": "4d6"}})
    rules.force_rolls([6, 6, 6, 6, 1])  # 24 damage; Wisp save fails
    res = tools.dispatch("trigger_hazard", {"hazard_id": "spikes", "characters": ["Wisp"]}, gs)
    assert res["results"][0]["downed"] is True
    assert gs.party["wisp"].hp == 0 and "unconscious" in gs.party["wisp"].conditions


def test_available_hazards_omits_sprung_and_gated():
    scene = {"hazards": {
        "a": {"name": "A", "ability": "dex", "dc": 10, "damage": "1d6"},
        "b": {"name": "B", "ability": "dex", "dc": 10, "damage": "1d6", "requires": "armed_b"},
    }}
    avail = tools._available_hazards(scene, {}, set(), "hall")
    assert [h["id"] for h in avail] == ["a"]  # b is gated/hidden
    avail2 = tools._available_hazards(scene, {}, {"hall:a"}, "hall")
    assert avail2 == []  # a is sprung
