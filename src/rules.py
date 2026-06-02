"""Deterministic game mechanics. This is the enforcement core.

Every function here returns a structured result and *refuses illegal actions in
code*. The DM model cannot talk its way past these: if a character is out of
spell slots, ``cast_spell`` returns ``ok=False`` and the model has to narrate
around the failure. That is the demo's money shot.

A simplified subset of the D&D 5e SRD (CC-BY-4.0) is used for mechanics. Swap in
more of the SRD as you flesh the project out.
"""

from __future__ import annotations

import random
import re
from collections import deque
from dataclasses import dataclass

# Module-level RNG so tests can seed it deterministically.
_rng = random.Random()
_forced: deque[int] = deque()


def force_rolls(values: list[int]) -> None:
    """Queue exact roll values consumed one-per-die before falling back to _rng (tests only)."""
    _forced.clear()
    _forced.extend(values)


def seed(value: int) -> None:
    """Seed the dice RNG (used by tests for reproducible rolls)."""
    _rng.seed(value)


_DICE_RE = re.compile(r"^\s*(\d*)d(\d+)\s*([+-]\s*\d+)?\s*$", re.IGNORECASE)


@dataclass
class RollResult:
    notation: str
    rolls: list[int]
    modifier: int
    total: int

    def describe(self) -> str:
        mod = f" {'+' if self.modifier >= 0 else '-'} {abs(self.modifier)}" if self.modifier else ""
        return f"{self.notation} -> {self.rolls}{mod} = {self.total}"


def roll(notation: str) -> RollResult:
    """Roll standard dice notation like '2d6+3', '1d20', 'd8-1'."""
    m = _DICE_RE.match(notation)
    if not m:
        raise ValueError(f"Bad dice notation: {notation!r}")
    count = int(m.group(1)) if m.group(1) else 1
    sides = int(m.group(2))
    modifier = int(m.group(3).replace(" ", "")) if m.group(3) else 0
    if count < 1 or count > 100 or sides < 2 or sides > 1000:
        raise ValueError(f"Unreasonable dice: {notation!r}")
    rolls = [_forced.popleft() if _forced else _rng.randint(1, sides) for _ in range(count)]
    return RollResult(notation, rolls, modifier, sum(rolls) + modifier)


def cast_spell(caster, spell_level: int) -> dict:
    """Enforce spell-slot economy. Returns a structured result; never raises on
    a legal-but-failed cast so the DM can narrate the outcome."""
    if spell_level == 0:  # cantrips are free
        return {"ok": True, "reason": "cantrip", "slots_remaining": "n/a"}
    available = caster.spell_slots.get(spell_level, 0)
    if available <= 0:
        return {
            "ok": False,
            "reason": f"{caster.name} has no level-{spell_level} spell slots remaining",
            "slots_remaining": 0,
        }
    caster.spell_slots[spell_level] = available - 1
    return {"ok": True, "reason": "slot expended", "slots_remaining": available - 1}


def _d20(advantage: bool = False, disadvantage: bool = False) -> tuple[int, list[int]]:
    """Roll a d20, or 2d20 keeping the higher (advantage) / lower (disadvantage).

    Returns (kept_nat, all_rolls). Goes through roll('1d20') so force_rolls/seed still
    drive the dice deterministically. Per the SRD, advantage and disadvantage cancel:
    if both are set the roll is a single straight d20.
    """
    if advantage and disadvantage:
        nat = roll("1d20").rolls[0]
        return nat, [nat]
    if advantage:
        a = roll("1d20").rolls[0]
        b = roll("1d20").rolls[0]
        return max(a, b), [a, b]
    if disadvantage:
        a = roll("1d20").rolls[0]
        b = roll("1d20").rolls[0]
        return min(a, b), [a, b]
    nat = roll("1d20").rolls[0]
    return nat, [nat]


def award_inspiration(character, cap: int = 1) -> dict:
    """Grant a character their single session reroll (inspiration). Engine-owned budget.

    Mirrors the spell-slot / Pearl-of-Power refusal style — never raises, returns a
    structured result. A PC holds at most ``cap`` (1) inspiration AND may only ever be
    awarded one per session: once spent (inspiration_used) it can never be re-awarded.
    The DM *decides* when to award (a soft, discretionary judgment); the cap and the dice
    stay in the engine, so the model can never manufacture a result.
    """
    if getattr(character, "inspiration_used", False):
        return {
            "ok": False,
            "reason": "already_used",
            "character": character.name,
            "error": f"{character.name} has already used their inspiration this session.",
        }
    current = getattr(character, "inspiration", 0)
    if current >= cap:
        return {
            "ok": False,
            "reason": "at_cap",
            "character": character.name,
            "inspiration": current,
            "error": f"{character.name} already holds inspiration.",
        }
    character.inspiration = 1
    return {"ok": True, "character": character.name, "inspiration": 1}


def _mark_dead(target) -> None:
    """Transition a PC to dead and keep condition tags consistent.

    A corpse is not 'unconscious' — drop that tag (added when first downed) and
    tag 'dead' so every consumer (/state, the model snapshot, get_state) agrees.
    """
    target.dead = True
    if hasattr(target, "conditions"):
        if "unconscious" in target.conditions:
            target.conditions.remove("unconscious")
        if "dead" not in target.conditions:
            target.conditions.append("dead")


def apply_damage(target, amount: int, from_crit: bool = False) -> dict:
    amount = max(0, int(amount))
    was_down = target.hp <= 0
    target.hp = max(0, target.hp - amount)
    downed = target.hp <= 0
    result = {"ok": True, "target": target.name, "damage": amount, "hp": target.hp, "downed": downed}

    is_pc = hasattr(target, "death_save_failures")
    if is_pc and not target.dead:
        if was_down:
            # Damage while already at 0 HP: add failure(s), possibly kill.
            if target.stable:
                target.stable = False  # re-enters dying
            if amount >= target.max_hp:
                _mark_dead(target)  # massive damage: instant death
            else:
                target.death_save_failures = min(3, target.death_save_failures + (2 if from_crit else 1))
                if target.death_save_failures >= 3:
                    _mark_dead(target)
            result.update({
                "death_save_failure": True,
                "death_save_failures": target.death_save_failures,
                "dead": target.dead,
            })
        elif downed:
            # Conscious → 0 HP: start dying, no failure.
            if "unconscious" not in target.conditions:
                target.conditions.append("unconscious")
    elif not is_pc:
        # NPC: original unconscious-on-down behavior.
        if downed and "unconscious" not in getattr(target, "conditions", []):
            if hasattr(target, "conditions"):
                target.conditions.append("unconscious")
    # Dead PC: no additional action.

    return result


def heal(target, amount: int) -> dict:
    amount = max(0, int(amount))
    # Dead PCs cannot be revived by healing.
    if hasattr(target, "death_save_failures") and target.dead:
        return {"ok": True, "target": target.name, "healed": 0, "hp": 0, "note": "cannot heal the dead"}
    hp_before = target.hp
    target.hp = min(target.max_hp, target.hp + amount)
    if target.hp > 0 and hasattr(target, "conditions") and "unconscious" in target.conditions:
        target.conditions.remove("unconscious")
    if target.hp > 0 and hasattr(target, "death_save_failures"):
        target.death_save_successes = 0
        target.death_save_failures = 0
        target.stable = False
    # Report the HP actually restored, not the raw roll — the max-HP cap may clip it.
    return {"ok": True, "target": target.name, "healed": target.hp - hp_before, "hp": target.hp}


