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


# --- expanded SRD tables: integrity + sampling ---------------------------------

def test_monster_inventories_reference_known_weapons():
    """Every weapon listed on a monster stat block must exist in the WEAPONS table,
    so spawn_npc + attack never auto-equip an unknown weapon."""
    bad = [(mid, w) for mid, e in rules.MONSTERS.items()
           for w in e.get("inventory", []) if w not in rules.WEAPONS]
    assert bad == [], f"monsters reference weapons absent from WEAPONS: {bad}"


def test_spell_dice_parse_and_have_required_keys():
    """Every SPELLS entry must declare by_slot + resolution, and every dice
    expression must be valid rules.roll() notation."""
    for key, spell in rules.SPELLS.items():
        assert "resolution" in spell and "by_slot" in spell, f"{key} missing keys"
        for lvl, expr in spell["by_slot"].items():
            rules.roll(expr)  # raises ValueError on bad notation


def test_weapon_entries_have_dice_and_type():
    """Every weapon must have parseable dice and a damage type."""
    for key, w in rules.WEAPONS.items():
        assert w.get("type"), f"{key} has no damage type"
        rules.roll(w["dice"])


def test_spawn_new_monster_full_hp():
    """A newly-added monster (ogre) spawns at full HP with its stat block."""
    kwargs = rules.spawn_npc("ogre")
    assert kwargs["name"] == "Ogre"
    assert kwargs["hp"] == kwargs["max_hp"] == 59
    assert "greatclub" in kwargs["inventory"]


def test_attack_with_new_weapon_uses_its_dice_and_type():
    """A PC can attack with a newly-added weapon (greatsword: 2d6 slashing)."""
    rules.seed(0)
    pc = Character(name="Hero", ability_modifiers={"str": 3}, inventory=["greatsword"],
                   proficiency_bonus=2)
    target = NPC(name="Dummy", max_hp=30, hp=30, ac=1)  # ac 1 → reliable hit
    res = rules.attack(pc, target, weapon="greatsword")
    assert res["ok"] is True
    assert res["weapon"] == "greatsword"
    assert res["damage_type"] == "slashing"
    if res["hit"]:
        assert res["damage_detail"].startswith("2d6")


def test_cast_new_save_spell_auto_hits():
    """Fireball (a save-for-half spell, modelled auto_hit) applies full single-target
    damage and consumes the slot."""
    caster = Character(name="Wisp", spell_slots={3: 1}, spells=["fireball"])
    target = NPC(name="Goblin", max_hp=40, hp=40, ac=15)
    rules.seed(0)
    res = rules.cast_damaging_spell(caster, target, "fireball", 3)
    assert res["ok"] is True
    assert res["auto_hit"] is True
    assert res["damage"] > 0
    assert caster.spell_slots[3] == 0
    assert target.hp == 40 - res["damage"]


