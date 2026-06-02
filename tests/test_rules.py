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
from tests._helpers import _make_combat_state


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
    assert res["healed"] == 20  # reports HP actually restored (0 -> 20), not the raw 50
    assert "unconscious" not in c.conditions


def test_heal_reports_effective_amount_when_capped():
    """`healed` is the HP actually restored after the max-HP cap, not the raw roll.
    A potion that rolls 7 on a target 6 below max reports healed == 6."""
    c = Character(name="Wisp", max_hp=16, hp=10)
    res = rules.heal(c, 7)
    assert res["hp"] == 16
    assert res["healed"] == 6  # capped: only 6 landed, not the rolled 7


def test_heal_reports_full_amount_when_uncapped():
    """Below the cap, `healed` equals the full amount applied."""
    c = Character(name="Aldric", max_hp=24, hp=10)
    res = rules.heal(c, 7)
    assert res["hp"] == 17
    assert res["healed"] == 7


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


def test_cast_leveled_spell_below_base_level_refused():
    """A leveled spell cannot be cast at a lower slot level than its tabled base.

    Guards the slot economy against a level-0 bypass: casting magic_missile (a
    1st-level spell) at spell_level 0 must NOT take the free-cantrip path nor the
    by_slot[max] fallback. The cast is refused before anything is consumed — no
    slot spent (including any higher slot the caster holds), no damage dealt.
    """
    caster = Character(
        name="Wisp",
        spell_slots={1: 0, 2: 1},          # out of L1, but holds an L2 slot
        spells=["magic_missile"],
    )
    target = NPC(name="Goblin", max_hp=30, hp=30)
    res = rules.cast_damaging_spell(caster, target, "magic_missile", 0)
    assert res["ok"] is False
    assert res.get("below_min_level") is True
    assert "level-1" in res["reason"]
    assert "damage" not in res                       # no damage resolved
    assert target.hp == 30                           # target untouched
    assert caster.spell_slots == {1: 0, 2: 1}        # NO slot consumed (L2 intact)


def test_cast_leveled_spell_upcast_above_base_still_allowed():
    """The min-level guard only blocks under-leveling — a legal upcast still works,
    spends the higher slot, and uses that slot's by_slot column (not the max)."""
    caster = Character(
        name="Wisp",
        spell_slots={1: 0, 2: 1},
        spells=["magic_missile"],
    )
    target = NPC(name="Goblin", max_hp=30, hp=30)
    res = rules.cast_damaging_spell(caster, target, "magic_missile", 2)
    assert res["ok"] is True
    assert res["slots_remaining"] == 0
    assert "4d4+4" in res["damage_detail"]           # L2 column, not L3 max
    assert caster.spell_slots == {1: 0, 2: 0}        # the L2 slot was spent


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


# --- skill proficiency & expertise -------------------------------------------

def test_skill_check_named_skill_adds_proficiency():
    rules.force_rolls([10])
    c = Character(name="Rogue", ability_modifiers={"dex": 3}, proficiency_bonus=2,
                  skill_proficiencies=["stealth"])
    res = rules.skill_check(c, "dex", dc=10, skill="stealth")
    assert res["skill"] == "stealth"
    assert res["proficient"] is True and res["expertise"] is False
    assert res["modifier"] == 5   # dex 3 + proficiency 2
    assert res["total"] == 15


def test_skill_check_expertise_doubles_proficiency():
    rules.force_rolls([10])
    c = Character(name="Rogue", ability_modifiers={"dex": 3}, proficiency_bonus=2,
                  skill_proficiencies=["stealth"], expertise=["stealth"])
    res = rules.skill_check(c, "dex", dc=10, skill="stealth")
    assert res["proficient"] is True and res["expertise"] is True
    assert res["modifier"] == 7   # dex 3 + 2 * proficiency 2


def test_skill_check_named_skill_not_proficient_no_bonus():
    rules.force_rolls([10])
    c = Character(name="Fighter", ability_modifiers={"dex": 1}, proficiency_bonus=2,
                  skill_proficiencies=["athletics"])  # proficient in a different skill
    res = rules.skill_check(c, "dex", dc=10, skill="stealth")
    assert res["proficient"] is False
    assert res["modifier"] == 1


def test_skill_check_named_skill_fixes_governing_ability():
    # The engine owns the skill->ability map: a named skill overrides a wrong ability arg.
    rules.force_rolls([10])
    c = Character(name="Cleric", ability_modifiers={"wis": 3, "str": -1}, proficiency_bonus=2,
                  skill_proficiencies=["perception"])
    res = rules.skill_check(c, "str", dc=10, skill="perception")
    assert res["ability"] == "wis"      # perception is a WIS skill, not the passed str
    assert res["modifier"] == 5         # wis 3 + proficiency 2


def test_skill_check_without_skill_is_raw_ability():
    # No skill named → raw ability roll, no proficiency, no skill fields (backward compatible).
    rules.force_rolls([10])
    c = Character(name="Rogue", ability_modifiers={"dex": 3}, proficiency_bonus=2,
                  skill_proficiencies=["stealth"])
    res = rules.skill_check(c, "dex", dc=10)
    assert res["modifier"] == 3
    assert "skill" not in res and "proficient" not in res