def roll_death_save(character) -> dict:
    """Roll a death saving throw for a dying PC (hp <= 0, not dead, not stable).

    Nat 20: revive at 1 HP, reset all counters.
    Nat 1:  two failures.
    2-9:    one failure.
    10-19:  one success.
    3 successes -> stable.
    3 failures  -> dead.
    """
    if not (hasattr(character, "death_save_failures") and character.is_dying):
        return {"ok": False, "reason": "not_dying", "character": character.name}

    nat = roll("1d20").rolls[0]

    if nat == 20:
        character.hp = 1
        character.death_save_successes = 0
        character.death_save_failures = 0
        character.stable = False
        character.dead = False
        if hasattr(character, "conditions") and "unconscious" in character.conditions:
            character.conditions.remove("unconscious")
        return {
            "ok": True,
            "character": character.name,
            "roll": nat,
            "result_kind": "revived",
            "successes": 0,
            "failures": 0,
            "hp": 1,
        }

    if nat == 1:
        character.death_save_failures = min(3, character.death_save_failures + 2)
    elif nat <= 9:
        character.death_save_failures = min(3, character.death_save_failures + 1)
    else:  # 10-19
        character.death_save_successes = min(3, character.death_save_successes + 1)

    if character.death_save_successes >= 3:
        character.stable = True
        character.death_save_successes = 0
        character.death_save_failures = 0
        result_kind = "stabilized"
    elif character.death_save_failures >= 3:
        _mark_dead(character)
        result_kind = "dead"
    elif nat <= 9:
        result_kind = "failure"
    else:
        result_kind = "success"

    return {
        "ok": True,
        "character": character.name,
        "roll": nat,
        "result_kind": result_kind,
        "successes": character.death_save_successes,
        "failures": character.death_save_failures,
        "hp": character.hp,
    }


# Canonical weapon table: damage die, damage type, optional finesse flag.
# Finesse weapons may use the higher of Str or Dex for damage.
# 5e SRD weapon table. The engine models damage die, damage type, and the finesse
# property (Str-or-Dex for the modifier). Other 5e properties — versatile, reach,
# thrown, two-handed, ammunition, loading — are not modelled; versatile weapons list
# their one-handed die. `dice` must be valid notation for rules.roll().
WEAPONS: dict[str, dict] = {
    # --- simple melee ---
    "club":           {"dice": "1d4",  "type": "bludgeoning"},
    "dagger":         {"dice": "1d4",  "type": "piercing",     "finesse": True},
    "greatclub":      {"dice": "1d8",  "type": "bludgeoning"},
    "handaxe":        {"dice": "1d6",  "type": "slashing"},
    "javelin":        {"dice": "1d6",  "type": "piercing"},
    "light hammer":   {"dice": "1d4",  "type": "bludgeoning"},
    "mace":           {"dice": "1d6",  "type": "bludgeoning"},
    "quarterstaff":   {"dice": "1d6",  "type": "bludgeoning"},
    "sickle":         {"dice": "1d4",  "type": "slashing"},
    "spear":          {"dice": "1d6",  "type": "piercing"},
    # --- simple ranged ---
    "dart":           {"dice": "1d4",  "type": "piercing",     "finesse": True},
    "light crossbow": {"dice": "1d8",  "type": "piercing"},
    "shortbow":       {"dice": "1d6",  "type": "piercing"},
    "sling":          {"dice": "1d4",  "type": "bludgeoning"},
    # --- martial melee ---
    "battleaxe":      {"dice": "1d8",  "type": "slashing"},
    "flail":          {"dice": "1d8",  "type": "bludgeoning"},
    "glaive":         {"dice": "1d10", "type": "slashing"},
    "greataxe":       {"dice": "1d12", "type": "slashing"},
    "greatsword":     {"dice": "2d6",  "type": "slashing"},
    "halberd":        {"dice": "1d10", "type": "slashing"},
    "lance":          {"dice": "1d12", "type": "piercing"},
    "longsword":      {"dice": "1d8",  "type": "slashing"},
    "maul":           {"dice": "2d6",  "type": "bludgeoning"},
    "morningstar":    {"dice": "1d8",  "type": "piercing"},
    "pike":           {"dice": "1d10", "type": "piercing"},
    "rapier":         {"dice": "1d8",  "type": "piercing",     "finesse": True},
    "scimitar":       {"dice": "1d6",  "type": "slashing",     "finesse": True},
    "shortsword":     {"dice": "1d6",  "type": "piercing",     "finesse": True},
    "trident":        {"dice": "1d6",  "type": "piercing"},
    "war pick":       {"dice": "1d8",  "type": "piercing"},
    "warhammer":      {"dice": "1d8",  "type": "bludgeoning"},
    "whip":           {"dice": "1d4",  "type": "slashing",     "finesse": True},
    # --- martial ranged ---
    "hand crossbow":  {"dice": "1d6",  "type": "piercing"},
    "heavy crossbow": {"dice": "1d10", "type": "piercing"},
    "longbow":        {"dice": "1d8",  "type": "piercing"},
}


def _weapon_modifier(character, finesse: bool) -> tuple[str, int]:
    """Return (ability_name, modifier) to add to damage rolls.

    Finesse weapons use whichever of Str or Dex is higher; all others use Str.
    Missing modifiers default to 0.
    """
    mods = getattr(character, "ability_modifiers", {})
    str_mod = mods.get("str", 0)
    dex_mod = mods.get("dex", 0)
    if finesse and dex_mod > str_mod:
        return "dex", dex_mod
    return "str", str_mod


def attack(attacker, defender, weapon: str | None = None,
           advantage: bool = False, disadvantage: bool = False) -> dict:
    """Resolve a single attack: d20 + bonus vs AC, then damage on a hit.

    advantage/disadvantage roll the to-hit as 2d20 keeping the higher/lower (they
    cancel if both set); when one is active the result carries roll_mode and the
    pair of dice in to_hit_rolls so the model can narrate it.

    PC attacker: must name their weapon; validated against inventory, to-hit =
    ability_mod + proficiency_bonus, damage = weapon dice + ability_mod.

    NPC attacker with no weapon named: auto-picks the first WEAPONS-table item
    from the NPC's inventory so the stat block's actual weapon is used. To-hit
    uses attacker.attack_bonus (the stat block's pre-computed total); damage =
    weapon dice + ability_mod.

    NPC attacker with no inventory weapons (or unarmed): falls back to
    attack_bonus + 1d6.

    A natural 20 always hits; a natural 1 always misses.
    """
    weapon_name: str | None = None
    damage_type: str | None = None
    damage_dice = "1d6"
    to_hit_bonus = attacker.attack_bonus  # stat-block fallback

    # NPCs auto-equip their first valid inventory weapon: use it when no weapon
    # arg is given OR when the arg fails validation (unknown weapon / not in
    # inventory). The model should never name an NPC's weapon; its guess is
    # silently ignored in favour of the stat-block weapon.
    # PCs must always name their weapon explicitly; errors surface to the model.
    is_npc = not hasattr(attacker, "proficiency_bonus")
    if is_npc:
        raw_inv = getattr(attacker, "inventory", [])
        inv_lower = [w.strip().lower() for w in raw_inv]
        weapon_candidate = (weapon or "").strip().lower()
        if not (weapon_candidate in WEAPONS and weapon_candidate in inv_lower):
            weapon = next((w for w in raw_inv if w.strip().lower() in WEAPONS), None)

    if weapon is not None:
        weapon_key = weapon.strip().lower()
        entry = WEAPONS.get(weapon_key)
        if entry is None:
            return {
                "ok": False,
                "error": f"Unknown weapon {weapon!r}. Known: {', '.join(WEAPONS)}.",
            }
        raw_inventory = getattr(attacker, "inventory", [])
        inventory = [i.strip().lower() for i in raw_inventory]
        if weapon_key not in inventory:
            available = [i for i in raw_inventory if i.strip().lower() in WEAPONS]
            avail_str = ", ".join(available) if available else "none"
            return {
                "ok": False,
                "error": f"{attacker.name} has no {weapon}; available: {avail_str}",
            }

        _, ability_mod = _weapon_modifier(attacker, entry.get("finesse", False))
        if is_npc:
            # NPC attack_bonus already encodes proficiency; derive damage mod only.
            to_hit_bonus = attacker.attack_bonus
        else:
            to_hit_bonus = ability_mod + attacker.proficiency_bonus

        base = entry["dice"]
        damage_dice = f"{base}+{ability_mod}" if ability_mod > 0 else (f"{base}{ability_mod}" if ability_mod < 0 else base)
        weapon_name = weapon
        damage_type = entry["type"]

    nat, d20_rolls = _d20(advantage=advantage, disadvantage=disadvantage)
    to_hit = nat + to_hit_bonus
    hit = nat == 20 or (nat != 1 and to_hit >= defender.ac)
    result = {
        "ok": True,
        "attacker": attacker.name,
        "defender": defender.name,
        "to_hit_roll": nat,
        "to_hit_bonus": to_hit_bonus,
        "to_hit_total": to_hit,
        "defender_ac": defender.ac,
        "hit": hit,
        "critical": nat == 20,
    }
    if advantage != disadvantage:  # exactly one set; both cancel to a straight roll
        result["roll_mode"] = "advantage" if advantage else "disadvantage"
        result["to_hit_rolls"] = d20_rolls
    if weapon_name:
        result["weapon"] = weapon_name
        result["damage_type"] = damage_type
    if hit:
        dmg = roll(damage_dice)
        if nat == 20:  # crit: double the dice
            dmg = RollResult(damage_dice, dmg.rolls * 2, dmg.modifier, sum(dmg.rolls * 2) + dmg.modifier)
        dealt = apply_damage(defender, dmg.total, from_crit=(nat == 20))
        result.update({"damage": dmg.total, "damage_detail": dmg.describe(), "defender_hp": dealt["hp"], "downed": dealt["downed"]})
    return result


