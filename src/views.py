"""The view layer — render game state and observability data to the terminal.

These are the presentation helpers behind the in-session commands (`/help`,
`/state`, `/recap`, `/roll`), the compact status HUD shown before each prompt,
and the tool/stats traces (`/trace`, `/full_trace`). They only read state and
write to stdout; the REPL controller lives in `main.py`.
"""

from __future__ import annotations

import sys

from .game_state import GameState
from .rules import CONSUMABLES, SPELLS, roll

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
    ("/cost", "summarize session token usage and estimated cost"),
    ("/export [path]", "write the story so far to a shareable Markdown log"),
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


def _pc_slots(c) -> str:
    """Spell slots as 'LN:current/max', surfacing the per-level cap (max_spell_slots)."""
    caps = getattr(c, "max_spell_slots", {}) or {}
    parts = [f"L{lvl}:{n}/{caps.get(lvl, n)}" for lvl, n in sorted(c.spell_slots.items())]
    return ", ".join(parts) or "none"


def _spell_name(spell_id: str) -> str:
    """Display name for a known spell: the SPELLS table's name, else the id title-cased."""
    entry = SPELLS.get(spell_id)
    if entry and entry.get("name"):
        return entry["name"]
    return spell_id.replace("_", " ").title()


def _known_spells_by_level(c) -> list[tuple[str, str, bool]]:
    """Build the spell-block rows for a caster: known spells grouped by level, with
    that level's slot budget — the block is the single home for a caster's slots.

    Returns [] for a non-caster (no known spells) so the caller knows to show no
    block and keep the header's `slots` line instead. For a caster, returns ordered
    (label, names, tapped) rows:
      - cantrips first ('Cantrips', no fraction — they cost no slot),
      - then every leveled slot in ascending order, labelled with its own budget
        ('L1 (2/2)'). A level the caster has SLOTS for but knows no spell at shows
        '— upcast only' (the slot is still castable by upcasting a lower spell), so
        nothing the header used to carry is lost,
      - then any spell not in the SPELLS table under '?'.
    `tapped` is True for a leveled row whose current slots are 0 — castable from
    that level only once refreshed (the rich view dims it). names uses the SPELLS
    display name (title-cased id fallback), name-sorted and comma-joined.
    """
    if not c.spells:
        return []
    caps = getattr(c, "max_spell_slots", {}) or {}
    buckets: dict = {}   # level (int) or None (untabled) -> [display names]
    for sid in c.spells:
        entry = SPELLS.get(sid)
        lvl = entry.get("level") if entry else None
        buckets.setdefault(lvl, []).append(_spell_name(sid))

    rows: list[tuple[str, str, bool]] = []
    if 0 in buckets:
        rows.append(("Cantrips", ", ".join(sorted(buckets.pop(0))), False))
    untabled = buckets.pop(None, None)
    # Every leveled row: the union of levels the caster knows a spell at and levels
    # they hold a slot for, so an upcast-only slot still appears.
    leveled = sorted({k for k in buckets} | set(c.spell_slots) | set(caps))
    for lvl in leveled:
        if lvl < 1:
            continue
        cur = c.spell_slots.get(lvl, 0)
        mx = caps.get(lvl, cur)
        names = ", ".join(sorted(buckets[lvl])) if lvl in buckets else "— upcast only"
        rows.append((f"L{lvl} ({cur}/{mx})", names, cur == 0))
    if untabled:
        rows.append(("?", ", ".join(sorted(untabled)), False))
    return rows


def _pc_status(c) -> str:
    """Health summary with the death-save lifecycle made explicit: a dying PC shows
    its running successes/failures, a stabilized one reads 'stable', a corpse 'dead'.
    Other conditions (poisoned, prone, …) are appended; the raw 'unconscious'/'dead'
    tags are folded into the lifecycle word so they don't double up."""
    if getattr(c, "dead", False):
        tags = ["dead"]
    elif c.is_dying:
        tags = [f"dying ({c.death_save_successes}✓ {c.death_save_failures}✗)"]
    elif getattr(c, "stable", False) and c.is_down:
        tags = ["stable"]
    else:
        tags = []
    tags += [x for x in c.conditions if x not in ("unconscious", "dead")]
    return ", ".join(tags) or "ok"