def test_cast_cantrip_is_free_and_resolves():
    """A damaging cantrip (fire_bolt at level 0) resolves without consuming a slot."""
    caster = Character(name="Wisp", spell_slots={1: 1}, spells=["fire_bolt"],
                       spellcasting_ability="int", ability_modifiers={"int": 5})
    target = NPC(name="Goblin", max_hp=20, hp=20, ac=1)  # ac 1 → reliable hit
    rules.seed(0)
    res = rules.cast_damaging_spell(caster, target, "fire_bolt", 0)
    assert res["ok"] is True
    assert caster.spell_slots == {1: 1}  # cantrip spent no slot


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
    gs.combat_starting = False  # simulate take_turn clearing the barrier before next_turn
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
    gs.combat_starting = False  # simulate take_turn clearing the barrier before next_turn
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
    ONE narration API call — the unified turn narration that folds the player's
    action together with both NPC actions in resolution order — followed by one
    closing player prompt.

    With engine-resolved NPC turns, the two NPC turns generate ZERO additional
    execute (tool-use) API calls. Only the player's action needs the LLM for tool
    use. Both NPC attack results are injected into messages as context, and the
    single unified narration (folding player + NPC beats, Change 1+2) names both.

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

    # Engine-resolved NPCs add zero execute calls. Only the player's action uses the LLM.
    assert len(execute_calls) == 1, (
        f"expected 1 execute call (player action only; NPC turns engine-resolved), "
        f"got {len(execute_calls)}"
    )

    assert len(narration_calls) == 1, (
        f"expected 1 unified narration call, got {len(narration_calls)} "
        f"(total API calls: {len(all_calls)})"
    )

    # The unified narration prompt must cover the player's action AND both NPCs.
    batch_messages = narration_calls[0][1]["messages"]
    last_user_prompt = next(
        m["content"] for m in reversed(batch_messages)
        if m["role"] == "user" and isinstance(m["content"], str)
    )
    assert "Aldric swings at Snik" in last_user_prompt, "unified prompt must include the player's action"
    assert "Snik" in last_user_prompt, "unified prompt must name Snik"
    assert "Narl" in last_user_prompt, "unified prompt must name Narl"

    # Engine-sourced closing prompt must address the next active player.
    assert "Wisp, what do you do?" in narration

    # api_stats must contain exactly ONE "thinking" entry (the player's action).
    # Without engine resolution the old code would have 3 (player + Snik + Narl).
    thinking_stats = [s for s in agent.api_stats if s["phase"] == "thinking"]
    assert len(thinking_stats) == 1, (
        f"expected 1 thinking call (player action only); "
        f"engine-resolved NPC turns must not add thinking entries. got {len(thinking_stats)}"
    )


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

    # API call sequence (combat player-turn fast path):
    # 1. _execute: model calls end_combat tool. action_used is already True, so
    #    _execute breaks here — no terminal model turn is spent (and would be scrubbed).
    ec_block = MagicMock()
    ec_block.type = "tool_use"; ec_block.id = "t1"
    ec_block.name = "end_combat"; ec_block.input = {}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [ec_block]

    # 2. _narrate_combat_over: post-combat prose
    narr_block = MagicMock()
    narr_block.type = "text"
    narr_block.text = "The goblin crumples. Silence falls over the chamber. The passage yawns ahead. What do you do?"
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("Aldric finishes the goblin")

    # Combat state cleared
    assert gs.combat_round == 0
    assert gs.combat_order == []

    # Post-combat narration was produced (contains the exploration prompt)
    assert "What do you do?" in narration

    # _narrate_combat_over was used: its prompt contains "Combat is over"
    narrate_call_messages = fake_client.messages.create.call_args_list[1][1]["messages"]
    last_user_content = next(
        m["content"] for m in reversed(narrate_call_messages) if m["role"] == "user"
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
    - The second API call (the terminating turn) carries a tool_result whose JSON
      content contains 'ok': false — proving the failure was fed back before the
      model narrated.
    - Exactly two API calls: out of combat the terminating turn IS the narration
      (merge of Change 1), so no separate narration call is spent.
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

    # Call 2 (terminating turn): model sees ok=False and narrates the fizzle in its
    # final message — out of combat this terminal response IS the narration.
    narr_block = MagicMock()
    narr_block.type = "text"
    narr_block.text = "Wisp reaches for the magic but finds only silence — the spell fizzles."
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("Wisp casts magic missile at Snik")

    # Engine enforced the failure — no HP change.
    assert gs.npcs["snik"].hp == 10

    # Tool trace records ok=False.
    cast_calls = [c for c in agent.tool_trace if c["name"] == "cast_spell"]
    assert len(cast_calls) == 1
    assert cast_calls[0]["result"]["ok"] is False

    # The second API call must carry the tool_result with the ok=False payload.
    # This is the "re-prompt": the agent fed the failure back before the model narrated.
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

    # The terminating turn was captured as the narration.
    assert "fizzles" in narration

    # Merge: terminating turn IS the narration — 2 calls, not 3.
    assert fake_client.messages.create.call_count == 2


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
    """next_turn skips a downed NPC and a dead PC; stops at a dying PC (new behavior)."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100   # first
    gs.npcs["snik"].ability_modifiers["dex"] = 0        # second
    gs.party["wisp"].ability_modifiers["dex"] = -100    # third
    res = tools.dispatch("start_combat", {"combatants": ["aldric", "snik", "wisp"]}, gs)
    assert res["combat_order"] == ["aldric", "snik", "wisp"]
    gs.combat_starting = False  # simulate take_turn clearing the barrier before next_turn

    gs.party["wisp"].hp = 0
    gs.party["wisp"].dead = True  # dead PC is skipped (unlike dying, which stops)

    # aldric (0) → snik (1): non-downed NPC, no skip
    adv1 = tools.dispatch("next_turn", {}, gs)
    assert adv1["ok"] is True
    assert adv1["active"] == "snik"
    assert "skipped_downed" not in adv1

    # snik (1) → wisp (2, dead PC — skip) → aldric (0, round 2)
    adv2 = tools.dispatch("next_turn", {}, gs)
    assert adv2["ok"] is True
    assert adv2["active"] == "aldric"
    assert adv2["round"] == 2
    assert adv2.get("skipped_downed") == ["wisp"]


def test_next_turn_all_downed_returns_error():
    """next_turn must return ok=False when every combatant is past the point of acting.
    A dying PC (not dead) stops next_turn rather than triggering the all-downed error,
    so we mark the PC as dead to reach the all-skipped else-branch."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    gs.combat_starting = False  # simulate take_turn clearing the barrier before next_turn
    gs.party["aldric"].hp = 0
    gs.party["aldric"].dead = True  # dead PC is skipped; dying would stop next_turn
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


# --- saving throws (reactive twin of skill_check) ----------------------------

def test_saving_throw_uses_ability_modifier():
    c = Character(name="Wisp", ability_modifiers={"dex": 2})
    rules.force_rolls([15])
    res = rules.saving_throw(c, "dex", dc=12)
    assert res["ok"] is True
    assert res["kind"] == "saving_throw"
    assert res["modifier"] == 2
    assert res["proficient"] is False
    assert res["roll"] == 15
    assert res["total"] == 17
    assert res["success"] is True


def test_saving_throw_proficient_adds_proficiency():
    # Proficient save adds proficiency_bonus; a plain check on the same ability never does.
    c = Character(name="Aldric", ability_modifiers={"wis": 1}, proficiency_bonus=2,
                  save_proficiencies=["wis"])
    rules.force_rolls([10])
    save = rules.saving_throw(c, "wis", dc=13)
    assert save["proficient"] is True
    assert save["modifier"] == 3          # 1 ability + 2 proficiency
    assert save["total"] == 13 and save["success"] is True
    rules.force_rolls([10])
    check = rules.skill_check(c, "wis", dc=13)
    assert check["modifier"] == 1         # check never adds proficiency
    assert check["total"] == 11 and check["success"] is False


def test_saving_throw_not_proficient_no_bonus():
    c = Character(name="Aldric", ability_modifiers={"con": 2}, proficiency_bonus=3,
                  save_proficiencies=["wis"])  # proficient in wis, NOT con
    rules.force_rolls([7])
    res = rules.saving_throw(c, "con", dc=10)
    assert res["proficient"] is False
    assert res["modifier"] == 2           # no proficiency for a non-proficient save
    assert res["total"] == 9 and res["success"] is False


def test_saving_throw_missing_ability_defaults_to_zero():
    c = Character(name="Hero", ability_modifiers={})
    rules.force_rolls([12])
    res = rules.saving_throw(c, "int", dc=10)
    assert res["modifier"] == 0
    assert res["total"] == 12


def test_saving_throw_npc_has_no_proficiency():
    # NPC lacks proficiency_bonus / save_proficiencies — just d20 + ability mod.
    npc = NPC(name="Snik", ability_modifiers={"dex": 1})
    rules.force_rolls([14])
    res = rules.saving_throw(npc, "dex", dc=10)
    assert res["proficient"] is False
    assert res["modifier"] == 1
    assert res["total"] == 15


def test_saving_throw_dispatch_is_not_turn_guarded():
    # A save is reactive: it must resolve for any actor regardless of whose turn it is,
    # unlike skill_check which is the active actor's action and IS turn-guarded.
    rules.seed(0)
    gs = _make_combat_state()
    tools.dispatch("start_combat", {"combatants": ["aldric", "wisp", "snik"]}, gs)
    gs.combat_starting = False  # take_turn clears the barrier before resolving actions
    active = tools._active_actor_name(gs)
    # Pick a party member who is NOT the active combatant.
    off_turn = next(c.name for c in gs.party.values() if c.name != active)
    res = tools.dispatch("saving_throw", {"character": off_turn, "ability": "dex", "dc": 10}, gs)
    assert res["ok"] is True
    assert res["character"] == off_turn

    # Same off-turn actor via skill_check IS refused by the turn guard.
    guarded = tools.dispatch("skill_check", {"character": off_turn, "ability": "dex", "dc": 10}, gs)
    assert guarded["ok"] is False
    assert "not" in guarded["error"].lower() and "turn" in guarded["error"].lower()


def test_saving_throw_dispatch_unknown_character():
    gs = _make_combat_state()
    res = tools.dispatch("saving_throw", {"character": "Nobody", "ability": "dex", "dc": 10}, gs)
    assert res["ok"] is False


# --- add_npc dispatch tests ---------------------------------------------------

def _gs_with_manifest(manifest: dict, location: str = "Test") -> GameState:
    """GameState whose current scene declares a reinforcements manifest.

    add_npc may only spawn instance_ids present in this manifest, mirroring the
    way move_scene gates on declared exits and take_item gates on the loot list.
    """
    gs = GameState(location=location)
    gs.current_scene = "here"
    gs.scenes = {"here": {"location": location, "reinforcements": manifest}}
    return gs


def test_add_npc_undeclared_rejected():
    """Spawning an instance_id not in the scene's reinforcements manifest is refused —
    the model cannot conjure a monster the author did not place."""
    gs = _gs_with_manifest({"goblin_two": {"template": "goblin"}})
    res = tools.dispatch("add_npc", {"instance_id": "dragon_one"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "not_declared"
    assert "goblin_two" in res["error"]  # available ids surfaced
    assert gs.npcs == {}


def test_add_npc_no_manifest_rejects_everything():
    """A scene with no reinforcements manifest can spawn nothing at all."""
    gs = GameState(location="Test")  # no scenes, no manifest
    res = tools.dispatch("add_npc", {"instance_id": "goblin_two"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "not_declared"
    assert "none" in res["error"]
    assert gs.npcs == {}


def test_add_npc_bad_manifest_template_rejected():
    """A manifest entry naming an unknown template is an authoring error → bad_manifest,
    and no actor is created."""
    gs = _gs_with_manifest({"wyrm": {"template": "dragon"}})
    res = tools.dispatch("add_npc", {"instance_id": "wyrm"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "bad_manifest"
    assert gs.npcs == {}


def test_add_npc_empty_instance_id_rejected():
    """An empty or whitespace-only instance_id is refused before the manifest check —
    the roster can never get a blank-string key."""
    gs = _gs_with_manifest({"goblin_two": {"template": "goblin"}})
    res = tools.dispatch("add_npc", {"instance_id": ""}, gs)
    assert res["ok"] is False
    assert res["reason"] == "missing_instance_id"
    assert gs.npcs == {}

    res = tools.dispatch("add_npc", {"instance_id": "   "}, gs)
    assert res["ok"] is False
    assert gs.npcs == {}


def test_add_npc_duplicate_spawn_rejected():
    """A reinforcement spawns once — a second add of the same key is refused so it
    cannot be farmed; the original NPC is untouched."""
    gs = _gs_with_manifest({"grix": {"template": "goblin"}})
    tools.dispatch("add_npc", {"instance_id": "grix"}, gs)
    res = tools.dispatch("add_npc", {"instance_id": "grix"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "already_spawned"
    assert gs.npcs["grix"].name == "Goblin"  # original untouched


def test_add_npc_collision_with_party_key_rejected():
    """A manifest id that collides with an existing party key is refused."""
    gs = _gs_with_manifest({"aldric": {"template": "goblin"}})
    gs.party["aldric"] = Character(name="Aldric")
    res = tools.dispatch("add_npc", {"instance_id": "aldric"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "already_spawned"


def test_add_npc_stats_and_overrides_from_manifest():
    """Stats and name come from the manifest entry — the model supplies only the id."""
    gs = _gs_with_manifest({"grix": {"template": "goblin", "name": "Grix"}})
    res = tools.dispatch("add_npc", {"instance_id": "grix"}, gs)
    assert res["ok"] is True
    assert res["combat"] is False
    npc = gs.npcs["grix"]
    assert npc.name == "Grix"
    assert npc.max_hp == 12
    assert npc.hp == 12       # full HP on spawn
    assert npc.ac == 13
    assert "shortsword" in npc.inventory
    assert "shortbow" in npc.inventory


def test_add_npc_manifest_honors_disposition_and_alertness():
    """Manifest entries expand exactly like scene NPCs, so authored disposition_dc /
    alertness_dc overrides flow through to the spawned reinforcement."""
    gs = _gs_with_manifest({"grix": {"template": "goblin", "disposition_dc": 14, "alertness_dc": 12}})
    res = tools.dispatch("add_npc", {"instance_id": "grix"}, gs)
    assert res["ok"] is True
    assert gs.npcs["grix"].disposition_dc == 14
    assert gs.npcs["grix"].alertness_dc == 12


def test_add_npc_locked_until_trigger_flag_set():
    """A reinforcement with a `requires` flag cannot be spawned until that flag is
    set; once it is, the same call succeeds."""
    gs = _gs_with_manifest({"wave_two": {"template": "orc", "requires": "alarm_raised"}})

    res = tools.dispatch("add_npc", {"instance_id": "wave_two"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "locked"
    assert res["required_flag"] == "alarm_raised"
    assert gs.npcs == {}

    gs.quest_flags["alarm_raised"] = True
    res = tools.dispatch("add_npc", {"instance_id": "wave_two"}, gs)
    assert res["ok"] is True
    assert "wave_two" in gs.npcs
    assert gs.npcs["wave_two"].max_hp == 15  # orc stats, requires stripped cleanly


def test_available_reinforcements_hides_locked_ids():
    """The surfaced id list omits triggered reinforcements until their flag is set,
    and always includes ungated ones."""
    scene = {
        "reinforcements": {
            "open_one": {"template": "goblin"},
            "gated_one": {"template": "orc", "requires": "alarm_raised"},
        }
    }
    assert tools._available_reinforcements(scene, {}) == ["open_one"]
    assert tools._available_reinforcements(scene, {"alarm_raised": True}) == ["open_one", "gated_one"]


def test_get_state_surfaces_only_gate_open_reinforcements():
    """get_state exposes ungated reinforcement ids but hides locked ones until the
    authored trigger fires."""
    gs = _gs_with_manifest({
        "open_one": {"template": "goblin"},
        "gated_one": {"template": "orc", "requires": "alarm_raised"},
    })
    res = tools.dispatch("get_state", {}, gs)
    assert res["state"]["reinforcements"] == ["open_one"]

    gs.quest_flags["alarm_raised"] = True
    res = tools.dispatch("get_state", {}, gs)
    assert res["state"]["reinforcements"] == ["open_one", "gated_one"]


def test_add_npc_not_in_combat_no_order_change():
    gs = _gs_with_manifest({"ugor": {"template": "orc"}})
    res = tools.dispatch("add_npc", {"instance_id": "ugor"}, gs)
    assert res["ok"] is True
    assert gs.combat_order == []
    assert gs.combat_round == 0
    assert "ugor" in gs.npcs


def test_add_npc_serializes_through_save_load():
    gs = _gs_with_manifest({"ugor": {"template": "orc"}})
    tools.dispatch("add_npc", {"instance_id": "ugor"}, gs)
    restored = GameState.from_dict(gs.to_dict())
    assert "ugor" in restored.npcs
    assert restored.npcs["ugor"].max_hp == 15       # orc stat block
    assert restored.npcs["ugor"].hp == 15
    assert "greataxe" in restored.npcs["ugor"].inventory


def test_add_npc_in_combat_npc_enters_order_and_pointer_stable():
    """Adding an NPC during combat must insert it into combat_order and leave
    the active combatant (by key) unchanged."""
    rules.seed(0)
    gs = _gs_with_manifest({"grix": {"template": "goblin"}}, location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    gs.npcs["snik"] = NPC(name="Snik", ability_modifiers={"dex": 0})
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    active_before = gs.combat_order[gs.combat_index]

    res = tools.dispatch("add_npc", {"instance_id": "grix"}, gs)

    assert res["ok"] is True
    assert res["combat"] is True
    assert "grix" in gs.combat_order
    assert len(gs.combat_order) == 3
    assert gs.combat_order[gs.combat_index] == active_before  # pointer stable


def test_add_npc_in_combat_initiative_stored():
    """The new NPC's initiative total is stored in combat_initiatives."""
    rules.seed(0)
    gs = _gs_with_manifest({"grix": {"template": "goblin"}}, location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    tools.dispatch("start_combat", {"combatants": ["aldric"]}, gs)

    res = tools.dispatch("add_npc", {"instance_id": "grix"}, gs)

    assert res["ok"] is True
    assert "grix" in gs.combat_initiatives
    assert gs.combat_initiatives["grix"] == res["initiative"]


def test_add_npc_in_combat_order_is_sorted_by_initiative():
    """After insertion the combat_order must be non-increasing by stored initiative."""
    rules.seed(0)
    gs = _gs_with_manifest({"grix": {"template": "goblin"}}, location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    gs.party["wisp"] = Character(name="Wisp", ability_modifiers={"dex": 0})
    tools.dispatch("start_combat", {"combatants": ["aldric", "wisp"]}, gs)

    tools.dispatch("add_npc", {"instance_id": "grix"}, gs)

    order = gs.combat_order
    for i in range(len(order) - 1):
        assert gs.combat_initiatives.get(order[i], 0) >= gs.combat_initiatives.get(order[i + 1], 0)


def test_add_npc_in_combat_before_active_shifts_pointer():
    """If the NPC is inserted before the active slot, combat_index increments so
    the same combatant remains active."""
    gs = _gs_with_manifest({"grix": {"template": "goblin"}}, location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    gs.npcs["snik"] = NPC(name="Snik", ability_modifiers={"dex": 0})
    # Manually craft known combat state: aldric first, snik second; pointer on snik.
    gs.combat_order = ["aldric", "snik"]
    gs.combat_index = 1          # pointer on snik
    gs.combat_round = 1
    # Set combat_initiatives["aldric"]=1 so any goblin roll beats it and slots first.
    gs.combat_initiatives = {"aldric": 1, "snik": 0}

    rules.seed(0)  # just need any roll > 1; verify pointer shifts
    res = tools.dispatch("add_npc", {"instance_id": "grix"}, gs)
    assert res["ok"] is True
    grix_pos = gs.combat_order.index("grix")
    if grix_pos <= 1:   # inserted at or before old combat_index (1)
        assert gs.combat_order[gs.combat_index] == "snik"   # pointer followed
    else:               # inserted after → pointer unchanged at 1
        assert gs.combat_index == 1


def test_add_npc_in_combat_cleared_by_end_combat():
    """end_combat must clear combat_initiatives so no stale entries remain."""
    rules.seed(0)
    gs = _gs_with_manifest({"grix": {"template": "goblin"}}, location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    tools.dispatch("start_combat", {"combatants": ["aldric"]}, gs)
    tools.dispatch("add_npc", {"instance_id": "grix"}, gs)
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
    gs = _gs_with_manifest({"grix": {"template": "goblin"}}, location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    tools.dispatch("start_combat", {"combatants": ["aldric"]}, gs)
    tools.dispatch("add_npc", {"instance_id": "grix"}, gs)

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
    """instance_id 'Grix' is rejected when 'grix' already exists as an NPC key.

    The manifest declares both casings so the rejection is the duplicate guard,
    not the manifest gate."""
    gs = _gs_with_manifest({"grix": {"template": "goblin"}, "Grix": {"template": "orc"}})
    tools.dispatch("add_npc", {"instance_id": "grix"}, gs)
    res = tools.dispatch("add_npc", {"instance_id": "Grix"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "already_spawned"
    assert gs.npcs["grix"].name == "Goblin"  # original untouched


def test_add_npc_duplicate_check_rejects_party_key_case_insensitive():
    """instance_id 'Aldric' is rejected when party key 'aldric' already exists."""
    gs = _gs_with_manifest({"Aldric": {"template": "goblin"}})
    gs.party["aldric"] = Character(name="Aldric")
    res = tools.dispatch("add_npc", {"instance_id": "Aldric"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "already_spawned"


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


def test_modify_hp_rejects_excessive_amount():
    """modify_hp refuses a magnitude larger than the target's max HP — the model
    cannot invent an arbitrary HP swing, and HP is unchanged on rejection.

    This is the canonical agent/chatbot distinction for the one tool that takes a
    model-supplied number: the engine refuses, the model must narrate the failure.
    """
    gs = GameState(location="Test")
    gs.party["hero"] = Character(name="Hero", max_hp=20, hp=20, ac=12)

    # Excessive damage is refused; HP untouched.
    res = tools.dispatch("modify_hp", {"target": "Hero", "amount": -9999}, gs)
    assert res["ok"] is False
    assert res["reason"] == "amount_out_of_range"
    assert gs.party["hero"].hp == 20

    # Excessive healing is refused the same way.
    gs.party["hero"].hp = 5
    res = tools.dispatch("modify_hp", {"target": "Hero", "amount": 100}, gs)
    assert res["ok"] is False
    assert res["reason"] == "amount_out_of_range"
    assert gs.party["hero"].hp == 5


def test_modify_hp_allows_amount_up_to_max_hp():
    """A flat amount whose magnitude equals max HP is the boundary and is allowed
    (e.g. an exactly-lethal hazard), so the bound rejects only what exceeds it."""
    gs = GameState(location="Test")
    gs.party["hero"] = Character(name="Hero", max_hp=20, hp=20, ac=12)

    res = tools.dispatch("modify_hp", {"target": "Hero", "amount": -20}, gs)
    assert res["ok"] is True
    assert gs.party["hero"].hp == 0


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
    take_turn routes to the post-combat wrap-up, no combat-turn closing prompt emitted.
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

    # No terminal model turn: attack sets action_used, so _execute breaks right after
    # the tool resolves. The engine (not the model) ends combat via _maybe_end_combat
    # while combat_round is still > 0.
    narr_block = MagicMock()
    narr_block.type = "text"
    narr_block.text = "Silence falls. The passage yawns ahead. What do you do?"
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, narr_resp]

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
    narrate_call_msgs = fake_client.messages.create.call_args_list[1][1]["messages"]
    last_user_content = next(
        m["content"] for m in reversed(narrate_call_msgs) if m["role"] == "user"
    )
    assert "Combat is over" in str(last_user_content)

    # No combat-turn prompt appended
    assert "Aldric, what do you do?" not in narration
    assert "Snik, what do you do?" not in narration


def test_combat_pc_action_narrated_when_next_combatant_is_pc():
    """Regression: in combat, a PC's action must be narrated even when the NEXT
    combatant in initiative is another conscious PC (so no NPC beats accumulate).

    The bug: narration was gated on combat_beats being non-empty. When a PC acted
    and the following combatant was also a PC, the NPC loop advanced the pointer and
    broke immediately, producing zero beats — so _narrate_turn was never called and
    the player's own action went un-narrated (only a 'thinking' API call, no
    narration). Observed across Wisp's turns in the order wisp -> aldric -> grik -> narl.
    """
    from unittest.mock import MagicMock, patch
    from src.dm_agent import DMAgent

    gs = GameState(location="Arena")
    gs.party["wisp"] = Character(name="Wisp", spell_slots={1: 1}, spells=["magic_missile"],
                                 spellcasting_ability="int", ability_modifiers={"int": 4})
    gs.party["aldric"] = Character(name="Aldric", inventory=["mace"], ability_modifiers={"str": 2})
    gs.npcs["snik"] = NPC(name="Snik", max_hp=30, hp=30, ac=10)

    # Force the order so Wisp acts, then a PC (Aldric) is next — no NPC beats this turn.
    tools.dispatch("start_combat", {"combatants": ["wisp", "aldric", "snik"]}, gs)
    gs.combat_order = ["wisp", "aldric", "snik"]
    gs.combat_index = 0
    gs.combat_round = 1
    gs.action_used = False
    gs.combat_starting = False

    # _execute: model casts magic_missile at Snik; action_used set -> _execute breaks
    # (no terminal model turn). One create() call for the tool-use phase.
    cast_block = MagicMock()
    cast_block.type = "tool_use"; cast_block.id = "t1"
    cast_block.name = "cast_spell"
    cast_block.input = {"caster": "Wisp", "spell_level": 1, "spell_name": "magic_missile", "target": "Snik"}
    exec_resp = MagicMock(); exec_resp.stop_reason = "tool_use"; exec_resp.content = [cast_block]

    narr_block = MagicMock(); narr_block.type = "text"
    narr_block.text = "Three darts of force slam into Snik."
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, narr_resp]
    agent = DMAgent(gs, client=fake_client)

    with patch.object(rules._rng, "randint", side_effect=[2, 2, 2]):  # 3d4 = 6, +3 = 9 dmg
        narration = agent.take_turn("Wisp fires magic missile at Snik")

    # Combat continues — Snik survived, round still > 0.
    assert gs.npcs["snik"].hp == 21
    assert gs.combat_round > 0
    # A narration call WAS made — the bug skipped it entirely (only the tool-use call).
    assert fake_client.messages.create.call_count == 2
    # The player's action narration reaches the player.
    assert "Three darts of force" in narration
    # _narrate_turn was the vehicle (its prompt enumerates beats; the player action is beat 1).
    narr_prompt = str(fake_client.messages.create.call_args_list[1][1]["messages"])
    assert "Narrate each of the following" in narr_prompt
    # The closing prompt addresses the next actor (Aldric).
    assert "Aldric" in narration


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


def test_model_end_combat_in_terminal_scene_still_fires_victory_epilogue():
    """Regression: a *model*-issued end_combat in a terminal scene must still declare
    victory and fire the epilogue.

    The bug: dispatch('end_combat') zeroes combat_round, so the subsequent
    _maybe_end_combat() short-circuited on its combat_round==0 guard and never set
    game_over — leaving the run stuck in a terminal scene with the epilogue unfired.
    The verdict now lives in _adjudicate_combat_outcome, called regardless of who
    ended combat, so the epilogue fires either way.
    """
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="The Vault", current_scene="vault")
    gs.scenes = {"vault": {"location": "The Vault", "exits": {}}}  # terminal: no exits
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100})
    gs.npcs["snik"] = NPC(name="Snik", max_hp=6, hp=0)  # last hostile already down

    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    gs.action_used = True  # Aldric has acted; the model now wraps up the fight

    # 1. _execute: model calls end_combat itself (the bug trigger). action_used is set,
    #    so _execute breaks here — no terminal model turn.
    ec_block = MagicMock()
    ec_block.type = "tool_use"; ec_block.id = "t1"
    ec_block.name = "end_combat"; ec_block.input = {}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [ec_block]

    # 2. _narrate_epilogue: the triumphant closing paragraph.
    epi_block = MagicMock()
    epi_block.type = "text"
    epi_block.text = "The vault falls silent. The party stands victorious amid the dust."
    epi_resp = MagicMock(); epi_resp.stop_reason = "end_turn"; epi_resp.content = [epi_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, epi_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("Aldric ends the fight")

    # Verdict declared despite the model — not the engine — ending combat.
    assert gs.game_over is True
    assert gs.game_outcome == "victory"
    assert gs.combat_round == 0 and gs.combat_order == []

    # The epilogue fired: its prose is returned, and the second call used the
    # victory-epilogue prompt ("the party has prevailed").
    assert "victorious" in narration
    epi_call_msgs = fake_client.messages.create.call_args_list[1][1]["messages"]
    last_user = next(m["content"] for m in reversed(epi_call_msgs) if m["role"] == "user")
    assert "the party has prevailed" in str(last_user)


def test_start_combat_prints_initiative_banner_once():
    """A plain start_combat must print a deterministic initiative readout to the player
    once, mirroring the ambush announcement — built by the engine, not the model."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Cave", current_scene="cave")
    gs.scenes = {"cave": {"location": "Cave", "exits": {"out": "exit"}}}
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100})  # leads
    gs.npcs["snik"] = NPC(name="Snik", max_hp=10, hp=10, ability_modifiers={"dex": -5})

    # 1. _execute: model calls start_combat (terminal call for this input).
    sc_block = MagicMock()
    sc_block.type = "tool_use"; sc_block.id = "t1"
    sc_block.name = "start_combat"; sc_block.input = {"combatants": ["Aldric", "Snik"]}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [sc_block]

    # 2. terminating turn: the model narrates the outbreak (no initiative list).
    narr_block = MagicMock()
    narr_block.type = "text"
    narr_block.text = "Steel rasps free as the goblin lunges."
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("Aldric attacks the goblin")

    # Deterministic banner present, in initiative order, exactly once.
    assert "**Initiative order:** Aldric → Snik" in narration
    assert narration.count("Initiative order:") == 1
    # It is a trailer, not stored in the rolling narration history.
    assert "Initiative order" not in agent.narration_history[-1][1]


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
    gs.combat_starting = False  # simulate take_turn clearing the barrier before next_turn
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


# --- set_quest_flag validation -----------------------------------------------

def _flag_state():
    gs = GameState(location="Test")
    return gs


def test_set_flag_normalizes_spaces_and_case():
    gs = _flag_state()
    res = tools.dispatch("set_quest_flag", {"flag": "Met The Oracle", "value": True}, gs)
    assert res["ok"] is True
    assert res["flag"] == "met_the_oracle"
    assert "met_the_oracle" in gs.quest_flags


def test_set_flag_normalizes_hyphens():
    gs = _flag_state()
    res = tools.dispatch("set_quest_flag", {"flag": "hero-reborn", "value": True}, gs)
    assert res["ok"] is True
    assert res["flag"] == "hero_reborn"


def test_set_flag_strips_illegal_chars():
    """Chars outside [a-z0-9_] are removed; the remainder is stored."""
    gs = _flag_state()
    res = tools.dispatch("set_quest_flag", {"flag": "door.opened!", "value": True}, gs)
    assert res["ok"] is True
    assert res["flag"] == "dooropened"


def test_set_flag_rejects_empty_after_normalization():
    """A key that normalizes to empty must return ok=False, reason bad_flag_key,
    and must not write anything to quest_flags."""
    for bad in ("", "   ", "!!!", "..."):
        gs = _flag_state()
        res = tools.dispatch("set_quest_flag", {"flag": bad, "value": True}, gs)
        assert res["ok"] is False, f"expected rejection for {bad!r}"
        assert res["reason"] == "bad_flag_key"
        assert gs.quest_flags == {}


def test_set_flag_rejects_reserved_keys():
    """Engine-owned keys must be rejected with reason reserved_flag_key."""
    for key in ("hp", "max_hp", "ac", "spell_slots", "damage", "initiative"):
        gs = _flag_state()
        res = tools.dispatch("set_quest_flag", {"flag": key, "value": True}, gs)
        assert res["ok"] is False, f"expected reserved rejection for {key!r}"
        assert res["reason"] == "reserved_flag_key"
        assert gs.quest_flags == {}


def test_set_flag_reserved_key_checked_after_normalization():
    """'HP' and 'Max-HP' normalize to reserved keys and must also be rejected."""
    for raw in ("HP", "Max-HP", "SPELL_SLOTS"):
        gs = _flag_state()
        res = tools.dispatch("set_quest_flag", {"flag": raw, "value": True}, gs)
        assert res["ok"] is False, f"expected reserved rejection for {raw!r}"
        assert res["reason"] == "reserved_flag_key"


def test_set_flag_accepts_all_primitive_value_types():
    """bool, str, int, float, and None must all be accepted."""
    primitives = [True, False, "slain", 42, 3.14, None]
    for val in primitives:
        gs = _flag_state()
        res = tools.dispatch("set_quest_flag", {"flag": "story_event", "value": val}, gs)
        assert res["ok"] is True, f"primitive {val!r} rejected unexpectedly"
        assert gs.quest_flags["story_event"] == val


def test_set_flag_rejects_non_primitive_value():
    """Lists and dicts are not JSON primitives and must be rejected."""
    for bad_val in ([], {}, [1, 2], {"a": 1}):
        gs = _flag_state()
        res = tools.dispatch("set_quest_flag", {"flag": "event", "value": bad_val}, gs)
        assert res["ok"] is False, f"expected rejection for value {bad_val!r}"
        assert res["reason"] == "bad_flag_value"
        assert gs.quest_flags == {}


def test_set_flag_default_value_is_true():
    """Omitting value must store True (not raise)."""
    gs = _flag_state()
    res = tools.dispatch("set_quest_flag", {"flag": "door_open"}, gs)
    assert res["ok"] is True
    assert gs.quest_flags["door_open"] is True


def test_set_flag_overwrites_existing():
    """Setting the same key twice keeps only the latest value."""
    gs = _flag_state()
    tools.dispatch("set_quest_flag", {"flag": "stage", "value": 1}, gs)
    tools.dispatch("set_quest_flag", {"flag": "stage", "value": 2}, gs)
    assert gs.quest_flags["stage"] == 2


def test_set_flag_stored_key_is_normalized():
    """The stored key in quest_flags is always the normalized form."""
    gs = _flag_state()
    tools.dispatch("set_quest_flag", {"flag": "Found The Key"}, gs)
    assert "found_the_key" in gs.quest_flags
    assert "Found The Key" not in gs.quest_flags


# --- clear_quest_flag --------------------------------------------------------

def test_clear_flag_removes_existing_key():
    gs = _flag_state()
    tools.dispatch("set_quest_flag", {"flag": "door_open", "value": True}, gs)
    res = tools.dispatch("clear_quest_flag", {"flag": "door_open"}, gs)
    assert res["ok"] is True
    assert res["removed"] is True
    assert res["flag"] == "door_open"
    assert "door_open" not in gs.quest_flags


def test_clear_flag_no_op_on_missing_key():
    gs = _flag_state()
    res = tools.dispatch("clear_quest_flag", {"flag": "nonexistent"}, gs)
    assert res["ok"] is True
    assert res["removed"] is False
    assert gs.quest_flags == {}


def test_clear_flag_normalizes_key():
    """Key normalization must match set_quest_flag so the same flag is targeted."""
    gs = _flag_state()
    tools.dispatch("set_quest_flag", {"flag": "met_the_oracle", "value": True}, gs)
    res = tools.dispatch("clear_quest_flag", {"flag": "Met The Oracle"}, gs)
    assert res["ok"] is True
    assert res["removed"] is True
    assert "met_the_oracle" not in gs.quest_flags


def test_clear_flag_rejects_empty_key():
    gs = _flag_state()
    res = tools.dispatch("clear_quest_flag", {"flag": "!!!"}, gs)
    assert res["ok"] is False
    assert res["reason"] == "bad_flag_key"


def test_clear_flag_missing_key_does_not_crash():
    """No-op on a missing key must not raise and must leave quest_flags unchanged."""
    gs = _flag_state()
    gs.quest_flags["existing"] = True
    res = tools.dispatch("clear_quest_flag", {"flag": "does_not_exist"}, gs)
    assert res["ok"] is True
    assert res["removed"] is False
    assert gs.quest_flags == {"existing": True}  # untouched


# --- quest_flag canonical tests (items 4–5 spec) ----------------------------

def test_set_and_overwrite():
    """set True → ok=True, flag is True; set same key to False → overwrites; exactly one key."""
    gs = _flag_state()
    res1 = tools.dispatch("set_quest_flag", {"flag": "door_unlocked", "value": True}, gs)
    assert res1["ok"] is True
    assert gs.quest_flags["door_unlocked"] is True

    res2 = tools.dispatch("set_quest_flag", {"flag": "door_unlocked", "value": False}, gs)
    assert res2["ok"] is True
    assert gs.quest_flags["door_unlocked"] is False
    assert len(gs.quest_flags) == 1   # overwrite, not append


def test_key_normalization():
    """'Door Unlocked' and 'door-unlocked' both normalize to 'door_unlocked'; only one key stored."""
    gs = _flag_state()
    tools.dispatch("set_quest_flag", {"flag": "Door Unlocked", "value": True}, gs)
    tools.dispatch("set_quest_flag", {"flag": "door-unlocked", "value": True}, gs)
    assert len(gs.quest_flags) == 1
    assert "door_unlocked" in gs.quest_flags


def test_roundtrip_safe():
    """After several valid sets json.dumps(quest_flags) must succeed without raising."""
    import json as _json
    gs = _flag_state()
    for flag, val in [
        ("clue_found", True),
        ("npc_disposition", "friendly"),
        ("secret_level", 3),
        ("completion_ratio", 0.5),
        ("unused_slot", None),
    ]:
        tools.dispatch("set_quest_flag", {"flag": flag, "value": val}, gs)
    assert _json.dumps(gs.quest_flags)  # must not raise


def test_clear():
    """clear removes an existing flag (removed=True); clearing a missing key is a no-op (removed=False)."""
    gs = _flag_state()
    tools.dispatch("set_quest_flag", {"flag": "door_unlocked", "value": True}, gs)

    res_remove = tools.dispatch("clear_quest_flag", {"flag": "door_unlocked"}, gs)
    assert res_remove["ok"] is True
    assert res_remove["removed"] is True
    assert "door_unlocked" not in gs.quest_flags

    res_noop = tools.dispatch("clear_quest_flag", {"flag": "door_unlocked"}, gs)
    assert res_noop["ok"] is True
    assert res_noop["removed"] is False


def test_snapshot_surfaces_flags():
    """_state_snapshot includes quest_flags for the model; print_state (/state) omits them.

    String-valued flags are redacted the same way get_state redacts them — the
    snapshot is injected every turn, so a raw string secret here would leak
    continuously. Boolean/numeric flags pass through so the model can read them.
    """
    import io, contextlib, json as _json
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent
    from src.main import print_state

    gs = GameState(location="Test Hall")
    gs.party["aldric"] = Character(name="Aldric")
    tools.dispatch("set_quest_flag", {"flag": "lever_pulled", "value": True}, gs)
    tools.dispatch("set_quest_flag", {"flag": "oracle_spoke", "value": "warned"}, gs)

    agent = DMAgent(gs, client=MagicMock())
    raw_snap = agent._state_snapshot()
    snap = _json.loads(raw_snap)

    # Boolean flags pass through; string flags are redacted (no secret leak).
    assert snap["quest_flags"]["lever_pulled"] is True
    assert snap["quest_flags"]["oracle_spoke"] == "<redacted>"
    assert "warned" not in raw_snap

    # Player-facing /state must not expose any quest flag (flags may be DM-secrets).
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print_state(gs)
    player_view = buf.getvalue()
    assert "lever_pulled" not in player_view
    assert "oracle_spoke" not in player_view


def test_snapshot_redacts_string_password_flag():
    """A password accidentally stored as a string flag never reaches the per-turn
    snapshot verbatim — regression for the get_state/snapshot redaction parity gap."""
    import json as _json
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(location="Vault")
    gs.party["aldric"] = Character(name="Aldric")
    gs.quest_flags["secret_phrase"] = "ashfall"

    agent = DMAgent(gs, client=MagicMock())
    raw_snap = agent._state_snapshot()
    assert "ashfall" not in raw_snap
    assert _json.loads(raw_snap)["quest_flags"]["secret_phrase"] == "<redacted>"


# --- narrative persistence & launch-mode tests --------------------------------

def test_narrative_persisted():
    """Narrative beats survive to_dict -> from_dict round-trip intact."""
    gs = GameState(location="Dungeon")
    gs.party["hero"] = Character(name="Hero")
    gs.narrative.append({"turn": 1, "text": "The hero slays the goblin."})
    gs.narrative.append({"turn": 2, "text": "The treasure chest is opened."})

    restored = GameState.from_dict(gs.to_dict())
    assert restored.narrative == gs.narrative
    assert restored.narrative[0]["text"] == "The hero slays the goblin."
    assert restored.narrative[1]["text"] == "The treasure chest is opened."


def test_launch_mode():
    """States with any play history are 'resume'; fresh states are 'new'."""
    from src.main import _launch_mode

    fresh = GameState(location="Start", scene="The adventure begins.")
    assert _launch_mode(fresh) == "new"

    with_narrative = GameState()
    with_narrative.narrative.append({"turn": 1, "text": "Something happened."})
    assert _launch_mode(with_narrative) == "resume"

    with_turn = GameState()
    with_turn.turn = 1
    assert _launch_mode(with_turn) == "resume"

    in_combat = GameState()
    in_combat.combat_round = 2
    assert _launch_mode(in_combat) == "resume"


def test_resume_opening_is_saved_tail():
    """_resume_opening returns last DM beat verbatim; scene text does not bleed in."""
    from src.main import _resume_opening

    scene_text = "The torch-lit hall stretches before you."
    last_beat = "Aldric drives his blade into the troll's knee; it staggers, roaring."

    gs = GameState(scene=scene_text)
    gs.narrative.append({"turn": 1, "text": "First beat."})
    gs.narrative.append({"turn": 2, "text": last_beat})

    opening = _resume_opening(gs)
    assert opening == last_beat
    assert scene_text not in opening


def test_resume_does_not_restart_combat():
    """The resume launch path is pure — it never calls tools or mutates combat state."""
    from src.main import _launch_mode, _resume_opening

    gs = GameState(location="Arena")
    gs.party["hero"] = Character(name="Hero")
    gs.npcs["goblin"] = NPC(name="Goblin", hostile=True)
    gs.narrative.append({"turn": 1, "text": "Combat erupts!"})
    gs.combat_round = 2
    gs.combat_order = ["hero", "goblin"]
    gs.combat_index = 1

    round_before = gs.combat_round
    index_before = gs.combat_index

    assert _launch_mode(gs) == "resume"
    opening = _resume_opening(gs)

    assert gs.combat_round == round_before
    assert gs.combat_index == index_before
    assert gs.combat_order == ["hero", "goblin"]  # unchanged — start_combat not called
    assert opening == "Combat erupts!"


def test_new_uses_scene_intro():
    """Fresh scenario is 'new'; resume opening is empty so scene text is the display."""
    from src.main import _launch_mode, _resume_opening

    scene = "You stand at the entrance to the Iron Keep."
    gs = GameState(scene=scene)

    assert _launch_mode(gs) == "new"
    assert _resume_opening(gs) == ""   # no narrative tail → nothing to replay
    assert gs.scene == scene           # scene text is what main() displays for new games


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


def test_character_death_save_fields_round_trip():
    gs = GameState(location="Test")
    c = Character(name="Hero", max_hp=20, hp=0,
                  death_save_successes=2, death_save_failures=1,
                  dead=False, stable=False)
    gs.party["hero"] = c
    restored = GameState.from_dict(gs.to_dict())
    rc = restored.party["hero"]
    assert rc.death_save_successes == 2
    assert rc.death_save_failures == 1
    assert rc.dead is False
    assert rc.stable is False


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


# --- apply_consumable ----------------------------------------------------------

def test_apply_consumable_healing_potion_rolls_and_applies():
    """healing_potion rolls 2d4+2 via heal; rolled == applied, hp rises by exact amount."""
    from unittest.mock import patch

    c = Character(name="Aldric", max_hp=20, hp=8)
    # Force 2d4 to [3, 4] → total = 3+4+2 = 9; hp: 8+9 = 17
    with patch.object(rules._rng, "randint", side_effect=[3, 4]):
        res = rules.apply_consumable(c, "healing_potion")

    assert res["ok"] is True
    assert res["effect"] == "heal"
    assert res["rolled"] == 9
    assert res["healed"] == 9
    assert res["hp"] == 17
    assert c.hp == 17


def test_apply_consumable_healing_potion_caps_at_max_hp():
    """Healing beyond max_hp is capped; hp is max_hp; healed reports the rolled amount."""
    from unittest.mock import patch

    c = Character(name="Aldric", max_hp=20, hp=18)
    # [4, 4] → 4+4+2 = 10; hp capped to 20; rules.heal reports healed=10 (input, not delta)
    with patch.object(rules._rng, "randint", side_effect=[4, 4]):
        res = rules.apply_consumable(c, "healing_potion")

    assert res["rolled"] == 10
    assert c.hp == 20           # capped at max
    assert res["hp"] == 20


def test_apply_consumable_pearl_of_power_restores_slot():
    """pearl_of_power increments spell_slots[1] by 1; returns new count."""
    c = Character(name="Wisp", max_hp=15, hp=15, spell_slots={1: 0})
    res = rules.apply_consumable(c, "pearl_of_power")

    assert res["ok"] is True
    assert res["effect"] == "restore_slot"
    assert res["level"] == 1
    assert res["slots_remaining"] == 1
    assert c.spell_slots[1] == 1


def test_apply_consumable_pearl_of_power_no_prior_key():
    """pearl_of_power works when spell_slots has no level-1 entry yet (defaults to 0)."""
    c = Character(name="Wisp", max_hp=15, hp=15, spell_slots={})
    res = rules.apply_consumable(c, "pearl_of_power")

    assert res["ok"] is True
    assert c.spell_slots[1] == 1


def test_apply_consumable_pearl_respects_max_slot_cap():
    """With a max_spell_slots cap, a Pearl restores up to but not past the cap."""
    c = Character(name="Wisp", max_hp=15, hp=15, spell_slots={1: 1}, max_spell_slots={1: 2})
    res = rules.apply_consumable(c, "pearl_of_power")
    assert res["ok"] is True
    assert c.spell_slots[1] == 2  # restored to the cap


def test_apply_consumable_pearl_at_cap_refused_not_consumed():
    """At the cap, a Pearl is refused (ok=False 'slots_full') and the slot is unchanged
    so dispatch leaves the item in inventory."""
    c = Character(name="Wisp", max_hp=15, hp=15, spell_slots={1: 2}, max_spell_slots={1: 2})
    res = rules.apply_consumable(c, "pearl_of_power")
    assert res["ok"] is False
    assert res["reason"] == "slots_full"
    assert c.spell_slots[1] == 2  # unchanged — no over-fill


def test_use_item_pearl_at_cap_keeps_item_and_turn():
    """use_item dispatch must not consume the Pearl or the action when slots are full."""
    gs = GameState(location="Hall")
    gs.party["wisp"] = Character(name="Wisp", max_hp=15, hp=15,
                                 spell_slots={1: 2}, max_spell_slots={1: 2},
                                 inventory=["pearl_of_power"])
    res = tools.dispatch("use_item", {"character": "Wisp", "item": "pearl_of_power"}, gs)
    assert res["ok"] is False and res["reason"] == "slots_full"
    assert "pearl_of_power" in gs.party["wisp"].inventory  # not consumed
    assert gs.action_used is False                          # turn kept alive


def test_max_spell_slots_defaults_from_scenario_allotment():
    """A scenario character (no max_spell_slots key) gets a cap equal to its starting slots."""
    scenario = {
        "current_scene": "a", "scenes": {"a": {"location": "A", "scene": "s", "npcs": {}}},
        "party": {"wisp": {"name": "Wisp", "max_hp": 15, "hp": 15, "spell_slots": {"1": 2}}},
    }
    gs = GameState.from_dict(scenario)
    assert gs.party["wisp"].max_spell_slots == {1: 2}


def test_apply_consumable_unknown_id_returns_error():
    """Unknown item_id returns ok=False 'unknown_consumable' without touching state."""
    c = Character(name="Aldric", max_hp=20, hp=10)
    res = rules.apply_consumable(c, "bottle_of_sand")

    assert res["ok"] is False
    assert res["reason"] == "unknown_consumable"
    assert "bottle_of_sand" in res["error"]
    assert c.hp == 10  # unchanged


# --- take_item -----------------------------------------------------------------

def _make_loot_state():
    gs = GameState(
        current_scene="vault",
        scenes={
            "vault": {
                "location": "The Vault",
                "scene": "Stone walls.",
                "loot": ["healing_potion"],
                "exits": {},
            }
        },
    )
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=10, inventory=[])
    return gs


def test_take_item_moves_to_inventory_and_removes_from_loot():
    """take_item adds the item to the carrier's inventory and removes it from scene loot."""
    gs = _make_loot_state()
    res = tools.dispatch("take_item", {"item": "healing_potion", "carrier": "Aldric"}, gs)

    assert res["ok"] is True
    assert res["item"] == "healing_potion"
    assert res["owner"] == "Aldric"
    assert "healing_potion" in gs.party["aldric"].inventory
    assert gs.scenes["vault"]["loot"] == []  # consumed from scene


def test_take_item_second_take_not_available():
    """The sole copy having been taken, a second take returns ok=False 'not_available'."""
    gs = _make_loot_state()
    tools.dispatch("take_item", {"item": "healing_potion", "carrier": "Aldric"}, gs)
    res = tools.dispatch("take_item", {"item": "healing_potion", "carrier": "Aldric"}, gs)

    assert res["ok"] is False
    assert res["reason"] == "not_available"
    assert gs.party["aldric"].inventory.count("healing_potion") == 1  # only first take


def test_take_item_unlisted_item_rejected():
    """An item absent from the scene loot (even a valid item id) cannot be taken."""
    gs = _make_loot_state()
    res = tools.dispatch("take_item", {"item": "longsword", "carrier": "Aldric"}, gs)

    assert res["ok"] is False
    assert res["reason"] == "not_available"
    assert gs.party["aldric"].inventory == []  # nothing granted


def test_take_item_two_copies_finite():
    """Two copies in loot allow exactly two takes; the third is not_available."""
    gs = GameState(
        current_scene="vault",
        scenes={"vault": {"location": "Vault", "loot": ["healing_potion", "healing_potion"], "exits": {}}},
    )
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=10, inventory=[])

    r1 = tools.dispatch("take_item", {"item": "healing_potion", "carrier": "Aldric"}, gs)
    r2 = tools.dispatch("take_item", {"item": "healing_potion", "carrier": "Aldric"}, gs)
    r3 = tools.dispatch("take_item", {"item": "healing_potion", "carrier": "Aldric"}, gs)

    assert r1["ok"] and r2["ok"]
    assert not r3["ok"] and r3["reason"] == "not_available"
    assert gs.party["aldric"].inventory.count("healing_potion") == 2
    assert gs.scenes["vault"]["loot"] == []


# --- use_item ------------------------------------------------------------------

def _make_use_state(combat: bool = False) -> GameState:
    gs = GameState()
    gs.party["aldric"] = Character(
        name="Aldric", max_hp=20, hp=8,
        inventory=["mace", "healing_potion", "healing_potion"],
        ability_modifiers={"dex": 100},  # guaranteed first in combat
    )
    if combat:
        gs.npcs["snik"] = NPC(name="Snik", max_hp=15, hp=15, ability_modifiers={"dex": 0})
        rules.seed(0)
        tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
        gs.combat_starting = False
    return gs


def test_use_item_heals_and_removes_exactly_one_copy():
    """use_item heals the character via the engine roll and removes exactly one inventory copy."""
    from unittest.mock import patch

    gs = _make_use_state()
    # Force 2d4 to [2, 3] → 2d4+2 = 7
    with patch.object(rules._rng, "randint", side_effect=[2, 3]):
        res = tools.dispatch("use_item", {"character": "Aldric", "item": "healing_potion"}, gs)

    assert res["ok"] is True
    assert res["effect"] == "heal"
    assert res["rolled"] == 7
    assert gs.party["aldric"].hp == 15           # 8 + 7
    assert gs.party["aldric"].inventory.count("healing_potion") == 1  # one copy remains
    assert "mace" in gs.party["aldric"].inventory                      # other items untouched


def test_use_item_not_consumable_returns_error_and_undoes_action():
    """Using a non-consumable inventory item (mace) returns ok=False 'not_consumable';
    action_used is left False so the character keeps their turn."""
    gs = _make_use_state()
    res = tools.dispatch("use_item", {"character": "Aldric", "item": "mace"}, gs)

    assert res["ok"] is False
    assert res["reason"] == "not_consumable"
    assert gs.action_used is False


def test_use_item_not_in_inventory_returns_error_and_undoes_action():
    """Using an item the character doesn't hold returns ok=False 'not_in_inventory';
    action_used is left False so the character keeps their turn."""
    gs = _make_use_state()
    res = tools.dispatch("use_item", {"character": "Aldric", "item": "greater_healing"}, gs)

    assert res["ok"] is False
    assert res["reason"] == "not_in_inventory"
    assert gs.action_used is False


# --- action economy ------------------------------------------------------------

def test_use_item_in_combat_consumes_action_and_blocks_follow_up_attack():
    """In combat, using a potion costs the character's action; a follow-up attack is refused."""
    from unittest.mock import patch

    gs = _make_use_state(combat=True)
    assert gs.combat_order[0] == "aldric"

    with patch.object(rules._rng, "randint", side_effect=[3, 2]):  # 2d4+2 = 7
        res = tools.dispatch("use_item", {"character": "Aldric", "item": "healing_potion"}, gs)

    assert res["ok"] is True
    assert gs.action_used is True

    follow_up = tools.dispatch(
        "attack", {"attacker": "Aldric", "defender": "Snik", "weapon": "mace"}, gs
    )
    assert follow_up["ok"] is False
    assert "already acted" in follow_up["error"]


def test_use_item_out_of_combat_is_free_and_does_not_set_action_used():
    """Out of combat, use_item is a free interaction; action_used stays False."""
    from unittest.mock import patch

    gs = _make_use_state()
    with patch.object(rules._rng, "randint", side_effect=[2, 2]):
        res = tools.dispatch("use_item", {"character": "Aldric", "item": "healing_potion"}, gs)

    assert res["ok"] is True
    assert gs.action_used is False


def test_use_item_on_downed_ally_revives_and_costs_giver_action():
    """A conscious PC spends their action to pour a potion into a downed ally: the
    ally revives (hp>0, death saves reset, unconscious cleared), the giver's potion
    is consumed, and it's the giver who spent the action."""
    from unittest.mock import patch

    gs = GameState()
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20,
                                   inventory=["healing_potion"], ability_modifiers={"dex": 100})
    gs.party["wisp"] = Character(name="Wisp", max_hp=16, hp=0,
                                 conditions=["unconscious"], death_save_failures=2)
    rules.seed(0)
    tools.dispatch("start_combat", {"combatants": ["aldric", "wisp"]}, gs)
    gs.combat_starting = False
    # Ensure Aldric is the active combatant (dex 100 wins initiative).
    assert gs.combat_order[gs.combat_index] == "aldric"

    with patch.object(rules._rng, "randint", side_effect=[2, 2]):  # heal dice
        res = tools.dispatch("use_item",
                             {"character": "Aldric", "item": "healing_potion", "target": "Wisp"}, gs)

    assert res["ok"] is True
    assert res["recipient"] == "Wisp"
    assert gs.party["wisp"].hp > 0                       # revived
    assert "unconscious" not in gs.party["wisp"].conditions
    assert gs.party["wisp"].death_save_failures == 0      # reset on revive
    assert "healing_potion" not in gs.party["aldric"].inventory  # giver's potion spent
    assert gs.action_used is True                         # giver spent the action


def test_use_item_unknown_target_rejected_and_keeps_turn():
    """A non-party target is rejected without consuming the item or the action."""
    gs = GameState()
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20, inventory=["healing_potion"])
    gs.npcs["snik"] = NPC(name="Snik", max_hp=10, hp=5, hostile=True)
    res = tools.dispatch("use_item",
                         {"character": "Aldric", "item": "healing_potion", "target": "Snik"}, gs)
    assert res["ok"] is False and res["reason"] == "unknown_target"
    assert "healing_potion" in gs.party["aldric"].inventory
    assert gs.action_used is False


# --- round-trip: take_item then to_dict / from_dict ----------------------------

def test_take_item_round_trip_inventory_and_loot():
    """take_item then to_dict → from_dict: inventory has the item; scene loot is depleted."""
    gs = GameState(
        current_scene="vault",
        scenes={
            "vault": {
                "location": "The Vault",
                "scene": "Stone walls.",
                "loot": ["healing_potion", "pearl_of_power"],
                "exits": {},
            }
        },
    )
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=10, inventory=[])

    tools.dispatch("take_item", {"item": "healing_potion", "carrier": "Aldric"}, gs)

    # Round-trip through dict (same path as save/load but without touching disk).
    restored = GameState.from_dict(gs.to_dict())

    assert "healing_potion" in restored.party["aldric"].inventory
    assert restored.scenes["vault"]["loot"] == ["pearl_of_power"]  # one copy gone, one remains


# --- gated exits and terminal endings -----------------------------------------

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


def test_maybe_end_combat_ungated_terminal_fires_victory():
    """_maybe_end_combat: combat ends in ungated terminal scene -> game_over=True."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(
        current_scene="end",
        scenes={"end": {"location": "End", "scene": "", "exits": {}}},
    )
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20, ability_modifiers={"dex": 0})
    gs.npcs["snik"] = NPC(name="Snik", max_hp=10, hp=0)  # already down
    gs.combat_order = ["aldric", "snik"]
    gs.combat_index = 0
    gs.combat_round = 1

    agent = DMAgent(gs, client=MagicMock())
    ended = agent._maybe_end_combat()

    assert ended is True
    assert gs.game_over is True
    assert gs.game_outcome == "victory"
    assert gs.combat_round == 0  # end_combat fired


def test_maybe_end_combat_gated_terminal_ends_combat_not_game():
    """_maybe_end_combat: combat ends in gated terminal scene -> combat cleared, game_over False."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(
        current_scene="final_chamber",
        scenes={
            "final_chamber": {
                "location": "Final Chamber",
                "scene": "",
                "exits": {},
                "exit_requires": "iron_door_open",
            },
        },
    )
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20, ability_modifiers={"dex": 0})
    gs.npcs["snik"] = NPC(name="Snik", max_hp=10, hp=0)  # already down
    gs.combat_order = ["aldric", "snik"]
    gs.combat_index = 0
    gs.combat_round = 1

    agent = DMAgent(gs, client=MagicMock())
    ended = agent._maybe_end_combat()

    assert ended is True
    assert gs.combat_round == 0   # combat ended
    assert gs.game_over is False  # victory deferred — party must open the gated exit


def test_npc_disposition_fields_round_trip():
    """disposition_dc and social_attempted survive to_dict / from_dict."""
    gs = GameState()
    gs.party["hero"] = Character(name="Hero", max_hp=10, hp=10)
    gs.npcs["guard"] = NPC(name="Guard", disposition_dc=12, social_attempted=True)
    gs.npcs["brute"] = NPC(name="Brute")  # defaults: None / False

    restored = GameState.from_dict(gs.to_dict())

    guard = restored.npcs["guard"]
    assert guard.disposition_dc == 12
    assert guard.social_attempted is True

    brute = restored.npcs["brute"]
    assert brute.disposition_dc is None
    assert brute.social_attempted is False


# ---------------------------------------------------------------------------
# resolve_npc_action
# ---------------------------------------------------------------------------

def test_resolve_npc_action_attacks_lowest_hp_pc():
    """Engine resolves a hostile NPC's attack targeting the lowest-HP conscious PC."""
    gs = GameState()
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=10)
    gs.party["wisp"]   = Character(name="Wisp",   max_hp=20, hp=5)   # lowest HP → target
    gs.npcs["snik"] = NPC(name="Snik", max_hp=10, hp=10, hostile=True)
    gs.combat_order = ["aldric", "wisp", "snik"]
    gs.combat_index = 2
    gs.combat_round = 1
    gs.action_used = False

    rules.force_rolls([15, 4])  # to-hit 15 (hits AC 12), damage 4
    result = tools.resolve_npc_action(gs.npcs["snik"], gs)

    assert result is not None
    args, res = result
    assert args["attacker"] == "Snik"
    assert args["defender"] == "Wisp"
    assert res["ok"] is True


def test_resolve_npc_action_breaks_hp_tie_by_combat_order():
    """When two PCs share HP, the one earlier in combat_order is targeted."""
    gs = GameState()
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=10)
    gs.party["wisp"]   = Character(name="Wisp",   max_hp=20, hp=10)  # same HP
    gs.npcs["snik"] = NPC(name="Snik", max_hp=10, hp=10, hostile=True)
    # Aldric is earlier in the order → tiebreak selects Aldric
    gs.combat_order = ["aldric", "wisp", "snik"]
    gs.combat_index = 2
    gs.combat_round = 1
    gs.action_used = False

    rules.force_rolls([15, 4])
    result = tools.resolve_npc_action(gs.npcs["snik"], gs)

    assert result is not None
    args, _ = result
    assert args["defender"] == "Aldric"


def test_resolve_npc_action_returns_none_non_hostile():
    """Non-hostile NPC → None (stands aside)."""
    gs = GameState()
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20)
    gs.npcs["guard"] = NPC(name="Guard", hostile=False)
    assert tools.resolve_npc_action(gs.npcs["guard"], gs) is None


def test_resolve_npc_action_returns_none_no_conscious_pc():
    """All PCs are down → None (no valid target)."""
    gs = GameState()
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=0)   # is_down
    gs.npcs["snik"] = NPC(name="Snik", hostile=True)
    gs.combat_order = ["aldric", "snik"]
    gs.combat_round = 1
    assert tools.resolve_npc_action(gs.npcs["snik"], gs) is None


def test_resolve_npc_action_returns_none_with_spells():
    """NPC with a spells attribute falls back to the model."""
    gs = GameState()
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20)
    gs.npcs["mage"] = NPC(name="Mage", hostile=True)
    gs.npcs["mage"].spells = ["magic_missile"]   # type: ignore
    gs.combat_order = ["aldric", "mage"]
    gs.combat_round = 1
    assert tools.resolve_npc_action(gs.npcs["mage"], gs) is None


# ---------------------------------------------------------------------------
# influence_npc
# ---------------------------------------------------------------------------

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


# --- failed parley auto-starts combat ----------------------------------------

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


# ---------------------------------------------------------------------------
# attempt_ambush — stealth surprise system
# ---------------------------------------------------------------------------

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
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scenes_test.json")
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


def test_ambush_npc_fields_round_trip():
    """alertness_dc and surprised survive to_dict → from_dict;
    pending_ambush and ambush_attempted are NOT in the serialized dict."""
    gs = GameState(location="Test")
    gs.npcs["guard"] = NPC(name="Guard", alertness_dc=12, surprised=True, hostile=True)
    gs.pending_ambush = True
    gs.ambush_attempted = True

    d = gs.to_dict()

    # NPC fields present
    assert d["npcs"]["guard"]["alertness_dc"] == 12
    assert d["npcs"]["guard"]["surprised"] is True

    # Transient GameState flags absent
    assert "pending_ambush" not in d
    assert "ambush_attempted" not in d

    restored = GameState.from_dict(d)
    assert restored.npcs["guard"].alertness_dc == 12
    assert restored.npcs["guard"].surprised is True
    assert restored.pending_ambush is False
    assert restored.ambush_attempted is False


def test_ambush_npc_defaults_on_old_save():
    """Old saves without alertness_dc/surprised load fine (defaults kick in)."""
    d = {
        "location": "Test",
        "npcs": {"snik": {"name": "Snik", "hp": 10, "max_hp": 10, "hostile": True}},
    }
    gs = GameState.from_dict(d)
    assert gs.npcs["snik"].alertness_dc is None
    assert gs.npcs["snik"].surprised is False


# --- companions / following ----------------------------------------------------

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


def test_companion_flag_round_trips():
    """The companion flag survives save/load."""
    gs = GameState(location="Camp")
    gs.npcs["brak"] = NPC(name="Brak", max_hp=12, hp=12, hostile=False, companion=True)
    restored = GameState.from_dict(gs.to_dict())
    assert restored.npcs["brak"].companion is True


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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")
