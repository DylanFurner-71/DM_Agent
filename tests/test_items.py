"""Items, consumables & inventory enforcement — no API.

take_item / use_item / apply_consumable and their action economy. Moved out of test_rules.py."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import rules, tools
from src.game_state import Character, NPC, GameState


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
