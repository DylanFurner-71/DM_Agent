"""Game state: the single source of truth the DM agent reads and mutates.

Design note
-----------
The whole point of this project is that the *game state lives in code, not in the
model's head*. The LLM narrates and decides what to attempt, but every number
(HP, spell slots, dice) is owned here and enforced by ``rules.py``. That
separation is what makes this an agent rather than a chatbot with a dice prop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Optional

from . import rules


def expand_npc_entry(v: dict) -> "NPC":
    """Expand a scenario NPC entry (possibly template-based) into a live NPC object."""
    if "template" in v:
        kwargs = rules.spawn_npc(v["template"], v.get("name"))
        for field_name, val in v.items():
            if field_name not in ("template", "name"):
                kwargs[field_name] = val
        # spawn_npc sets hp = template max_hp; if max_hp was overridden but hp
        # was not explicitly specified, sync hp to the new max so the NPC starts
        # at full health relative to its overridden HP cap.
        if "max_hp" in v and "hp" not in v:
            kwargs["hp"] = kwargs["max_hp"]
        return NPC(**kwargs)
    return NPC(**v)


@dataclass
class Character:
    name: str
    level: int = 1
    max_hp: int = 10
    hp: int = 10
    ac: int = 12
    attack_bonus: int = 4   # used only for unarmed / NPC fallback
    proficiency_bonus: int = 2
    # Spell slots by spell level, e.g. {1: 2, 2: 0}. Decremented on cast.
    spell_slots: dict[int, int] = field(default_factory=dict)
    # Per-level slot cap; restore effects (e.g. Pearl of Power) cannot push a level
    # above this. Defaults on load to the starting allotment when absent.
    max_spell_slots: dict[int, int] = field(default_factory=dict)
    # Ability modifiers (not scores), e.g. {"str": 1, "dex": 2, "con": 0, "int": 3, "wis": 1, "cha": 0}
    ability_modifiers: dict[str, int] = field(default_factory=dict)
    inventory: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)  # e.g. "unconscious", "prone"
    spellcasting_ability: str = ""  # ability modifier key used for spell attack/save, e.g. "int", "wis"
    spells: list[str] = field(default_factory=list)  # known spell ids, e.g. ["magic_missile"]
    # Ability keys this character is proficient in for SAVING THROWS, e.g. ["wis", "con"].
    # Proficient saves add proficiency_bonus; a plain ability check adds none.
    save_proficiencies: list[str] = field(default_factory=list)
    # SKILL names this character is proficient in, e.g. ["stealth", "perception"] (see
    # rules.SKILLS for the skill->ability map). A skill_check that names a proficient skill
    # adds proficiency_bonus; expertise skills add it twice. Without a named skill, a check
    # is a raw ability roll.
    skill_proficiencies: list[str] = field(default_factory=list)
    expertise: list[str] = field(default_factory=list)
    death_save_successes: int = 0
    death_save_failures: int = 0
    dead: bool = False
    stable: bool = False
    # Inspiration: a single DM-awarded reroll (see rules.award_inspiration). A PC holds
    # at most one (inspiration 0/1) and only ever one for the whole session — once spent,
    # inspiration_used locks it so it can never be re-awarded.
    inspiration: int = 0
    inspiration_used: bool = False
    # Gold pieces this character carries (a per-PC purse). Engine-owned: changed only
    # through rules.add_gold / rules.spend_gold, and spend refuses an overspend.
    gold: int = 0

    @property
    def is_down(self) -> bool:
        return self.hp <= 0

    @property
    def is_dead(self) -> bool:
        return self.dead

    @property
    def is_stable(self) -> bool:
        return self.stable

    @property
    def is_dying(self) -> bool:
        return self.hp <= 0 and not self.dead and not self.stable


@dataclass
class NPC:
    name: str
    max_hp: int = 8
    hp: int = 8
    ac: int = 12
    attack_bonus: int = 3
    hostile: bool = True
    ability_modifiers: dict[str, int] = field(default_factory=dict)
    inventory: list[str] = field(default_factory=list)
    disposition_dc: int | None = None   # None = cannot be reasoned with
    social_attempted: bool = False      # one persuasion attempt allowed per NPC
    alertness_dc: int | None = None     # None = always alert (cannot be ambushed); numeric = stealth DC
    surprised: bool = False             # True during the surprise round; cleared when the NPC's slot is skipped
    companion: bool = False             # recruited ally: travels across scenes, fights hostiles on the party's side
    # Merchant catalogue: {item_id: price_gp}. A non-empty shop marks this NPC a merchant;
    # stock is an infinite priced catalogue (buy_item/sell_item), and the merchant buys back
    # only what it stocks, at half price (see rules.buy_item/sell_item).
    shop: dict[str, int] = field(default_factory=dict)

    @property
    def is_down(self) -> bool:
        return self.hp <= 0


@dataclass
class GameState:
    location: str = "An unremarkable crossroads."
    scene: str = ""
    current_scene: str = ""   # active scene key; empty when not using multi-scene format
    scenes: dict = field(default_factory=dict)  # {scene_key: {location, scene, npcs}}
    party: dict[str, Character] = field(default_factory=dict)
    npcs: dict[str, NPC] = field(default_factory=dict)
    quest_flags: dict[str, bool] = field(default_factory=dict)
    sprung_hazards: list[str] = field(default_factory=list)  # "<scene>:<hazard_id>" of fired one-shot hazards
    turn: int = 0
    log: list[str] = field(default_factory=list)
    transcript: list[dict] = field(default_factory=list)  # {"kind": "player"|"dm", "text": str}
    narrative: list[dict] = field(default_factory=list)   # {"turn": int, "text": str} — DM beats only
    # Combat state — defaults to "not in combat".
    combat_order: list[str] = field(default_factory=list)  # party/NPC dict keys in initiative order
    combat_index: int = 0    # index into combat_order for the active combatant
    combat_round: int = 0    # increments each time the order wraps; 0 = not in combat
    action_used: bool = False  # True after the active combatant uses an action; reset by next_turn
    combat_initiatives: dict[str, int] = field(default_factory=dict)  # {key: initiative_total}
    game_over: bool = False
    game_outcome: str = ""  # "" | "victory" | "defeat"
    # Transient runtime flags — NOT serialized (defaults restore on load).
    combat_starting: bool = False   # set when start_combat fires; blocks action tools for rest of _execute
    pending_ambush: bool = False    # a won, unconsumed ambush is queued
    ambush_attempted: bool = False  # an attempt was made in the current scene (one shot)

    # --- lookup helpers -------------------------------------------------
    def find_actor(self, name: str):
        """Case-insensitive lookup across party and NPCs. Returns the object or None."""
        key = name.strip().lower()
        for c in self.party.values():
            if c.name.lower() == key:
                return c
        for n in self.npcs.values():
            if n.name.lower() == key:
                return n
        return None

    def record(self, event: str) -> None:
        self.log.append(f"[turn {self.turn}] {event}")

    # --- persistence ----------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "location": self.location,
            "scene": self.scene,
            "current_scene": self.current_scene,
            "scenes": self.scenes,
            "party": {k: asdict(v) for k, v in self.party.items()},
            "npcs": {k: asdict(v) for k, v in self.npcs.items()},
            "quest_flags": self.quest_flags,
            "sprung_hazards": self.sprung_hazards,
            "turn": self.turn,
            "log": self.log,
            "transcript": self.transcript,
            "narrative": self.narrative,
            "combat_order": self.combat_order,
            "combat_index": self.combat_index,
            "combat_round": self.combat_round,
            "action_used": self.action_used,
            "combat_initiatives": self.combat_initiatives,
            "game_over": self.game_over,
            "game_outcome": self.game_outcome,
        }

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    def restore(self, d: dict) -> None:
        """Replace every field in place from a serialized dict (used by /undo).

        Mutates this instance rather than returning a new object so existing
        references (the REPL's local, the agent's self.state) stay valid after a
        rewind. Round-trips losslessly through from_dict/to_dict, so a snapshot
        taken with to_dict restores exactly — including live NPC HP, spent slots,
        and mid-combat pointers. Transient runtime flags (combat_starting,
        pending_ambush, ambush_attempted) are not serialized and reset to their
        defaults, which is correct at a turn boundary.
        """
        self.__dict__.update(GameState.from_dict(d).__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "GameState":
        current_scene = d.get("current_scene", "")
        scenes = d.get("scenes", {})

        # Fresh multi-scene scenario files have no top-level "npcs" key; expand
        # location/scene/npcs from the active scene. Savegames always write a
        # top-level "npcs" key (via to_dict), so they bypass this and load the
        # live NPC state directly, preserving HP changes from the session.
        if "npcs" not in d and current_scene and current_scene in scenes:
            scene_data = scenes[current_scene]
            location = d.get("location", scene_data.get("location", ""))
            scene_text = d.get("scene", scene_data.get("scene", ""))
            npc_entries = scene_data.get("npcs", {})
        else:
            location = d.get("location", "")
            scene_text = d.get("scene", "")
            npc_entries = d.get("npcs", {})

        gs = cls(
            location=location,
            scene=scene_text,
            current_scene=current_scene,
            scenes=scenes,
            quest_flags=d.get("quest_flags", {}),
            sprung_hazards=d.get("sprung_hazards", []),
            turn=d.get("turn", 0),
            log=d.get("log", []),
            transcript=d.get("transcript", []),
            narrative=d.get("narrative", []),
            combat_order=d.get("combat_order", []),
            combat_index=d.get("combat_index", 0),
            combat_round=d.get("combat_round", 0),
            action_used=d.get("action_used", False),
            combat_initiatives=d.get("combat_initiatives", {}),
            game_over=d.get("game_over", False),
            game_outcome=d.get("game_outcome", ""),
        )
        for k, v in d.get("party", {}).items():
            # JSON keys are strings; spell_slots keys must be ints.
            v = dict(v)
            v["spell_slots"] = {int(lvl): n for lvl, n in v.get("spell_slots", {}).items()}
            # Spell-slot cap: coerce JSON string keys to ints. When the key is ABSENT
            # (a fresh scenario lists full slots), default the cap to that allotment;
            # when PRESENT (a savegame, even empty), preserve it so round-trips are lossless.
            if "max_spell_slots" in v:
                v["max_spell_slots"] = {int(lvl): n for lvl, n in v["max_spell_slots"].items()}
            else:
                v["max_spell_slots"] = dict(v["spell_slots"])
            gs.party[k] = Character(**v)
        for k, v in npc_entries.items():
            gs.npcs[k] = expand_npc_entry(v)
        return gs

    @classmethod
    def load(cls, path: str) -> "GameState":
        with open(path) as f:
            return cls.from_dict(json.load(f))
