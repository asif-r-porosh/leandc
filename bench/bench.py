"""Run benchmark tasks through `claude -p` against exactly one MCP server (dc, dc-tuned or leandc), then bucket the calls.

usage: python3 bench.py [--reps N] [--tasks id,id] [--arms dc,dc-tuned,leandc] [--stop-5h 0.90] [--stop-week 0.90]
Each run: fresh copy of fixture-src, fresh Claude Code session, built-in tools off, only one MCP server.
- DC arms get a fresh copy of the DC home per run (DC keeps usage counters there and shows feedback text after enough calls).
- The arm order rotates per task and repetition, so no arm always runs first (prompt-cache warmth).
- Every run records the usage windows the CLI reports. Above --stop-5h the driver waits for the 5-hour window to reset;
  above --stop-week it stops (re-run the same command later: finished runs are skipped).
- Runs that fail for infrastructure reasons are marked "broken" and retried once.
"""
import argparse, hashlib, json, os, platform, re, shutil, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tasks import BENCH, TASKS

SYSTEM = "You are a coding assistant. Use the provided tools when needed. Be concise."
ARMS = {"dc": ("mcp-dc.json", "mcp__dc"), "dc-tuned": ("mcp-dc-tuned.json", "mcp__dc"), "leandc": ("mcp-leandc.json", "mcp__leandc")}
LIMIT_RE = re.compile(r"usage limit|limit reached|hit your limit", re.I)


class Stop(Exception):
    pass


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
    if not results:
        raise RuntimeError("no result event in the stream")
    res = dict(results[-1])
    res["num_turns"] = sum(r.get("num_turns", 0) for r in results)
    res["total_cost_usd"] = sum(r.get("total_cost_usd", 0) for r in results)
    u = {}
    for r in results:  # multi-message conversations: one result per invocation, usage is per invocation
        for k, v in r.get("usage", {}).items():
            if isinstance(v, int):
                u[k] = u.get(k, 0) + v
    init = next((e for e in ev if e.get("type") == "system" and e.get("subtype") == "init"), {})
    rl = [e["rate_limit_info"] for e in ev if e.get("type") == "rate_limit_event"]
    win = (rl[-1].get("unifiedWindows") or {}) if rl else {}
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
        "is_error": res.get("is_error"), "result_subtype": res.get("subtype"), "turns": res.get("num_turns"), "cost_usd": res.get("total_cost_usd"),
        "in_tokens": in_total, "fresh_in": u.get("input_tokens", 0), "cache_create": u.get("cache_creation_input_tokens", 0),
        "cache_read": u.get("cache_read_input_tokens", 0), "out_tokens": u.get("output_tokens", 0),
        "total_tokens": in_total + u.get("output_tokens", 0), "usage_sum_check_in": chk,
        "calls": len(order), "tool_errors": errs, "builtin_calls": builtin, "dup_calls": dup_calls,
        "dup_identical": same_calls, "dup_changed": changed_calls,
        "write_tok_est": write_ch // 4, "read_tok_est": read_ch // 4, "waste_tok_est": waste_ch // 4,
        "model": init.get("model"), "cli_version": init.get("claude_code_version"), "n_tools": len(init.get("tools") or []),
        "mcp_status": [[x.get("name"), x.get("status")] for x in (init.get("mcp_servers") or [])],
        "util_5h": (win.get("five_hour") or {}).get("utilization"), "resets_5h": (win.get("five_hour") or {}).get("resetsAt"),
        "util_week": (win.get("seven_day") or {}).get("utilization"), "resets_week": (win.get("seven_day") or {}).get("resetsAt"),
        "limit_hit": any(x.get("status") == "rejected" for x in rl),
        "per_tool": per_tool, "answer": res.get("result", ""), "rows": rows,
    }


