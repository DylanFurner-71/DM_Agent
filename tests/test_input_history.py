"""Tests for the readline input-history helper — no API, no real terminal needed."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.main import _init_input_history

readline = pytest.importorskip("readline")  # skip on platforms without readline


def test_init_returns_true_when_readline_present(tmp_path):
    assert _init_input_history(tmp_path / ".hist") is True


def test_init_no_history_file_is_fine(tmp_path):
    """A first run (no existing history file) must not raise and must not create one
    just by reading — the file appears only when history is written at exit."""
    hist = tmp_path / ".hist"
    assert _init_input_history(hist) is True
    assert not hist.exists()


def test_init_creates_parent_dir(tmp_path):
    """The save dir is created lazily; pointing history into a missing dir is fine."""
    hist = tmp_path / "missing_dir" / ".hist"
    assert _init_input_history(hist) is True
    assert hist.parent.is_dir()


def test_init_loads_existing_history(tmp_path):
    """An existing history file is loaded so prior commands are recallable."""
    hist = tmp_path / ".hist"
    readline.clear_history()
    readline.add_history("/state")
    readline.write_history_file(str(hist))
    readline.clear_history()
    assert readline.get_current_history_length() == 0

    assert _init_input_history(hist) is True
    items = [readline.get_history_item(i) for i in range(1, readline.get_current_history_length() + 1)]
    assert "/state" in items


def test_init_survives_unreadable_history(tmp_path):
    """A corrupt/unreadable history file must never abort startup."""
    hist = tmp_path / ".hist"
    hist.mkdir()  # a directory where a file is expected → OSError on read, swallowed
    assert _init_input_history(hist) is True


def test_init_returns_false_without_readline(monkeypatch):
    """When readline can't be imported, the helper degrades quietly to False."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "readline":
            raise ImportError("no readline")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert _init_input_history() is False
