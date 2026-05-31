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


def test_overkill_damage_clamps_hp_to_zero():
    """Damage exceeding remaining HP sets HP to exactly 0, never negative."""
    snik = NPC(name="Snik", max_hp=12, hp=12)
    res = rules.apply_damage(snik, 15)
    assert snik.hp == 0
    assert snik.hp >= 0          # never negative
    assert res["hp"] == 0
    assert res["downed"] is True
    assert snik.is_down is True


def test_overkill_attack_clamps_hp_to_zero():
    """An attack roll whose damage exceeds the defender's HP sets HP to 0, not below."""
    rules.seed(0)
    # Give attacker guaranteed-high stats so the attack almost certainly hits and deals
    # enough damage to overkill; use a defender with 1 HP so any hit is overkill.
    attacker = Character(
        name="A", attack_bonus=0,
        inventory=["greataxe"],
        ability_modifiers={"str": 10},  # +10 str so even minimum roll exceeds 1 HP
        proficiency_bonus=2,
    )
    defender = NPC(name="D", ac=1, hp=1, max_hp=1)
    res = rules.attack(attacker, defender, weapon="greataxe")
    assert res["ok"] is True
    if res["hit"]:
        assert defender.hp == 0
        assert defender.hp >= 0      # never negative
        assert res["defender_hp"] == 0
        assert res["downed"] is True
        assert defender.is_down is True


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


def test_spawn_npc_goblin_stat_block():
    """spawn_npc returns the correct canonical stat block for a goblin."""
    kwargs = rules.spawn_npc("goblin")
    assert kwargs["name"] == "Goblin"
    assert kwargs["max_hp"] == 12
    assert kwargs["hp"] == 12           # starts at full HP
    assert kwargs["ac"] == 13
    assert kwargs["attack_bonus"] == 4
    assert kwargs["ability_modifiers"]["dex"] == 2
    assert "shortsword" in kwargs["inventory"]
    assert "shortbow" in kwargs["inventory"]


def test_spawn_npc_name_override():
    """spawn_npc allows overriding the display name without changing the stat block."""
    kwargs = rules.spawn_npc("goblin", name="Snik")
    assert kwargs["name"] == "Snik"
    assert kwargs["max_hp"] == 12


def test_spawn_npc_unknown_raises():
    """spawn_npc raises KeyError for an unrecognised monster id."""
    with pytest.raises(KeyError, match="dragon"):
        rules.spawn_npc("dragon")


def test_spawn_npc_can_attack_with_inventory_weapon():
    """An NPC built from spawn_npc can use its inventory weapon through dispatch."""
    rules.seed(0)
    gs = GameState(location="Test")
    gs.npcs["grix"] = NPC(**rules.spawn_npc("goblin", name="Grix"))
    gs.party["hero"] = Character(name="Hero", max_hp=20, hp=20, ac=12)
    res = tools.dispatch("attack", {"attacker": "Grix", "defender": "Hero", "weapon": "shortsword"}, gs)
    assert res["ok"] is True
    assert res["weapon"] == "shortsword"
    assert res["damage_type"] == "piercing"


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
    order, initiatives = rules.roll_initiative({"aldric": a, "snik": b})
    assert sorted(order) == ["aldric", "snik"]
    assert len(order) == 2
    assert set(initiatives) == {"aldric", "snik"}


def test_roll_initiative_highest_total_first():
    rules.seed(0)
    # +100 dex → total ≥ 101; -100 dex → total ≤ -80. No overlap possible.
    fast = Character(name="Fast", ability_modifiers={"dex": 100})
    slow = Character(name="Slow", ability_modifiers={"dex": -100})
    order, _ = rules.roll_initiative({"fast": fast, "slow": slow})
    assert order == ["fast", "slow"]


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
    order, _ = rules.roll_initiative({"a": ca, "b": cb})
    assert order[0] == expected_first


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
    gs.combat_starting = False  # simulate take_turn clearing the barrier after player phase
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
    gs.combat_starting = False  # simulate take_turn clearing the barrier after player phase
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
    gs.combat_starting = False  # simulate take_turn clearing the barrier after player phase
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
    gs.combat_starting = False  # simulate take_turn clearing the barrier after player phase
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
    gs.combat_starting = False  # simulate take_turn clearing the barrier after player phase
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
    gs.combat_starting = False  # simulate take_turn clearing the barrier after player phase
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
    gs.combat_starting = False  # simulate take_turn clearing the barrier after player phase

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


def test_context_bounded_regardless_of_history_length():
    """_build_turn_context always returns at most 2*NARRATION_WINDOW messages,
    and uses the most recent entries, regardless of how many turns have passed."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent, NARRATION_WINDOW

    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric")
    agent = DMAgent(gs, client=MagicMock())

    # Simulate 20 turns of history — far beyond the window.
    for i in range(20):
        agent.narration_history.append((f"player did {i}", f"narration {i}"))

    messages = agent._build_turn_context()

    assert len(messages) == 2 * NARRATION_WINDOW
    # Most recent entries are at the end.
    assert "player did 19" in messages[-2]["content"]
    assert "narration 19" in messages[-1]["content"]
    # Oldest entries (0–15) are not present.
    assert all("player did 0" not in m["content"] for m in messages)


def test_state_snapshot_reflects_current_state_not_history():
    """_state_snapshot reads self.state directly, so it always shows the LATEST
    HP, spell slots, NPCs, and combat status — even when narration_history entries
    contain stale information from earlier turns."""
    import json as _json
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(location="The Ember Chamber")
    gs.party["aldric"] = Character(
        name="Aldric", max_hp=24, hp=7,
        spell_slots={1: 0},
        conditions=["poisoned"],
    )
    gs.npcs["grik"] = NPC(name="Grik", max_hp=18, hp=5)
    gs.quest_flags["door_open"] = True
    gs.current_scene = "ember_chamber"

    agent = DMAgent(gs, client=MagicMock())
    # Add stale history with wrong HP to confirm the snapshot ignores it.
    agent.narration_history.append(("old input", "Aldric had 24/24 HP previously"))

    snap = _json.loads(agent._state_snapshot())

    assert snap["location"] == "The Ember Chamber"
    assert snap["current_scene"] == "ember_chamber"
    assert snap["party"]["Aldric"]["hp"] == "7/24"           # current, not stale
    assert snap["party"]["Aldric"]["conditions"] == ["poisoned"]
    assert snap["npcs"]["Grik"]["hp"] == "5/18"
    assert snap["quest_flags"]["door_open"] is True
    assert "combat" not in snap                              # not in combat


def test_tool_results_present_in_context_during_execute_loop():
    """Within a single turn's tool-use loop, the tool_result from the first API
    call must be present in the messages passed to the second API call.
    The between-turn context reset must NOT strip mid-turn results."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric")

    # Call 1 (_execute): model calls get_state
    tool_block = MagicMock()
    tool_block.type = "tool_use"; tool_block.id = "t1"
    tool_block.name = "get_state"; tool_block.input = {}
    exec_resp1 = MagicMock()
    exec_resp1.stop_reason = "tool_use"; exec_resp1.content = [tool_block]

    # Call 2 (_execute loop): model sees result and stops
    stop_block = MagicMock(); stop_block.type = "text"; stop_block.text = ""
    exec_resp2 = MagicMock()
    exec_resp2.stop_reason = "end_turn"; exec_resp2.content = [stop_block]

    # Call 3 (_narrate)
    narr_block = MagicMock(); narr_block.type = "text"; narr_block.text = "Done."
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp1, exec_resp2, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    agent.take_turn("Aldric looks around")

    # The second call (execute loop continuation) must carry the tool_result.
    call2_msgs = fake_client.messages.create.call_args_list[1][1]["messages"]
    tool_result_present = any(
        isinstance(m["content"], list) and
        any(isinstance(c, dict) and c.get("type") == "tool_result" for c in m["content"])
        for m in call2_msgs if m["role"] == "user"
    )
    assert tool_result_present, "tool_result must survive the within-turn loop — context reset is between turns only"


