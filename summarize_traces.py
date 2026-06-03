"""Summarize token cost and wall time of API calls across demo stats-trace files.

Iterates every `demo*stats_trace.json` in a saves directory (default `saves/`),
each of which is a per-turn stats trace produced by the smoke test / `/save`. For
every file it sums the API-call wall time and token usage, and estimates the USD
cost — pricing each call by its OWN recorded model via the engine's own cost
helpers in `src.views` (so a two-model trace is costed correctly, not blended).

The report is printed, then you're prompted for a file name to write it to (default
`trace_summed.txt`) inside the git-ignored `summaries/` directory — give a fresh name
to keep results across runs; an existing file asks before overwriting.

Usage:
    python3 summarize_traces.py                 # scans ./saves
    python3 summarize_traces.py path/to/saves   # custom directory
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

from src import views

# Files start with "demo" and end with "stats_trace.json" (the suffix the engine
# writes for a stats trace). Default model only matters for legacy calls with no
# recorded `model` tag; per-call tags in these traces take precedence.
GLOB = "demo*stats_trace.json"
DEFAULT_MODEL = "claude-sonnet-4-6"
SUMMARY_DIR = Path("summaries")  # all reports are written here (git-ignored)


def summarize_trace(name: str, trace: list) -> dict:
    """Aggregate usage, wall time, and estimated cost for one loaded stats trace."""
    usage = views.aggregate_usage(trace)          # calls, elapsed, token buckets
    cost = views.estimate_cost_mixed(trace, DEFAULT_MODEL)
    tokens = usage["input"] + usage["output"] + usage["cache_read"] + usage["cache_write"]
    return {
        "name": name,
        "calls": usage["calls"],
        "elapsed": usage["elapsed"],
        "tokens": tokens,
        "cost": cost["total"],
    }


def model_breakdown(traces: list) -> dict:
    """Group every API call across all traces by its recorded model, summing calls,
    wall time, tokens, and estimated cost. A call with no `model` tag (legacy traces)
    counts under DEFAULT_MODEL — the same fallback estimate_cost_mixed uses."""
    agg: dict[str, dict] = defaultdict(lambda: {"calls": 0, "elapsed": 0.0, "tokens": 0, "cost": 0.0})
    for trace in traces:
        for turn in trace:
            for ac in turn.get("api_calls", []):
                model = ac.get("model") or DEFAULT_MODEL
                usage = ac.get("usage", {})
                row = agg[model]
                row["calls"] += 1
                row["elapsed"] += ac.get("elapsed", 0.0)
                row["tokens"] += sum(usage.get(k, 0) for k in ("input", "output", "cache_read", "cache_write"))
                row["cost"] += views.estimate_cost(usage, model)["total"]
    return agg


def main() -> int:
    saves_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("saves")
    if not saves_dir.is_dir():
        print(f"No such directory: {saves_dir}")
        return 1

    files = sorted(saves_dir.glob(GLOB))
    if not files:
        print(f"No files matching {GLOB!r} in {saves_dir}/.")
        return 1

    traces = [(f.name, json.loads(f.read_text())) for f in files]
    rows = [summarize_trace(name, trace) for name, trace in traces]

    name_w = max(len(r["name"]) for r in rows)
    header = f"{'file':<{name_w}}  {'calls':>5}  {'tokens':>10}  {'time (s)':>9}  {'cost (USD)':>11}"
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(f"{r['name']:<{name_w}}  {r['calls']:>5}  {r['tokens']:>10,}  "
                     f"{r['elapsed']:>9.2f}  {'$' + format(r['cost'], '.4f'):>11}")
    lines.append("-" * len(header))

    total_calls = sum(r["calls"] for r in rows)
    total_tokens = sum(r["tokens"] for r in rows)
    total_time = sum(r["elapsed"] for r in rows)
    total_cost = sum(r["cost"] for r in rows)
    lines.append(f"{'TOTAL (' + str(len(rows)) + ' files)':<{name_w}}  {total_calls:>5}  "
                 f"{total_tokens:>10,}  {total_time:>9.2f}  {'$' + format(total_cost, '.4f'):>11}")

    # Per-model breakdown — splits every API call by the model that served it, so the
    # two-model split (fast tool-selection vs quality narration) is visible rather than
    # blended into one number. Percentages are of the grand totals above.
    agg = model_breakdown([t for _, t in traces])
    mname_w = max((len(m) for m in agg), default=5)
    mheader = (f"\nBy model:\n{'model':<{mname_w}}  {'calls':>5} {'%':>4}  "
               f"{'tokens':>10}  {'time (s)':>9} {'%':>4}  {'cost (USD)':>11} {'%':>4}")
    lines.append(mheader)
    lines.append("-" * (len(mheader) - len("\nBy model:\n")))
    for model, a in sorted(agg.items(), key=lambda kv: -kv[1]["cost"]):
        pc = 100 * a["calls"] / total_calls if total_calls else 0
        pt = 100 * a["elapsed"] / total_time if total_time else 0
        po = 100 * a["cost"] / total_cost if total_cost else 0
        lines.append(f"{model:<{mname_w}}  {a['calls']:>5} {pc:>3.0f}%  {a['tokens']:>10,}  "
                     f"{a['elapsed']:>9.2f} {pt:>3.0f}%  {'$' + format(a['cost'], '.4f'):>11} {po:>3.0f}%")

    lines.append("\nNote: cost is an estimate from src.views.MODEL_PRICING (list prices), never a billed figure.")

    report = "\n".join(lines) + "\n"
    print(report, end="")

    # Prompt for a destination so results can be kept across runs (a fresh name each
    # time) rather than always clobbering one file. Blank input keeps the default.
    # All reports land in the git-ignored summaries/ directory; only the bare file
    # name is taken from the prompt (any leading path is stripped).
    default = "trace_summed.txt"
    try:
        answer = input(f"\nWrite results to which file? (in {SUMMARY_DIR}/) [{default}]: ").strip()
    except EOFError:
        answer = ""  # non-interactive (piped/CI) — fall back to the default
    SUMMARY_DIR.mkdir(exist_ok=True)
    name = Path(answer or default).name  # ignore any directory the user typed
    out_path = SUMMARY_DIR / name
    if not out_path.suffix:
        out_path = out_path.with_suffix(".txt")
    if out_path.exists():
        try:
            if input(f"{out_path} exists — overwrite? [y/N]: ").strip().lower() not in ("y", "yes"):
                print("Aborted; nothing written.")
                return 0
        except EOFError:
            pass  # non-interactive — proceed with the overwrite
    out_path.write_text(report)
    print(f"Wrote summary to {out_path} ({len(rows)} files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
