"""Scenario validator: lint an authored scenario before play.

Usage:
    python -m src.validate data/scenario.json
    python -m src.validate data/*.json          # validate several at once

Catches authored-content bugs that would otherwise only surface mid-session:
exits that point at nonexistent scenes, NPC/reinforcement templates that aren't
in MONSTERS, hazard damage that isn't valid dice, gate flags that can never be
satisfied, and scene/party entries whose keys would crash the loader.

The engine is the source of truth, so every check is derived from how the engine
actually consumes the data (rules.MONSTERS/WEAPONS/SPELLS/CONSUMABLES,
tools._normalize_flag_key, game_state's dataclass fields) — not from a separate
schema that could drift. Issues are split into ERRORS (the scenario will misbehave
or fail to load) and WARNINGS (likely an authoring mistake, but the engine has a
fallback). Exit status is non-zero only when there are errors.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections import deque

from . import rules
from . import tools
from .game_state import Character, GameState, NPC

_ABILITIES = {"str", "dex", "con", "int", "wis", "cha"}
_CHARACTER_FIELDS = {f.name for f in dataclasses.fields(Character)}
_NPC_FIELDS = {f.name for f in dataclasses.fields(NPC)}
_EXIT_KEYS = {"to", "requires", "requires_answer", "denied"}
_HAZARD_KEYS = {"name", "ability", "dc", "damage", "damage_type", "on_success",
                "once", "requires", "hidden"}
# Scene-level keys the engine actually reads (move_scene, get_state, _available_*).
# A scene dict is consumed via .get(), so an unknown key is silently ignored rather
# than crashing — a typo like 'hazardz' would quietly disable an intended manifest,
# which is exactly what the unknown-key warning catches everywhere else.
_SCENE_KEYS = {"location", "scene", "npcs", "loot", "exits", "hazards",
               "reinforcements", "exit_requires", "exit_denied"}
# Top-level scenario keys the loader/saver knows. Derived from GameState.to_dict so it
# tracks the real schema (savegame fields included) and can't drift. `title`/`blurb`
# are author metadata read only by the start menu — the loader ignores them — so add
# them explicitly here.
_TOP_LEVEL_KEYS = set(GameState().to_dict()) | {"title", "blurb"}
# Known fields whose VALUES the dataclasses don't type-check: a wrong type loads fine
# via Character(**v)/NPC(**v) and only crashes mid-session, so the validator does.
_CHARACTER_INT_FIELDS = ("level", "max_hp", "hp", "ac", "attack_bonus", "proficiency_bonus",
                         "death_save_successes", "death_save_failures", "inspiration", "gold")
_CHARACTER_LIST_FIELDS = ("inventory", "conditions", "spells", "save_proficiencies",
                          "skill_proficiencies", "expertise")
_NPC_INT_FIELDS = ("max_hp", "hp", "ac", "attack_bonus")
_NPC_NULLABLE_INT_FIELDS = ("disposition_dc", "alertness_dc")  # int or null


class Report:
    """Accumulates validation issues for one scenario, tagged by location."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, where: str, msg: str) -> None:
        self.errors.append(f"[{where}] {msg}")

    def warn(self, where: str, msg: str) -> None:
        self.warnings.append(f"[{where}] {msg}")

    @property
    def ok(self) -> bool:
        return not self.errors


def _is_valid_dice(notation) -> bool:
    """True if `notation` is dice rules.roll() would accept (same bounds, no roll)."""
    m = rules._DICE_RE.match(str(notation))
    if not m:
        return False
    count = int(m.group(1)) if m.group(1) else 1
    sides = int(m.group(2))
    return 1 <= count <= 100 and 2 <= sides <= 1000


def _suggest(name: str, options) -> str:
    """Return a ' (did you mean 'x'?)' hint when a close match exists, else ''."""
    import difflib
    match = difflib.get_close_matches(str(name), list(options), n=1, cutoff=0.7)
    return f" (did you mean {match[0]!r}?)" if match else ""


def _is_int(v) -> bool:
    """True for a real integer — JSON booleans are ints in Python, so exclude them."""
    return isinstance(v, int) and not isinstance(v, bool)


