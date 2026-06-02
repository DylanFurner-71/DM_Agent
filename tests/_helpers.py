"""Shared test helpers used across more than one test module."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.game_state import Character, NPC, GameState


def _make_combat_state():
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    gs.party["wisp"] = Character(name="Wisp", ability_modifiers={"dex": 2},
                                 spells=["magic_missile"])
    gs.npcs["snik"] = NPC(name="Snik", ability_modifiers={"dex": 1})
    return gs