def test_npc_turns_batched_into_single_narration_call():
    """A cycle resolving [player, npc_A, npc_B, next_player] must produce exactly
    two narration API calls — one for the player action, one batched call carrying
    both NPC actions in resolution order — followed by one closing player prompt.

    Narration calls are identified as client.messages.create invocations that have
    no 'tools' kwarg; tool-use executions always pass tools=TOOLS.
    """
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100})
    gs.npcs["snik"]    = NPC(name="Snik",  max_hp=20, hp=20, ability_modifiers={"dex": 0})
    gs.npcs["narl"]    = NPC(name="Narl",  max_hp=20, hp=20, ability_modifiers={"dex": -50})
    gs.party["wisp"]   = Character(name="Wisp",  ability_modifiers={"dex": -100})

    res = tools.dispatch("start_combat", {"combatants": ["aldric", "snik", "narl", "wisp"]}, gs)
    assert res["combat_order"] == ["aldric", "snik", "narl", "wisp"], "precondition: known order"

    fake_text = MagicMock()
    fake_text.type = "text"
    fake_text.text = "Narration."
    fake_resp = MagicMock()
    fake_resp.stop_reason = "end_turn"
    fake_resp.content = [fake_text]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_resp

    agent = DMAgent(gs, client=fake_client)
    gs.action_used = True  # Aldric has used his action; loop must advance past him
    narration = agent.take_turn("Aldric swings at Snik")

    all_calls = fake_client.messages.create.call_args_list
    # Narration calls omit 'tools'; execute calls include it.
    narration_calls = [c for c in all_calls if "tools" not in c[1]]
    execute_calls   = [c for c in all_calls if "tools"     in c[1]]

    assert len(narration_calls) == 2, (
        f"expected 2 narration calls, got {len(narration_calls)} "
        f"(total API calls: {len(all_calls)})"
    )
    assert len(execute_calls) == 3, (
        f"expected 3 execute calls (player + snik + narl), got {len(execute_calls)}"
    )

    # The batched narration prompt (second narration call) must name both NPCs.
    batch_messages = narration_calls[1][1]["messages"]
    last_user_prompt = next(
        m["content"] for m in reversed(batch_messages)
        if m["role"] == "user" and isinstance(m["content"], str)
    )
    assert "Snik" in last_user_prompt, "batch prompt must name Snik"
    assert "Narl" in last_user_prompt, "batch prompt must name Narl"

    # Engine-sourced closing prompt must address the next active player.
    assert "Wisp, what do you do?" in narration


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


# --- add_npc dispatch tests ---------------------------------------------------

def test_add_npc_unknown_template_rejected():
    gs = GameState(location="Test")
    res = tools.dispatch("add_npc", {"template": "dragon", "instance_id": "dragon_one"}, gs)
    assert res["ok"] is False
    assert "dragon" in res["error"]
    assert gs.npcs == {}


def test_add_npc_duplicate_npc_key_rejected():
    gs = GameState(location="Test")
    tools.dispatch("add_npc", {"template": "goblin", "instance_id": "grix"}, gs)
    res = tools.dispatch("add_npc", {"template": "orc", "instance_id": "grix"}, gs)
    assert res["ok"] is False
    assert "grix" in res["error"]
    assert gs.npcs["grix"].name == "Goblin"  # original untouched


def test_add_npc_duplicate_party_key_rejected():
    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric")
    res = tools.dispatch("add_npc", {"template": "goblin", "instance_id": "aldric"}, gs)
    assert res["ok"] is False
    assert "aldric" in res["error"]


def test_add_npc_correct_stats_and_name_override():
    gs = GameState(location="Test")
    res = tools.dispatch("add_npc", {"template": "goblin", "instance_id": "grix", "name": "Grix"}, gs)
    assert res["ok"] is True
    assert res["combat"] is False
    npc = gs.npcs["grix"]
    assert npc.name == "Grix"
    assert npc.max_hp == 12
    assert npc.hp == 12       # full HP on spawn
    assert npc.ac == 13
    assert "shortsword" in npc.inventory
    assert "shortbow" in npc.inventory


def test_add_npc_not_in_combat_no_order_change():
    gs = GameState(location="Test")
    res = tools.dispatch("add_npc", {"template": "orc", "instance_id": "ugor"}, gs)
    assert res["ok"] is True
    assert gs.combat_order == []
    assert gs.combat_round == 0
    assert "ugor" in gs.npcs


def test_add_npc_serializes_through_save_load():
    gs = GameState(location="Test")
    tools.dispatch("add_npc", {"template": "orc", "instance_id": "ugor"}, gs)
    restored = GameState.from_dict(gs.to_dict())
    assert "ugor" in restored.npcs
    assert restored.npcs["ugor"].max_hp == 15       # orc stat block
    assert restored.npcs["ugor"].hp == 15
    assert "greataxe" in restored.npcs["ugor"].inventory


def test_add_npc_in_combat_npc_enters_order_and_pointer_stable():
    """Adding an NPC during combat must insert it into combat_order and leave
    the active combatant (by key) unchanged."""
    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    gs.npcs["snik"] = NPC(name="Snik", ability_modifiers={"dex": 0})
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    active_before = gs.combat_order[gs.combat_index]

    res = tools.dispatch("add_npc", {"template": "goblin", "instance_id": "grix"}, gs)

    assert res["ok"] is True
    assert res["combat"] is True
    assert "grix" in gs.combat_order
    assert len(gs.combat_order) == 3
    assert gs.combat_order[gs.combat_index] == active_before  # pointer stable


def test_add_npc_in_combat_initiative_stored():
    """The new NPC's initiative total is stored in combat_initiatives."""
    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    tools.dispatch("start_combat", {"combatants": ["aldric"]}, gs)

    res = tools.dispatch("add_npc", {"template": "goblin", "instance_id": "grix"}, gs)

    assert res["ok"] is True
    assert "grix" in gs.combat_initiatives
    assert gs.combat_initiatives["grix"] == res["initiative"]


def test_add_npc_in_combat_order_is_sorted_by_initiative():
    """After insertion the combat_order must be non-increasing by stored initiative."""
    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    gs.party["wisp"] = Character(name="Wisp", ability_modifiers={"dex": 0})
    tools.dispatch("start_combat", {"combatants": ["aldric", "wisp"]}, gs)

    tools.dispatch("add_npc", {"template": "goblin", "instance_id": "grix"}, gs)

    order = gs.combat_order
    for i in range(len(order) - 1):
        assert gs.combat_initiatives.get(order[i], 0) >= gs.combat_initiatives.get(order[i + 1], 0)


def test_add_npc_in_combat_before_active_shifts_pointer():
    """If the NPC is inserted before the active slot, combat_index increments so
    the same combatant remains active."""
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    gs.npcs["snik"] = NPC(name="Snik", ability_modifiers={"dex": 0})
    # Manually craft known combat state: aldric first, snik second; pointer on snik.
    gs.combat_order = ["aldric", "snik"]
    gs.combat_index = 1          # pointer on snik
    gs.combat_round = 1
    gs.combat_initiatives = {"aldric": 10, "snik": 5}

    # Goblin has dex +2; with a d20 roll of any value ≥ 9 its initiative > 10+0=10
    # and it slots at position 0, before aldric, before snik.
    # Force that: set combat_initiatives["aldric"]=1 so any goblin roll beats it.
    gs.combat_initiatives = {"aldric": 1, "snik": 0}

    # seed so d20=20 → goblin initiative = 20+2=22, always first
    rules.seed(0)  # just need any roll > 1; verify pointer shifts
    res = tools.dispatch("add_npc", {"template": "goblin", "instance_id": "grix"}, gs)
    assert res["ok"] is True
    grix_init = gs.combat_initiatives["grix"]
    grix_pos = gs.combat_order.index("grix")
    if grix_pos <= 1:   # inserted at or before old combat_index (1)
        assert gs.combat_order[gs.combat_index] == "snik"   # pointer followed
    else:               # inserted after → pointer unchanged at 1
        assert gs.combat_index == 1


def test_add_npc_in_combat_cleared_by_end_combat():
    """end_combat must clear combat_initiatives so no stale entries remain."""
    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    tools.dispatch("start_combat", {"combatants": ["aldric"]}, gs)
    tools.dispatch("add_npc", {"template": "goblin", "instance_id": "grix"}, gs)
    assert gs.combat_initiatives != {}

    tools.dispatch("end_combat", {}, gs)
    assert gs.combat_initiatives == {}


def test_from_dict_expands_template_npc():
    """NPC entries with a 'template' key are expanded via spawn_npc at load time."""
    d = {
        "location": "Test",
        "npcs": {"snik": {"template": "goblin", "name": "Snik", "hostile": True}},
    }
    gs = GameState.from_dict(d)
    npc = gs.npcs["snik"]
    assert npc.name == "Snik"
    assert npc.max_hp == 12
    assert npc.hp == 12
    assert npc.ac == 13
    assert npc.hostile is True
    assert "shortsword" in npc.inventory


def test_scenario2_loads_correctly():
    """two_scenes_test.json (multi-scene format) populates state from current_scene."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scenes_test.json")
    gs = GameState.load(path)
    # current_scene is barrow_entrance → snik should be in the live roster
    assert "snik" in gs.npcs
    npc = gs.npcs["snik"]
    assert npc.name == "Snik"
    assert npc.max_hp == 12
    assert npc.hp == 12
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
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scenes_test.json")
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
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scenes_test.json")
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
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scenes_test.json")
    gs = GameState.load(path)
    gs.party["aldric"].hp = 10   # simulate damage

    tools.dispatch("move_scene", {"scene_key": "ember_chamber"}, gs)

    assert gs.party["aldric"].hp == 10   # unchanged
    assert "aldric" in gs.party
    assert "wisp" in gs.party


def test_move_scene_unknown_key_rejected():
    """move_scene with an unknown scene_key returns ok=False."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scenes_test.json")
    gs = GameState.load(path)
    original_location = gs.location

    res = tools.dispatch("move_scene", {"scene_key": "nowhere"}, gs)

    assert res["ok"] is False
    assert "nowhere" in res["error"]
    assert gs.location == original_location   # state unchanged


