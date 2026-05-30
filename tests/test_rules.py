"""Tests for the enforcement core. These run with no network/API needed and are
the proof that the rules are enforced in code, not by model goodwill.

Run:  python -m pytest -q     (or)     python tests/test_rules.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import rules
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