def _npc_descriptor(n) -> tuple[str, str]:
    """(kind, disposition) for an NPC line — distinguishes a recruited companion
    ('ally'/'companion') from a plain hostile or neutral bystander."""
    if getattr(n, "companion", False):
        return "ally", "companion"
    return "NPC", ("hostile" if n.hostile else "friendly")


def _scene_nav(state) -> tuple[list[str], list[str]]:
    """(exit labels, loot ids) for the current scene; both empty in a free-form or
    terminal (exit-less) scene. Hidden hazards/DCs are deliberately not surfaced."""
    scene = state.scenes.get(state.current_scene, {}) if state.current_scene else {}
    return list(scene.get("exits", {}).keys()), list(scene.get("loot", []) or [])


def _print_state_plain(state: GameState) -> None:
    print(f"\n  Location: {state.location}")
    exits, loot = _scene_nav(state)
    if exits:
        print(f"  Exits: {', '.join(exits)}")
    if loot:
        print(f"  Loot here: {', '.join(loot)}")
    for c in state.party.values():
        # Casters carry their slots in the spell block below; non-casters (no known
        # spells) keep the header's slots segment as their only slot readout.
        slots_seg = "" if c.spells else f" | slots {_pc_slots(c)}"
        print(f"  {c.name}: HP {c.hp}/{c.max_hp} | AC {c.ac}{slots_seg} | {_pc_status(c)}")
        def _fmt_item(item: str) -> str:
            return f"{item} (consumable)" if item.lower() in CONSUMABLES else item
        inv = ", ".join(_fmt_item(i) for i in c.inventory) if c.inventory else "—"
        print(f"    Inventory: {inv}")
        rows = _known_spells_by_level(c)
        if rows:
            ability = f" [{c.spellcasting_ability}]" if c.spellcasting_ability else ""
            width = max(len(label) for label, _, _ in rows)
            print(f"    Spells{ability}")
            for label, names, _ in rows:
                print(f"      {label:<{width}}  {names}")
    for n in state.npcs.values():
        kind, disposition = _npc_descriptor(n)
        status = " [down]" if n.is_down else ""
        print(f"  {n.name} ({kind}){status}: HP {n.hp}/{n.max_hp} | AC {n.ac} | atk +{n.attack_bonus} | {disposition}")
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
    exits, loot = _scene_nav(state)
    if exits:
        con.print(f"  Exits: {escape(', '.join(exits))}")
    if loot:
        con.print(f"  Loot here: [yellow]{escape(', '.join(loot))}[/yellow]")
    for c in state.party.values():
        # Casters carry their slots in the spell block below; non-casters keep the
        # header's slots segment as their only slot readout.
        slots_seg = "" if c.spells else f" | slots {escape(_pc_slots(c))}"
        con.print(
            f"  [bold cyan]{escape(c.name)}[/bold cyan]: "
            f"HP {c.hp}/{c.max_hp} | AC {c.ac}{slots_seg} | {escape(_pc_status(c))}"
        )
        def _fmt_item(item: str) -> str:
            return f"{item} (consumable)" if item.lower() in CONSUMABLES else item
        inv = ", ".join(_fmt_item(i) for i in c.inventory) if c.inventory else "—"
        con.print(f"    Inventory: {escape(inv)}")
        rows = _known_spells_by_level(c)
        if rows:
            ability = f" [{c.spellcasting_ability}]" if c.spellcasting_ability else ""
            width = max(len(label) for label, _, _ in rows)
            con.print(f"    Spells{escape(ability)}")
            for label, names, tapped in rows:
                line = f"      {label:<{width}}  {escape(names)}"
                con.print(f"[dim]{line}[/dim]" if tapped else line)
    for n in state.npcs.values():
        kind, disposition = _npc_descriptor(n)
        color = "cyan" if kind == "ally" else ("red" if n.hostile else "green")
        status = " [dim](down)[/dim]" if n.is_down else ""
        con.print(
            f"  [{color}]{escape(n.name)}[/{color}] ({kind}){status}: "
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


# ---------------------------------------------------------------------------
# /cost — session token + estimated-spend summary
# ---------------------------------------------------------------------------

# Published Anthropic list prices in USD per 1M tokens (input, output), keyed by
# model-id prefix so a minor version bump (…-4-6 → …-4-7) still matches. Cache
# pricing is derived from the standard 5-minute multipliers (writes 1.25x input,
# reads 0.10x input), so only the base input/output rates live here. Update when
# the MODEL constant or Anthropic's prices change; estimate only, never billed.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4":   {"input": 15.0, "output": 75.0},
    "claude-sonnet-4": {"input": 3.0,  "output": 15.0},
    "claude-haiku-4":  {"input": 1.0,  "output": 5.0},
}
_DEFAULT_PRICING = {"input": 3.0, "output": 15.0}  # Sonnet-class fallback for an unknown id
_CACHE_WRITE_MULT = 1.25   # 5-minute cache write surcharge over base input
_CACHE_READ_MULT = 0.10    # cache hit discount off base input


