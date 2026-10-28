"""Startup self-check.

leandc uses what the OS ships (BACKLOG N1), so this only *insists* on bash and on a
writable state dir. Everything else is information: which OS and userland flavor
(Claude writes commands for it), and which optional tools happen to be installed.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

from .config import Config
from .jobs import build_env

OK, INFO, WARN, FAIL = "ok", "info", "warn", "FAIL"


@dataclass
class Item:
    level: str
    name: str
    detail: str


@dataclass
class Report:
    items: list[Item] = field(default_factory=list)
    facts: dict[str, str] = field(default_factory=dict)

    def add(self, level: str, name: str, detail: str) -> None:
        self.items.append(Item(level, name, detail))

    @property
    def failed(self) -> bool:
        return any(i.level == FAIL for i in self.items)

    @property
    def warned(self) -> bool:
        return any(i.level == WARN for i in self.items)

    def render(self) -> str:
        lines = [f"{i.level:<4} {i.name:<14} {i.detail}" for i in self.items]
        verdict = "FAILED" if self.failed else ("passed with warnings" if self.warned else "passed")
        return "\n".join(lines + [f"self-check {verdict}"])


def _sh(env: dict[str, str], script: str, timeout: float = 10) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", script],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return p.returncode, (p.stdout + p.stderr).strip()


def _first_version(text: str) -> str:
    m = re.search(r"\d+(?:\.\d+)+", text)
    return m.group(0) if m else "?"


def _os_name() -> str:
    """Human-readable OS, e.g. 'macOS 15.1' or 'Ubuntu 24.04.4 LTS'."""
    if platform.system() == "Darwin":
        return f"macOS {platform.mac_ver()[0]}".strip()
    try:
        with open("/etc/os-release") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return f"{platform.system()} {platform.release()}"


def _flavor(env: dict[str, str]) -> str:
    """Userland flavor: GNU (Linux), BSD (macOS) or busybox, judged from `ls --version`."""
    rc, out = _sh(env, "ls --version 2>&1 | head -1")
    if rc == 0 and "GNU" in out:
        return "GNU"
    if "BusyBox" in out or "busybox" in out:
        return "busybox"
    return "BSD" if platform.system() in ("Darwin", "FreeBSD", "OpenBSD", "NetBSD") else "unknown"


def run_check(cfg: Config) -> Report:
    rep = Report()
    env = build_env(cfg, None)
    path = env["PATH"]

    # --- what Claude needs to know to write commands
    rep.facts["os"] = _os_name()
    rep.facts["flavor"] = _flavor(env)
    rep.add(INFO, "os", f"{rep.facts['os']} {platform.machine()}")
    rep.add(INFO, "userland", f"{rep.facts['flavor']} (commands are written for this flavor)")

    # --- python (a plain requirement)
    py = sys.version_info
    rep.facts["python"] = f"{py.major}.{py.minor}"
    rep.add(OK if (py.major, py.minor) >= (3, 12) else FAIL, "python", f"{py.major}.{py.minor}.{py.micro} (need 3.12 or newer)")

    # --- shell: the one command we insist on
    for name in cfg.required_commands:
        found = shutil.which(name, path=path)
        rep.add(OK if found else FAIL, name, found or "missing (required)")
    rc, out = _sh(env, "bash --version | head -1")
    ver = _first_version(out)
    rep.facts["bash"] = ver
    major = int(ver.split(".")[0]) if ver != "?" else 0
    rep.add(OK if major >= 3 else FAIL, "bash version", f"{ver}" + (" (old but works)" if major < 5 else ""))

    # --- optional tools: information only, never a warning
    for name in cfg.recommended_commands:
        found = shutil.which(name, path=path)
        rep.add(INFO, name, found or "not installed (optional)")

    # --- state dir writable
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        probe = os.path.join(cfg.state_dir, ".probe")
        with open(probe, "w") as fh:
            fh.write("x")
        os.unlink(probe)
        rep.add(OK, "state_dir", cfg.state_dir)
    except OSError as exc:
        rep.add(FAIL, "state_dir", f"{cfg.state_dir} is not writable: {exc.strerror}")

    # --- default cwd
    if os.path.isdir(cfg.default_cwd):
        rep.add(OK, "default_cwd", cfg.default_cwd)
    else:
        rep.add(FAIL, "default_cwd", f"{cfg.default_cwd} does not exist")
    return rep
