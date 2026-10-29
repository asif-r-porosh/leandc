"""Aggregate runs/*/summary.json into delta tables. Delta = leandc - DC; negative = leandc uses less."""
import json, statistics as st, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from tasks import BENCH, TASKS

runs = {}
for f in sorted((BENCH / "runs").glob("*/summary.json")):
    s = json.load(open(f))
    runs.setdefault((s["task"], s["arm"]), []).append(s)

W = {t["id"]: t["weight"] for t in TASKS}
METRICS = [
    ("Total tokens (input incl. cache + output)", "total_tokens", "{:,.0f}"),
    ("Output tokens (what Claude writes)", "out_tokens", "{:,.0f}"),
    ("Cost USD", "cost_usd", "{:.4f}"),
    ("Turns", "turns", "{:.1f}"),
    ("Tool calls", "calls", "{:.1f}"),
    ("BUCKET write: est. tokens in call arguments", "write_tok_est", "{:,.0f}"),
    ("BUCKET read: est. tokens in tool results", "read_tok_est", "{:,.0f}"),
    ("BUCKET repeat: est. tokens wasted on identical re-reads", "waste_tok_est", "{:,.0f}"),
]


def med(task, arm, key):
    v = [r.get(key) or 0 for r in runs.get((task, arm), [])]
    return st.median(v) if v else None


def row(label, a, b, fmt):
    if a is None or b is None:
        return f"| {label} | n/a | n/a | | |"
    d = b - a
    pct = f"{100 * d / a:+.0f}%" if a else "n/a"
    return f"| {label} | {fmt.format(a)} | {fmt.format(b)} | {fmt.format(d).replace('-', '-')} | {pct} |"


out = []
n = max(len(v) for v in runs.values())
out.append(f"Runs per task per tool: {n}. Passed: " + ", ".join(
    f"{arm} {sum(r['passed'] for (t, a), rs in runs.items() if a == arm for r in rs)}/{sum(len(rs) for (t, a), rs in runs.items() if a == arm)}"
    for arm in ("dc", "leandc")) + "\n")
for title, key, fmt in METRICS:
    out.append(f"### {title}\n\n| Task | DC | leandc | Δ | Δ % |\n|---|---|---|---|---|")
    tw_a = tw_b = 0.0
    for t in TASKS:
        a, b = med(t["id"], "dc", key), med(t["id"], "leandc", key)
        out.append(row(t["id"], a, b, fmt))
        if a is not None and b is not None:
            tw_a += W[t["id"]] * a
            tw_b += W[t["id"]] * b
    out.append(row("**Weighted total**", tw_a, tw_b, fmt))
    out.append(row("Sum (unweighted)", sum(med(t["id"], "dc", key) or 0 for t in TASKS), sum(med(t["id"], "leandc", key) or 0 for t in TASKS), fmt))
    out.append("")
for title, key, fmt in (("Spread (min-max over reps): total tokens", "total_tokens", "{:,.0f}"), ("Spread (min-max over reps): cost USD", "cost_usd", "{:.4f}")):
    out.append(f"### {title}\n\n| Task | DC | leandc |\n|---|---|---|")
    for t in TASKS:
        cell = lambda arm: " - ".join(fmt.format(x) for x in (min(r[key] for r in runs[(t['id'], arm)]), max(r[key] for r in runs[(t['id'], arm)])))
        out.append(f"| {t['id']} | {cell('dc')} | {cell('leandc')} |")
    out.append("")
rep = {a: [sum(r.get(k, 0) for (t, x), rs in runs.items() if x == a for r in rs) for k in ("dup_calls", "dup_identical", "dup_changed", "calls")] for a in ("dc", "leandc")}
out.append("### Repeats over all runs (dup calls, identical-output repeats, changed-output repeats, total calls)\n")
out.append(f"DC {rep['dc']}  leandc {rep['leandc']}\n")
out.append("### Pass / fail\n\n| Task | DC | leandc |\n|---|---|---|")
for t in TASKS:
    f = lambda arm: "/".join("pass" if r["passed"] else "FAIL" for r in runs.get((t["id"], arm), []))
    out.append(f"| {t['id']} | {f('dc')} | {f('leandc')} |")
out.append("\n### Sanity: session total vs sum of per-message usage (input side)\n")
bad = [(s["task"], s["arm"], s["in_tokens"], s["usage_sum_check_in"]) for rs in runs.values() for s in rs if s["in_tokens"] != s["usage_sum_check_in"]]
out.append("all runs match" if not bad else "mismatch: " + str(bad))
text = "\n".join(out)
(BENCH / "report.md").write_text(text)
print(text)
