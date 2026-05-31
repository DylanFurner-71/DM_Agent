"""Tool definitions and dispatch.

``TOOLS`` is the schema list passed to the Anthropic API. ``dispatch`` executes a
tool call against the live ``GameState`` and returns a JSON-serializable result
that gets fed back to the model. The model never mutates state directly — it can
only *request* an action, which the deterministic code below grants or denies.
"""

from __future__ import annotations

import re

from . import rules
from .game_state import NPC, expand_npc_entry

# Keys the model must not use as quest flags — these name engine-owned fields.
_RESERVED_FLAG_KEYS = frozenset({
    "hp", "max_hp", "ac", "spell_slots", "damage", "initiative",
})


def _normalize_flag_key(raw: str) -> str | None:
    """Normalize a quest flag key; return None if the result is empty.

    Pipeline: strip → lowercase → spaces/hyphens → underscores →
    remove chars outside [a-z0-9_].
    """
    key = raw.strip().lower().replace(" ", "_").replace("-", "_")
    key = re.sub(r"[^a-z0-9_]", "", key)
    return key or None

TOOLS = [
    {
        "name": "roll_dice",
        "description": (
            "Roll dice in standard notation — for fiction or flavor ONLY "
            "(e.g. random encounter table, which way an NPC flees, coins in a chest). "
            "NEVER use this for anything that changes HP, slots, or conditions — those have "
            "dedicated atomic tools: attack (weapon damage), cast_spell (spell damage), "
            "apply_dice (trap/hazard/potion dice)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"notation": {"type": "string", "description": "Dice notation, e.g. '1d20+5'"}},
            "required": ["notation"],
        },
    },
    {
        "name": "attack",
        "description": (
            "Resolve a weapon attack. defender is optional: omit it and the engine "
            "auto-selects the sole living hostile (ok=false with reason 'ambiguous_target' "
            "if multiple are present, 'no_target' if none). Explicit naming always wins. "
            "For PC attackers, always supply weapon (validated against inventory). "
            "For NPC attackers, weapon is optional — engine auto-equips the first inventory "
            "weapon from the WEAPONS table. "
            "The engine derives to-hit bonus and damage modifier automatically — never "
            "supply dice or modifiers yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attacker": {"type": "string", "description": "Name of the attacking actor"},
                "defender": {"type": "string", "description": "Name of the defending actor. Omit to auto-select the sole living hostile."},
                "weapon":   {"type": "string", "description": "Weapon name, e.g. 'mace', 'dagger', 'longsword'. Must be in the attacker's inventory."},
            },
            "required": ["attacker"],
        },
    },
    {
        "name": "cast_spell",
        "description": (
            "Cast a spell. For damaging spells (e.g. 'magic_missile'), supply spell_name; "
            "target is optional — omit it and the engine auto-selects the sole living hostile "
            "(ok=false with reason 'ambiguous_target' listing candidates if multiple are "
            "present; 'no_target' if none). Explicit naming always wins and may be any actor. "
            "The engine rolls and applies damage atomically; do NOT call roll_dice or "
            "modify_hp for the damage afterward. "
            "For utility or buff spells, omit spell_name/target; only the slot is consumed. "
            "Returns ok=false if no slot of that level remains."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "caster": {"type": "string", "description": "Name of the casting character"},
                "spell_level": {"type": "integer", "description": "Slot level (0 for a cantrip)"},
                "spell_name": {"type": "string", "description": "Spell name, e.g. 'magic_missile'. Required for damaging spells."},
                "target": {"type": "string", "description": "Target name. Omit to auto-select the sole living hostile."},
            },
            "required": ["caster", "spell_level"],
        },
    },
    {
        "name": "modify_hp",
        "description": (
            "Apply a flat, known amount of damage (negative) or healing (positive) — "
            "for exact non-dice values ONLY, e.g. 'the lava deals exactly 20' or "
            "'the potion restores exactly 10 HP'. "
            "For any dice-rolled amount use apply_dice. "
            "Do NOT use this for spell damage — use cast_spell instead."
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
        "name": "apply_dice",
        "description": (
            "Roll a dice expression AND apply the result to an actor's HP atomically — "
            "rolled == applied is guaranteed, the same way attack and cast_spell work. "
            "Use for ALL dice-based damage or healing from traps, hazards, and potions. "
            "Never resolve these with roll_dice + modify_hp. "
            "kind: 'damage' (default) or 'healing'. "
            "source: optional label for the log (e.g. 'spike trap', 'healing potion')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target":   {"type": "string", "description": "Name of the target actor"},
                "notation": {"type": "string", "description": "Dice expression, e.g. '2d6', '2d4+2'"},
                "kind":     {"type": "string", "enum": ["damage", "healing"], "description": "damage (default) or healing"},
                "source":   {"type": "string", "description": "Optional label for the log, e.g. 'spike trap'"},
            },
            "required": ["target", "notation"],
        },
    },
    {
        "name": "set_quest_flag",
        "description": (
            "Record a named story flag with a value. "
            "flag is normalized: stripped, lowercased, spaces/hyphens → underscores, "
            "restricted to [a-z0-9_]; empty-after-normalization is rejected. "
            "value must be a JSON primitive (bool, string, int, float, or null); "
            "omit to default to true. "
            "Reserved engine keys (hp, max_hp, ac, spell_slots, damage, initiative) "
            "are rejected — flags are not a backdoor to mechanical state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "flag": {"type": "string", "description": "Flag name; normalized to snake_case."},
                "value": {"description": "A JSON primitive — bool, string, int, float, or null. Defaults to true."},
            },
            "required": ["flag"],
        },
    },
    {
        "name": "clear_quest_flag",
        "description": (
            "Remove a quest flag. flag is normalized the same way as set_quest_flag "
            "(stripped, lowercased, spaces/hyphens → underscores, [a-z0-9_] only). "
            "If the key is absent the call is a no-op (ok=True, removed=False, no crash)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "flag": {"type": "string", "description": "Flag name to remove; normalized to snake_case."},
            },
            "required": ["flag"],
        },
    },
    {
        "name": "move_scene",
        "description": (
            "Move the party to a new location. Two modes:\n"
            "NAMED SCENE (use when exits is shown in the state snapshot): pass scene_key "
            "matching one of the VALUES in the current scene's exits map — the engine "
            "validates the move is along a declared exit, then sets location, scene text, "
            "and replaces the NPC roster. Party is untouched. "
            "NEVER supply a scene_key that is not listed in the current scene's exits — "
            "moves to non-adjacent scenes are rejected even if the scene is defined. "
            "A scene whose exits map is empty is a dead end; no further move is possible.\n"
            "FREE-FORM (no named scenes defined): pass location string and optional scene "
            "description to update them directly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scene_key": {"type": "string", "description": "A scene_key that appears as a VALUE in the current scene's exits map, e.g. 'ember_chamber'."},
                "location":  {"type": "string", "description": "Free-form location name. Use when no named scenes are defined."},
                "scene":     {"type": "string", "description": "Free-form scene description. Optional companion to location."},
            },
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
    {
        "name": "add_npc",
        "description": (
            "Instantiate a new NPC from a canonical monster template and add it to the roster. "
            "Available templates: goblin, orc, skeleton. "
            "instance_id is the unique state key (e.g. 'goblin_two'); it must not already exist. "
            "name overrides the display name (optional). "
            "Outside combat: NPC is added to the roster and can be named in start_combat. "
            "During combat (reinforcements): the engine rolls the NPC's initiative and inserts "
            "it into the turn order at the correct sorted slot; the active combatant is unchanged. "
            "Returns ok=false for an unknown template or a duplicate instance_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "template":    {"type": "string", "description": "Monster template id: 'goblin', 'orc', or 'skeleton'"},
                "instance_id": {"type": "string", "description": "Unique state key for this NPC, e.g. 'goblin_two'"},
                "name":        {"type": "string", "description": "Optional display name override"},
            },
            "required": ["template", "instance_id"],
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


def _resolve_actor_key(identifier: str, state) -> str | None:
    """Resolve a combatant identifier to its canonical dict key.

    Tries in order: case-insensitive key match, then case-insensitive display-name
    match. Returns None when nothing matches.
    """
    ident_lower = identifier.strip().lower()
    all_actors = {**state.party, **state.npcs}
    for key in all_actors:
        if key.lower() == ident_lower:
            return key
    for key, actor in all_actors.items():
        if actor.name.lower() == ident_lower:
            return key
    return None


def _pc_turn_decision(pc) -> str:
    """Return how next_turn and the combat loop should handle a PC slot.

    "roll"  — dying (hp<=0, not dead, not stable): engine rolls a death save.
    "break" — conscious: stop and prompt.
    "skip"  — dead or stable: skip this slot.
    """
    if pc.is_dying:
        return "roll"
    if not pc.is_down:
        return "break"
    return "skip"


def _resolve_offensive_target(target_arg: str, state, exclude_name: str = "") -> tuple:
    """Resolve a target for an offensive action (attack or damaging spell).

    Returns (target, None, auto_selected) on success, or (None, error_dict, False) on failure.
    auto_selected is True when no name was given and the engine picked the sole living hostile.

    Named target:  resolve via find_actor; error if unknown. No restriction to hostiles —
                   targeting an ally is legal.
    No target:     candidates = hostile NPCs that are alive; during combat, restricted to
                   those in combat_order. The attacker (exclude_name) is always excluded so
                   an NPC never appears in its own candidate list.
                     0  → ok=false, reason "no_target"
                     1  → auto-resolve; caller should surface auto_target in the result
                    >1  → ok=false, reason "ambiguous_target", candidates list
    """
    target_arg = (target_arg or "").strip()

    if target_arg:
        target = state.find_actor(target_arg)
        if not target:
            return None, {"ok": False, "error": f"Unknown target {target_arg!r}; call get_state."}, False
        return target, None, False

    excl = exclude_name.strip().lower()
    candidates = [
        npc for key, npc in state.npcs.items()
        if npc.hostile and not npc.is_down
        and (state.combat_round == 0 or key in state.combat_order)
        and (not excl or npc.name.lower() != excl)
    ]

    if not candidates:
        return None, {"ok": False, "reason": "no_target", "error": "No living hostile targets present."}, False

    if len(candidates) == 1:
        return candidates[0], None, True

    return None, {
        "ok": False,
        "reason": "ambiguous_target",
        "error": "Multiple living targets — name one explicitly.",
        "candidates": [npc.name for npc in candidates],
    }, False


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
        if not attacker:
            return {"ok": False, "error": "Unknown attacker; call get_state to see valid names."}
        if state.combat_starting:
            return {"ok": False, "reason": "combat_starting",
                    "error": "Combat is starting this turn — wait for the initiative order before acting."}
        err = _turn_guard(attacker.name, state) or _action_guard(state)
        if err:
            return err
        target, err, auto_selected = _resolve_offensive_target(args.get("defender", ""), state, exclude_name=attacker.name)
        if err:
            state.action_used = False
            return err
        res = rules.attack(attacker, target, args.get("weapon"))
        if res["ok"]:
            state.record(f"{attacker.name} attacks {target.name}: {'hit' if res['hit'] else 'miss'}")
            if auto_selected:
                res["auto_target"] = target.name
        else:
            state.action_used = False  # invalid action — undo guard; character stays active
        return res

    if name == "cast_spell":
        caster = state.find_actor(args["caster"])
        if not caster:
            return {"ok": False, "error": "Unknown caster; call get_state."}
        if state.combat_starting:
            return {"ok": False, "reason": "combat_starting",
                    "error": "Combat is starting this turn — wait for the initiative order before acting."}
        err = _turn_guard(caster.name, state) or _action_guard(state)
        if err:
            return err
        spell_name = args.get("spell_name", "").strip()
        if spell_name:
            target, err, auto_selected = _resolve_offensive_target(args.get("target", ""), state, exclude_name=caster.name)
            if err:
                state.action_used = False
                return err
            res = rules.cast_damaging_spell(caster, target, spell_name, int(args["spell_level"]))
            if res["ok"]:
                state.record(f"{caster.name} casts {spell_name} at {target.name}: {res.get('damage_detail', 'no damage table')}")
                if auto_selected:
                    res["auto_target"] = target.name
            else:
                state.action_used = False  # invalid action — undo guard; character stays active
                state.record(f"{caster.name} tried to cast {spell_name}: {res.get('reason', res.get('error'))}")
        else:
            res = rules.cast_spell(caster, int(args["spell_level"]))
            if res["ok"]:
                state.record(f"{caster.name} casts level-{args['spell_level']} spell: {res['reason']}")
            else:
                state.action_used = False  # invalid action — undo guard; character stays active
        return res

    if name == "modify_hp":
        target = state.find_actor(args["target"])
        if not target:
            return {"ok": False, "error": "Unknown target; call get_state."}
        amount = int(args["amount"])
        res = rules.heal(target, amount) if amount >= 0 else rules.apply_damage(target, -amount)
        return res

    if name == "apply_dice":
        target = state.find_actor(args["target"])
        if not target:
            return {"ok": False, "error": "Unknown target; call get_state."}
        try:
            rolled = rules.roll(args["notation"])
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        kind = args.get("kind", "damage")
        source = args.get("source", "")
        label = f"{source}: " if source else ""
        if kind == "healing":
            applied = rules.heal(target, rolled.total)
            state.record(f"{label}heal {target.name} {rolled.total} ({rolled.describe()})")
            return {
                "ok": True,
                "target": target.name,
                "roll": rolled.total,
                "roll_detail": rolled.describe(),
                "target_hp": applied["hp"],
            }
        applied = rules.apply_damage(target, rolled.total)
        state.record(f"{label}{target.name} takes {rolled.total} damage ({rolled.describe()})")
        return {
            "ok": True,
            "target": target.name,
            "roll": rolled.total,
            "roll_detail": rolled.describe(),
            "target_hp": applied["hp"],
            "downed": applied["downed"],
        }

    if name == "set_quest_flag":
        raw_key = args.get("flag", "")
        key = _normalize_flag_key(raw_key)
        if not key:
            return {
                "ok": False,
                "reason": "bad_flag_key",
                "error": f"Flag key {raw_key!r} normalizes to empty; use letters, digits, or underscores.",
            }
        if key in _RESERVED_FLAG_KEYS:
            return {
                "ok": False,
                "reason": "reserved_flag_key",
                "error": f"{key!r} is reserved for engine mechanics; choose a different flag name.",
            }
        value = args.get("value", True)
        if not (value is None or isinstance(value, (bool, str, int, float))):
            return {
                "ok": False,
                "reason": "bad_flag_value",
                "error": (
                    f"Flag value must be a JSON primitive (bool, str, int, float, or null), "
                    f"got {type(value).__name__}."
                ),
            }
        state.quest_flags[key] = value
        state.record(f"flag {key} = {value!r}")
        return {"ok": True, "flag": key, "value": value}

    if name == "clear_quest_flag":
        raw_key = args.get("flag", "")
        key = _normalize_flag_key(raw_key)
        if not key:
            return {
                "ok": False,
                "reason": "bad_flag_key",
                "error": f"Flag key {raw_key!r} normalizes to empty; use letters, digits, or underscores.",
            }
        removed = key in state.quest_flags
        if removed:
            del state.quest_flags[key]
            state.record(f"flag {key} cleared")
        return {"ok": True, "flag": key, "removed": removed}

    if name == "move_scene":
        if state.scenes:
            scene_key = args.get("scene_key", "").strip()
            # Current scene's exits: {player-facing label: target scene_key}
            current_exits: dict = (
                state.scenes.get(state.current_scene, {}).get("exits", {})
                if state.current_scene else {}
            )
            if not scene_key:
                if current_exits:
                    exits_str = "; ".join(
                        f"{lbl!r} -> {tgt!r}" for lbl, tgt in current_exits.items()
                    )
                    return {
                        "ok": False,
                        "error": f"scene_key is required. Current exits: {exits_str}.",
                    }
                return {
                    "ok": False,
                    "error": "scene_key is required. The current scene has no exits.",
                }
            exit_targets = set(current_exits.values())
            if scene_key not in exit_targets:
                if current_exits:
                    exits_str = "; ".join(
                        f"{lbl!r} -> {tgt!r}" for lbl, tgt in current_exits.items()
                    )
                    return {
                        "ok": False,
                        "error": (
                            f"{scene_key!r} is not a declared exit from the current scene. "
                            f"Available exits: {exits_str}."
                        ),
                    }
                if state.combat_round == 0:
                    state.game_over = True
                    state.game_outcome = "victory"
                    return {"ok": True, "adventure_complete": True, "outcome": "victory"}
                return {
                    "ok": False,
                    "error": "The current scene has no exits — there is nowhere to move.",
                }
            scene_data = state.scenes.get(scene_key)
            if scene_data is None:
                return {
                    "ok": False,
                    "error": f"Scene {scene_key!r} is declared as an exit but has no definition.",
                }
            state.current_scene = scene_key
            state.location = scene_data.get("location", "")
            state.scene = scene_data.get("scene", "")
            state.npcs = {k: expand_npc_entry(v) for k, v in scene_data.get("npcs", {}).items()}
            state.record(f"scene -> {scene_key} ({state.location})")
            return {
                "ok": True,
                "scene_key": scene_key,
                "location": state.location,
                "npcs": list(state.npcs),
            }
        # Free-form fallback for scenarios without a scenes dict.
        if "location" not in args:
            return {"ok": False, "error": "location is required when no named scenes are defined."}
        state.location = args["location"]
        if "scene" in args:
            state.scene = args["scene"]
        state.record(f"scene -> {state.location}")
        return {"ok": True, "location": state.location}

    if name == "get_state":
        d = state.to_dict()
        if state.scenes:
            del d["scenes"]   # omit verbose definitions from model context
            exits: dict = (
                state.scenes.get(state.current_scene, {}).get("exits", {})
                if state.current_scene else {}
            )
            d["exits"] = exits  # {player-facing label: target scene_key}
        return {"ok": True, "state": d}

    if name == "start_combat":
        all_actors = {**state.party, **state.npcs}
        combatant_ids = args.get("combatants", [])
        if not combatant_ids:
            return {"ok": False, "error": "combatants list is empty."}
        canonical_keys: list[str] = []
        seen: set[str] = set()
        unresolved: list[str] = []
        for ident in combatant_ids:
            key = _resolve_actor_key(ident, state)
            if key is None:
                unresolved.append(ident)
            elif key not in seen:
                canonical_keys.append(key)
                seen.add(key)
        if unresolved:
            valid = ", ".join(f"{k} ({a.name})" for k, a in all_actors.items())
            return {"ok": False, "error": f"Unknown combatant(s) {unresolved}. Valid actors: {valid}."}
        ordered, initiatives = rules.roll_initiative({k: all_actors[k] for k in canonical_keys})
        state.combat_order = ordered
        state.combat_initiatives = initiatives
        state.combat_index = 0
        state.combat_round = 1
        state.action_used = False
        state.combat_starting = True   # barrier: deny action tools for rest of this player _execute
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
            if actor is None:
                break
            if active_key in state.party:
                decision = _pc_turn_decision(actor)
                if decision == "skip":
                    skipped.append(active_key)
                    reason = "dead" if actor.is_dead else "stable"
                    state.record(f"skipping {active_key} ({actor.name}) — {reason}")
                    continue
                # "roll" (dying) or "break" (conscious) — stop here
                break
            else:
                if actor.is_down:
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
        state.combat_initiatives = {}
        state.combat_index = 0
        state.combat_round = 0
        state.record("combat ended")
        return {"ok": True}

    if name == "roll_death_save":
        character = state.find_actor(args["character"])
        if not character:
            return {"ok": False, "error": "Unknown character; call get_state."}
        if not (hasattr(character, "death_save_failures") and character.is_dying):
            return {"ok": False, "reason": "not_dying", "character": args["character"]}
        res = rules.roll_death_save(character)
        state.record(
            f"{character.name} death save: roll {res['roll']} → {res['result_kind']} "
            f"(S{res.get('successes', 0)}/F{res.get('failures', 0)})"
        )
        return res

    if name == "skill_check":
        character = state.find_actor(args["character"])
        if not character:
            return {"ok": False, "error": "Unknown character; call get_state to see valid names."}
        if state.combat_starting:
            return {"ok": False, "reason": "combat_starting",
                    "error": "Combat is starting this turn — wait for the initiative order before acting."}
        err = _turn_guard(character.name, state)
        if err:
            return err
        res = rules.skill_check(character, args["ability"], int(args["dc"]))
        if res["ok"]:
            state.record(f"{character.name} {args['ability']} check DC {args['dc']}: {'success' if res['success'] else 'failure'}")
        return res

    if name == "lookup_rule":
        return rules.lookup_rule(args["topic"])

    if name == "add_npc":
        template = args.get("template", "").strip().lower()
        instance_id = args.get("instance_id", "").strip()
        display_name = args.get("name")

        if template not in rules.MONSTERS:
            return {
                "ok": False,
                "error": f"Unknown template {template!r}. Known: {', '.join(sorted(rules.MONSTERS))}.",
            }
        instance_id_lower = instance_id.lower()
        if any(k.lower() == instance_id_lower for k in {**state.npcs, **state.party}):
            return {"ok": False, "error": f"instance_id {instance_id!r} already exists."}

        npc = NPC(**rules.spawn_npc(template, display_name))
        state.npcs[instance_id] = npc

        if state.combat_round > 0:
            # Roll initiative and find the correct sorted insertion slot.
            dex = npc.ability_modifiers.get("dex", 0)
            new_init = rules.roll("1d20").total + dex
            state.combat_initiatives[instance_id] = new_init

            all_actors = {**state.party, **state.npcs}
            insert_pos = len(state.combat_order)
            for i, key in enumerate(state.combat_order):
                existing_init = state.combat_initiatives.get(key, 0)
                existing_actor = all_actors.get(key)
                existing_dex = existing_actor.ability_modifiers.get("dex", 0) if existing_actor else 0
                if new_init > existing_init or (new_init == existing_init and dex > existing_dex):
                    insert_pos = i
                    break

            state.combat_order.insert(insert_pos, instance_id)
            # Shift the pointer so the same combatant stays active.
            if insert_pos <= state.combat_index:
                state.combat_index += 1

            state.record(
                f"add_npc {instance_id} ({npc.name}) from {template}: "
                f"initiative {new_init}, inserted at position {insert_pos}"
            )
            return {
                "ok": True,
                "instance_id": instance_id,
                "name": npc.name,
                "template": template,
                "hp": npc.hp,
                "ac": npc.ac,
                "combat": True,
                "initiative": new_init,
                "position": insert_pos,
                "combat_order": state.combat_order,
            }

        state.record(f"add_npc {instance_id} ({npc.name}) from {template}")
        return {
            "ok": True,
            "instance_id": instance_id,
            "name": npc.name,
            "template": template,
            "hp": npc.hp,
            "ac": npc.ac,
            "combat": False,
        }

    return {"ok": False, "error": f"Unknown tool {name!r}"}
