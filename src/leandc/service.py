"""Service control: `leandc setup`, `start`, `stop` and `status`, as a systemd unit (Linux) or a launchd daemon (Mac).

A service that starts at boot with nobody logged in is a system service, so setup, start and
stop need root. The program is run as root by `sudo leandc ...`, so it should be installed with sudo
(`sudo pipx install --global`): then its files belong to root and no other user can change them.

The service runs as an unprivileged runner and can read the config but not change it:
config folder /etc/leandc (root:leandc 751), config file root:leandc 640, unit or plist owned by root.

Everything that touches the machine goes through `System`, so the tests need neither root, systemd nor launchd.
"""
from __future__ import annotations

import os
import plistlib
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from .config import ConfigError, load_config
from .server import check_http

GROUP = "leandc"
UNIT_NAME = "leandc.service"
LABEL = "net.leandc.service"  # the launchd label on the Mac
STATE_DIR = "/var/lib/leandc"
MAC_PATH = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
LOG_FILE = "/var/log/leandc.log"  # the Mac has no journal: launchd writes the output here
INSTALL_HINT = "sudo pipx install --global git+https://github.com/asif-r-porosh/leandc"

UNIT = """\
# Written by `leandc start`. Running `sudo leandc start` again rewrites this file.
[Unit]
Description=leandc MCP server
After=network-online.target
Wants=network-online.target

[Service]
User={user}
SupplementaryGroups={group}
StateDirectory=leandc
WorkingDirectory=~
ExecStart={binary} serve --config {config}
Restart=on-failure
RestartSec=3
# Do NOT set KillMode=process: leandc kills its own jobs on stop; stray children must not outlive the service.
KillMode=control-group
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
"""


class ServiceError(Exception):
    """A refusal or failure with a message meant for the person at the terminal."""


def default_unit() -> Path:
    if sys.platform == "darwin":
        return Path("/Library/LaunchDaemons") / f"{LABEL}.plist"
    return Path("/etc/systemd/system") / UNIT_NAME


@dataclass(frozen=True)
class Paths:
    config_dir: Path = Path("/etc/leandc")
    unit: Path = field(default_factory=default_unit)  # the systemd unit, or the launchd plist on the Mac
    state_dir: Path = Path(STATE_DIR)  # the Mac has no StateDirectory=, so `start` creates this
    log: Path = Path(LOG_FILE)

    @property
    def config(self) -> Path:
        return self.config_dir / "config.toml"


