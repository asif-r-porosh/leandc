"""Benchmark tasks: prompt, optional setup (inject a bug), verifier, weight.

Weights are a documented assumption, loosely from the DC history mix (read_file 51% of calls,
start_process 30%, edit_block 10%, write_file 5%); sessions carry the multi-step share.
Tasks marked stress=True have weight 0: they probe leandc's smaller caps and are reported separately.
"""
import ast, hashlib, os, re, subprocess
from pathlib import Path

# Work dir (fixture copy, venv, DC home, runs). Created by setup.sh; keep it outside the repo.
BENCH = Path(os.environ.get("LEANDC_BENCH_HOME", Path.home() / ".cache" / "leandc-bench"))
PY = BENCH / "bench-venv/bin/python"
TESTCMD = "cd {work} && PYTHONPATH=src " + str(PY) + " -m pytest tests/test_cmdnames.py"  # project addopts already has -q


def _last(text, tag):
    for ln in reversed((text or "").strip().splitlines()):
        if ln.strip().startswith(tag):
            return ln.strip()[len(tag):].strip()
    return None


def _pytest(work, target):
    r = subprocess.run([str(PY), "-m", "pytest", target, "-q"], cwd=work, env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True, timeout=120)
    return r.returncode == 0


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


# ---- verifiers: (work: Path, answer: str) -> bool -------------------------------------------
def v_funcs(work, ans):
    got = _last(ans, "FUNCS:")
    want = {n.name for n in ast.walk(ast.parse((work / "src/leandc/files.py").read_text())) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return got is not None and {x.strip().strip("`()") for x in got.split(",") if x.strip()} == want


def v_where(work, ans):
    got = (_last(ans, "ANSWER:") or "").replace("`", "").strip()
    return re.fullmatch(r"\S*cmdnames\.py\s+20", got) is not None


def v_table(work, ans):
    p = work / "docs/commands.md"
    if not p.exists():
        return False
    want = ["| n | n2 | n3 |", "|---|---|---|"] + [f"| {n} | {n*n} | {n**3} |" for n in range(1, 121)]
    return [ln.rstrip() for ln in p.read_text().strip().splitlines()] == want


def v_maxnames(work, ans):
    src = (work / "src/leandc/cmdnames.py").read_text()
    test = (work / "tests/test_cmdnames.py").read_text()
    return "MAX_NAMES = 30" in src and "30" in test and "<= 20" not in test and "== 20" not in test and _pytest(work, "tests/test_cmdnames.py")


def v_suite(work, ans):
    # One project timing test (event-loop gap) is flaky on a busy machine; a correct report of 130 plus the failure counts as a pass.
    n = _last(ans, "PASSED:")
    return n == "131" or (n == "130" and re.search(r"\b1 (test )?(failed|failure)", ans, re.I) is not None)


def v_lines(work, ans):
    return _last(ans, "LINES:") == "109"


def inject_bug(work):
    p = work / "src/leandc/cmdnames.py"
    s = p.read_text()
    assert "len(counts) < MAX_NAMES" in s
    p.write_text(s.replace("len(counts) < MAX_NAMES", "len(counts) <= MAX_NAMES"))
    (work / ".test_sha").write_text(_sha(work / "tests/test_cmdnames.py"))


def v_bugfix(work, ans):
    unchanged = (work / ".test_sha").read_text() == _sha(work / "tests/test_cmdnames.py")
    return unchanged and "len(counts) < MAX_NAMES" in (work / "src/leandc/cmdnames.py").read_text() and _pytest(work, "tests/test_cmdnames.py")


def v_sudo(work, ans):
    r = subprocess.run([str(PY), "-c", "from leandc.cmdnames import command_names as c; print(c('sudo apt update'), c('ls | sudo grep x'))"],
                       cwd=work, env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    ok = r.stdout.strip() == "{'apt': 1} {'ls': 1, 'grep': 1}"
    return ok and "sudo" in (work / "tests/test_cmdnames.py").read_text() and _pytest(work, "tests/test_cmdnames.py")


def v_conv(work, ans):
    r = subprocess.run([str(PY), "-c", "from leandc.cmdnames import command_names as c; print(c('sudo apt update'), c('doas ls'), c('nice make'))"],
                       cwd=work, env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    return r.stdout.strip() == "{'apt': 1} {'ls': 1} {'make': 1}" and _pytest(work, "tests/test_cmdnames.py")


# ---- stress tasks (weight 0, reported separately): do leandc's smaller caps cost extra calls? --------------
def gen_bigmod(work):
    """A ~1,560-line Python module with 120 functions: reading it completely takes several reads under a line cap."""
    d = work / "data"
    d.mkdir(exist_ok=True)
    out = ['"""Generated module for the benchmark."""', ""]
    for n in range(120):
        h = hashlib.sha1(f"fn-{n}".encode()).hexdigest()[:6]
        out += [f"def handler_{h}(value, scale={n + 1}):", f'    """Handler number {n}."""', "    total = 0", "    for i in range(scale):",
                "        if i % 2 == 0:", "            total += value * i", "        else:", "            total -= i", "    if total < 0:",
                "        total = -total", f"    return total + {n}", "", ""]
    (d / "bigmod.py").write_text("\n".join(out))


def v_bigmod(work, ans):
    got = _last(ans, "FUNCS:")
    want = {n.name for n in ast.walk(ast.parse((work / "data/bigmod.py").read_text())) if isinstance(n, ast.FunctionDef)}
    return got is not None and {x.strip().strip("`()") for x in got.split(",") if x.strip()} == want


def gen_oddline(work):
    """A 3,000-line log where one line breaks the pattern; it has no 'ERROR' keyword to search for."""
    d = work / "data"
    d.mkdir(exist_ok=True)
    (d / "gen.py").write_text(
        "for n in range(1, 3001):\n"
        "    if n == 1742:\n"
        "        print('warn step 1742 code=E7731 disk quota exceeded')\n"
        "    else:\n"
        "        print(f'ok step {n:04d} elapsed={n * 3}ms')\n")


def v_oddline(work, ans):
    return _last(ans, "CODE:") == "E7731"


CONV = [
    "Read {work}/src/leandc/cmdnames.py and {work}/tests/test_cmdnames.py, then explain in two sentences how command_names decides which words are commands.",
    "Make `sudo` a prefix that is skipped, so `sudo apt update` counts `apt`. Add a test case for it in tests/test_cmdnames.py.",
    "Do the same for `doas` and `nice`, with test cases.",
    "Run the tests: " + TESTCMD + "  and tell me the result.",
    "Show me the final version of command_names() and the test cases you added, exactly as they are now in the files.",
    "Which other files in {work} import or call command_names? List file and line number.",
]

TASKS = [
    dict(id="t1-read-whole", weight=0.15, verify=v_funcs,
         prompt="Read {work}/src/leandc/files.py completely. List the names of every function defined in it (including nested ones). "
                "Put them comma-separated on the last line of your answer, prefixed with 'FUNCS:'."),
    dict(id="t2-grep-read", weight=0.15, verify=v_where,
         prompt="In the project at {work}, find the code that counts command names for the usage log. Tell me which file it is in and the "
                "value of its MAX_NAMES limit. Last line of your answer: 'ANSWER: <file name> <value>'."),
    dict(id="t3-write-120", weight=0.05, verify=v_table,
         prompt="Create the file {work}/docs/commands.md containing exactly a markdown table: header row `| n | n2 | n3 |`, separator row `|---|---|---|`, "
                "then one row per n from 1 to 120 in the form `| n | n squared | n cubed |` with the numbers computed. No other text in the file."),
    dict(id="t4-edit-test", weight=0.10, verify=v_maxnames,
         prompt="In {work}, change MAX_NAMES in src/leandc/cmdnames.py from 20 to 30 and update the test in tests/test_cmdnames.py that checks the limit so it "
                "still passes. Then run: " + TESTCMD + " and confirm the result."),
    dict(id="t5-full-suite", weight=0.20, verify=v_suite,
         prompt="Run the complete test suite of the project in {work} with: cd {work} && PYTHONPATH=src " + str(PY) + " -m pytest . It takes about 30 seconds. "
                "Tell me how many tests passed. Last line of your answer: 'PASSED: <number>'."),
    dict(id="t6-error-recover", weight=0.05, verify=v_lines,
         prompt="Read {work}/src/leandc/shapping.py (the name may be misspelled). Find what the file is really called and tell me how many lines it has. "
                "Last line of your answer: 'LINES: <number>'."),
    dict(id="t7-bugfix-session", weight=0.10, verify=v_bugfix, setup=inject_bug,
         prompt="A test in {work}/tests/test_cmdnames.py is failing. Find the cause in the source code and fix it without editing the tests. "
                "Run: " + TESTCMD + " to confirm it passes."),
    dict(id="t8-feature-session", weight=0.05, verify=v_sudo,
         prompt="In {work}, make the command-name counter treat `sudo` as a prefix: `sudo apt update` must count `apt`, not `sudo`. "
                "Add a test case for it in tests/test_cmdnames.py, then run: " + TESTCMD + " and confirm it passes."),
    dict(id="t9-long-conversation", weight=0.15, verify=v_conv, turns=CONV),
    dict(id="t10-big-file", weight=0, stress=True, verify=v_bigmod, setup=gen_bigmod,
         prompt="Read {work}/data/bigmod.py completely (it is long). List the names of every function defined in it. "
                "Put them comma-separated on the last line of your answer, prefixed with 'FUNCS:'."),
    dict(id="t11-odd-line", weight=0, stress=True, verify=v_oddline, setup=gen_oddline,
         prompt="Run: cd {work} && " + str(PY) + " data/gen.py  It prints a long log in which exactly one line does not follow the pattern of the others. "
                "Tell me the code on that line. Last line of your answer: 'CODE: <code>'."),
]
