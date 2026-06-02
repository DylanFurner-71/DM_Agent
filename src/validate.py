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
from .game_state import Character, NPC

_ABILITIES = {"str", "dex", "con", "int", "wis", "cha"}
_CHARACTER_FIELDS = {f.name for f in dataclasses.fields(Character)}
_NPC_FIELDS = {f.name for f in dataclasses.fields(NPC)}
_EXIT_KEYS = {"to", "requires", "requires_answer", "denied"}
_HAZARD_KEYS = {"name", "ability", "dc", "damage", "damage_type", "on_success",
                "once", "requires", "hidden"}


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

    if "requires" in entry and allow_requires:
        _check_gate_flag(rep, f"{where}.requires", entry["requires"])

    for item in entry.get("inventory", []) or []:
        if isinstance(item, str) and item.strip().lower() not in rules.WEAPONS:
            rep.warn(
                where,
                f"inventory item {item!r} is not in WEAPONS — if it was meant as a "
                f"weapon the NPC will fall back to an unarmed attack{_suggest(item.strip().lower(), rules.WEAPONS)}",
            )


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
        for slots_field in ("spell_slots", "max_spell_slots"):
            slots = member.get(slots_field, {})
            if isinstance(slots, dict):
                for lvl in slots:
                    if not str(lvl).lstrip("-").isdigit():
                        rep.error(where, f"{slots_field} level key {lvl!r} is not an integer — would crash on load")
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

    _check_party(rep, data.get("party"))

    scenes = data.get("scenes")
    if scenes is None:
        # Free-form scenario (no scene graph) — nothing more to graph-check.
        if "location" not in data:
            rep.warn("root", "no 'scenes' and no 'location' — the session starts with an empty location")
        return rep
    if not isinstance(scenes, dict) or not scenes:
        rep.error("scenes", "'scenes' must be a non-empty object")
        return rep

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
