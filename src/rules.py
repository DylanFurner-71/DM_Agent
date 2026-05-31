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
from dataclasses import dataclass

# Module-level RNG so tests can seed it deterministically.
_rng = random.Random()


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
    rolls = [_rng.randint(1, sides) for _ in range(count)]
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


def apply_damage(target, amount: int) -> dict:
    amount = max(0, int(amount))
    target.hp = max(0, target.hp - amount)
    downed = target.hp <= 0
    if downed and "unconscious" not in getattr(target, "conditions", []):
        if hasattr(target, "conditions"):
            target.conditions.append("unconscious")
    return {"ok": True, "target": target.name, "damage": amount, "hp": target.hp, "downed": downed}


def heal(target, amount: int) -> dict:
    amount = max(0, int(amount))
    target.hp = min(target.max_hp, target.hp + amount)
    if target.hp > 0 and hasattr(target, "conditions") and "unconscious" in target.conditions:
        target.conditions.remove("unconscious")
    return {"ok": True, "target": target.name, "healed": amount, "hp": target.hp}


# Canonical weapon table: damage die, damage type, optional finesse flag.
# Finesse weapons may use the higher of Str or Dex for damage.
WEAPONS: dict[str, dict] = {
    "dagger":         {"dice": "1d4",  "type": "piercing",     "finesse": True},
    "shortsword":     {"dice": "1d6",  "type": "piercing",     "finesse": True},
    "rapier":         {"dice": "1d8",  "type": "piercing",     "finesse": True},
    "handaxe":        {"dice": "1d6",  "type": "slashing"},
    "longsword":      {"dice": "1d8",  "type": "slashing"},
    "greataxe":       {"dice": "1d12", "type": "slashing"},
    "mace":           {"dice": "1d6",  "type": "bludgeoning"},
    "quarterstaff":   {"dice": "1d6",  "type": "bludgeoning"},
    "greatclub":      {"dice": "1d8",  "type": "bludgeoning"},
    "shortbow":       {"dice": "1d6",  "type": "piercing"},
    "longbow":        {"dice": "1d8",  "type": "piercing"},
    "light crossbow": {"dice": "1d8",  "type": "piercing"},
    "spear":          {"dice": "1d6",  "type": "piercing"},
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


def attack(attacker, defender, weapon: str | None = None) -> dict:
    """Resolve a single attack: d20 + bonus vs AC, then damage on a hit.

    With a weapon name: validates the attacker's inventory, looks up the WEAPONS
    table for the damage die and type, computes to-hit bonus from ability_mod +
    proficiency_bonus, and damage expression from ability_mod alone (proficiency
    does not add to damage in 5e SRD).

    Without a weapon (NPC / unarmed fallback): uses attacker.attack_bonus and 1d6.

    A natural 20 always hits; a natural 1 always misses.
    """
    weapon_name: str | None = None
    damage_type: str | None = None
    damage_dice = "1d6"
    to_hit_bonus = attacker.attack_bonus  # NPC / unarmed fallback

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
        proficiency = getattr(attacker, "proficiency_bonus", 0)
        to_hit_bonus = ability_mod + proficiency

        base = entry["dice"]
        damage_dice = f"{base}+{ability_mod}" if ability_mod > 0 else (f"{base}{ability_mod}" if ability_mod < 0 else base)
        weapon_name = weapon
        damage_type = entry["type"]

    d20 = roll("1d20")
    nat = d20.rolls[0]
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
    if weapon_name:
        result["weapon"] = weapon_name
        result["damage_type"] = damage_type
    if hit:
        dmg = roll(damage_dice)
        if nat == 20:  # crit: double the dice
            dmg = RollResult(damage_dice, dmg.rolls * 2, dmg.modifier, sum(dmg.rolls * 2) + dmg.modifier)
        dealt = apply_damage(defender, dmg.total)
        result.update({"damage": dmg.total, "damage_detail": dmg.describe(), "defender_hp": dealt["hp"], "downed": dealt["downed"]})
    return result


def skill_check(character, ability: str, dc: int) -> dict:
    """Roll d20 + the character's ability modifier against DC. Always resolves."""
    ability = ability.strip().lower()
    modifier = character.ability_modifiers.get(ability, 0)
    r = roll("1d20")
    nat = r.rolls[0]
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
    }