class System:
    """Everything that touches the machine. Tests replace this class."""

    def is_linux(self) -> bool:
        return sys.platform.startswith("linux")

    def is_mac(self) -> bool:
        return sys.platform == "darwin"

    def has_systemd(self) -> bool:
        return os.path.isdir("/run/systemd/system")

    def euid(self) -> int:
        return os.geteuid()

    def sudo_user(self) -> str | None:
        return os.environ.get("SUDO_USER") or None

    def getenv(self, key: str) -> str | None:
        return os.environ.get(key)

    def run(self, cmd: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: command not found")

    def user(self, name: str) -> tuple[int, int, str] | None:
        import pwd

        try:
            pw = pwd.getpwnam(name)
        except KeyError:
            return None
        return pw.pw_uid, pw.pw_gid, pw.pw_dir

    def group_gid(self, name: str) -> int | None:
        import grp

        try:
            return grp.getgrnam(name).gr_gid
        except KeyError:
            return None

    def chown(self, path: Path, uid: int, gid: int) -> None:
        os.chown(path, uid, gid)

    def stat(self, path: Path) -> os.stat_result:
        return os.stat(path)

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def interactive(self) -> bool:
        return sys.stdin.isatty()

    def ask(self, prompt: str) -> str:
        return input(prompt)

    def edit(self, cmd: list[str]) -> int:
        return subprocess.call(cmd)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def say(text: str = "") -> None:
    print(text)


def warn(text: str) -> None:
    print(f"leandc: {text}", file=sys.stderr)


# ------------------------------------------------------------------ checks ----

def precheck(system: System, need_root: bool, what: str) -> None:
    if system.is_linux():
        if not system.has_systemd():
            raise ServiceError("systemd was not found on this machine")
    elif not system.is_mac():
        raise ServiceError("service control works on Linux with systemd and on macOS only")
    if need_root and system.euid() != 0:
        raise ServiceError(f"{what} needs root: run it with sudo")


def find_binary(system: System) -> str:
    found = system.which("leandc")
    if not found and os.path.isabs(sys.argv[0]) and os.path.basename(sys.argv[0]) == "leandc":
        found = sys.argv[0]  # started by its full path: sudo's PATH may not hold the install folder
    if not found:
        raise ServiceError(f"the leandc command was not found on PATH; install it with sudo so every user can run it: {INSTALL_HINT}")
    return os.path.abspath(found)


def resolve_runner(system: System, name: str | None) -> str:
    name = name or system.sudo_user()
    if not name:
        raise ServiceError("no user given and SUDO_USER is not set: pass a username, for example: sudo leandc start alice")
    info = system.user(name)
    if info is None:
        raise ServiceError(f"user {name!r} does not exist")
    if info[0] == 0:
        raise ServiceError("root cannot be the runner: pick an unprivileged user")
    return name


def config_protection(system: System, paths: Paths) -> tuple[bool | None, str]:
    """(True, '') when only root can change the config and others cannot read the secret; (None, why) when unknown."""
    try:
        file_st = system.stat(paths.config)
        dir_st = system.stat(paths.config_dir)
    except FileNotFoundError:
        return None, "no config yet"
    except PermissionError:
        return None, "this user cannot inspect the config folder"
    for st, what in ((file_st, "config file"), (dir_st, "config folder")):
        if st.st_uid != 0:
            return False, f"the {what} is not owned by root"
        if st.st_mode & 0o022:
            return False, f"the {what} can be written by group or others"
    if file_st.st_mode & 0o004:
        return False, "the config file can be read by everyone"
    return True, ""


def config_problem(path: Path) -> str | None:
    """Why the config could not run as a service, else None."""
    try:
        cfg = load_config(path)
    except (ConfigError, OSError) as exc:
        return str(exc)
    if cfg.transport != "http":
        return 'a service needs transport = "http" (stdio is for the desktop app)'
    try:
        check_http(cfg.http.path_secret, cfg.http.allowed_hosts)
    except ConfigError as exc:
        return str(exc)
    return None


# ------------------------------------------------------------------ setup ----

def render_template() -> str:
    """The packaged example config with a fresh secret, the service state folder and no host yet."""
    text = resources.files("leandc").joinpath("config.example.toml").read_text(encoding="utf-8")
    secret = secrets.token_urlsafe(32)
    steps: tuple[tuple[str, Any], ...] = (
        (r"^# leandc config\..*$", "# leandc service config. Edit it with: sudo leandc setup"),
        (r"^# Read once at startup\..*\n# so a personal config.*$",
         "# Read once at startup. The file is owned by root: the service runs as a normal user and cannot change it."),
        (r'^(path_secret = )""', lambda m: f'{m.group(1)}"{secret}"'),
        (r'^(state_dir = )"[^"]*"', lambda m: f'{m.group(1)}"{STATE_DIR}"'),
        (r"^(allowed_hosts = )\[[^\]]*\]", lambda m: f"{m.group(1)}[]"),
    )
    for pattern, repl in steps:
        text, n = re.subn(pattern, repl, text, count=1, flags=re.M)
        if n != 1:
            raise ServiceError(f"the packaged config template changed shape (no match for {pattern})")
    return text


def mask_secret(text: str) -> str:
    return re.sub(r"""^(\s*path_secret\s*=\s*)("[^"]*"|'[^']*')""", r'\1"********"', text, flags=re.M)


# Window editors open the file as the normal user, who cannot read the root-only temp copy, and many return at once.
WINDOW_EDITORS = {"code", "code-insiders", "codium", "cursor", "windsurf", "subl", "mate", "atom", "zed", "open", "gedit", "kate"}


def pick_editor(system: System) -> list[str]:
    for var in ("VISUAL", "EDITOR"):
        value = system.getenv(var)
        if value and value.strip():
            try:
                parts = shlex.split(value)
            except ValueError:
                continue
            if parts and os.path.basename(parts[0]) in WINDOW_EDITORS:
                warn(f"{var}={parts[0]} is a window editor and cannot open the root-only config copy; using a terminal editor")
                continue
            if parts:
                return parts
    for name in ("nano", "vi"):
        found = system.which(name)
        if found:
            return [found]
    raise ServiceError("no editor found: set EDITOR or install nano")


def edit_until_valid(system: System, text: str) -> str | None:
    """Open `text` in an editor on a private temp copy until it is a valid service config. None: given up."""
    editor = pick_editor(system)
    tmpdir = tempfile.mkdtemp(prefix="leandc-setup-")  # mode 700
    try:
        path = Path(tmpdir) / "config.toml"
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0o600)
        while True:
            if system.edit(editor + [str(path)]) != 0:
                warn("the editor exited with an error; nothing was installed")
                return None
            problem = config_problem(path)
            if problem is None:
                return path.read_text(encoding="utf-8")
            warn(f"config problem: {problem}")
            if not system.interactive() or system.ask("Edit again? [Y/n] ").strip().lower() in ("n", "no"):
                warn("nothing was installed")
                return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def write_atomic(system: System, path: Path, text: str, mode: int, owner: tuple[int, int] | None = None) -> None:
    tmp = path.with_name(path.name + ".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, mode)
    if owner is not None:
        system.chown(tmp, owner[0], owner[1])
    os.replace(tmp, path)


def free_mac_gid(system: System) -> int | None:
    """The first unused group id in 300 to 399, the range for service groups; None when the list cannot be read."""
    out = system.run(["dscl", ".", "-list", "/Groups", "PrimaryGroupID"])
    if out.returncode != 0:
        return None
    used = {int(line.split()[-1]) for line in out.stdout.splitlines() if line.split() and line.split()[-1].isdigit()}
    return next((gid for gid in range(300, 400) if gid not in used), None)


def ensure_group(system: System) -> int:
    gid = system.group_gid(GROUP)
    if gid is None:
        if system.is_mac():
            wanted = free_mac_gid(system)
            cmd = ["dseditgroup", "-o", "create", "-r", "leandc service"] + (["-i", str(wanted)] if wanted else []) + [GROUP]
        else:
            cmd = ["groupadd", "--system", GROUP]
        result = system.run(cmd)
        for _ in range(5):  # the Mac needs a moment before the new group shows up in lookups
            gid = system.group_gid(GROUP)
            if gid is not None or result.returncode != 0:
                break
            system.sleep(1)
        if result.returncode != 0 or gid is None:
            raise ServiceError(f"could not create the group {GROUP}: {(result.stderr or result.stdout).strip() or 'no reason given'}")
    return gid


def cmd_setup(system: System, paths: Paths, show: bool) -> int:
    if show:
        try:
            text = paths.config.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ServiceError("no service config yet: run sudo leandc setup") from None
        except PermissionError:
            raise ServiceError("the config cannot be read by this user: run sudo leandc setup --show") from None
        print(mask_secret(text), end="")
        return 0
    precheck(system, True, "setup")
    binary = system.which("leandc")
    gid = ensure_group(system)
    existing = paths.config.read_text(encoding="utf-8") if paths.config.exists() else None
    new = edit_until_valid(system, existing if existing is not None else render_template())
    if new is None:
        return 1
    if new == existing:
        say("no changes")
        return 0
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    system.chown(paths.config_dir, 0, gid)
    os.chmod(paths.config_dir, 0o751)
    write_atomic(system, paths.config, new, 0o640, (0, gid))
    say(f"config saved: {paths.config}")
    say("next: sudo leandc start [username]")
    return 0


# ------------------------------------------------------------ start, stop ----

def unit_quote(value: str) -> str:
    return f'"{value}"' if re.search(r"\s", value) else value


def systemctl(system: System, *args: str) -> subprocess.CompletedProcess:
    result = system.run(["systemctl", *args])
    if result.returncode != 0:
        raise ServiceError(f"systemctl {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
    return result


def cmd_start(system: System, paths: Paths, username: str | None) -> int:
    precheck(system, True, "start")
    binary = find_binary(system)
    if system.group_gid(GROUP) is None or not paths.config.exists():
        raise ServiceError("no service config yet: run sudo leandc setup first")
    protected, why = config_protection(system, paths)
    if protected is not True:
        raise ServiceError(f"the config is not protected ({why}): run sudo leandc setup, which sets owner and mode")
    problem = config_problem(paths.config)
    if problem:
        raise ServiceError(f"the config cannot run as a service: {problem}")
    runner = resolve_runner(system, username)
    if system.is_mac():
        install_launchd(system, paths, runner, binary)
    else:
        unit = UNIT.format(user=runner, group=GROUP, binary=unit_quote(binary), config=unit_quote(str(paths.config)))
        write_atomic(system, paths.unit, unit, 0o644)
        systemctl(system, "daemon-reload")
        systemctl(system, "enable", UNIT_NAME)
        systemctl(system, "restart", UNIT_NAME)
    for _ in range(5):
        system.sleep(1)
        if is_running(system):
            say(f"leandc is running as {runner} and starts at every boot")
            return 0
    if system.is_mac():
        warn(f"the service did not start; see the log: {paths.log}")
    else:
        warn(f"the service did not become active; see: journalctl -u {UNIT_NAME} -n 30")
    return 1


def cmd_stop(system: System, paths: Paths) -> int:
    precheck(system, True, "stop")
    if not paths.unit.exists():
        say("the leandc service is not installed; nothing to stop")
        return 0
    if system.is_mac():
        # No enable or disable on the Mac: removing the plist is what keeps it from starting at boot.
        system.run(["launchctl", "bootout", f"system/{LABEL}"])  # fails when not loaded: fine
        paths.unit.unlink()
    else:
        systemctl(system, "disable", "--now", UNIT_NAME)
    say("leandc is stopped and will not start at boot")
    return 0


# ------------------------------------------------------------------ launchd ----

def render_plist(runner: str, binary: str, config: str, log: str) -> str:
    plist = {
        "Label": LABEL,
        "ProgramArguments": [binary, "serve", "--config", config],
        "UserName": runner,
        "GroupName": GROUP,  # the primary group, so the runner reads the config (640, group leandc)
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # restart after a crash, not after a clean stop
        "ThrottleInterval": 3,
        "ExitTimeOut": 15,
        # launchd starts jobs with a bare PATH (/usr/bin:/bin:/usr/sbin:/sbin); `run` needs the usual tool folders
        "EnvironmentVariables": {"PATH": MAC_PATH},
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    return plistlib.dumps(plist).decode("utf-8")


def install_launchd(system: System, paths: Paths, runner: str, binary: str) -> None:
    uid, _, _ = system.user(runner)  # type: ignore[misc]  # resolve_runner checked it exists
    gid = system.group_gid(GROUP) or 0
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.state_dir, 0o755)
    system.chown(paths.state_dir, uid, gid)
    paths.log.touch(exist_ok=True)
    os.chmod(paths.log, 0o640)
    system.chown(paths.log, uid, gid)
    write_atomic(system, paths.unit, render_plist(runner, binary, str(paths.config), str(paths.log)), 0o644, (0, 0))
    system.run(["launchctl", "bootout", f"system/{LABEL}"])  # a changed plist needs a reload; fails when not loaded: fine
    for _ in range(5):  # bootout can still be tearing the old job down; bootstrap then fails for a moment
        result = system.run(["launchctl", "bootstrap", "system", str(paths.unit)])
        if result.returncode == 0:
            return
        system.sleep(1)
    if result.returncode != 0:
        raise ServiceError(f"launchctl bootstrap failed: {(result.stderr or result.stdout).strip()}")


def mac_job(system: System) -> tuple[str, int | None] | None:
    """(state, pid) of the daemon as launchd reports it; None when launchctl cannot tell (or it is not loaded)."""
    result = system.run(["launchctl", "print", f"system/{LABEL}"])
    if result.returncode != 0:
        return None
    state = re.search(r"^\s*state = (\S+)", result.stdout, flags=re.M)
    pid = re.search(r"^\s*pid = (\d+)", result.stdout, flags=re.M)
    return (state.group(1) if state else "?", int(pid.group(1)) if pid else None)


def is_running(system: System) -> bool:
    if system.is_mac():
        job = mac_job(system)
        return job is not None and job[0] == "running"
    return system.run(["systemctl", "is-active", UNIT_NAME]).stdout.strip() == "active"


# ------------------------------------------------------------------ status ----

def status_launchd(system: System, paths: Paths) -> None:
    """The Mac has no enable state: the plist in /Library/LaunchDaemons is what starts at boot."""
    try:
        runner = plistlib.loads(paths.unit.read_bytes()).get("UserName") or "?"
    except (FileNotFoundError, ValueError):
        say("service:  stopped (not installed)")
        say("boot:     disabled")
        return
    job = mac_job(system)
    if job is None and system.euid() != 0:
        # launchctl may refuse a normal user; fall back to a process search
        found = system.run(["pgrep", "-f", f"serve --config {paths.config}"])
        say(f"service:  {'running' if found.returncode == 0 else 'not running'}")
    else:
        say(f"service:  {job[0] if job else 'not running'}" + (f" (pid {job[1]})" if job and job[1] else ""))
    say("boot:     enabled")
    say(f"runner:   {runner}")
    say(f"log:      {paths.log}")


def cmd_status(system: System, paths: Paths) -> int:
    precheck(system, False, "status")
    if system.is_mac():
        status_launchd(system, paths)
    else:
        shown = system.run(
            ["systemctl", "show", UNIT_NAME, "-p", "LoadState", "-p", "ActiveState", "-p", "SubState",
             "-p", "UnitFileState", "-p", "User", "--no-pager"]
        )
        props = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)
        if props.get("LoadState") != "loaded":
            say("service:  not installed")
        else:
            say(f"service:  {props.get('ActiveState', '?')} ({props.get('SubState', '?')})")
            say(f"boot:     {props.get('UnitFileState') or '?'}")
            say(f"runner:   {props.get('User') or '?'}")
    say(f"config:   {paths.config}")
    protected, why = config_protection(system, paths)
    if protected is True:
        say("config protected: yes")
    elif protected is False:
        say(f"config protected: no ({why})")
    else:
        say(f"config protected: unknown ({why})")
    if protected is not None and paths.config.exists():
        if system.euid() == 0:
            try:
                cfg = load_config(paths.config)
                say(f"listen:   {cfg.http.host}:{cfg.http.port} ({cfg.transport})")
            except (ConfigError, OSError) as exc:
                say(f"listen:   config problem: {exc}")
        else:
            say("details:  run sudo leandc status for the transport and port")
    return 0


def run_command(args: Any, system: System | None = None, paths: Paths | None = None) -> int:
    system = system or System()
    paths = paths or Paths()
    try:
        if args.cmd == "setup":
            return cmd_setup(system, paths, args.show)
        if args.cmd == "start":
            return cmd_start(system, paths, args.username)
        if args.cmd == "stop":
            return cmd_stop(system, paths)
        return cmd_status(system, paths)
    except ServiceError as exc:
        warn(str(exc))
        return 2