def _price_for(model: str) -> dict[str, float]:
    for prefix, price in MODEL_PRICING.items():
        if model.startswith(prefix):
            return price
    return _DEFAULT_PRICING


def aggregate_usage(full_trace: list) -> dict:
    """Sum token usage, call count, and wall time across every API call in the trace."""
    tot = {"calls": 0, "elapsed": 0.0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for entry in full_trace:
        for ac in entry.get("api_calls", []):
            tot["calls"] += 1
            tot["elapsed"] += ac.get("elapsed", 0.0)
            u = ac.get("usage", {})
            tot["input"] += u.get("input", 0)
            tot["output"] += u.get("output", 0)
            tot["cache_read"] += u.get("cache_read", 0)
            tot["cache_write"] += u.get("cache_write", 0)
    return tot


def estimate_cost(usage: dict, model: str) -> dict:
    """Estimated USD cost per token bucket (+ 'total') for aggregated usage."""
    p = _price_for(model)
    pin, pout = p["input"], p["output"]
    cost = {
        "input": usage["input"] / 1e6 * pin,
        "cache_write": usage["cache_write"] / 1e6 * pin * _CACHE_WRITE_MULT,
        "cache_read": usage["cache_read"] / 1e6 * pin * _CACHE_READ_MULT,
        "output": usage["output"] / 1e6 * pout,
    }
    cost["total"] = sum(cost.values())
    return cost


def format_cost(full_trace: list, model: str) -> str:
    """A session token + estimated-cost summary built from the stats trace.

    Pure string builder (like format_hud) so it's testable without I/O; returns a
    'nothing yet' line before any API call. Cost is an estimate from MODEL_PRICING,
    not a billed figure.
    """
    u = aggregate_usage(full_trace)
    if u["calls"] == 0:
        return "  No API calls recorded yet — play a turn first."
    c = estimate_cost(u, model)
    total_tokens = u["input"] + u["cache_write"] + u["cache_read"] + u["output"]
    known = any(model.startswith(prefix) for prefix in MODEL_PRICING)
    rows = [
        ("Input",       u["input"],       c["input"]),
        ("Cache write", u["cache_write"], c["cache_write"]),
        ("Cache read",  u["cache_read"],  c["cache_read"]),
        ("Output",      u["output"],      c["output"]),
    ]
    lines = [
        f"  Session cost — model {model}" + ("" if known else " (unknown id; Sonnet-rate estimate)"),
        f"    {u['calls']} API call{'s' * (u['calls'] != 1)} over {u['elapsed']:.1f}s",
    ]
    for label, toks, dollars in rows:
        lines.append(f"    {label:<12} {toks:>9,} tok   ${dollars:.4f}")
    lines.append(f"    {'Total':<12} {total_tokens:>9,} tok   ${c['total']:.4f}")
    lines.append("    (estimate; cache write 1.25x / read 0.10x base input rate)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /export — the playthrough as a shareable Markdown session log
# ---------------------------------------------------------------------------

def format_transcript_markdown(state: GameState) -> str:
    """Render the session transcript as a shareable Markdown document.

    Reads state.transcript ([{kind: 'player'|'dm', text}], chronological) — the same
    record /recap replays — and lays the exchange out as Markdown: each player input
    as a bold "You:" line, each DM beat as its own prose paragraph(s). Pure string
    builder (no I/O), and returns "" when nothing has been played yet so the caller
    can skip writing an empty file. The DM text is already leak-screened in storage.
    """
    transcript = state.transcript
    if not transcript:
        return ""
    turns = sum(1 for e in transcript if e.get("kind") == "player")
    lines = [
        "# DM Agent — Session Log",
        "",
        f"*{turns} turn{'s' * (turns != 1)}*",
        "",
        "---",
        "",
    ]
    for entry in transcript:
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"**You:** {text}" if entry.get("kind") == "player" else text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
