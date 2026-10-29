import asyncio
import json
import os
import re
import time

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from leandc.config import Caps, Config
from leandc.jobs import JobManager, build_env
from leandc.procs import job_command, run_command

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_cfg(tmp_path, **caps):
    return Config(default_cwd=str(tmp_path), state_dir=str(tmp_path / "state"), caps=Caps(**caps))


@pytest.fixture
async def env(tmp_path):
    cfg = make_cfg(tmp_path)
    mgr = JobManager(cfg)
    await mgr.start()
    yield cfg, mgr
    await mgr.stop()


async def run(env_, cmd, **kw):
    cfg, mgr = env_
    return await run_command(cfg, mgr, build_env, cmd, **kw)


async def job(env_, **kw):
    cfg, mgr = env_
    return await job_command(cfg, mgr, **kw)


def job_id(text):
    return re.search(r"job=([0-9a-f]{8}-\d+)", text).group(1)


async def err(coro):
    with pytest.raises(ToolError) as exc:
        await coro
    return str(exc.value)


# ---------------------------------------------------------------- run ----

async def test_run_simple(env, tmp_path):
    out = await run(env, "echo hello")
    first, body = out.split("\n", 1)
    assert first.startswith("exit=0 cwd=") and str(tmp_path) in first and "dur=" in first
    assert body == "hello"


async def test_run_exit_code_and_stderr_merged(env):
    out = await run(env, "echo out; echo err >&2; exit 3")
    assert out.startswith("exit=3 ")
    assert "out" in out and "err" in out