def _spend_inspiration(character, use_inspiration: bool) -> tuple[int, dict]:
    """Roll the d20 for a check/save, spending inspiration for advantage when asked.

    Returns (kept_nat, extra_fields) where extra_fields is merged into the result so the
    model can narrate the reroll. When use_inspiration is True and the character holds a
    point, the engine rolls 2d20-keep-higher, zeroes the point, and locks it for the
    session (inspiration_used). When asked but none is held, it rolls normally and reports
    inspiration_used=False so the model narrates 'no luck left to spend'.
    """
    if not use_inspiration:
        nat, _ = _d20(advantage=False)
        return nat, {}
    if getattr(character, "inspiration", 0) >= 1:
        nat, rolls = _d20(advantage=True)
        character.inspiration = 0
        character.inspiration_used = True
        return nat, {"inspiration_used": True, "inspiration_rolls": rolls}
    nat, _ = _d20(advantage=False)
    return nat, {"inspiration_used": False, "inspiration_reason": "no_inspiration"}


def skill_check(character, ability: str, dc: int, use_inspiration: bool = False) -> dict:
    """Roll d20 + the character's ability modifier against DC. Always resolves.

    Passing use_inspiration spends the character's inspiration (if held) for advantage —
    see _spend_inspiration; the engine owns the reroll and the budget.
    """
    ability = ability.strip().lower()
    modifier = character.ability_modifiers.get(ability, 0)
    nat, insp = _spend_inspiration(character, use_inspiration)
    total = nat + modifier
    sign = "+" if modifier >= 0 else ""
    return {
        "ok": True,
        "character": character.name,
        "ability": ability,
        "modifier": modifier,
        "roll": nat,
        "total": total,
        "dc": dc,
        "success": total >= dc,
        "detail": f"d20({nat}) {sign}{modifier} = {total} vs DC {dc}",
        **insp,
    }


def saving_throw(character, ability: str, dc: int, use_inspiration: bool = False) -> dict:
    """Roll a saving throw: d20 + ability modifier (+ proficiency if proficient in
    that save) against a DC. Always resolves.

    The reactive twin of ``skill_check``: a *check* is a proactive action a character
    takes (perception, persuasion, athletics), while a *save* is rolled to RESIST an
    effect that happens to them (DEX vs a trap, CON vs poison, WIS vs fear). The two
    differ mechanically here in one way: a character proficient in the save (the
    ability appears in its ``save_proficiencies``) adds ``proficiency_bonus``, whereas
    a plain ability check never adds proficiency. NPCs (no proficiency_bonus / save
    list) simply roll d20 + ability modifier. There is no natural-1/20 auto-fail/
    auto-succeed, matching ``skill_check``.
    """
    ability = ability.strip().lower()
    modifier = character.ability_modifiers.get(ability, 0)
    proficient = ability in {a.strip().lower() for a in getattr(character, "save_proficiencies", [])}
    if proficient:
        modifier += getattr(character, "proficiency_bonus", 0)
    nat, insp = _spend_inspiration(character, use_inspiration)
    total = nat + modifier
    sign = "+" if modifier >= 0 else ""
    return {
        "ok": True,
        "kind": "saving_throw",
        "character": character.name,
        "ability": ability,
        "modifier": modifier,
        "proficient": proficient,
        "roll": nat,
        "total": total,
        "dc": dc,
        "success": total >= dc,
        "detail": f"{ability.upper()} save: d20({nat}) {sign}{modifier} = {total} vs DC {dc}",
        **insp,
    }


def roll_initiative(combatants: dict) -> tuple[list[str], dict[str, int]]:
    """Return (ordered_keys, {key: initiative_total}), highest initiative first.

    The model must never decide turn order — this function is the sole authority.
    Returning the totals alongside the order lets callers (e.g. add_npc) splice
    a new combatant into the correct slot without re-rolling everyone.

    Tie-breaking (fully deterministic):
      1. Higher Dex modifier wins (quicker reflexes edge out the slower combatant).
      2. If Dex modifiers are also equal, the insertion order of *combatants* is
         preserved (Python's sort is stable), giving a consistent result across
         repeated calls with the same input mapping.

    Missing "dex" in ability_modifiers is treated as 0.
    """
    entries: list[tuple[str, int, int]] = []
    initiatives: dict[str, int] = {}
    for key, c in combatants.items():
        dex = c.ability_modifiers.get("dex", 0)
        total = roll("1d20").total + dex
        initiatives[key] = total
        entries.append((key, total, dex))
    # Negate both keys so the highest values sort first; stable sort preserves
    # insertion order when both keys are equal.
    entries.sort(key=lambda e: (-e[1], -e[2]))
    return [key for key, _, _ in entries], initiatives


