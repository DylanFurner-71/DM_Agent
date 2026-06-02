"""Tests for add_npc author-declared reinforcements and available_reinforcements. Extracted from test_rules.py; the enforcement core
stays there. Run:  python -m pytest -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import rules, tools
from src.game_state import Character, NPC, GameState
from tests._helpers import _make_combat_state


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