def test_skill_check_unknown_skill_keeps_passed_ability():
    rules.force_rolls([10])
    c = Character(name="Hero", ability_modifiers={"dex": 2}, proficiency_bonus=2)
    res = rules.skill_check(c, "dex", dc=10, skill="lockpicking")
    assert res["ability"] == "dex"      # unknown skill doesn't remap the ability
    assert res["proficient"] is False
    assert res["modifier"] == 2


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


# --- inspiration: a single DM-awarded reroll -------------------------------

def test_award_inspiration_grants_one():
    c = Character(name="Aldric")
    res = rules.award_inspiration(c)
    assert res["ok"] is True
    assert res["inspiration"] == 1
    assert c.inspiration == 1
    assert c.inspiration_used is False


def test_award_inspiration_refused_at_cap():
    c = Character(name="Aldric", inspiration=1)
    res = rules.award_inspiration(c)
    assert res["ok"] is False
    assert res["reason"] == "at_cap"
    assert c.inspiration == 1  # unchanged


def test_award_inspiration_refused_after_use():
    # Lifetime lock: once spent, a PC can never be re-awarded for the session.
    c = Character(name="Aldric", inspiration_used=True)
    res = rules.award_inspiration(c)
    assert res["ok"] is False
    assert res["reason"] == "already_used"
    assert c.inspiration == 0


def test_skill_check_inspiration_keeps_higher_and_spends():
    c = Character(name="Aldric", ability_modifiers={"dex": 0}, inspiration=1)
    rules.force_rolls([3, 17])  # 2d20 with advantage → keep 17
    res = rules.skill_check(c, "dex", dc=10, use_inspiration=True)
    assert res["roll"] == 17
    assert res["inspiration_used"] is True
    assert res["inspiration_rolls"] == [3, 17]
    assert c.inspiration == 0          # point spent
    assert c.inspiration_used is True  # lifetime-locked


def test_saving_throw_inspiration_keeps_higher_and_spends():
    c = Character(name="Aldric", ability_modifiers={"con": 0}, inspiration=1)
    rules.force_rolls([18, 4])  # keep 18
    res = rules.saving_throw(c, "con", dc=12, use_inspiration=True)
    assert res["roll"] == 18
    assert res["inspiration_used"] is True
    assert c.inspiration == 0 and c.inspiration_used is True


def test_use_inspiration_with_none_held_rolls_normally():
    c = Character(name="Aldric", ability_modifiers={"dex": 0})  # holds none
    rules.force_rolls([5, 19])  # only the first d20 is consumed — no advantage
    res = rules.skill_check(c, "dex", dc=10, use_inspiration=True)
    assert res["roll"] == 5
    assert res["inspiration_used"] is False
    assert res["inspiration_reason"] == "no_inspiration"
    assert c.inspiration_used is False  # nothing was spent or locked


def test_check_and_save_without_flag_unchanged():
    # Default use_inspiration=False adds no inspiration keys and consumes one d20.
    c = Character(name="Aldric", ability_modifiers={"dex": 1}, inspiration=1)
    rules.force_rolls([8, 20])
    chk = rules.skill_check(c, "dex", dc=5)
    assert chk["roll"] == 8 and "inspiration_used" not in chk
    assert c.inspiration == 1  # untouched when the flag is off
    sav = rules.saving_throw(c, "dex", dc=5)
    assert sav["roll"] == 20 and "inspiration_used" not in sav


def test_saving_throw_npc_has_no_proficiency():
    # NPC lacks proficiency_bonus / save_proficiencies — just d20 + ability mod.
    npc = NPC(name="Snik", ability_modifiers={"dex": 1})
    rules.force_rolls([14])
    res = rules.saving_throw(npc, "dex", dc=10)
    assert res["proficient"] is False
    assert res["modifier"] == 1
    assert res["total"] == 15


# --- advantage / disadvantage -------------------------------------------------

def test_d20_advantage_keeps_higher():
    rules.force_rolls([3, 17])
    nat, rolls = rules._d20(advantage=True)
    assert nat == 17 and rolls == [3, 17]


def test_d20_disadvantage_keeps_lower():
    rules.force_rolls([17, 3])
    nat, rolls = rules._d20(disadvantage=True)
    assert nat == 3 and rolls == [17, 3]


def test_d20_advantage_and_disadvantage_cancel():
    # Both set → a single straight d20 (only one value consumed).
    rules.force_rolls([5, 20])
    nat, rolls = rules._d20(advantage=True, disadvantage=True)
    assert nat == 5 and rolls == [5]


def _melee_pc():
    return Character(name="A", attack_bonus=0, inventory=["mace"],
                     ability_modifiers={"str": 0}, proficiency_bonus=2)


def test_attack_advantage_keeps_higher_to_hit():
    rules.force_rolls([3, 17, 4])  # 2d20 keep 17, then 4 damage
    res = rules.attack(_melee_pc(), NPC(name="D", ac=1, hp=30, max_hp=30), "mace", advantage=True)
    assert res["to_hit_roll"] == 17
    assert res["roll_mode"] == "advantage"
    assert res["to_hit_rolls"] == [3, 17]
    assert res["hit"] is True


