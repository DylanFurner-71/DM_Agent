"""The view layer — render game state and observability data to the terminal.

These are the presentation helpers behind the in-session commands (`/help`,
`/state`, `/recap`, `/roll`), the compact status HUD shown before each prompt,
and the tool/stats traces (`/trace`, `/full_trace`). They only read state and
write to stdout; the REPL controller lives in `main.py`.
"""

from __future__ import annotations

import sys

from .game_state import GameState
from .rules import CONSUMABLES, roll

# Optional rich rendering. The app degrades to plain text when rich isn't
# installed, so it stays a soft dependency — every helper checks _rich_on().
try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.markup import escape
    _RICH = True
except ImportError:  # pragma: no cover - exercised only without rich installed
    _RICH = False

_PLAIN = False          # forced off by --plain
_CONSOLE: "Console | None" = None


def set_plain(plain: bool) -> None:
    """Force plain output (no color/Markdown/spinner). main() also passes True
    automatically when stdout isn't a terminal, so pipes and CI stay clean."""
    global _PLAIN
    _PLAIN = plain


def _rich_on() -> bool:
    """True only when rich is installed, not forced plain, and stdout is a tty."""
    return _RICH and not _PLAIN and sys.stdout.isatty()


def _console() -> "Console":
    global _CONSOLE
    if _CONSOLE is None:
        _CONSOLE = Console()
    return _CONSOLE


def render_markdown(text: str) -> None:
    """Render prose (scene text, narration recap) as Markdown when rich is active,
    else print it verbatim. Caller owns the surrounding blank lines."""
    if not text:
        return
    if _rich_on():
        _console().print(Markdown(text))
    else:
        print(text)


def banner(scenario: str) -> None:
    """The launch header."""
    if _rich_on():
        con = _console()
        con.rule("[bold]DM AGENT[/bold]")
        con.print(f"  type [cyan]/help[/cyan] for commands  ·  scenario: {escape(scenario)}")
        con.rule()
    else:
        print("=" * 60)
        print("  DM AGENT — type /help for commands")
        print(f"  Scenario: {scenario}")
        print("=" * 60)


