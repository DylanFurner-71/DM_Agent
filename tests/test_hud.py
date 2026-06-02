"""Tests for the compact status HUD (src.views.format_hud) — no API, no I/O.

The HUD is pure reformatting of state /state already exposes, so it's tested as a
string builder: HP bars, slots, conditions, and the in-combat initiative line with
the active actor marked and dying/dead/companion markers.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import views
from src.views import (
    format_hud, render_hud, _hp_bar, _hp_color, _combatant_marker, _combatant_color,
    _known_spell_groups, _hud_inventory_lines,
)
from src.game_state import Character, NPC, GameState

ESC = "\x1b"  # ANSI escape introducer


def _force_rich(monkeypatch):
    """Drive the rich HUD path with a deterministic no-color console (stable under any
    ambient FORCE_COLOR), so assertions check content/structure, not ANSI."""
    if not views._RICH:
        pytest.skip("rich not installed")
    from rich.console import Console
    monkeypatch.setattr(views, "_rich_on", lambda: True)
    monkeypatch.setattr(
        views, "_CONSOLE",
        Console(no_color=True, highlight=False, force_terminal=False, width=200),
    )


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


# --- known spells grouped by level (HUD sub-lines) ---------------------------

def _caster_state() -> GameState:
    gs = GameState(location="Tower")
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=24, spell_slots={1: 2},
                                   spells=["guiding_bolt"], spellcasting_ability="wis")
    gs.party["wisp"] = Character(name="Wisp", max_hp=16, hp=16, spell_slots={1: 2, 2: 1},
                                 spells=["magic_missile", "chromatic_orb"],
                                 spellcasting_ability="int")
    return gs


def test_known_spell_groups_order_and_labels():
    c = Character(name="W", spells=["magic_missile", "fire_bolt", "chromatic_orb"])
    # cantrips ('C') first, then ascending level; names sorted, display-cased
    assert _known_spell_groups(c) == [("C", "Fire Bolt"), ("L1", "Chromatic Orb, Magic Missile")]


def test_known_spell_groups_untabled_bucketed():
    c = Character(name="W", spells=["feather_fall"])   # not in SPELLS
    assert _known_spell_groups(c) == [("?", "Feather Fall")]


def test_known_spell_groups_empty_for_non_caster():
    assert _known_spell_groups(Character(name="F")) == []


def test_hud_lists_known_spells_grouped():
    hud = format_hud(_caster_state())
    assert "L1 Guiding Bolt" in hud
    assert "L1 Chromatic Orb, Magic Missile" in hud


def test_hud_spell_subline_indented_under_bar():
    hud = format_hud(_caster_state())
    line = next(ln for ln in hud.splitlines() if "Guiding Bolt" in ln)
    # aligned under the HP bar: 2 + namew(6 for 'Aldric') + 2 = 10 leading spaces
    assert line == " " * 10 + "L1 Guiding Bolt"


def test_hud_non_caster_adds_no_spell_line():
    # _two_pc_state PCs have slots but no known spells → no spell sub-lines
    hud = format_hud(_two_pc_state())
    assert len(hud.splitlines()) == 4   # 2 rules + 2 PC lines, nothing extra


# --- inventory: Items line + Consumables line (with counts) ------------------

def test_hud_inventory_lines_split_gear_and_consumables():
    c = Character(name="Wisp", inventory=["dagger", "spellbook", "healing_potion",
                                          "healing_potion", "pearl_of_power"])
    assert _hud_inventory_lines(c) == [
        "Items: dagger, spellbook",
        "Consumables: healing_potion x2, pearl_of_power",
    ]


def test_hud_inventory_single_consumable_has_no_count():
    c = Character(name="Rogue", inventory=["healing_potion"])
    assert _hud_inventory_lines(c) == ["Consumables: healing_potion"]


def test_hud_inventory_gear_only_omits_consumables_line():
    c = Character(name="Aldric", inventory=["mace", "holy symbol", "torch"])
    assert _hud_inventory_lines(c) == ["Items: mace, holy symbol, torch"]


def test_hud_inventory_empty_returns_no_lines():
    assert _hud_inventory_lines(Character(name="Bare")) == []


def test_hud_inventory_consumables_keep_first_seen_order():
    c = Character(name="W", inventory=["pearl_of_power", "healing_potion", "healing_potion"])
    assert _hud_inventory_lines(c) == ["Consumables: pearl_of_power, healing_potion x2"]


def test_hud_renders_inventory_indented_before_spells():
    gs = GameState(location="Barrow")
    gs.party["wisp"] = Character(
        name="Wisp", max_hp=16, hp=16, spell_slots={1: 2},
        spells=["magic_missile"], spellcasting_ability="int",
        inventory=["dagger", "healing_potion", "healing_potion"],
    )
    hud = format_hud(gs)
    lines = hud.splitlines()
    # indented sub-lines (alignment depends on the longest name; just assert indent)
    assert any(ln.startswith("  ") and ln.strip() == "Items: dagger" for ln in lines)
    assert any(ln.strip() == "Consumables: healing_potion x2" for ln in lines)
    # inventory lines come before the spell sub-line
    items_i = next(i for i, ln in enumerate(lines) if "Items: dagger" in ln)
    spell_i = next(i for i, ln in enumerate(lines) if "Magic Missile" in ln)
    assert items_i < spell_i


# --- HUD color: helpers + rich/plain dispatch --------------------------------

def test_hp_color_bands():
    assert _hp_color(24, 24) == "green"     # full → green
    assert _hp_color(13, 24) == "green"     # >50%
    assert _hp_color(9, 24) == "yellow"     # 25–50%
    assert _hp_color(4, 16) == "red"        # ≤25%
    assert _hp_color(0, 16) == "red"        # down
    assert _hp_color(5, 0) == "red"         # guard: zero max


def test_combatant_color_by_side():
    gs = GameState(location="X")
    gs.party["pc"] = Character(name="PC", max_hp=10, hp=10)
    gs.npcs["foe"] = NPC(name="Foe", max_hp=8, hp=8, hostile=True)
    gs.npcs["pal"] = NPC(name="Pal", max_hp=8, hp=8, hostile=False)
    ally = NPC(name="Ally", max_hp=8, hp=8, hostile=False); ally.companion = True
    gs.npcs["ally"] = ally
    assert _combatant_color(gs.party["pc"], "pc", gs) == "cyan"
    assert _combatant_color(gs.npcs["foe"], "foe", gs) == "red"
    assert _combatant_color(gs.npcs["pal"], "pal", gs) == "green"
    assert _combatant_color(gs.npcs["ally"], "ally", gs) == "cyan"
    gs.party["pc"].hp = 0
    assert _combatant_color(gs.party["pc"], "pc", gs) == "dim"
    gs.npcs["foe"].hp = 0
    assert _combatant_color(gs.npcs["foe"], "foe", gs) == "dim"


def test_render_hud_plain_matches_format_hud(capsys):
    views.set_plain(True)   # force the plain branch
    gs = _caster_state()
    render_hud(gs)
    out = capsys.readouterr().out
    assert out == format_hud(gs) + "\n"   # plain path is byte-identical to the builder
    assert ESC not in out


def test_render_hud_empty_party_prints_nothing(capsys):
    render_hud(GameState(location="Void"))
    assert capsys.readouterr().out == ""


def test_render_hud_rich_keeps_content(monkeypatch, capsys):
    _force_rich(monkeypatch)
    gs = _caster_state()
    gs.party["aldric"].conditions = ["prone"]
    render_hud(gs)
    out = capsys.readouterr().out
    assert "Aldric" in out and "Wisp" in out
    assert "24/24" in out                 # HP fraction preserved
    assert "[prone]" in out               # bracket tag rendered literally, not parsed
    assert "L1 Guiding Bolt" in out       # spell sub-line still present
    assert "█" in out                     # bar rendered


def test_render_hud_rich_combat_line(monkeypatch, capsys):
    _force_rich(monkeypatch)
    gs = _combat_state()
    render_hud(gs)
    out = capsys.readouterr().out
    assert "Round 2" in out
    assert "▶Wisp" in out                 # active marker survives coloring
    assert "Narl(down)" in out


# --- PC AC + status display (status shown only when not "ok") -----------------

def test_hud_shows_pc_ac():
    gs = GameState(location="Hall")
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=24, ac=16)
    assert "AC 16" in format_hud(gs)


def test_hud_omits_status_when_ok():
    gs = GameState(location="Hall")
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=24, ac=16)  # healthy
    pc_line = next(ln for ln in format_hud(gs).splitlines() if "Aldric" in ln)
    assert "[" not in pc_line   # no status bracket for an "ok" PC


def test_hud_shows_status_when_not_ok():
    gs = GameState(location="Hall")
    gs.party["wisp"] = Character(name="Wisp", max_hp=16, hp=5, ac=12, conditions=["prone"])
    assert "[prone]" in format_hud(gs)


def test_hud_stable_pc_reads_stable_not_dying():
    """A stable PC (0 HP, stabilized) must read 'stable', not 'dying' — uses the shared
    _pc_status, fixing the old ad-hoc tag that called any 0-HP PC 'dying'."""
    gs = GameState(location="Crypt")
    gs.party["boric"] = Character(name="Boric", max_hp=30, hp=0, ac=18, stable=True)
    line = next(ln for ln in format_hud(gs).splitlines() if "Boric" in ln)
    assert "[stable]" in line
    assert "dying" not in line


def test_hud_dying_pc_shows_save_progress():
    gs = GameState(location="Crypt")
    dying = Character(name="Aldric", max_hp=24, hp=0, ac=16)
    dying.death_save_successes, dying.death_save_failures = 1, 2
    gs.party["a"] = dying
    line = next(ln for ln in format_hud(gs).splitlines() if "Aldric" in ln)
    assert "dying (1✓ 2✗)" in line


def test_hud_rich_shows_ac_and_status(monkeypatch, capsys):
    _force_rich(monkeypatch)
    gs = GameState(location="Hall")
    gs.party["wisp"] = Character(name="Wisp", max_hp=16, hp=5, ac=12, conditions=["prone"])
    render_hud(gs)
    out = capsys.readouterr().out
    assert "AC 12" in out
    assert "[prone]" in out
