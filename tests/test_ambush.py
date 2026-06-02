"""Tests for attempt_ambush, surprise, and companions. Extracted from test_rules.py; the enforcement core
stays there. Run:  python -m pytest -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import rules, tools
from src.game_state import Character, NPC, GameState
from tests._helpers import _make_combat_state


def _ambush_state(*, combat_round: int = 0, two_members: bool = True):
    """Minimal state for ambush tests: party with two members (dex +2, +0) and
    two hostile NPCs with alertness_dc=12 each."""
    gs = GameState(location="Corridor")
    gs.party["rogue"] = Character(
        name="Rogue", max_hp=20, hp=20,
        ability_modifiers={"dex": 2},
    )
    if two_members:
        gs.party["fighter"] = Character(
            name="Fighter", max_hp=20, hp=20,
            ability_modifiers={"dex": 0},
        )
    gs.npcs["guard1"] = NPC(name="Guard1", hostile=True, alertness_dc=12)
    gs.npcs["guard2"] = NPC(name="Guard2", hostile=True, alertness_dc=12)
    if combat_round > 0:
        gs.combat_order = ["rogue", "guard1"]
        gs.combat_index = 0
        gs.combat_round = combat_round
    return gs


def test_attempt_ambush_success():
    """Both party members beat bar 12 → success=True, pending_ambush=True, ambush_attempted=True."""
    gs = _ambush_state()
    rules.force_rolls([15, 14])  # Rogue: 15+2=17>=12; Fighter: 14+0=14>=12
    res = tools.dispatch("attempt_ambush", {}, gs)
    assert res["ok"] is True
    assert res["success"] is True
    assert res["bar"] == 12
    assert gs.pending_ambush is True
    assert gs.ambush_attempted is True
    assert len(res["rolls"]) == 2
    assert all(r["success"] for r in res["rolls"])


def test_attempt_ambush_failure():
    """One party member misses bar → success=False, pending_ambush stays False."""
    gs = _ambush_state()
    rules.force_rolls([15, 3])  # Rogue: 17>=12 ok; Fighter: 3+0=3 < 12 fail
    res = tools.dispatch("attempt_ambush", {}, gs)
    assert res["ok"] is True
    assert res["success"] is False
    assert gs.pending_ambush is False
    assert gs.ambush_attempted is True


def test_attempt_ambush_bar_is_max_alertness():
    """With DCs 12 and 15, bar=15; roll that beats 12 but not 15 → fail."""
    gs = GameState(location="Corridor")
    gs.party["rogue"] = Character(name="Rogue", max_hp=20, hp=20, ability_modifiers={"dex": 2})
    gs.party["fighter"] = Character(name="Fighter", max_hp=20, hp=20, ability_modifiers={"dex": 0})
    gs.npcs["guard1"] = NPC(name="Guard1", hostile=True, alertness_dc=12)
    gs.npcs["guard2"] = NPC(name="Guard2", hostile=True, alertness_dc=15)
    # force rolls: Rogue 13+2=15>=15 ok; Fighter 13+0=13 < 15 fail → success=False
    rules.force_rolls([13, 13])
    res = tools.dispatch("attempt_ambush", {}, gs)
    assert res["ok"] is True
    assert res["bar"] == 15
    assert res["success"] is False


def test_attempt_ambush_cannot_ambush_when_any_always_alert():
    """Any hostile with alertness_dc=None → ok=False 'cannot_ambush', no rolls made,
    and the engine auto-starts combat (the alert foe spotted the party)."""
    gs = GameState(location="Corridor")
    gs.party["rogue"] = Character(name="Rogue", max_hp=20, hp=20, ability_modifiers={"dex": 2})
    gs.npcs["guard"] = NPC(name="Guard", hostile=True, alertness_dc=12)
    gs.npcs["sentinel"] = NPC(name="Sentinel", hostile=True, alertness_dc=None)
    res = tools.dispatch("attempt_ambush", {}, gs)
    assert res["ok"] is False
    assert res["reason"] == "cannot_ambush"
    # No surprise was possible, so combat starts as a fair fight.
    assert res["combat_started"] is True
    assert gs.combat_round == 1
    assert gs.pending_ambush is False  # never set — nobody is surprised
    assert all(not n.surprised for n in gs.npcs.values())
    assert gs.ambush_attempted is True  # consumed: the attempt tipped the party's hand


def test_attempt_ambush_no_target():
    """No living hostiles → ok=False 'no_target'."""
    gs = GameState(location="Corridor")
    gs.party["rogue"] = Character(name="Rogue", max_hp=20, hp=20)
    gs.npcs["guard"] = NPC(name="Guard", hostile=True, alertness_dc=12, hp=0)  # downed
    res = tools.dispatch("attempt_ambush", {}, gs)
    assert res["ok"] is False
    assert res["reason"] == "no_target"


def test_attempt_ambush_in_combat_rejected():
    """combat_round > 0 → ok=False 'in_combat'."""
    gs = _ambush_state(combat_round=1)
    res = tools.dispatch("attempt_ambush", {}, gs)
    assert res["ok"] is False
    assert res["reason"] == "in_combat"


def test_attempt_ambush_one_shot_per_scene():
    """Second attempt_ambush in same scene → ok=False 'already_attempted'."""
    gs = _ambush_state()
    rules.force_rolls([5, 5])  # fail first attempt
    tools.dispatch("attempt_ambush", {}, gs)
    assert gs.ambush_attempted is True
    res = tools.dispatch("attempt_ambush", {}, gs)
    assert res["ok"] is False
    assert res["reason"] == "already_attempted"


def test_start_combat_consumes_pending_ambush():
    """pending_ambush=True → after start_combat each hostile NPC has surprised=True,
    PCs do not, and pending_ambush is cleared."""
    gs = _ambush_state()
    gs.pending_ambush = True
    rules.seed(0)
    res = tools.dispatch("start_combat", {"combatants": ["rogue", "fighter", "guard1", "guard2"]}, gs)
    assert res["ok"] is True
    assert gs.pending_ambush is False
    assert gs.npcs["guard1"].surprised is True
    assert gs.npcs["guard2"].surprised is True
    assert gs.party["rogue"].surprised is False if hasattr(gs.party["rogue"], "surprised") else True
    assert "surprised" in res
    assert set(res["surprised"]) == {"Guard1", "Guard2"}


def test_start_combat_no_pending_ambush_no_surprise():
    """No pending_ambush → NPCs are not surprised after start_combat."""
    gs = _ambush_state()
    rules.seed(0)
    tools.dispatch("start_combat", {"combatants": ["rogue", "guard1"]}, gs)
    assert gs.npcs["guard1"].surprised is False


def test_next_turn_skips_surprised_npc_round1_clears_flag():
    """Round 1: next_turn skips a surprised NPC and clears its flag."""
    gs = GameState(location="Arena")
    gs.party["rogue"] = Character(name="Rogue", ability_modifiers={"dex": 100})
    gs.npcs["guard"] = NPC(name="Guard", max_hp=12, hp=12, ability_modifiers={"dex": -100})
    gs.npcs["guard"].surprised = True
    rules.seed(0)
    tools.dispatch("start_combat", {"combatants": ["rogue", "guard"]}, gs)
    gs.pending_ambush = False  # already consumed; don't set surprised twice
    # Reset guard surprised to True manually since start_combat would clear pending_ambush
    gs.npcs["guard"].surprised = True

    gs.combat_starting = False  # simulate take_turn clearing the barrier before next_turn
    # Advance from rogue (index 0) to guard (index 1, surprised) — should skip to rogue round 2
    adv = tools.dispatch("next_turn", {}, gs)
    assert adv["ok"] is True
    assert adv["active"] == "rogue"
    assert adv["round"] == 2
    assert gs.npcs["guard"].surprised is False  # flag cleared
    assert "guard" in adv.get("skipped_surprised", [])  # surprised, not down
    assert "skipped_downed" not in adv  # a healthy surprised NPC must not be reported as down


def test_next_turn_surprised_npc_acts_in_round2():
    """After the surprise round, the NPC's flag is gone and it is no longer skipped."""
    gs = GameState(location="Arena")
    gs.party["rogue"] = Character(name="Rogue", ability_modifiers={"dex": 100})
    gs.npcs["guard"] = NPC(name="Guard", max_hp=12, hp=12, ability_modifiers={"dex": -100})
    rules.seed(0)
    tools.dispatch("start_combat", {"combatants": ["rogue", "guard"]}, gs)
    gs.npcs["guard"].surprised = True  # manually flag for this test

    gs.combat_starting = False  # simulate take_turn clearing the barrier before next_turn
    # Round 1 skip: advance → skip guard → back to rogue, round 2
    adv1 = tools.dispatch("next_turn", {}, gs)
    assert adv1["round"] == 2
    assert gs.npcs["guard"].surprised is False

    # Round 2: advance from rogue → guard; guard is NOT skipped now
    adv2 = tools.dispatch("next_turn", {}, gs)
    assert adv2["ok"] is True
    assert adv2["active"] == "guard"
    assert adv2["round"] == 2
    assert "skipped_downed" not in adv2


