"""Tests for the scenario validator — no API, no network.

A valid base scenario is mutated to trip each check, asserting the right
severity (error vs warning). Errors mean the scenario would misbehave or fail to
load; warnings mean a likely authoring mistake the engine can fall back from.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import validate
from src.validate import validate_scenario, _is_valid_dice


def _valid() -> dict:
    """A minimal, clean two-scene scenario: a → b, b terminal."""
    return {
        "current_scene": "a",
        "scenes": {
            "a": {
                "location": "Room A",
                "scene": "The first room.",
                "npcs": {"snik": {"template": "goblin", "name": "Snik", "hostile": True}},
                "loot": ["healing_potion", "bronze_key"],
                "exits": {"the north arch": "b"},
            },
            "b": {
                "location": "Room B",
                "scene": "The last room.",
                "npcs": {},
                "exits": {},
            },
        },
        "party": {
            "hero": {
                "name": "Hero",
                "spells": ["magic_missile"],
                "spellcasting_ability": "int",
            }
        },
    }


def _errs(data) -> str:
    return "\n".join(validate_scenario(data).errors)


def _warns(data) -> str:
    return "\n".join(validate_scenario(data).warnings)


# --- the happy path -----------------------------------------------------------

def test_valid_scenario_is_clean():
    rep = validate_scenario(_valid())
    assert rep.ok, rep.errors
    assert rep.errors == []
    assert rep.warnings == [], rep.warnings


def test_all_shipped_scenarios_validate():
    """Every scenario shipped under data/ (incl. data/demos/) must pass cleanly."""
    import glob
    data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
    files = glob.glob(os.path.join(data_dir, "**", "*.json"), recursive=True)
    assert files, "expected scenario files in data/"
    for path in files:
        with open(path) as f:
            rep = validate_scenario(json.load(f))
        assert rep.ok, f"{os.path.relpath(path, data_dir)} has errors: {rep.errors}"


# --- structural / load-crash errors ------------------------------------------

def test_exit_to_unknown_scene_is_error():
    d = _valid()
    d["scenes"]["a"]["exits"]["the north arch"] = "nowhere"
    assert "not a defined scene" in _errs(d)


def test_unknown_template_is_error():
    d = _valid()
    d["scenes"]["a"]["npcs"]["snik"]["template"] = "gobblin"
    out = _errs(d)
    assert "not in MONSTERS" in out
    assert "goblin" in out  # close-match suggestion


def test_unknown_npc_field_is_error():
    d = _valid()
    d["scenes"]["a"]["npcs"]["snik"]["hostle"] = True  # typo'd 'hostile'
    assert "unknown NPC field" in _errs(d)


def test_inline_npc_without_name_is_error():
    d = _valid()
    d["scenes"]["a"]["npcs"]["mystery"] = {"max_hp": 10, "hp": 10}
    assert "needs a 'name'" in _errs(d)


def test_party_unknown_field_is_error():
    d = _valid()
    d["party"]["hero"]["hsp"] = 30  # typo'd field
    assert "unknown character field" in _errs(d)


def test_party_missing_name_is_error():
    d = _valid()
    del d["party"]["hero"]["name"]
    assert "missing required 'name'" in _errs(d)


def test_empty_party_is_error():
    d = _valid()
    d["party"] = {}
    assert "non-empty 'party'" in _errs(d)


def test_non_integer_spell_slot_key_is_error():
    d = _valid()
    d["party"]["hero"]["spell_slots"] = {"one": 2}
    assert "is not an integer" in _errs(d)


def test_current_scene_not_defined_is_error():
    d = _valid()
    d["current_scene"] = "ghost"
    assert "not a defined scene" in _errs(d)


def test_current_scene_missing_is_error():
    d = _valid()
    del d["current_scene"]
    assert "current_scene" in _errs(d)


# --- gate-flag checks ---------------------------------------------------------

def test_unnormalized_gate_flag_is_error():
    d = _valid()
    d["scenes"]["a"]["exits"]["the north arch"] = {
        "to": "b", "requires": "Ward Lowered", "denied": "barred",
    }
    out = _errs(d)
    assert "not in normalized form" in out
    assert "ward_lowered" in out


def test_reserved_gate_flag_is_error():
    d = _valid()
    d["scenes"]["a"]["exits"]["the north arch"] = {
        "to": "b", "requires": "hp", "denied": "barred",
    }
    assert "reserved engine key" in _errs(d)


def test_empty_answer_gate_is_error():
    d = _valid()
    d["scenes"]["a"]["exits"]["the north arch"] = {
        "to": "b", "requires_answer": "  ", "denied": "barred",
    }
    assert "non-empty password" in _errs(d)


def test_answer_gate_without_denied_is_warning():
    d = _valid()
    d["scenes"]["a"]["exits"]["the north arch"] = {"to": "b", "requires_answer": "ashfall"}
    assert "no 'denied' text" in _warns(d)


# --- hazards ------------------------------------------------------------------

def test_hazard_bad_ability_is_error():
    d = _valid()
    d["scenes"]["a"]["hazards"] = {"darts": {"ability": "luck", "dc": 13, "damage": "2d6"}}
    assert "is not one of" in _errs(d)


def test_hazard_bad_dice_is_error():
    d = _valid()
    d["scenes"]["a"]["hazards"] = {"darts": {"ability": "dex", "dc": 13, "damage": "2x6"}}
    assert "not valid dice notation" in _errs(d)


def test_hazard_bad_on_success_is_warning():
    d = _valid()
    d["scenes"]["a"]["hazards"] = {
        "gas": {"ability": "con", "dc": 12, "damage": "1d6", "on_success": "quarter"},
    }
    assert "on_success" in _warns(d)


# --- reinforcements -----------------------------------------------------------

def test_reinforcement_bad_template_is_error():
    d = _valid()
    d["scenes"]["a"]["reinforcements"] = {"wave": {"template": "dragonn", "name": "X"}}
    assert "not in MONSTERS" in _errs(d)


def test_reinforcement_unnormalized_requires_is_error():
    d = _valid()
    d["scenes"]["a"]["reinforcements"] = {
        "wave": {"template": "goblin", "name": "X", "requires": "Alarm Raised"},
    }
    assert "not in normalized form" in _errs(d)


# --- warnings (engine has a fallback) ----------------------------------------

def test_unreachable_scene_is_warning():
    d = _valid()
    d["scenes"]["island"] = {"location": "Island", "scene": "Cut off.", "exits": {}}
    assert "unreachable" in _warns(d)


def test_loot_near_miss_is_warning():
    d = _valid()
    d["scenes"]["a"]["loot"] = ["Healing Potion"]  # space + caps won't resolve
    out = _warns(d)
    assert "won't resolve at play" in out
    assert "healing_potion" in out


def test_narrative_loot_is_not_flagged():
    d = _valid()
    d["scenes"]["a"]["loot"] = ["bronze_key", "ancient_scroll", "sundering_crown"]
    rep = validate_scenario(d)
    assert rep.ok
    assert not any("loot" in w for w in rep.warnings)


def test_unknown_spell_is_warning():
    d = _valid()
    d["party"]["hero"]["spells"] = ["firebolt"]  # not the canonical 'fire_bolt'
    assert "not in rules.SPELLS" in _warns(d)


def test_spells_without_ability_is_warning():
    d = _valid()
    del d["party"]["hero"]["spellcasting_ability"]
    assert "no spellcasting_ability" in _warns(d)


def test_npc_nonweapon_inventory_is_warning():
    d = _valid()
    d["scenes"]["a"]["npcs"]["snik"]["inventory"] = ["rusty shiv"]
    assert "not in WEAPONS" in _warns(d)


def test_exit_requires_with_exits_is_warning():
    d = _valid()
    d["scenes"]["a"]["exit_requires"] = "some_flag"
    assert "only applies to exitless scenes" in _warns(d)


# --- dice helper --------------------------------------------------------------

def test_is_valid_dice():
    assert _is_valid_dice("2d6")
    assert _is_valid_dice("2d4+2")
    assert _is_valid_dice("d20")
    assert not _is_valid_dice("2x6")
    assert not _is_valid_dice("")
    assert not _is_valid_dice("200d6")   # count out of bounds
    assert not _is_valid_dice("1d1")     # sides out of bounds


# --- free-form scenarios ------------------------------------------------------

def test_freeform_scenario_without_scenes_is_ok():
    d = {"location": "A crossroads.", "party": {"hero": {"name": "Hero"}}}
    rep = validate_scenario(d)
    assert rep.ok


# --- CLI main -----------------------------------------------------------------

def test_main_returns_zero_on_valid(tmp_path, capsys):
    p = tmp_path / "good.json"
    p.write_text(json.dumps(_valid()))
    assert validate.main([str(p)]) == 0
    assert "✓" in capsys.readouterr().out


def test_main_returns_one_on_errors(tmp_path):
    d = _valid()
    d["scenes"]["a"]["exits"]["the north arch"] = "nowhere"
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(d))
    assert validate.main([str(p)]) == 1


def test_main_handles_missing_file():
    assert validate.main(["/no/such/scenario.json"]) == 1


def test_main_handles_bad_json(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not valid json")
    assert validate.main([str(p)]) == 1


# --- field-value type checks (load-clean, crash-at-play bugs) ------------------

def test_party_non_integer_hp_is_error():
    data = _valid()
    data["party"]["hero"]["hp"] = "twelve"
    assert "hp must be an integer" in _errs(data)


def test_party_non_integer_ac_is_error():
    data = _valid()
    data["party"]["hero"]["ac"] = 15.5  # floats are not ints
    assert "ac must be an integer" in _errs(data)


def test_party_list_field_as_string_is_error():
    data = _valid()
    data["party"]["hero"]["inventory"] = "mace"  # must be a list
    assert "inventory must be a list" in _errs(data)


def test_ability_modifiers_non_int_value_is_error():
    data = _valid()
    data["party"]["hero"]["ability_modifiers"] = {"str": "+3"}
    assert "ability_modifiers['str'] must be an integer" in _errs(data)


def test_ability_modifiers_unknown_ability_is_warning():
    data = _valid()
    data["party"]["hero"]["ability_modifiers"] = {"strength": 3}
    assert "not a known ability" in _warns(data)


def test_spell_slot_value_non_int_is_error():
    data = _valid()
    data["party"]["hero"]["spell_slots"] = {"1": "two"}
    assert "count must be an integer" in _errs(data)


def test_npc_non_integer_max_hp_is_error():
    data = _valid()
    data["scenes"]["a"]["npcs"]["snik"]["max_hp"] = "lots"
    assert "max_hp must be an integer" in _errs(data)


def test_npc_nullable_dc_allows_null_and_int():
    data = _valid()
    data["scenes"]["a"]["npcs"]["snik"]["disposition_dc"] = None
    assert validate_scenario(data).ok
    data["scenes"]["a"]["npcs"]["snik"]["disposition_dc"] = 12
    assert validate_scenario(data).ok


def test_npc_non_integer_dc_is_error():
    data = _valid()
    data["scenes"]["a"]["npcs"]["snik"]["alertness_dc"] = "high"
    assert "alertness_dc must be an integer or null" in _errs(data)


def test_unknown_top_level_key_is_warning():
    data = _valid()
    data["scens"] = {}  # typo for "scenes"
    assert "unknown top-level key 'scens'" in _warns(data)


# --- sanity checks: impossible HP/AC and ambiguous actor names -----------------

def test_hp_exceeds_max_hp_is_warning():
    data = _valid()
    data["party"]["hero"].update({"max_hp": 10, "hp": 20})
    assert "hp 20 exceeds max_hp 10" in _warns(data)
    assert validate_scenario(data).ok  # a warning, not an error


def test_nonpositive_max_hp_is_warning():
    data = _valid()
    data["party"]["hero"]["max_hp"] = 0
    assert "max_hp is 0" in _warns(data)


def test_low_ac_is_warning():
    data = _valid()
    data["party"]["hero"]["ac"] = 0
    assert "ac is 0" in _warns(data)


def test_npc_hp_exceeds_template_max_is_warning():
    # A goblin (template max_hp 12) given hp 100 with no max override → flagged.
    data = _valid()
    data["scenes"]["a"]["npcs"]["snik"]["hp"] = 100
    assert "exceeds max_hp 12" in _warns(data)


def test_duplicate_party_name_is_warning():
    data = _valid()
    data["party"]["twin"] = {"name": "Hero"}  # same name as party.hero
    assert "shared by" in _warns(data)


def test_npc_name_collides_with_party_is_warning():
    data = _valid()
    data["scenes"]["a"]["npcs"]["snik"]["name"] = "Hero"  # collides with the PC
    assert "collides with party.hero" in _warns(data)


def test_two_scene_npcs_same_name_is_warning():
    data = _valid()
    data["scenes"]["a"]["npcs"]["snik2"] = {"template": "goblin", "name": "Snik"}
    assert "collides with" in _warns(data)


def test_same_name_in_different_scenes_is_clean():
    # The same NPC name in two separate scenes is fine — they're never present together.
    data = _valid()
    data["scenes"]["b"]["npcs"]["snik_again"] = {"template": "goblin", "name": "Snik"}
    assert "collides" not in _warns(data)
