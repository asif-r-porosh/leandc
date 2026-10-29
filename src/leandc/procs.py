"""`run` and `job` tool logic."""
from __future__ import annotations

import os
import re

from .config import Config
from .errors import fail
from .jobs import DiskJob, Job, JobManager, clock, exit_text, read_buffer
from .shaping import cap, clean, shape_output

_HINT_RUNNING = "[still running: job(id, wait=<s>) waits, job(id, tail=<n>) reads, job(id, action=kill) stops]"
_MIN_CHARS = 200


def _check_int(name: str, value: int | None, default: int, lo: int, hi: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        fail("BAD_PARAM", f"{name} must be an integer from {lo} to {hi}", f"Got {value!r}")
    return value


def resolve_cwd(cfg: Config, cwd: str | None) -> str:
    if cwd is None:
        cwd = cfg.default_cwd
    p = os.path.expanduser(cwd)
    if not os.path.isabs(p):
        p = os.path.join(cfg.default_cwd, p)
    p = os.path.normpath(p)
    if not os.path.exists(p):
        fail("NOT_FOUND", f"cwd does not exist: {p}", "Create it first or pass another cwd")
    if not os.path.isdir(p):
        fail("BAD_PARAM", f"cwd is not a directory: {p}")
    return p


def check_env(env: dict[str, str] | None) -> dict[str, str] | None:
    if env is None:
        return None
    for k, v in env.items():
        if not isinstance(k, str) or not k or "=" in k or "\x00" in k:
            fail("BAD_PARAM", f"invalid environment variable name {k!r}")
        if not isinstance(v, str) or "\x00" in v:
            fail("BAD_PARAM", f"environment variable {k} must be a string without NUL")
    return env


def _notes(shaped, dropped: int) -> str:
    notes = list(shaped.notes)
    if dropped:
        notes.append(f"{dropped} bytes dropped from the start of the job buffer")
    return f"[shaped: {', '.join(notes)}]" if notes else ""


async def run_command(
    cfg: Config,
    mgr: JobManager,
    build_env,
    cmd: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    max_chars: int | None = None,
) -> str:
    caps = cfg.caps
    if not isinstance(cmd, str) or not cmd.strip():
        fail("BAD_PARAM", "cmd is empty", "Pass the shell command to run")
    window = _check_int("timeout", timeout, caps.sync_default_s, 1, caps.sync_max_s)
    limit = _check_int("max_chars", max_chars, caps.run_default_chars, _MIN_CHARS, caps.run_max_chars)
    workdir = resolve_cwd(cfg, cwd)
    full_env = build_env(cfg, check_env(env))

    job = await mgr.spawn(cmd, workdir, full_env)
    finished = await mgr.wait(job, window)
    text, dropped = read_buffer(job.dir)
    shaped = shape_output(text, limit)
    note = _notes(shaped, dropped)

    if finished:
        header = f"exit={exit_text(job.exit_code)} cwd={workdir} dur={job.age():.1f}s"
        mgr.release(job)
        tail = [note] if note else []
    else:
        header = f"running job={job.id} after {window}s cwd={workdir}"
        tail = ([note] if note else []) + [_HINT_RUNNING]
    body = shaped.text.rstrip("\n") if shaped.text.strip() else "(no output)"
    return "\n".join([header, body, *tail])


# ------------------------------------------------------------------ job ----

def _status_line(job: Job | DiskJob) -> str:
    if isinstance(job, Job):
        if job.done.is_set():
            return f"job {job.id} {job.status} exit={exit_text(job.exit_code)} age={job.age():.0f}s cwd={job.cwd}"
        return f"job {job.id} running age={job.age():.0f}s cwd={job.cwd}"
    m = job.meta
    if job.lost:
        return (
            f"job {job.id} LOST: service restarted at {clock(m.get('lost_at', 0))}; the process is gone, rerun the command "
            "(output up to the restart is below)"
        )
    ex = m.get("exit")
    return f"job {job.id} {job.status} exit={exit_text(ex)} cwd={m.get('cwd', '?')}"


def _list(mgr: JobManager) -> str:
    rows = mgr.list_jobs()
    if not rows:
        return f"no jobs (boot {mgr.boot})"
    out = [f"jobs (current boot {mgr.boot}); newest first"]
    for job_id, status, age, ex, preview in rows[:50]:
        parts = [job_id, status, f"{age:.0f}s"]
        if ex:
            parts.append(f"exit={ex}")
        if preview:
            parts.append(preview)
        out.append(" ".join(parts))
    if len(rows) > 50:
        out.append(f"[+{len(rows) - 50} older jobs not shown]")
    return "\n".join(out)


def _slice(cfg: Config, text: str, dropped: int, offset, limit, tail, grep) -> str:
    caps = cfg.caps
    cleaned, notes = clean(text)
    lines = cleaned.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    n = len(lines)

    for name, val in (("limit", limit), ("tail", tail)):
        if val is not None:
            _check_int(name, val, 0, 1, caps.read_max_lines)
    if offset is not None and offset < 0:
        fail("BAD_PARAM", "offset must be 0 or more", "Use tail=<n> for the last lines")
    want = limit or 100

    if grep is not None:
        try:
            rx = re.compile(grep)
        except re.error as exc:
            fail("BAD_PARAM", f"grep is not a valid regex: {exc}")
        hits = [(i + 1, ln) for i, ln in enumerate(lines) if rx.search(ln)]
        total = len(hits)
        chosen = hits[-tail:] if tail else hits[(offset or 0) : (offset or 0) + want]
        body = "\n".join(f"{i}:{ln}" for i, ln in chosen)
        header = f"{total} of {n} lines match; showing {len(chosen)}, {dropped} bytes dropped"
        more = bool(chosen) and not tail and (offset or 0) + len(chosen) < total
        nxt = (offset or 0) + len(chosen)
    else:
        if tail:
            start = max(n - tail, 0)
            chosen_lines = lines[start:]
        elif offset is not None or limit is not None:
            start = offset or 0
            if n and start >= n:
                fail("BAD_PARAM", f"offset {start} is past the end ({n} lines)")
            chosen_lines = lines[start : start + want]
        else:
            start = max(n - 100, 0)
            chosen_lines = lines[start:]
        first, last = (start + 1, start + len(chosen_lines)) if chosen_lines else (0, 0)
        body = "\n".join(chosen_lines)
        header = f"lines {first}-{last} of {n}, {dropped} bytes dropped"
        more = bool(chosen_lines) and last < n and not tail
        nxt = last

    capped = cap(body, caps.run_default_chars)
    out = header + (f" next offset={nxt}" if more else "") + "\n" + (capped.text if capped.text else "(no output yet)")
    notes = notes + capped.notes
    if notes:
        out += f"\n[shaped: {', '.join(notes)}]"
    return out


async def job_command(
    cfg: Config,
    mgr: JobManager,
    id: str | None = None,
    action: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
    tail: int | None = None,
    grep: str | None = None,
    wait: int | None = None,
) -> str:
    caps = cfg.caps
    if id is None:
        return _list(mgr)
    action = action or "output"
    if action not in ("output", "status", "kill"):
        fail("BAD_PARAM", f"action '{action}' is not valid", "Use output, status or kill")
    wait_s = _check_int("wait", wait, 0, 0, caps.job_wait_max_s)
    job = mgr.lookup(id)

    if action == "kill":
        if isinstance(job, DiskJob):
            if job.lost:
                fail("JOB_LOST", f"job {id} is gone: service restarted at {clock(job.meta.get('lost_at', 0))}", "Nothing to kill; rerun the command")
            return f"job {id} already {job.status}"
        if job.done.is_set():
            return f"job {id} already {job.status} exit={exit_text(job.exit_code)}"
        await mgr.kill(job)
        return _status_line(job)

    if isinstance(job, Job) and wait_s and not job.done.is_set():
        await mgr.wait(job, wait_s)

    if action == "status":
        return _status_line(job)

    text, dropped = read_buffer(job.dir)
    return _status_line(job) + "\n" + _slice(cfg, text, dropped, offset, limit, tail, grep)