def _check_ability_modifiers(rep: Report, where: str, am) -> None:
    """ability_modifiers must be an {ability: int} map; a non-int value breaks roll math."""
    if am is None:
        return
    if not isinstance(am, dict):
        rep.error(where, f"ability_modifiers must be an object, got {type(am).__name__}")
        return
    for ab, val in am.items():
        if isinstance(ab, str) and ab.strip().lower() not in _ABILITIES:
            rep.warn(where, f"ability_modifiers key {ab!r} is not a known ability {sorted(_ABILITIES)} — it is ignored")
        if not _is_int(val):
            rep.error(where, f"ability_modifiers[{ab!r}] must be an integer, got {val!r}")


def _check_hp_ac_sanity(rep: Report, where: str, hp, max_hp, ac) -> None:
    """Flag impossible HP/AC values (warnings — they load, but make no sense at play)."""
    if _is_int(max_hp) and max_hp < 1:
        rep.warn(where, f"max_hp is {max_hp} — a creature with no positive max HP starts down")
    if _is_int(hp) and hp < 0:
        rep.warn(where, f"hp is {hp} — negative HP; the engine treats <= 0 as down")
    if _is_int(hp) and _is_int(max_hp) and hp > max_hp:
        rep.warn(where, f"hp {hp} exceeds max_hp {max_hp}")
    if _is_int(ac) and ac < 1:
        rep.warn(where, f"ac is {ac} — unusually low; nearly every attack will hit")


def _effective_name(entry) -> str | None:
    """The name find_actor will see for an NPC entry: the explicit name, else the
    template's default name (so two bare {'template': 'goblin'} entries collide)."""
    if not isinstance(entry, dict):
        return None
    if isinstance(entry.get("name"), str) and entry["name"].strip():
        return entry["name"]
    template = rules.MONSTERS.get(entry.get("template", ""))
    return template["name"] if template else None


def _check_name_collisions(rep: Report, party, scenes) -> None:
    """find_actor matches case-insensitively by name across party + the active scene's
    NPCs, returning the first match — so a shared name makes later actors untargetable.
    Collisions only matter among actors that can be present at once (party + one scene +
    its reinforcements), not across different scenes.
    """
    party_names: dict[str, list[str]] = {}
    if isinstance(party, dict):
        for k, m in party.items():
            name = m.get("name") if isinstance(m, dict) else None
            if isinstance(name, str) and name.strip():
                party_names.setdefault(name.strip().lower(), []).append(f"party.{k}")
    for low, locs in party_names.items():
        if len(locs) > 1:
            rep.warn("party", f"name {low!r} is shared by {', '.join(locs)} — find_actor can't tell them apart")

    if not isinstance(scenes, dict):
        return
    for skey, scene in scenes.items():
        if not isinstance(scene, dict):
            continue
        seen = {low: locs[0] for low, locs in party_names.items()}  # party is present in every scene
        for group in ("npcs", "reinforcements"):
            entries = scene.get(group) or {}
            if not isinstance(entries, dict):
                continue
            for nkey, entry in entries.items():
                name = _effective_name(entry)
                if not isinstance(name, str):
                    continue
                low = name.strip().lower()
                loc = f"scenes.{skey}.{group}.{nkey}"
                if low in seen:
                    rep.warn(loc, f"name {name!r} collides with {seen[low]} — find_actor matches by name and can't target both")
                else:
                    seen[low] = loc


def _check_gate_flag(rep: Report, where: str, flag) -> None:
    """A gating flag must be settable: non-empty, already in normalized form, and
    not a reserved engine key. move_scene/add_npc/trigger_hazard test the flag
    name *as written*, but set_quest_flag only ever stores the normalized key — so
    a non-normalized `requires` names a flag that can never be set."""
    if not isinstance(flag, str) or not flag.strip():
        rep.error(where, f"gate flag must be a non-empty string, got {flag!r}")
        return
    normalized = tools._normalize_flag_key(flag)
    if normalized != flag:
        rep.error(
            where,
            f"gate flag {flag!r} is not in normalized form — set_quest_flag would "
            f"store it as {normalized!r}, so this gate can never open. Use {normalized!r}.",
        )
    if flag in tools._RESERVED_FLAG_KEYS:
        rep.error(where, f"gate flag {flag!r} is a reserved engine key and cannot be set")