# 5e SRD damaging spells the engine can resolve. Each entry needs `by_slot`
# (slot level → damage dice; the model passes the slot it cast at) and `resolution`:
#   "auto_hit"     — no attack roll; damage applied (e.g. magic_missile).
#   "spell_attack" — d20 + (spell ability mod + proficiency) vs AC; miss = no damage.
# The engine has no saving-throw mechanic yet (see README "Need to implement"), so
# save-for-half spells (fireball, cone of cold, …) are modelled as "auto_hit" full
# damage against a single target. Area spells likewise resolve against one target;
# the model narrates the blast. Non-damage spells (buffs, healing, control) are not
# tabled — casting one consumes the slot and is narrated (see cast_damaging_spell).
SPELLS: dict[str, dict] = {
    # --- cantrips (level 0) ---
    "fire_bolt":      {"name": "Fire Bolt",      "level": 0, "tradition": "arcane", "resolution": "spell_attack", "effect": "damage", "damage_type": "fire",      "by_slot": {0: "1d10"}, "target": "single"},
    "ray_of_frost":   {"name": "Ray of Frost",   "level": 0, "tradition": "arcane", "resolution": "spell_attack", "effect": "damage", "damage_type": "cold",      "by_slot": {0: "1d8"},  "target": "single"},
    "shocking_grasp": {"name": "Shocking Grasp", "level": 0, "tradition": "arcane", "resolution": "spell_attack", "effect": "damage", "damage_type": "lightning", "by_slot": {0: "1d8"},  "target": "single"},
    "chill_touch":    {"name": "Chill Touch",    "level": 0, "tradition": "arcane", "resolution": "spell_attack", "effect": "damage", "damage_type": "necrotic",  "by_slot": {0: "1d8"},  "target": "single"},
    "eldritch_blast": {"name": "Eldritch Blast", "level": 0, "tradition": "arcane", "resolution": "spell_attack", "effect": "damage", "damage_type": "force",     "by_slot": {0: "1d10"}, "target": "single"},
    "thorn_whip":     {"name": "Thorn Whip",     "level": 0, "tradition": "primal", "resolution": "spell_attack", "effect": "damage", "damage_type": "piercing",  "by_slot": {0: "1d6"},  "target": "single"},
    "produce_flame":  {"name": "Produce Flame",  "level": 0, "tradition": "primal", "resolution": "spell_attack", "effect": "damage", "damage_type": "fire",      "by_slot": {0: "1d8"},  "target": "single"},
    "sacred_flame":   {"name": "Sacred Flame",   "level": 0, "tradition": "divine", "resolution": "auto_hit",     "effect": "damage", "damage_type": "radiant",   "by_slot": {0: "1d8"},  "target": "single"},
    "poison_spray":   {"name": "Poison Spray",   "level": 0, "tradition": "arcane", "resolution": "auto_hit",     "effect": "damage", "damage_type": "poison",    "by_slot": {0: "1d12"}, "target": "single"},
    "vicious_mockery": {"name": "Vicious Mockery", "level": 0, "tradition": "arcane", "resolution": "auto_hit",   "effect": "damage", "damage_type": "psychic",   "by_slot": {0: "1d4"},  "target": "single"},
    # --- level 1 ---
    "magic_missile": {
        "name": "Magic Missile",
        "level": 1,
        "tradition": "arcane",
        "resolution": "auto_hit",
        "effect": "damage",
        "damage_type": "force",
        "by_slot": {1: "3d4+3", 2: "4d4+4", 3: "5d4+5"},
        "target": "single",
    },
    "guiding_bolt": {
        "name": "Guiding Bolt",
        "level": 1,
        "tradition": "divine",
        "resolution": "spell_attack",
        "effect": "damage",
        "damage_type": "radiant",
        "by_slot": {1: "4d6", 2: "5d6", 3: "6d6"},
        "target": "single",
    },
    "chromatic_orb": {
        "name": "Chromatic Orb",
        "level": 1,
        "tradition": "arcane",
        "resolution": "spell_attack",
        "effect": "damage",
        "damage_type": "force",
        "by_slot": {1: "3d8", 2: "4d8", 3: "5d8"},
        "target": "single",
    },
    "burning_hands":  {"name": "Burning Hands",  "level": 1, "tradition": "arcane", "resolution": "auto_hit",     "effect": "damage", "damage_type": "fire",      "by_slot": {1: "3d6", 2: "4d6", 3: "5d6", 4: "6d6"}, "target": "single"},
    "thunderwave":    {"name": "Thunderwave",    "level": 1, "tradition": "arcane", "resolution": "auto_hit",     "effect": "damage", "damage_type": "thunder",   "by_slot": {1: "2d8", 2: "3d8", 3: "4d8"}, "target": "single"},
    "witch_bolt":     {"name": "Witch Bolt",     "level": 1, "tradition": "arcane", "resolution": "spell_attack", "effect": "damage", "damage_type": "lightning", "by_slot": {1: "1d12", 2: "2d12", 3: "3d12"}, "target": "single"},
    "inflict_wounds": {"name": "Inflict Wounds", "level": 1, "tradition": "divine", "resolution": "spell_attack", "effect": "damage", "damage_type": "necrotic",  "by_slot": {1: "3d10", 2: "4d10", 3: "5d10"}, "target": "single"},
    "hellish_rebuke": {"name": "Hellish Rebuke", "level": 1, "tradition": "arcane", "resolution": "auto_hit",     "effect": "damage", "damage_type": "fire",      "by_slot": {1: "2d10", 2: "3d10", 3: "4d10"}, "target": "single"},
    "ice_knife":      {"name": "Ice Knife",      "level": 1, "tradition": "arcane", "resolution": "spell_attack", "effect": "damage", "damage_type": "piercing",  "by_slot": {1: "1d10", 2: "1d10", 3: "1d10"}, "target": "single"},
    # --- level 2 ---
    "scorching_ray":  {"name": "Scorching Ray",  "level": 2, "tradition": "arcane", "resolution": "spell_attack", "effect": "damage", "damage_type": "fire",      "by_slot": {2: "6d6", 3: "8d6", 4: "10d6"}, "target": "single"},
    "shatter":        {"name": "Shatter",        "level": 2, "tradition": "arcane", "resolution": "auto_hit",     "effect": "damage", "damage_type": "thunder",   "by_slot": {2: "3d8", 3: "4d8", 4: "5d8"}, "target": "single"},
    "acid_arrow":     {"name": "Acid Arrow",     "level": 2, "tradition": "arcane", "resolution": "spell_attack", "effect": "damage", "damage_type": "acid",      "by_slot": {2: "4d4", 3: "5d4", 4: "6d4"}, "target": "single"},
    "moonbeam":       {"name": "Moonbeam",       "level": 2, "tradition": "primal", "resolution": "auto_hit",     "effect": "damage", "damage_type": "radiant",   "by_slot": {2: "2d10", 3: "3d10", 4: "4d10"}, "target": "single"},
    # --- level 3 ---
    "fireball":       {"name": "Fireball",       "level": 3, "tradition": "arcane", "resolution": "auto_hit",     "effect": "damage", "damage_type": "fire",      "by_slot": {3: "8d6", 4: "9d6", 5: "10d6", 6: "11d6"}, "target": "single"},
    "lightning_bolt": {"name": "Lightning Bolt", "level": 3, "tradition": "arcane", "resolution": "auto_hit",     "effect": "damage", "damage_type": "lightning", "by_slot": {3: "8d6", 4: "9d6", 5: "10d6", 6: "11d6"}, "target": "single"},
    "vampiric_touch": {"name": "Vampiric Touch", "level": 3, "tradition": "arcane", "resolution": "spell_attack", "effect": "damage", "damage_type": "necrotic",  "by_slot": {3: "3d6", 4: "4d6", 5: "5d6"}, "target": "single"},
    "call_lightning": {"name": "Call Lightning", "level": 3, "tradition": "primal", "resolution": "auto_hit",     "effect": "damage", "damage_type": "lightning", "by_slot": {3: "3d10", 4: "4d10", 5: "5d10"}, "target": "single"},
    # --- level 4 ---
    "blight":         {"name": "Blight",         "level": 4, "tradition": "arcane", "resolution": "auto_hit",     "effect": "damage", "damage_type": "necrotic",  "by_slot": {4: "8d8", 5: "9d8", 6: "10d8"}, "target": "single"},
    "ice_storm":      {"name": "Ice Storm",      "level": 4, "tradition": "arcane", "resolution": "auto_hit",     "effect": "damage", "damage_type": "cold",      "by_slot": {4: "2d8", 5: "3d8"}, "target": "single"},
    # --- level 5 ---
    "cone_of_cold":   {"name": "Cone of Cold",   "level": 5, "tradition": "arcane", "resolution": "auto_hit",     "effect": "damage", "damage_type": "cold",      "by_slot": {5: "8d8", 6: "9d8", 7: "10d8"}, "target": "single"},
    "flame_strike":   {"name": "Flame Strike",   "level": 5, "tradition": "divine", "resolution": "auto_hit",     "effect": "damage", "damage_type": "fire",      "by_slot": {5: "8d6", 6: "9d6", 7: "10d6"}, "target": "single"},
    # --- level 6 ---
    "disintegrate":   {"name": "Disintegrate",   "level": 6, "tradition": "arcane", "resolution": "auto_hit",     "effect": "damage", "damage_type": "force",     "by_slot": {6: "10d6+40", 7: "13d6+40", 8: "16d6+40"}, "target": "single"},
    "chain_lightning": {"name": "Chain Lightning", "level": 6, "tradition": "arcane", "resolution": "auto_hit",   "effect": "damage", "damage_type": "lightning", "by_slot": {6: "10d8", 7: "11d8"}, "target": "single"},
    # --- level 7 ---
    "finger_of_death": {"name": "Finger of Death", "level": 7, "tradition": "arcane", "resolution": "auto_hit",   "effect": "damage", "damage_type": "necrotic",  "by_slot": {7: "7d8+30"}, "target": "single"},
    "delayed_blast_fireball": {"name": "Delayed Blast Fireball", "level": 7, "tradition": "arcane", "resolution": "auto_hit", "effect": "damage", "damage_type": "fire", "by_slot": {7: "12d6", 8: "13d6", 9: "14d6"}, "target": "single"},
    # --- level 8 ---
    "horrid_wilting": {"name": "Horrid Wilting", "level": 8, "tradition": "arcane", "resolution": "auto_hit",     "effect": "damage", "damage_type": "necrotic",  "by_slot": {8: "10d10", 9: "11d10"}, "target": "single"},
    # --- level 9 ---
    "meteor_swarm":   {"name": "Meteor Swarm",   "level": 9, "tradition": "arcane", "resolution": "auto_hit",     "effect": "damage", "damage_type": "fire",      "by_slot": {9: "40d6"}, "target": "single"},
}


