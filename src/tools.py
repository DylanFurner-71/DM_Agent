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


def _target(exit_val) -> str:
    """Extract the destination scene_key from a bare string or gated exit dict."""
    return exit_val["to"] if isinstance(exit_val, dict) else exit_val


def _exits_for_model(exits: dict) -> dict:
    """Return exits with sensitive fields stripped — safe for model context.

    'denied' is stripped so the model can't echo it before the gate fires.
    'requires_answer' is replaced with answer_required: true — the literal
    password is never surfaced to the model as structured state.
    Builds a copy; never mutates state.scenes.
    """
    result = {}
    for label, val in exits.items():
        if isinstance(val, dict):
            entry: dict = {"to": val["to"]}
            if "requires" in val:
                entry["requires"] = val["requires"]
            if "requires_answer" in val:
                entry["answer_required"] = True
            result[label] = entry
        else:
            result[label] = val
    return result


def _available_reinforcements(scene_data: dict, quest_flags: dict) -> list[str]:
    """Reinforcement ids whose authored trigger is currently satisfied.

    An entry with a `requires` flag is hidden until that flag is set (mirrors
    gated exits); an entry with no `requires` is always available. The model only
    ever sees — and can only spawn — ids returned here; locked ones stay invisible.
    """
    out = []
    for rid, entry in scene_data.get("reinforcements", {}).items():
        req = entry.get("requires") if isinstance(entry, dict) else None
        if not req or quest_flags.get(req):
            out.append(rid)
    return out


def _available_hazards(scene_data: dict, quest_flags: dict, sprung: set, scene_key: str) -> list[dict]:
    """Hazard ids whose gate is open and which have not already sprung.

    Mirrors _available_reinforcements: a hazard with a `requires` flag is hidden until
    that flag is set; a one-shot hazard (once, default True) already in `sprung` is
    omitted. Returns [{id, name, hidden}] only — never the DC, ability, or damage, which
    are engine-owned and surface only in the trigger_hazard result.
    """
    out = []
    for hid, entry in scene_data.get("hazards", {}).items():
        if not isinstance(entry, dict):
            continue
        req = entry.get("requires")
        if req and not quest_flags.get(req):
            continue
        if entry.get("once", True) and f"{scene_key}:{hid}" in sprung:
            continue
        out.append({"id": hid, "name": entry.get("name", hid), "hidden": bool(entry.get("hidden", False))})
    return out


def _normalize_flag_key(raw: str) -> str | None:
    """Normalize a quest flag key; return None if the result is empty.

    Pipeline: strip → lowercase → spaces/hyphens → underscores →
    remove chars outside [a-z0-9_].
    """
    key = raw.strip().lower().replace(" ", "_").replace("-", "_")
    key = re.sub(r"[^a-z0-9_]", "", key)
    return key or None


def _normalize_answer(s: str) -> str:
    """Normalize a spoken password: strip surrounding whitespace/punctuation, lowercase, collapse spaces."""
    s = s.strip()
    s = re.sub(r"^[^\w]+|[^\w]+$", "", s)  # strip surrounding non-word chars (punctuation etc.)
    s = s.lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _redact_quest_flags(flags: dict) -> dict:
    return {k: "<redacted>" if isinstance(v, str) else v for k, v in flags.items()}


# NPC fields the model must never see. alertness_dc is the hidden stealth DC the
# README redacts from "everything the model sees"; disposition_dc is its social twin —
# the one-shot persuasion gate. The model is meant to learn an NPC's reachability by
# *attempting* (influence_npc -> 'immovable'; attempt_ambush -> 'cannot_ambush'/bar),
# never by reading the number. The per-turn _state_snapshot already omits both; this
# closes the same leak on the get_state channel (which dumps state.to_dict()).
_HIDDEN_NPC_FIELDS = ("disposition_dc", "alertness_dc")