def test_surprised_hostile_counts_for_maybe_end_combat():
    """A surprised (alive, hostile) NPC prevents _maybe_end_combat from ending the fight."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(location="Arena")
    gs.party["rogue"] = Character(name="Rogue", max_hp=20, hp=20, ability_modifiers={"dex": 100})
    gs.npcs["guard"] = NPC(name="Guard", max_hp=12, hp=12, hostile=True, ability_modifiers={"dex": -100})
    rules.seed(0)
    tools.dispatch("start_combat", {"combatants": ["rogue", "guard"]}, gs)
    gs.npcs["guard"].surprised = True  # manually flag

    agent = DMAgent(gs, client=MagicMock())
    ended = agent._maybe_end_combat()
    assert ended is False
    assert gs.combat_round == 1  # combat continues


def test_surprised_hostile_is_valid_offensive_target():
    """A surprised NPC is returned as an auto-target candidate."""
    gs = GameState(location="Arena")
    gs.party["rogue"] = Character(name="Rogue", max_hp=20, hp=20, ability_modifiers={"dex": 100},
                                  inventory=["dagger"], proficiency_bonus=2)
    gs.npcs["guard"] = NPC(name="Guard", max_hp=12, hp=12, hostile=True, ac=1, ability_modifiers={"dex": -100})
    rules.seed(0)
    tools.dispatch("start_combat", {"combatants": ["rogue", "guard"]}, gs)
    gs.combat_starting = False
    gs.npcs["guard"].surprised = True

    target, err, auto = tools._resolve_offensive_target("", gs, exclude_name="Rogue")
    assert err is None
    assert target is not None
    assert target.name == "Guard"


def test_move_scene_resets_ambush_flags():
    """Successful scene change resets pending_ambush and ambush_attempted to False."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scene_loot_quest_item.json")
    gs = GameState.load(path)
    gs.pending_ambush = True
    gs.ambush_attempted = True
    res = tools.dispatch("move_scene", {"scene_key": "ember_chamber"}, gs)
    assert res["ok"] is True
    assert gs.pending_ambush is False
    assert gs.ambush_attempted is False