async def test_run_cwd_and_echo(env, tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    out = await run(env, "pwd", cwd=str(sub))
    assert f"cwd={sub}" in out.splitlines()[0] and out.splitlines()[1] == str(sub)


async def test_run_bad_cwd(env, tmp_path):
    assert (await err(run(env, "true", cwd=str(tmp_path / "nope")))).startswith("ERROR NOT_FOUND:")


async def test_run_env_vars(env):
    out = await run(env, "echo $FOO", env={"FOO": "bar"})
    assert out.endswith("bar")


async def test_run_stdin_is_closed(env):
    out = await asyncio.wait_for(run(env, "cat; echo done"), 5)
    assert out.endswith("done") and out.startswith("exit=0")


async def test_run_no_output(env):
    assert (await run(env, "true")).endswith("(no output)")


async def test_run_ansi_and_progress_declared(env):
    out = await run(env, r"printf '\033[31mred\033[0m\n10%%\r100%%\n'")
    assert "red" in out and "\x1b" not in out
    assert "[shaped:" in out and "ansi stripped" in out


async def test_run_output_cap_head_and_tail(env):
    out = await run(env, "seq 1 20000", max_chars=1000)
    assert "\n1\n" in out and "\n20000\n[shaped:" in out
    assert "omitted" in out and len(out) < 1500


async def test_run_default_cap_is_8000(env):
    out = await run(env, "seq 1 20000")
    assert len(out) < 8400


async def test_run_param_validation(env):
    assert "timeout" in await err(run(env, "true", timeout=500))
    assert "max_chars" in await err(run(env, "true", max_chars=999999))
    assert "empty" in await err(run(env, "  "))
    assert "environment variable" in await err(run(env, "true", env={"A=B": "x"}))


async def test_run_not_login_shell_no_rc(env, tmp_path, monkeypatch):
    rc = tmp_path / "evil_rc"
    rc.write_text("echo RCFILE_RAN\n")
    monkeypatch.setenv("BASH_ENV", str(rc))
    out = await run(env, "echo ok")
    assert "RCFILE_RAN" not in out


async def test_run_sync_job_is_cleaned_up(env, tmp_path):
    await run(env, "echo x")
    cfg, mgr = env
    assert not list(mgr.root.iterdir())


async def test_run_long_returns_job_then_finishes(env):
    out = await run(env, "echo start; sleep 2; echo end", timeout=1)
    assert out.startswith("running job=") and "start" in out and "[still running" in out
    jid = job_id(out)
    res = await job(env, id=jid, wait=10)
    assert "exited exit=0" in res.splitlines()[0]
    assert "start" in res and "end" in res


async def test_cancelled_request_does_not_kill_job(env):
    """A client timeout cancels the request; the command must keep running."""
    cfg, mgr = env
    task = asyncio.create_task(run_command(cfg, mgr, build_env, "sleep 1; echo survived", timeout=30))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (j,) = list(mgr.jobs.values())
    await asyncio.wait_for(j.done.wait(), 5)
    assert j.exit_code == 0
    assert "survived" in (j.dir / "out.log").read_text()


async def test_background_child_does_not_hang_run(env):
    t0 = time.monotonic()
    out = await run(env, "sleep 31 >/dev/null 2>&1 & echo pid=$!")
    assert out.startswith("exit=0") and "pid=" in out
    assert time.monotonic() - t0 < 5
    pid = int(re.search(r"pid=(\d+)", out).group(1))
    os.kill(pid, 15)  # clean up exactly the orphan we made


async def test_parallel_runs_are_concurrent(env):
    t0 = time.monotonic()
    outs = await asyncio.gather(*[run(env, "sleep 1; echo hi") for _ in range(8)])
    assert time.monotonic() - t0 < 3.0
    assert all(o.startswith("exit=0") for o in outs)


async def test_loop_not_blocked_by_large_output(env):
    """Sustained heavy output must not stall the event loop (DC stalled under load)."""
    gaps: list[float] = []

    async def ticker():
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    t = asyncio.create_task(ticker())
    t0 = time.monotonic()
    await run(env, "yes | head -c 1500000000", max_chars=1000, timeout=60)
    elapsed = time.monotonic() - t0
    t.cancel()
    assert elapsed > 0.8, "command too short to prove anything"
    assert len(gaps) > 10
    assert max(gaps) < 0.25


async def test_huge_output_buffer_rolls_and_reports_dropped(tmp_path):
    cfg = make_cfg(tmp_path, job_buffer_bytes=20_000)
    mgr = JobManager(cfg)
    await mgr.start()
    try:
        out = await run_command(cfg, mgr, build_env, "seq 1 200000", max_chars=2000)
        assert "bytes dropped from the start" in out and "\n200000\n[shaped:" in out
    finally:
        await mgr.stop()


# ---------------------------------------------------------------- job ----

async def test_job_list_empty_and_populated(env):
    assert (await job(env)).startswith("no jobs")
    out = await run(env, "sleep 3", timeout=1)
    jid = job_id(out)
    listing = await job(env)
    assert jid in listing and "running" in listing and "sleep 3" in listing


async def test_job_output_tail_offset_grep(env):
    out = await run(env, "seq 1 500; sleep 5", timeout=1)
    jid = job_id(out)
    t = await job(env, id=jid, tail=3)
    assert "lines 498-500 of 500" in t and t.rstrip().endswith("500")
    o = await job(env, id=jid, offset=10, limit=3)
    assert "lines 11-13 of 500" in o and "next offset=13" in o
    g = await job(env, id=jid, grep=r"^4\d$")
    assert "10 of 500 lines match" in g and "40:40" in g
    assert "BAD_PARAM" in await err(job(env, id=jid, grep="("))


async def test_job_default_output_is_last_100_lines(env):
    out = await run(env, "seq 1 500; sleep 5", timeout=1)
    res = await job(env, id=job_id(out))
    assert "lines 401-500 of 500" in res


async def test_job_wait_returns_early_on_exit(env):
    out = await run(env, "sleep 1.2; echo fin", timeout=1)
    t0 = time.monotonic()
    res = await job(env, id=job_id(out), wait=30)
    assert time.monotonic() - t0 < 5
    assert "exited" in res and "fin" in res


async def test_job_wait_times_out_still_running(env):
    out = await run(env, "sleep 10", timeout=1)
    t0 = time.monotonic()
    res = await job(env, id=job_id(out), wait=1)
    assert 0.8 < time.monotonic() - t0 < 3
    assert res.startswith(f"job {job_id(out)} running")


async def test_job_kill_process_group(env):
    # unique duration per run so stale sleeps from other runs cannot match
    n = 40000 + os.getpid() % 9000
    pat = f"sleep [{str(n)[0]}]{str(n)[1:]}"
    out = await run(env, f"sleep {n} & sleep {n}; echo never", timeout=1)
    jid = job_id(out)
    res = await job(env, id=jid, action="kill")
    assert "killed" in res and "signal:SIGTERM" in res
    chk = await run(env, f"pgrep -f '{pat}' | wc -l")
    assert chk.splitlines()[1].strip() == "0"
    again = await job(env, id=jid, action="kill")
    assert "already killed" in again


async def test_job_status(env):
    out = await run(env, "sleep 5", timeout=1)
    st = await job(env, id=job_id(out), action="status")
    assert st.startswith(f"job {job_id(out)} running") and "\n" not in st


async def test_job_unknown_and_malformed(env):
    assert (await err(job(env, id="deadbeef-1"))).startswith("ERROR JOB_UNKNOWN:")
    assert (await err(job(env, id="nonsense"))).startswith("ERROR JOB_UNKNOWN:")
    assert "action" in await err(job(env, id="deadbeef-1", action="restart"))
    assert "wait" in await err(job(env, id="deadbeef-1", wait=9999))


async def test_job_lost_after_restart(tmp_path):
    cfg = make_cfg(tmp_path)
    mgr1 = JobManager(cfg)
    await mgr1.start()
    out = await run_command(cfg, mgr1, build_env, "echo before; sleep 100", timeout=1)
    jid = job_id(out)
    # simulate a crash: no clean stop; leave meta saying "running"
    proc = mgr1.jobs[jid].proc
    meta = json.loads((mgr1.root / jid / "meta.json").read_text())
    assert meta["status"] == "running"
    mgr2 = JobManager(cfg)  # new boot id, same state dir
    await mgr2.start()
    try:
        assert mgr2.boot != mgr1.boot
        st = await job_command(cfg, mgr2, id=jid, action="status")
        assert "LOST" in st and "restarted at" in st
        o = await job_command(cfg, mgr2, id=jid)
        assert "LOST" in o and "before" in o  # output up to the restart is readable
        assert (await err(job_command(cfg, mgr2, id=jid, action="kill"))).startswith("ERROR JOB_LOST:")
        assert jid in await job_command(cfg, mgr2)
    finally:
        proc.kill()
        await mgr2.stop()
        await mgr1.stop()


async def test_finished_job_from_earlier_boot_still_readable(tmp_path):
    cfg = make_cfg(tmp_path)
    mgr1 = JobManager(cfg)
    await mgr1.start()
    out = await run_command(cfg, mgr1, build_env, "echo old; sleep 1.5; echo older", timeout=1)
    jid = job_id(out)
    await mgr1.wait(mgr1.jobs[jid], 10)
    mgr2 = JobManager(cfg)
    await mgr2.start()
    try:
        o = await job_command(cfg, mgr2, id=jid)
        assert "exited exit=0" in o and "older" in o and "LOST" not in o
    finally:
        await mgr2.stop()
        await mgr1.stop()


async def test_job_expiry(tmp_path):
    cfg = make_cfg(tmp_path, job_expiry_h=1)
    mgr1 = JobManager(cfg)
    await mgr1.start()
    out = await run_command(cfg, mgr1, build_env, "sleep 1.2; echo x", timeout=1)
    jid = job_id(out)
    await mgr1.wait(mgr1.jobs[jid], 10)
    meta_file = mgr1.root / jid / "meta.json"
    meta = json.loads(meta_file.read_text())
    meta["ended"] -= 2 * 3600
    meta_file.write_text(json.dumps(meta))
    mgr2 = JobManager(cfg)
    await mgr2.start()  # sweeps on start
    try:
        assert (await err(job_command(cfg, mgr2, id=jid))).startswith("ERROR JOB_EXPIRED:")
    finally:
        await mgr2.stop()
        await mgr1.stop()


async def test_stop_kills_running_jobs(tmp_path):
    cfg = make_cfg(tmp_path)
    mgr = JobManager(cfg)
    await mgr.start()
    out = await run_command(cfg, mgr, build_env, "sleep 77", timeout=1)
    job_ = mgr.jobs[job_id(out)]
    pid = job_.proc.pid
    await mgr.stop()
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
