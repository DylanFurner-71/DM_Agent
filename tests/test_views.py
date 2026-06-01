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


def test_print_state_plain_unchanged(capsys):
    views.set_plain(True)
    views.print_state(_state())
    out = capsys.readouterr().out
    assert "Aldric: HP 7/24" in out
    assert "Grik (NPC)" in out
    assert "hostile" in out
    assert ESC not in out


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
    """Drive the rich branches under pytest capture. rich detects the non-tty sink
    and emits no ANSI, so we exercise the code path and assert content, not color."""
    if not views._RICH:
        pytest.skip("rich not installed")
    monkeypatch.setattr(views, "_rich_on", lambda: True)


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
