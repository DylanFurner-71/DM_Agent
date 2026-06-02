"""Tests for influence_npc and parley-to-combat. Extracted from test_rules.py; the enforcement core
stays there. Run:  python -m pytest -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import rules, tools
from src.game_state import Character, NPC, GameState
from tests._helpers import _make_combat_state


def _influence_state(*, combat: bool = False):
    """Minimal GameState for influence_npc tests: one PC (cha +2) and one swayable NPC."""
    gs = GameState()
    gs.party["aldric"] = Character(
        name="Aldric", max_hp=20, hp=20,
        ability_modifiers={"cha": 2},
    )
    gs.npcs["snik"] = NPC(name="Snik", max_hp=10, hp=10, hostile=True, disposition_dc=12)
    if combat:
        gs.combat_order = ["aldric", "snik"]
        gs.combat_index = 0
        gs.combat_round = 1
        gs.action_used = False
    return gs


def test_influence_npc_success():
    """cha +2, DC 12, force roll 15 -> total 17 >= 12: success, npc turns non-hostile."""
    gs = _influence_state()
    rules.force_rolls([15])
    res = tools.dispatch("influence_npc", {"character": "Aldric", "npc": "Snik", "approach": "persuade"}, gs)
    assert res["ok"] is True
    assert res["success"] is True
    assert res["now_hostile"] is False
    assert gs.npcs["snik"].hostile is False
    assert gs.npcs["snik"].social_attempted is True


def test_influence_npc_failure():
    """cha +2, DC 12, force roll 3 -> total 5 < 12: failure, npc stays hostile."""
    gs = _influence_state()
    rules.force_rolls([3])
    res = tools.dispatch("influence_npc", {"character": "Aldric", "npc": "Snik", "approach": "intimidate"}, gs)
    assert res["ok"] is True
    assert res["success"] is False
    assert res["now_hostile"] is True
    assert gs.npcs["snik"].hostile is True
    assert gs.npcs["snik"].social_attempted is True


def test_influence_npc_one_attempt_only():
    """A second influence_npc on the same NPC -> ok=False 'already_attempted', no roll made.
    Uses a failed first attempt so the NPC stays hostile (reaching already_attempted, not not_hostile).
    combat_starting is cleared between the two calls to simulate take_turn's barrier reset."""
    gs = _influence_state()
    rules.force_rolls([3])  # fail: total 5 < DC 12, NPC stays hostile
    tools.dispatch("influence_npc", {"character": "Aldric", "npc": "Snik", "approach": "persuade"}, gs)
    assert gs.npcs["snik"].social_attempted is True
    gs.combat_starting = False  # simulate take_turn clearing the barrier before the next turn
    res = tools.dispatch("influence_npc", {"character": "Aldric", "npc": "Snik", "approach": "persuade"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "already_attempted"


def test_influence_npc_immovable():
    """NPC with disposition_dc=None cannot be influenced."""
    gs = GameState()
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20, ability_modifiers={"cha": 2})
    gs.npcs["brute"] = NPC(name="Brute", hostile=True)  # disposition_dc defaults to None
    res = tools.dispatch("influence_npc", {"character": "Aldric", "npc": "Brute", "approach": "persuade"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "immovable"


def test_influence_npc_not_hostile():
    """Already non-hostile NPC -> ok=False 'not_hostile'."""
    gs = GameState()
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20, ability_modifiers={"cha": 2})
    gs.npcs["snik"] = NPC(name="Snik", hostile=False, disposition_dc=10)
    res = tools.dispatch("influence_npc", {"character": "Aldric", "npc": "Snik", "approach": "persuade"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "not_hostile"


def test_influence_npc_downed_target():
    """Downed NPC -> ok=False (target_down)."""
    gs = GameState()
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20, ability_modifiers={"cha": 2})
    gs.npcs["snik"] = NPC(name="Snik", hp=0, hostile=True, disposition_dc=10)
    res = tools.dispatch("influence_npc", {"character": "Aldric", "npc": "Snik", "approach": "persuade"}, gs)
    assert res["ok"] is False


def test_influence_npc_provocation():
    """Attacking a non-hostile NPC flips it hostile."""
    gs = GameState()
    gs.party["aldric"] = Character(
        name="Aldric", max_hp=20, hp=20, attack_bonus=4, inventory=["mace"],
        ability_modifiers={"str": 3},
    )
    gs.npcs["snik"] = NPC(name="Snik", hp=20, max_hp=20, ac=1, hostile=False)
    rules.force_rolls([15, 4])  # to-hit 15 (hit), damage 4
    res = tools.dispatch("attack", {"attacker": "Aldric", "defender": "Snik", "weapon": "mace"}, gs)
    assert res["ok"] is True
    assert res["hit"] is True
    assert res.get("provoked") is True
    assert gs.npcs["snik"].hostile is True


def test_influence_npc_ends_combat():
    """Successful influence on the last hostile NPC -> _maybe_end_combat ends the fight."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = _influence_state(combat=True)
    rules.force_rolls([15])
    tools.dispatch("influence_npc", {"character": "Aldric", "npc": "Snik", "approach": "persuade"}, gs)
    assert gs.npcs["snik"].hostile is False

    agent = DMAgent(gs, client=MagicMock())
    ended = agent._maybe_end_combat()
    assert ended is True
    assert gs.combat_round == 0


def test_influence_npc_action_economy_in_combat():
    """In combat, influence_npc sets action_used; a follow-up attack on the same turn is refused."""
    gs = _influence_state(combat=True)
    rules.force_rolls([3])  # failed attempt; turn stays with Aldric but action is spent
    res = tools.dispatch("influence_npc", {"character": "Aldric", "npc": "Snik", "approach": "intimidate"}, gs)
    assert res["ok"] is True
    assert gs.action_used is True

    follow_up = tools.dispatch("attack", {"attacker": "Aldric", "defender": "Snik", "weapon": "mace"}, gs)
    assert follow_up["ok"] is False
    assert "already acted" in follow_up["error"]


def test_influence_npc_out_of_combat_no_turn_guard():
    """Out of combat, influence_npc works freely without a turn guard."""
    gs = _influence_state()  # no combat
    rules.force_rolls([15])
    res = tools.dispatch("influence_npc", {"character": "Aldric", "npc": "Snik", "approach": "persuade"}, gs)
    assert res["ok"] is True
    assert gs.action_used is False


def test_failed_parley_out_of_combat_starts_combat():
    """Out of combat, failed influence_npc auto-initiates combat and returns combat info."""
    gs = _influence_state()  # no combat
    rules.force_rolls([3])   # cha +2, DC 12: total 5 — fail
    res = tools.dispatch("influence_npc", {"character": "Aldric", "npc": "Snik", "approach": "persuade"}, gs)
    assert res["ok"] is True
    assert res["success"] is False
    assert res["combat_started"] is True
    assert gs.combat_round == 1
    assert gs.combat_order != []
    assert "aldric" in gs.combat_order
    assert "snik" in gs.combat_order
    assert res["combat_order"] == gs.combat_order
    assert res["active"] == gs.combat_order[0]
    assert res["active_name"] is not None
    assert res["round"] == 1


def test_successful_parley_does_not_start_combat():
    """Successful influence_npc must NOT auto-start combat."""
    gs = _influence_state()
    rules.force_rolls([15])   # total 17 >= 12 — success
    res = tools.dispatch("influence_npc", {"character": "Aldric", "npc": "Snik", "approach": "persuade"}, gs)
    assert res["ok"] is True
    assert res["success"] is True
    assert "combat_started" not in res
    assert gs.combat_round == 0


def test_failed_parley_in_combat_does_not_restart():
    """In combat, a failed parley costs the action but does not touch combat state."""
    gs = _influence_state(combat=True)
    round_before = gs.combat_round
    order_before = list(gs.combat_order)
    rules.force_rolls([3])
    res = tools.dispatch("influence_npc", {"character": "Aldric", "npc": "Snik", "approach": "intimidate"}, gs)
    assert res["ok"] is True
    assert res["success"] is False
    assert "combat_started" not in res
    assert gs.combat_round == round_before
    assert gs.combat_order == order_before


def test_failed_parley_combat_includes_all_conscious_fighters():
    """All conscious party members and all hostile living NPCs enter the auto-started combat."""
    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20, ability_modifiers={"cha": 0, "dex": 0})
    gs.party["wisp"]   = Character(name="Wisp",   max_hp=20, hp=20, ability_modifiers={"cha": 0, "dex": 0})
    gs.party["dead"]   = Character(name="Dead",   max_hp=20, hp=0,  ability_modifiers={})   # downed — excluded
    gs.npcs["snik"] = NPC(name="Snik",  max_hp=10, hp=10, hostile=True,  disposition_dc=10)
    gs.npcs["narl"] = NPC(name="Narl",  max_hp=10, hp=10, hostile=True)   # no disposition_dc
    gs.npcs["ally"] = NPC(name="Ally",  max_hp=10, hp=10, hostile=False)  # non-hostile — excluded
    rules.force_rolls([1])   # total 1 < DC 10 — fail
    res = tools.dispatch("influence_npc", {"character": "Aldric", "npc": "Snik", "approach": "persuade"}, gs)
    assert res["ok"] is True
    assert res["combat_started"] is True
    order = gs.combat_order
    assert "aldric" in order
    assert "wisp"   in order
    assert "snik"   in order
    assert "narl"   in order
    assert "dead"   not in order    # downed PC excluded
    assert "ally"   not in order    # non-hostile NPC excluded


def test_failed_parley_combat_starting_blocks_attack():
    """After failed parley auto-starts combat, combat_starting=True blocks an attack
    in the same _execute hop (same safety barrier as explicit start_combat)."""
    gs = _influence_state()
    gs.npcs["snik"].hp = gs.npcs["snik"].max_hp = 20  # enough HP to survive
    rules.force_rolls([3])   # fail → auto-combat
    tools.dispatch("influence_npc", {"character": "Aldric", "npc": "Snik", "approach": "persuade"}, gs)
    assert gs.combat_starting is True  # barrier set

    # Attack in the same hop must be refused with reason "combat_starting"
    atk = tools.dispatch("attack", {"attacker": "Aldric", "defender": "Snik"}, gs)
    assert atk["ok"] is False
    assert atk["reason"] == "combat_starting"
