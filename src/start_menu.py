"""The launch start menu — an arrow-key welcome page shown by `python -m src.main`
when invoked with no scenario argument on an interactive terminal.

It lets the player pick from the shipped adventures in `data/adventures/`, resume
their last autosaved game, or load any other savegame. The selector reads single
keypresses in raw mode (stdlib `termios`/`tty`) and redraws a highlighted list via
`rich`; it degrades to a numbered `input()` prompt when raw mode isn't available
(e.g. stock Windows). All terminal concerns live here — `main.py` calls
`run_start_menu()` and gets back a path to load, or None if the player quit.

The pure model helpers (`_load_adventures`, `_build_landing`, `_other_saves`) take
no input and are unit-tested without a TTY.
"""

from __future__ import annotations

import json
import os
import select as _select  # aliased — this module defines its own select() below
import sys
from pathlib import Path

from . import views

# Raw-mode single-keypress reading is POSIX-only. Absent it, select() falls back
# to a numbered prompt, so the menu still works (just without live arrow keys).
try:
    import termios
    import tty
    _RAW_OK = True
except ImportError:  # pragma: no cover - non-POSIX (e.g. stock Windows)
    _RAW_OK = False

try:
    from rich.console import Group
    from rich.text import Text
    from rich.live import Live
    _RICH = True
except ImportError:  # pragma: no cover - rich is a soft dependency
    _RICH = False

ADVENTURES_DIR = Path("data/adventures")
SAVE_DIR = Path("saves")
AUTOSAVE_NAME = "autosave"

# Sentinels stored as a landing row's "action" instead of a scenario path.
_OTHER_SAVES = "__OTHER_SAVES__"


# --- model (pure, TTY-free) ---------------------------------------------------

def _prettify(stem: str) -> str:
    """Filename stem → a readable fallback title, e.g. 'emberdeep_mine' → 'Emberdeep Mine'."""
    return stem.replace("_", " ").title()


def _load_adventures(directory: Path = ADVENTURES_DIR) -> list[tuple[str, str, str]]:
    """Return sorted (path, title, blurb) for every adventure JSON in `directory`.

    `title` falls back to a prettified filename and `blurb` to '' when the fields
    are missing or the file won't parse, so an old or malformed adventure still
    lists rather than vanishing from the menu.
    """
    directory = Path(directory)
    out: list[tuple[str, str, str]] = []
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        title, blurb = _prettify(path.stem), ""
        try:
            d = json.loads(path.read_text())
            title = d.get("title") or title
            blurb = d.get("blurb") or ""
        except (OSError, json.JSONDecodeError):
            pass
        out.append((str(path), title, blurb))
    return out


def _other_saves(save_dir: Path = SAVE_DIR, autosave_name: str = AUTOSAVE_NAME) -> list[Path]:
    """Sorted savegames in `save_dir`, excluding the rolling autosave and the
    `_stats_trace.json` sidecars (which aren't loadable as games)."""
    save_dir = Path(save_dir)
    if not save_dir.is_dir():
        return []
    skip = f"{autosave_name}.json"
    return sorted(
        p for p in save_dir.glob("*.json")
        if p.name != skip and not p.name.endswith("_stats_trace.json")
    )


def _build_landing(
    adventures: list[tuple[str, str, str]],
    save_dir: Path = SAVE_DIR,
    autosave_name: str = AUTOSAVE_NAME,
) -> list[tuple[str, str]]:
    """Build the ordered landing rows as (label, action) pairs.

    `action` is a scenario path to load, or the _OTHER_SAVES sentinel. Resume and
    Load-other-saves rows appear only when there's something to resume/load.
    """
    save_dir = Path(save_dir)
    autosave = save_dir / f"{autosave_name}.json"
    rows: list[tuple[str, str]] = []
    if autosave.exists():
        rows.append(("↩  Resume last game", str(autosave)))
    for path, title, blurb in adventures:
        rows.append((f"{title}  —  {blurb}" if blurb else title, path))
    if _other_saves(save_dir, autosave_name):
        rows.append(("📁  Load other saves…", _OTHER_SAVES))
    return rows