def _npcs_for_model(npcs: dict) -> dict:
    """Strip the hidden challenge DCs (_HIDDEN_NPC_FIELDS) from an NPC dict map."""
    return {
        key: ({k: v for k, v in entry.items() if k not in _HIDDEN_NPC_FIELDS}
              if isinstance(entry, dict) else entry)
        for key, entry in npcs.items()
    }

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
                "advantage":    {"type": "boolean", "description": "Set true ONLY when the fiction clearly favors the attacker (flanking, target prone/restrained, attacking from hiding). The engine rolls 2d20 and keeps the higher. Don't set by default."},
                "disadvantage": {"type": "boolean", "description": "Set true ONLY when the attacker is clearly hindered (attacker blinded/prone, target heavily obscured). The engine rolls 2d20 and keeps the lower. Setting both advantage and disadvantage cancels to a normal roll."},
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
                "advantage":    {"type": "boolean", "description": "For a spell-attack spell only: set true ONLY when the fiction clearly favors the caster (target prone/restrained, casting from hiding). The engine rolls 2d20 and keeps the higher. Inert for auto-hit spells like magic_missile."},
                "disadvantage": {"type": "boolean", "description": "For a spell-attack spell only: set true ONLY when the caster is clearly hindered (blinded, target heavily obscured). The engine rolls 2d20 and keeps the lower. Setting both cancels to a normal roll."},
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
            "The magnitude may not exceed the target's max HP (ok=false reason "
            "'amount_out_of_range'); larger swings are refused. "
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
            "A scene whose exits map is empty is TERMINAL: to CONCLUDE the adventure from "
            "such a scene once no hostiles remain, call move_scene with the current scene's "
            "own key — the engine grants victory and ends the run (ok=false reason "
            "'hostiles_present' if a foe still stands).\n"
            "FREE-FORM (no named scenes defined): pass location string and optional scene "
            "description to update them directly.\n"
            "Not usable during combat — returns ok=false reason 'in_combat'; end the fight first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scene_key": {"type": "string", "description": "A scene_key that appears as a VALUE in the current scene's exits map, e.g. 'ember_chamber'."},
                "location":  {"type": "string", "description": "Free-form location name. Use when no named scenes are defined."},
                "scene":     {"type": "string", "description": "Free-form scene description. Optional companion to location."},
                "answer":    {"type": "string", "description": "Spoken word or phrase at an answer_required exit. Relay EXACTLY what the player said — do not translate, complete, or correct it."},
            },
        },
    },
    {
        "name": "get_state",
        "description": "Read the current game state (party HP and slots, NPCs, location, flags). Call this whenever you need exact numbers.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "attempt_ambush",
        "description": (
            "Before combat, attempt a group stealth ambush. The engine rolls a Dex check for every "
            "conscious party member vs the highest alertness DC among living hostiles. "
            "SUCCESS requires every member to beat the bar (weakest-link). "
            "On success, pending_ambush is set; call start_combat next — the engine will mark "
            "hostiles as surprised so they lose their first turn. "
            "On failure, no surprise — call start_combat for a fair fight. "
            "This tool does NOT start combat; call start_combat separately. "
            "Rejected (ok=false) when: combat is already in progress ('in_combat'); an attempt was "
            "already made this scene ('already_attempted'); no living hostiles present ('no_target'). "
            "If any hostile is always-alert ('cannot_ambush'), the engine refuses the ambush AND "
            "auto-starts combat (combat_started=true, with combat_order/active/active_name/round) — "
            "the alert foe noticed the party; do NOT call start_combat yourself, just narrate the "
            "spotted approach and announce the initiative order. "
            "ok=True with success=False means the check was valid but the party failed stealth."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
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
        "description": (
            "Roll d20 + a character's ability modifier against a Difficulty Class (DC). "
            "Use for any non-attack check — perception, persuasion, athletics, stealth, arcana, etc. "
            "Whenever the check corresponds to a named skill, pass skill (e.g. 'stealth', "
            "'persuasion'): the engine then uses that skill's governing ability and adds the "
            "character's proficiency bonus if they're proficient (twice for expertise). Omit skill "
            "for a raw ability check. You never supply the bonus — the engine owns it. "
            "Set use_inspiration=true ONLY when the player chooses to spend that character's "
            "inspiration on this check — the engine rolls with advantage (2d20 keep higher) and "
            "spends the point; if they hold none the result reports inspiration_used=false."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "character": {"type": "string", "description": "Name of the character making the check"},
                "ability": {"type": "string", "description": "Ability name: str, dex, con, int, wis, or cha (ignored if a known skill is named)"},
                "dc": {"type": "integer", "description": "Difficulty Class the total must meet or exceed"},
                "skill": {"type": "string", "description": "Optional 5e skill, e.g. 'stealth', 'perception', 'persuasion'. Names the skill so the engine can add proficiency/expertise and pick the governing ability."},
                "use_inspiration": {"type": "boolean", "description": "Spend the character's inspiration for advantage on this check. Only when the player opts to."},
            },
            "required": ["character", "ability", "dc"],
        },
    },
    {
        "name": "saving_throw",
        "description": (
            "Roll a saving throw for a character to RESIST an effect — d20 + ability "
            "modifier vs a DC. Use this (NOT skill_check) whenever something happens TO a "
            "character that they try to resist: a trap (usually dex), poison or disease "
            "(con), a fear or charm effect (wis or cha), being shoved (str), and the like. "
            "A saving throw is REACTIVE, not an action: it is not turn-guarded, costs no "
            "action, and may be rolled for ANY affected character regardless of whose turn "
            "it is. Proficient saves add proficiency automatically. The engine owns the "
            "roll — never decide the outcome yourself; on a failed save, apply the "
            "consequence with the matching tool (apply_dice for dice damage, modify_hp for "
            "a flat amount). Set use_inspiration=true ONLY when the player chooses to spend "
            "that character's inspiration on this save — the engine rolls with advantage "
            "(2d20 keep higher) and spends the point; if they hold none the result reports "
            "inspiration_used=false."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "character": {"type": "string", "description": "Name of the character making the save"},
                "ability": {"type": "string", "description": "Ability name: str, dex, con, int, wis, or cha"},
                "dc": {"type": "integer", "description": "Difficulty Class the total must meet or exceed"},
                "use_inspiration": {"type": "boolean", "description": "Spend the character's inspiration for advantage on this save. Only when the player opts to."},
            },
            "required": ["character", "ability", "dc"],
        },
    },
    {
        "name": "trigger_hazard",
        "description": (
            "Spring an author-placed hazard or trap in the current scene against one or more "
            "characters. The scene's `hazards` manifest is the SOLE authority — you may only "
            "trigger a hazard id listed in the state snapshot's `hazards` (ok=false reason "
            "'not_declared' otherwise); you cannot invent a trap, and you NEVER supply its "
            "save ability, DC, or damage — those are author-owned and the engine applies them. "
            "characters: optional list of names to affect; omit to affect every conscious party "
            "member (an area hazard). For each affected character the engine rolls the authored "
            "saving throw and applies the authored damage atomically — full on a failed save, "
            "half if the hazard is save-for-half, none on a success. A hazard marked hidden is a "
            "concealed trap: do not reveal it until you spring it — trigger it when the fiction "
            "warrants (the party crosses the plate, opens the chest, lingers in the gas). "
            "One-shot hazards fire once (ok=false reason 'already_sprung' after); a gated hazard "
            "is refused until its flag is set ('locked'). Do NOT call saving_throw or apply_dice "
            "yourself for a declared hazard — trigger_hazard owns the whole resolution."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hazard_id": {"type": "string", "description": "Hazard id from the scene's manifest, e.g. 'dart_trap'."},
                "characters": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Names of affected characters. Omit to affect all conscious party members.",
                },
            },
            "required": ["hazard_id"],
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
        "name": "take_item",
        "description": (
            "Take an item from the current scene's loot list and add it to a party member's "
            "inventory. The scene's loot list is the sole authority — the model cannot conjure "
            "items not listed there; ok=false with reason 'not_available' if absent (mirrors "
            "move_scene rejecting undeclared exits). Each take removes exactly one instance, "
            "so items cannot be farmed. "
            "carrier: optional — omit to auto-select the sole party member; name one explicitly "
            "when multiple are present (returns reason 'ambiguous_carrier' with candidates). "
            "Not action-guarded — looting is a free interaction, valid in and out of combat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item":    {"type": "string", "description": "Item id to take, e.g. 'healing_potion'. Must appear in the current scene's loot list."},
                "carrier": {"type": "string", "description": "Name of the party member who receives the item. Omit to auto-select when only one member is present."},
            },
            "required": ["item"],
        },
    },
    {
        "name": "add_npc",
        "description": (
            "Bring an AUTHOR-DECLARED reinforcement into the current scene by its instance_id. "
            "The scene's reinforcements manifest is the sole authority — you may only spawn an "
            "instance_id the author declared there (ok=false reason 'not_declared' otherwise, "
            "with the available ids listed). You cannot conjure a monster the author did not place, "
            "exactly as move_scene cannot invent an exit and take_item cannot invent loot. "
            "Stats, name, and template all come from the manifest entry — never supply them. "
            "You may only spawn an id that appears in the state snapshot's 'reinforcements' "
            "list; some reinforcements stay hidden behind an authored trigger and cannot be "
            "spawned until that trigger fires (ok=false reason 'locked' if you try). "
            "Each reinforcement spawns once (ok=false reason 'already_spawned' on a repeat). "
            "Outside combat the NPC joins the roster and can be named in start_combat. "
            "During combat the engine rolls its initiative and inserts it at the correct sorted "
            "slot; the active combatant is unchanged. "
            "Reveal reinforcements through the fiction (a door bursts open) — do not announce the manifest."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "The reinforcement key to spawn; must be declared in the current scene's reinforcements manifest, e.g. 'goblin_two'."},
            },
            "required": ["instance_id"],
        },
    },
    {
        "name": "use_item",
        "description": (
            "Use a consumable item from a character's inventory — on themselves, or on "
            "a party ally named in `target` (e.g. pour a healing potion down a downed "
            "ally's throat to revive them; the heal revives and resets death saves). "
            "The item must be in character.inventory (ok=false 'not_in_inventory') AND "
            "in the CONSUMABLES table (ok=false 'not_consumable' — a mace is not drinkable). "
            "`target`, if given, must be a party member (ok=false 'unknown_target'). "
            "In combat, using an item IS the USER's action and is turn-guarded — the "
            "active character spends their turn administering it; the recipient need not "
            "be active and may be unconscious. "
            "Out of combat, the guards no-op and the item is used freely. "
            "On a validation failure the action guard is undone so the character keeps their turn. "
            "Applies the effect atomically via the engine (rolled == applied). A Pearl of "
            "Power at full slots is refused ('slots_full') and not consumed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "character": {"type": "string", "description": "Name of the character using the item (spends the action)"},
                "item":      {"type": "string", "description": "Item id to use, e.g. 'healing_potion'. Must be in character's inventory."},
                "target":    {"type": "string", "description": "Optional party ally to receive the item. Omit for self-use."},
            },
            "required": ["character", "item"],
        },
    },
    {
        "name": "influence_npc",
        "description": (
            "Attempt to sway a hostile NPC through persuasion or intimidation. "
            "Rolls Charisma against the NPC's disposition_dc; on success the NPC turns non-hostile. "
            "Each NPC allows exactly one attempt — the result is permanent whether it succeeds or fails. "
            "Invalid if: the NPC is already down, not hostile, has no disposition_dc (immovable), "
            "or a social attempt was already made. "
            "In combat this costs the character's action and is turn-guarded. "
            "Out of combat the guards no-op. "
            "On a validation failure the action guard is undone so the character keeps their turn. "
            "The engine rolls and applies the result atomically — never call skill_check separately. "
            "OUT-OF-COMBAT FAILURE: the engine automatically initiates combat for all present fighters "
            "and merges the result — the response contains combat_started=true, combat_order, active, "
            "and active_name. Announce the order and stop; do NOT call start_combat yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "character": {"type": "string", "description": "Name of the party member making the attempt"},
                "npc":       {"type": "string", "description": "Name of the NPC to influence"},
                "approach":  {"type": "string", "enum": ["persuade", "intimidate"], "description": "The social approach taken"},
            },
            "required": ["character", "npc", "approach"],
        },
    },
    {
        "name": "recruit_npc",
        "description": (
            "Recruit a non-hostile NPC as a travelling companion after the party has "
            "won them over. The NPC must already be non-hostile (de-escalate first with "
            "influence_npc) — ok=false 'hostile' otherwise. Recruit BETWEEN fights, not "
            "mid-combat (ok=false 'in_combat'); also rejected if the NPC is down "
            "('target_down') or already a companion ('already_companion'). "
            "A companion follows the party across scenes and, once you include it in "
            "start_combat, fights hostiles on the party's side (the engine resolves its "
            "attacks automatically, like a hostile's). It does not replace a party member: "
            "a full party wipe is still a defeat even if a companion survives."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "npc": {"type": "string", "description": "Name of the non-hostile NPC to recruit"},
            },
            "required": ["npc"],
        },
    },
    {
        "name": "award_inspiration",
        "description": (
            "Award a party member their single session reroll (inspiration) — a DM reward "
            "for clever play, a strong roleplay moment, or a bold choice. This is YOUR "
            "discretionary call, but the engine owns the budget: a character holds at most "
            "one and gets only ONE for the entire session — ok=false reason 'at_cap' if they "
            "already hold one, 'already_used' if they have already spent theirs (it can never "
            "be re-awarded). Only party members can be inspired (ok=false 'not_a_pc' for an "
            "NPC). The player spends it later by choosing use_inspiration on a skill_check or "
            "saving_throw. Award sparingly and narrate the moment of inspiration."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "character": {"type": "string", "description": "Name of the party member to inspire"},
            },
            "required": ["character"],
        },
    },
    {
        "name": "add_gold",
        "description": (
            "Credit gold pieces to a party member's purse — loot, a reward, a sale. Gold is "
            "engine-owned and tracked only through this tool and spend_gold; never narrate a "
            "balance into existence or track it yourself. amount is a positive integer. "
            "Only party members carry gold (ok=false 'not_a_pc' for an NPC)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "character": {"type": "string", "description": "Name of the party member gaining gold"},
                "amount": {"type": "integer", "description": "Gold pieces to add (positive)"},
            },
            "required": ["character", "amount"],
        },
    },
    {
        "name": "spend_gold",
        "description": (
            "Deduct gold pieces from a party member's purse — a purchase, a bribe, a toll. The "
            "engine owns the balance and REFUSES an overspend: ok=false reason "
            "'insufficient_gold' (with their current gold) if they can't cover it, and nothing "
            "is deducted — narrate that they can't afford it rather than spending coin they "
            "lack. amount is a positive integer. Only party members carry gold (ok=false "
            "'not_a_pc' for an NPC)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "character": {"type": "string", "description": "Name of the party member spending gold"},
                "amount": {"type": "integer", "description": "Gold pieces to spend (positive)"},
            },
            "required": ["character", "amount"],
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


def _select_npc_attack_target(state):
    """Select the best PC target for a simple NPC attack.

    Policy: lowest-HP conscious PC wins. Ties broken by position in
    combat_order (earlier = higher priority). Edit only this function
    to swap targeting strategy.
    """
    order_pos = {k: i for i, k in enumerate(state.combat_order)}
    candidates = [
        (pc, order_pos.get(key, len(state.combat_order)))
        for key, pc in state.party.items()
        if not pc.is_down
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0].hp, item[1]))
    return candidates[0][0]