def cast_damaging_spell(caster, target, spell_name: str, spell_level: int,
                        advantage: bool = False, disadvantage: bool = False) -> dict:
    """Consume a spell slot and apply spell damage atomically.

    advantage/disadvantage only affect a "spell_attack" roll (2d20 keep higher/lower;
    they cancel if both set) — "auto_hit" spells make no roll, so the flags are inert.

    Validates before consuming anything:
      (a) caster knows the spell (spell_id in caster.spells, when attribute present)
      (b) caster has a slot of the required level (delegated to cast_spell)

    Resolution:
      "auto_hit"     — no attack roll; damage applied unconditionally (e.g. magic_missile).
      "spell_attack" — d20 + (spellcasting_ability mod + proficiency from level) vs AC.
                       On a miss the slot is consumed but no damage is applied.

    The engine owns both the roll and the HP change. rolled == applied is the invariant.
    """
    spell_key = spell_name.strip().lower().replace(" ", "_")

    # (a) Knowledge check — before cast_spell so nothing is consumed on failure.
    known = getattr(caster, "spells", None)
    if known is not None and spell_key not in known:
        return {"ok": False, "reason": f"{caster.name} does not know {spell_name}"}

    # (a.5) Minimum-level check — a leveled spell cannot be cast below its tabled
    # base level. Without this, declaring spell_level 0 takes the free-cantrip
    # path in cast_spell AND the by_slot[max(...)] fallback below, casting a
    # leveled spell for no slot at its highest upcast. Refuse before anything is
    # consumed; the model must narrate the fizzle.
    spell = SPELLS.get(spell_key)
    if spell is not None and spell_level < spell["level"]:
        return {
            "ok": False,
            "reason": (
                f"{spell.get('name', spell_name)} is a level-{spell['level']} spell "
                f"and cannot be cast with a level-{spell_level} slot"
            ),
            "below_min_level": True,
        }

    # (b) Slot check + consumption — returns ok=False without consuming if empty.
    slot_res = cast_spell(caster, spell_level)
    if not slot_res["ok"]:
        return slot_res

    # No tabled entry, or a tabled non-damage spell (no dice): the engine can't
    # resolve a number, so the slot is consumed and the effect is narrated.
    if spell is None or spell.get("effect") != "damage" or not spell.get("by_slot"):
        return {
            **slot_res,
            "damage_applied": False,
            "note": (
                f"No damaging spell entry for {spell_name!r}. "
                "Slot consumed; describe the effect narratively or add it to SPELLS."
            ),
        }

    by_slot = spell["by_slot"]
    dice_expr = by_slot.get(spell_level, by_slot[max(by_slot)])
    resolution = spell["resolution"]

    result = {
        "ok": True,
        "caster": caster.name,
        "spell": spell_name,
        "spell_level": spell_level,
        "slots_remaining": slot_res["slots_remaining"],
        "auto_hit": resolution == "auto_hit",
        "target": target.name,
    }

    if resolution == "spell_attack":
        ability = getattr(caster, "spellcasting_ability", "")
        ability_mod = getattr(caster, "ability_modifiers", {}).get(ability, 0) if ability else 0
        proficiency = 2 + (getattr(caster, "level", 1) - 1) // 4
        spell_attack_bonus = ability_mod + proficiency

        nat, d20_rolls = _d20(advantage=advantage, disadvantage=disadvantage)
        to_hit_total = nat + spell_attack_bonus
        hit = nat == 20 or (nat != 1 and to_hit_total >= target.ac)

        result.update({
            "to_hit_roll": nat,
            "to_hit_bonus": spell_attack_bonus,
            "to_hit_total": to_hit_total,
            "defender_ac": target.ac,
            "hit": hit,
            "critical": nat == 20,
        })
        if advantage != disadvantage:  # exactly one set; both cancel to a straight roll
            result["roll_mode"] = "advantage" if advantage else "disadvantage"
            result["to_hit_rolls"] = d20_rolls

        if not hit:
            return result  # slot consumed; no damage on a miss

        dmg = roll(dice_expr)
        if nat == 20:  # crit: double the dice
            dmg = RollResult(dice_expr, dmg.rolls * 2, dmg.modifier, sum(dmg.rolls * 2) + dmg.modifier)
    else:
        dmg = roll(dice_expr)

    dealt = apply_damage(target, dmg.total, from_crit=result.get("critical", False))
    result.update({
        "damage": dmg.total,
        "damage_detail": dmg.describe(),
        "target_hp": dealt["hp"],
        "downed": dealt["downed"],
    })
    return result


