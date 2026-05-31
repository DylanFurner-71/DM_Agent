"""Tests for the enforcement core. These run with no network/API needed and are
the proof that the rules are enforced in code, not by model goodwill.

Run:  python -m pytest -q     (or)     python tests/test_rules.py
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import rules, tools
from src.game_state import Character, NPC, GameState


def test_spell_slots_run_out():
    wisp = Character(name="Wisp", spell_slots={1: 1})
    first = rules.cast_spell(wisp, 1)
    assert first["ok"] is True
    assert first["slots_remaining"] == 0
    # Second cast must be refused in code — this is the money shot.
    second = rules.cast_spell(wisp, 1)
    assert second["ok"] is False
    assert "no level-1 spell slots" in second["reason"]


def test_cantrips_are_free():
    c = Character(name="C", spell_slots={})
    for _ in range(5):
        assert rules.cast_spell(c, 0)["ok"] is True


def test_damage_downs_and_clamps():
    n = NPC(name="Goblin", max_hp=8, hp=8)
    res = rules.apply_damage(n, 100)
    assert res["hp"] == 0  # clamped, never negative
    assert res["downed"] is True


def test_heal_clamps_to_max_and_revives():
    c = Character(name="Hero", max_hp=20, hp=0, conditions=["unconscious"])
    res = rules.heal(c, 50)
    assert res["hp"] == 20  # cannot exceed max
    assert "unconscious" not in c.conditions


def test_weapon_resolves_damage_and_to_hit():
    """Naming a weapon derives dice, damage_type, and to-hit from inventory + stats."""
    rules.seed(0)
    atk = Character(name="A", attack_bonus=0, inventory=["mace"],
                    ability_modifiers={"str": 3}, proficiency_bonus=2)
    dfn = NPC(name="D", ac=1, hp=30, max_hp=30)
    res = rules.attack(atk, dfn, weapon="mace")
    assert res["ok"] is True
    assert res["weapon"] == "mace"
    assert res["damage_type"] == "bludgeoning"
    assert res["to_hit_bonus"] == 5          # str(3) + proficiency(2)
    if res["hit"]:
        assert "1d6+3" in res["damage_detail"]  # damage = ability_mod only


def test_finesse_weapon_uses_dex_when_higher():
    """Finesse weapon (rapier) uses Dex for both to-hit and damage when Dex > Str."""
    rules.seed(0)
    atk = Character(name="Rogue", attack_bonus=0, inventory=["rapier"],
                    ability_modifiers={"str": 1, "dex": 5}, proficiency_bonus=2)
    dfn = NPC(name="D", ac=1, hp=30, max_hp=30)
    res = rules.attack(atk, dfn, weapon="rapier")
    assert res["weapon"] == "rapier"
    assert res["damage_type"] == "piercing"
    assert res["to_hit_bonus"] == 7          # dex(5) + proficiency(2)
    if res["hit"]:
        assert "1d8+5" in res["damage_detail"]


def test_finesse_weapon_uses_str_when_str_higher():
    """Finesse weapon falls back to Str when Str >= Dex."""
    rules.seed(0)
    atk = Character(name="A", attack_bonus=0, inventory=["dagger"],
                    ability_modifiers={"str": 4, "dex": 1}, proficiency_bonus=2)
    dfn = NPC(name="D", ac=1, hp=20, max_hp=20)
    res = rules.attack(atk, dfn, weapon="dagger")
    assert res["to_hit_bonus"] == 6          # str(4) + proficiency(2)
    if res["hit"]:
        assert "1d4+4" in res["damage_detail"]


def test_no_weapon_arg_uses_attack_bonus_fallback():
    """No weapon argument → falls back to attack_bonus + 1d6 (NPC / unarmed path)."""
    rules.seed(0)
    atk = Character(name="A", attack_bonus=3)
    dfn = NPC(name="D", ac=1, hp=20, max_hp=20)
    res = rules.attack(atk, dfn)
    assert "weapon" not in res
    assert res["to_hit_bonus"] == 3
    if res["hit"]:
        assert res["damage_detail"].startswith("1d6")


def test_attack_rejects_weapon_not_in_inventory():
    """ok=False with the requested weapon AND the attacker's actual weapons listed."""
    atk = Character(name="Wisp", attack_bonus=0, inventory=["dagger"])
    dfn = NPC(name="D", ac=1, hp=20, max_hp=20)
    res = rules.attack(atk, dfn, weapon="mace")
    assert res["ok"] is False
    assert "mace" in res["error"]       # requested weapon named
    assert "Wisp" in res["error"]       # attacker named
    assert "dagger" in res["error"]     # available weapon listed


def test_attack_rejects_weapon_not_in_inventory_no_weapons():
    """When the attacker carries no weapons at all the error says 'available: none'."""
    atk = Character(name="Wisp", attack_bonus=0, inventory=["rope", "torch"])
    dfn = NPC(name="D", ac=1, hp=20, max_hp=20)
    res = rules.attack(atk, dfn, weapon="mace")
    assert res["ok"] is False
    assert "none" in res["error"]


