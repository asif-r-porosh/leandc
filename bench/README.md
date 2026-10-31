# bench: LeanDC vs Desktop Commander, token usage

The bench folder holds the benchmark behind the token numbers of LeanDC, so the runs can be repeated. The folder is not installed with LeanDC.

## Summary

- **What was measured:** tokens used by the same 11 tasks in three setups (Desktop Commander 0.3.1, the same with stricter limits, and LeanDC 0.1.0), 3 runs of each, 99 runs in total. "Tokens" means everything sent to the model on each turn plus what the model wrote.
- **Result:** LeanDC used fewer tokens than Desktop Commander on all 11 tasks, from 37% to 90% fewer. On every task, even the most expensive LeanDC run used fewer tokens than the cheapest run of either Desktop Commander setup. All 11 tasks added up: 762,274 tokens for Desktop Commander, 242,649 for LeanDC (68% fewer). Every run passed the task check.
- **Why:** most of the difference follows the size of the tool list, and the full list is sent on every turn. LeanDC has 5 tools and about 2,100 tokens of tool descriptions; Desktop Commander 0.3.1 has 25 tools and about 13,500.
- **What is not claimed:** anything about cost or about how much of any plan's usage allowance is used. No measurement of the Chat app. No forecast of real use. See "Limits" below.

## Results

Total tokens per task. Median of 3 runs; the lowest and highest run are in brackets. "DC tuned" is Desktop Commander with stricter read and write limits (see "Setups").

| Task | Desktop Commander | DC tuned | LeanDC | LeanDC vs Desktop Commander |
|---|---|---|---|---|
| t1 read a whole file | 33,997 (33,997 to 34,002) | 34,020 (34,020 to 34,058) | 12,064 (12,058 to 12,084) | -65% |
| t2 search, then read | 60,458 (60,161 to 60,579) | 61,104 (61,026 to 61,159) | 6,047 (6,040 to 6,061) | -90% |
| t3 write a 120-row table | 29,149 (29,063 to 29,228) | 29,133 (29,113 to 29,159) | 6,269 (6,224 to 6,269) | -78% |
| t4 edit and test | 61,718 (44,505 to 114,034) | 121,103 (115,509 to 121,339) | 19,673 (13,988 to 20,256) | -68% |
| t5 full test suite | 28,843 (28,840 to 28,845) | 28,875 (28,872 to 28,879) | 6,099 (6,098 to 6,106) | -79% |
| t6 recover from a wrong file name | 43,371 (43,366 to 43,374) | 43,439 (43,434 to 43,453) | 9,103 (5,937 to 9,110) | -79% |
| t7 bug-fix session | 63,717 (63,715 to 63,734) | 65,611 (64,645 to 66,073) | 17,171 (15,825 to 17,692) | -73% |
| t8 feature session | 81,748 (65,189 to 81,789) | 82,513 (65,444 to 82,563) | 19,906 (14,285 to 25,971) | -76% |
| t9 six-message conversation | 259,952 (238,596 to 260,335) | 262,318 (261,522 to 262,890) | 102,049 (91,778 to 102,117) | -61% |
| t10 read a 1,560-line file completely | 55,613 (55,592 to 55,613) | 55,623 (40,937 to 55,672) | 34,838 (34,817 to 34,851) | -37% |
| t11 find the odd line in a 3,000-line log | 43,708 (43,695 to 43,714) | 43,755 (43,753 to 43,783) | 9,430 (9,418 to 9,571) | -78% |
| **All 11 tasks added up** | 762,274 | 827,494 | 242,649 | -68% |

Nothing is weighted: the last row is a plain sum of the 11 task medians.

## What was measured, and how

**Two layers of evidence.**

1. **The tool list.** LeanDC has 5 tools (`run`, `job`, `read`, `write`, `edit`). Desktop Commander 0.3.1 has 25. The size of the descriptions was measured with a one-word prompt (`smoke.sh`): with no tool server attached, the input was 552 tokens; attaching Desktop Commander added 13,453 and attaching LeanDC added 2,095, hence about 13,500 and about 2,100. The tool list is fixed by the code of each version, so the size repeats from run to run.
2. **Task runs.** The model chooses the steps freely, so the numbers vary from run to run (see the ranges above). The runs show whether the tool-list difference carries through to real tasks and whether the work still gets done. The runs agree with layer 1: on the two-turn tasks t1, t3 and t5, the gap between Desktop Commander and LeanDC is 11,000 to 11,400 tokens per turn, close to 13,453 minus 2,095 (11,358).