def _check_npc_entry(rep: Report, where: str, entry: dict, *, allow_requires: bool) -> None:
    """Validate a scene NPC or reinforcement spec the way expand_npc_entry consumes it.

    Template entries must name a real MONSTERS template; inline entries define an
    NPC directly. Either way an unrecognized key would make NPC(**kwargs) raise at
    load/spawn, so unknown keys are errors. `requires` is allowed only for
    reinforcements (add_npc strips it before expanding).
    """
    if not isinstance(entry, dict):
        rep.error(where, f"must be an object, got {type(entry).__name__}")
        return

    allowed = set(_NPC_FIELDS)
    if allow_requires:
        allowed.add("requires")

    if "template" in entry:
        allowed.add("template")
        template = entry["template"]
        if template not in rules.MONSTERS:
            rep.error(where, f"template {template!r} is not in MONSTERS{_suggest(template, rules.MONSTERS)}")
    elif "name" not in entry:
        rep.error(where, "inline NPC needs a 'name' (or a 'template')")

    for key in entry:
        if key not in allowed:
            rep.error(where, f"unknown NPC field {key!r} — would crash on load{_suggest(key, allowed)}")

    if "name" in entry and not isinstance(entry["name"], str):
        rep.error(where, f"name must be a string, got {entry['name']!r}")
    for f in _NPC_INT_FIELDS:
        if f in entry and not _is_int(entry[f]):
            rep.error(where, f"{f} must be an integer, got {entry[f]!r} — crashes the engine math at play")
    for f in _NPC_NULLABLE_INT_FIELDS:
        if f in entry and entry[f] is not None and not _is_int(entry[f]):
            rep.error(where, f"{f} must be an integer or null, got {entry[f]!r}")
    _check_ability_modifiers(rep, where, entry.get("ability_modifiers"))
    # Resolve the effective max HP (a template supplies it when not overridden) so an
    # hp override that exceeds it is caught even for template NPCs.
    template = rules.MONSTERS.get(entry.get("template", "")) if "template" in entry else None
    eff_max = entry["max_hp"] if "max_hp" in entry else (template.get("max_hp") if template else None)
    _check_hp_ac_sanity(rep, where, entry.get("hp"), eff_max, entry.get("ac"))

    if "requires" in entry and allow_requires:
        _check_gate_flag(rep, f"{where}.requires", entry["requires"])

    inventory = entry.get("inventory", [])
    if "inventory" in entry and not isinstance(inventory, list):
        rep.error(where, f"inventory must be a list, got {type(inventory).__name__}")
    elif isinstance(inventory, list):
        for item in inventory:
            if isinstance(item, str) and item.strip().lower() not in rules.WEAPONS:
                rep.warn(
                    where,
                    f"inventory item {item!r} is not in WEAPONS — if it was meant as a "
                    f"weapon the NPC will fall back to an unarmed attack{_suggest(item.strip().lower(), rules.WEAPONS)}",
                )

    shop = entry.get("shop")
    if shop is not None:
        if not isinstance(shop, dict):
            rep.error(where, f"shop must be an object of item->price, got {type(shop).__name__}")
        else:
            for item_id, price in shop.items():
                if not _is_int(price) or price < 1:
                    rep.error(where, f"shop price for {item_id!r} must be a positive integer, got {price!r}")
                norm = str(item_id).strip().lower()
                if norm not in rules.WEAPONS and norm not in rules.CONSUMABLES:
                    rep.warn(where, f"shop item {item_id!r} is not a known weapon or consumable — sellable but mechanically inert{_suggest(norm, list(rules.WEAPONS) + list(rules.CONSUMABLES))}")