def roll_initiative(combatants: dict) -> list[str]:
    """Return combatant keys sorted by initiative (d20 + Dex modifier), highest first.

    The model must never decide turn order — this function is the sole authority.

    Tie-breaking (fully deterministic):
      1. Higher Dex modifier wins (quicker reflexes edge out the slower combatant).
      2. If Dex modifiers are also equal, the insertion order of *combatants* is
         preserved (Python's sort is stable), giving a consistent result across
         repeated calls with the same input mapping.

    Missing "dex" in ability_modifiers is treated as 0.
    """
    entries = []
    for key, c in combatants.items():
        dex = c.ability_modifiers.get("dex", 0)
        total = roll("1d20").total + dex
        entries.append((key, total, dex))
    # Negate both keys so the highest values sort first; stable sort preserves
    # insertion order when both keys are equal.
    entries.sort(key=lambda e: (-e[1], -e[2]))
    return [key for key, _, _ in entries]


# Canonical damage expression for each known damaging spell, keyed by slot level.
# Always a complete NdX+M expression so the engine rolls exactly one expression
# and that total is the HP delta — no manual modifier is ever added by the model.
SPELL_DAMAGE: dict[str, dict[int, str]] = {
    "magic_missile": {
        1: "3d4+3",   # 3 missiles × (1d4+1)
        2: "4d4+4",
        3: "5d4+5",
        4: "6d4+6",
        5: "7d4+7",
    },
}


def cast_damaging_spell(caster, target, spell_name: str, spell_level: int) -> dict:
    """Consume a spell slot and apply spell damage atomically.

    The engine owns both the roll and the HP change — the model supplies only the
    spell name, caster, target, and slot level. rolled == applied is the invariant.
    """
    slot_res = cast_spell(caster, spell_level)
    if not slot_res["ok"]:
        return slot_res

    spell_key = spell_name.strip().lower().replace(" ", "_")
    level_map = SPELL_DAMAGE.get(spell_key)
    if level_map is None:
        return {
            **slot_res,
            "damage_applied": False,
            "note": (
                f"No damage table for {spell_name!r}. "
                "Slot consumed; describe the effect narratively or add it to SPELL_DAMAGE."
            ),
        }

    # Clamp to the highest known level if the slot exceeds the table.
    dice_expr = level_map.get(spell_level, level_map[max(level_map)])
    dmg = roll(dice_expr)
    dealt = apply_damage(target, dmg.total)

    return {
        "ok": True,
        "caster": caster.name,
        "spell": spell_name,
        "spell_level": spell_level,
        "slots_remaining": slot_res["slots_remaining"],
        "auto_hit": True,
        "damage": dmg.total,
        "damage_detail": dmg.describe(),
        "target": target.name,
        "target_hp": dealt["hp"],
        "downed": dealt["downed"],
    }


# --- a tiny SRD-lite rules reference the DM can look things up in ----------
SRD_RULES = {
    "advantage": "Roll 2d20, take the higher. Granted by favorable circumstances.",
    "disadvantage": "Roll 2d20, take the lower. From hindrances or impairment.",
    "death_saves": "At 0 HP a character is unconscious and makes death saves (DC 10). Three successes stabilize; three failures kill.",
    "spell_slots": "Casting a leveled spell consumes one slot of that level or higher. Cantrips are free. Slots refresh on a long rest.",
    "armor_class": "An attack hits if the d20 roll plus attack bonus meets or exceeds the target's AC.",
    "magic_missile": "1st-level force evocation, auto-hit, no attack roll. Damage: 3d4+3 (L1); each slot level above 1st adds one missile (+1d4+1). Use cast_spell with spell_name='magic_missile' and a target.",
}


def lookup_rule(topic: str) -> dict:
    key = topic.strip().lower().replace(" ", "_")
    if key in SRD_RULES:
        return {"ok": True, "topic": key, "text": SRD_RULES[key]}
    return {"ok": False, "topic": topic, "text": f"No SRD entry for {topic!r}. Known topics: {', '.join(SRD_RULES)}"}
