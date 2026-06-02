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

from src import rules, tools
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


def test_inspiration_round_trips(tmp_path):
    """A held inspiration point and the lifetime-used lock must survive save/load."""
    gs = GameState(party={
        "holder": Character(name="Holder", inspiration=1),
        "spent": Character(name="Spent", inspiration=0, inspiration_used=True),
    })
    loaded = _save_load(gs, tmp_path)
    assert loaded.party["holder"].inspiration == 1
    assert loaded.party["holder"].inspiration_used is False
    assert loaded.party["spent"].inspiration == 0
    assert loaded.party["spent"].inspiration_used is True


def test_inspiration_defaults_on_older_save(tmp_path):
    """A save predating inspiration (no field) loads with the safe defaults."""
    path = tmp_path / "old.json"
    gs = GameState(party={"hero": Character(name="Hero")})
    d = gs.to_dict()
    for pc in d["party"].values():
        pc.pop("inspiration", None)
        pc.pop("inspiration_used", None)
    import json as _json
    path.write_text(_json.dumps(d))
    loaded = GameState.load(str(path))
    assert loaded.party["hero"].inspiration == 0
    assert loaded.party["hero"].inspiration_used is False


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


def test_npc_shop_round_trips(tmp_path):
    """A merchant NPC's shop catalogue survives save+load."""
    gs = GameState(location="Market", current_scene="", scenes={})
    gs.npcs["garric"] = NPC(name="Garric", hostile=False, shop={"longsword": 15, "dagger": 2})
    loaded = _save_load(gs, tmp_path)
    assert loaded.npcs["garric"].shop == {"longsword": 15, "dagger": 2}


def test_gold_round_trips_and_defaults(tmp_path):
    """Character.gold survives save+load; an older save with no gold key defaults to 0."""
    gs = GameState(party={
        "rich": Character(name="Rich", gold=42),
        "broke": Character(name="Broke"),  # no gold → default 0
    })
    loaded = _save_load(gs, tmp_path)
    assert loaded.party["rich"].gold == 42
    assert loaded.party["broke"].gold == 0


def test_skill_proficiencies_and_expertise_round_trip(tmp_path):
    """skill_proficiencies / expertise survive save+load; older saves default to []."""
    gs = GameState(party={
        "rogue": Character(name="Rogue", skill_proficiencies=["stealth", "perception"],
                           expertise=["stealth"]),
        "plain": Character(name="Plain"),  # no skill data → defaults
    })
    loaded = _save_load(gs, tmp_path)
    assert loaded.party["rogue"].skill_proficiencies == ["stealth", "perception"]
    assert loaded.party["rogue"].expertise == ["stealth"]
    assert loaded.party["plain"].skill_proficiencies == []
    assert loaded.party["plain"].expertise == []


def test_sprung_hazards_preserved(tmp_path):
    """sprung one-shot hazards must survive the round-trip so a reloaded session
    doesn't re-arm a trap the party already triggered."""
    gs = _make_midgame_state()
    gs.sprung_hazards = ["ember_chamber:dart_trap"]
    loaded = _save_load(gs, tmp_path)
    assert loaded.sprung_hazards == ["ember_chamber:dart_trap"]


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


# --- moved from test_rules.py ------------------------------------------------
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




def test_multi_scene_savegame_round_trip():
    """Saving and reloading preserves scenes dict and live NPC state (not re-expanded)."""
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "data", "two_scene_loot_quest_item.json")
    gs = GameState.load(path)
    gs.npcs["snik"].hp = 3   # simulate combat damage

    restored = GameState.from_dict(gs.to_dict())

    assert restored.current_scene == "barrow_entrance"
    assert "ember_chamber" in restored.scenes
    assert restored.npcs["snik"].hp == 3   # live state, not re-expanded from template




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




def test_companion_flag_round_trips():
    """The companion flag survives save/load."""
    gs = GameState(location="Camp")
    gs.npcs["brak"] = NPC(name="Brak", max_hp=12, hp=12, hostile=False, companion=True)
    restored = GameState.from_dict(gs.to_dict())
    assert restored.npcs["brak"].companion is True