def test_move_scene_missing_scene_key_rejected():
    """move_scene without scene_key when scenes are defined returns ok=False."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scenes_test.json")
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


def test_multi_scene_savegame_round_trip():
    """Saving and reloading preserves scenes dict and live NPC state (not re-expanded)."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scenes_test.json")
    gs = GameState.load(path)
    gs.npcs["snik"].hp = 3   # simulate combat damage

    restored = GameState.from_dict(gs.to_dict())

    assert restored.current_scene == "barrow_entrance"
    assert "ember_chamber" in restored.scenes
    assert restored.npcs["snik"].hp == 3   # live state, not re-expanded from template


def test_get_state_surfaces_available_scenes():
    """get_state returns the current scene's exits, not the global scene list."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scenes_test.json")
    gs = GameState.load(path)
    res = tools.dispatch("get_state", {}, gs)
    assert res["ok"] is True
    state_dict = res["state"]
    assert "exits" in state_dict
    assert state_dict["exits"] == {"the passage descending deeper into the barrow": "ember_chamber"}
    assert "available_scenes" not in state_dict
    assert "scenes" not in state_dict   # omitted to keep context lean


def test_add_npc_combat_initiatives_round_trips_through_json():
    """combat_initiatives is preserved across save/load."""
    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    tools.dispatch("start_combat", {"combatants": ["aldric"]}, gs)
    tools.dispatch("add_npc", {"template": "goblin", "instance_id": "grix"}, gs)

    restored = GameState.from_dict(gs.to_dict())
    assert restored.combat_initiatives == gs.combat_initiatives
    assert "grix" in restored.combat_initiatives


# --- start_combat / add_npc identifier resolution ---------------------------

def _make_named_state():
    """State with display names that differ in case from dict keys."""
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    gs.party["wisp"]   = Character(name="Wisp",   ability_modifiers={"dex": 2})
    gs.npcs["grik"]    = NPC(name="Grik",          ability_modifiers={"dex": 1})
    return gs


def test_start_combat_accepts_display_names():
    """start_combat(['Aldric','Wisp','Grik']) resolves to canonical lowercase keys."""
    rules.seed(42)
    gs = _make_named_state()
    res = tools.dispatch("start_combat", {"combatants": ["Aldric", "Wisp", "Grik"]}, gs)
    assert res["ok"] is True
    assert set(res["combat_order"]) == {"aldric", "wisp", "grik"}


def test_start_combat_display_names_and_keys_produce_same_order():
    """Display names and dict keys resolve to the same canonical combat_order
    when the RNG is seeded identically."""
    gs1 = _make_named_state()
    rules.seed(42)
    res1 = tools.dispatch("start_combat", {"combatants": ["Aldric", "Wisp", "Grik"]}, gs1)

    gs2 = _make_named_state()
    rules.seed(42)
    res2 = tools.dispatch("start_combat", {"combatants": ["aldric", "wisp", "grik"]}, gs2)

    assert res1["ok"] is True
    assert res2["ok"] is True
    assert res1["combat_order"] == res2["combat_order"]


def test_start_combat_rejects_unknown_identifier():
    """An identifier that matches neither a key nor a display name returns ok=False."""
    gs = _make_named_state()
    res = tools.dispatch("start_combat", {"combatants": ["aldric", "nobody"]}, gs)
    assert res["ok"] is False
    assert "nobody" in res["error"]


def test_start_combat_mixed_case_keys_accepted():
    """Identifiers like 'ALDRIC' or 'wiSP' still resolve to canonical keys."""
    rules.seed(0)
    gs = _make_named_state()
    res = tools.dispatch("start_combat", {"combatants": ["ALDRIC", "wiSP"]}, gs)
    assert res["ok"] is True
    assert set(res["combat_order"]) == {"aldric", "wisp"}


def test_add_npc_duplicate_check_is_case_insensitive():
    """instance_id 'Grix' is rejected when 'grix' already exists as an NPC key."""
    gs = GameState(location="Test")
    tools.dispatch("add_npc", {"template": "goblin", "instance_id": "grix"}, gs)
    res = tools.dispatch("add_npc", {"template": "orc", "instance_id": "Grix"}, gs)
    assert res["ok"] is False
    assert "Grix" in res["error"]
    assert gs.npcs["grix"].name == "Goblin"  # original untouched


def test_add_npc_duplicate_check_rejects_party_key_case_insensitive():
    """instance_id 'Aldric' is rejected when party key 'aldric' already exists."""
    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric")
    res = tools.dispatch("add_npc", {"template": "goblin", "instance_id": "Aldric"}, gs)
    assert res["ok"] is False
    assert "Aldric" in res["error"]


# --- roll extremes: crit, fumble, auto-hit -----------------------------------

def test_weapon_crit_doubles_dice_not_modifier():
    """Nat 20 with mace (1d6+str): dice list doubled, modifier added exactly once.

    damage_dice = '1d6+2'; roll yields [3] → crit makes [3, 3]; total = 6 + 2 = 8.
    If the modifier were doubled the total would be 10; if dice not doubled it would be 5.
    """
    from unittest.mock import patch
    atk = Character(
        name="A", proficiency_bonus=2, inventory=["mace"],
        ability_modifiers={"str": 2},
    )
    dfn = NPC(name="D", ac=10, hp=30, max_hp=30)

    # randint calls in order: d20=20 (crit hit), then 1d6=3 (damage)
    with patch.object(rules._rng, "randint", side_effect=[20, 3]):
        res = rules.attack(atk, dfn, weapon="mace")

    assert res["to_hit_roll"] == 20
    assert res["critical"] is True
    assert res["hit"] is True
    assert res["damage"] == 3 * 2 + 2   # dice doubled (6), modifier once (+2) = 8
    assert 30 - dfn.hp == res["damage"]  # applied == rolled


def test_weapon_nat1_auto_miss():
    """Nat 1 misses unconditionally, even against AC 1. Defender HP unchanged."""
    from unittest.mock import patch
    atk = Character(
        name="A", proficiency_bonus=2, inventory=["mace"],
        ability_modifiers={"str": 5},
    )
    dfn = NPC(name="D", ac=1, hp=20, max_hp=20)  # AC so low any real roll would hit

    # Only the d20 fires; no damage roll on a miss.
    with patch.object(rules._rng, "randint", side_effect=[1]):
        res = rules.attack(atk, dfn, weapon="mace")

    assert res["to_hit_roll"] == 1
    assert res["hit"] is False
    assert res["critical"] is False
    assert dfn.hp == 20  # untouched


def test_spell_attack_crit_doubles_dice():
    """Nat 20 on guiding_bolt (4d6): dice list doubled to 8 values; damage applied == rolled;
    slot consumed."""
    from unittest.mock import patch
    caster = Character(
        name="C", level=3, proficiency_bonus=2,
        spellcasting_ability="wis",
        ability_modifiers={"wis": 3},
        spell_slots={1: 1},
        spells=["guiding_bolt"],
    )
    target = NPC(name="T", ac=10, hp=40, max_hp=40)

    # d20=20 (crit), then 4d6=[2,3,4,5]; crit doubles to [2,3,4,5,2,3,4,5] → 28
    with patch.object(rules._rng, "randint", side_effect=[20, 2, 3, 4, 5]):
        res = rules.cast_damaging_spell(caster, target, "guiding_bolt", 1)

    assert res["to_hit_roll"] == 20
    assert res["critical"] is True
    assert res["hit"] is True
    assert res["damage"] == (2 + 3 + 4 + 5) * 2   # 28 — four dice values each doubled
    assert 40 - target.hp == res["damage"]
    assert res["slots_remaining"] == 0


def test_spell_attack_nat1_auto_miss():
    """Nat 1 on guiding_bolt misses even against AC 1; slot consumed, no damage applied."""
    from unittest.mock import patch
    caster = Character(
        name="C", level=1, proficiency_bonus=2,
        spellcasting_ability="wis",
        ability_modifiers={"wis": 3},
        spell_slots={1: 1},
        spells=["guiding_bolt"],
    )
    target = NPC(name="T", ac=1, hp=20, max_hp=20)

    # Only the d20 fires; no damage roll on a miss.
    with patch.object(rules._rng, "randint", side_effect=[1]):
        res = rules.cast_damaging_spell(caster, target, "guiding_bolt", 1)

    assert res["to_hit_roll"] == 1
    assert res["hit"] is False
    assert res["critical"] is False
    assert "damage" not in res
    assert target.hp == 20           # untouched
    assert res["slots_remaining"] == 0  # slot consumed despite miss


def test_auto_hit_ignores_ac():
    """magic_missile (auto_hit) hits AC 99 and applies damage — no to-hit roll needed."""
    from unittest.mock import patch
    caster = Character(name="C", level=1, spell_slots={1: 1}, spells=["magic_missile"])
    target = NPC(name="T", ac=99, hp=30, max_hp=30)

    # 3d4+3: three randint(1,4) calls, no d20.
    with patch.object(rules._rng, "randint", side_effect=[2, 3, 4]):
        res = rules.cast_damaging_spell(caster, target, "magic_missile", 1)

    assert res["ok"] is True
    assert res["auto_hit"] is True
    assert "hit" not in res           # no spell-attack roll → no hit key
    assert res["damage"] == 2 + 3 + 4 + 3  # 3d4+3 = 12
    assert 30 - target.hp == res["damage"]
    assert res["slots_remaining"] == 0


# --- spell scaling -----------------------------------------------------------

def test_magic_missile_upcast_uses_higher_by_slot():
    """Upcasting magic_missile uses by_slot[level] expression; damage applied == rolled."""
    from unittest.mock import patch

    caster = Character(name="C", spell_slots={2: 1, 3: 1}, spells=["magic_missile"])
    target = NPC(name="T", ac=10, hp=100, max_hp=100)

    # Level 2: "4d4+4" → four d4 rolls
    hp_before = target.hp
    with patch.object(rules._rng, "randint", side_effect=[2, 2, 2, 2]):
        res2 = rules.cast_damaging_spell(caster, target, "magic_missile", 2)

    assert res2["ok"] is True
    assert "4d4+4" in res2["damage_detail"]
    assert hp_before - target.hp == res2["damage"]

    # Level 3: "5d4+5" → five d4 rolls
    hp_before = target.hp
    with patch.object(rules._rng, "randint", side_effect=[2, 2, 2, 2, 2]):
        res3 = rules.cast_damaging_spell(caster, target, "magic_missile", 3)

    assert res3["ok"] is True
    assert "5d4+5" in res3["damage_detail"]
    assert hp_before - target.hp == res3["damage"]


@pytest.mark.parametrize("level,expected_proficiency", [
    (1, 2), (4, 2), (5, 3), (8, 3), (9, 4), (13, 5), (17, 6),
])
def test_spell_attack_bonus_proficiency_by_level(level, expected_proficiency):
    """to_hit_bonus == wis_mod(3) + 2+(level-1)//4; attack_bonus(99) must not appear."""
    from unittest.mock import patch

    caster = Character(
        name="C",
        level=level,
        attack_bonus=99,                    # sentinel — must not leak into spell attack
        spellcasting_ability="wis",
        ability_modifiers={"wis": 3},
        spell_slots={1: 1},
        spells=["guiding_bolt"],
    )
    # AC 99 ensures a miss with d20=10 → exactly one randint call, no damage dice.
    target = NPC(name="T", ac=99, hp=40, max_hp=40)

    with patch.object(rules._rng, "randint", side_effect=[10]):
        res = rules.cast_damaging_spell(caster, target, "guiding_bolt", 1)

    assert res["ok"] is True
    assert res["to_hit_bonus"] == 3 + expected_proficiency
    assert res["to_hit_bonus"] != 99    # attack_bonus sentinel was not used