def test_move_scene_free_form_resets_ambush_flags():
    """Free-form move_scene also resets both flags."""
    gs = GameState(location="Start")
    gs.pending_ambush = True
    gs.ambush_attempted = True
    res = tools.dispatch("move_scene", {"location": "New Room"}, gs)
    assert res["ok"] is True
    assert gs.pending_ambush is False
    assert gs.ambush_attempted is False


def test_end_combat_clears_surprised_on_surviving_npc():
    """end_combat sets surprised=False on all NPCs, even survivors."""
    gs = GameState(location="Arena")
    gs.party["rogue"] = Character(name="Rogue", max_hp=20, hp=20)
    gs.npcs["guard"] = NPC(name="Guard", max_hp=12, hp=12, hostile=True)
    gs.npcs["guard"].surprised = True
    gs.combat_order = ["rogue", "guard"]
    gs.combat_index = 0
    gs.combat_round = 1
    tools.dispatch("end_combat", {}, gs)
    assert gs.npcs["guard"].surprised is False


def test_ambush_npc_defaults_on_old_save():
    """Old saves without alertness_dc/surprised load fine (defaults kick in)."""
    d = {
        "location": "Test",
        "npcs": {"snik": {"name": "Snik", "hp": 10, "max_hp": 10, "hostile": True}},
    }
    gs = GameState.from_dict(d)
    assert gs.npcs["snik"].alertness_dc is None
    assert gs.npcs["snik"].surprised is False


def test_recruit_npc_marks_companion():
    """A non-hostile, present NPC becomes a companion."""
    gs = GameState(location="Camp")
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20)
    gs.npcs["brak"] = NPC(name="Brak", max_hp=12, hp=12, hostile=False)
    res = tools.dispatch("recruit_npc", {"npc": "Brak"}, gs)
    assert res["ok"] is True and res["companion"] is True
    assert gs.npcs["brak"].companion is True


def test_recruit_npc_rejected_while_hostile():
    """Can't recruit a still-hostile NPC — must de-escalate first."""
    gs = GameState(location="Camp")
    gs.npcs["snik"] = NPC(name="Snik", max_hp=10, hp=10, hostile=True)
    res = tools.dispatch("recruit_npc", {"npc": "Snik"}, gs)
    assert res["ok"] is False and res["reason"] == "hostile"
    assert gs.npcs["snik"].companion is False


def test_recruit_npc_rejected_in_combat():
    """Recruiting happens between fights, not mid-combat."""
    gs = GameState(location="Camp", combat_round=1)
    gs.npcs["brak"] = NPC(name="Brak", max_hp=12, hp=12, hostile=False)
    res = tools.dispatch("recruit_npc", {"npc": "Brak"}, gs)
    assert res["ok"] is False and res["reason"] == "in_combat"


