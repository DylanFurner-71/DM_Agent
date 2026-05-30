"""Tool definitions and dispatch.

``TOOLS`` is the schema list passed to the Anthropic API. ``dispatch`` executes a
tool call against the live ``GameState`` and returns a JSON-serializable result
that gets fed back to the model. The model never mutates state directly — it can
only *request* an action, which the deterministic code below grants or denies.
"""

from __future__ import annotations

from . import rules

TOOLS = [
    {
        "name": "roll_dice",
        "description": "Roll dice in standard notation (e.g. '2d6+3', '1d20'). Use this for ALL randomness; never invent a result.",
        "input_schema": {
            "type": "object",
            "properties": {"notation": {"type": "string", "description": "Dice notation, e.g. '1d20+5'"}},
            "required": ["notation"],
        },
    },
    {
        "name": "attack",
        "description": "Resolve an attack of one actor against another: rolls to hit vs AC and applies damage on a hit.",
        "input_schema": {
            "type": "object",
            "properties": {
                "attacker": {"type": "string", "description": "Name of the attacking actor"},
                "defender": {"type": "string", "description": "Name of the defending actor"},
                "damage_dice": {"type": "string", "description": "Damage dice notation, e.g. '1d8+2'", "default": "1d6"},
            },
            "required": ["attacker", "defender"],
        },
    },
    {
        "name": "cast_spell",
        "description": "Attempt to cast a leveled spell. Returns ok=false if the caster has no slot of that level — you MUST respect that and narrate the failure.",
        "input_schema": {
            "type": "object",
            "properties": {
                "caster": {"type": "string", "description": "Name of the casting character"},
                "spell_level": {"type": "integer", "description": "Spell level (0 for a cantrip)"},
            },
            "required": ["caster", "spell_level"],
        },
    },
    {
        "name": "modify_hp",
        "description": "Apply damage (negative) or healing (positive) to an actor outside of a normal attack.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "amount": {"type": "integer", "description": "Negative to damage, positive to heal"},
            },
            "required": ["target", "amount"],
        },
    },
    {
        "name": "set_quest_flag",
        "description": "Record a story milestone as a boolean flag (e.g. 'met_the_oracle').",
        "input_schema": {
            "type": "object",
            "properties": {
                "flag": {"type": "string"},
                "value": {"type": "boolean", "default": True},
            },
            "required": ["flag"],
        },
    },
    {
        "name": "move_scene",
        "description": "Update the party's location and scene description when they travel or the setting changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string"},
                "scene": {"type": "string"},
            },
            "required": ["location"],
        },
    },
    {
        "name": "get_state",
        "description": "Read the current game state (party HP and slots, NPCs, location, flags). Call this whenever you need exact numbers.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "start_combat",
        "description": "Roll initiative for the listed combatants, set turn order in state, and begin round 1. Pass the state dict-keys of every participant (party members and NPCs). Returns the turn order so you know who acts first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "combatants": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "State dict-keys of every combatant, e.g. ['aldric', 'wisp', 'snik']. Call get_state to see valid keys.",
                },
            },
            "required": ["combatants"],
        },
    },
    {
        "name": "next_turn",
        "description": "Advance to the next combatant in initiative order. Automatically increments the round counter when the order wraps. The engine calls this between turns — do NOT call it yourself during narration.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "end_combat",
        "description": "Clear all combat state (turn order, pointer, round counter). Call when the fight is over — enemies defeated, fled, or parley reached.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "skill_check",
        "description": "Roll d20 + a character's ability modifier against a Difficulty Class (DC). Use for any non-attack check — perception, persuasion, athletics, stealth, arcana, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "character": {"type": "string", "description": "Name of the character making the check"},
                "ability": {"type": "string", "description": "Ability name: str, dex, con, int, wis, or cha"},
                "dc": {"type": "integer", "description": "Difficulty Class the total must meet or exceed"},
            },
            "required": ["character", "ability", "dc"],
        },
    },
    {
        "name": "lookup_rule",
        "description": "Look up a rules topic (e.g. 'advantage', 'death_saves', 'spell_slots') in the SRD reference.",
        "input_schema": {
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
        },
    },
]


