"""Linux service control: `leandc setup`, `start`, `stop` and `status` for a systemd service.

A service that starts at boot with nobody logged in is a system service, so setup, start and
stop need root. The program is run as root by `sudo leandc ...`, so it must be installed for all
users (`sudo pipx install --global`): a copy in a user's home could be edited by that user.

The service runs as an unprivileged runner and can read the config but not change it:
config folder /etc/leandc (root:leandc 751), config file root:leandc 640, unit file owned by root.

Everything that touches the machine goes through `System`, so the tests need neither root nor systemd.
"""
from __future__ import annotations

import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from .config import ConfigError, load_config
from .server import check_http

GROUP = "leandc"
UNIT_NAME = "leandc.service"
STATE_DIR = "/var/lib/leandc"
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


@dataclass(frozen=True)
class Paths:
    config_dir: Path = Path("/etc/leandc")
    unit: Path = Path("/etc/systemd/system") / UNIT_NAME

    @property
    def config(self) -> Path:
        return self.config_dir / "config.toml"


class System:
    """Everything that touches the machine. Tests replace this class."""

    def is_linux(self) -> bool:
        return sys.platform.startswith("linux")

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
    if not system.is_linux():
        raise ServiceError("service control works on Linux with systemd only for now (the Mac comes later)")
    if not system.has_systemd():
        raise ServiceError("systemd was not found on this machine")
    if need_root and system.euid() != 0:
        raise ServiceError(f"{what} needs root: run it with sudo")


def unsafe_path_reason(system: System, path: str | Path) -> str | None:
    """Why `path` (resolved) or a folder above it could be changed by someone other than root, else None."""
    cur = Path(os.path.realpath(path))
    for part in (cur, *cur.parents):
        st = system.stat(part)
        if st.st_uid != 0:
            return f"{part} is not owned by root"
        if st.st_mode & 0o002 or (st.st_mode & 0o020 and st.st_gid != 0):
            return f"{part} can be changed by users other than root"
    return None


def find_binary(system: System) -> str:
    found = system.which("leandc")
    if not found:
        raise ServiceError(f"the leandc command was not found on PATH; install it for all users: {INSTALL_HINT}")
    return os.path.abspath(found)


def require_root_owned_install(system: System, binary: str) -> None:
    for path in (Path(__file__).parent, Path(binary), Path(binary).parent):
        why = unsafe_path_reason(system, path)
        if why:
            raise ServiceError(
                f"{why}. These commands run leandc as root, so the program must be installed for all users: {INSTALL_HINT}"
            )


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


def pick_editor(system: System) -> list[str]:
    for var in ("VISUAL", "EDITOR"):
        value = system.getenv(var)
        if value and value.strip():
            try:
                parts = shlex.split(value)
            except ValueError:
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


def ensure_group(system: System) -> int:
    gid = system.group_gid(GROUP)
    if gid is None:
        result = system.run(["groupadd", "--system", GROUP])
        gid = system.group_gid(GROUP)
        if result.returncode != 0 or gid is None:
            raise ServiceError(f"could not create the group {GROUP}: {result.stderr.strip()}")
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
    require_root_owned_install(system, binary or sys.argv[0])
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
    require_root_owned_install(system, binary)
    if system.group_gid(GROUP) is None or not paths.config.exists():
        raise ServiceError("no service config yet: run sudo leandc setup first")
    protected, why = config_protection(system, paths)
    if protected is not True:
        raise ServiceError(f"the config is not protected ({why}): run sudo leandc setup, which sets owner and mode")
    problem = config_problem(paths.config)
    if problem:
        raise ServiceError(f"the config cannot run as a service: {problem}")
    runner = resolve_runner(system, username)
    unit = UNIT.format(user=runner, group=GROUP, binary=unit_quote(binary), config=unit_quote(str(paths.config)))
    write_atomic(system, paths.unit, unit, 0o644)
    systemctl(system, "daemon-reload")
    systemctl(system, "enable", UNIT_NAME)
    systemctl(system, "restart", UNIT_NAME)
    for _ in range(5):
        system.sleep(1)
        if system.run(["systemctl", "is-active", UNIT_NAME]).stdout.strip() == "active":
            say(f"leandc is running as {runner} and starts at every boot")
            return 0
    warn(f"the service did not become active; see: journalctl -u {UNIT_NAME} -n 30")
    return 1


def cmd_stop(system: System, paths: Paths) -> int:
    precheck(system, True, "stop")
    if not paths.unit.exists():
        say("the leandc service is not installed; nothing to stop")
        return 0
    systemctl(system, "disable", "--now", UNIT_NAME)
    say("leandc is stopped and will not start at boot")
    return 0


# ------------------------------------------------------------------ status ----

def cmd_status(system: System, paths: Paths) -> int:
    precheck(system, False, "status")
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