def arm_config(arm, d):
    """Write this run's MCP config. DC arms get their own fresh copy of the DC home with the usage counters removed."""
    cfg_file, _ = ARMS[arm]
    cfg = json.loads((BENCH / cfg_file).read_text())
    for srv in cfg["mcpServers"].values():
        env = srv.get("env") or {}
        if "HOME" in env:
            home = d / "home"
            shutil.copytree(env["HOME"], home)
            dcd = home / ".claude-server-commander"
            for junk in ("tool-history.jsonl", "claude_tool_call.log"):
                (dcd / junk).unlink(missing_ok=True)
            cp = dcd / "config.json"
            c = json.loads(cp.read_text())
            c.pop("usageStats", None)
            c.update(welcomeOnboardingEligible=False, pendingWelcomeOnboarding=False, telemetryEnabled=False)
            cp.write_text(json.dumps(c, indent=1))
            env["HOME"] = str(home)
    p = d / "mcp.json"
    p.write_text(json.dumps(cfg))
    return p


def run_once(task, arm, rep, d):
    work = d / "work"
    shutil.copytree(BENCH / "fixture-src", work)
    if task.get("setup"):
        task["setup"](work)
    cfg = arm_config(arm, d)
    allow = ARMS[arm][1]
    prompts = task.get("turns") or [task["prompt"]]
    multi = len(prompts) > 1
    env = {**os.environ, "ENABLE_TOOL_SEARCH": "false"}
    t0 = time.time()
    streams, sid, timed_out = [], None, False
    for i, p in enumerate(prompts):
        cmd = ["claude", "-p", p.format(work=work), "--model", "sonnet", "--tools", "", "--strict-mcp-config",
               "--mcp-config", str(cfg), "--allowedTools", allow, "--system-prompt", SYSTEM, "--setting-sources", "",
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
                timed_out = True
                print("TIMEOUT", d.name, i, flush=True)
        streams.append(sp)
        for l in open(sp):
            if '"type":"result"' in l:
                sid = json.loads(l).get("session_id", sid)
        if timed_out:
            break
    dur = time.time() - t0
    try:
        s = analyze(streams)
    except Exception as e:
        s = {"is_error": True, "answer": f"analyze failed: {e!r}", "analyze_failed": True}
    try:
        s["passed"] = bool(task["verify"](work, s.get("answer", "")))
    except Exception as e:
        s["passed"], s["verify_error"] = False, repr(e)
    # broken = the run itself did not work (not "the model got it wrong")
    why = []
    if timed_out: why.append("timeout")
    if s.get("analyze_failed"): why.append("no result")
    if s.get("is_error") and s.get("result_subtype") != "error_max_turns": why.append("error result")
    if s.get("limit_hit"): why.append("limit")
    if not s.get("analyze_failed") and (s.get("n_tools", 0) < 1 or any(st != "connected" for _, st in s.get("mcp_status", []))): why.append("server not connected")
    s["hit_max_turns"] = s.get("result_subtype") == "error_max_turns"
    s.update(task=task["id"], arm=arm, rep=rep, wall_s=round(dur, 1), timed_out=timed_out, broken=why, ts=time.time())
    return s


def run_one(task, arm, rep, guard):
    rid = f"{task['id']}-{arm}-r{rep}"
    d = BENCH / "runs" / rid
    if (d / "summary.json").exists():
        print("skip", rid, flush=True)
        return
    guard()
    for attempt in (1, 2):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        s = run_once(task, arm, rep, d)
        if "limit" in s["broken"] or (s.get("is_error") and LIMIT_RE.search(s.get("answer") or "")):
            shutil.rmtree(d, ignore_errors=True)
            raise Stop(f"usage limit reached during {rid}; run the same command again after the reset")
        if not s["broken"] or attempt == 2:
            break
        print("BROKEN", rid, s["broken"], "- retrying once", flush=True)
        (BENCH / "runs" / f"{rid}.broken-attempt1.json").write_text(json.dumps({k: v for k, v in s.items() if k not in ("rows", "per_tool")}, indent=1))
    s["attempts"] = attempt
    (d / "summary.json").write_text(json.dumps(s, indent=1))
    guard.last = (s.get("util_5h"), s.get("resets_5h"), s.get("util_week"))
    print(f"{rid}: pass={s['passed']} broken={s['broken'] or '-'} turns={s.get('turns')} calls={s.get('calls')} tokens={s.get('total_tokens')} "
          f"cost={s.get('cost_usd')} wall={s['wall_s']}s 5h={s.get('util_5h')} week={s.get('util_week')}", flush=True)


class Guard:
    """Called before every run: waits out a nearly full 5-hour window, stops on a nearly full weekly one."""
    def __init__(self, s5, sw):
        self.s5, self.sw, self.last = s5, sw, None

    def __call__(self):
        if not self.last:
            return
        u5, r5, uw = self.last
        if uw is not None and uw >= self.sw:
            raise Stop(f"weekly usage {uw:.0%} >= {self.sw:.0%}; stopping. Re-run the same command later.")
        if u5 is not None and u5 >= self.s5 and r5:
            wait = max(0, r5 - time.time()) + 90
            print(f"5-hour usage {u5:.0%} >= {self.s5:.0%}: waiting {wait / 60:.0f} min for the reset", flush=True)
            time.sleep(wait)
            self.last = None


def sh(*c):
    try:
        return subprocess.run(c, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception as e:
        return f"? {e!r}"


def write_meta(args):
    repo = Path(__file__).resolve().parent.parent
    meta = {"started": time.strftime("%Y-%m-%d %H:%M:%S %z"), "args": vars(args), "platform": platform.platform(), "machine": platform.machine(),
            "claude_cli": sh("claude", "--version"), "node": sh("node", "--version"), "python": platform.python_version(),
            "repo_head": sh("git", "-C", str(repo), "rev-parse", "--short", "HEAD"),
            "repo_dirty_files": [l for l in sh("git", "-C", str(repo), "status", "--porcelain").splitlines()],
            "fixture_commit": (BENCH / "fixture-commit").read_text().strip() if (BENCH / "fixture-commit").exists() else None}
    try:
        meta["dc_args"] = json.loads((BENCH / "mcp-dc.json").read_text())["mcpServers"]["dc"]["args"]
        py = Path(json.loads((BENCH / "mcp-leandc.json").read_text())["mcpServers"]["leandc"]["command"]).parent / "python"
        meta["leandc_version"] = sh(str(py), "-c", "import importlib.metadata as m;print(m.version('leandc'))")
        pkg = sh(str(py), "-c", "import leandc,os;print(os.path.dirname(leandc.__file__))")
        meta["leandc_installed_vs_repo_src"] = sh("diff", "-rq", pkg, str(repo / "src/leandc"), "-x", "__pycache__") or "identical"
    except Exception as e:
        meta["meta_error"] = repr(e)
    (BENCH / "meta.json").write_text(json.dumps(meta, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--tasks", default="all")
    ap.add_argument("--arms", default="dc,dc-tuned,leandc")
    ap.add_argument("--stop-5h", type=float, default=0.90)
    ap.add_argument("--stop-week", type=float, default=0.90)
    a = ap.parse_args()
    write_meta(a)
    want = None if a.tasks == "all" else set(a.tasks.split(","))
    arms = a.arms.split(",")
    tasks = [t for t in TASKS if not want or t["id"] in want]
    guard = Guard(a.stop_5h, a.stop_week)
    try:
        for rep in range(1, a.reps + 1):
            for ti, t in enumerate(tasks):
                k = (ti + rep - 1) % len(arms)   # rotate who goes first
                for arm in arms[k:] + arms[:k]:
                    run_one(t, arm, rep, guard)
    except Stop as e:
        print("STOPPED:", e, flush=True)
        sys.exit(3)
    print("ALL DONE", flush=True)
