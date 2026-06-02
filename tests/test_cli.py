"""Tests for the CLI argument surface and the --seed dice-reproducibility hook.

No API and no interactive loop: the parser is built by _make_parser() so the
argument surface is unit-testable, and the seed contract is exercised directly
against rules.roll (the same RNG the flag fixes at startup).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.main import _make_parser
from src import rules


# --- argument parsing ---------------------------------------------------------

def test_seed_flag_parses_to_int():
    args = _make_parser().parse_args(["--seed", "42"])
    assert args.seed == 42


def test_seed_defaults_to_none():
    args = _make_parser().parse_args([])
    assert args.seed is None


def test_seed_coexists_with_scenario_and_other_flags():
    args = _make_parser().parse_args(["data/x.json", "--seed", "7", "--plain", "--no-hud"])
    assert args.scenario == "data/x.json"
    assert args.seed == 7
    assert args.plain is True
    assert args.no_hud is True


# --- the contract the flag relies on -----------------------------------------

def test_same_seed_reproduces_roll_sequence():
    """The whole point of --seed: identical seed → identical dice sequence."""
    rules.force_rolls([])          # clear any queued forced rolls from other tests
    rules.seed(123)
    first = [rules.roll("1d20").total for _ in range(10)]
    rules.seed(123)
    second = [rules.roll("1d20").total for _ in range(10)]
    assert first == second


def test_different_seeds_diverge():
    rules.force_rolls([])
    rules.seed(1)
    a = [rules.roll("1d20").total for _ in range(10)]
    rules.seed(2)
    b = [rules.roll("1d20").total for _ in range(10)]
    assert a != b   # 20**10 collision odds — effectively never equal