# --- NPC default-weapon behavior ---------------------------------------------

def test_npc_no_weapon_uses_equipped_weapon():
    """NPC attack with no weapon arg draws from the stat-block inventory (shortsword),
    not the unarmed 1d6 fallback — 'weapon' key is present in the result."""
    rules.seed(0)
    npc = NPC(**rules.spawn_npc("goblin", name="Snik"))
    target = Character(name="Hero", max_hp=20, hp=20, ac=1)
    res = rules.attack(npc, target)
    assert res["ok"] is True
    assert res["weapon"] == "shortsword"
    assert res["damage_type"] == "piercing"


def test_npc_bogus_weapon_ignored_uses_equipped():
    """NPC attack with weapon='scimitar' (absent from WEAPONS and from inventory):
    the model's guess is silently ignored; engine falls back to shortsword."""
    rules.seed(0)
    npc = NPC(**rules.spawn_npc("goblin", name="Snik"))
    target = Character(name="Hero", max_hp=20, hp=20, ac=1)
    res = rules.attack(npc, target, weapon="scimitar")
    assert res["ok"] is True
    assert res["weapon"] == "shortsword"


def test_pc_no_weapon_keeps_unarmed_fallback():
    """PC with no weapon arg still takes the unarmed path: no 'weapon' key,
    damage starts with '1d6', to_hit_bonus == attack_bonus."""
    rules.seed(0)
    pc = Character(name="Hero", attack_bonus=5)
    target = NPC(name="D", ac=1, hp=20, max_hp=20)
    res = rules.attack(pc, target)
    assert res["ok"] is True
    assert "weapon" not in res
    assert res["to_hit_bonus"] == 5
    if res["hit"]:
        assert res["damage_detail"].startswith("1d6")


# --- apply_dice: atomic roll-and-apply tool ----------------------------------

def test_apply_dice_damage_rolls_and_applies_same():
    """Engine rolls 2d6 and applies exactly that total (no model step in between).
    Predict the roll with a parallel Random — not by calling rules.roll, which
    would advance _rng and diverge from the dispatch call's draw sequence."""
    import random as _stdlib

    seed = 7
    preview = _stdlib.Random(seed)
    predicted = preview.randint(1, 6) + preview.randint(1, 6)   # mirrors roll("2d6")

    gs = GameState(location="Test")
    gs.party["hero"] = Character(name="Hero", max_hp=100, hp=100, ac=12)
    rules.seed(seed)
    res = tools.dispatch("apply_dice", {"target": "Hero", "notation": "2d6", "source": "spike trap"}, gs)

    assert res["ok"] is True
    assert res["roll"] == predicted
    assert 100 - gs.party["hero"].hp == predicted   # applied == rolled, no clamp
    assert res["downed"] is False


def test_apply_dice_healing_clamps_at_max():
    """Healing is clamped at max_hp; the full rolled total is still reported."""
    from unittest.mock import patch
    gs = GameState(location="Test")
    gs.party["hero"] = Character(name="Hero", max_hp=10, hp=3, ac=12)

    # Force 2d4+2 → [4,4]+2 = 10, which exceeds the 7-point gap to max
    with patch.object(rules._rng, "randint", side_effect=[4, 4]):
        res = tools.dispatch("apply_dice", {"target": "Hero", "notation": "2d4+2", "kind": "healing"}, gs)

    assert res["ok"] is True
    assert res["roll"] == 10            # full roll reported, not the 7-point delta
    assert gs.party["hero"].hp == 10   # clamped at max_hp, not 3+10=13


def test_apply_dice_damage_overkill_floors_at_zero():
    """Overkill damage floors HP at 0; reported roll is the full amount, not the clamped delta."""
    from unittest.mock import patch
    gs = GameState(location="Test")
    gs.party["hero"] = Character(name="Hero", max_hp=20, hp=1, ac=12)

    with patch.object(rules._rng, "randint", side_effect=[6]):
        res = tools.dispatch("apply_dice", {"target": "Hero", "notation": "1d6", "source": "lava"}, gs)

    assert res["ok"] is True
    assert res["roll"] == 6         # full roll, not the clamped delta (1)
    assert gs.party["hero"].hp == 0
    assert res["downed"] is True


def test_apply_dice_bad_notation_rejected():
    """Invalid dice notation returns ok=False; target HP is unchanged."""
    gs = GameState(location="Test")
    gs.party["hero"] = Character(name="Hero", max_hp=20, hp=20, ac=12)
    res = tools.dispatch("apply_dice", {"target": "Hero", "notation": "not_dice"}, gs)
    assert res["ok"] is False
    assert gs.party["hero"].hp == 20


def test_apply_dice_unknown_target_rejected():
    """Unknown target name returns ok=False with no state change."""
    gs = GameState(location="Test")
    res = tools.dispatch("apply_dice", {"target": "Nobody", "notation": "1d6"}, gs)
    assert res["ok"] is False


def test_modify_hp_fixed_still_works():
    """modify_hp with fixed integer amounts still damages and heals (regression)."""
    gs = GameState(location="Test")
    gs.party["hero"] = Character(name="Hero", max_hp=20, hp=20, ac=12)

    res = tools.dispatch("modify_hp", {"target": "Hero", "amount": -5}, gs)
    assert res["ok"] is True
    assert gs.party["hero"].hp == 15

    res = tools.dispatch("modify_hp", {"target": "Hero", "amount": 3}, gs)
    assert res["ok"] is True
    assert gs.party["hero"].hp == 18


def test_roll_dice_applies_nothing():
    """roll_dice is a pure oracle — it returns a total but changes no actor's HP,
    spell slots, or conditions (regression: must stay a no-op against tracked state)."""
    rules.seed(0)
    gs = GameState(location="Test")
    gs.party["hero"] = Character(
        name="Hero", max_hp=20, hp=15, spell_slots={1: 2}, conditions=["prone"],
    )
    gs.npcs["goblin"] = NPC(name="Goblin", max_hp=12, hp=12)

    res = tools.dispatch("roll_dice", {"notation": "3d6+2"}, gs)

    assert res["ok"] is True
    assert "result" in res
    assert gs.party["hero"].hp == 15
    assert gs.party["hero"].spell_slots == {1: 2}
    assert gs.party["hero"].conditions == ["prone"]
    assert gs.npcs["goblin"].hp == 12


