"""Aggregate runs/*/summary.json into delta tables. Delta = leandc - baseline; negative = leandc uses less.

Baselines: dc (default config) and, when present, dc-tuned (DC with a hard 300-line read cap and an 800-line write limit; stricter than leandc on reads).
Stress tasks (weight 0) are listed in their own section and are not part of any weighted total.
Runs flagged "broken" (timeout, API error, server not connected, ...) are left out of every table and listed separately.
"""
import json, statistics as st, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from tasks import BENCH, TASKS

runs, broken = {}, []
for f in sorted((BENCH / "runs").glob("*/summary.json")):
    s = json.load(open(f))
    if s.get("broken"):
        broken.append(s)
    else:
        runs.setdefault((s["task"], s["arm"]), []).append(s)
first_attempts = sorted((BENCH / "runs").glob("*.broken-attempt1.json"))

NAMES = {"dc": "DC", "dc-tuned": "DC tuned", "leandc": "leandc"}
ARMS = [a for a in ("dc", "dc-tuned", "leandc") if any(x == a for (_, x) in runs)]
CORE = [t for t in TASKS if not t.get("stress")]
STRESS = [t for t in TASKS if t.get("stress")]
W = {t["id"]: t["weight"] for t in TASKS}
METRICS = [
    ("Total tokens (input incl. cache + output)", "total_tokens", "{:,.0f}"),
    ("Output tokens (what Claude writes)", "out_tokens", "{:,.0f}"),
    ("Cost USD (API list-price estimate, not what a subscription is billed)", "cost_usd", "{:.4f}"),
    ("Turns", "turns", "{:.1f}"),
    ("Tool calls", "calls", "{:.1f}"),
    ("Wall seconds (includes server start-up; DC starts through npx)", "wall_s", "{:.0f}"),
    ("BUCKET write: est. tokens in call arguments", "write_tok_est", "{:,.0f}"),
    ("BUCKET read: est. tokens in tool results", "read_tok_est", "{:,.0f}"),
    ("BUCKET repeat: est. tokens wasted on identical re-reads", "waste_tok_est", "{:,.0f}"),
]


def med(task, arm, key):
    v = [r[key] for r in runs.get((task, arm), []) if r.get(key) is not None]
    return st.median(v) if v else None


def row(label, a, b, fmt):
    if a is None or b is None:
        return f"| {label} | n/a | n/a | | |"
    d = b - a
    pct = f"{100 * d / a:+.0f}%" if a else "n/a"
    return f"| {label} | {fmt.format(a)} | {fmt.format(b)} | {fmt.format(d)} | {pct} |"


def cell(task, arm, key, fmt):
    v = med(task, arm, key)
    return "n/a" if v is None else fmt.format(v)


def wtotal(arm, key):
    return sum(W[t["id"]] * med(t["id"], arm, key) for t in CORE if med(t["id"], arm, key) is not None)


def table_head(first):
    return f"| {first} | " + " | ".join(NAMES[a] for a in ARMS) + " |\n|---|" + "---|" * len(ARMS)


out = []
if not runs:
    sys.exit("no runs found in " + str(BENCH / "runs"))

meta_p = BENCH / "meta.json"
if meta_p.exists():
    m = json.load(open(meta_p))
    out.append("## What was tested\n")
    out.append(f"Started {m.get('started')} on {m.get('platform')} ({m.get('machine')}). Claude Code CLI {m.get('claude_cli')}, node {m.get('node')}, python {m.get('python')}.")
    out.append(f"leandc {m.get('leandc_version')} (installed copy vs repo source: {m.get('leandc_installed_vs_repo_src')}); repo HEAD {m.get('repo_head')}; "
               f"DC {' '.join(m.get('dc_args', [])[-1:])}; fixture commit {str(m.get('fixture_commit'))[:7]}.")
    models = sorted({r.get("model") for rs in runs.values() for r in rs})
    out.append(f"Model reported by the sessions: {', '.join(map(str, models))}. Repo files modified when the run started: {len(m.get('repo_dirty_files', []))}.\n")

out.append("## Runs counted (broken runs excluded)\n")
out.append(table_head("Task"))
for t in TASKS:
    out.append(f"| {t['id']} | " + " | ".join(str(len(runs.get((t['id'], a), []))) for a in ARMS) + " |")
out.append("")
out.append("Passed: " + ", ".join(f"{NAMES[a]} {sum(r['passed'] for (t, x), v in runs.items() if x == a for r in v)}/{sum(len(v) for (t, x), v in runs.items() if x == a)}" for a in ARMS))
out.append(f"\nBroken runs left out: {len(broken)} (after retry); first attempts that were broken and retried: {len(first_attempts)}.")
for s in broken:
    out.append(f"- {s['task']} {s['arm']} r{s['rep']}: {', '.join(s['broken'])}")
out.append("")

