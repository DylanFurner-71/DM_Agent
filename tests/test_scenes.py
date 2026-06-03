"""Tests for scene loading, move_scene, declared/gated exits, terminal-scene conclusion. Extracted from test_rules.py; the enforcement core
stays there. Run:  python -m pytest -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import rules, tools
from src.game_state import Character, NPC, GameState
from tests._helpers import _make_combat_state


def test_scenario2_loads_correctly():
    """two_scene_loot_quest_item.json (multi-scene format) populates state from current_scene."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scene_loot_quest_item.json")
    gs = GameState.load(path)
    # current_scene is barrow_entrance → snik should be in the live roster
    assert "snik" in gs.npcs
    npc = gs.npcs["snik"]
    assert npc.name == "Snik"
    assert npc.max_hp == 20
    assert npc.hp == 20
    assert npc.ac == 13
    assert "shortsword" in npc.inventory
    # Party is top-level and must load too
    assert "aldric" in gs.party
    assert "wisp" in gs.party
    # scenes dict is preserved for future transitions
    assert "ember_chamber" in gs.scenes
    assert gs.current_scene == "barrow_entrance"


def test_multi_scene_load_location_and_scene_text():
    """location and scene text are pulled from the active scene on fresh load."""
    d = {
        "current_scene": "ember_chamber",
        "scenes": {
            "ember_chamber": {
                "location": "The Ember Chamber",
                "scene": "Braziers burn with sourceless flame.",
                "npcs": {},
            }
        },
        "party": {},
    }
    gs = GameState.from_dict(d)
    assert gs.location == "The Ember Chamber"
    assert gs.scene == "Braziers burn with sourceless flame."


def test_move_scene_replaces_npcs_and_updates_location():
    """move_scene with a scene_key replaces the NPC roster and updates location/scene."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scene_loot_quest_item.json")
    gs = GameState.load(path)
    assert "snik" in gs.npcs

    res = tools.dispatch("move_scene", {"scene_key": "ember_chamber"}, gs)

    assert res["ok"] is True
    assert res["scene_key"] == "ember_chamber"
    assert gs.current_scene == "ember_chamber"
    assert gs.location == "The Ashen Barrow — The Ember Chamber"
    # Old NPC gone; new scene's NPCs present
    assert "snik" not in gs.npcs
    assert "grik" in gs.npcs
    assert "narl" in gs.npcs


def test_move_scene_npc_stats_and_overrides():
    """move_scene expands template NPCs and applies per-entry overrides (e.g. max_hp)."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scene_loot_quest_item.json")
    gs = GameState.load(path)
    tools.dispatch("move_scene", {"scene_key": "ember_chamber"}, gs)

    grik = gs.npcs["grik"]
    assert grik.name == "Grik"
    assert grik.max_hp == 18   # overridden from goblin template's 12
    assert grik.hp == 18       # starts at full overridden HP
    assert grik.ac == 13       # template value unchanged

    narl = gs.npcs["narl"]
    assert narl.name == "Narl"
    assert narl.max_hp == 12   # standard goblin


def test_move_scene_party_untouched():
    """Scene transitions must never modify the party."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scene_loot_quest_item.json")
    gs = GameState.load(path)
    gs.party["aldric"].hp = 10   # simulate damage

    tools.dispatch("move_scene", {"scene_key": "ember_chamber"}, gs)

    assert gs.party["aldric"].hp == 10   # unchanged
    assert "aldric" in gs.party
    assert "wisp" in gs.party


def test_move_scene_unknown_key_rejected():
    """move_scene with an unknown scene_key returns ok=False."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scene_loot_quest_item.json")
    gs = GameState.load(path)
    original_location = gs.location

    res = tools.dispatch("move_scene", {"scene_key": "nowhere"}, gs)

    assert res["ok"] is False
    assert "nowhere" in res["error"]
    assert gs.location == original_location   # state unchanged