# --- input / rendering --------------------------------------------------------

def _read_key() -> str:
    """Read one keypress in raw mode; return 'up' / 'down' / 'enter' / 'quit' / 'other'.

    Restores the terminal in a finally so a raised exception can't leave it in
    raw mode. A bare ESC (no following bytes within a brief window) and Ctrl-C / q
    all map to 'quit'; ↑/↓ arrows and k/j map to up/down.
    """
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        # Read at the raw fd level (not sys.stdin) so the buffered reader can't
        # swallow the rest of an escape sequence where select() can't see it.
        ch = os.read(fd, 1)
        if ch == b"\x1b":  # ESC: bare quit, or the lead byte of an arrow sequence
            if _select.select([fd], [], [], 0.05)[0]:
                seq = os.read(fd, 2)
                if seq == b"[A":
                    return "up"
                if seq == b"[B":
                    return "down"
            return "quit"
        if ch in (b"\r", b"\n"):
            return "enter"
        if ch in (b"\x03", b"q", b"Q"):  # Ctrl-C or q
            return "quit"
        if ch in (b"k", b"K"):
            return "up"
        if ch in (b"j", b"J"):
            return "down"
        return "other"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _render(title: str, options: list[str], idx: int) -> "Group":
    lines = [Text(title, style="bold"), Text("")]
    for i, opt in enumerate(options):
        if i == idx:
            lines.append(Text(f" ❯ {opt}", style="bold cyan"))
        else:
            lines.append(Text(f"   {opt}", style="dim"))
    lines.append(Text(""))
    lines.append(Text("   ↑/↓ move · enter select · q quit", style="dim italic"))
    return Group(*lines)


def _select_numbered(title: str, options: list[str]) -> int | None:
    """input()-based fallback selector: returns a 0-based index or None to cancel."""
    print()
    print(title)
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    while True:
        try:
            raw = input("Select a number (or q to quit): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if raw in ("q", "quit"):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        if raw:
            print("  Not a valid choice.")


def select(title: str, options: list[str]) -> int | None:
    """Show an arrow-key list; return the chosen 0-based index, or None if cancelled.

    Uses a live rich-rendered, raw-mode selector when available; otherwise falls
    back to a numbered prompt.
    """
    if not options:
        return None
    if not (_RAW_OK and _RICH and views._rich_on()):
        return _select_numbered(title, options)
    idx = 0
    con = views._console()
    with Live(_render(title, options, idx), console=con, auto_refresh=False, screen=False) as live:
        while True:
            live.update(_render(title, options, idx), refresh=True)
            key = _read_key()
            if key == "up":
                idx = (idx - 1) % len(options)
            elif key == "down":
                idx = (idx + 1) % len(options)
            elif key == "enter":
                return idx
            elif key == "quit":
                return None


# --- orchestration ------------------------------------------------------------

def _choose_other_save(save_dir: Path, autosave_name: str) -> str | None:
    """Second page: pick a save to load, or None to go back to the landing page."""
    saves = _other_saves(save_dir, autosave_name)
    labels = ["←  Go back"] + [p.name for p in saves]
    idx = select("Load a save", labels)
    if idx is None or idx == 0:  # cancelled or 'Go back'
        return None
    return str(saves[idx - 1])


def run_start_menu(save_dir: Path = SAVE_DIR, autosave_name: str = AUTOSAVE_NAME) -> str | None:
    """Drive the start menu; return the chosen scenario/save path, or None to quit."""
    adventures = _load_adventures()
    while True:
        rows = _build_landing(adventures, save_dir, autosave_name)
        if not rows:
            return None
        idx = select("Choose your adventure", [label for label, _ in rows])
        if idx is None:
            return None
        action = rows[idx][1]
        if action == _OTHER_SAVES:
            chosen = _choose_other_save(save_dir, autosave_name)
            if chosen is None:
                continue  # 'Go back' → redraw the landing page
            return chosen
        return action
