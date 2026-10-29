# bench: leandc vs Desktop Commander (DC), token usage

Not installed with leandc; it lives in the repo so the token claim can be reproduced.

## What it measures

Real Claude Code sessions (`claude -p`), one MCP server per session (`--strict-mcp-config`), built-in tools off, same model, same prompts, a fresh copy of the same project each time. Per run it records tokens (input incl. cache, output), cost, turns, and per call buckets taken from the stream:

- **write**: characters Claude sends in call arguments (file content, commands, edit text)
- **read**: characters returned in tool results
- **repeat**: calls identical to an earlier call; "identical output" is pure waste, "changed output" is a legitimate re-read after an edit

9 tasks (read, grep+read, write, edit+test, full test suite, error recovery, two sessions, a 6-message conversation), each with a verifier script, so a cheaper tool that fails the task is not counted as a win. Weights are an assumption, loosely from the DC tool history (read_file 51% of calls, start_process 30%, edit_block 10%, write_file 5%).

## Run it

Needs the `claude` CLI logged in (a Claude Code login, separate from the chat app), `git`, `python3` 3.12+, `node`/`npx` (DC is launched with npx), and leandc installed (`./install.sh`). Every run spends your Claude usage: about $0.01 to $0.05 per task per tool, $0.35 to $0.45 for the long conversation; the full 54-run set cost about $3.4.

```
bench/setup.sh                                   # builds ~/.cache/leandc-bench (LEANDC_BENCH_HOME to move it)
export LEANDC_BENCH_HOME=~/.cache/leandc-bench
bench/smoke.sh                                   # schema cost: none vs DC vs leandc, one trivial prompt each
python3 bench/bench.py --reps 3                  # all tasks, both tools (add --tasks t1-read-whole,... / --arms leandc)
python3 bench/report.py                          # medians, delta tokens and %, buckets, spread, repeats -> report.md
```

`bench.py` skips runs that already have a `summary.json`, so it can be stopped and resumed. `rescore.py` re-runs the verifiers over stored work dirs after a verifier fix, with no new model calls.

Notes:
- The fixture is pinned to leandc commit `1a4f2d0` (`FIXTURE_REF` in `setup.sh`); the verifiers expect that snapshot.
- DC runs under a throwaway `HOME` with a copy of your `.bashrc` and telemetry off; your real DC logs are not touched. The copy stays in the work dir, outside the repo.
- Cost depends on prompt-cache warmth. The runs alternate tools back to back, so mostly warm. On a cold start (first message of a session) the gap is larger: the smoke test measured +13,453 schema tokens for DC and +2,099 for leandc, and a cold t5 run cost $0.063 (DC) vs $0.017 (leandc), against $0.013 vs $0.007 warm. Token counts do not depend on this; the warm cost delta is the conservative figure.
- Tool search is turned off so all schemas load up front, as in claude.ai. Claude Code's default defers MCP tool schemas, which would lower DC's schema cost.

## Result snapshot

`results-2026-10-29.md`: DC 0.3.1 vs leandc 0.1.0, 9 tasks, 3 reps, Sonnet 5.5, all 54 runs passed. Weighted total tokens -70% (80,576 vs 24,112), cost -26%. Write tokens unchanged. Read tokens -27%. Repeats were not exercised (no identical re-reads in any run). Caveats: the author of leandc wrote the tasks and the fixture is the leandc repo; DC 0.3.1 did not reproduce the `.bashrc` warning lines seen with the older version in the real logs.