class Spinner:
    """A pre-stream 'thinking' spinner. Active during the API latency before the
    first narration token; the caller stops it when streaming begins. A no-op in
    plain/non-tty mode or without rich, so it never interferes with piped output."""

    def __init__(self, message: str):
        self.message = message
        self._status = None

    def start(self) -> None:
        if self._status is None and _rich_on():
            self._status = _console().status(self.message, spinner="dots")
            self._status.start()

    def stop(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    def __enter__(self) -> "Spinner":
        self.start()
        return self

    def __exit__(self, *exc) -> bool:
        self.stop()
        return False


_COMMANDS = [
    ("/help", "show this list of commands"),
    ("/state", "show party HP, slots, inventory, NPCs, and combat status"),
    ("/hud", "toggle the compact status HUD shown before each prompt"),
    ("/recap", "replay the story so far (the DM's narration beats)"),
    ("/roll <notation>", "roll dice openly, e.g. /roll 2d6+3 (flavor only — not enforced state)"),
    ("/undo", "rewind the last turn (the game autosaves after every turn)"),
    ("/trace", "show the tools the agent called each turn"),
    ("/full_trace", "show the tool trace with per-call timing and token usage"),
    ("/save [path]", "save the game (prompts for a name if omitted)"),
    ("/quit", "end the session"),
]


def print_help() -> None:
    print("\n  Commands:")
    for cmd, desc in _COMMANDS:
        print(f"    {cmd:<18} — {desc}")
    print()


def print_recap(state: GameState) -> None:
    """Replay the DM's narration beats so far — the story without the mechanics."""
    if not state.narrative:
        print("\n  Nothing has happened yet.\n")
        return
    print("\n  — The story so far —\n")
    if _rich_on():
        for beat in state.narrative:
            render_markdown(beat["text"])
            print()
    else:
        for beat in state.narrative:
            print(f"  {beat['text']}\n")


def print_roll(notation: str) -> None:
    """Roll dice openly via the engine. Flavor/divination only — touches no state."""
    if not notation:
        print("  Usage: /roll <notation>, e.g. /roll 1d20+5\n")
        return
    try:
        r = roll(notation)
    except ValueError as e:
        print(f"  {e}\n")
        return
    if _rich_on():
        _console().print(f"  🎲 [bold cyan]{escape(r.describe())}[/bold cyan]\n")
    else:
        print(f"  🎲 {r.describe()}\n")


def print_state(state: GameState) -> None:
    if _rich_on():
        _print_state_rich(state)
    else:
        _print_state_plain(state)


def _print_state_plain(state: GameState) -> None:
    print(f"\n  Location: {state.location}")
    for c in state.party.values():
        slots = ", ".join(f"L{lvl}:{n}" for lvl, n in sorted(c.spell_slots.items())) or "none"
        status = ", ".join(c.conditions) or "ok"
        print(f"  {c.name}: HP {c.hp}/{c.max_hp} | slots {slots} | {status}")
        def _fmt_item(item: str) -> str:
            return f"{item} (consumable)" if item.lower() in CONSUMABLES else item
        inv = ", ".join(_fmt_item(i) for i in c.inventory) if c.inventory else "—"
        print(f"    Inventory: {inv}")
        if c.spells:
            ability = f" [{c.spellcasting_ability}]" if c.spellcasting_ability else ""
            print(f"    Spells{ability}: {', '.join(c.spells)}")
    for n in state.npcs.values():
        disposition = "hostile" if n.hostile else "friendly"
        status = " [down]" if n.is_down else ""
        print(f"  {n.name} (NPC){status}: HP {n.hp}/{n.max_hp} | AC {n.ac} | atk +{n.attack_bonus} | {disposition}")
        if n.inventory:
            print(f"    Inventory: {', '.join(n.inventory)}")
    if state.combat_round > 0:
        all_actors = {**state.party, **state.npcs}
        order = " → ".join(
            all_actors[k].name if k in all_actors else k
            for k in state.combat_order
        )
        active_key = state.combat_order[state.combat_index]
        active_name = all_actors[active_key].name if active_key in all_actors else active_key
        print(f"  Combat: round {state.combat_round} | {order} | up: {active_name}")
    else:
        print("  Combat: not in combat")
    print()


def _print_state_rich(state: GameState) -> None:
    """Same content as _print_state_plain, with colored names/dispositions. Every
    interpolated string is escape()d so scenario/player text can't inject markup."""
    con = _console()
    con.print(f"\n  Location: [bold]{escape(state.location)}[/bold]")
    for c in state.party.values():
        slots = ", ".join(f"L{lvl}:{n}" for lvl, n in sorted(c.spell_slots.items())) or "none"
        status = ", ".join(c.conditions) or "ok"
        con.print(
            f"  [bold cyan]{escape(c.name)}[/bold cyan]: "
            f"HP {c.hp}/{c.max_hp} | slots {escape(slots)} | {escape(status)}"
        )
        def _fmt_item(item: str) -> str:
            return f"{item} (consumable)" if item.lower() in CONSUMABLES else item
        inv = ", ".join(_fmt_item(i) for i in c.inventory) if c.inventory else "—"
        con.print(f"    Inventory: {escape(inv)}")
        if c.spells:
            ability = f" [{c.spellcasting_ability}]" if c.spellcasting_ability else ""
            con.print(f"    Spells{escape(ability)}: {escape(', '.join(c.spells))}")
    for n in state.npcs.values():
        color = "red" if n.hostile else "green"
        disposition = "hostile" if n.hostile else "friendly"
        status = " [dim](down)[/dim]" if n.is_down else ""
        con.print(
            f"  [{color}]{escape(n.name)}[/{color}] (NPC){status}: "
            f"HP {n.hp}/{n.max_hp} | AC {n.ac} | atk +{n.attack_bonus} | {disposition}"
        )
        if n.inventory:
            con.print(f"    Inventory: {escape(', '.join(n.inventory))}")
    if state.combat_round > 0:
        all_actors = {**state.party, **state.npcs}
        order = " → ".join(
            all_actors[k].name if k in all_actors else k
            for k in state.combat_order
        )
        active_key = state.combat_order[state.combat_index]
        active_name = all_actors[active_key].name if active_key in all_actors else active_key
        con.print(
            f"  Combat: round {state.combat_round} | {escape(order)} | "
            f"up: [bold]{escape(active_name)}[/bold]"
        )
    else:
        con.print("  Combat: not in combat")
    con.print()


def _hp_bar(hp: int, max_hp: int, width: int = 10) -> str:
    """A compact filled/empty HP bar. A sliver always shows while hp > 0 so a
    badly-wounded actor never reads as full-empty until they're actually down."""
    if max_hp <= 0:
        return "░" * width
    filled = max(0, min(width, round(hp / max_hp * width)))
    if hp > 0 and filled == 0:
        filled = 1
    return "█" * filled + "░" * (width - filled)


def _combatant_marker(actor, key: str, state: GameState) -> str:
    """Initiative-order marker: dying/dead for PCs, down/ally for NPCs ('' if none)."""
    if key in state.party:
        if getattr(actor, "dead", False):
            return "(dead)"
        if actor.hp <= 0:
            return "(dying)"
        return ""
    if actor.is_down:
        return "(down)"
    if getattr(actor, "companion", False):
        return "(ally)"
    return ""


def format_hud(state: GameState, width: int = 60) -> str:
    """A compact status header for display before each prompt.

    Shows each PC's HP bar, spell slots, and conditions, and — in combat — the
    round plus the initiative order with the active actor marked (▶) and
    dying/dead/companion markers. Pure reformatting of data `/state` already
    exposes; returns "" for an empty party so the caller can skip printing.
    """
    pcs = list(state.party.values())
    if not pcs:
        return ""
    namew = max(len(c.name) for c in pcs)
    rule = "─" * width
    lines = [rule]
    for c in pcs:
        seg = f"  {c.name:<{namew}}  {_hp_bar(c.hp, c.max_hp)} {c.hp:>3}/{c.max_hp:<3}"
        slots = " ".join(f"L{lvl}:{n}" for lvl, n in sorted(c.spell_slots.items()))
        if slots:
            seg += f"  {slots}"
        tags = []
        if c.dead:
            tags.append("dead")
        elif c.hp <= 0:
            tags.append("dying")
        tags += [x for x in c.conditions if x not in ("unconscious", "dead")]
        if tags:
            seg += f"  [{', '.join(tags)}]"
        lines.append(seg)
    if state.combat_round > 0 and state.combat_order:
        all_actors = {**state.party, **state.npcs}
        active_key = state.combat_order[state.combat_index]
        parts = []
        for k in state.combat_order:
            a = all_actors.get(k)
            label = (a.name if a else k) + (_combatant_marker(a, k, state) if a else "")
            if k == active_key:
                label = f"▶{label}"
            parts.append(label)
        lines.append(f"  ⚔ Round {state.combat_round}: " + " → ".join(parts))
    lines.append(rule)
    return "\n".join(lines)


def print_tool_trace(trace: list) -> None:
    if not trace:
        return
    print("  [tools]")
    for call in trace:
        result_summary = {k: v for k, v in call["result"].items() if k != "state"}
        print(f"    {call['name']}({call['input']}) -> {result_summary}")
    print()


def print_full_trace(full_trace: list) -> None:
    if not full_trace:
        print("  No tool calls recorded yet.\n")
        return
    for entry in full_trace:
        print(f"  [Turn {entry['turn']}] > {entry.get('input', '')}")
        if not entry["calls"]:
            print("    (no tool calls)")
        for call in entry["calls"]:
            result_summary = {k: v for k, v in call["result"].items() if k != "state"}
            print(f"    {call['name']}({call['input']}) -> {result_summary}")
    print()


def _build_stats_trace(full_trace: list) -> list:
    """Build a per-turn stats structure: tool calls + api call timing/tokens."""
    result = []
    for entry in full_trace:
        result.append({
            "turn": entry["turn"],
            "player_input": entry.get("input", ""),
            "tool_calls": [
                {"name": c["name"], "input": c["input"], "result": c["result"]}
                for c in entry["calls"]
            ],
            "api_calls": entry.get("api_calls", []),
        })
    return result


def print_full_trace_verbose(full_trace: list) -> None:
    if not full_trace:
        print("  No tool calls recorded yet.\n")
        return
    for entry in full_trace:
        print(f"  [Turn {entry['turn']}] > {entry.get('input', '')}")
        if not entry["calls"]:
            print("    (no tool calls)")
        for call in entry["calls"]:
            result_summary = {k: v for k, v in call["result"].items() if k != "state"}
            print(f"    {call['name']}({call['input']}) -> {result_summary}")
        for ac in entry.get("api_calls", []):
            u = ac["usage"]
            tokens = f"in={u['input']} out={u['output']}"
            if u.get("cache_read"):
                tokens += f" cache_read={u['cache_read']}"
            if u.get("cache_write"):
                tokens += f" cache_write={u['cache_write']}"
            print(f"    api:{ac['phase']} {ac['elapsed']:.1f}s | {tokens}")
    print()
