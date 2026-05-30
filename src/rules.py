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


def attack(attacker, defender, damage_dice: str = "1d6") -> dict:
    """Resolve a single attack: d20 + bonus vs AC, then damage on a hit.
    A natural 20 always hits; a natural 1 always misses."""
    d20 = roll("1d20")
    nat = d20.rolls[0]
    to_hit = nat + attacker.attack_bonus
    hit = nat == 20 or (nat != 1 and to_hit >= defender.ac)
    result = {
        "ok": True,
        "attacker": attacker.name,
        "defender": defender.name,
        "to_hit_roll": nat,
        "to_hit_total": to_hit,
        "defender_ac": defender.ac,
        "hit": hit,
        "critical": nat == 20,
    }
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


# --- a tiny SRD-lite rules reference the DM can look things up in ----------
SRD_RULES = {
    "advantage": "Roll 2d20, take the higher. Granted by favorable circumstances.",
    "disadvantage": "Roll 2d20, take the lower. From hindrances or impairment.",
    "death_saves": "At 0 HP a character is unconscious and makes death saves (DC 10). Three successes stabilize; three failures kill.",
    "spell_slots": "Casting a leveled spell consumes one slot of that level or higher. Cantrips are free. Slots refresh on a long rest.",
    "armor_class": "An attack hits if the d20 roll plus attack bonus meets or exceeds the target's AC.",
}


def lookup_rule(topic: str) -> dict:
    key = topic.strip().lower().replace(" ", "_")
    if key in SRD_RULES:
        return {"ok": True, "topic": key, "text": SRD_RULES[key]}
    return {"ok": False, "topic": topic, "text": f"No SRD entry for {topic!r}. Known topics: {', '.join(SRD_RULES)}"}