def test_attack_rejects_unknown_weapon():
    """Specifying a weapon name absent from WEAPONS returns ok=False."""
    atk = Character(name="A", attack_bonus=0, inventory=["banana"])
    dfn = NPC(name="D", ac=1, hp=20, max_hp=20)
    res = rules.attack(atk, dfn, weapon="banana")
    assert res["ok"] is False
    assert "banana" in res["error"]


def test_attack_respects_ac_deterministically():
    rules.seed(1)  # fixed rolls for reproducibility
    atk = Character(name="A", attack_bonus=0)
    high_ac = NPC(name="Wall", ac=99, hp=50)
    low_ac = NPC(name="Sack", ac=1, hp=50)
    assert rules.attack(atk, high_ac)["hit"] is False  # cannot beat AC 99
    assert rules.attack(atk, low_ac)["hit"] is True     # always beats AC 1


def test_dice_notation_parsing():
    rules.seed(42)
    r = rules.roll("2d6+3")
    assert len(r.rolls) == 2
    assert r.total == sum(r.rolls) + 3
    for bad in ["", "d", "2x6", "1d1"]:
        try:
            rules.roll(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


def test_state_round_trips_through_json(tmp_path=None):
    gs = GameState(location="Crypt")
    gs.party["w"] = Character(name="Wisp", spell_slots={1: 2, 2: 1}, inventory=["dagger"])
    gs.npcs["g"] = NPC(name="Goblin")
    gs.quest_flags["door_open"] = True
    restored = GameState.from_dict(gs.to_dict())
    assert restored.location == "Crypt"
    assert restored.party["w"].spell_slots == {1: 2, 2: 1}  # int keys preserved
    assert restored.npcs["g"].name == "Goblin"
    assert restored.quest_flags["door_open"] is True


def test_combat_state_round_trips_through_json():
    gs = GameState(location="Arena")
    gs.party["w"] = Character(name="Wisp")
    gs.npcs["g"] = NPC(name="Goblin")
    gs.combat_order = ["w", "g"]
    gs.combat_index = 1
    gs.combat_round = 3
    restored = GameState.from_dict(gs.to_dict())
    assert restored.combat_order == ["w", "g"]
    assert restored.combat_index == 1
    assert restored.combat_round == 3


def test_roll_initiative_returns_all_keys_including_npcs():
    rules.seed(0)
    a = Character(name="Aldric", ability_modifiers={"dex": 0})
    b = NPC(name="Snik")  # no ability_modifiers → dex treated as 0
    result = rules.roll_initiative({"aldric": a, "snik": b})
    assert sorted(result) == ["aldric", "snik"]
    assert len(result) == 2


def test_roll_initiative_highest_total_first():
    rules.seed(0)
    # +100 dex → total ≥ 101; -100 dex → total ≤ -80. No overlap possible.
    fast = Character(name="Fast", ability_modifiers={"dex": 100})
    slow = Character(name="Slow", ability_modifiers={"dex": -100})
    result = rules.roll_initiative({"fast": fast, "slow": slow})
    assert result == ["fast", "slow"]


def test_roll_initiative_dex_breaks_total_tie():
    import random as _stdlib
    # Preview what seed=77 produces for two sequential 1d20 rolls, then assign
    # Dex modifiers that force equal totals so the tie-breaker (higher Dex) decides.
    preview = _stdlib.Random(77)
    roll_a, roll_b = preview.randint(1, 20), preview.randint(1, 20)
    # dex_a=5; set dex_b so that roll_a+dex_a == roll_b+dex_b (equal totals).
    dex_a, dex_b = 5, 5 + (roll_a - roll_b)
    # Whichever combatant has the higher Dex modifier should come first.
    expected_first = "a" if dex_a >= dex_b else "b"
    rules.seed(77)
    ca = Character(name="A", ability_modifiers={"dex": dex_a})
    cb = Character(name="B", ability_modifiers={"dex": dex_b})
    result = rules.roll_initiative({"a": ca, "b": cb})
    assert result[0] == expected_first


def test_combat_defaults_to_not_in_combat():
    gs = GameState.from_dict({"location": "Town"})  # old save without combat fields
    assert gs.combat_order == []
    assert gs.combat_index == 0
    assert gs.combat_round == 0


# --- combat dispatch tests (no API) ----------------------------------------

def _make_combat_state():
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    gs.party["wisp"] = Character(name="Wisp", ability_modifiers={"dex": 2},
                                 spells=["magic_missile"])
    gs.npcs["snik"] = NPC(name="Snik", ability_modifiers={"dex": 1})
    return gs


def test_start_combat_sets_order_and_round():
    rules.seed(0)
    gs = _make_combat_state()
    res = tools.dispatch("start_combat", {"combatants": ["aldric", "wisp", "snik"]}, gs)
    assert res["ok"] is True
    assert set(res["combat_order"]) == {"aldric", "wisp", "snik"}
    assert len(res["combat_order"]) == 3
    assert gs.combat_round == 1
    assert gs.combat_index == 0
    assert gs.combat_order == res["combat_order"]
    assert res["active"] == gs.combat_order[0]


def test_start_combat_rejects_unknown_key():
    gs = _make_combat_state()
    res = tools.dispatch("start_combat", {"combatants": ["aldric", "nobody"]}, gs)
    assert res["ok"] is False
    assert "nobody" in res["error"]


def test_next_turn_advances_pointer():
    rules.seed(0)
    gs = _make_combat_state()
    tools.dispatch("start_combat", {"combatants": ["aldric", "wisp"]}, gs)
    second = gs.combat_order[1]
    res = tools.dispatch("next_turn", {}, gs)
    assert res["ok"] is True
    assert res["active"] == second
    assert gs.combat_index == 1
    assert res["round"] == 1


def test_next_turn_wraps_and_increments_round():
    rules.seed(0)
    gs = _make_combat_state()
    tools.dispatch("start_combat", {"combatants": ["aldric", "wisp"]}, gs)
    first = gs.combat_order[0]
    tools.dispatch("next_turn", {}, gs)       # index 0 → 1
    res = tools.dispatch("next_turn", {}, gs)  # index 1 → wraps to 0, round 2
    assert res["ok"] is True
    assert res["active"] == first
    assert gs.combat_index == 0
    assert res["round"] == 2
    assert gs.combat_round == 2


def test_next_turn_without_combat_errors():
    gs = _make_combat_state()
    res = tools.dispatch("next_turn", {}, gs)
    assert res["ok"] is False


def test_damaging_spell_rolled_equals_applied():
    """Invariant: the engine's rolled damage total must exactly equal the HP removed.
    The model must never add a modifier by hand — rolled == applied, always."""
    rules.seed(7)
    gs = _make_combat_state()
    gs.party["wisp"].spell_slots = {1: 2}
    # Give Snik enough HP that no 3d4+3 roll (max 15) causes overkill clamping,
    # keeping the rolled == applied invariant clean.
    gs.npcs["snik"].hp = gs.npcs["snik"].max_hp = 30
    snik_hp_before = gs.npcs["snik"].hp

    res = tools.dispatch("cast_spell", {
        "caster": "Wisp",
        "spell_name": "magic_missile",
        "target": "Snik",
        "spell_level": 1,
    }, gs)

    assert res["ok"] is True
    hp_removed = snik_hp_before - gs.npcs["snik"].hp
    assert hp_removed == res["damage"]          # rolled == applied
    assert "3d4+3" in res["damage_detail"]      # full expression in trace
    assert res["slots_remaining"] == 1          # slot was consumed


def test_damaging_spell_no_slot_fails():
    gs = _make_combat_state()
    gs.party["wisp"].spell_slots = {1: 0}
    snik_hp_before = gs.npcs["snik"].hp
    res = tools.dispatch("cast_spell", {
        "caster": "Wisp",
        "spell_name": "magic_missile",
        "target": "Snik",
        "spell_level": 1,
    }, gs)
    assert res["ok"] is False
    assert gs.npcs["snik"].hp == snik_hp_before  # no HP change on failed cast


def test_cast_damaging_spell_known_slot_applies_by_slot():
    """Known spell, slot available: by_slot[level] expression is rolled and applied exactly.
    No modifier may be added by the caller — rolled == applied is the invariant."""
    rules.seed(7)
    caster = Character(name="Wisp", spell_slots={1: 2}, spells=["magic_missile"])
    target = NPC(name="Goblin", max_hp=30, hp=30)  # high HP so no overkill clamping
    hp_before = target.hp
    res = rules.cast_damaging_spell(caster, target, "magic_missile", 1)
    assert res["ok"] is True
    assert "3d4+3" in res["damage_detail"]           # by_slot[1] expression used
    assert target.hp == hp_before - res["damage"]    # applied == rolled exactly
    assert res["slots_remaining"] == 1               # one slot consumed from two


def test_cast_damaging_spell_unknown_to_caster_fails():
    """Spell not in caster.spells: ok=False before consuming slot or touching HP."""
    caster = Character(name="Wisp", spell_slots={3: 2}, spells=["magic_missile"])
    target = NPC(name="Goblin", max_hp=10, hp=10)
    res = rules.cast_damaging_spell(caster, target, "fireball", 3)
    assert res["ok"] is False
    assert "Wisp" in res["reason"]
    assert "fireball" in res["reason"]
    assert target.hp == 10              # HP untouched
    assert caster.spell_slots == {3: 2} # slot untouched


def test_cast_damaging_spell_no_slot_fails():
    """No slot of the required level: ok=False, slots and target HP unchanged."""
    caster = Character(name="Wisp", spell_slots={1: 0}, spells=["magic_missile"])
    target = NPC(name="Goblin", max_hp=10, hp=10)
    res = rules.cast_damaging_spell(caster, target, "magic_missile", 1)
    assert res["ok"] is False
    assert "level-1" in res["reason"]
    assert target.hp == 10              # HP untouched
    assert caster.spell_slots == {1: 0} # slot untouched


def test_spell_attack_bonus_uses_spellcasting_ability_not_attack_bonus():
    """spell_attack bonus = spellcasting_ability_mod + proficiency_from_level.
    The caster's attack_bonus field must NOT be used."""
    rules.seed(42)
    caster = Character(
        name="Aldric", level=3,
        attack_bonus=99,                    # must NOT appear in to_hit_bonus
        spellcasting_ability="wis",
        ability_modifiers={"wis": 3},
        spell_slots={1: 2},
        spells=["guiding_bolt"],
    )
    target = NPC(name="Snik", max_hp=40, hp=40, ac=12)
    res = rules.cast_damaging_spell(caster, target, "guiding_bolt", 1)
    assert res["ok"] is True
    assert res["to_hit_bonus"] == 5              # wis(3) + proficiency(2) for level 3
    assert res["to_hit_total"] == res["to_hit_roll"] + 5
    assert res["defender_ac"] == 12
    assert res["slots_remaining"] == 1
    # Result must be self-consistent regardless of the dice outcome.
    expected_hit = res["to_hit_roll"] == 20 or (res["to_hit_roll"] != 1 and res["to_hit_total"] >= 12)
    assert res["hit"] is expected_hit
    if res["hit"]:
        assert target.hp == 40 - res["damage"]
        assert "4d6" in res["damage_detail"]    # by_slot[1] expression used
    else:
        assert target.hp == 40                  # miss: no damage applied


def test_spell_attack_miss_slot_consumed_no_damage():
    """On a miss the slot is consumed but no damage is applied and 'damage' is absent."""
    rules.seed(1)  # seed(1) gives a non-20 first 1d20 (confirmed by test_attack_respects_ac)
    caster = Character(
        name="Aldric", level=3,
        spellcasting_ability="wis",
        ability_modifiers={"wis": 3},
        spell_slots={1: 2},
        spells=["guiding_bolt"],
    )
    target = NPC(name="Wall", max_hp=40, hp=40, ac=99)  # unreachable without nat 20
    res = rules.cast_damaging_spell(caster, target, "guiding_bolt", 1)
    assert res["ok"] is True
    assert res["hit"] is False
    assert res["critical"] is False
    assert "damage" not in res              # no damage key on a miss
    assert target.hp == 40                  # HP untouched
    assert res["slots_remaining"] == 1      # slot consumed despite miss


# --- legacy: kept for dispatch-layer coverage --------------------------------

def test_damaging_spell_unknown_to_caster_fails():
    """cast_damaging_spell rejects a spell the caster does not know,
    leaving spell slots and target HP unchanged."""
    wisp = Character(name="Wisp", spell_slots={3: 2}, spells=["magic_missile"])
    goblin = NPC(name="Goblin", max_hp=10, hp=10)
    res = rules.cast_damaging_spell(wisp, goblin, "fireball", 3)
    assert res["ok"] is False
    assert "Wisp" in res["reason"]
    assert "fireball" in res["reason"]
    assert goblin.hp == 10             # HP untouched
    assert wisp.spell_slots == {3: 2}  # slot untouched


# --- dispatch-level rejection safety (no crash, state unchanged) -------------

def test_dispatch_attack_unowned_weapon_no_crash():
    """dispatch('attack') with a weapon not in the attacker's inventory must return
    ok=False without raising KeyError and leave the defender's HP unchanged."""
    gs = _make_combat_state()
    # Aldric carries nothing — dagger is a known weapon but not in his inventory.
    hp_before = gs.npcs["snik"].hp
    log_len = len(gs.log)
    res = tools.dispatch("attack", {"attacker": "Aldric", "defender": "Snik", "weapon": "dagger"}, gs)
    assert res["ok"] is False
    assert gs.npcs["snik"].hp == hp_before  # HP untouched
    assert len(gs.log) == log_len           # no log entry written for a rejection


def test_dispatch_cast_unknown_spell_no_crash():
    """dispatch('cast_spell') for a spell not in the caster's spells list must return
    ok=False without raising and leave slots and target HP unchanged."""
    gs = _make_combat_state()
    gs.party["wisp"].spell_slots = {3: 2}
    hp_before = gs.npcs["snik"].hp
    log_len = len(gs.log)
    res = tools.dispatch("cast_spell", {
        "caster": "Wisp",
        "spell_name": "fireball",
        "target": "Snik",
        "spell_level": 3,
    }, gs)
    assert res["ok"] is False
    assert gs.npcs["snik"].hp == hp_before           # HP untouched
    assert gs.party["wisp"].spell_slots == {3: 2}    # slot untouched


def test_turn_guard_blocks_non_active_attacker():
    rules.seed(0)
    gs = _make_combat_state()
    res = tools.dispatch("start_combat", {"combatants": ["aldric", "wisp", "snik"]}, gs)
    active_key = res["active"]
    # Pick a party member who is NOT active to try attacking.
    non_active = next(k for k in ("aldric", "wisp") if k != active_key)
    non_active_name = gs.party[non_active].name
    result = tools.dispatch("attack", {"attacker": non_active_name, "defender": "Snik"}, gs)
    assert result["ok"] is False
    assert non_active_name in result["error"]


def test_turn_guard_allows_active_attacker():
    rules.seed(0)
    gs = _make_combat_state()
    # Give Aldric a guaranteed-high initiative so he's first.
    gs.party["aldric"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    assert gs.combat_order[0] == "aldric"
    result = tools.dispatch("attack", {"attacker": "Aldric", "defender": "Snik"}, gs)
    assert result["ok"] is True


def test_turn_guard_inactive_outside_combat():
    gs = _make_combat_state()
    # No combat started — turn guard must not block anything.
    result = tools.dispatch("attack", {"attacker": "Aldric", "defender": "Snik"}, gs)
    assert result["ok"] is True


def test_turn_guard_blocks_cast_spell_out_of_turn():
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["wisp"].spell_slots = {1: 2}
    res = tools.dispatch("start_combat", {"combatants": ["aldric", "wisp", "snik"]}, gs)
    active_key = res["active"]
    # If Wisp is not active, casting should be rejected.
    if active_key != "wisp":
        result = tools.dispatch("cast_spell", {"caster": "Wisp", "spell_level": 1}, gs)
        assert result["ok"] is False
        assert "Wisp" in result["error"]


def test_action_guard_blocks_second_attack():
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100  # guaranteed first
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    assert gs.combat_order[0] == "aldric"

    res1 = tools.dispatch("attack", {"attacker": "Aldric", "defender": "Snik"}, gs)
    assert res1["ok"] is True
    assert gs.action_used is True

    res2 = tools.dispatch("attack", {"attacker": "Aldric", "defender": "Snik"}, gs)
    assert res2["ok"] is False
    assert "already acted" in res2["error"]
    assert "Aldric" in res2["error"]


def test_second_attack_blocked_hp_unchanged():
    """First attack on a combatant's turn is accepted; a second attack before
    next_turn must be refused AND must leave the target's HP exactly where the
    first attack left it."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100   # guaranteed first
    gs.npcs["snik"].hp = gs.npcs["snik"].max_hp = 20    # enough HP to survive

    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    assert gs.combat_order[0] == "aldric"

    res1 = tools.dispatch("attack", {"attacker": "Aldric", "defender": "Snik"}, gs)
    assert res1["ok"] is True
    hp_after_first = gs.npcs["snik"].hp

    res2 = tools.dispatch("attack", {"attacker": "Aldric", "defender": "Snik"}, gs)
    assert res2["ok"] is False
    assert "already acted" in res2["error"]
    assert gs.npcs["snik"].hp == hp_after_first   # blocked attack must not touch HP


def test_action_guard_blocks_second_cast():
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["wisp"].spell_slots = {1: 2}
    gs.party["wisp"].ability_modifiers["dex"] = 100  # Wisp first
    tools.dispatch("start_combat", {"combatants": ["wisp", "snik"]}, gs)
    assert gs.combat_order[0] == "wisp"

    res1 = tools.dispatch("cast_spell", {
        "caster": "Wisp", "spell_name": "magic_missile", "target": "Snik", "spell_level": 1,
    }, gs)
    assert res1["ok"] is True

    res2 = tools.dispatch("cast_spell", {
        "caster": "Wisp", "spell_name": "magic_missile", "target": "Snik", "spell_level": 1,
    }, gs)
    assert res2["ok"] is False
    assert "already acted" in res2["error"]
    assert "Wisp" in res2["error"]


def test_next_turn_resets_action_flag():
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)

    tools.dispatch("attack", {"attacker": "Aldric", "defender": "Snik"}, gs)
    assert gs.action_used is True

    tools.dispatch("next_turn", {}, gs)  # advance to Snik
    assert gs.action_used is False

    res = tools.dispatch("attack", {"attacker": "Snik", "defender": "Aldric"}, gs)
    assert res["ok"] is True


def test_action_guard_inactive_outside_combat():
    gs = _make_combat_state()
    # No combat — action_used is never set; same combatant may act freely.
    res1 = tools.dispatch("attack", {"attacker": "Aldric", "defender": "Snik"}, gs)
    res2 = tools.dispatch("attack", {"attacker": "Aldric", "defender": "Snik"}, gs)
    assert res1["ok"] is True
    assert res2["ok"] is True
    assert gs.action_used is False


def test_start_combat_clears_action_flag():
    rules.seed(0)
    gs = _make_combat_state()
    gs.action_used = True  # simulate leftover state
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    assert gs.action_used is False


def test_combat_loop_halts_at_player_no_skip():
    """No-API round-loop test.

    Scenario: initiative order Aldric (player) → Snik (NPC) → Wisp (player).
    After Aldric acts, the engine must:
      - advance to Snik and resolve his NPC turn (exactly one next_turn call)
      - advance to Wisp and halt (exactly one more next_turn call)
      - leave combat_index pointing at Wisp with combat_round still 1
    No extra next_turn that would push Wisp's slot into round 2.
    """
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    # Extreme Dex values guarantee the order regardless of dice.
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100})
    gs.npcs["snik"]   = NPC(name="Snik", max_hp=20, hp=20, ability_modifiers={"dex": 0})
    gs.party["wisp"]  = Character(name="Wisp", ability_modifiers={"dex": -100})

    res = tools.dispatch("start_combat", {"combatants": ["aldric", "snik", "wisp"]}, gs)
    assert res["combat_order"] == ["aldric", "snik", "wisp"], "precondition: known order"

    # Fake client always returns a plain text response — no tool calls, no API hit.
    fake_block = MagicMock()
    fake_block.type = "text"
    fake_block.text = "Narration."
    fake_resp = MagicMock()
    fake_resp.stop_reason = "end_turn"
    fake_resp.content = [fake_block]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_resp

    agent = DMAgent(gs, client=fake_client)
    # Simulate Aldric having used his action (fake client makes no tool calls, so
    # action_used would otherwise stay False and the loop wouldn't advance past him).
    gs.action_used = True
    narration = agent.take_turn("Aldric swings at Snik")

    # Engine pointer must be on Wisp, still in round 1.
    assert gs.combat_order[gs.combat_index] == "wisp", (
        f"expected pointer on wisp, got {gs.combat_order[gs.combat_index]} "
        f"(round {gs.combat_round})"
    )
    assert gs.combat_round == 1, "extra next_turn pushed engine into round 2"

    # Authoritative turn line must name the engine's active combatant.
    assert "Wisp, what do you do?" in narration, "engine-sourced closing prompt missing or wrong name"

    # Exactly two next_turn advances: Aldric→Snik, Snik→Wisp.
    next_turns = [c for c in agent.tool_trace if c["name"] == "next_turn"]
    assert len(next_turns) == 2, f"expected 2 next_turn calls, got {len(next_turns)}"
    assert next_turns[0]["result"]["active"] == "snik"
    assert next_turns[1]["result"]["active"] == "wisp"


@pytest.mark.parametrize("first_key,second_key", [
    ("aldric", "wisp"),   # Aldric wins initiative — input names Wisp
    ("wisp", "aldric"),   # Wisp wins initiative  — input names Aldric
])
def test_loop_halts_at_first_player_when_input_names_another(first_key, second_key):
    """When a player is first in initiative and the input names a different player,
    the loop must halt at the first player and prompt for them — never advancing past.

    Covers the regression: start_combat sets active=first_player, turn guard rejects
    the named action (action_used stays False), loop must NOT call next_turn.
    """
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100 if first_key == "aldric" else -100})
    gs.party["wisp"]   = Character(name="Wisp",   ability_modifiers={"dex": 100 if first_key == "wisp"   else -100}, spell_slots={1: 2})
    gs.npcs["snik"]    = NPC(name="Snik", max_hp=20, hp=20, ability_modifiers={"dex": 0})

    res = tools.dispatch("start_combat", {"combatants": ["aldric", "wisp", "snik"]}, gs)
    assert gs.combat_order[0] == first_key, "precondition: first_key won initiative"

    # Fake client: no tool calls — simulates model stopping after a turn-guard rejection.
    fake_block = MagicMock(); fake_block.type = "text"; fake_block.text = "Narration."
    fake_resp  = MagicMock(); fake_resp.stop_reason = "end_turn"; fake_resp.content = [fake_block]
    fake_client = MagicMock(); fake_client.messages.create.return_value = fake_resp

    agent = DMAgent(gs, client=fake_client)
    second_name = gs.party[second_key].name
    first_name  = gs.party[first_key].name

    narration = agent.take_turn(f"{second_name} casts magic missile at Snik")

    assert gs.combat_order[gs.combat_index] == first_key, (
        f"Loop advanced past {first_name} to {gs.combat_order[gs.combat_index]}"
    )
    assert f"{first_name}, what do you do?" in narration, (
        f"Expected prompt for {first_name}, got: {narration!r}"
    )
    next_turns = [c for c in agent.tool_trace if c["name"] == "next_turn"]
    assert next_turns == [], f"next_turn must not be called when active player hasn't acted; got {next_turns}"


def test_end_combat_triggers_post_combat_narration():
    """When end_combat fires during _execute, take_turn must:
    - call _narrate_combat_over (not _narrate) for that action
    - leave combat state cleared (combat_round == 0)
    - NOT append a combat-turn prompt
    """
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    gs.action_used = True  # Aldric has acted

    # API call sequence:
    # 1. _execute: model calls end_combat tool
    ec_block = MagicMock()
    ec_block.type = "tool_use"; ec_block.id = "t1"
    ec_block.name = "end_combat"; ec_block.input = {}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [ec_block]

    # 2. _execute loop: model returns end_turn (no more tools)
    done_block = MagicMock(); done_block.type = "text"; done_block.text = ""
    done_resp = MagicMock()
    done_resp.stop_reason = "end_turn"; done_resp.content = [done_block]

    # 3. _narrate_combat_over: post-combat prose
    narr_block = MagicMock()
    narr_block.type = "text"
    narr_block.text = "The goblin crumples. Silence falls over the chamber. The passage yawns ahead. What do you do?"
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, done_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("Aldric finishes the goblin")

    # Combat state cleared
    assert gs.combat_round == 0
    assert gs.combat_order == []

    # Post-combat narration was produced (contains the exploration prompt)
    assert "What do you do?" in narration

    # _narrate_combat_over was used: its prompt contains "Combat is over"
    third_call_messages = fake_client.messages.create.call_args_list[2][1]["messages"]
    last_user_content = next(
        m["content"] for m in reversed(third_call_messages) if m["role"] == "user"
    )
    assert "Combat is over" in str(last_user_content)

    # No combat-turn prompt appended after end_combat
    assert "Aldric, what do you do?" not in narration
    assert "Snik, what do you do?" not in narration


def test_agent_re_prompts_after_failed_cast_not_success():
    """When cast_spell returns ok=False (no slots), the agent must feed the failure
    result back to the model before requesting narration — never silently succeeding.

    Structural assertions:
    - Engine did not apply damage (HP unchanged).
    - Tool trace records ok=False.
    - The second API call (execute-loop continuation) carries a tool_result whose
      JSON content contains 'ok': false — proving the agent re-prompted with the failure.
    - Exactly three API calls: two for the execute phase, one for narration.
    """
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(location="Dungeon")
    gs.party["wisp"] = Character(name="Wisp", spell_slots={1: 0})  # no slots remaining
    gs.npcs["snik"] = NPC(name="Snik", max_hp=10, hp=10)

    # Call 1 (_execute): model tries cast_spell
    cs_block = MagicMock()
    cs_block.type = "tool_use"; cs_block.id = "t1"
    cs_block.name = "cast_spell"
    cs_block.input = {"caster": "Wisp", "spell_level": 1, "spell_name": "magic_missile", "target": "Snik"}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [cs_block]

    # Call 2 (_execute loop): model sees ok=False result, stops
    stop_block = MagicMock(); stop_block.type = "text"; stop_block.text = ""
    stop_resp = MagicMock(); stop_resp.stop_reason = "end_turn"; stop_resp.content = [stop_block]

    # Call 3 (_narrate): model narrates the fizzle (content doesn't affect assertions)
    narr_block = MagicMock()
    narr_block.type = "text"
    narr_block.text = "Wisp reaches for the magic but finds only silence — the spell fizzles."
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, stop_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    agent.take_turn("Wisp casts magic missile at Snik")

    # Engine enforced the failure — no HP change.
    assert gs.npcs["snik"].hp == 10

    # Tool trace records ok=False.
    cast_calls = [c for c in agent.tool_trace if c["name"] == "cast_spell"]
    assert len(cast_calls) == 1
    assert cast_calls[0]["result"]["ok"] is False

    # The second API call must carry the tool_result with the ok=False payload.
    # This is the "re-prompt": the agent fed the failure back before asking for narration.
    call2_msgs = fake_client.messages.create.call_args_list[1][1]["messages"]
    tool_result_content = next(
        (c["content"]
         for m in call2_msgs if m["role"] == "user" and isinstance(m["content"], list)
         for c in m["content"] if isinstance(c, dict) and c.get("type") == "tool_result"),
        None,
    )
    assert tool_result_content is not None, "agent must feed tool_result back before narrating"
    assert '"ok": false' in tool_result_content, (
        f"tool_result must contain ok=false, got: {tool_result_content!r}"
    )

    # Two-phase protocol: 2 execute calls + 1 narrate call.
    assert fake_client.messages.create.call_count == 3


def test_invalid_action_does_not_advance_turn():
    """Attack rejected for an unowned weapon must:
    - leave the active combatant unchanged (no next_turn fired)
    - leave the round number unchanged
    - leave action_used=False so a follow-up valid attack by the same character succeeds.
    """
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    # Aldric goes first (extreme dex) and carries only a mace — no dagger.
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100},
                                   inventory=["mace"])
    gs.npcs["snik"] = NPC(name="Snik", max_hp=20, hp=20, ability_modifiers={"dex": 0})

    res = tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    assert gs.combat_order[0] == "aldric"

    # Fake client: model calls attack(dagger) → rejected; stops; narrates rejection.
    dagger_block = MagicMock()
    dagger_block.type = "tool_use"; dagger_block.id = "t1"
    dagger_block.name = "attack"
    dagger_block.input = {"attacker": "Aldric", "defender": "Snik", "weapon": "dagger"}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [dagger_block]

    stop_block = MagicMock(); stop_block.type = "text"; stop_block.text = ""
    stop_resp = MagicMock(); stop_resp.stop_reason = "end_turn"; stop_resp.content = [stop_block]

    narr_block = MagicMock()
    narr_block.type = "text"
    narr_block.text = "Aldric has no dagger — he's carrying a mace. What does Aldric do?"
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, stop_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    agent.take_turn("Aldric attacks Snik with dagger")

    # Active combatant and round unchanged.
    assert gs.combat_order[gs.combat_index] == "aldric", (
        f"pointer moved to {gs.combat_order[gs.combat_index]} after invalid action"
    )
    assert gs.combat_round == 1, "round advanced after invalid action"
    assert gs.action_used is False, "action_used must be False — no valid action was taken"

    # No next_turn must have fired.
    next_turns = [c for c in agent.tool_trace if c["name"] == "next_turn"]
    assert next_turns == [], f"next_turn fired {len(next_turns)} time(s) after invalid action"

    # Follow-up: the same character's valid attack resolves normally.
    follow_up = tools.dispatch(
        "attack", {"attacker": "Aldric", "defender": "Snik", "weapon": "mace"}, gs
    )
    assert follow_up["ok"] is True, "valid follow-up attack must succeed"
    assert gs.action_used is True  # action now consumed by the valid attack


def test_next_turn_skips_downed_combatant():
    """next_turn must skip any combatant at 0 HP and land on the next live one."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100   # first
    gs.npcs["snik"].ability_modifiers["dex"] = 0        # second
    gs.party["wisp"].ability_modifiers["dex"] = -100    # third (will be downed)
    res = tools.dispatch("start_combat", {"combatants": ["aldric", "snik", "wisp"]}, gs)
    assert res["combat_order"] == ["aldric", "snik", "wisp"]

    gs.party["wisp"].hp = 0  # down Wisp before her turn arrives

    # aldric (0) → snik (1): non-downed, no skip
    adv1 = tools.dispatch("next_turn", {}, gs)
    assert adv1["ok"] is True
    assert adv1["active"] == "snik"
    assert "skipped_downed" not in adv1

    # snik (1) → wisp (2, downed — skip) → aldric (0, round 2)
    adv2 = tools.dispatch("next_turn", {}, gs)
    assert adv2["ok"] is True
    assert adv2["active"] == "aldric"
    assert adv2["round"] == 2
    assert adv2.get("skipped_downed") == ["wisp"]


def test_next_turn_all_downed_returns_error():
    """next_turn must return ok=False when every combatant is at 0 HP."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    gs.party["aldric"].hp = 0
    gs.npcs["snik"].hp = 0
    res = tools.dispatch("next_turn", {}, gs)
    assert res["ok"] is False
    assert "end_combat" in res["error"]


def test_end_combat_clears_state():
    rules.seed(0)
    gs = _make_combat_state()
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    res = tools.dispatch("end_combat", {}, gs)
    assert res["ok"] is True
    assert gs.combat_order == []
    assert gs.combat_index == 0
    assert gs.combat_round == 0


def test_skill_check_uses_ability_modifier():
    rules.seed(5)  # first roll will be deterministic
    c = Character(name="Wisp", ability_modifiers={"int": 4})
    # Seed 5 produces a roll of 6 on 1d20 → total 10 with +4 modifier
    res = rules.skill_check(c, "int", dc=10)
    assert res["ok"] is True
    assert res["modifier"] == 4
    assert res["total"] == res["roll"] + 4
    assert res["success"] == (res["total"] >= 10)
    assert res["ability"] == "int"


def test_skill_check_missing_ability_defaults_to_zero():
    c = Character(name="Hero", ability_modifiers={})
    rules.seed(15)
    res = rules.skill_check(c, "cha", dc=5)
    assert res["modifier"] == 0
    assert res["total"] == res["roll"]


def test_skill_check_case_insensitive():
    c = Character(name="Hero", ability_modifiers={"wis": 3})
    rules.seed(1)
    res = rules.skill_check(c, "WIS", dc=1)
    assert res["modifier"] == 3


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")