# --- canonical monster stat blocks -----------------------------------------
# Values the engine owns; the model must never invent HP, AC, or attack numbers.
# inventory lists the weapons an NPC carries; dispatch validates these against WEAPONS.
MONSTERS: dict[str, dict] = {
    "goblin": {
        "name": "Goblin",
        "max_hp": 12,
        "ac": 13,
        "attack_bonus": 4,
        "ability_modifiers": {"str": -1, "dex": 2, "con": 0, "int": 0, "wis": -1, "cha": -1},
        "disposition_dc": 18,
        "alertness_dc": 9,
        "inventory": ["shortsword", "shortbow"],
    },
    "orc": {
        "name": "Orc",
        "max_hp": 15,
        "ac": 13,
        "attack_bonus": 5,
        "ability_modifiers": {"str": 3, "dex": 1, "con": 3, "int": -2, "wis": 0, "cha": 0},
        "disposition_dc": 9,
        "alertness_dc": 10,
        "inventory": ["greataxe"],
    },
    "skeleton": {
        "name": "Skeleton",
        "max_hp": 13,
        "ac": 13,
        "attack_bonus": 4,
        "ability_modifiers": {"str": 0, "dex": 3, "con": 2, "int": -2, "wis": -1, "cha": -3},
        "disposition_dc": 8,
        "alertness_dc": 9,
        "inventory": ["shortsword", "shortbow"],
    },
    # Beasts and undead with an empty inventory use the unarmed fallback (attack_bonus
    # + 1d6) for their natural attacks; weapon-users list weapons from the WEAPONS table.
    # alertness_dc is each creature's 5e passive Perception (the DC to sneak past it).
    # disposition_dc (the Charisma-check DC to turn a hostile NPC friendly) is a randomly
    # assigned default, NOT a 5e stat. Authors can override either per-NPC, e.g. null for
    # an always-alert sentry or an NPC that cannot be reasoned with.
    # --- low threat ---
    "kobold":          {"name": "Kobold",          "max_hp": 5,   "ac": 12, "attack_bonus": 4, "ability_modifiers": {"str": -2, "dex": 2, "con": -1, "int": -1, "wis": -2, "cha": -1}, "disposition_dc": 19, "alertness_dc": 8, "inventory": ["dagger", "sling"]},
    "giant_rat":       {"name": "Giant Rat",       "max_hp": 7,   "ac": 12, "attack_bonus": 4, "ability_modifiers": {"str": -2, "dex": 2, "con": 0, "int": -4, "wis": 0, "cha": -3}, "disposition_dc": 12, "alertness_dc": 10, "inventory": []},
    "cultist":         {"name": "Cultist",         "max_hp": 9,   "ac": 12, "attack_bonus": 3, "ability_modifiers": {"str": 0, "dex": 1, "con": 0, "int": 0, "wis": 0, "cha": 0}, "disposition_dc": 11, "alertness_dc": 10, "inventory": ["scimitar"]},
    "acolyte":         {"name": "Acolyte",         "max_hp": 9,   "ac": 10, "attack_bonus": 2, "ability_modifiers": {"str": 0, "dex": 0, "con": 0, "int": 0, "wis": 2, "cha": 0}, "disposition_dc": 11, "alertness_dc": 12, "inventory": ["club"]},
    "guard":           {"name": "Guard",           "max_hp": 11,  "ac": 16, "attack_bonus": 3, "ability_modifiers": {"str": 1, "dex": 1, "con": 1, "int": 0, "wis": 0, "cha": 0}, "disposition_dc": 10, "alertness_dc": 12, "inventory": ["spear"]},
    "bandit":          {"name": "Bandit",          "max_hp": 11,  "ac": 12, "attack_bonus": 3, "ability_modifiers": {"str": 0, "dex": 1, "con": 1, "int": 0, "wis": 0, "cha": 0}, "disposition_dc": 19, "alertness_dc": 10, "inventory": ["scimitar", "light crossbow"]},
    "hobgoblin":       {"name": "Hobgoblin",       "max_hp": 11,  "ac": 18, "attack_bonus": 3, "ability_modifiers": {"str": 1, "dex": 1, "con": 1, "int": 0, "wis": 0, "cha": -1}, "disposition_dc": 9, "alertness_dc": 10, "inventory": ["longsword", "longbow"]},
    "wolf":            {"name": "Wolf",            "max_hp": 11,  "ac": 13, "attack_bonus": 4, "ability_modifiers": {"str": 1, "dex": 2, "con": 1, "int": -4, "wis": 1, "cha": -2}, "disposition_dc": 18, "alertness_dc": 13, "inventory": []},
    "scout":           {"name": "Scout",           "max_hp": 16,  "ac": 13, "attack_bonus": 4, "ability_modifiers": {"str": 0, "dex": 2, "con": 1, "int": 0, "wis": 1, "cha": 0}, "disposition_dc": 19, "alertness_dc": 15, "inventory": ["shortsword", "longbow"]},
    "gnoll":           {"name": "Gnoll",           "max_hp": 22,  "ac": 15, "attack_bonus": 4, "ability_modifiers": {"str": 2, "dex": 1, "con": 0, "int": -2, "wis": 0, "cha": -2}, "disposition_dc": 16, "alertness_dc": 10, "inventory": ["spear", "longbow"]},
    "zombie":          {"name": "Zombie",          "max_hp": 22,  "ac": 8,  "attack_bonus": 3, "ability_modifiers": {"str": 1, "dex": -2, "con": 3, "int": -4, "wis": -2, "cha": -3}, "disposition_dc": 9, "alertness_dc": 8, "inventory": []},
    "ghoul":           {"name": "Ghoul",           "max_hp": 22,  "ac": 12, "attack_bonus": 4, "ability_modifiers": {"str": 1, "dex": 2, "con": 0, "int": -2, "wis": 0, "cha": -2}, "disposition_dc": 17, "alertness_dc": 10, "inventory": []},
    "specter":         {"name": "Specter",         "max_hp": 22,  "ac": 12, "attack_bonus": 4, "ability_modifiers": {"str": -5, "dex": 2, "con": 0, "int": 0, "wis": 0, "cha": 0}, "disposition_dc": 14, "alertness_dc": 10, "inventory": []},
    "giant_spider":    {"name": "Giant Spider",    "max_hp": 26,  "ac": 14, "attack_bonus": 5, "ability_modifiers": {"str": 2, "dex": 3, "con": 1, "int": -4, "wis": 0, "cha": -3}, "disposition_dc": 8, "alertness_dc": 10, "inventory": []},
    "worg":            {"name": "Worg",            "max_hp": 26,  "ac": 13, "attack_bonus": 5, "ability_modifiers": {"str": 3, "dex": 1, "con": 1, "int": -2, "wis": 0, "cha": -1}, "disposition_dc": 8, "alertness_dc": 14, "inventory": []},
    "bugbear":         {"name": "Bugbear",         "max_hp": 27,  "ac": 16, "attack_bonus": 4, "ability_modifiers": {"str": 2, "dex": 2, "con": 1, "int": -1, "wis": 0, "cha": -1}, "disposition_dc": 9, "alertness_dc": 10, "inventory": ["morningstar", "javelin"]},
    "thug":            {"name": "Thug",            "max_hp": 32,  "ac": 11, "attack_bonus": 4, "ability_modifiers": {"str": 2, "dex": 0, "con": 2, "int": 0, "wis": 0, "cha": 0}, "disposition_dc": 11, "alertness_dc": 10, "inventory": ["mace", "heavy crossbow"]},
    "animated_armor":  {"name": "Animated Armor",  "max_hp": 33,  "ac": 18, "attack_bonus": 4, "ability_modifiers": {"str": 2, "dex": 0, "con": 1, "int": -5, "wis": -4, "cha": -5}, "disposition_dc": 11, "alertness_dc": 6, "inventory": []},
    "dire_wolf":       {"name": "Dire Wolf",       "max_hp": 37,  "ac": 14, "attack_bonus": 5, "ability_modifiers": {"str": 3, "dex": 2, "con": 2, "int": -4, "wis": 1, "cha": -2}, "disposition_dc": 16, "alertness_dc": 13, "inventory": []},
    # --- mid threat ---
    "wight":           {"name": "Wight",           "max_hp": 45,  "ac": 14, "attack_bonus": 4, "ability_modifiers": {"str": 2, "dex": 2, "con": 3, "int": 0, "wis": 1, "cha": 2}, "disposition_dc": 17, "alertness_dc": 13, "inventory": ["longsword", "longbow"]},
    "knight":          {"name": "Knight",          "max_hp": 52,  "ac": 18, "attack_bonus": 5, "ability_modifiers": {"str": 3, "dex": 0, "con": 2, "int": 0, "wis": 0, "cha": 2}, "disposition_dc": 8, "alertness_dc": 10, "inventory": ["greatsword", "heavy crossbow"]},
    "mummy":           {"name": "Mummy",           "max_hp": 58,  "ac": 11, "attack_bonus": 5, "ability_modifiers": {"str": 3, "dex": -1, "con": 2, "int": -2, "wis": 0, "cha": 1}, "disposition_dc": 16, "alertness_dc": 10, "inventory": []},
    "veteran":         {"name": "Veteran",         "max_hp": 58,  "ac": 17, "attack_bonus": 5, "ability_modifiers": {"str": 3, "dex": 1, "con": 2, "int": 0, "wis": 0, "cha": 0}, "disposition_dc": 11, "alertness_dc": 12, "inventory": ["longsword", "shortsword", "heavy crossbow"]},
    "werewolf":        {"name": "Werewolf",        "max_hp": 58,  "ac": 12, "attack_bonus": 4, "ability_modifiers": {"str": 2, "dex": 1, "con": 2, "int": 0, "wis": 0, "cha": 0}, "disposition_dc": 19, "alertness_dc": 14, "inventory": []},
    "owlbear":         {"name": "Owlbear",         "max_hp": 59,  "ac": 13, "attack_bonus": 7, "ability_modifiers": {"str": 5, "dex": 1, "con": 3, "int": -4, "wis": 1, "cha": -2}, "disposition_dc": 18, "alertness_dc": 13, "inventory": []},
    "ogre":            {"name": "Ogre",            "max_hp": 59,  "ac": 11, "attack_bonus": 6, "ability_modifiers": {"str": 4, "dex": -1, "con": 3, "int": -3, "wis": -2, "cha": -2}, "disposition_dc": 19, "alertness_dc": 8, "inventory": ["greatclub", "javelin"]},
    "bandit_captain":  {"name": "Bandit Captain",  "max_hp": 65,  "ac": 15, "attack_bonus": 5, "ability_modifiers": {"str": 2, "dex": 3, "con": 2, "int": 2, "wis": 0, "cha": 2}, "disposition_dc": 16, "alertness_dc": 10, "inventory": ["scimitar", "dagger"]},
    "berserker":       {"name": "Berserker",       "max_hp": 67,  "ac": 13, "attack_bonus": 5, "ability_modifiers": {"str": 3, "dex": 1, "con": 3, "int": -1, "wis": 0, "cha": -1}, "disposition_dc": 14, "alertness_dc": 10, "inventory": ["greataxe"]},
    "wraith":          {"name": "Wraith",          "max_hp": 67,  "ac": 13, "attack_bonus": 6, "ability_modifiers": {"str": -2, "dex": 3, "con": 3, "int": 1, "wis": 2, "cha": 2}, "disposition_dc": 11, "alertness_dc": 12, "inventory": []},
    # --- high threat ---
    "troll":           {"name": "Troll",           "max_hp": 84,  "ac": 15, "attack_bonus": 7, "ability_modifiers": {"str": 4, "dex": 1, "con": 5, "int": -2, "wis": -1, "cha": -2}, "disposition_dc": 15, "alertness_dc": 12, "inventory": []},
    "hill_giant":      {"name": "Hill Giant",      "max_hp": 105, "ac": 13, "attack_bonus": 8, "ability_modifiers": {"str": 5, "dex": -1, "con": 4, "int": -3, "wis": -1, "cha": -2}, "disposition_dc": 17, "alertness_dc": 12, "inventory": ["greatclub"]},
    "vampire":         {"name": "Vampire",         "max_hp": 144, "ac": 16, "attack_bonus": 9, "ability_modifiers": {"str": 4, "dex": 4, "con": 4, "int": 3, "wis": 2, "cha": 4}, "disposition_dc": 12, "alertness_dc": 17, "inventory": []},
    "fire_giant":      {"name": "Fire Giant",      "max_hp": 162, "ac": 18, "attack_bonus": 11, "ability_modifiers": {"str": 7, "dex": -1, "con": 6, "int": 0, "wis": 2, "cha": 1}, "disposition_dc": 20, "alertness_dc": 16, "inventory": ["greatsword"]},
    "stone_golem":     {"name": "Stone Golem",     "max_hp": 178, "ac": 17, "attack_bonus": 10, "ability_modifiers": {"str": 6, "dex": -1, "con": 5, "int": -4, "wis": 0, "cha": -5}, "disposition_dc": 8, "alertness_dc": 10, "inventory": []},
    "young_red_dragon": {"name": "Young Red Dragon", "max_hp": 178, "ac": 18, "attack_bonus": 10, "ability_modifiers": {"str": 6, "dex": 0, "con": 5, "int": 2, "wis": 0, "cha": 4}, "disposition_dc": 20, "alertness_dc": 18, "inventory": []},
    "adult_red_dragon": {"name": "Adult Red Dragon", "max_hp": 256, "ac": 19, "attack_bonus": 14, "ability_modifiers": {"str": 8, "dex": 0, "con": 7, "int": 3, "wis": 1, "cha": 5}, "disposition_dc": 20, "alertness_dc": 23, "inventory": []},
    "pit_fiend":       {"name": "Pit Fiend",       "max_hp": 300, "ac": 19, "attack_bonus": 14, "ability_modifiers": {"str": 8, "dex": 2, "con": 7, "int": 6, "wis": 4, "cha": 7}, "disposition_dc": 10, "alertness_dc": 14, "inventory": []},
    "ancient_red_dragon": {"name": "Ancient Red Dragon", "max_hp": 546, "ac": 22, "attack_bonus": 17, "ability_modifiers": {"str": 10, "dex": 0, "con": 9, "int": 4, "wis": 2, "cha": 6}, "disposition_dc": 19, "alertness_dc": 26, "inventory": []},
    "tarrasque":       {"name": "Tarrasque",       "max_hp": 676, "ac": 25, "attack_bonus": 19, "ability_modifiers": {"str": 10, "dex": 0, "con": 10, "int": -4, "wis": 0, "cha": 0}, "disposition_dc": 14, "alertness_dc": 10, "inventory": []},
}