# --- offensive target auto-resolution ----------------------------------------

def test_spell_auto_targets_sole_enemy():
    """No target named, one living hostile → auto-resolves, damage applied, slot consumed once."""
    rules.seed(7)
    gs = GameState(location="Test")
    gs.party["wisp"] = Character(name="Wisp", spell_slots={1: 2}, spells=["magic_missile"])
    gs.npcs["snik"] = NPC(name="Snik", max_hp=30, hp=30)
    snik_hp_before = gs.npcs["snik"].hp
    slots_before = gs.party["wisp"].spell_slots[1]

    res = tools.dispatch("cast_spell", {
        "caster": "Wisp",
        "spell_name": "magic_missile",
        "spell_level": 1,
    }, gs)

    assert res["ok"] is True
    assert res["auto_target"] == "Snik"
    assert gs.party["wisp"].spell_slots[1] == slots_before - 1   # slot consumed
    assert gs.npcs["snik"].hp < snik_hp_before                    # damage applied
    assert snik_hp_before - gs.npcs["snik"].hp == res["damage"]   # rolled == applied


def test_spell_ambiguous_target_asks():
    """Two living hostiles, no target → ok=false, reason ambiguous_target, nothing spent, turn stays."""
    rules.seed(0)
    gs = GameState(location="Test")
    gs.party["wisp"] = Character(name="Wisp", spell_slots={1: 2}, spells=["magic_missile"],
                                 ability_modifiers={"dex": 100})
    gs.npcs["grik"] = NPC(name="Grik", max_hp=18, hp=18, hostile=True)
    gs.npcs["narl"] = NPC(name="Narl", max_hp=12, hp=12, hostile=True)
    grik_hp = gs.npcs["grik"].hp
    narl_hp = gs.npcs["narl"].hp
    slots_before = gs.party["wisp"].spell_slots[1]

    tools.dispatch("start_combat", {"combatants": ["wisp", "grik", "narl"]}, gs)
    gs.combat_starting = False  # simulate take_turn clearing the barrier after player phase
    assert gs.combat_order[0] == "wisp"

    res = tools.dispatch("cast_spell", {
        "caster": "Wisp",
        "spell_name": "magic_missile",
        "spell_level": 1,
    }, gs)

    assert res["ok"] is False
    assert res["reason"] == "ambiguous_target"
    assert set(res["candidates"]) == {"Grik", "Narl"}
    assert gs.party["wisp"].spell_slots[1] == slots_before   # slot NOT consumed
    assert gs.npcs["grik"].hp == grik_hp                     # no damage
    assert gs.npcs["narl"].hp == narl_hp                     # no damage
    assert gs.action_used is False                           # turn stays alive


def test_spell_explicit_target_with_multiple_enemies():
    """Two living hostiles, explicit target Grik → Grik takes damage, Narl untouched."""
    rules.seed(7)
    gs = GameState(location="Test")
    gs.party["wisp"] = Character(name="Wisp", spell_slots={1: 2}, spells=["magic_missile"])
    gs.npcs["grik"] = NPC(name="Grik", max_hp=30, hp=30, hostile=True)
    gs.npcs["narl"] = NPC(name="Narl", max_hp=30, hp=30, hostile=True)
    narl_hp = gs.npcs["narl"].hp

    res = tools.dispatch("cast_spell", {
        "caster": "Wisp",
        "spell_name": "magic_missile",
        "target": "Grik",
        "spell_level": 1,
    }, gs)

    assert res["ok"] is True
    assert gs.npcs["narl"].hp == narl_hp   # Narl untouched
    assert gs.npcs["grik"].hp < 30         # Grik takes damage


def test_spell_no_valid_target():
    """All hostiles downed → ok=false reason no_target, slot NOT consumed."""
    gs = GameState(location="Test")
    gs.party["wisp"] = Character(name="Wisp", spell_slots={1: 2}, spells=["magic_missile"])
    gs.npcs["snik"] = NPC(name="Snik", max_hp=12, hp=0, hostile=True)  # already down
    slots_before = gs.party["wisp"].spell_slots[1]

    res = tools.dispatch("cast_spell", {
        "caster": "Wisp",
        "spell_name": "magic_missile",
        "spell_level": 1,
    }, gs)

    assert res["ok"] is False
    assert res["reason"] == "no_target"
    assert gs.party["wisp"].spell_slots[1] == slots_before   # slot NOT consumed


def test_spell_explicit_target_allows_ally():
    """Naming a party member as target is honored — explicit targeting is permissive."""
    rules.seed(7)
    gs = GameState(location="Test")
    gs.party["wisp"] = Character(name="Wisp", spell_slots={1: 2}, spells=["magic_missile"])
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=24)
    gs.npcs["snik"] = NPC(name="Snik", max_hp=12, hp=12, hostile=True)
    snik_hp_before = gs.npcs["snik"].hp
    aldric_hp_before = gs.party["aldric"].hp

    res = tools.dispatch("cast_spell", {
        "caster": "Wisp",
        "spell_name": "magic_missile",
        "target": "Aldric",
        "spell_level": 1,
    }, gs)

    assert res["ok"] is True
    assert gs.npcs["snik"].hp == snik_hp_before        # enemy untouched
    assert gs.party["aldric"].hp < aldric_hp_before    # ally takes damage


def test_attack_auto_targets_sole_enemy():
    """No defender named, one living hostile → auto-resolves, attack resolves, auto_target surfaced."""
    rules.seed(0)
    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric", inventory=["mace"],
                                   ability_modifiers={"str": 3}, proficiency_bonus=2)
    gs.npcs["snik"] = NPC(name="Snik", max_hp=20, hp=20, hostile=True, ac=1)

    res = tools.dispatch("attack", {"attacker": "Aldric", "weapon": "mace"}, gs)

    assert res["ok"] is True
    assert res["auto_target"] == "Snik"
    assert "hit" in res


def test_attack_ambiguous_target_asks():
    """Two living hostiles, no defender → ok=false, reason ambiguous_target, action_used reset."""
    rules.seed(0)
    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100},
                                   inventory=["mace"], proficiency_bonus=2)
    gs.npcs["grik"] = NPC(name="Grik", max_hp=18, hp=18, hostile=True)
    gs.npcs["narl"] = NPC(name="Narl", max_hp=12, hp=12, hostile=True)
    grik_hp = gs.npcs["grik"].hp
    narl_hp = gs.npcs["narl"].hp

    tools.dispatch("start_combat", {"combatants": ["aldric", "grik", "narl"]}, gs)
    gs.combat_starting = False  # simulate take_turn clearing the barrier after player phase
    assert gs.combat_order[0] == "aldric"

    res = tools.dispatch("attack", {"attacker": "Aldric", "weapon": "mace"}, gs)

    assert res["ok"] is False
    assert res["reason"] == "ambiguous_target"
    assert set(res["candidates"]) == {"Grik", "Narl"}
    assert gs.npcs["grik"].hp == grik_hp   # no damage
    assert gs.npcs["narl"].hp == narl_hp   # no damage
    assert gs.action_used is False          # turn stays alive


# --- engine-driven end-of-combat detection -----------------------------------

def test_engine_auto_ends_combat_when_last_enemy_downed():
    """Player attack downs the last hostile → _maybe_end_combat fires, combat_round=0,
    _narrate_for routes to post-combat wrap-up, no combat-turn closing prompt emitted.
    Scene has exits so the run does not end here (game_over stays False)."""
    from unittest.mock import MagicMock, patch
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena", current_scene="arena")
    gs.scenes = {
        "arena": {"location": "Arena", "exits": {"the vault ahead": "vault"}},
        "vault": {"location": "The Vault", "exits": {}},
    }
    gs.party["aldric"] = Character(
        name="Aldric",
        ability_modifiers={"str": 5, "dex": 100},
        inventory=["mace"],
        proficiency_bonus=2,
    )
    gs.npcs["snik"] = NPC(name="Snik", max_hp=1, hp=1, ac=1)

    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    assert gs.combat_order[0] == "aldric"

    # Model calls attack(Aldric, mace) — engine auto-selects Snik
    atk_block = MagicMock()
    atk_block.type = "tool_use"; atk_block.id = "t1"
    atk_block.name = "attack"
    atk_block.input = {"attacker": "Aldric", "weapon": "mace"}

    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [atk_block]

    done_block = MagicMock(); done_block.type = "text"; done_block.text = ""
    done_resp = MagicMock(); done_resp.stop_reason = "end_turn"; done_resp.content = [done_block]

    narr_block = MagicMock()
    narr_block.type = "text"
    narr_block.text = "Silence falls. The passage yawns ahead. What do you do?"
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, done_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)

    # d20=15 (hit), 1d6=3 (damage → 3+5=8 > Snik's 1 HP)
    with patch.object(rules._rng, "randint", side_effect=[15, 3]):
        narration = agent.take_turn("Aldric strikes Snik")

    assert gs.npcs["snik"].hp == 0 and gs.npcs["snik"].is_down

    # Engine ended combat automatically
    assert gs.combat_round == 0
    assert gs.combat_order == []
    ec_calls = [c for c in agent.tool_trace if c["name"] == "end_combat"]
    assert len(ec_calls) == 1

    # _narrate_combat_over was used (its prompt contains "Combat is over")
    third_call_msgs = fake_client.messages.create.call_args_list[2][1]["messages"]
    last_user_content = next(
        m["content"] for m in reversed(third_call_msgs) if m["role"] == "user"
    )
    assert "Combat is over" in str(last_user_content)

    # No combat-turn prompt appended
    assert "Aldric, what do you do?" not in narration
    assert "Snik, what do you do?" not in narration