def _check_party(rep: Report, party) -> None:
    if not isinstance(party, dict) or not party:
        rep.error("party", "a scenario needs a non-empty 'party' object")
        return
    for key, member in party.items():
        where = f"party.{key}"
        if not isinstance(member, dict):
            rep.error(where, f"must be an object, got {type(member).__name__}")
            continue
        if "name" not in member:
            rep.error(where, "missing required 'name' — would crash on load")
        for k in member:
            if k not in _CHARACTER_FIELDS:
                rep.error(where, f"unknown character field {k!r} — would crash on load{_suggest(k, _CHARACTER_FIELDS)}")
        if "name" in member and not isinstance(member["name"], str):
            rep.error(where, f"name must be a string, got {member['name']!r}")
        for f in _CHARACTER_INT_FIELDS:
            if f in member and not _is_int(member[f]):
                rep.error(where, f"{f} must be an integer, got {member[f]!r} — crashes the engine math at play")
        for f in _CHARACTER_LIST_FIELDS:
            if f in member and not isinstance(member[f], list):
                rep.error(where, f"{f} must be a list, got {type(member[f]).__name__}")
        _check_ability_modifiers(rep, where, member.get("ability_modifiers"))
        _check_hp_ac_sanity(rep, where, member.get("hp"), member.get("max_hp"), member.get("ac"))
        if _is_int(member.get("gold")) and member["gold"] < 0:
            rep.warn(where, f"gold is {member['gold']} — negative starting gold")
        for slots_field in ("spell_slots", "max_spell_slots"):
            slots = member.get(slots_field, {})
            if not isinstance(slots, dict):
                if slots_field in member:
                    rep.error(where, f"{slots_field} must be an object of level->count, got {type(slots).__name__}")
                continue
            for lvl, count in slots.items():
                if not str(lvl).lstrip("-").isdigit():
                    rep.error(where, f"{slots_field} level key {lvl!r} is not an integer — would crash on load")
                if not _is_int(count):
                    rep.error(where, f"{slots_field}[{lvl!r}] count must be an integer, got {count!r}")
        ability = member.get("spellcasting_ability", "")
        spells = member.get("spells", []) or []
        if ability and ability not in _ABILITIES:
            rep.warn(where, f"spellcasting_ability {ability!r} is not a known ability {sorted(_ABILITIES)}")
        if spells and not ability:
            rep.warn(where, "has spells but no spellcasting_ability — spell-attack spells use a +0 modifier")
        for spell in spells:
            if isinstance(spell, str) and spell not in rules.SPELLS:
                rep.warn(where, f"spell {spell!r} is not in rules.SPELLS — it can be cast but deals no engine damage{_suggest(spell, rules.SPELLS)}")
        for save in member.get("save_proficiencies", []) or []:
            if isinstance(save, str) and save.strip().lower() not in _ABILITIES:
                rep.warn(where, f"save_proficiencies entry {save!r} is not a known ability")
        for field_name in ("skill_proficiencies", "expertise"):
            for skill in member.get(field_name, []) or []:
                if isinstance(skill, str) and rules._normalize_skill(skill) not in rules.SKILLS:
                    rep.warn(where, f"{field_name} entry {skill!r} is not a known skill{_suggest(rules._normalize_skill(skill), rules.SKILLS)} — it adds no proficiency")


def _check_exits(rep: Report, scene_key: str, scene: dict, scene_keys: set) -> None:
    exits = scene.get("exits", {})
    if not isinstance(exits, dict):
        rep.error(f"scenes.{scene_key}.exits", f"must be an object, got {type(exits).__name__}")
        return
    for label, val in exits.items():
        where = f"scenes.{scene_key}.exits[{label!r}]"
        if isinstance(val, str):
            target = val
        elif isinstance(val, dict):
            if "to" not in val:
                rep.error(where, "gated exit must have a 'to' destination")
                continue
            target = val["to"]
            for k in val:
                if k not in _EXIT_KEYS:
                    rep.warn(where, f"unknown exit field {k!r}{_suggest(k, _EXIT_KEYS)}")
            if "requires" in val:
                _check_gate_flag(rep, f"{where}.requires", val["requires"])
                if "denied" not in val:
                    rep.warn(where, "gated exit has no 'denied' text — the engine will use a generic refusal")
            if "requires_answer" in val:
                ans = val["requires_answer"]
                if not isinstance(ans, str) or not ans.strip():
                    rep.error(where, "requires_answer must be a non-empty password string")
                if "denied" not in val:
                    rep.warn(where, "answer-gated exit has no 'denied' text — the engine will use a generic refusal")
        else:
            rep.error(where, f"exit must be a scene_key string or a gated object, got {type(val).__name__}")
            continue
        if target not in scene_keys:
            rep.error(where, f"exit target {target!r} is not a defined scene{_suggest(target, scene_keys)}")


