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