def dispatch(name: str, args: dict, state) -> dict:
    """Execute one tool call against the live state. Returns a JSON-able dict."""
    if name == "roll_dice":
        try:
            r = rules.roll(args["notation"])
            return {"ok": True, "result": r.total, "detail": r.describe()}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    if name == "attack":
        attacker = state.find_actor(args["attacker"])
        defender = state.find_actor(args["defender"])
        if not attacker or not defender:
            return {"ok": False, "error": "Unknown attacker or defender; call get_state to see valid names."}
        res = rules.attack(attacker, defender, args.get("damage_dice", "1d6"))
        state.record(f"{attacker.name} attacks {defender.name}: {'hit' if res['hit'] else 'miss'}")
        return res

    if name == "cast_spell":
        caster = state.find_actor(args["caster"])
        if not caster:
            return {"ok": False, "error": "Unknown caster; call get_state."}
        res = rules.cast_spell(caster, int(args["spell_level"]))
        state.record(f"{caster.name} casts level-{args['spell_level']} spell: {res['reason']}")
        return res

    if name == "modify_hp":
        target = state.find_actor(args["target"])
        if not target:
            return {"ok": False, "error": "Unknown target; call get_state."}
        amount = int(args["amount"])
        res = rules.heal(target, amount) if amount >= 0 else rules.apply_damage(target, -amount)
        return res

    if name == "set_quest_flag":
        state.quest_flags[args["flag"]] = bool(args.get("value", True))
        state.record(f"flag {args['flag']} = {state.quest_flags[args['flag']]}")
        return {"ok": True, "flag": args["flag"], "value": state.quest_flags[args["flag"]]}

    if name == "move_scene":
        state.location = args["location"]
        if "scene" in args:
            state.scene = args["scene"]
        state.record(f"scene -> {state.location}")
        return {"ok": True, "location": state.location}

    if name == "get_state":
        return {"ok": True, "state": state.to_dict()}

    if name == "start_combat":
        all_actors = {**state.party, **state.npcs}
        combatant_keys = args.get("combatants", [])
        if not combatant_keys:
            return {"ok": False, "error": "combatants list is empty."}
        unknown = [k for k in combatant_keys if k not in all_actors]
        if unknown:
            return {"ok": False, "error": f"Unknown combatant key(s) {unknown}; call get_state to see valid keys."}
        ordered = rules.roll_initiative({k: all_actors[k] for k in combatant_keys})
        state.combat_order = ordered
        state.combat_index = 0
        state.combat_round = 1
        active_key = ordered[0]
        state.record(f"combat started round 1, order: {ordered}, first: {active_key}")
        return {"ok": True, "combat_order": ordered, "active": active_key, "active_name": all_actors[active_key].name, "round": 1}

    if name == "next_turn":
        if not state.combat_order:
            return {"ok": False, "error": "No combat in progress; call start_combat first."}
        state.combat_index += 1
        if state.combat_index >= len(state.combat_order):
            state.combat_index = 0
            state.combat_round += 1
        active_key = state.combat_order[state.combat_index]
        all_actors = {**state.party, **state.npcs}
        active_name = all_actors[active_key].name if active_key in all_actors else active_key
        state.record(f"round {state.combat_round}, turn -> {active_key} ({active_name})")
        return {"ok": True, "active": active_key, "active_name": active_name, "round": state.combat_round, "combat_index": state.combat_index}

    if name == "end_combat":
        state.combat_order = []
        state.combat_index = 0
        state.combat_round = 0
        state.record("combat ended")
        return {"ok": True}

    if name == "skill_check":
        character = state.find_actor(args["character"])
        if not character:
            return {"ok": False, "error": "Unknown character; call get_state to see valid names."}
        res = rules.skill_check(character, args["ability"], int(args["dc"]))
        state.record(f"{character.name} {args['ability']} check DC {args['dc']}: {'success' if res['success'] else 'failure'}")
        return res

    if name == "lookup_rule":
        return rules.lookup_rule(args["topic"])

    return {"ok": False, "error": f"Unknown tool {name!r}"}