def test_maybe_end_combat_clears_when_last_hostile_down():
    """Sole hostile already at 0 HP → returns True, combat_round cleared to 0."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    gs.npcs["snik"] = NPC(name="Snik", max_hp=12, hp=0)  # already downed
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    assert gs.combat_round == 1

    agent = DMAgent(gs, client=MagicMock())
    ended = agent._maybe_end_combat()

    assert ended is True
    assert gs.combat_round == 0
    assert gs.combat_order == []


def test_maybe_end_combat_continues_with_living_hostile():
    """Two hostiles, one downed — one still alive → returns False, combat unchanged."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    gs.npcs["snik"] = NPC(name="Snik", max_hp=12, hp=0)    # downed
    gs.npcs["narl"] = NPC(name="Narl", max_hp=12, hp=12)   # alive
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik", "narl"]}, gs)
    round_before = gs.combat_round

    agent = DMAgent(gs, client=MagicMock())
    ended = agent._maybe_end_combat()

    assert ended is False
    assert gs.combat_round == round_before
    assert agent.tool_trace == []


def test_maybe_end_combat_ends_on_party_wipe():
    """All PCs at 0 HP with a hostile still alive → returns True, combat ends (symmetric defeat)."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=0, ability_modifiers={"dex": 0})
    gs.party["wisp"]   = Character(name="Wisp",   max_hp=16, hp=0, ability_modifiers={"dex": 0})
    gs.npcs["snik"]    = NPC(name="Snik", max_hp=12, hp=12)
    tools.dispatch("start_combat", {"combatants": ["aldric", "wisp", "snik"]}, gs)
    assert gs.combat_round == 1

    agent = DMAgent(gs, client=MagicMock())
    ended = agent._maybe_end_combat()

    assert ended is True
    assert gs.combat_round == 0
    assert gs.combat_order == []


def test_player_kills_last_enemy_ends_combat():
    """take_turn whose player action downs the sole hostile: combat_round==0, end_combat in
    trace, post-combat wrap-up used, no '<Name>, what do you do?' prompt appended.
    Scene has exits so the run continues (game_over stays False, post-combat beat fires)."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena", current_scene="arena")
    gs.scenes = {
        "arena": {"location": "Arena", "exits": {"the vault ahead": "vault"}},
        "vault": {"location": "The Vault", "exits": {}},
    }
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100})
    gs.npcs["snik"]    = NPC(name="Snik", max_hp=12, hp=12)
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    assert gs.combat_order[0] == "aldric"

    # Model calls modify_hp(-12) — no dice needed, cleanly downs Snik.
    hp_block = MagicMock()
    hp_block.type = "tool_use"; hp_block.id = "t1"
    hp_block.name = "modify_hp"
    hp_block.input = {"target": "Snik", "amount": -12}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [hp_block]

    done_block = MagicMock(); done_block.type = "text"; done_block.text = ""
    done_resp = MagicMock(); done_resp.stop_reason = "end_turn"; done_resp.content = [done_block]

    narr_block = MagicMock()
    narr_block.type = "text"
    narr_block.text = "Snik crumples. Silence falls. The passage yawns ahead. What do you do?"
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, done_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("Aldric finishes Snik")

    assert gs.npcs["snik"].hp == 0

    # Engine ended combat automatically
    assert gs.combat_round == 0
    assert gs.combat_order == []

    # end_combat is in the tool trace
    ec_calls = [c for c in agent.tool_trace if c["name"] == "end_combat"]
    assert len(ec_calls) == 1

    # Post-combat wrap-up prompt used (not the regular narrate)
    third_call_msgs = fake_client.messages.create.call_args_list[2][1]["messages"]
    last_user_content = next(
        m["content"] for m in reversed(third_call_msgs) if m["role"] == "user"
    )
    assert "Combat is over" in str(last_user_content)

    # No combat-turn prompt appended
    assert "Aldric, what do you do?" not in narration
    assert "Snik, what do you do?" not in narration


def test_maybe_end_combat_noop_outside_combat():
    """_maybe_end_combat is idempotent — no-op and no trace entry when not in combat."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric")
    gs.npcs["snik"] = NPC(name="Snik", max_hp=12, hp=0)  # downed, but not in combat
    agent = DMAgent(gs, client=MagicMock())

    agent._maybe_end_combat()

    assert gs.combat_round == 0       # unchanged
    assert agent.tool_trace == []     # nothing appended


def test_maybe_end_combat_noop_when_enemies_alive():
    """_maybe_end_combat does not end combat when living hostiles remain."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    gs.npcs["snik"] = NPC(name="Snik", max_hp=12, hp=12)
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)

    agent = DMAgent(gs, client=MagicMock())
    agent._maybe_end_combat()

    assert gs.combat_round == 1       # still in combat
    assert agent.tool_trace == []     # nothing appended


# --- _closing_prompt ---------------------------------------------------------

def _agent_with_trace(gs, trace_calls):
    """Return a DMAgent whose tool_trace is pre-populated with trace_calls."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent
    agent = DMAgent(gs, client=MagicMock())
    agent.tool_trace = trace_calls
    return agent


def test_closing_prompt_names_targets_after_ambiguous():
    """Wisp is active; trace has ambiguous_target with Grik + Narl; prompt addresses
    Wisp and names both candidates — not the generic 'what do you do?'."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["wisp"].ability_modifiers["dex"] = 100  # Wisp wins initiative → active
    tools.dispatch("start_combat", {"combatants": ["wisp", "snik"]}, gs)
    assert gs.combat_order[0] == "wisp"

    trace = [{"name": "cast_spell", "input": {"caster": "Wisp"}, "result": {
        "ok": False, "reason": "ambiguous_target", "candidates": ["Grik", "Narl"],
    }}]
    agent = _agent_with_trace(gs, trace)
    prompt = agent._closing_prompt()

    assert prompt is not None
    assert "Wisp" in prompt
    assert "Grik" in prompt
    assert "Narl" in prompt
    assert "what do you do?" not in prompt


def test_closing_prompt_generic_on_normal_turn():
    """Wisp active, no ambiguous_target in trace → 'Wisp, what do you do?'."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["wisp"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["wisp", "snik"]}, gs)
    assert gs.combat_order[0] == "wisp"

    agent = _agent_with_trace(gs, [])
    assert agent._closing_prompt() == "Wisp, what do you do?"


def test_closing_prompt_none_when_not_in_combat():
    """Not in combat (combat_round == 0) → None."""
    gs = _make_combat_state()
    agent = _agent_with_trace(gs, [])
    assert agent._closing_prompt() is None


def test_closing_prompt_generic_in_combat():
    """Active combatant up, no ambiguous_target in trace → generic prompt."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    assert gs.combat_order[0] == "aldric"

    agent = _agent_with_trace(gs, [])
    assert agent._closing_prompt() == "Aldric, what do you do?"


def test_closing_prompt_none_outside_combat():
    """Not in combat → None."""
    gs = _make_combat_state()
    agent = _agent_with_trace(gs, [])
    assert agent._closing_prompt() is None


def test_closing_prompt_none_when_active_is_down():
    """Active combatant at 0 HP → None (no prompt for a downed actor)."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    gs.party["aldric"].hp = 0

    agent = _agent_with_trace(gs, [])
    assert agent._closing_prompt() is None


def test_closing_prompt_two_candidates():
    """ambiguous_target with 2 candidates → '<Name>, name your target — A or B?'"""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)

    trace = [{"name": "attack", "input": {"attacker": "Aldric"}, "result": {
        "ok": False, "reason": "ambiguous_target", "candidates": ["Grik", "Narl"],
    }}]
    agent = _agent_with_trace(gs, trace)
    assert agent._closing_prompt() == "Aldric, name your target — Grik or Narl?"


def test_closing_prompt_three_candidates():
    """ambiguous_target with 3 candidates → '<Name>, name your target — A, B, or C?'"""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)

    trace = [{"name": "attack", "input": {"attacker": "Aldric"}, "result": {
        "ok": False, "reason": "ambiguous_target", "candidates": ["Grik", "Narl", "Ugor"],
    }}]
    agent = _agent_with_trace(gs, trace)
    assert agent._closing_prompt() == "Aldric, name your target — Grik, Narl, or Ugor?"


def test_closing_prompt_npc_ambiguous_does_not_pollute_next_player():
    """When an NPC's attack got ambiguous_target this cycle, the next active player
    must receive the generic 'what do you do?' — not the NPC's candidate list."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = -100   # goes last
    gs.npcs["snik"].ability_modifiers["dex"] = 100       # goes first
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    tools.dispatch("next_turn", {}, gs)   # advance pointer to aldric
    assert gs.combat_order[gs.combat_index] == "aldric"

    # Trace contains Snik's ambiguous_target rejection — must not affect Aldric's prompt.
    trace = [{"name": "attack", "input": {"attacker": "Snik"}, "result": {
        "ok": False, "reason": "ambiguous_target", "candidates": ["Grik", "Narl"],
    }}]
    agent = _agent_with_trace(gs, trace)
    assert agent._closing_prompt() == "Aldric, what do you do?"