**Method.**

- Each run is a separate Claude Code session in non-interactive mode (`claude -p`) with exactly one tool server attached and the built-in tools of Claude Code switched off. Tool search is off, so all tool descriptions of a server are loaded up front.
- Same prompt, same model, and a fresh copy of the same project for every run. The three setups take turns going first.
- Desktop Commander gets a fresh settings folder for every run. LeanDC runs from a private copy of the config, so the real usage log stays untouched.
- Tokens are taken from the usage reported by each session: all input sent to the model across the turns of the run, plus the output.
- A script checks every run. A cheaper run with a wrong result would not count; all 99 passed.

**Tasks.** Read a whole file; search then read; write a 120-row table; edit a value and the matching test; run the full test suite; recover from a wrong file name; a bug-fix session; a feature session; a six-message conversation; read a 1,560-line file completely; find the one odd line in a 3,000-line log.

**Setups.**

- `dc`: Desktop Commander 0.3.1, default settings.
- `dc-tuned`: Desktop Commander 0.3.1 with a hard 300-line read cap and an 800-line write limit. LeanDC reads 300 lines by default but up to 1,000 lines (40,000 characters) per call, so DC tuned is stricter than LeanDC on reads. The setup shows what a hard 300-line cap costs and is not a copy of the limits of LeanDC.
- `leandc`: LeanDC 0.1.0.

**Versions.** Desktop Commander 0.3.1, LeanDC 0.1.0, model Sonnet 5.5 (`claude-sonnet-5-5`), Claude Code 2.1.292, macOS on Apple silicon. The project used for the tasks is pinned in `setup.sh`, and `meta.json` in the work folder records the versions actually used.

**Where LeanDC is not better.**

- t4: LeanDC made 6 tool calls, Desktop Commander 5.
- t9: LeanDC made 12 calls against 9, took 18 turns against 15, and wrote about 15% more output.
- Over all 11 tasks the two used the same number of calls (36) and turns (52), and about the same wall time (146 s for LeanDC, 154 s for Desktop Commander, server start-up included). The saving is in tokens, not speed.
- Desktop Commander's t4 ranged from 44,505 to 114,034 tokens between runs, so a median of 3 is a rough figure there.

## Limits

- **Tokens only.** No claim is made about cost or about how much of any plan's usage allowance is used. The benchmark counts tokens and nothing else.
- **Not the Chat app.** Claude Code drives the sessions with a one-line system prompt. The Chat app adds fixed text to every turn, making the percentages smaller.
- **Not a forecast.** The tasks are small, were written by the author of LeanDC, and run on the LeanDC repository. Results come from 3 runs per task, one model, one machine and one version of each tool. Other versions or models may give different numbers. Real use is not projected here.
- **Desktop Commander at default settings** (except in the tuned setup). Other configurations may differ.

## Run the benchmark

Needs: the `claude` CLI logged in, `git`, `python3` 3.12 or newer, `node` with `npx` (Desktop Commander is started with npx), and LeanDC installed (`./install.sh`). A full run uses part of the usage allowance of the logged-in Claude account: 99 sessions, about 25 minutes, roughly five million tokens counted as above. Keep the machine awake.

```
bench/setup.sh                                  # builds ~/.cache/leandc-bench (LEANDC_BENCH_HOME to move the folder)
export LEANDC_BENCH_HOME=~/.cache/leandc-bench
bench/smoke.sh                                  # tool-list size: no server vs Desktop Commander vs LeanDC, one trivial prompt each
python3 bench/bench.py --reps 3                 # all 11 tasks, all three setups
python3 bench/report.py                         # tables -> report.md in the work folder
```

- `bench.py` options: `--tasks t1-read-whole,...`, `--arms dc,leandc`, `--reps N`.
- Finished runs are skipped, so a run can be stopped and resumed.
- The usage windows of the account are recorded after each run. The runner waits for the 5-hour window to reset above `--stop-5h` (default 0.90) and stops above `--stop-week` (default 0.90).
- A run failing for a technical reason (timeout, error, server not connected) is marked broken, retried once, and left out of the report tables.
- `rescore.py` re-runs the check scripts over stored runs after a check is fixed, with no new model calls.
- `smoke.sh` also tests LeanDC over HTTPS when `mcp-leandc-http.json` is placed in the work folder. The file holds the secret URL, so keep the file out of the repo.
- `report.py` also prints weighted totals and cost estimates. The weights are an assumption and the cost is a list-price estimate; neither appears in the results above.