def spawn_npc(monster_id: str, name: str | None = None) -> dict:
    """Return NPC constructor kwargs from the MONSTERS table.

    Usage:
        from src.game_state import NPC
        npc = NPC(**rules.spawn_npc("goblin", name="Snik"))

    Raises KeyError for an unknown monster_id.
    """
    template = MONSTERS.get(monster_id)
    if template is None:
        raise KeyError(f"Unknown monster {monster_id!r}. Known: {', '.join(sorted(MONSTERS))}")
    kwargs = dict(template)
    if name is not None:
        kwargs["name"] = name
    kwargs["hp"] = kwargs["max_hp"]  # start at full HP
    return kwargs


CONSUMABLES: dict[str, dict] = {
    "healing_potion": {"name": "Potion of Healing",        "effect": "heal",         "dice": "2d4+2"},
    "greater_healing": {"name": "Potion of Greater Healing", "effect": "heal",         "dice": "4d4+4"},
    "pearl_of_power":  {"name": "Pearl of Power",            "effect": "restore_slot", "level": 1},
}


def apply_consumable(character, item_id: str) -> dict:
    """Apply the mechanical effect of a consumable item.

    Does NOT remove the item from inventory — that is the caller's (dispatch's) job,
    matching the separation in rules.attack / rules.heal vs dispatch.

    Effects:
      "heal"         — roll the item's dice expression, call heal(), return rolled + hp.
      "restore_slot" — increment spell_slots[level] by 1, return new count.
    """
    item = CONSUMABLES.get(item_id.strip().lower())
    if item is None:
        return {
            "ok": False,
            "reason": "unknown_consumable",
            "error": f"Unknown consumable {item_id!r}. Known: {', '.join(CONSUMABLES)}.",
        }

    effect = item["effect"]

    if effect == "heal":
        rolled = roll(item["dice"])
        result = heal(character, rolled.total)
        return {
            "ok": True,
            "item": item["name"],
            "effect": "heal",
            "rolled": rolled.total,
            "roll_detail": rolled.describe(),
            "healed": result["healed"],
            "hp": result["hp"],
        }

    if effect == "restore_slot":
        level = item["level"]
        current = character.spell_slots.get(level, 0)
        cap = getattr(character, "max_spell_slots", {}).get(level)
        # At cap → refuse so the item isn't wasted (dispatch leaves it in inventory).
        if cap is not None and current >= cap:
            return {
                "ok": False,
                "reason": "slots_full",
                "item": item["name"],
                "effect": "restore_slot",
                "level": level,
                "slots_remaining": current,
                "max": cap,
                "error": f"{character.name} already has the maximum level-{level} slots ({cap}).",
            }
        new_count = current + 1 if cap is None else min(current + 1, cap)
        character.spell_slots[level] = new_count
        return {
            "ok": True,
            "item": item["name"],
            "effect": "restore_slot",
            "level": level,
            "slots_remaining": new_count,
            "max": cap,
        }

    return {"ok": False, "reason": "unknown_effect", "error": f"Unhandled effect {effect!r} for {item_id!r}."}