def test_closing_prompt_ambiguous_only_when_active_is_up():
    """ambiguous_target in trace but active combatant is down → None, not the target prompt."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    gs.party["aldric"].hp = 0

    trace = [{"name": "attack", "input": {"attacker": "Aldric"}, "result": {
        "ok": False, "reason": "ambiguous_target", "candidates": ["Grik", "Narl"],
    }}]
    agent = _agent_with_trace(gs, trace)
    assert agent._closing_prompt() is None


def test_closing_prompt_ok_false_non_ambiguous_gives_generic():
    """ok=false with a different reason (e.g. 'no_target') → generic prompt, not target prompt."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)

    trace = [{"name": "attack", "input": {}, "result": {
        "ok": False, "reason": "no_target", "error": "No living hostile targets present.",
    }}]
    agent = _agent_with_trace(gs, trace)
    assert agent._closing_prompt() == "Aldric, what do you do?"


def test_take_turn_emits_ambiguous_target_prompt():
    """End-to-end: when attack returns ambiguous_target, take_turn's output contains
    the candidate-naming prompt rather than the generic 'what do you do?' prompt."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100},
                                   inventory=["mace"], proficiency_bonus=2)
    gs.npcs["grik"] = NPC(name="Grik", max_hp=18, hp=18, hostile=True, ability_modifiers={"dex": 0})
    gs.npcs["narl"] = NPC(name="Narl", max_hp=12, hp=12, hostile=True, ability_modifiers={"dex": -50})

    tools.dispatch("start_combat", {"combatants": ["aldric", "grik", "narl"]}, gs)
    assert gs.combat_order[0] == "aldric"

    # Model calls attack without naming a defender → engine returns ambiguous_target.
    atk_block = MagicMock()
    atk_block.type = "tool_use"; atk_block.id = "t1"
    atk_block.name = "attack"
    atk_block.input = {"attacker": "Aldric", "weapon": "mace"}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [atk_block]

    stop_block = MagicMock(); stop_block.type = "text"; stop_block.text = ""
    stop_resp = MagicMock(); stop_resp.stop_reason = "end_turn"; stop_resp.content = [stop_block]

    narr_block = MagicMock()
    narr_block.type = "text"; narr_block.text = "There are two enemies before you."
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, stop_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("Aldric attacks")

    assert "Aldric, name your target" in narration
    assert "Grik" in narration
    assert "Narl" in narration
    assert "Aldric, what do you do?" not in narration


# --- game over: victory and defeat -------------------------------------------

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
    """Terminal scene but combat_round > 0: no victory, existing no-exits rejection."""
    gs = GameState(location="Final Chamber", current_scene="final_room")
    gs.scenes = {"final_room": {"location": "Final Chamber", "exits": {}}}
    gs.combat_round = 1
    gs.combat_order = ["aldric"]
    res = tools.dispatch("move_scene", {"scene_key": "anywhere"}, gs)
    assert res["ok"] is False
    assert gs.game_over is False


def test_party_wipe_sets_defeat():
    """All PCs at 0 HP with a living hostile: _maybe_end_combat ends combat and sets defeat."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=0, ability_modifiers={"dex": 0})
    gs.party["wisp"]   = Character(name="Wisp",   max_hp=16, hp=0, ability_modifiers={"dex": 0})
    gs.npcs["snik"]    = NPC(name="Snik", max_hp=12, hp=12)
    tools.dispatch("start_combat", {"combatants": ["aldric", "wisp", "snik"]}, gs)
    assert gs.combat_round == 1

    agent = DMAgent(gs, client=MagicMock())
    ended = agent._maybe_end_combat()

    assert ended is True
    assert gs.combat_round == 0
    assert gs.game_over is True
    assert gs.game_outcome == "defeat"


def test_combat_victory_no_game_over():
    """All hostiles down, party alive in a scene WITH exits: combat ends, game_over stays
    False — the run may have more scenes to visit."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena", current_scene="arena")
    gs.scenes = {
        "arena": {"location": "Arena", "exits": {"the vault ahead": "vault"}},
        "vault": {"location": "The Vault", "exits": {}},
    }
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=24, ability_modifiers={"dex": 0})
    gs.npcs["snik"]    = NPC(name="Snik", max_hp=12, hp=0)  # already downed
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)

    agent = DMAgent(gs, client=MagicMock())
    ended = agent._maybe_end_combat()

    assert ended is True
    assert gs.combat_round == 0
    assert gs.game_over is False
    assert gs.game_outcome == ""


def test_combat_victory_in_terminal_scene_ends_run():
    """All hostiles down, party alive in a terminal scene (no exits): _maybe_end_combat
    ends combat AND sets game_over=True / game_outcome='victory' — no move_scene needed.
    Covers single-scene scenarios (no scenes dict) and named terminal scenes."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Final Chamber")   # no scenes dict — terminal by definition
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=24, ability_modifiers={"dex": 0})
    gs.npcs["snik"]    = NPC(name="Snik", max_hp=12, hp=0)  # already downed
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)

    agent = DMAgent(gs, client=MagicMock())
    ended = agent._maybe_end_combat()

    assert ended is True
    assert gs.combat_round == 0
    assert gs.game_over is True
    assert gs.game_outcome == "victory"


def test_game_over_roundtrips():
    """game_over and game_outcome survive to_dict/from_dict; absent keys load as defaults."""
    gs = GameState(location="End")
    gs.game_over = True
    gs.game_outcome = "victory"
    restored = GameState.from_dict(gs.to_dict())
    assert restored.game_over is True
    assert restored.game_outcome == "victory"

    # Old save without the keys must load with clean defaults.
    old = GameState.from_dict({"location": "Old", "party": {}, "npcs": {}})
    assert old.game_over is False
    assert old.game_outcome == ""


def test_game_over_emits_epilogue_no_prompt():
    """take_turn: victory from move_scene → output contains epilogue, no closing prompt."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(location="Final Chamber", current_scene="final_room")
    gs.scenes = {"final_room": {"location": "Final Chamber", "exits": {}}}
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=24, ability_modifiers={"dex": 0})

    # Call 1 (_execute): model calls move_scene
    ms_block = MagicMock()
    ms_block.type = "tool_use"; ms_block.id = "t1"
    ms_block.name = "move_scene"; ms_block.input = {"scene_key": "onward"}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [ms_block]

    # Call 2 (_execute loop): model sees adventure_complete result, stops
    done_block = MagicMock(); done_block.type = "text"; done_block.text = ""
    done_resp = MagicMock(); done_resp.stop_reason = "end_turn"; done_resp.content = [done_block]

    # Call 3 (_narrate_epilogue): returns victory epilogue
    epi_block = MagicMock()
    epi_block.type = "text"
    epi_block.text = "The party emerges victorious from the depths of the Ashen Barrow."
    epi_resp = MagicMock(); epi_resp.stop_reason = "end_turn"; epi_resp.content = [epi_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, done_resp, epi_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("We press onward")

    assert gs.game_over is True
    assert gs.game_outcome == "victory"
    assert "victorious" in narration           # epilogue present
    assert "Aldric, what do you do?" not in narration  # no closing prompt
    assert fake_client.messages.create.call_count == 3  # execute×2, epilogue×1


# --- exits / scene topology tests -------------------------------------------

def test_move_scene_follows_declared_exit():
    """move_scene to a scene_key that is a declared exit of the current scene succeeds."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scenes_test.json")
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
    """Terminal scene + active combat: move_scene is rejected and error mentions no exits."""
    gs = GameState(location="Chamber", current_scene="ember_chamber")
    gs.scenes = {
        "ember_chamber": {"location": "The Ember Chamber", "exits": {}},
    }
    gs.combat_round = 1
    gs.combat_order = ["aldric"]
    res = tools.dispatch("move_scene", {"scene_key": "anywhere"}, gs)
    assert res["ok"] is False
    assert "no exits" in res["error"].lower()


