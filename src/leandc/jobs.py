"""Job manager (sections 3.1, 3.2; D7, D30).

Every command runs as a *job* owned by the server, never by the request that
started it: a client that times out or disconnects cancels its request, and the
command keeps running (stub finding S14). `run` waits for the job for a short
sync window and returns the output if it finished, else a job id.

Job ids are `<boot8>-<seq>`. Output goes to a rolling file on disk (1 MB by
default) so it stays readable after a restart. Processes do not survive a
service restart: on start-up any job still marked running is marked lost.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .errors import fail

_ID_RE = re.compile(r"^[0-9a-f]{8}-\d+$")
_GRACE_AFTER_EXIT_S = 0.5
_KILL_GRACE_S = 5.0


def _now() -> float:
    return time.time()


def clock(ts: float) -> str:
    return time.strftime("%H:%M %Z", time.localtime(ts))


def signal_name(rc: int) -> str:
    try:
        return signal.Signals(-rc).name
    except ValueError:
        return f"signal {-rc}"


def exit_text(rc: int | None) -> str:
    if rc is None:
        return "?"
    return str(rc) if rc >= 0 else f"signal:{signal_name(rc)}"


# ------------------------------------------------------------- buffer ----

class RollingBuffer:
    """Append-only file that keeps roughly the last `cap` bytes."""

    def __init__(self, path: Path, cap: int, on_rotate) -> None:
        self.path = path
        self.cap = max(cap, 1024)
        self.dropped = 0
        self._size = 0
        self._on_rotate = on_rotate
        self._fh = open(path, "ab")

    def write(self, chunk: bytes) -> None:
        self._fh.write(chunk)
        self._fh.flush()
        self._size += len(chunk)
        if self._size > self.cap * 2:
            self._rotate()

    def _rotate(self) -> None:
        self._fh.close()
        data = self.path.read_bytes()
        keep = data[-self.cap :]
        nl = keep.find(b"\n")
        if 0 <= nl < len(keep) - 1:
            keep = keep[nl + 1 :]
        self.dropped += len(data) - len(keep)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(keep)
        os.replace(tmp, self.path)
        self._fh = open(self.path, "ab")
        self._size = len(keep)
        self._on_rotate()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._fh.close()


# ---------------------------------------------------------------- job ----

@dataclass
class Job:
    id: str
    cwd: str
    dir: Path
    cmd_preview: str  # memory only; never written to disk (D12)
    started: float = field(default_factory=_now)
    status: str = "running"  # running | exited | killed
    exit_code: int | None = None
    ended: float | None = None
    proc: asyncio.subprocess.Process | None = None
    buf: RollingBuffer | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    killed: bool = False
    task: asyncio.Task | None = None

    def meta(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "cwd": self.cwd,
            "status": self.status,
            "exit": self.exit_code,
            "started": self.started,
            "ended": self.ended,
            "dropped": self.buf.dropped if self.buf else 0,
        }

    def age(self) -> float:
        return (self.ended or _now()) - self.started


@dataclass
class DiskJob:
    """A job from an earlier boot, read back from disk."""

    id: str
    dir: Path
    meta: dict[str, Any]

    @property
    def status(self) -> str:
        return self.meta.get("status", "unknown")

    @property
    def lost(self) -> bool:
        return self.status == "lost"


def _write_meta(directory: Path, meta: dict[str, Any]) -> None:
    tmp = directory / "meta.json.tmp"
    tmp.write_text(json.dumps(meta))
    os.replace(tmp, directory / "meta.json")


def read_buffer(directory: Path) -> tuple[str, int]:
    """Return (text, dropped_bytes) for a job directory."""
    try:
        data = (directory / "out.log").read_bytes()
    except FileNotFoundError:
        data = b""
    try:
        meta = json.loads((directory / "meta.json").read_text())
    except (FileNotFoundError, ValueError):
        meta = {}
    return data.decode("utf-8", "replace"), int(meta.get("dropped", 0))


# ------------------------------------------------------------ manager ----

def build_env(cfg: Config, extra: dict[str, str] | None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("BASH_ENV", None)
    env.pop("ENV", None)
    path = env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PATH"] = path
    env.update({"NO_COLOR": "1", "TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0"})
    if extra:
        env.update(extra)
    return env


class JobManager:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.root = Path(cfg.state_dir) / "jobs"
        self.boot = secrets.token_hex(4)
        self.boot_started = _now()
        self._seq = 0
        self.jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task] = set()
        self._expired: set[str] = set()
        self._janitor: asyncio.Task | None = None

    # -- lifecycle ----------------------------------------------------

    async def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        exp = self.root.parent / "expired.txt"
        if exp.exists():
            self._expired = set(exp.read_text().split())
        for d in self.root.iterdir():
            meta_file = d / "meta.json"
            if not d.is_dir() or not meta_file.exists():
                continue
            try:
                meta = json.loads(meta_file.read_text())
            except ValueError:
                continue
            if meta.get("status") == "running":
                meta["status"] = "lost"
                meta["lost_at"] = self.boot_started
                _write_meta(d, meta)
        self.sweep()
        self._janitor = asyncio.create_task(self._janitor_loop())

    async def stop(self) -> None:
        if self._janitor:
            self._janitor.cancel()
        for job in list(self.jobs.values()):
            if job.status == "running":
                await self.kill(job)
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _janitor_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            self.sweep()

    def sweep(self) -> None:
        """Delete finished jobs older than the retention; remember their ids."""
        cutoff = _now() - self.cfg.caps.job_expiry_h * 3600
        removed: list[str] = []
        for d in self.root.iterdir():
            if not d.is_dir() or d.name in self.jobs and self.jobs[d.name].status == "running":
                continue
            try:
                meta = json.loads((d / "meta.json").read_text())
                stamp = meta.get("ended") or meta.get("lost_at") or meta.get("started") or 0
                finished = meta.get("status") != "running"
            except (FileNotFoundError, ValueError):
                stamp, finished = d.stat().st_mtime, True
            if finished and stamp < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                self.jobs.pop(d.name, None)
                removed.append(d.name)
        if removed:
            self._expired.update(removed)
            with open(self.root.parent / "expired.txt", "a") as fh:
                fh.write("\n".join(removed) + "\n")

    # -- spawning -----------------------------------------------------

    async def spawn(self, cmd: str, cwd: str, env: dict[str, str]) -> Job:
        self._seq += 1
        job_id = f"{self.boot}-{self._seq}"
        directory = self.root / job_id
        directory.mkdir(parents=True)
        shell = shutil.which(self.cfg.shell, path=env["PATH"]) or self.cfg.shell
        argv = [shell, "--noprofile", "--norc", "-c", cmd] if os.path.basename(shell) == "bash" else [shell, "-c", cmd]
        job = Job(id=job_id, cwd=cwd, dir=directory, cmd_preview=" ".join(cmd.split())[:60])
        job.buf = RollingBuffer(directory / "out.log", self.cfg.caps.job_buffer_bytes, lambda: _write_meta(directory, job.meta()))
        try:
            job.proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError:
            job.buf.close()
            shutil.rmtree(directory, ignore_errors=True)
            fail("INTERNAL", f"shell '{self.cfg.shell}' not found", "Check `shell` in the config")
        except PermissionError:
            job.buf.close()
            shutil.rmtree(directory, ignore_errors=True)
            fail("DENIED", f"cannot execute in {cwd}", "Check directory permissions")
        self.jobs[job_id] = job
        _write_meta(directory, job.meta())
        # Server-owned task: not a child of the request that called us (D30).
        job.task = asyncio.create_task(self._pump(job))
        self._tasks.add(job.task)
        job.task.add_done_callback(self._tasks.discard)
        return job

    async def _read(self, job: Job) -> None:
        assert job.proc and job.proc.stdout and job.buf
        while chunk := await job.proc.stdout.read(65536):
            job.buf.write(chunk)

    async def _pump(self, job: Job) -> None:
        assert job.proc and job.buf
        reader = asyncio.create_task(self._read(job))
        try:
            rc = await job.proc.wait()
            # Background children can hold the pipe open after the shell exits.
            # Give trailing output a moment, then stop reading.
            done, _ = await asyncio.wait({reader}, timeout=_GRACE_AFTER_EXIT_S)
            if not done:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader
            job.exit_code = rc
            job.status = "killed" if job.killed else "exited"
        except asyncio.CancelledError:
            reader.cancel()
            job.status = "killed"
            raise
        finally:
            job.ended = _now()
            job.buf.close()
            with contextlib.suppress(Exception):
                _write_meta(job.dir, job.meta())
            job.done.set()

    # -- control ------------------------------------------------------

    async def wait(self, job: Job, seconds: float) -> bool:
        """Wait for the job to finish. Cancelling the caller never touches the job."""
        if job.done.is_set():
            return True
        try:
            await asyncio.wait_for(job.done.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return False
        return True

    async def kill(self, job: Job) -> None:
        if not job.proc or job.done.is_set():
            return
        job.killed = True
        pgid = job.proc.pid
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGTERM)
        if not await self.wait(job, _KILL_GRACE_S):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
            await self.wait(job, _KILL_GRACE_S)

    def release(self, job: Job) -> None:
        """Forget a job that finished inside the sync window (nothing to come back to)."""
        self.jobs.pop(job.id, None)
        shutil.rmtree(job.dir, ignore_errors=True)

    # -- lookup -------------------------------------------------------

    def lookup(self, job_id: str) -> Job | DiskJob:
        if not isinstance(job_id, str) or not _ID_RE.match(job_id):
            fail("JOB_UNKNOWN", f"'{job_id}' is not a job id", "Ids look like 3fa2b1c4-7; call job with no id to list jobs")
        if job_id in self.jobs:
            return self.jobs[job_id]
        directory = self.root / job_id
        meta_file = directory / "meta.json"
        if meta_file.exists():
            try:
                return DiskJob(job_id, directory, json.loads(meta_file.read_text()))
            except ValueError:
                pass
        if job_id in self._expired:
            fail("JOB_EXPIRED", f"job {job_id} expired (finished jobs are kept {self.cfg.caps.job_expiry_h}h)", "Rerun the command")
        fail("JOB_UNKNOWN", f"no job {job_id}", "Call job with no id to list jobs")

    def list_jobs(self) -> list[tuple[str, str, float, str, str]]:
        """(id, status, age_s, exit, preview) newest first."""
        rows: list[tuple[float, tuple[str, str, float, str, str]]] = []
        seen: set[str] = set()
        for job in self.jobs.values():
            rows.append((job.started, (job.id, job.status, job.age(), exit_text(job.exit_code) if job.done.is_set() else "", job.cmd_preview)))
            seen.add(job.id)
        for d in self.root.iterdir():
            if d.name in seen or not (d / "meta.json").exists():
                continue
            try:
                meta = json.loads((d / "meta.json").read_text())
            except ValueError:
                continue
            ended = meta.get("ended") or meta.get("lost_at") or _now()
            age = ended - meta.get("started", ended)
            rows.append((meta.get("started", 0), (d.name, meta.get("status", "?"), age, exit_text(meta.get("exit")) if meta.get("exit") is not None else "", "")))
        rows.sort(key=lambda r: r[0], reverse=True)
        return [r[1] for r in rows]
