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
    target.hp = min(target.max_hp, target.hp + amount)
    if target.hp > 0 and hasattr(target, "conditions") and "unconscious" in target.conditions:
        target.conditions.remove("unconscious")
    if target.hp > 0 and hasattr(target, "death_save_failures"):
        target.death_save_successes = 0
        target.death_save_failures = 0
        target.stable = False
    return {"ok": True, "target": target.name, "healed": amount, "hp": target.hp}


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
        dealt = apply_damage(defender, dmg.total, from_crit=(nat == 20))
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


SPELLS: dict[str, dict] = {
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
}


def cast_damaging_spell(caster, target, spell_name: str, spell_level: int) -> dict:
    """Consume a spell slot and apply spell damage atomically.

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

    # (b) Slot check + consumption — returns ok=False without consuming if empty.
    slot_res = cast_spell(caster, spell_level)
    if not slot_res["ok"]:
        return slot_res

    spell = SPELLS.get(spell_key)
    if spell is None:
        return {
            **slot_res,
            "damage_applied": False,
            "note": (
                f"No spell entry for {spell_name!r}. "
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

        d20_res = roll("1d20")
        nat = d20_res.rolls[0]
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
        "inventory": ["shortsword", "shortbow"],
    },
    "orc": {
        "name": "Orc",
        "max_hp": 15,
        "ac": 13,
        "attack_bonus": 5,
        "ability_modifiers": {"str": 3, "dex": 1, "con": 3, "int": -2, "wis": -1, "cha": -1},
        "inventory": ["greataxe"],
    },
    "skeleton": {
        "name": "Skeleton",
        "max_hp": 13,
        "ac": 13,
        "attack_bonus": 4,
        "ability_modifiers": {"str": 0, "dex": 2, "con": 2, "int": -4, "wis": -2, "cha": -3},
        "inventory": ["shortsword", "shortbow"],
    },
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
    "spell_slots": "Casting a leveled spell consumes one slot of that level or higher. Cantrips are free. Slots refresh on a long rest.",
    "armor_class": "An attack hits if the d20 roll plus attack bonus meets or exceeds the target's AC.",
    "magic_missile": "1st-level force evocation, auto-hit, no attack roll. Damage: 3d4+3 (L1); each slot level above 1st adds one missile (+1d4+1). Use cast_spell with spell_name='magic_missile' and a target.",
    "chromatic_orb": "1st-level arcane evocation, spell attack roll. Damage: 3d8 force (L1); each slot level above 1st adds one die (+1d8). Use cast_spell with spell_name='chromatic_orb' and a target.",
}


def lookup_rule(topic: str) -> dict:
    key = topic.strip().lower().replace(" ", "_")
    if key in SRD_RULES:
        return {"ok": True, "topic": key, "text": SRD_RULES[key]}
    return {"ok": False, "topic": topic, "text": f"No SRD entry for {topic!r}. Known topics: {', '.join(SRD_RULES)}"}