def _check_hazards(rep: Report, scene_key: str, scene: dict) -> None:
    hazards = scene.get("hazards", {})
    if not isinstance(hazards, dict):
        rep.error(f"scenes.{scene_key}.hazards", f"must be an object, got {type(hazards).__name__}")
        return
    for hid, entry in hazards.items():
        where = f"scenes.{scene_key}.hazards.{hid}"
        if not isinstance(entry, dict):
            rep.error(where, f"must be an object, got {type(entry).__name__}")
            continue
        for k in entry:
            if k not in _HAZARD_KEYS:
                rep.warn(where, f"unknown hazard field {k!r}{_suggest(k, _HAZARD_KEYS)}")
        ability = str(entry.get("ability", "")).strip().lower()
        if ability not in _ABILITIES:
            rep.error(where, f"ability {entry.get('ability')!r} is not one of {sorted(_ABILITIES)}")
        if "dc" not in entry:
            rep.warn(where, "no 'dc' — the engine defaults to 10")
        elif not isinstance(entry["dc"], int) or isinstance(entry["dc"], bool):
            rep.error(where, f"dc must be an integer, got {entry['dc']!r}")
        damage = entry.get("damage")
        if damage is None:
            rep.warn(where, "no 'damage' — the hazard will deal nothing on a failed save")
        elif not _is_valid_dice(damage):
            rep.error(where, f"damage {damage!r} is not valid dice notation — trigger_hazard would fail at play")
        on_success = entry.get("on_success")
        if on_success is not None and on_success not in ("none", "half"):
            rep.warn(where, f"on_success {on_success!r} is not 'none' or 'half' — treated as 'none'")
        if "requires" in entry:
            _check_gate_flag(rep, f"{where}.requires", entry["requires"])


def _check_reinforcements(rep: Report, scene_key: str, scene: dict) -> None:
    reinf = scene.get("reinforcements", {})
    if not isinstance(reinf, dict):
        rep.error(f"scenes.{scene_key}.reinforcements", f"must be an object, got {type(reinf).__name__}")
        return
    for rid, entry in reinf.items():
        _check_npc_entry(rep, f"scenes.{scene_key}.reinforcements.{rid}", entry, allow_requires=True)


def _check_reachability(rep: Report, current: str, scenes: dict) -> None:
    """Warn about scenes unreachable from current_scene via declared exits."""
    seen = {current}
    queue = deque([current])
    while queue:
        node = scenes.get(queue.popleft(), {})
        for val in node.get("exits", {}).values():
            target = tools._target(val)
            if target in scenes and target not in seen:
                seen.add(target)
                queue.append(target)
    for key in scenes:
        if key not in seen:
            rep.warn(f"scenes.{key}", "scene is unreachable from current_scene via any declared exit")