def test_get_state_surfaces_current_exits():
    """get_state returns the current scene's exits dict, not a global scene list."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scenes_test.json")
    gs = GameState.load(path)
    assert gs.current_scene == "barrow_entrance"
    res = tools.dispatch("get_state", {}, gs)
    assert res["ok"] is True
    state_dict = res["state"]
    # exits contains the current scene's exits (label → target), not the full scene list
    assert "exits" in state_dict
    assert state_dict["exits"] == {"the passage descending deeper into the barrow": "ember_chamber"}
    assert "scenes" not in state_dict
    assert "available_scenes" not in state_dict


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


# --- combat_starting barrier -------------------------------------------------

def test_start_combat_barrier_blocks_attack():
    """After start_combat fires, attack returns ok=False reason=combat_starting;
    HP is unchanged and action_used stays False."""
    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", inventory=["mace"],
                                   ability_modifiers={"dex": 100}, proficiency_bonus=2)
    gs.npcs["snik"] = NPC(name="Snik", max_hp=12, hp=12, ability_modifiers={"dex": 0})
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    assert gs.combat_starting is True

    hp_before = gs.npcs["snik"].hp
    res = tools.dispatch("attack", {"attacker": "Aldric", "defender": "Snik", "weapon": "mace"}, gs)

    assert res["ok"] is False
    assert res["reason"] == "combat_starting"
    assert gs.npcs["snik"].hp == hp_before   # no damage
    assert gs.action_used is False            # turn not consumed


def test_start_combat_barrier_blocks_cast_spell():
    """After start_combat fires, cast_spell returns ok=False reason=combat_starting;
    spell slot is unchanged and action_used stays False."""
    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["wisp"] = Character(name="Wisp", spell_slots={1: 2}, spells=["magic_missile"],
                                  ability_modifiers={"dex": 100})
    gs.npcs["snik"] = NPC(name="Snik", max_hp=12, hp=12, ability_modifiers={"dex": 0})
    tools.dispatch("start_combat", {"combatants": ["wisp", "snik"]}, gs)

    slots_before = gs.party["wisp"].spell_slots[1]
    hp_before = gs.npcs["snik"].hp
    res = tools.dispatch("cast_spell", {
        "caster": "Wisp", "spell_level": 1, "spell_name": "magic_missile", "target": "Snik",
    }, gs)

    assert res["ok"] is False
    assert res["reason"] == "combat_starting"
    assert gs.npcs["snik"].hp == hp_before              # no damage
    assert gs.party["wisp"].spell_slots[1] == slots_before  # no slot consumed
    assert gs.action_used is False


def test_closing_prompt_youre_up_after_start_combat():
    """When start_combat fires this turn and a PC is active, _closing_prompt uses
    the 'you're up' variant instead of the generic 'what do you do?'."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100})
    gs.npcs["snik"] = NPC(name="Snik", ability_modifiers={"dex": 0})

    sc_result = tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    assert gs.combat_order[0] == "aldric"

    agent = _agent_with_trace(gs, [
        {"name": "start_combat", "input": {"combatants": ["aldric", "snik"]}, "result": sc_result},
    ])
    prompt = agent._closing_prompt()

    assert prompt is not None
    assert "Aldric" in prompt
    assert "you're up" in prompt


def test_take_turn_start_combat_defers_attack():
    """End-to-end: a player input that calls start_combat AND attack in the same
    tool-use phase has the attack silently blocked (combat_starting barrier).
    The engine then prompts the first PC with the 'you're up' variant."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100},
                                   inventory=["mace"], proficiency_bonus=2)
    gs.npcs["snik"] = NPC(name="Snik", max_hp=12, hp=12, ability_modifiers={"dex": 0})

    # Model returns two tool_use blocks in one hop: start_combat then attack.
    sc_block = MagicMock()
    sc_block.type = "tool_use"; sc_block.id = "t1"
    sc_block.name = "start_combat"; sc_block.input = {"combatants": ["aldric", "snik"]}

    atk_block = MagicMock()
    atk_block.type = "tool_use"; atk_block.id = "t2"
    atk_block.name = "attack"; atk_block.input = {"attacker": "Aldric", "weapon": "mace"}

    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [sc_block, atk_block]

    done_block = MagicMock(); done_block.type = "text"; done_block.text = ""
    done_resp = MagicMock(); done_resp.stop_reason = "end_turn"; done_resp.content = [done_block]

    narr_block = MagicMock()
    narr_block.type = "text"; narr_block.text = "Torchlight flickers as steel rings out — combat begins."
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, done_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("move toward the goblin and attack")

    # Attack was blocked — Snik's HP must be untouched.
    assert gs.npcs["snik"].hp == 12

    # Barrier rejection is in the trace.
    atk_calls = [c for c in agent.tool_trace if c["name"] == "attack"]
    assert len(atk_calls) == 1
    assert atk_calls[0]["result"]["ok"] is False
    assert atk_calls[0]["result"]["reason"] == "combat_starting"

    # action_used is False — the blocked attack did not consume the turn.
    assert gs.action_used is False

    # Closing prompt addresses the first PC with the initiative variant.
    assert "Aldric" in narration
    assert "you're up" in narration


def test_start_combat_resolves_no_actions():
    """start_combat rolls initiative and establishes order but must never change any
    combatant's HP and must never set action_used — it only initialises the round."""
    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20, ability_modifiers={"dex": 0})
    gs.party["wisp"]   = Character(name="Wisp",   max_hp=16, hp=16, ability_modifiers={"dex": 2})
    gs.npcs["snik"]    = NPC(name="Snik", max_hp=12, hp=12, ability_modifiers={"dex": 1})

    hp_before = {k: c.hp for d in (gs.party, gs.npcs) for k, c in d.items()}

    res = tools.dispatch("start_combat", {"combatants": ["aldric", "wisp", "snik"]}, gs)

    assert res["ok"] is True
    assert gs.combat_round == 1
    assert res["active"] == gs.combat_order[0]
    for key, hp in hp_before.items():
        actor = gs.party.get(key) or gs.npcs.get(key)
        assert actor.hp == hp, f"{key} HP changed during start_combat"
    assert gs.action_used is False


def test_action_denied_during_combat_start():
    """With combat_starting=True (as if start_combat just fired this take_turn), both
    attack and cast_spell must be denied: ok=False, reason='combat_starting', defender HP
    unchanged, slots unchanged, and action_used stays False for both."""
    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", inventory=["mace"],
                                   ability_modifiers={"dex": 100}, proficiency_bonus=2)
    gs.party["wisp"]   = Character(name="Wisp", spell_slots={1: 2}, spells=["magic_missile"],
                                   ability_modifiers={"dex": 0})
    gs.npcs["snik"]    = NPC(name="Snik", max_hp=12, hp=12, ability_modifiers={"dex": 0})
    tools.dispatch("start_combat", {"combatants": ["aldric", "wisp", "snik"]}, gs)
    # combat_starting is True — both PCs attempt to act before the engine clears the barrier

    hp_before    = gs.npcs["snik"].hp
    slots_before = gs.party["wisp"].spell_slots[1]

    atk = tools.dispatch("attack", {
        "attacker": "Aldric", "defender": "Snik", "weapon": "mace",
    }, gs)
    assert atk["ok"] is False
    assert atk["reason"] == "combat_starting"
    assert gs.npcs["snik"].hp == hp_before
    assert gs.action_used is False

    cast = tools.dispatch("cast_spell", {
        "caster": "Wisp", "spell_level": 1, "spell_name": "magic_missile", "target": "Snik",
    }, gs)
    assert cast["ok"] is False
    assert cast["reason"] == "combat_starting"
    assert gs.npcs["snik"].hp == hp_before          # still unchanged after both attempts
    assert gs.party["wisp"].spell_slots[1] == slots_before


def test_first_pc_not_skipped():
    """When start_combat resolves and the first combatant in initiative order is a PC,
    the NPC loop must halt immediately — next_turn is never called, the combat pointer
    stays on that PC, and the engine emits the 'you're up' closing prompt."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100})  # wins initiative
    gs.npcs["snik"]    = NPC(name="Snik", max_hp=12, hp=12, ability_modifiers={"dex": 0})

    # Model calls start_combat only, then stops — no attack attempt.
    sc_block = MagicMock()
    sc_block.type = "tool_use"; sc_block.id = "t1"
    sc_block.name = "start_combat"; sc_block.input = {"combatants": ["aldric", "snik"]}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [sc_block]

    done_block = MagicMock(); done_block.type = "text"; done_block.text = ""
    done_resp  = MagicMock(); done_resp.stop_reason = "end_turn"; done_resp.content = [done_block]

    narr_block = MagicMock()
    narr_block.type = "text"; narr_block.text = "Swords are drawn — combat begins."
    narr_resp  = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, done_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("we approach the goblin")

    # Aldric (dex=100) is first in initiative order.
    assert gs.combat_order[0] == "aldric"

    # NPC loop: first combatant is a PC → break on i=0, next_turn never fired.
    next_turns = [c for c in agent.tool_trace if c["name"] == "next_turn"]
    assert next_turns == [], f"next_turn called {len(next_turns)} time(s) — first PC was skipped"

    # Combat pointer unchanged; still round 1.
    assert gs.combat_order[gs.combat_index] == "aldric"
    assert gs.combat_round == 1

    # Engine issues the initiative-announcing closing prompt.
    assert "Aldric" in narration
    assert "you're up" in narration


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")