# --- a tiny SRD-lite rules reference the DM can look things up in ----------
SRD_RULES = {
    "advantage": "Roll 2d20, take the higher. Granted by favorable circumstances.",
    "disadvantage": "Roll 2d20, take the lower. From hindrances or impairment.",
    "death_saves": "At 0 HP a character is unconscious and makes death saves (DC 10). Three successes stabilize; three failures kill.",
    "saving_throws": "To resist an effect, roll d20 + the relevant ability modifier (+ proficiency if proficient in that save) vs a DC. Use the saving_throw tool — not skill_check, which is a proactive action. A save is reactive: not an action, and rolled by whoever is affected.",
    "spell_slots": "Casting a leveled spell consumes one slot of that level or higher. Cantrips are free. Slots refresh on a long rest.",
    "armor_class": "An attack hits if the d20 roll plus attack bonus meets or exceeds the target's AC.",
    "magic_missile": "1st-level force evocation, auto-hit, no attack roll. Damage: 3d4+3 (L1); each slot level above 1st adds one missile (+1d4+1). Use cast_spell with spell_name='magic_missile' and a target.",
    "chromatic_orb": "1st-level arcane evocation, spell attack roll. Damage: 3d8 force (L1); each slot level above 1st adds one die (+1d8). Use cast_spell with spell_name='chromatic_orb' and a target.",
    "attack": "A weapon attack rolls d20 + ability modifier + proficiency bonus vs the target's AC (Str for melee, Dex for finesse or ranged — the engine uses whichever is better). On a hit, damage is the weapon's dice + that same ability modifier. NPCs roll d20 + their listed attack_bonus. See critical_hit for natural 20/1. Use the attack tool — the engine owns the roll and damage.",
    "critical_hit": "A natural 20 on an attack or spell-attack roll always hits and doubles the damage dice (ability modifiers are not doubled). A natural 1 always misses. A critical hit against a creature already at 0 HP costs it two death-save failures instead of one.",
    "skill_check": "A proactive ability check: roll d20 + the relevant ability modifier vs a DC. It resolves on the total — unlike an attack, a natural 20 is not an automatic success and a natural 1 is not an automatic failure. Use the skill_check tool for perception, athletics, arcana, stealth, persuasion, etc. To resist an effect happening TO you, use saving_throw instead.",
    "spell_attack": "Some spells (e.g. chromatic_orb) require a spell attack roll: d20 + spellcasting ability modifier + proficiency vs the target's AC; a miss deals no damage. Auto-hit spells (e.g. magic_missile) skip the roll. Always cast with cast_spell, which spends the slot and lets the engine own the damage; a natural 20 doubles the spell's dice.",
    "initiative": "When combat begins, every combatant rolls d20 + Dex modifier; the highest acts first, ties breaking toward the higher Dex. The engine is the sole authority on turn order — never narrate a different order or let a combatant act out of turn.",
    "surprise": "Before combat the party may attempt a stealth ambush (attempt_ambush): the engine rolls a Dex check for each conscious party member against the highest alertness DC among the living hostiles. On success the hostiles are surprised and lose their first turn; on failure the fight is fair. Some foes are always alert and cannot be ambushed.",
    "influence": "Out of combat a party member can try to sway a hostile NPC (influence_npc): the engine rolls a Charisma check against the NPC's disposition DC. Success makes the NPC non-hostile; each NPC can be influenced only once. A failed parley out of combat starts a fight with everyone present. Use influence_npc, never skill_check, for social attempts.",
    "hazards": "Author-placed scene dangers (a dart trap, spore cloud, rune-ward) are declared in a scene's hazards manifest and sprung with trigger_hazard. The engine owns the save ability, DC, and damage: it rolls each affected character's saving throw and applies the authored damage atomically — full on a failed save, half if the hazard is save-for-half, none on a success. You may only trigger a declared hazard, never invent one or supply its numbers.",
    "healing": "Healing restores HP up to the target's maximum (never above it) and cannot revive a dead character. Healing a dying or stable character at 0 HP brings them back to consciousness and resets their death saves. Rolled healing (a potion via use_item, or apply_dice) is rolled and applied by the engine — never narrate a specific amount yourself.",
    "using_items": "A consumable is spent with use_item; the engine applies its fixed effect (a healing potion rolls and restores HP; a Pearl of Power restores one spell slot, refused at the slot cap). In combat, using an item is the user's action and is turn-guarded, and it may instead be administered to a party ally — including a downed one, reviving them. Out of combat there is no action cost.",
    "proficiency_bonus": "The proficiency bonus is added by the engine to weapon attack rolls, spell attacks, and saving throws the character is proficient in (its save_proficiencies) — never to a plain ability check (skill_check). It is a flat per-character value (+2 by default), and for spellcasting scales with caster level (+2 at levels 1-4).",
    "inspiration": "A single DM-awarded reroll. The DM may grant a character inspiration (award_inspiration) to reward clever play or strong roleplay. A character holds at most one and gets only one for the entire session — once spent it is never re-awarded. To spend it, set use_inspiration=true on that character's skill_check or saving_throw: the engine rolls 2d20 and keeps the higher, then locks the point. If none is held when spent, the roll is normal and the result reports inspiration_used=false. The engine owns the reroll and the budget — never narrate a chosen result.",
}


def lookup_rule(topic: str) -> dict:
    key = topic.strip().lower().replace(" ", "_")
    if key in SRD_RULES:
        return {"ok": True, "topic": key, "text": SRD_RULES[key]}
    return {"ok": False, "topic": topic, "text": f"No SRD entry for {topic!r}. Known topics: {', '.join(SRD_RULES)}"}
