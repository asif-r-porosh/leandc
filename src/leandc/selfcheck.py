"""Startup self-check (spec section 4).

Resolves every expected command through the same PATH that `run` uses (leandc bin
dir first) and checks it is the right flavor. Mismatches are reported, never
silently tolerated.
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

OK, WARN, FAIL = "ok", "warn", "FAIL"


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


def run_check(cfg: Config) -> Report:
    rep = Report()
    env = build_env(cfg, None)
    path = env["PATH"]

    # --- environment facts
    rep.facts["os"] = f"{platform.system()} {platform.machine()}"
    rep.add(OK, "platform", f"{platform.system()} {platform.release()} {platform.machine()}")
    if platform.system() == "Linux":
        libc = " ".join(platform.libc_ver())
        rep.add(OK, "libc", libc or "unknown")

    # --- python
    py = sys.version_info
    rep.facts["python"] = f"{py.major}.{py.minor}"
    rep.add(OK if (py.major, py.minor) == (3, 12) else WARN, "python", f"{py.major}.{py.minor}.{py.micro} (want 3.12)")

    # --- bin dir
    if cfg.bin_dir:
        if os.path.isdir(cfg.bin_dir):
            rep.add(OK, "bin_dir", cfg.bin_dir)
        else:
            rep.add(FAIL, "bin_dir", f"{cfg.bin_dir} does not exist; run bootstrap.sh")
    else:
        rep.add(WARN, "bin_dir", "not set: commands resolve through the plain PATH, flavors are not controlled")

    # --- commands
    for name in cfg.required_commands:
        found = shutil.which(name, path=path)
        rep.add(OK if found else FAIL, name, found or "missing (required)")
    for name in cfg.recommended_commands:
        found = shutil.which(name, path=path)
        rep.add(OK if found else WARN, name, found or "missing (recommended)")

    # --- flavors (each runs through the same PATH `run` uses)
    rc, out = _sh(env, "bash --version | head -1")
    ver = _first_version(out)
    rep.facts["bash"] = ver
    major = int(ver.split(".")[0]) if ver != "?" else 0
    rep.add(OK if major >= 5 else FAIL, "bash version", f"{ver} (want 5 or newer)")

    checks = [
        ("sed GNU", "sed --version 2>&1 | head -1", lambda r, o: r == 0 and "GNU" in o, "GNU sed needed (sed -i without suffix)"),
        ("grep -P", "echo a1 | grep -oP 'a\\K\\d'", lambda r, o: r == 0 and o == "1", "grep with -P needed"),
        ("find -printf", "find . -maxdepth 0 -printf '%p'", lambda r, o: r == 0 and o == ".", "GNU find needed"),
        ("date -d", "date -d '2020-01-02' +%F", lambda r, o: r == 0 and o == "2020-01-02", "GNU date needed"),
        ("stat -c", "stat -c %s /dev/null", lambda r, o: r == 0 and o == "0", "GNU stat needed"),
        ("readlink -f", "readlink -f .", lambda r, o: r == 0 and o.startswith("/"), "GNU readlink needed"),
        ("rg flavor", "rg --version | head -1", lambda r, o: r == 0 and o.startswith("ripgrep"), "ripgrep needed"),
    ]
    for name, script, ok_fn, why in checks:
        rc, out = _sh(env, script)
        rep.add(OK if ok_fn(rc, out) else FAIL, name, out.splitlines()[0][:60] if ok_fn(rc, out) and out else why)
        if name == "rg flavor" and ok_fn(rc, out):
            rep.facts["rg"] = _first_version(out)

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