def test_attack_disadvantage_keeps_lower_to_hit():
    rules.force_rolls([17, 3])  # keep 3 → to-hit 5 vs AC 10 → miss, no damage die
    res = rules.attack(_melee_pc(), NPC(name="D", ac=10, hp=30, max_hp=30), "mace", disadvantage=True)
    assert res["to_hit_roll"] == 3
    assert res["roll_mode"] == "disadvantage"
    assert res["to_hit_rolls"] == [17, 3]
    assert res["hit"] is False


def test_attack_without_flags_has_no_roll_mode():
    rules.force_rolls([12, 4])
    res = rules.attack(_melee_pc(), NPC(name="D", ac=1, hp=30, max_hp=30), "mace")
    assert res["to_hit_roll"] == 12
    assert "roll_mode" not in res and "to_hit_rolls" not in res


def test_attack_both_flags_cancel_to_straight_roll():
    rules.force_rolls([9, 20, 4])  # both set → one d20 (9); 20 is the damage die
    res = rules.attack(_melee_pc(), NPC(name="D", ac=1, hp=30, max_hp=30), "mace",
                       advantage=True, disadvantage=True)
    assert res["to_hit_roll"] == 9
    assert "roll_mode" not in res  # cancelled → not reported as a mode


def _spell_attacker():
    return Character(name="Wisp", level=1, spells=["chromatic_orb"], spell_slots={1: 1},
                     spellcasting_ability="int", ability_modifiers={"int": 3})


def test_cast_damaging_spell_advantage_keeps_higher():
    rules.force_rolls([2, 16, 4, 4, 4])  # 2d20 keep 16, then 3d8 damage
    res = rules.cast_damaging_spell(_spell_attacker(), NPC(name="D", ac=1, hp=40, max_hp=40),
                                    "chromatic_orb", 1, advantage=True)
    assert res["to_hit_roll"] == 16
    assert res["roll_mode"] == "advantage"
    assert res["to_hit_rolls"] == [2, 16]
    assert res["hit"] is True


def test_cast_damaging_spell_disadvantage_keeps_lower():
    rules.force_rolls([18, 2])  # keep 2 → to-hit 7 vs AC 25 → miss
    res = rules.cast_damaging_spell(_spell_attacker(), NPC(name="D", ac=25, hp=40, max_hp=40),
                                    "chromatic_orb", 1, disadvantage=True)
    assert res["to_hit_roll"] == 2
    assert res["roll_mode"] == "disadvantage"
    assert res["hit"] is False


def test_cast_auto_hit_spell_ignores_advantage():
    # magic_missile auto-hits — no attack roll, so the flag is inert (no roll_mode).
    caster = Character(name="Wisp", level=1, spells=["magic_missile"], spell_slots={1: 1},
                       spellcasting_ability="int", ability_modifiers={"int": 3})
    res = rules.cast_damaging_spell(caster, NPC(name="D", ac=10, hp=40, max_hp=40),
                                    "magic_missile", 1, advantage=True)
    assert res["auto_hit"] is True
    assert "roll_mode" not in res and "to_hit_roll" not in res


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


# --- hazards & traps (author-placed, engine-owned numbers) -------------------


# --- add_npc dispatch tests ---------------------------------------------------


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


# --- game over: victory and defeat -------------------------------------------


# --- exits / scene topology tests -------------------------------------------


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


# --- gated exits and terminal endings -----------------------------------------


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


# --- failed parley auto-starts combat ----------------------------------------


# ---------------------------------------------------------------------------
# attempt_ambush — stealth surprise system
# ---------------------------------------------------------------------------


# --- companions / following ----------------------------------------------------


# --- SRD_RULES / lookup_rule coverage ---------------------------------------

def test_srd_covers_core_mechanics():
    """Every core mechanic the engine enforces must have a lookup_rule entry, so the
    model narrates it from the reference (aligned with the code) rather than training
    memory. The expected set is the contract: adding a mechanic means adding both the
    SRD_RULES entry and its key here (per CLAUDE.md's "add a test for any new rules
    function"). Fails loudly if an entry is dropped or a core mechanic ships unreferenced."""
    expected = {
        "advantage", "disadvantage", "death_saves", "saving_throws", "spell_slots",
        "armor_class", "attack", "critical_hit", "skill_check", "spell_attack",
        "initiative", "surprise", "influence", "magic_missile", "chromatic_orb",
        "hazards", "healing", "using_items", "proficiency_bonus",
    }
    missing = expected - set(rules.SRD_RULES)
    assert not missing, f"SRD_RULES is missing core entries: {sorted(missing)}"
    for key in expected:
        assert rules.lookup_rule(key)["ok"] is True, f"lookup_rule({key!r}) should resolve"


def test_lookup_rule_unknown_topic():
    res = rules.lookup_rule("teleportation")
    assert res["ok"] is False
    assert "Known topics" in res["text"]   # miss path lists the available topics