def test_move_scene_missing_scene_key_rejected():
    """move_scene without scene_key when scenes are defined returns ok=False."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scene_loot_quest_item.json")
    gs = GameState.load(path)

    res = tools.dispatch("move_scene", {"location": "Somewhere"}, gs)

    assert res["ok"] is False


def test_move_scene_free_form_without_scenes():
    """move_scene still accepts location/scene strings when no scenes dict is defined."""
    gs = GameState(location="Start")
    res = tools.dispatch("move_scene", {"location": "The Forest", "scene": "Tall oaks."}, gs)
    assert res["ok"] is True
    assert gs.location == "The Forest"
    assert gs.scene == "Tall oaks."


def test_move_scene_concludes_empty_terminal_scene():
    """3a hard gate: from a hostile-free terminal scene (empty exits), calling
    move_scene to conclude grants victory — the engine, not the model, sets game_over."""
    gs = GameState(location="Vault", current_scene="vault")
    gs.scenes = {"vault": {"location": "Vault", "exits": {}}}  # terminal
    gs.party["aldric"] = Character(name="Aldric")
    res = tools.dispatch("move_scene", {"scene_key": "vault"}, gs)
    assert res["ok"] is True
    assert res.get("adventure_complete") is True
    assert res.get("outcome") == "victory"
    assert gs.game_over is True and gs.game_outcome == "victory"


def test_move_scene_conclude_refused_while_hostiles_present():
    """The conclude path is hard-gated: it refuses while a living hostile remains, so
    the model's soft leave/finish trigger can never end the run with foes standing."""
    gs = GameState(location="Vault", current_scene="vault")
    gs.scenes = {"vault": {"location": "Vault", "exits": {}}}  # terminal
    gs.party["aldric"] = Character(name="Aldric")
    gs.npcs["snik"] = NPC(name="Snik", hp=5, hostile=True)  # still up
    res = tools.dispatch("move_scene", {"scene_key": "vault"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "hostiles_present"
    assert gs.game_over is False
    # A downed (or non-hostile) NPC does not block conclusion.
    gs.npcs["snik"].hp = 0
    res2 = tools.dispatch("move_scene", {"scene_key": "vault"}, gs)
    assert res2["ok"] is True and gs.game_over is True


def test_move_scene_from_terminal_completes():
    """Terminal scene + no active combat: move_scene triggers victory."""
    gs = GameState(location="Final Chamber", current_scene="final_room")
    gs.scenes = {"final_room": {"location": "Final Chamber", "exits": {}}}
    res = tools.dispatch("move_scene", {"scene_key": "anywhere"}, gs)
    assert res["ok"] is True
    assert res.get("adventure_complete") is True
    assert res.get("outcome") == "victory"
    assert gs.game_over is True
    assert gs.game_outcome == "victory"


def test_move_scene_nonterminal_unchanged():
    """Normal declared-exit transition: game_over stays False."""
    gs = GameState(location="Start", current_scene="a")
    gs.scenes = {
        "a": {"location": "A", "exits": {"forward": "b"}},
        "b": {"location": "B", "exits": {}},
    }
    res = tools.dispatch("move_scene", {"scene_key": "b"}, gs)
    assert res["ok"] is True
    assert gs.current_scene == "b"
    assert gs.game_over is False


def test_move_scene_terminal_in_combat_does_not_complete():
    """Terminal scene but combat_round > 0: refused for being in combat, no victory."""
    gs = GameState(location="Final Chamber", current_scene="final_room")
    gs.scenes = {"final_room": {"location": "Final Chamber", "exits": {}}}
    gs.combat_round = 1
    gs.combat_order = ["aldric"]
    res = tools.dispatch("move_scene", {"scene_key": "anywhere"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "in_combat"
    assert gs.game_over is False


def test_move_scene_follows_declared_exit():
    """move_scene to a scene_key that is a declared exit of the current scene succeeds."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scene_loot_quest_item.json")
    gs = GameState.load(path)
    assert gs.current_scene == "barrow_entrance"
    res = tools.dispatch("move_scene", {"scene_key": "ember_chamber"}, gs)
    assert res["ok"] is True
    assert gs.current_scene == "ember_chamber"


def test_move_scene_rejects_non_exit():
    """move_scene to a defined scene that is not in the current scene's exits is rejected."""
    gs = GameState(location="Start", current_scene="a")
    gs.scenes = {
        "a": {"location": "A", "exits": {"to b": "b"}},
        "b": {"location": "B", "exits": {}},
        "c": {"location": "C", "exits": {}},
    }
    # 'c' is defined but not reachable from 'a'
    res = tools.dispatch("move_scene", {"scene_key": "c"}, gs)
    assert res["ok"] is False
    assert "c" in res["error"]
    assert gs.current_scene == "a"   # state unchanged


def test_move_scene_terminal_has_no_exits():
    """Terminal scene + active combat: move_scene is refused with reason in_combat."""
    gs = GameState(location="Chamber", current_scene="ember_chamber")
    gs.scenes = {
        "ember_chamber": {"location": "The Ember Chamber", "exits": {}},
    }
    gs.combat_round = 1
    gs.combat_order = ["aldric"]
    res = tools.dispatch("move_scene", {"scene_key": "anywhere"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "in_combat"


def test_move_scene_declared_exit_refused_in_combat():
    """A declared-exit move is refused while combat is active; combat state untouched.

    Regression for the stale-combat_order bug: moving scenes mid-combat used to rebuild
    state.npcs from the destination while leaving combat_order pointing at the old scene's
    combatants. The move is now refused, so combat state cannot go stale.
    """
    gs = GameState(location="Start", current_scene="a")
    gs.scenes = {
        "a": {"location": "A", "exits": {"forward": "b"}},
        "b": {"location": "B", "exits": {}},
    }
    gs.party["aldric"] = Character(name="Aldric")
    gs.npcs["snik"] = NPC(name="Snik")
    gs.combat_round = 1
    gs.combat_order = ["aldric", "snik"]
    gs.combat_index = 0
    res = tools.dispatch("move_scene", {"scene_key": "b"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "in_combat"
    # State unchanged — the bug was leaving combat_order stale after a move.
    assert gs.current_scene == "a"
    assert gs.combat_round == 1
    assert gs.combat_order == ["aldric", "snik"]


def test_state_snapshot_includes_exits():
    """_state_snapshot includes the current scene's exits when exits are defined."""
    import json as _json
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(location="Barrow Entrance", current_scene="barrow_entrance")
    gs.scenes = {
        "barrow_entrance": {
            "location": "Barrow Entrance",
            "exits": {"the dark passage ahead": "ember_chamber"},
        },
        "ember_chamber": {"location": "The Ember Chamber", "exits": {}},
    }
    gs.party["aldric"] = Character(name="Aldric")

    agent = DMAgent(gs, client=MagicMock())
    snap = _json.loads(agent._state_snapshot())

    assert "exits" in snap
    assert snap["exits"] == {"the dark passage ahead": "ember_chamber"}


def _make_gated_exits_state(flag: str | None = None) -> GameState:
    """State with one ungated string exit and one gated dict exit."""
    gs = GameState(
        current_scene="hall",
        scenes={
            "hall": {
                "location": "The Hall",
                "scene": "Two doors.",
                "exits": {
                    "north door": "north_room",
                    "iron door": {"to": "vault", "requires": "has_key", "denied": "The iron door is locked."},
                },
            },
            "north_room": {"location": "North Room", "scene": "", "exits": {}},
            "vault":      {"location": "The Vault",  "scene": "", "exits": {}},
        },
    )
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20)
    if flag:
        gs.quest_flags[flag] = True
    return gs


def _make_terminal_gated_state(flag: str | None = None) -> GameState:
    """Terminal scene with exit_requires; no regular exits."""
    gs = GameState(
        current_scene="final_chamber",
        scenes={
            "final_chamber": {
                "location": "Final Chamber",
                "scene": "An iron door bars the exit.",
                "exits": {},
                "exit_requires": "iron_door_open",
                "exit_denied": "The iron door is sealed.",
            },
        },
    )
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20)
    if flag:
        gs.quest_flags[flag] = True
    return gs


def test_gated_exit_target_extraction_dict():
    """_target() returns the 'to' value from a gated exit dict."""
    from src.tools import _target
    assert _target({"to": "vault", "requires": "has_key", "denied": "Locked."}) == "vault"


def test_gated_exit_target_extraction_string():
    """_target() passes a bare string through unchanged."""
    from src.tools import _target
    assert _target("north_room") == "north_room"


def test_exits_for_model_strips_denied_keeps_requires():
    """_exits_for_model removes 'denied'; string exits unchanged; 'requires' preserved."""
    from src.tools import _exits_for_model
    exits = {
        "north door": "north_room",
        "iron door": {"to": "vault", "requires": "has_key", "denied": "Locked."},
    }
    result = _exits_for_model(exits)
    assert result["north door"] == "north_room"
    assert result["iron door"] == {"to": "vault", "requires": "has_key"}
    assert "denied" not in result["iron door"]


def test_ungated_string_exit_backward_compat():
    """Ungated string exits move as before after the gated-exit changes."""
    gs = _make_gated_exits_state()
    res = tools.dispatch("move_scene", {"scene_key": "north_room"}, gs)
    assert res["ok"] is True
    assert gs.current_scene == "north_room"


def test_gated_exit_flag_absent_returns_locked():
    """Gated exit without the flag: ok=False reason='locked', current_scene unchanged."""
    gs = _make_gated_exits_state()  # no flag
    res = tools.dispatch("move_scene", {"scene_key": "vault"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "locked"
    assert res["required_flag"] == "has_key"
    assert res["error"] == "The iron door is locked."
    assert gs.current_scene == "hall"  # unchanged


def test_gated_exit_flag_set_allows_move():
    """Gated exit with the required flag set: move proceeds normally."""
    gs = _make_gated_exits_state(flag="has_key")
    res = tools.dispatch("move_scene", {"scene_key": "vault"}, gs)
    assert res["ok"] is True
    assert gs.current_scene == "vault"


def test_non_declared_exit_rejected_not_locked():
    """A scene_key absent from all exits is rejected as undeclared, not as locked."""
    gs = _make_gated_exits_state()
    res = tools.dispatch("move_scene", {"scene_key": "secret_passage"}, gs)
    assert res["ok"] is False
    assert res.get("reason") != "locked"
    assert gs.current_scene == "hall"


def test_terminal_scene_no_exit_requires_fires_victory():
    """Backward compat: terminal scene with no exit_requires grants victory on move attempt."""
    gs = GameState(
        current_scene="end",
        scenes={"end": {"location": "End", "scene": "", "exits": {}}},
    )
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20)
    res = tools.dispatch("move_scene", {"scene_key": "anywhere"}, gs)
    assert res["ok"] is True
    assert res.get("outcome") == "victory"
    assert gs.game_over is True


def test_terminal_scene_exit_requires_flag_absent_returns_locked():
    """Terminal scene with exit_requires: flag absent -> locked, game_over stays False."""
    gs = _make_terminal_gated_state()  # no flag
    res = tools.dispatch("move_scene", {"scene_key": "anywhere"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "locked"
    assert res["required_flag"] == "iron_door_open"
    assert res["error"] == "The iron door is sealed."
    assert gs.game_over is False


def test_terminal_scene_exit_requires_flag_set_fires_victory():
    """Terminal scene with exit_requires: flag set -> game_over True, outcome victory."""
    gs = _make_terminal_gated_state(flag="iron_door_open")
    res = tools.dispatch("move_scene", {"scene_key": "anywhere"}, gs)
    assert res["ok"] is True
    assert res.get("outcome") == "victory"
    assert gs.game_over is True
    assert gs.game_outcome == "victory"
