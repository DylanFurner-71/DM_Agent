"""Tests for the rich rendering layer in src.views — no API, no real terminal.

These pin the two contracts that matter for the Color & Markdown feature:
  1. PLAIN PATH (forced via set_plain, or any non-tty like a pytest capture): output
     is byte-for-byte the old plain text with no ANSI escapes — pipes and CI stay clean.
  2. RICH PATH (forced on): the rich branches render without raising and without leaking
     markup, and the spinner is inert wherever rich is off.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import views
from src.game_state import Character, GameState, NPC

ESC = "\x1b"  # ANSI escape introducer — must never appear in plain output


@pytest.fixture(autouse=True)
def _reset_plain():
    """Each test starts from a known state and restores the module flag after."""
    saved = views._PLAIN
    views.set_plain(False)
    yield
    views.set_plain(saved)


def _state() -> GameState:
    gs = GameState(location="The Ember Chamber")
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=7, spell_slots={1: 1})
    gs.npcs["grik"] = NPC(name="Grik", max_hp=18, hp=5, hostile=True)
    return gs


# --- plain path: no ANSI, content preserved ---------------------------------

def test_rich_off_when_forced_plain(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    views.set_plain(True)
    assert views._rich_on() is False


def test_rich_off_when_not_a_tty(monkeypatch):
    # pytest captures stdout, so isatty() is already False; assert that gates rich off.
    views.set_plain(False)
    assert views._rich_on() is False


def test_render_markdown_plain_prints_verbatim(capsys):
    views.set_plain(True)
    views.render_markdown("**The door** grinds open.")
    out = capsys.readouterr().out
    assert "**The door** grinds open." in out   # not interpreted — printed as-is
    assert ESC not in out


def test_render_markdown_empty_prints_nothing(capsys):
    views.render_markdown("")
    assert capsys.readouterr().out == ""


def test_print_roll_plain_no_ansi(capsys):
    views.set_plain(True)
    views.print_roll("2d6+3")
    out = capsys.readouterr().out
    assert "🎲" in out
    assert ESC not in out


def test_print_state_plain_core_fields(capsys):
    views.set_plain(True)
    views.print_state(_state())
    out = capsys.readouterr().out
    assert "Aldric: HP 7/24 | AC 12" in out   # PC AC now shown, like NPCs
    assert "Grik (NPC)" in out
    assert "hostile" in out
    assert ESC not in out


def test_pc_abilities_signed_and_ordered():
    c = Character(name="X", ability_modifiers={"str": 4, "dex": 0, "con": 3,
                                               "int": -1, "wis": 1, "cha": 2})
    assert views._pc_abilities(c) == "STR +4  DEX +0  CON +3  INT -1  WIS +1  CHA +2"
    assert views._pc_abilities(Character(name="Y")) == ""  # no modifiers → omitted


def test_print_state_shows_ability_modifiers(capsys):
    views.set_plain(True)
    gs = GameState(location="The Ember Chamber")
    gs.party["aldric"] = Character(
        name="Aldric", max_hp=24, hp=24,
        ability_modifiers={"str": 3, "dex": 1, "con": 2, "int": 0, "wis": -1, "cha": 0},
    )
    views.print_state(gs)
    out = capsys.readouterr().out
    assert "Abilities: STR +3  DEX +1  CON +2  INT +0  WIS -1  CHA +0" in out
    assert ESC not in out


def test_print_state_shows_merchant_shop(capsys):
    views.set_plain(True)
    gs = GameState(location="Market")
    gs.npcs["garric"] = NPC(name="Garric", hostile=False, shop={"longsword": 15})
    views.print_state(gs)
    out = capsys.readouterr().out
    assert "Shop:" in out and "longsword (15 gp)" in out


def test_print_state_shows_gold_when_held(capsys):
    views.set_plain(True)
    gs = GameState(location="Vault")
    gs.party["a"] = Character(name="Aldric", max_hp=24, hp=24, gold=42)
    gs.party["b"] = Character(name="Wisp", max_hp=16, hp=16)  # no gold
    views.print_state(gs)
    out = capsys.readouterr().out
    assert "42 gp" in out          # holder shows the purse
    assert "Wisp" in out and "0 gp" not in out  # broke PC shows no gp figure


# --- /state: death-save status ----------------------------------------------

def test_print_state_shows_death_save_progress(capsys):
    views.set_plain(True)
    gs = GameState(location="Crypt")
    dying = Character(name="Aldric", max_hp=24, hp=0, conditions=["unconscious"])
    dying.death_save_successes, dying.death_save_failures = 2, 1
    gs.party["a"] = dying
    views.print_state(gs)
    out = capsys.readouterr().out
    assert "dying" in out
    assert "2✓" in out and "1✗" in out
    assert "unconscious" not in out   # folded into the 'dying' lifecycle word


def test_print_state_shows_stable_and_dead(capsys):
    views.set_plain(True)
    gs = GameState(location="Crypt")
    gs.party["b"] = Character(name="Boric", max_hp=30, hp=0, stable=True)
    gs.party["g"] = Character(name="Grak", max_hp=22, hp=0, dead=True, conditions=["dead"])
    views.print_state(gs)
    out = capsys.readouterr().out
    assert "Boric: HP 0/30 | AC" in out and "| stable" in out
    assert "Grak" in out and "| dead" in out


# --- /state: companion allies -----------------------------------------------

def test_print_state_tags_companion_ally(capsys):
    views.set_plain(True)
    gs = GameState(location="Road")
    gs.npcs["lyra"] = NPC(name="Lyra", hostile=False, companion=True, hp=22, max_hp=22)
    gs.npcs["grik"] = NPC(name="Grik", hostile=True, hp=18, max_hp=18)
    views.print_state(gs)
    out = capsys.readouterr().out
    assert "Lyra (ally)" in out and "| companion" in out
    assert "Grik (NPC)" in out and "| hostile" in out


# --- /state: PC AC + slot caps ----------------------------------------------

def test_print_state_shows_pc_ac_and_slot_caps(capsys):
    views.set_plain(True)
    gs = GameState(location="Hall")
    gs.party["w"] = Character(name="Wisp", max_hp=18, hp=18, ac=13,
                              spell_slots={1: 0, 2: 1}, max_spell_slots={1: 2, 2: 1})
    views.print_state(gs)
    out = capsys.readouterr().out
    assert "AC 13" in out
    assert "L1:0/2" in out and "L2:1/1" in out


# --- /state: scene exits & loot ---------------------------------------------

def test_print_state_lists_exits_and_loot(capsys):
    views.set_plain(True)
    gs = GameState(location="The Ember Chamber", current_scene="ember")
    gs.scenes = {"ember": {"location": "The Ember Chamber",
                           "exits": {"the dark passage": "tomb"},
                           "loot": ["healing_potion", "iron key"]}}
    views.print_state(gs)
    out = capsys.readouterr().out
    assert "Exits: the dark passage" in out
    assert "Loot here: healing_potion, iron key" in out


def test_print_state_omits_nav_when_free_form(capsys):
    views.set_plain(True)
    gs = GameState(location="Nowhere")
    gs.party["a"] = Character(name="A")
    views.print_state(gs)
    out = capsys.readouterr().out
    assert "Exits:" not in out
    assert "Loot here:" not in out


def test_print_state_rich_tags_companion(monkeypatch, capsys):
    _force_rich(monkeypatch)
    gs = GameState(location="Road")
    gs.npcs["lyra"] = NPC(name="Lyra", hostile=False, companion=True, hp=22, max_hp=22)
    views.print_state(gs)
    out = capsys.readouterr().out
    assert "Lyra (ally)" in out and "companion" in out


def test_banner_plain(capsys):
    views.set_plain(True)
    views.banner("data/scenario.json")
    out = capsys.readouterr().out
    assert "DM AGENT" in out
    assert "data/scenario.json" in out
    assert ESC not in out


# --- spinner: inert in plain/non-tty, safe to start/stop --------------------

def test_spinner_noop_when_plain():
    views.set_plain(True)
    sp = views.Spinner("thinking…")
    sp.start()
    assert sp._status is None   # never created a live status
    sp.stop()                   # idempotent, no raise


def test_spinner_stop_without_start_is_safe():
    views.Spinner("x").stop()   # must not raise


def test_spinner_context_manager_plain():
    views.set_plain(True)
    with views.Spinner("x") as sp:
        assert sp._status is None


# --- forced-rich path: renders without raising, strips to plain on capture ---

def _force_rich(monkeypatch):
    """Drive the rich branches under pytest capture and assert content, not color.

    We pin a deterministic Console (no color, no auto-highlight, wide enough not to
    wrap) so these tests stay stable regardless of the ambient terminal env — in
    particular FORCE_COLOR/COLORTERM, which would otherwise make rich emit ANSI even
    into a non-tty capture and split the asserted substrings (e.g. 'HP 7/24' →
    'HP <esc>7<esc>/<esc>24<esc>'). Markup parsing still runs, so the escaping
    contract (a bracketed name rendered literally) is still exercised."""
    if not views._RICH:
        pytest.skip("rich not installed")
    from rich.console import Console
    monkeypatch.setattr(views, "_rich_on", lambda: True)
    monkeypatch.setattr(
        views, "_CONSOLE",
        Console(no_color=True, highlight=False, force_terminal=False, width=200),
    )


def test_print_state_rich_renders_names(monkeypatch, capsys):
    _force_rich(monkeypatch)
    views.print_state(_state())
    out = capsys.readouterr().out
    assert "Aldric" in out
    assert "Grik" in out
    assert "HP 7/24" in out


def test_print_roll_rich_renders(monkeypatch, capsys):
    _force_rich(monkeypatch)
    views.print_roll("1d20+5")
    assert "🎲" in capsys.readouterr().out


def test_print_state_rich_does_not_leak_markup(monkeypatch, capsys):
    """A bracket-bearing NPC name must be escaped, not parsed as a style tag."""
    _force_rich(monkeypatch)
    gs = _state()
    gs.npcs["weird"] = NPC(name="Grik [the Bold]", max_hp=10, hp=10, hostile=True)
    views.print_state(gs)
    out = capsys.readouterr().out
    assert "Grik [the Bold]" in out   # rendered literally, not swallowed as markup


# --- /cost: usage aggregation, pricing, and summary -------------------------

def _trace(*usages):
    """Build a full_trace with one api_call per usage dict, grouped into one turn."""
    return [{"turn": 1, "input": "x", "calls": [],
             "api_calls": [{"phase": "thinking", "elapsed": 1.5, "usage": u} for u in usages]}]


def test_aggregate_usage_sums_all_buckets():
    trace = _trace(
        {"input": 100, "output": 20, "cache_write": 500},
        {"input": 50, "output": 10, "cache_read": 400},
    )
    agg = views.aggregate_usage(trace)
    assert agg["calls"] == 2
    assert agg["input"] == 150
    assert agg["output"] == 30
    assert agg["cache_write"] == 500
    assert agg["cache_read"] == 400
    assert agg["elapsed"] == 3.0


def test_aggregate_usage_empty_trace():
    agg = views.aggregate_usage([])
    assert agg["calls"] == 0
    assert agg["input"] == 0


def test_price_for_matches_family_prefix():
    assert views._price_for("claude-sonnet-4-6")["input"] == 3.0
    assert views._price_for("claude-opus-4-8")["output"] == 75.0
    assert views._price_for("claude-haiku-4-5")["input"] == 1.0
    # Unknown id falls back to Sonnet-class pricing.
    assert views._price_for("gpt-9") == views._DEFAULT_PRICING


def test_estimate_cost_applies_cache_multipliers():
    # 1M of each bucket makes the per-token math easy to verify against list price.
    usage = {"input": 1_000_000, "output": 1_000_000,
             "cache_write": 1_000_000, "cache_read": 1_000_000}
    c = views.estimate_cost(usage, "claude-sonnet-4-6")
    assert c["input"] == pytest.approx(3.0)
    assert c["output"] == pytest.approx(15.0)
    assert c["cache_write"] == pytest.approx(3.0 * 1.25)
    assert c["cache_read"] == pytest.approx(3.0 * 0.10)
    assert c["total"] == pytest.approx(3.0 + 15.0 + 3.75 + 0.30)


def test_format_cost_empty():
    out = views.format_cost([], "claude-sonnet-4-6")
    assert "No API calls recorded yet" in out


def test_format_cost_summary_lines():
    trace = _trace({"input": 1000, "output": 200, "cache_read": 5000, "cache_write": 800})
    out = views.format_cost(trace, "claude-sonnet-4-6")
    assert "claude-sonnet-4-6" in out
    assert "1 API call " in out          # singular, exactly one call
    assert "Input" in out and "Output" in out
    assert "Cache write" in out and "Cache read" in out
    assert "Total" in out
    assert "$" in out
    assert "\x1b" not in out             # plain string, no ANSI


def test_format_cost_unknown_model_flagged():
    trace = _trace({"input": 1000, "output": 200})
    out = views.format_cost(trace, "some-other-model")
    assert "unknown id" in out.lower()


# --- /export: transcript → Markdown -----------------------------------------

def test_format_transcript_markdown_empty():
    assert views.format_transcript_markdown(GameState(location="Void")) == ""


def test_format_transcript_markdown_renders_exchange():
    gs = GameState(location="Crypt")
    gs.transcript = [
        {"kind": "player", "text": "Aldric opens the door."},
        {"kind": "dm", "text": "The door groans wide onto darkness."},
        {"kind": "player", "text": "He steps through."},
        {"kind": "dm", "text": "Cold air rushes past. *Something* stirs ahead."},
    ]
    md = views.format_transcript_markdown(gs)
    assert md.startswith("# DM Agent — Session Log")
    assert "*2 turns*" in md                       # two player turns
    assert "**You:** Aldric opens the door." in md
    assert "The door groans wide onto darkness." in md
    assert "**You:** He steps through." in md
    assert "*Something* stirs ahead." in md         # DM markdown preserved verbatim
    assert md.endswith("\n")


def test_format_transcript_markdown_singular_turn():
    gs = GameState(location="Hall")
    gs.transcript = [
        {"kind": "player", "text": "Look around."},
        {"kind": "dm", "text": "A bare stone hall."},
    ]
    md = views.format_transcript_markdown(gs)
    assert "*1 turn*" in md                          # singular, not "1 turns"


def test_format_transcript_markdown_skips_blank_entries():
    gs = GameState(location="Hall")
    gs.transcript = [
        {"kind": "player", "text": "  "},            # whitespace-only → skipped
        {"kind": "dm", "text": "The torch gutters."},
    ]
    md = views.format_transcript_markdown(gs)
    assert "**You:**" not in md
    assert "The torch gutters." in md


# --- spell display: known spells grouped by level with slot budget ----------

def _caster() -> Character:
    return Character(
        name="Wisp", max_hp=16, hp=16, ac=12,
        spell_slots={1: 2, 2: 1}, max_spell_slots={1: 2, 2: 2},
        spellcasting_ability="int",
        spells=["fire_bolt", "ray_of_frost", "magic_missile", "chromatic_orb", "scorching_ray"],
    )


def test_known_spells_grouped_and_ordered():
    rows = views._known_spells_by_level(_caster())
    assert [label for label, _, _ in rows] == ["Cantrips", "L1 (2/2)", "L2 (1/2)"]
    assert rows[0][1] == "Fire Bolt, Ray of Frost"           # cantrips first, name-sorted
    assert rows[1][1] == "Chromatic Orb, Magic Missile"      # display names, sorted
    assert rows[2][1] == "Scorching Ray"
    assert all(tapped is False for _, _, tapped in rows)     # all levels have slots


def test_known_spells_tapped_level_flagged():
    c = _caster()
    c.spell_slots = {1: 2, 2: 0}
    l2 = next(r for r in views._known_spells_by_level(c) if r[0].startswith("L2"))
    assert l2[0] == "L2 (0/2)"
    assert l2[2] is True   # known but uncastable from this level now → dimmed in rich


def test_known_spells_slot_fraction_uses_cap():
    """The fraction is current/max — max from max_spell_slots, not the live count."""
    c = _caster()
    c.spell_slots = {1: 0, 2: 1}     # L1 spent
    rows = dict((label.split()[0], label) for label, _, _ in views._known_spells_by_level(c))
    assert rows["L1"] == "L1 (0/2)"


def test_known_spells_untabled_bucketed_last():
    c = _caster()
    c.spells.append("feather_fall")   # not in the SPELLS table
    rows = views._known_spells_by_level(c)
    assert rows[-1][0] == "?"
    assert "Feather Fall" in rows[-1][1]   # id title-cased as a fallback name


def test_known_spells_empty_for_non_caster():
    assert views._known_spells_by_level(Character(name="Fighter")) == []


def test_print_state_plain_groups_spells(capsys):
    views.set_plain(True)
    gs = GameState(location="Tower")
    gs.party["wisp"] = _caster()
    views.print_state(gs)
    out = capsys.readouterr().out
    assert "Spells [int]" in out
    assert "Cantrips  Fire Bolt, Ray of Frost" in out
    assert "L1 (2/2)  Chromatic Orb, Magic Missile" in out
    assert "L2 (1/2)  Scorching Ray" in out
    assert ESC not in out


def test_print_state_rich_groups_spells(monkeypatch, capsys):
    _force_rich(monkeypatch)
    gs = GameState(location="Tower")
    gs.party["wisp"] = _caster()
    views.print_state(gs)
    out = capsys.readouterr().out
    assert "Cantrips" in out and "Magic Missile" in out and "L2 (1/2)" in out


def test_print_state_rich_colored_inventory_and_npc_atk_render(monkeypatch, capsys):
    """The colored /state paths — per-item inventory markup (gear vs. consumable),
    AC, and the hostile-only atk color — must render without markup errors and keep
    their content under the no-color console (a malformed tag would raise or drop text)."""
    _force_rich(monkeypatch)
    gs = GameState(location="Hall")
    gs.party["wisp"] = Character(name="Wisp", max_hp=16, hp=16, ac=12,
                                 inventory=["dagger", "healing_potion"])
    gs.npcs["grik"] = NPC(name="Grik", max_hp=18, hp=18, hostile=True, inventory=["shortsword"])
    views.print_state(gs)
    out = capsys.readouterr().out
    assert "dagger" in out and "healing_potion (consumable)" in out   # gear + consumable
    assert "AC 12" in out
    assert "Grik" in out and "shortsword" in out and "atk +3" in out  # hostile atk path


# --- "block owns slots": header drops slots for casters; upcast-only rows -----

def test_known_spells_upcast_only_level_shown():
    """A slot level the caster knows no spell at still appears (castable by upcast)."""
    c = Character(name="Mage", spells=["magic_missile"],
                  spell_slots={1: 2, 2: 1}, max_spell_slots={1: 2, 2: 1},
                  spellcasting_ability="int")
    rows = views._known_spells_by_level(c)
    assert [label for label, _, _ in rows] == ["L1 (2/2)", "L2 (1/1)"]
    assert rows[1][1] == "— upcast only"
    assert rows[1][2] is False   # has a slot → castable by upcast, not dimmed


def test_known_spells_upcast_only_tapped_when_no_slots():
    c = Character(name="Mage", spells=["magic_missile"],
                  spell_slots={1: 2, 2: 0}, max_spell_slots={1: 2, 2: 1})
    l2 = next(r for r in views._known_spells_by_level(c) if r[0].startswith("L2"))
    assert l2 == ("L2 (0/1)", "— upcast only", True)


def test_print_state_plain_caster_drops_header_slots(capsys):
    views.set_plain(True)
    gs = GameState(location="Tower")
    gs.party["wisp"] = _caster()
    views.print_state(gs)
    out = capsys.readouterr().out
    header = next(ln for ln in out.splitlines() if ln.strip().startswith("Wisp:"))
    assert "slots" not in header   # the block owns the caster's slots now
    assert "Spells [int]" in out   # block present instead


def test_print_state_plain_non_caster_keeps_header_slots(capsys):
    views.set_plain(True)
    gs = GameState(location="Hall")
    gs.party["rogue"] = Character(name="Rogue", max_hp=20, hp=20, ac=14,
                                  spell_slots={1: 1}, max_spell_slots={1: 1})
    views.print_state(gs)
    out = capsys.readouterr().out
    header = next(ln for ln in out.splitlines() if ln.strip().startswith("Rogue:"))
    assert "slots L1:1/1" in header   # no spell block → header keeps the readout
