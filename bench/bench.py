"""Run benchmark tasks through `claude -p` against exactly one MCP server (dc or leandc), then bucket the calls.

usage: python3 bench.py [--reps N] [--tasks id,id] [--arms dc,leandc]
Each run: fresh copy of fixture-src, fresh Claude Code session, built-in tools off, only one MCP server.
"""
import argparse, hashlib, json, os, shutil, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tasks import BENCH, TASKS

SYSTEM = "You are a coding assistant. Use the provided tools when needed. Be concise."
ARMS = {"dc": ("mcp-dc.json", "mcp__dc"), "leandc": ("mcp-leandc.json", "mcp__leandc")}


def text_of(c):
    if isinstance(c, str):
        return c
    return "".join(b.get("text", "") for b in (c or []) if isinstance(b, dict))


def analyze(streams):
    ev = [json.loads(l) for s in streams for l in open(s) if l.strip()]
    calls, order, msg_usage = {}, [], {}
    for e in ev:
        if e.get("type") == "assistant":
            m = e["message"]
            if m.get("id") and m.get("usage"):
                msg_usage[m["id"]] = m["usage"]
            for b in m["content"]:
                if b.get("type") == "tool_use":
                    calls[b["id"]] = {"name": b["name"], "input": b["input"], "result": None, "error": False}
                    order.append(b["id"])
        elif e.get("type") == "user" and isinstance(e["message"].get("content"), list):
            for b in e["message"]["content"]:
                if b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
                    calls[b["tool_use_id"]]["result"] = text_of(b.get("content"))
                    calls[b["tool_use_id"]]["error"] = bool(b.get("is_error"))
    results = [e for e in ev if e.get("type") == "result"]
    res = dict(results[-1])
    res["num_turns"] = sum(r.get("num_turns", 0) for r in results)
    res["total_cost_usd"] = sum(r.get("total_cost_usd", 0) for r in results)
    u = {}
    for r in results:  # multi-message conversations: one result per invocation, usage is per invocation
        for k, v in r.get("usage", {}).items():
            if isinstance(v, int):
                u[k] = u.get(k, 0) + v
    seen, rows = {}, []
    write_ch = read_ch = waste_ch = dup_calls = same_calls = changed_calls = builtin = errs = 0
    per_tool = {}
    for cid in order:
        c = calls[cid]
        name = c["name"].split("__")[-1] if c["name"].startswith("mcp__") else "BUILTIN:" + c["name"]
        builtin += not c["name"].startswith("mcp__")
        a = len(json.dumps(c["input"], separators=(",", ":")))
        r = len(c["result"] or "")
        errs += c["error"]
        write_ch += a
        read_ch += r
        key = (c["name"], json.dumps(c["input"], sort_keys=True))
        h = hashlib.md5((c["result"] or "").encode()).hexdigest()
        tag = "new"
        if key in seen:
            dup_calls += 1
            if seen[key] == h:
                tag, waste_ch, same_calls = "repeat-identical", waste_ch + r, same_calls + 1
            else:
                tag, changed_calls = "repeat-changed", changed_calls + 1
        seen[key] = h
        t = per_tool.setdefault(name, [0, 0, 0])
        t[0] += 1; t[1] += a; t[2] += r
        rows.append({"tool": name, "arg_ch": a, "res_ch": r, "tag": tag, "error": c["error"]})
    in_total = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
    chk = sum(x.get("input_tokens", 0) + x.get("cache_creation_input_tokens", 0) + x.get("cache_read_input_tokens", 0) for x in msg_usage.values())
    return {
        "is_error": res.get("is_error"), "turns": res.get("num_turns"), "cost_usd": res.get("total_cost_usd"),
        "in_tokens": in_total, "fresh_in": u.get("input_tokens", 0), "cache_create": u.get("cache_creation_input_tokens", 0),
        "cache_read": u.get("cache_read_input_tokens", 0), "out_tokens": u.get("output_tokens", 0),
        "total_tokens": in_total + u.get("output_tokens", 0), "usage_sum_check_in": chk,
        "calls": len(order), "tool_errors": errs, "builtin_calls": builtin, "dup_calls": dup_calls,
        "dup_identical": same_calls, "dup_changed": changed_calls,
        "write_tok_est": write_ch // 4, "read_tok_est": read_ch // 4, "waste_tok_est": waste_ch // 4,
        "per_tool": per_tool, "answer": res.get("result", ""), "rows": rows,
    }


def run_one(task, arm, rep):
    rid = f"{task['id']}-{arm}-r{rep}"
    d = BENCH / "runs" / rid
    if (d / "summary.json").exists():
        print("skip", rid, flush=True)
        return
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    work = d / "work"
    shutil.copytree(BENCH / "fixture-src", work)
    if task.get("setup"):
        task["setup"](work)
    cfg, allow = ARMS[arm]
    prompts = task.get("turns") or [task["prompt"]]
    multi = len(prompts) > 1
    env = {**os.environ, "ENABLE_TOOL_SEARCH": "false"}
    t0 = time.time()
    streams, sid = [], None
    for i, p in enumerate(prompts):
        cmd = ["claude", "-p", p.format(work=work), "--model", "sonnet", "--tools", "", "--strict-mcp-config",
               "--mcp-config", str(BENCH / cfg), "--allowedTools", allow, "--system-prompt", SYSTEM, "--setting-sources", "",
               "--disable-slash-commands", "--output-format", "stream-json", "--verbose", "--max-turns", "40"]
        if not multi:
            cmd.append("--no-session-persistence")
        if i:
            cmd += ["--resume", sid]
        sp = d / f"stream{i}.jsonl"
        with open(sp, "w") as out, open(d / f"stderr{i}.txt", "w") as err:
            try:
                subprocess.run(cmd, cwd=d, env=env, stdout=out, stderr=err, timeout=1500)
            except subprocess.TimeoutExpired:
                print("TIMEOUT", rid, i, flush=True)
        streams.append(sp)
        for l in open(sp):
            if '"type":"result"' in l:
                sid = json.loads(l).get("session_id", sid)
    dur = time.time() - t0
    try:
        s = analyze(streams)
    except Exception as e:  # keep going; the run is recorded as broken
        s = {"is_error": True, "answer": f"analyze failed: {e!r}"}
    try:
        s["passed"] = bool(task["verify"](work, s.get("answer", "")))
    except Exception as e:
        s["passed"], s["verify_error"] = False, repr(e)
    s.update(task=task["id"], arm=arm, rep=rep, wall_s=round(dur, 1))
    (d / "summary.json").write_text(json.dumps(s, indent=1))
    print(f"{rid}: pass={s['passed']} turns={s.get('turns')} calls={s.get('calls')} tokens={s.get('total_tokens')} cost={s.get('cost_usd')} wall={s['wall_s']}s", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--tasks", default="all")
    ap.add_argument("--arms", default="dc,leandc")
    a = ap.parse_args()
    want = None if a.tasks == "all" else set(a.tasks.split(","))
    for rep in range(1, a.reps + 1):
        for t in TASKS:
            if want and t["id"] not in want:
                continue
            for arm in a.arms.split(","):  # alternate arms per task so drift hits both equally
                run_one(t, arm, rep)
    print("ALL DONE", flush=True)