if "leandc" in ARMS:
    for base in [a for a in ("dc", "dc-tuned") if a in ARMS]:
        out.append(f"## {NAMES[base]} vs leandc (core tasks, weighted)\n")
        for title, key, fmt in METRICS:
            out.append(f"### {title}\n\n| Task | {NAMES[base]} | leandc | Δ | Δ % |\n|---|---|---|---|---|")
            tw_a = tw_b = 0.0
            su_a = su_b = 0.0
            for t in CORE:
                a, b = med(t["id"], base, key), med(t["id"], "leandc", key)
                out.append(row(t["id"], a, b, fmt))
                if a is not None and b is not None:
                    tw_a += W[t["id"]] * a
                    tw_b += W[t["id"]] * b
                    su_a += a
                    su_b += b
            out.append(row("**Weighted total**", tw_a, tw_b, fmt))
            out.append(row("Sum (unweighted)", su_a, su_b, fmt))
            out.append("")

if STRESS:
    out.append("## Stress tasks (weight 0, not in any weighted total)\n")
    for title, key, fmt in (("Total tokens", "total_tokens", "{:,.0f}"), ("Tool calls", "calls", "{:.1f}"), ("Turns", "turns", "{:.1f}"), ("Wall seconds", "wall_s", "{:.0f}")):
        out.append(f"### {title}\n\n" + table_head("Task"))
        for t in STRESS:
            out.append(f"| {t['id']} | " + " | ".join(cell(t["id"], a, key, fmt) for a in ARMS) + " |")
        out.append("")

if "leandc" in ARMS:
    out.append("## What if the Chat app adds a fixed block to every turn? (a calculation, not a measurement)\n")
    out.append("Weighted total tokens + X tokens for every turn (the block is sent again on every model turn). Turns are the measured medians.\n")
    out.append("| X per turn | " + " | ".join(NAMES[a] for a in ARMS) + " | leandc vs " + " / ".join(NAMES[a] for a in ARMS if a != "leandc") + " |\n|---|" + "---|" * (len(ARMS) + 1))
    for x in (0, 5000, 10000, 20000):
        tot = {a: wtotal(a, "total_tokens") + x * wtotal(a, "turns") for a in ARMS}
        rel = " / ".join(f"{100 * (tot['leandc'] - tot[a]) / tot[a]:+.0f}%" for a in ARMS if a != "leandc" and tot[a])
        out.append(f"| {x:,} | " + " | ".join(f"{tot[a]:,.0f}" for a in ARMS) + f" | {rel} |")
    out.append("")

for title, key, fmt in (("Spread (min-max over reps): total tokens", "total_tokens", "{:,.0f}"), ("Spread (min-max over reps): cost USD", "cost_usd", "{:.4f}")):
    out.append(f"### {title}\n\n" + table_head("Task"))
    for t in TASKS:
        def rng(arm):
            vals = [r[key] for r in runs.get((t["id"], arm), []) if r.get(key) is not None]
            return "n/a" if not vals else " - ".join(fmt.format(x) for x in (min(vals), max(vals)))
        out.append(f"| {t['id']} | " + " | ".join(rng(a) for a in ARMS) + " |")
    out.append("")

out.append("### Repeats over all runs (dup calls, identical-output repeats, changed-output repeats, total calls)\n")
for a in ARMS:
    rep = [sum(r.get(k, 0) for (t, x), rs in runs.items() if x == a for r in rs) for k in ("dup_calls", "dup_identical", "dup_changed", "calls")]
    out.append(f"{NAMES[a]} {rep}  ")
out.append("")
out.append("### Pass / fail\n\n" + table_head("Task"))
for t in TASKS:
    f = lambda arm: "/".join("pass" if r["passed"] else "FAIL" for r in runs.get((t["id"], arm), [])) or "n/a"
    out.append(f"| {t['id']} | " + " | ".join(f(a) for a in ARMS) + " |")

out.append("\n### Sanity checks\n")
allr = [r for rs in runs.values() for r in rs]
bad = [(s["task"], s["arm"], s["in_tokens"], s["usage_sum_check_in"]) for s in allr if "in_tokens" in s and s["in_tokens"] != s["usage_sum_check_in"]]
out.append("- Session total vs sum of per-message usage (input side): " + ("all runs match" if not bad else "mismatch: " + str(bad)))
out.append("- Tools the model could see, per arm: " + "; ".join(f"{NAMES[a]} {sorted({r.get('n_tools') for rs in [v for (t, x), v in runs.items() if x == a] for r in rs})}" for a in ARMS))
out.append(f"- Built-in tool calls (should be 0): {sum(r.get('builtin_calls', 0) for r in allr)}")
out.append(f"- Runs that hit the 40-turn limit: {sum(bool(r.get('hit_max_turns')) for r in allr)}")
out.append(f"- Runs needing a retry: {sum(r.get('attempts', 1) > 1 for r in allr)}")

timed = sorted((r for r in allr if r.get("ts") and r.get("util_5h") is not None), key=lambda r: r["ts"])
if timed:
    out.append("\n### Usage bar during the run (as reported by the CLI after each run)\n")
    a, b = timed[0], timed[-1]
    toks = sum(r.get("total_tokens", 0) for r in allr)
    out.append(f"- 5-hour window: {a['util_5h']:.0%} after the first counted run -> {b['util_5h']:.0%} after the last ({len(timed)} runs, {toks:,} tokens counted; the window may have reset in between).")
    out.append(f"- Weekly window: {a['util_week']:.0%} -> {b['util_week']:.0%}.")
    out.append("- Other activity (chat, other sessions) also moves these numbers, so this is a rough guide, not a price per token.")
text = "\n".join(out)
(BENCH / "report.md").write_text(text)
print(text)
