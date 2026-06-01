"""Tests for the compact status HUD (src.views.format_hud) — no API, no I/O.

The HUD is pure reformatting of state /state already exposes, so it's tested as a
string builder: HP bars, slots, conditions, and the in-combat initiative line with
the active actor marked and dying/dead/companion markers.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.views import format_hud, _hp_bar, _combatant_marker
from src.game_state import Character, NPC, GameState


# --- HP bar -------------------------------------------------------------------

def test_hp_bar_full_and_empty():
    assert _hp_bar(10, 10, width=10) == "█" * 10
    assert _hp_bar(0, 10, width=10) == "░" * 10


def test_hp_bar_half():
    assert _hp_bar(5, 10, width=10) == "█████░░░░░"


def test_hp_bar_sliver_shows_while_alive():
    """1/24 rounds to 0 filled cells, but a living actor must show a sliver."""
    bar = _hp_bar(1, 24, width=10)
    assert bar.startswith("█")
    assert bar == "█" + "░" * 9


def test_hp_bar_zero_max_is_safe():
    assert _hp_bar(0, 0, width=6) == "░" * 6


# --- out of combat ------------------------------------------------------------

def _two_pc_state() -> GameState:
    gs = GameState(location="Hall")
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=24, spell_slots={1: 2})
    gs.party["wisp"] = Character(name="Wisp", max_hp=16, hp=11, spell_slots={1: 1, 2: 1})
    return gs


def test_hud_empty_party_is_blank():
    assert format_hud(GameState(location="Void")) == ""


def test_hud_shows_party_hp_and_slots():
    hud = format_hud(_two_pc_state())
    assert "Aldric" in hud and "24/24" in hud
    assert "Wisp" in hud and "11/16" in hud
    assert "L1:2" in hud
    assert "L1:1 L2:1" in hud
    assert "█" in hud and "░" in hud  # bars rendered
    assert "Round" not in hud          # not in combat → no combat line


def test_hud_shows_conditions_tag():
    gs = _two_pc_state()
    gs.party["aldric"].conditions = ["prone"]
    hud = format_hud(gs)
    assert "[prone]" in hud


def test_hud_dying_and_dead_tags():
    gs = _two_pc_state()
    gs.party["aldric"].hp = 0                       # dying (down, not dead)
    gs.party["wisp"].hp = 0
    gs.party["wisp"].dead = True
    hud = format_hud(gs)
    assert "dying" in hud
    assert "dead" in hud
    # 'unconscious'/'dead' raw conditions are conveyed by the tag, not duplicated
    gs.party["aldric"].conditions = ["unconscious"]
    assert "[unconscious]" not in format_hud(gs)


# --- in combat ----------------------------------------------------------------

def _combat_state() -> GameState:
    gs = _two_pc_state()
    gs.npcs["narl"] = NPC(name="Narl", max_hp=12, hp=0, hostile=True)   # downed foe
    gs.npcs["grik"] = NPC(name="Grik", max_hp=12, hp=8, hostile=True)
    gs.combat_order = ["aldric", "narl", "wisp", "grik"]
    gs.combat_index = 2          # Wisp active
    gs.combat_round = 2
    return gs


def test_hud_combat_line_round_and_order():
    hud = format_hud(_combat_state())
    assert "Round 2" in hud
    # order preserved, arrow-joined
    assert "Aldric" in hud and "Narl" in hud and "Grik" in hud
    assert "→" in hud


def test_hud_active_actor_marked():
    hud = format_hud(_combat_state())
    combat_line = [ln for ln in hud.splitlines() if "Round 2" in ln][0]
    assert "▶Wisp" in combat_line          # index 2 is Wisp
    assert "▶Aldric" not in combat_line


def test_hud_down_marker_in_order():
    hud = format_hud(_combat_state())
    assert "Narl(down)" in hud


def test_hud_companion_marker():
    gs = _combat_state()
    ally = NPC(name="Snik", max_hp=12, hp=12, hostile=False)
    ally.companion = True
    gs.npcs["snik"] = ally
    gs.combat_order.append("snik")
    hud = format_hud(gs)
    assert "Snik(ally)" in hud


# --- marker helper ------------------------------------------------------------

def test_combatant_marker_cases():
    gs = GameState(location="X")
    gs.party["pc"] = Character(name="PC", max_hp=10, hp=10)
    gs.npcs["foe"] = NPC(name="Foe", max_hp=8, hp=8, hostile=True)
    assert _combatant_marker(gs.party["pc"], "pc", gs) == ""
    gs.party["pc"].hp = 0
    assert _combatant_marker(gs.party["pc"], "pc", gs) == "(dying)"
    gs.party["pc"].dead = True
    assert _combatant_marker(gs.party["pc"], "pc", gs) == "(dead)"
    gs.npcs["foe"].hp = 0
    assert _combatant_marker(gs.npcs["foe"], "foe", gs) == "(down)"