def _select_companion_attack_target(state):
    """Select the best hostile target for a companion ally's attack: lowest-HP
    living hostile, ties broken by initiative position."""
    order_pos = {k: i for i, k in enumerate(state.combat_order)}
    candidates = [
        (npc, order_pos.get(key, len(state.combat_order)))
        for key, npc in state.npcs.items()
        if npc.hostile and not npc.is_down
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0].hp, item[1]))
    return candidates[0][0]


def resolve_npc_action(npc, state) -> tuple[dict, dict] | None:
    """Engine-resolve a simple NPC's turn without an LLM call.

    Returns (input_args, dispatch_result) to append to tool_trace,
    or None to signal the model should decide instead.

    A recruited companion (non-hostile, companion=True) fights on the party's side:
    it attacks the lowest-HP living hostile. A plain hostile attacks the party.

    Falls back to None when:
      - NPC is a plain non-hostile (no companion flag) — stands aside, no action
      - No valid target exists (companion: no living hostile; hostile: no conscious PC)
      - NPC has capabilities the engine doesn't model (e.g. spells)
    """
    if getattr(npc, "spells", None):
        return None
    if getattr(npc, "companion", False) and not npc.hostile:
        target = _select_companion_attack_target(state)
    elif npc.hostile:
        target = _select_npc_attack_target(state)
    else:
        return None  # plain non-hostile NPC stands aside
    if target is None:
        return None
    # Pass defender explicitly — _resolve_offensive_target auto-pick only
    # scans hostile NPCs (PC→NPC direction) and won't auto-target for these cases.
    args = {"attacker": npc.name, "defender": target.name}
    return args, dispatch("attack", args, state)


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
        res = rules.attack(attacker, target, args.get("weapon"),
                           advantage=bool(args.get("advantage", False)),
                           disadvantage=bool(args.get("disadvantage", False)))
        if res["ok"]:
            mode = f" ({res['roll_mode']})" if res.get("roll_mode") else ""
            state.record(f"{attacker.name} attacks {target.name}: {'hit' if res['hit'] else 'miss'}{mode}")
            if auto_selected:
                res["auto_target"] = target.name
            if res["hit"] and hasattr(target, "hostile") and not target.hostile:
                target.hostile = True
                state.record(f"{target.name} provoked — now hostile")
                res["provoked"] = True
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
            res = rules.cast_damaging_spell(caster, target, spell_name, int(args["spell_level"]),
                                            advantage=bool(args.get("advantage", False)),
                                            disadvantage=bool(args.get("disadvantage", False)))
            if res["ok"]:
                mode = f" ({res['roll_mode']})" if res.get("roll_mode") else ""
                state.record(f"{caster.name} casts {spell_name} at {target.name}: {res.get('damage_detail', 'no damage table')}{mode}")
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
        # Bound the magnitude: a single flat effect may not exceed the target's
        # max HP. This caps the one place a model-supplied number reaches tracked
        # state — an arbitrary HP swing is refused, not silently applied.
        if abs(amount) > target.max_hp:
            return {
                "ok": False,
                "reason": "amount_out_of_range",
                "error": (
                    f"modify_hp amount {amount} exceeds {target.name}'s max HP "
                    f"({target.max_hp}). Use apply_dice for rolled effects, or supply "
                    f"a plausible flat amount."
                ),
            }
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
        if isinstance(value, str) and (key.endswith("_password") or key.endswith("_answer")):
            return {
                "ok": False,
                "reason": "reserved_answer_key",
                "error": (
                    f"{key!r} is an answer-gate flag; its value is set by the scenario, "
                    f"not by the model. Use a boolean or integer if you need to track state."
                ),
            }
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
        return {"ok": True, "flag": key, "value": "<redacted>" if isinstance(value, str) else value}

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
        # No travel mid-combat: changing scenes here would rebuild state.npcs from the
        # destination while leaving combat_order/round/index pointing at the old scene's
        # combatants — a stale, incoherent fight. Refuse outright (mirrors recruit_npc);
        # the sanctioned way to leave a fight is to end it first (defeat/flee/parley).
        if state.combat_round > 0:
            return {
                "ok": False,
                "reason": "in_combat",
                "error": "Cannot travel while combat is underway — end the fight first.",
            }
        if state.scenes:
            scene_key = args.get("scene_key", "").strip()
            # Current scene's exits: {player-facing label: bare scene_key or gated dict}
            current_scene_data: dict = (
                state.scenes.get(state.current_scene, {})
                if state.current_scene else {}
            )
            current_exits: dict = current_scene_data.get("exits", {})
            if not scene_key:
                if current_exits:
                    exits_str = "; ".join(
                        f"{lbl!r} -> {_target(v)!r}" for lbl, v in current_exits.items()
                    )
                    return {
                        "ok": False,
                        "error": f"scene_key is required. Current exits: {exits_str}.",
                    }
                return {
                    "ok": False,
                    "error": "scene_key is required. The current scene has no exits.",
                }
            exit_targets = {_target(v) for v in current_exits.values()}
            if scene_key not in exit_targets:
                if current_exits:
                    exits_str = "; ".join(
                        f"{lbl!r} -> {_target(v)!r}" for lbl, v in current_exits.items()
                    )
                    return {
                        "ok": False,
                        "error": (
                            f"{scene_key!r} is not a declared exit from the current scene. "
                            f"Available exits: {exits_str}."
                        ),
                    }
                # Terminal scene: concluding here ends the run. Hard-gate it.
                if state.combat_round == 0:
                    # Cannot conclude while a hostile still stands — the model's
                    # leave/finish trigger is soft, but this refusal is hard, so a
                    # premature conclude can never end the run with foes alive.
                    if any(n.hostile and not n.is_down for n in state.npcs.values()):
                        return {
                            "ok": False,
                            "reason": "hostiles_present",
                            "error": "Foes still stand — finish the fight before leaving.",
                        }
                    exit_req = current_scene_data.get("exit_requires")
                    if exit_req and not state.quest_flags.get(exit_req):
                        return {
                            "ok": False,
                            "reason": "locked",
                            "required_flag": exit_req,
                            "error": current_scene_data.get("exit_denied", "That way is barred."),
                        }
                    state.game_over = True
                    state.game_outcome = "victory"
                    return {"ok": True, "adventure_complete": True, "outcome": "victory"}
                return {
                    "ok": False,
                    "error": "The current scene has no exits — there is nowhere to move.",
                }
            # scene_key is a declared exit target — check for a flag gate.
            exit_val = next(v for v in current_exits.values() if _target(v) == scene_key)
            if isinstance(exit_val, dict) and "requires" in exit_val:
                req = exit_val["requires"]
                # Gate check: truthiness (extension point: {"flag": x, "equals": v}).
                if not state.quest_flags.get(req):
                    return {
                        "ok": False,
                        "reason": "locked",
                        "required_flag": req,
                        "error": exit_val.get("denied", "That way is barred."),
                    }
            if isinstance(exit_val, dict) and "requires_answer" in exit_val:
                answer_arg = (args.get("answer") or "").strip()
                if not answer_arg or _normalize_answer(answer_arg) != _normalize_answer(exit_val["requires_answer"]):
                    return {
                        "ok": False,
                        "reason": "locked",
                        "error": exit_val.get("denied", "That way is barred."),
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
            new_npcs = {k: expand_npc_entry(v) for k, v in scene_data.get("npcs", {}).items()}
            # Recruited companions follow the party into the new scene. Keep their key
            # unless the destination already uses it, then suffix to avoid a clash.
            followers = []
            for key, npc in state.npcs.items():
                if getattr(npc, "companion", False) and not npc.is_down:
                    npc.surprised = False  # a fresh scene is not a surprise round
                    new_key = key if key not in new_npcs else f"{key}_ally"
                    new_npcs[new_key] = npc
                    followers.append(npc.name)
            state.npcs = new_npcs
            state.pending_ambush = False
            state.ambush_attempted = False
            state.record(f"scene -> {scene_key} ({state.location})"
                         + (f"; companions follow: {followers}" if followers else ""))
            return {
                "ok": True,
                "scene_key": scene_key,
                "location": state.location,
                "npcs": list(state.npcs),
                **({"companions": followers} if followers else {}),
            }
        # Free-form fallback for scenarios without a scenes dict.
        if "location" not in args:
            return {"ok": False, "error": "location is required when no named scenes are defined."}
        state.location = args["location"]
        if "scene" in args:
            state.scene = args["scene"]
        state.pending_ambush = False
        state.ambush_attempted = False
        state.record(f"scene -> {state.location}")
        return {"ok": True, "location": state.location}

    if name == "get_state":
        d = state.to_dict()
        # Drop the unbounded session history. transcript/narrative/log grow every
        # turn and the model never acts on them (it has the rolling narration window),
        # but to_dict carries them for SAVES. Re-injecting them here cost ~4.5k tokens
        # late in a profiled run (successful_end_trigger turn 11 = 20.4s, 2.5x avg) and
        # rode along through every later hop of that turn. Extends the lean-context
        # principle (DECISIONS §3) already applied to `scenes`; saves stay complete.
        for _hist in ("transcript", "narrative", "log"):
            d.pop(_hist, None)
        d["quest_flags"] = _redact_quest_flags(d.get("quest_flags", {}))
        if d.get("npcs"):
            d["npcs"] = _npcs_for_model(d["npcs"])  # hide disposition_dc / alertness_dc
        if state.scenes:
            del d["scenes"]   # omit verbose definitions from model context
            current_scene_data = (
                state.scenes.get(state.current_scene, {})
                if state.current_scene else {}
            )
            exits = current_scene_data.get("exits", {})
            d["exits"] = _exits_for_model(exits)  # denied text stripped; requires surfaced
            loot = current_scene_data.get("loot", [])
            d["loot"] = loot
            exit_req = current_scene_data.get("exit_requires")
            if exit_req:
                d["exit_requires"] = exit_req
            reinf = _available_reinforcements(current_scene_data, state.quest_flags)
            if reinf:
                d["reinforcements"] = reinf  # only gate-open ids; locked ones stay hidden
            haz = _available_hazards(current_scene_data, state.quest_flags, set(state.sprung_hazards), state.current_scene)
            if haz:
                d["hazards"] = haz  # id/name/hidden only — DC and damage stay engine-owned
        return {"ok": True, "state": d}

    if name == "attempt_ambush":
        if state.combat_round > 0:
            return {"ok": False, "reason": "in_combat", "error": "Cannot attempt ambush during combat."}
        if state.ambush_attempted:
            return {"ok": False, "reason": "already_attempted", "error": "Ambush already attempted in this scene."}
        hostiles = [n for n in state.npcs.values() if n.hostile and not n.is_down]
        if not hostiles:
            return {"ok": False, "reason": "no_target", "error": "No living hostiles to ambush."}
        if any(h.alertness_dc is None for h in hostiles):
            # An always-alert foe spots the party — no surprise is possible, and
            # the attempt itself tips the party's hand. Drop straight into a fair
            # fight, mirroring the failed-parley auto-combat path below.
            state.ambush_attempted = True
            state.record("attempt_ambush cannot_ambush — always-alert foe; auto-starting combat")
            result = {
                "ok": False,
                "reason": "cannot_ambush",
                "error": "One or more foes are always alert — cannot get the drop on them.",
            }
            combatant_keys = (
                [k for k, c in state.party.items() if not c.is_down] +
                [k for k, n in state.npcs.items() if n.hostile and not n.is_down]
            )
            if combatant_keys:
                combat_res = dispatch("start_combat", {"combatants": combatant_keys}, state)
                result["combat_started"] = True
                result["combat_order"] = combat_res.get("combat_order")
                result["active"] = combat_res.get("active")
                result["active_name"] = combat_res.get("active_name")
                result["round"] = combat_res.get("round")
            return result

        bar = max(h.alertness_dc for h in hostiles)
        party_members = [c for c in state.party.values() if not c.is_down]
        rolls = []
        for member in party_members:
            check = rules.skill_check(member, "dex", bar)
            rolls.append({
                "character": member.name,
                "roll": check["roll"],
                "modifier": check["modifier"],
                "total": check["total"],
                "success": check["success"],
            })
        success = all(r["success"] for r in rolls)

        state.ambush_attempted = True
        if success:
            state.pending_ambush = True
        state.record(f"attempt_ambush bar={bar} success={success}")

        return {
            "ok": True,
            "success": success,
            "bar": bar,
            "rolls": rolls,
            "hostiles": [h.name for h in hostiles],
        }

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
        # Consume any pending ambush: mark hostile NPCs as surprised.
        surprised_names: list[str] = []
        if state.pending_ambush:
            for key in ordered:
                if key in state.npcs and state.npcs[key].hostile:
                    state.npcs[key].surprised = True
                    surprised_names.append(state.npcs[key].name)
            state.pending_ambush = False
        active_key = ordered[0]
        state.record(f"combat started round 1, order: {ordered}, first: {active_key}")
        result: dict = {"ok": True, "combat_order": ordered, "active": active_key, "active_name": all_actors[active_key].name, "round": 1}
        if surprised_names:
            result["surprised"] = surprised_names
        return result

    if name == "next_turn":
        if not state.combat_order:
            return {"ok": False, "error": "No combat in progress; call start_combat first."}
        if state.combat_starting:
            return {"ok": False, "reason": "combat_starting",
                    "error": "Combat just started — let the engine resolve the surprise round before advancing turns."}
        all_actors = {**state.party, **state.npcs}
        skipped_downed: list[str] = []
        skipped_surprised: list[str] = []
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
                    skipped_downed.append(active_key)
                    reason = "dead" if actor.is_dead else "stable"
                    state.record(f"skipping {active_key} ({actor.name}) — {reason}")
                    continue
                # "roll" (dying) or "break" (conscious) — stop here
                break
            else:
                if actor.is_down:
                    skipped_downed.append(active_key)
                    state.record(f"skipping downed {active_key} ({actor.name})")
                    continue
                if state.combat_round == 1 and actor.surprised:
                    skipped_surprised.append(active_key)
                    state.record(f"surprised, skipping {active_key} ({actor.name})")
                    actor.surprised = False
                    continue
                break
        else:
            return {"ok": False, "error": "All combatants are at 0 HP; call end_combat."}
        active_name = actor.name if actor else active_key
        state.action_used = False
        state.record(f"round {state.combat_round}, turn -> {active_key} ({active_name})")
        result = {"ok": True, "active": active_key, "active_name": active_name, "round": state.combat_round, "combat_index": state.combat_index}
        if skipped_downed:
            result["skipped_downed"] = skipped_downed
        if skipped_surprised:
            result["skipped_surprised"] = skipped_surprised
        return result

    if name == "end_combat":
        state.combat_order = []
        state.combat_initiatives = {}
        state.combat_index = 0
        state.combat_round = 0
        for npc in state.npcs.values():
            npc.surprised = False
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
        res = rules.skill_check(character, args["ability"], int(args["dc"]),
                                bool(args.get("use_inspiration", False)),
                                skill=args.get("skill"))
        if res["ok"]:
            insp = " (inspired)" if res.get("inspiration_used") else ""
            prof = " (expertise)" if res.get("expertise") else (" (proficient)" if res.get("proficient") else "")
            label = res.get("skill", args["ability"])
            state.record(f"{character.name} {label} check DC {args['dc']}{prof}{insp}: {'success' if res['success'] else 'failure'}")
        return res

    if name == "saving_throw":
        character = state.find_actor(args["character"])
        if not character:
            return {"ok": False, "error": "Unknown character; call get_state to see valid names."}
        # A saving throw is reactive — it resists an effect and is NOT the actor's
        # action. Deliberately no turn/action/combat_starting guard: any affected
        # character may save on anyone's turn (e.g. everyone saves vs a trap).
        res = rules.saving_throw(character, args["ability"], int(args["dc"]), bool(args.get("use_inspiration", False)))
        if res["ok"]:
            insp = " (inspired)" if res.get("inspiration_used") else ""
            state.record(
                f"{character.name} {args['ability']} save DC {args['dc']}{insp}: "
                f"{'success' if res['success'] else 'failure'}"
            )
        return res

    if name == "award_inspiration":
        character = state.find_actor(args["character"])
        if not character:
            return {"ok": False, "error": "Unknown character; call get_state to see valid names."}
        # Inspiration is a PC-only reward; an NPC has no death-save lifecycle attr.
        if not hasattr(character, "death_save_failures"):
            return {"ok": False, "reason": "not_a_pc",
                    "error": f"{character.name} is not a party member; only PCs can be inspired."}
        res = rules.award_inspiration(character)
        if res["ok"]:
            state.record(f"{character.name} awarded inspiration")
        return res

    if name in ("add_gold", "spend_gold"):
        character = state.find_actor(args["character"])
        if not character:
            return {"ok": False, "error": "Unknown character; call get_state to see valid names."}
        # Gold is a PC-only purse; NPCs have no gold field.
        if not hasattr(character, "death_save_failures"):
            return {"ok": False, "reason": "not_a_pc",
                    "error": f"{character.name} is not a party member; only PCs carry gold."}
        amount = int(args["amount"])
        if name == "add_gold":
            res = rules.add_gold(character, amount)
            if res["ok"]:
                state.record(f"{character.name} gains {amount} gp (→ {res['gold']})")
        else:
            res = rules.spend_gold(character, amount)
            if res["ok"]:
                state.record(f"{character.name} spends {amount} gp (→ {res['gold']})")
            else:
                state.record(f"{character.name} cannot spend {amount} gp ({res['reason']})")
        return res

    if name == "trigger_hazard":
        hazard_id = args.get("hazard_id", "").strip()
        if not hazard_id:
            return {"ok": False, "reason": "missing_hazard_id", "error": "hazard_id is required."}
        scene_data = state.scenes.get(state.current_scene, {}) if state.current_scene else {}
        manifest = scene_data.get("hazards", {})
        canon = next((k for k in manifest if k.lower() == hazard_id.lower()), None)
        entry = manifest.get(canon) if canon else None
        if not isinstance(entry, dict):
            available = ", ".join(
                h["id"] for h in _available_hazards(
                    scene_data, state.quest_flags, set(state.sprung_hazards), state.current_scene)
            ) or "none"
            return {
                "ok": False,
                "reason": "not_declared",
                "error": f"{hazard_id!r} is not a declared hazard in this scene. Available: {available}.",
            }
        # Authored trigger: a hazard with a `requires` flag stays disarmed until set
        # (mirrors gated reinforcements / exits) and is hidden from the snapshot until then.
        req = entry.get("requires")
        if req and not state.quest_flags.get(req):
            return {"ok": False, "reason": "locked", "required_flag": req,
                    "error": f"{hazard_id!r} is not armed yet — its trigger has not fired."}
        once = entry.get("once", True)
        sprung_key = f"{state.current_scene}:{canon}"
        if once and sprung_key in state.sprung_hazards:
            return {"ok": False, "reason": "already_sprung", "error": f"{hazard_id!r} has already been sprung."}

        ability = str(entry.get("ability", "dex")).strip().lower()
        dc = int(entry.get("dc", 10))
        damage_dice = entry.get("damage", "")
        on_success = entry.get("on_success", "none")  # "none" | "half"

        # Affected characters: explicit list, or all conscious party members (area hazard).
        names = args.get("characters") or [c.name for c in state.party.values() if not c.is_down]
        targets = []
        for nm in names:
            actor = state.find_actor(nm)
            if actor is None:
                return {"ok": False, "reason": "unknown_target", "error": f"Unknown character {nm!r}; call get_state."}
            targets.append(actor)
        if not targets:
            return {"ok": False, "reason": "no_targets", "error": "No characters to affect."}

        # Roll the hazard's damage ONCE (shared across targets, like an area trap):
        # failers take it in full, successes take half (save-for-half) or none.
        dmg_total, dmg_detail = 0, ""
        if damage_dice:
            try:
                rolled = rules.roll(damage_dice)
            except ValueError as e:
                return {"ok": False, "error": f"Bad hazard damage {damage_dice!r}: {e}"}
            dmg_total, dmg_detail = rolled.total, rolled.describe()

        results = []
        for actor in targets:
            save = rules.saving_throw(actor, ability, dc)
            if save["success"]:
                dealt = dmg_total // 2 if on_success == "half" else 0
            else:
                dealt = dmg_total
            applied = rules.apply_damage(actor, dealt) if dealt else {"hp": actor.hp, "downed": actor.is_down}
            results.append({
                "character": actor.name,
                "save_roll": save["roll"],
                "save_total": save["total"],
                "success": save["success"],
                "damage": dealt,
                "hp": applied["hp"],
                "downed": applied.get("downed", actor.is_down),
            })

        if once:
            state.sprung_hazards.append(sprung_key)
        state.record(
            f"hazard {canon} sprung ({ability} DC {dc}"
            + (f", {damage_dice}={dmg_total}" if damage_dice else "") + "): "
            + "; ".join(f"{r['character']} {'saved' if r['success'] else 'failed'} took {r['damage']}" for r in results)
        )
        return {
            "ok": True,
            "hazard": entry.get("name", canon),
            "ability": ability,
            "dc": dc,
            "damage_roll": dmg_total,
            "damage_detail": dmg_detail,
            "damage_type": entry.get("damage_type"),
            "on_success": on_success,
            "results": results,
        }

    if name == "lookup_rule":
        return rules.lookup_rule(args["topic"])

    if name == "add_npc":
        instance_id = args.get("instance_id", "").strip()
        if not instance_id:
            return {
                "ok": False,
                "reason": "missing_instance_id",
                "error": "instance_id is required and cannot be empty.",
            }

        # The current scene's reinforcements manifest is the SOLE authority for
        # what may be spawned — the model can only trigger an author-declared
        # reinforcement, never conjure a monster (mirrors move_scene/exits and
        # take_item/loot). Stats, name, and template come from the manifest entry.
        scene_data = state.scenes.get(state.current_scene, {}) if state.current_scene else {}
        manifest: dict = scene_data.get("reinforcements", {})
        entry = next(
            (v for k, v in manifest.items() if k.lower() == instance_id.lower()),
            None,
        )
        if entry is None:
            available = ", ".join(_available_reinforcements(scene_data, state.quest_flags)) or "none"
            return {
                "ok": False,
                "reason": "not_declared",
                "error": (
                    f"{instance_id!r} is not a declared reinforcement for this scene. "
                    f"Available: {available}."
                ),
            }

        # Authored trigger: a reinforcement with a `requires` flag stays locked
        # until that flag is set (mirrors gated exits). It is also hidden from the
        # model's state snapshot until then, so a locked spawn means the model
        # named an id it should not yet see.
        req = entry.get("requires") if isinstance(entry, dict) else None
        if req and not state.quest_flags.get(req):
            return {
                "ok": False,
                "reason": "locked",
                "required_flag": req,
                "error": f"{instance_id!r} cannot arrive yet — its trigger has not fired.",
            }

        # Each reinforcement spawns once — the duplicate guard prevents farming.
        instance_id_lower = instance_id.lower()
        if any(k.lower() == instance_id_lower for k in {**state.npcs, **state.party}):
            return {
                "ok": False,
                "reason": "already_spawned",
                "error": f"instance_id {instance_id!r} already exists.",
            }

        # Build exactly like a scene NPC so template overrides (name, max_hp,
        # disposition_dc, alertness_dc, …) declared by the author are honored.
        # `requires` is an add_npc-manifest concept, not an NPC field — strip it.
        spawn_entry = {k: v for k, v in entry.items() if k != "requires"} if isinstance(entry, dict) else entry
        try:
            npc = expand_npc_entry(spawn_entry)
        except (KeyError, TypeError) as e:
            return {"ok": False, "reason": "bad_manifest",
                    "error": f"Reinforcement {instance_id!r} is misdeclared: {e}"}
        template = entry.get("template", "") if isinstance(entry, dict) else ""
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

    if name == "use_item":
        character = state.find_actor(args["character"])
        if not character:
            return {"ok": False, "error": "Unknown character; call get_state."}
        if state.combat_starting:
            return {"ok": False, "reason": "combat_starting",
                    "error": "Combat is starting this turn — wait for the initiative order before acting."}
        err = _turn_guard(character.name, state) or _action_guard(state)
        if err:
            return err

        item_key = args.get("item", "").strip().lower()
        inv_lower = [i.strip().lower() for i in character.inventory]

        if item_key not in inv_lower:
            state.action_used = False
            available = [i for i in character.inventory if i.strip().lower() in rules.CONSUMABLES]
            return {
                "ok": False,
                "reason": "not_in_inventory",
                "error": (
                    f"{character.name} does not have {item_key!r}."
                    + (f" Usable items in inventory: {available}." if available else "")
                ),
            }

        if item_key not in rules.CONSUMABLES:
            state.action_used = False
            return {
                "ok": False,
                "reason": "not_consumable",
                "error": f"{item_key!r} is not a consumable item.",
            }

        # Resolve the recipient: self by default, or a named party ally.
        recipient = character
        target_arg = (args.get("target") or "").strip()
        if target_arg:
            recipient = state.find_actor(target_arg)
            if recipient is None or recipient.name.lower() not in {c.name.lower() for c in state.party.values()}:
                state.action_used = False
                return {"ok": False, "reason": "unknown_target",
                        "error": f"{target_arg!r} is not a party member; items may only be given to allies."}

        res = rules.apply_consumable(recipient, item_key)
        if not res.get("ok"):
            # No effect (e.g. slots already full) — keep the turn alive and the item.
            state.action_used = False
            return res
        # Consume ONE instance from the USER's inventory (may hold duplicates).
        idx = inv_lower.index(item_key)
        character.inventory.pop(idx)
        if recipient is not character:
            res["recipient"] = recipient.name
            state.record(f"{character.name} uses {item_key} on {recipient.name}: {res.get('effect')} ({res})")
        else:
            state.record(f"{character.name} uses {item_key}: {res.get('effect')} ({res})")
        return res

    if name == "influence_npc":
        character = state.find_actor(args["character"])
        if not character:
            return {"ok": False, "error": "Unknown character; call get_state."}
        npc = state.find_actor(args["npc"])
        if not npc or not hasattr(npc, "hostile"):
            return {"ok": False, "error": f"Unknown NPC {args['npc']!r}; call get_state."}
        if state.combat_starting:
            return {"ok": False, "reason": "combat_starting",
                    "error": "Combat is starting this turn — wait for the initiative order before acting."}
        err = _turn_guard(character.name, state) or _action_guard(state)
        if err:
            return err
        if npc.is_down:
            state.action_used = False
            return {"ok": False, "reason": "target_down", "error": f"{npc.name} is already down."}
        if not npc.hostile:
            state.action_used = False
            return {"ok": False, "reason": "not_hostile", "error": f"{npc.name} is not hostile."}
        if npc.disposition_dc is None:
            state.action_used = False
            return {"ok": False, "reason": "immovable", "error": f"{npc.name} cannot be reasoned with."}
        if npc.social_attempted:
            state.action_used = False
            return {"ok": False, "reason": "already_attempted", "error": f"{npc.name} will not be moved further."}
        approach = args.get("approach", "persuade")
        res = rules.skill_check(character, "cha", npc.disposition_dc)
        npc.social_attempted = True
        if res["success"]:
            npc.hostile = False
        state.record(
            f"{character.name} {approach}s {npc.name} (DC {npc.disposition_dc}): "
            f"{'success' if res['success'] else 'failure'} — now_hostile={npc.hostile}"
        )
        result = {
            "ok": True,
            "character": character.name,
            "npc": npc.name,
            "approach": approach,
            "roll": res["roll"],
            "total": res["total"],
            "dc": res["dc"],
            "success": res["success"],
            "now_hostile": npc.hostile,
        }
        # Failed parley out of combat: auto-initiate a fight with everyone present.
        if not res["success"] and state.combat_round == 0:
            combatant_keys = (
                [k for k, c in state.party.items() if not c.is_down] +
                [k for k, n in state.npcs.items() if n.hostile and not n.is_down]
            )
            if combatant_keys:
                combat_res = dispatch("start_combat", {"combatants": combatant_keys}, state)
                result["combat_started"] = True
                result["combat_order"] = combat_res.get("combat_order")
                result["active"] = combat_res.get("active")
                result["active_name"] = combat_res.get("active_name")
                result["round"] = combat_res.get("round")
        return result

    if name == "recruit_npc":
        npc = state.find_actor(args.get("npc", ""))
        if not npc or not hasattr(npc, "hostile"):
            return {"ok": False, "error": f"Unknown NPC {args.get('npc')!r}; call get_state."}
        if state.combat_round > 0:
            return {"ok": False, "reason": "in_combat",
                    "error": "Recruit allies between fights, not mid-combat."}
        if npc.is_down:
            return {"ok": False, "reason": "target_down", "error": f"{npc.name} is down."}
        if npc.hostile:
            return {"ok": False, "reason": "hostile",
                    "error": f"{npc.name} is still hostile — win them over with influence_npc first."}
        if npc.companion:
            return {"ok": False, "reason": "already_companion",
                    "error": f"{npc.name} is already travelling with the party."}
        npc.companion = True
        state.record(f"{npc.name} recruited as a companion")
        return {"ok": True, "npc": npc.name, "companion": True}

    if name == "take_item":
        item_arg = args.get("item", "").strip().lower()
        if not item_arg:
            return {"ok": False, "error": "item is required."}

        # Scene loot is the sole authority — the model cannot conjure items.
        scene_data = state.scenes.get(state.current_scene, {}) if state.current_scene else {}
        loot_list: list = scene_data.get("loot", [])
        loot_lower = [i.strip().lower() for i in loot_list]
        if item_arg not in loot_lower:
            available = loot_list if loot_list else []
            return {
                "ok": False,
                "reason": "not_available",
                "error": (
                    f"{item_arg!r} is not in this scene's loot."
                    + (f" Available: {available}." if available else " The scene has no loot.")
                ),
            }

        # Resolve carrier.
        carrier_arg = (args.get("carrier") or "").strip()
        if carrier_arg:
            carrier = state.find_actor(carrier_arg)
            if not carrier or not hasattr(carrier, "proficiency_bonus"):
                return {"ok": False, "error": f"Unknown party member {carrier_arg!r}; call get_state."}
        else:
            pcs = list(state.party.values())
            if len(pcs) == 1:
                carrier = pcs[0]
            else:
                return {
                    "ok": False,
                    "reason": "ambiguous_carrier",
                    "error": "Multiple party members — name one with carrier.",
                    "candidates": [c.name for c in pcs],
                }

        # Remove ONE instance from scene loot (finite — cannot be farmed).
        idx = loot_lower.index(item_arg)
        original = loot_list.pop(idx)
        carrier.inventory.append(original)
        state.record(f"{carrier.name} takes {original} from scene {state.current_scene!r}")
        return {"ok": True, "item": original, "owner": carrier.name}

    return {"ok": False, "error": f"Unknown tool {name!r}"}