def test_companion_follows_across_scene():
    """A companion travels with the party through move_scene into the next scene."""
    gs = GameState(
        current_scene="hall",
        scenes={
            "hall": {"location": "Hall", "scene": "s", "npcs": {}, "exits": {"north": "vault"}},
            "vault": {"location": "Vault", "scene": "s", "npcs": {"guard": {"name": "Guard", "hp": 8, "max_hp": 8, "hostile": True}}},
        },
    )
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20)
    gs.npcs["brak"] = NPC(name="Brak", max_hp=12, hp=12, hostile=False, companion=True)

    res = tools.dispatch("move_scene", {"scene_key": "vault"}, gs)
    assert res["ok"] is True
    assert "Brak" in res.get("companions", [])
    names = {n.name for n in gs.npcs.values()}
    assert "Brak" in names      # companion came along
    assert "Guard" in names     # plus the new scene's own NPC


def test_companion_attacks_hostile_on_its_turn():
    """resolve_npc_action makes a companion attack the lowest-HP living hostile."""
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20)
    gs.npcs["brak"] = NPC(name="Brak", max_hp=12, hp=12, hostile=False, companion=True,
                          attack_bonus=8, inventory=["shortsword"])
    gs.npcs["snik"] = NPC(name="Snik", max_hp=10, hp=10, hostile=True)
    # Companion is the active combatant.
    gs.combat_order = ["brak", "snik", "aldric"]
    gs.combat_index = 0
    gs.combat_round = 1
    gs.action_used = False

    rules.seed(0)
    resolution = tools.resolve_npc_action(gs.npcs["brak"], gs)
    assert resolution is not None
    args, result = resolution
    assert args["attacker"] == "Brak"
    assert args["defender"] == "Snik"   # attacked the hostile, not the party
    assert result["ok"] is True


def test_plain_non_hostile_npc_still_stands_aside():
    """A non-hostile NPC that is NOT a companion takes no action (regression)."""
    gs = GameState(location="Arena")
    gs.npcs["bystander"] = NPC(name="Bystander", max_hp=8, hp=8, hostile=False)
    gs.npcs["snik"] = NPC(name="Snik", max_hp=10, hp=10, hostile=True)
    assert tools.resolve_npc_action(gs.npcs["bystander"], gs) is None


def test_state_snapshot_shows_surprised_not_alertness_dc():
    """_state_snapshot surfaces surprised=True but never alertness_dc."""
    import json as _json
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(location="Corridor")
    gs.party["rogue"] = Character(name="Rogue", max_hp=20, hp=20)
    gs.npcs["guard"] = NPC(name="Guard", max_hp=12, hp=12, hostile=True,
                           alertness_dc=14, surprised=True)

    agent = DMAgent(gs, client=MagicMock())
    snap = _json.loads(agent._state_snapshot())

    guard_entry = snap["npcs"]["Guard"]
    assert guard_entry.get("surprised") is True
    assert "alertness_dc" not in guard_entry


def test_state_snapshot_omits_surprised_when_false():
    """_state_snapshot does not include 'surprised' when it is False."""
    import json as _json
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(location="Corridor")
    gs.party["rogue"] = Character(name="Rogue", max_hp=20, hp=20)
    gs.npcs["guard"] = NPC(name="Guard", max_hp=12, hp=12, hostile=True, surprised=False)

    agent = DMAgent(gs, client=MagicMock())
    snap = _json.loads(agent._state_snapshot())

    assert "surprised" not in snap["npcs"]["Guard"]


def test_leading_npc_surprised_skipped_in_take_turn():
    """When start_combat places a surprised NPC first, take_turn must skip its action
    in round 1 and halt at the first conscious PC without calling NPC attack tools."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    # Force NPC first via extreme dex
    gs.npcs["guard"] = NPC(name="Guard", max_hp=12, hp=12, hostile=True,
                           ability_modifiers={"dex": 100}, alertness_dc=10)
    gs.party["rogue"] = Character(name="Rogue", max_hp=20, hp=20,
                                  ability_modifiers={"dex": -100})

    # Manually start combat with guard first, mark it surprised
    tools.dispatch("start_combat", {"combatants": ["guard", "rogue"]}, gs)
    assert gs.combat_order[0] == "guard"
    gs.npcs["guard"].surprised = True

    # Fake client: no tool calls — start_combat was already called
    fake_text = MagicMock(); fake_text.type = "text"; fake_text.text = "Ready."
    fake_resp = MagicMock(); fake_resp.stop_reason = "end_turn"; fake_resp.content = [fake_text]
    fake_client = MagicMock(); fake_client.messages.create.return_value = fake_resp

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("We sneak up and attack")

    # Engine must skip the surprised guard and prompt the rogue
    assert gs.npcs["guard"].surprised is False   # flag cleared
    assert "Rogue, " in narration or "Rogue," in narration  # closing prompt names rogue

    # No attack tool fired for guard (it was surprised)
    attack_calls = [c for c in agent.tool_trace if c["name"] == "attack" and
                    (c["input"].get("attacker") or "").lower() == "guard"]
    assert attack_calls == [], f"Surprised guard must not attack in round 1; got {attack_calls}"
