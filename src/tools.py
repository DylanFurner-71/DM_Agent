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
        "description": (
            "Resolve a weapon attack. Specify the attacker, defender, and the weapon "
            "the attacker is using (must be in their inventory). The engine validates "
            "the inventory, looks up the damage die from the WEAPONS table, and derives "
            "the to-hit bonus (ability_mod + proficiency) and damage modifier (ability_mod) "
            "automatically — never supply dice or modifiers yourself. "
            "Omit weapon only for NPC unarmed strikes; the engine uses attack_bonus + 1d6."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attacker": {"type": "string", "description": "Name of the attacking actor"},
                "defender": {"type": "string", "description": "Name of the defending actor"},
                "weapon":   {"type": "string", "description": "Weapon name, e.g. 'mace', 'dagger', 'longsword'. Must be in the attacker's inventory."},
            },
            "required": ["attacker", "defender"],
        },
    },
    {
        "name": "cast_spell",
        "description": (
            "Cast a spell. For damaging spells (e.g. 'magic_missile'), supply spell_name "
            "and target — the engine rolls the full damage expression and applies it "
            "atomically; do NOT call roll_dice or modify_hp for the damage afterward. "
            "For utility or buff spells, omit spell_name/target; only the slot is consumed. "
            "Returns ok=false if no slot of that level remains."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "caster": {"type": "string", "description": "Name of the casting character"},
                "spell_level": {"type": "integer", "description": "Slot level (0 for a cantrip)"},
                "spell_name": {"type": "string", "description": "Spell name, e.g. 'magic_missile'. Required for damaging spells."},
                "target": {"type": "string", "description": "Target name. Required for damaging spells."},
            },
            "required": ["caster", "spell_level"],
        },
    },
    {
        "name": "modify_hp",
        "description": (
            "Apply damage (negative) or healing (positive) for non-spell effects: "
            "traps, potions, environmental hazards. "
            "Do NOT use this for spell damage — use cast_spell with spell_name and target instead."
        ),
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


def _active_actor_name(state) -> str | None:
    """Return the name of the current active combatant, or None when not in combat."""
    if not state.combat_order or state.combat_round == 0:
        return None
    active_key = state.combat_order[state.combat_index]
    all_actors = {**state.party, **state.npcs}
    actor = all_actors.get(active_key)
    return actor.name if actor else None


def _action_guard(state) -> dict | None:
    """Return ok=False if the active combatant has already used their action this turn.

    When the action is available, sets state.action_used = True and returns None so
    the caller can proceed. No-ops outside of combat.
    """
    if not state.combat_order or state.combat_round == 0:
        return None  # not in combat — no restriction
    if state.action_used:
        active_key = state.combat_order[state.combat_index]
        all_actors = {**state.party, **state.npcs}
        actor = all_actors.get(active_key)
        name = actor.name if actor else active_key
        return {"ok": False, "error": f"{name} has already acted this turn."}
    state.action_used = True
    return None


def _turn_guard(actor_name: str, state) -> dict | None:
    """Return an ok=False error if actor_name is not the active combatant, else None."""
    active = _active_actor_name(state)
    if active is None:
        return None  # not in combat — no restriction
    if actor_name.strip().lower() != active.lower():
        return {
            "ok": False,
            "error": (
                f"It is not {actor_name}'s turn. "
                f"The active combatant is {active}. "
                "Wait for their turn before acting."
            ),
        }
    return None


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
        err = _turn_guard(attacker.name, state) or _action_guard(state)
        if err:
            return err
        res = rules.attack(attacker, defender, args.get("weapon"))
        state.record(f"{attacker.name} attacks {defender.name}: {'hit' if res['hit'] else 'miss'}")
        return res

    if name == "cast_spell":
        caster = state.find_actor(args["caster"])
        if not caster:
            return {"ok": False, "error": "Unknown caster; call get_state."}
        err = _turn_guard(caster.name, state) or _action_guard(state)
        if err:
            return err
        spell_name = args.get("spell_name", "").strip()
        target_name = args.get("target", "").strip()
        if spell_name and target_name:
            target = state.find_actor(target_name)
            if not target:
                return {"ok": False, "error": f"Unknown target {target_name!r}; call get_state."}
            res = rules.cast_damaging_spell(caster, target, spell_name, int(args["spell_level"]))
            if res["ok"]:
                state.record(f"{caster.name} casts {spell_name} at {target.name}: {res.get('damage_detail', 'no damage table')}")
            else:
                state.record(f"{caster.name} tried to cast {spell_name}: {res.get('reason', res.get('error'))}")
        else:
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
        state.action_used = False
        active_key = ordered[0]
        state.record(f"combat started round 1, order: {ordered}, first: {active_key}")
        return {"ok": True, "combat_order": ordered, "active": active_key, "active_name": all_actors[active_key].name, "round": 1}

    if name == "next_turn":
        if not state.combat_order:
            return {"ok": False, "error": "No combat in progress; call start_combat first."}
        all_actors = {**state.party, **state.npcs}
        skipped: list[str] = []
        for _ in range(len(state.combat_order)):
            state.combat_index += 1
            if state.combat_index >= len(state.combat_order):
                state.combat_index = 0
                state.combat_round += 1
            active_key = state.combat_order[state.combat_index]
            actor = all_actors.get(active_key)
            if actor is not None and actor.is_down:
                skipped.append(active_key)
                state.record(f"skipping downed {active_key} ({actor.name})")
                continue
            break
        else:
            return {"ok": False, "error": "All combatants are at 0 HP; call end_combat."}
        active_name = actor.name if actor else active_key
        state.action_used = False
        state.record(f"round {state.combat_round}, turn -> {active_key} ({active_name})")
        result = {"ok": True, "active": active_key, "active_name": active_name, "round": state.combat_round, "combat_index": state.combat_index}
        if skipped:
            result["skipped_downed"] = skipped
        return result

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
        err = _turn_guard(character.name, state)
        if err:
            return err
        res = rules.skill_check(character, args["ability"], int(args["dc"]))
        state.record(f"{character.name} {args['ability']} check DC {args['dc']}: {'success' if res['success'] else 'failure'}")
        return res

    if name == "lookup_rule":
        return rules.lookup_rule(args["topic"])

    return {"ok": False, "error": f"Unknown tool {name!r}"}