def validate_scenario(data: dict) -> Report:
    """Validate a parsed scenario dict and return a Report. Pure — no I/O."""
    rep = Report()
    if not isinstance(data, dict):
        rep.error("root", f"scenario must be a JSON object, got {type(data).__name__}")
        return rep

    for key in data:
        if key not in _TOP_LEVEL_KEYS:
            rep.warn("root", f"unknown top-level key {key!r} — ignored by the loader{_suggest(key, _TOP_LEVEL_KEYS)}")

    _check_party(rep, data.get("party"))
    _check_name_collisions(rep, data.get("party"), data.get("scenes"))

    scenes = data.get("scenes")
    if scenes is None:
        # Free-form scenario (no scene graph) — nothing more to graph-check.
        if "location" not in data:
            rep.warn("root", "no 'scenes' and no 'location' — the session starts with an empty location")
        return rep
    if not isinstance(scenes, dict) or not scenes:
        rep.error("scenes", "'scenes' must be a non-empty object")
        return rep

    # A scene-based scenario is a menu-facing adventure, so it must carry the
    # start-menu metadata. (Free-form scenarios returned above are exempt — they have
    # no menu presence.)
    for field in ("title", "blurb"):
        val = data.get(field)
        if not isinstance(val, str) or not val.strip():
            rep.error("root", f"missing required {field!r} — every scene-based scenario "
                              f"must declare a non-empty {field} (shown in the start menu)")

    scene_keys = set(scenes)
    current = data.get("current_scene")
    if not current:
        rep.error("current_scene", "missing — the engine won't know which scene to start in")
    elif current not in scene_keys:
        rep.error("current_scene", f"{current!r} is not a defined scene{_suggest(current, scene_keys)}")

    for scene_key, scene in scenes.items():
        where = f"scenes.{scene_key}"
        if not isinstance(scene, dict):
            rep.error(where, f"must be an object, got {type(scene).__name__}")
            continue
        for k in scene:
            if k not in _SCENE_KEYS:
                rep.warn(where, f"unknown scene field {k!r} — ignored by the engine{_suggest(k, _SCENE_KEYS)}")
        if not scene.get("location"):
            rep.warn(where, "no 'location' name")
        if not scene.get("scene"):
            rep.warn(where, "no 'scene' description text")

        npcs = scene.get("npcs", {})
        if not isinstance(npcs, dict):
            rep.error(f"{where}.npcs", f"must be an object, got {type(npcs).__name__}")
        else:
            for nkey, entry in npcs.items():
                _check_npc_entry(rep, f"{where}.npcs.{nkey}", entry, allow_requires=False)

        loot = scene.get("loot", [])
        if not isinstance(loot, list):
            rep.error(f"{where}.loot", f"must be a list, got {type(loot).__name__}")
        else:
            for item in loot:
                if not isinstance(item, str) or not item.strip():
                    rep.error(f"{where}.loot", f"loot entries must be non-empty strings, got {item!r}")
                    continue
                # Narrative quest items are free-form and fine; only flag a near-miss
                # where a consumable/weapon won't resolve at play due to formatting.
                norm = item.strip().lower().replace(" ", "_")
                known = item.strip().lower() in rules.CONSUMABLES or item.strip().lower() in rules.WEAPONS
                near = norm in rules.CONSUMABLES or norm in rules.WEAPONS
                if not known and near:
                    rep.warn(f"{where}.loot", f"loot id {item!r} won't resolve at play; did you mean {norm!r}?")

        _check_exits(rep, scene_key, scene, scene_keys)
        _check_hazards(rep, scene_key, scene)
        _check_reinforcements(rep, scene_key, scene)

        exit_req = scene.get("exit_requires")
        if exit_req is not None:
            _check_gate_flag(rep, f"{where}.exit_requires", exit_req)
            if scene.get("exits"):
                rep.warn(where, "exit_requires is set but the scene has exits — the terminal-ending gate only applies to exitless scenes")
            if not scene.get("exit_denied"):
                rep.warn(where, "exit_requires is set but there is no 'exit_denied' text")

    if current in scene_keys:
        _check_reachability(rep, current, scenes)

    return rep


def _print_report(path: str, rep: Report) -> None:
    print(f"\n{path}")
    for e in rep.errors:
        print(f"  ERROR   {e}")
    for w in rep.warnings:
        print(f"  WARNING {w}")
    ne, nw = len(rep.errors), len(rep.warnings)
    mark = "✓" if rep.ok else "✗"
    print(f"  {mark} {ne} error{'s' * (ne != 1)}, {nw} warning{'s' * (nw != 1)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.validate",
        description="Lint an authored scenario JSON before play.",
    )
    parser.add_argument("paths", nargs="+", help="scenario JSON file(s) to validate")
    args = parser.parse_args(argv)

    any_errors = False
    for path in args.paths:
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            print(f"\n{path}\n  ERROR   file not found")
            any_errors = True
            continue
        except json.JSONDecodeError as e:
            print(f"\n{path}\n  ERROR   invalid JSON: {e}")
            any_errors = True
            continue
        rep = validate_scenario(data)
        _print_report(path, rep)
        any_errors = any_errors or not rep.ok

    print()
    return 1 if any_errors else 0


if __name__ == "__main__":
    sys.exit(main())
