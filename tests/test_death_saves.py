"""Death-save cycle enforcement — no API.

The full downed/dying/dead state machine, damage-while-down, and the loop's
death-save handling. Moved out of test_rules.py."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import rules, tools
from src.game_state import Character, NPC, GameState


# --- Stage 1: death saves -------------------------------------------------------

def _make_roll_result(n: int):
    from src.rules import RollResult
    return RollResult("1d20", [n], 0, n)




def test_death_save_properties_healthy():
    c = Character(name="Hero", max_hp=10, hp=10)
    assert c.is_dying is False
    assert c.is_stable is False
    assert c.is_dead is False
    assert c.is_down is False




def test_death_save_properties_fresh_down():
    c = Character(name="Hero", max_hp=10, hp=0)
    assert c.is_down is True
    assert c.is_dying is True
    assert c.is_stable is False
    assert c.is_dead is False




def test_death_save_properties_stable():
    c = Character(name="Hero", max_hp=10, hp=0, stable=True)
    assert c.is_stable is True
    assert c.is_dying is False




def test_death_save_properties_dead():
    c = Character(name="Hero", max_hp=10, hp=0, dead=True)
    assert c.is_dead is True
    assert c.is_dying is False




def test_death_save_nat20_revives():
    from unittest.mock import patch
    c = Character(name="Hero", max_hp=20, hp=0, conditions=["unconscious"],
                  death_save_failures=2, death_save_successes=1)
    with patch.object(rules, "roll", return_value=_make_roll_result(20)):
        res = rules.roll_death_save(c)
    assert res["ok"] is True
    assert res["result_kind"] == "revived"
    assert res["hp"] == 1
    assert c.hp == 1
    assert res["successes"] == 0
    assert res["failures"] == 0
    assert c.death_save_successes == 0
    assert c.death_save_failures == 0
    assert "unconscious" not in c.conditions




def test_death_save_nat1_adds_two_failures():
    from unittest.mock import patch
    c = Character(name="Hero", max_hp=20, hp=0, death_save_failures=0)
    with patch.object(rules, "roll", return_value=_make_roll_result(1)):
        res = rules.roll_death_save(c)
    assert res["ok"] is True
    assert res["result_kind"] == "failure"
    assert res["failures"] == 2
    assert c.death_save_failures == 2




def test_death_save_nat1_caps_at_three_and_dies():
    from unittest.mock import patch
    c = Character(name="Hero", max_hp=20, hp=0, death_save_failures=2)
    with patch.object(rules, "roll", return_value=_make_roll_result(1)):
        res = rules.roll_death_save(c)
    assert res["result_kind"] == "dead"
    assert c.dead is True
    assert res["failures"] == 3




def test_death_save_low_roll_adds_one_failure():
    from unittest.mock import patch
    c = Character(name="Hero", max_hp=20, hp=0, death_save_failures=0)
    with patch.object(rules, "roll", return_value=_make_roll_result(5)):
        res = rules.roll_death_save(c)
    assert res["result_kind"] == "failure"
    assert res["failures"] == 1
    assert c.death_save_failures == 1




def test_death_save_mid_roll_adds_one_success():
    from unittest.mock import patch
    c = Character(name="Hero", max_hp=20, hp=0, death_save_successes=0)
    with patch.object(rules, "roll", return_value=_make_roll_result(15)):
        res = rules.roll_death_save(c)
    assert res["result_kind"] == "success"
    assert res["successes"] == 1
    assert c.death_save_successes == 1




def test_death_save_three_successes_stabilizes():
    from unittest.mock import patch
    c = Character(name="Hero", max_hp=20, hp=0, death_save_successes=2)
    with patch.object(rules, "roll", return_value=_make_roll_result(15)):
        res = rules.roll_death_save(c)
    assert res["result_kind"] == "stabilized"
    assert c.stable is True
    assert c.death_save_successes == 0
    assert c.death_save_failures == 0




def test_death_save_three_failures_kills():
    from unittest.mock import patch
    c = Character(name="Hero", max_hp=20, hp=0, death_save_failures=2,
                  conditions=["unconscious"])
    with patch.object(rules, "roll", return_value=_make_roll_result(5)):
        res = rules.roll_death_save(c)
    assert res["result_kind"] == "dead"
    assert c.dead is True
    # On death the unconscious tag is dropped for a 'dead' tag so /state and the
    # model snapshot stop reporting a corpse as merely unconscious.
    assert "unconscious" not in c.conditions
    assert "dead" in c.conditions




def test_death_by_damage_while_down_clears_unconscious():
    """A PC who dies from the third failed death save then takes a killing hit —
    or any damage-while-down death — must shed 'unconscious' for 'dead'."""
    c = Character(name="Hero", max_hp=20, hp=0, death_save_failures=2,
                  conditions=["unconscious"])
    res = rules.apply_damage(c, 3)  # one more failure -> 3 -> dead
    assert res["dead"] is True
    assert c.dead is True
    assert "unconscious" not in c.conditions
    assert "dead" in c.conditions




def test_death_by_massive_damage_clears_unconscious():
    c = Character(name="Hero", max_hp=20, hp=0, conditions=["unconscious"])
    res = rules.apply_damage(c, 20)  # >= max_hp while down -> instant death
    assert res["dead"] is True
    assert c.dead is True
    assert "unconscious" not in c.conditions
    assert "dead" in c.conditions




def test_death_save_refuses_conscious_pc():
    c = Character(name="Hero", max_hp=20, hp=10)
    res = rules.roll_death_save(c)
    assert res["ok"] is False
    assert res["reason"] == "not_dying"




def test_death_save_refuses_stable_pc():
    c = Character(name="Hero", max_hp=20, hp=0, stable=True)
    res = rules.roll_death_save(c)
    assert res["ok"] is False
    assert res["reason"] == "not_dying"




def test_death_save_refuses_dead_pc():
    c = Character(name="Hero", max_hp=20, hp=0, dead=True)
    res = rules.roll_death_save(c)
    assert res["ok"] is False
    assert res["reason"] == "not_dying"




def test_heal_resets_death_save_state():
    c = Character(name="Hero", max_hp=20, hp=0,
                  death_save_failures=2, death_save_successes=1,
                  stable=False, conditions=["unconscious"])
    res = rules.heal(c, 5)
    assert res["ok"] is True
    assert c.hp == 5
    assert c.death_save_successes == 0
    assert c.death_save_failures == 0
    assert c.stable is False
    assert "unconscious" not in c.conditions




def test_heal_dead_pc_is_noop():
    c = Character(name="Hero", max_hp=20, hp=0, dead=True)
    res = rules.heal(c, 10)
    assert res["ok"] is True
    assert res["healed"] == 0
    assert res["hp"] == 0
    assert c.hp == 0
    assert c.dead is True
    assert "note" in res




def test_heal_npc_unaffected_by_death_save_fields():
    """NPC heal still works; NPCs have no death_save_failures attr."""
    npc = NPC(name="Goblin", max_hp=12, hp=0)
    res = rules.heal(npc, 5)
    assert res["ok"] is True
    assert npc.hp == 5
    assert not hasattr(npc, "death_save_failures")




# --- Stage 2: next_turn / death-save dispatch / _pc_turn_decision ---------------

def _make_npc_dying_conscious_state():
    """Returns a state with combat_order [snik (NPC), wisp (dying PC), aldric (conscious PC)]
    at combat_index=0 (snik's turn), ready for next_turn to advance."""
    gs = GameState(location="Arena")
    gs.npcs["snik"] = NPC(name="Snik", ability_modifiers={"dex": 0})
    gs.party["wisp"] = Character(name="Wisp", ability_modifiers={"dex": 0}, hp=0)  # is_dying
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    gs.combat_order = ["snik", "wisp", "aldric"]
    gs.combat_index = 0
    gs.combat_round = 1
    gs.combat_initiatives = {"snik": 15, "wisp": 10, "aldric": 5}
    return gs




def test_pc_turn_decision_dying():
    pc = Character(name="Hero", hp=0)
    assert tools._pc_turn_decision(pc) == "roll"




def test_pc_turn_decision_conscious():
    pc = Character(name="Hero", hp=10, max_hp=10)
    assert tools._pc_turn_decision(pc) == "break"




def test_pc_turn_decision_stable():
    pc = Character(name="Hero", hp=0, stable=True)
    assert tools._pc_turn_decision(pc) == "skip"




def test_pc_turn_decision_dead():
    pc = Character(name="Hero", hp=0, dead=True)
    assert tools._pc_turn_decision(pc) == "skip"




def test_next_turn_stops_at_dying_pc():
    """next_turn stops at a dying PC (hp=0, not dead/stable) — does not skip it."""
    gs = _make_npc_dying_conscious_state()
    res = tools.dispatch("next_turn", {}, gs)
    assert res["ok"] is True
    assert res["active"] == "wisp"
    assert "skipped_downed" not in res




def test_next_turn_skips_stable_pc_to_conscious():
    """A stable PC is skipped by next_turn; the next conscious PC becomes active."""
    gs = _make_npc_dying_conscious_state()
    gs.party["wisp"].stable = True
    res = tools.dispatch("next_turn", {}, gs)
    assert res["ok"] is True
    assert res["active"] == "aldric"
    assert "wisp" in res.get("skipped_downed", [])




def test_next_turn_skips_dead_pc_to_conscious():
    """A dead PC is skipped by next_turn; the next conscious PC becomes active."""
    gs = _make_npc_dying_conscious_state()
    gs.party["wisp"].dead = True
    res = tools.dispatch("next_turn", {}, gs)
    assert res["ok"] is True
    assert res["active"] == "aldric"
    assert "wisp" in res.get("skipped_downed", [])




def test_next_turn_still_skips_downed_npc():
    """A downed NPC is still skipped (NPC skip behavior unchanged)."""
    gs = GameState(location="Arena")
    gs.npcs["snik"] = NPC(name="Snik", hp=0)     # downed NPC
    gs.party["aldric"] = Character(name="Aldric") # conscious PC
    gs.combat_order = ["snik", "aldric"]
    gs.combat_index = 1   # aldric is active; next advances → wraps → snik (skip) → aldric
    gs.combat_round = 1
    gs.combat_initiatives = {"snik": 15, "aldric": 5}
    res = tools.dispatch("next_turn", {}, gs)
    # wraps past snik (downed NPC, skip) to aldric round 2
    assert res["ok"] is True
    assert res["active"] == "aldric"
    assert "snik" in res.get("skipped_downed", [])




def test_dispatch_roll_death_save_non_dying_rejected():
    """roll_death_save dispatch on a conscious PC returns ok=False reason 'not_dying'."""
    gs = GameState(location="Test")
    gs.party["hero"] = Character(name="Hero", hp=10)
    res = tools.dispatch("roll_death_save", {"character": "Hero"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "not_dying"




def test_dispatch_roll_death_save_on_dying_pc():
    """roll_death_save dispatch on a dying PC calls rules.roll_death_save and records the result."""
    from unittest.mock import patch
    gs = GameState(location="Test")
    gs.party["hero"] = Character(name="Hero", max_hp=20, hp=0)
    log_len = len(gs.log)
    with patch.object(rules, "roll", return_value=_make_roll_result(15)):
        res = tools.dispatch("roll_death_save", {"character": "Hero"}, gs)
    assert res["ok"] is True
    assert res["result_kind"] == "success"
    assert res["roll"] == 15
    assert len(gs.log) == log_len + 1   # record() was called




def test_combat_loop_rolls_death_save_for_dying_pc():
    """When the NPC loop lands on a dying PC, the engine dispatches roll_death_save
    automatically, appends a death_save beat, and the loop continues past that PC."""
    from unittest.mock import MagicMock, patch
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    # Order: aldric (active, has acted) → wisp (dying) → snik (NPC) → [back to aldric]
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100}, hp=10)
    gs.party["wisp"]   = Character(name="Wisp",   ability_modifiers={"dex": 0},   hp=0)  # dying
    gs.npcs["snik"]    = NPC(name="Snik", max_hp=20, hp=20, ability_modifiers={"dex": -100})

    tools.dispatch("start_combat", {"combatants": ["aldric", "wisp", "snik"]}, gs)
    assert gs.combat_order == ["aldric", "wisp", "snik"]

    fake_text = MagicMock(); fake_text.type = "text"; fake_text.text = "Narration."
    fake_resp = MagicMock(); fake_resp.stop_reason = "end_turn"; fake_resp.content = [fake_text]
    fake_client = MagicMock(); fake_client.messages.create.return_value = fake_resp

    agent = DMAgent(gs, client=fake_client)
    gs.action_used = True  # aldric has acted

    # Force a failure roll so wisp stays dying (not revived)
    with patch.object(rules, "roll", return_value=_make_roll_result(5)):
        agent.take_turn("Aldric attacks Snik")

    death_saves = [c for c in agent.tool_trace if c["name"] == "roll_death_save"]
    assert len(death_saves) == 1
    assert death_saves[0]["result"]["result_kind"] == "failure"

    # After wisp's save (still dying), loop must advance to snik (NPC) and prompt aldric again
    npc_execs = [c for c in agent.tool_trace if c["name"] == "next_turn"]
    # next_turn calls: aldric→wisp, wisp→snik, snik→aldric (3 calls)
    assert any(r["result"]["active"] == "wisp" for r in npc_execs)
    assert any(r["result"]["active"] == "snik" for r in npc_execs)




# --- Stage 3: damage while down ------------------------------------------------

def test_apply_damage_dying_pc_adds_one_failure():
    c = Character(name="Hero", max_hp=20, hp=0)
    res = rules.apply_damage(c, 5)
    assert res["death_save_failure"] is True
    assert res["death_save_failures"] == 1
    assert c.death_save_failures == 1
    assert c.dead is False




def test_apply_damage_dying_pc_crit_adds_two_failures():
    c = Character(name="Hero", max_hp=20, hp=0)
    res = rules.apply_damage(c, 5, from_crit=True)
    assert c.death_save_failures == 2
    assert res["death_save_failures"] == 2
    assert c.dead is False




def test_apply_damage_dying_pc_three_failures_kills():
    c = Character(name="Hero", max_hp=20, hp=0, death_save_failures=2)
    res = rules.apply_damage(c, 5)
    assert c.death_save_failures == 3
    assert c.dead is True
    assert res["dead"] is True




def test_apply_damage_dying_pc_crit_caps_at_three():
    c = Character(name="Hero", max_hp=20, hp=0, death_save_failures=2)
    res = rules.apply_damage(c, 5, from_crit=True)
    assert c.death_save_failures == 3   # capped, not 4
    assert c.dead is True




def test_apply_damage_stable_pc_re_enters_dying_and_adds_failure():
    c = Character(name="Hero", max_hp=20, hp=0, stable=True)
    res = rules.apply_damage(c, 5)
    assert c.stable is False
    assert c.is_dying is True
    assert c.death_save_failures == 1
    assert res["death_save_failure"] is True




def test_apply_damage_stable_pc_crit_clears_stable_and_two_failures():
    c = Character(name="Hero", max_hp=20, hp=0, stable=True)
    rules.apply_damage(c, 5, from_crit=True)
    assert c.stable is False
    assert c.death_save_failures == 2




def test_apply_damage_conscious_to_zero_no_death_save_failure():
    c = Character(name="Hero", max_hp=20, hp=5, conditions=[])
    res = rules.apply_damage(c, 8)
    assert c.hp == 0
    assert c.is_dying is True
    assert c.death_save_failures == 0
    assert "death_save_failure" not in res
    assert "unconscious" in c.conditions




def test_apply_damage_crit_conscious_to_zero_no_failure():
    c = Character(name="Hero", max_hp=20, hp=5, conditions=[])
    res = rules.apply_damage(c, 8, from_crit=True)
    assert c.hp == 0
    assert c.is_dying is True
    assert c.death_save_failures == 0
    assert "death_save_failure" not in res
    assert "unconscious" in c.conditions




def test_apply_damage_massive_hit_while_down_instant_death():
    c = Character(name="Hero", max_hp=20, hp=0, death_save_failures=0)
    res = rules.apply_damage(c, 20)  # exactly max_hp — instant death
    assert c.dead is True
    assert res["dead"] is True
    assert c.death_save_failures == 0  # killed by damage, not accumulated saves




def test_apply_damage_npc_at_zero_unchanged():
    npc = NPC(name="Goblin", max_hp=12, hp=0)
    res = rules.apply_damage(npc, 5)
    assert res["ok"] is True
    assert npc.hp == 0
    assert "death_save_failure" not in res
    assert not hasattr(npc, "death_save_failures")




def test_attack_nat20_on_dying_pc_adds_two_failures():
    from unittest.mock import patch
    attacker = Character(
        name="A", proficiency_bonus=2, inventory=["mace"],
        ability_modifiers={"str": 2},
    )
    defender = Character(name="B", max_hp=20, hp=0, conditions=["unconscious"])
    # randint calls: d20=20 (crit), then 1d6=3 (damage die)
    with patch.object(rules._rng, "randint", side_effect=[20, 3]):
        res = rules.attack(attacker, defender, weapon="mace")
    assert res["critical"] is True
    assert res["hit"] is True
    assert defender.death_save_failures == 2




def test_attack_non_crit_on_dying_pc_adds_one_failure():
    from unittest.mock import patch
    attacker = Character(
        name="A", proficiency_bonus=2, inventory=["mace"],
        ability_modifiers={"str": 2},
    )
    defender = Character(name="B", max_hp=20, hp=0, ac=1, conditions=["unconscious"])
    # d20=10 (hit vs AC 1), then 1d6=4
    with patch.object(rules._rng, "randint", side_effect=[10, 4]):
        res = rules.attack(attacker, defender, weapon="mace")
    assert res["hit"] is True
    assert res["critical"] is False
    assert defender.death_save_failures == 1




def test_cast_spell_crit_on_dying_pc_adds_two_failures():
    from unittest.mock import patch
    caster = Character(
        name="C", level=1, proficiency_bonus=2,
        spellcasting_ability="wis",
        ability_modifiers={"wis": 3},
        spell_slots={1: 1},
        spells=["guiding_bolt"],
    )
    target = Character(name="T", max_hp=20, hp=0, ac=10, conditions=["unconscious"])
    # d20=20 (crit), then 4d6=[2,2,2,2]
    with patch.object(rules._rng, "randint", side_effect=[20, 2, 2, 2, 2]):
        res = rules.cast_damaging_spell(caster, target, "guiding_bolt", 1)
    assert res["critical"] is True
    assert target.death_save_failures == 2




def test_combat_loop_revived_pc_becomes_active():
    """A nat-20 death save revives the PC; the loop breaks and the closing prompt addresses them."""
    from unittest.mock import MagicMock, patch
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100}, hp=10)
    gs.party["wisp"]   = Character(name="Wisp",   ability_modifiers={"dex": 0},   hp=0)  # dying
    gs.npcs["snik"]    = NPC(name="Snik", max_hp=20, hp=20, ability_modifiers={"dex": -100})

    tools.dispatch("start_combat", {"combatants": ["aldric", "wisp", "snik"]}, gs)
    assert gs.combat_order == ["aldric", "wisp", "snik"]

    fake_text = MagicMock(); fake_text.type = "text"; fake_text.text = "Narration."
    fake_resp = MagicMock(); fake_resp.stop_reason = "end_turn"; fake_resp.content = [fake_text]
    fake_client = MagicMock(); fake_client.messages.create.return_value = fake_resp

    agent = DMAgent(gs, client=fake_client)
    gs.action_used = True

    with patch.object(rules, "roll", return_value=_make_roll_result(20)):
        narration = agent.take_turn("Aldric attacks Snik")

    # Wisp revived — pointer should be on wisp, hp=1
    assert gs.party["wisp"].hp == 1
    assert gs.combat_order[gs.combat_index] == "wisp"
    assert "Wisp, what do you do?" in narration

    death_saves = [c for c in agent.tool_trace if c["name"] == "roll_death_save"]
    assert len(death_saves) == 1
    assert death_saves[0]["result"]["result_kind"] == "revived"
