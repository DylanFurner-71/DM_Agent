"""Save/load round-trip tests — no API needed.

Exercises the savegame branch of from_dict (top-level "npcs" key present),
where live HP, spent spell slots, conditions, template-overridden stats,
and mid-combat state must all survive json.dump → json.load exactly.

This is the branch that SCENARIO loading never exercises: scenario files have
no top-level "npcs" key and re-expand NPCs from the scene definition, which
would reset HP. Savegames must use the already-live NPC objects instead.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.game_state import Character, NPC, GameState


def _make_midgame_state() -> GameState:
    """Build a state that hits every fragile serialization spot at once:

    - multi-scene scenario (current_scene + scenes map)
    - mid-combat (order, index, round > 0, action_used=True, initiatives)
    - damaged NPC at live HP (not template default)
    - template-overridden NPC (Grik max_hp=18, not goblin-template 12)
    - PC with reduced HP
    - spent spell slot (level-1 slot count = 0)
    - PC condition ("prone")
    """
    gs = GameState(
        current_scene="ember_chamber",
        location="The Ember Chamber",
        scene="Braziers burn with sourceless flame.",
        scenes={
            "barrow_entrance": {
                "location": "Barrow Entrance",
                "scene": "Dark.",
                "npcs": {
                    "snik": {"template": "goblin", "name": "Snik", "hostile": True},
                },
                "exits": {"descend": "ember_chamber"},
            },
            "ember_chamber": {
                "location": "The Ember Chamber",
                "scene": "Braziers burn with sourceless flame.",
                "npcs": {
                    "grik": {"template": "goblin", "name": "Grik", "max_hp": 18, "hostile": True},
                    "narl": {"template": "goblin", "name": "Narl", "hostile": True},
                },
                "exits": {},
            },
        },
    )

    # Aldric: reduced HP, spent level-1 slot, condition.
    gs.party["aldric"] = Character(
        name="Aldric",
        level=3,
        max_hp=24,
        hp=10,
        ac=16,
        attack_bonus=5,
        proficiency_bonus=2,
        spell_slots={1: 0, 2: 1},  # level-1 slot spent
        ability_modifiers={"str": 2, "dex": 0, "con": 2, "int": 1, "wis": 3, "cha": 1},
        inventory=["mace", "holy symbol"],
        conditions=["prone"],
        spellcasting_ability="wis",
        spells=["guiding_bolt"],
        save_proficiencies=["wis", "cha"],
    )
    # Wisp: full HP, slots intact, no conditions.
    gs.party["wisp"] = Character(
        name="Wisp",
        level=3,
        max_hp=16,
        hp=16,
        ac=12,
        attack_bonus=4,
        proficiency_bonus=2,
        spell_slots={1: 3, 2: 2},
        ability_modifiers={"str": -1, "dex": 2, "con": 0, "int": 4, "wis": 1, "cha": 2},
        inventory=["dagger"],
        conditions=[],
        spellcasting_ability="int",
        spells=["magic_missile", "chromatic_orb"],
    )

    # Grik: template override (max_hp=18 not goblin-default 12), damaged.
    gs.npcs["grik"] = NPC(
        name="Grik",
        max_hp=18,
        hp=7,
        ac=13,
        attack_bonus=4,
        hostile=True,
        ability_modifiers={"str": -1, "dex": 2, "con": 0, "int": 0, "wis": -1, "cha": -1},
        inventory=["shortsword", "shortbow"],
    )
    # Narl: standard goblin stat block, undamaged.
    gs.npcs["narl"] = NPC(
        name="Narl",
        max_hp=12,
        hp=12,
        ac=13,
        attack_bonus=4,
        hostile=True,
        ability_modifiers={"str": -1, "dex": 2, "con": 0, "int": 0, "wis": -1, "cha": -1},
        inventory=["shortsword", "shortbow"],
    )

    # Mid-combat: pointer on Wisp (index 2), round 3, Aldric's action already used.
    gs.combat_order = ["aldric", "grik", "wisp", "narl"]
    gs.combat_index = 2
    gs.combat_round = 3
    gs.action_used = True
    gs.combat_initiatives = {"aldric": 15, "grik": 12, "wisp": 10, "narl": 8}
    gs.turn = 7
    gs.quest_flags = {"door_opened": True}

    return gs


def _save_load(gs: GameState, tmp_path) -> GameState:
    path = tmp_path / "mid_save.json"
    gs.save(str(path))
    return GameState.load(str(path))


# --- headline test -----------------------------------------------------------

def test_roundtrip_is_lossless(tmp_path):
    """to_dict() before save == to_dict() after load — catches any field that
    survives the Python layer but loses precision through JSON serialization."""
    gs = _make_midgame_state()
    loaded = _save_load(gs, tmp_path)
    assert gs.to_dict() == loaded.to_dict()


# --- targeted assertions (readable failures when the headline breaks) ---------

def test_spell_slots_keys_stay_ints(tmp_path):
    """JSON stringifies int dict keys; from_dict must restore them as ints.
    Both the zero-value (spent) and positive-value slots must survive."""
    gs = _make_midgame_state()
    loaded = _save_load(gs, tmp_path)
    slots = loaded.party["aldric"].spell_slots
    assert all(isinstance(k, int) for k in slots), (
        f"spell_slots keys must be int after load, got: {list(slots)}"
    )
    assert slots[1] == 0, "spent level-1 slot must remain 0"
    assert slots[2] == 1, "unspent level-2 slot must remain 1"


def test_npc_live_hp_preserved(tmp_path):
    """The savegame branch must load from the top-level 'npcs' key (live state),
    not re-expand from the scene definition which would reset HP to template max."""
    gs = _make_midgame_state()
    loaded = _save_load(gs, tmp_path)
    assert loaded.npcs["grik"].hp == 7, (
        "Grik's damaged HP must survive; re-expansion from scene would reset to max"
    )
    assert loaded.npcs["narl"].hp == 12


def test_template_override_persists(tmp_path):
    """Grik was spawned with max_hp=18 (overriding the goblin template's 12).
    Both the override and the damaged current HP must survive the round-trip."""
    gs = _make_midgame_state()
    loaded = _save_load(gs, tmp_path)
    grik = loaded.npcs["grik"]
    assert grik.max_hp == 18, (
        "max_hp override must survive; re-expansion from goblin template would give 12"
    )
    assert grik.hp == 7, "damaged HP must not be reset to max on load"


def test_combat_state_preserved(tmp_path):
    """All five combat fields must round-trip exactly, and the active pointer
    must still resolve to a live actor in the merged party+npc map."""
    gs = _make_midgame_state()
    loaded = _save_load(gs, tmp_path)

    assert loaded.combat_order == ["aldric", "grik", "wisp", "narl"]
    assert loaded.combat_index == 2
    assert loaded.combat_round == 3
    assert loaded.action_used is True
    assert loaded.combat_initiatives == {"aldric": 15, "grik": 12, "wisp": 10, "narl": 8}

    active_key = loaded.combat_order[loaded.combat_index]
    all_actors = {**loaded.party, **loaded.npcs}
    assert active_key in all_actors, (
        f"active combatant key {active_key!r} must resolve after load"
    )


def test_conditions_preserved(tmp_path):
    """Character conditions list must survive the round-trip — both a non-empty
    list ("prone") and an empty list."""
    gs = _make_midgame_state()
    loaded = _save_load(gs, tmp_path)
    assert loaded.party["aldric"].conditions == ["prone"]
    assert loaded.party["wisp"].conditions == []


def test_save_proficiencies_preserved(tmp_path):
    """save_proficiencies must survive the round-trip — a populated list and the
    default empty list (absent-key default applies for older saves)."""
    gs = _make_midgame_state()
    loaded = _save_load(gs, tmp_path)
    assert loaded.party["aldric"].save_proficiencies == ["wis", "cha"]
    assert loaded.party["wisp"].save_proficiencies == []


# --- savegame branch guard ---------------------------------------------------

def test_savegame_branch_used_not_scene_expansion(tmp_path):
    """The saved JSON must have a top-level 'npcs' key so from_dict takes the
    savegame branch rather than re-expanding NPCs from the scene definition."""
    gs = _make_midgame_state()
    path = tmp_path / "mid_save.json"
    gs.save(str(path))
    raw = json.loads(path.read_text())
    assert "npcs" in raw, (
        "to_dict must write top-level 'npcs'; its absence would cause from_dict "
        "to re-expand from the scene definition and reset live HP"
    )
    # Also confirm the scenes definition for ember_chamber still has template
    # entries (so the branch distinction is meaningful, not moot).
    scene_npc = raw["scenes"]["ember_chamber"]["npcs"]["grik"]
    assert "template" in scene_npc, (
        "scene definition must retain template entries; the live npcs key is what "
        "provides the actual loaded state"
    )
