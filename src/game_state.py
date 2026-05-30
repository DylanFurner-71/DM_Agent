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


@dataclass
class Character:
    name: str
    level: int = 1
    max_hp: int = 10
    hp: int = 10
    ac: int = 12
    attack_bonus: int = 4
    # Spell slots by spell level, e.g. {1: 2, 2: 0}. Decremented on cast.
    spell_slots: dict[int, int] = field(default_factory=dict)
    # Ability modifiers (not scores), e.g. {"str": 1, "dex": 2, "con": 0, "int": 3, "wis": 1, "cha": 0}
    ability_modifiers: dict[str, int] = field(default_factory=dict)
    inventory: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)  # e.g. "unconscious", "prone"

    @property
    def is_down(self) -> bool:
        return self.hp <= 0


@dataclass
class NPC:
    name: str
    max_hp: int = 8
    hp: int = 8
    ac: int = 12
    attack_bonus: int = 3
    hostile: bool = True

    @property
    def is_down(self) -> bool:
        return self.hp <= 0


@dataclass
class GameState:
    location: str = "An unremarkable crossroads."
    scene: str = ""
    party: dict[str, Character] = field(default_factory=dict)
    npcs: dict[str, NPC] = field(default_factory=dict)
    quest_flags: dict[str, bool] = field(default_factory=dict)
    turn: int = 0
    log: list[str] = field(default_factory=list)

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
            "party": {k: asdict(v) for k, v in self.party.items()},
            "npcs": {k: asdict(v) for k, v in self.npcs.items()},
            "quest_flags": self.quest_flags,
            "turn": self.turn,
            "log": self.log,
        }

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "GameState":
        gs = cls(
            location=d.get("location", ""),
            scene=d.get("scene", ""),
            quest_flags=d.get("quest_flags", {}),
            turn=d.get("turn", 0),
            log=d.get("log", []),
        )
        for k, v in d.get("party", {}).items():
            # JSON keys are strings; spell_slots keys must be ints.
            v = dict(v)
            v["spell_slots"] = {int(lvl): n for lvl, n in v.get("spell_slots", {}).items()}
            gs.party[k] = Character(**v)
        for k, v in d.get("npcs", {}).items():
            gs.npcs[k] = NPC(**v)
        return gs

    @classmethod
    def load(cls, path: str) -> "GameState":
        with open(path) as f:
            return cls.from_dict(json.load(f))
