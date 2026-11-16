"""Service control: setup, start, stop, status. A fake System replaces root, systemd and the editor."""
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from leandc import server, service
from leandc.service import Paths, ServiceError

GOOD = """transport = "http"
state_dir = "/var/lib/leandc"
[http]
port = 8765
path_secret = "{secret}"
allowed_hosts = ["mcp.example.test"]
"""
SECRET = "k" * 30


class FakeSystem(service.System):
    def __init__(self):
        self.linux = True
        self.mac = False
        self.systemd = True
        self.euid_value = 0
        self.sudo = "alice"
        self.env = {}
        self.users = {"alice": (1001, 1001, "/home/alice"), "bob": (1002, 1002, "/home/bob"), "root": (0, 0, "/root")}
        self.groups = {"leandc": 990}
        self.owners = {}          # Path -> uid (default 0, root)
        self.modes = {}           # Path -> mode (default 0o755)
        self.stat_errors = {}     # Path -> exception
        self.found = {"leandc": "/usr/local/bin/leandc", "nano": "/usr/bin/nano", "vi": "/usr/bin/vi"}
        self.calls = []           # every run() command
        self.chowns = []
        self.edits = []
        self.on_edit = lambda path: None
        self.answers = []
        self.asked = []
        self.tty = True
        self.active = "active"
        self.show = ""
        self.job = ""             # what `launchctl print` prints (Mac); empty: the job is not loaded
        self.pgrep = 1            # exit code of pgrep
        self.fail = set()         # command prefixes (tuples) that return 1

    def is_linux(self):
        return self.linux

    def is_mac(self):
        return self.mac

    def has_systemd(self):
        return self.systemd

    def euid(self):
        return self.euid_value

    def sudo_user(self):
        return self.sudo

    def getenv(self, key):
        return self.env.get(key)

    def run(self, cmd):
        self.calls.append(list(cmd))
        for prefix in self.fail:
            if tuple(cmd[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(cmd, 1, "", "boom")
        if cmd[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(cmd, 0, self.active + "\n", "")
        if cmd[:2] == ["systemctl", "show"]:
            return subprocess.CompletedProcess(cmd, 0, self.show, "")
        if cmd[0] == "groupadd" or cmd[:3] == ["dseditgroup", "-o", "create"]:
            self.groups[service.GROUP] = 991
        if cmd[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(cmd, 0 if self.job else 113, self.job, "")
        if cmd[0] == "pgrep":
            return subprocess.CompletedProcess(cmd, self.pgrep, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def user(self, name):
        return self.users.get(name)

    def group_gid(self, name):
        return self.groups.get(name)

    def chown(self, path, uid, gid):
        self.chowns.append((Path(path), uid, gid))

    def stat(self, path):
        path = Path(path)
        if path in self.stat_errors:
            raise self.stat_errors[path]
        return SimpleNamespace(
            st_uid=self.owners.get(path, 0), st_gid=0, st_mode=self.modes.get(path, 0o755)
        )

    def which(self, name):
        return self.found.get(name)

    def interactive(self):
        return self.tty

    def ask(self, prompt):
        self.asked.append(prompt)
        return self.answers.pop(0)

    def edit(self, cmd):
        self.edits.append(list(cmd))
        self.on_edit(Path(cmd[-1]))
        return 0

    def sleep(self, seconds):
        pass


@pytest.fixture
def fake():
    return FakeSystem()


@pytest.fixture
def paths(tmp_path):
    return Paths(config_dir=tmp_path / "etc" / "leandc", unit=tmp_path / "units" / "leandc.service",
                 state_dir=tmp_path / "var" / "lib" / "leandc", log=tmp_path / "var" / "log" / "leandc.log")


def fill_host(path: Path):
    path.write_text(path.read_text().replace("allowed_hosts = []", 'allowed_hosts = ["mcp.example.test"]'))


def install_protected_config(fake, paths, text=None):
    paths.config_dir.mkdir(parents=True)
    paths.unit.parent.mkdir(parents=True)
    paths.config.write_text(text or GOOD.format(secret=SECRET))
    fake.modes[paths.config] = 0o640
    fake.modes[paths.config_dir] = 0o751


def args(cmd, **kw):
    return SimpleNamespace(cmd=cmd, show=kw.get("show", False), username=kw.get("username"))


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# ------------------------------------------------------------- template ----

def test_template_has_fresh_secret_service_state_and_no_host(tmp_path):
    a, b = service.render_template(), service.render_template()
    secrets_ = []
    for text in (a, b):
        line = next(l for l in text.splitlines() if l.startswith("path_secret"))
        secrets_.append(line.split('"')[1])
    assert secrets_[0] != secrets_[1]
    assert all(len(s) >= server.MIN_SECRET_LEN and "/" not in s for s in secrets_)
    assert 'state_dir = "/var/lib/leandc"' in a
    assert "allowed_hosts = []" in a
    assert a.splitlines()[0].startswith("# leandc service config")
    p = tmp_path / "c.toml"
    p.write_text(a)
    assert "allowed_hosts is empty" in service.config_problem(p)
    fill_host(p)
    assert service.config_problem(p) is None


def test_config_problem_rejects_stdio_unknown_key_and_short_secret(tmp_path):
    p = tmp_path / "c.toml"
    for text, fragment in (
        ('transport = "stdio"\n', "needs transport"),
        ('target = "vm"\n', "unknown key"),
        (GOOD.format(secret="short"), "path_secret"),
    ):
        p.write_text(text)
        assert fragment in service.config_problem(p)


def test_mask_secret_hides_only_the_secret():
    text = GOOD.format(secret=SECRET)
    masked = service.mask_secret(text)
    assert SECRET not in masked and 'path_secret = "********"' in masked
    assert 'port = 8765' in masked and 'allowed_hosts = ["mcp.example.test"]' in masked


def test_pick_editor_order(fake):
    assert service.pick_editor(fake) == ["/usr/bin/nano"]
    fake.found.pop("nano")
    assert service.pick_editor(fake) == ["/usr/bin/vi"]
    fake.env["EDITOR"] = "code -w"
    assert service.pick_editor(fake) == ["code", "-w"]
    fake.env["VISUAL"] = "emacs"
    assert service.pick_editor(fake) == ["emacs"]
    fake.found.pop("vi")
    fake.env.clear()
    with pytest.raises(ServiceError, match="no editor"):
        service.pick_editor(fake)


# ---------------------------------------------------------------- setup ----

def test_setup_first_run_installs_a_protected_config(fake, paths):
    fake.groups.clear()
    fake.on_edit = fill_host
    assert service.run_command(args("setup"), fake, paths) == 0
    assert ["groupadd", "--system", "leandc"] in fake.calls
    assert fake.edits and fake.edits[0][0] == "/usr/bin/nano"
    assert 'allowed_hosts = ["mcp.example.test"]' in paths.config.read_text()
    assert mode_of(paths.config) == 0o640 and mode_of(paths.config_dir) == 0o751
    assert (paths.config_dir / "config.toml.new", 0, 991) in fake.chowns   # chown happens before the atomic rename
    assert (paths.config_dir, 0, 991) in fake.chowns
    assert not (paths.config_dir / "config.toml.new").exists()
    temp = Path(fake.edits[0][-1]).parent
    assert not temp.exists()


def test_setup_invalid_then_fixed_asks_to_edit_again(fake, paths, capsys):
    fake.answers = ["y"]
    state = {"n": 0}

    def edit(path):
        state["n"] += 1
        if state["n"] == 2:
            fill_host(path)

    fake.on_edit = edit
    assert service.run_command(args("setup"), fake, paths) == 0
    assert state["n"] == 2 and fake.asked == ["Edit again? [Y/n] "]
    assert "allowed_hosts is empty" in capsys.readouterr().err


def test_setup_invalid_without_a_terminal_installs_nothing(fake, paths):
    fake.tty = False
    assert service.run_command(args("setup"), fake, paths) == 1
    assert not paths.config.exists()


def test_setup_answer_no_installs_nothing(fake, paths):
    fake.answers = ["n"]
    assert service.run_command(args("setup"), fake, paths) == 1
    assert not paths.config.exists()


def test_setup_without_changes_leaves_the_file_alone(fake, paths, capsys):
    install_protected_config(fake, paths)
    before = paths.config.stat().st_mtime_ns
    assert service.run_command(args("setup"), fake, paths) == 0
    assert "no changes" in capsys.readouterr().out
    assert paths.config.stat().st_mtime_ns == before and not fake.chowns


def test_setup_edits_the_existing_config(fake, paths):
    install_protected_config(fake, paths)
    fake.on_edit = lambda p: p.write_text(p.read_text().replace("8765", "9001"))
    assert service.run_command(args("setup"), fake, paths) == 0
    assert "port = 9001" in paths.config.read_text()


def test_setup_needs_root(fake, paths, capsys):
    fake.euid_value = 1000
    assert service.run_command(args("setup"), fake, paths) == 2
    assert "needs root" in capsys.readouterr().err
    assert not paths.config.exists()


def test_setup_show_hides_the_secret(fake, paths, capsys):
    install_protected_config(fake, paths)
    assert service.run_command(args("setup", show=True), fake, paths) == 0
    out = capsys.readouterr().out
    assert SECRET not in out and "********" in out


def test_setup_show_without_config_or_access(fake, paths, capsys, monkeypatch):
    assert service.run_command(args("setup", show=True), fake, paths) == 2
    assert "no service config yet" in capsys.readouterr().err
    install_protected_config(fake, paths)
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError()))
    assert service.run_command(args("setup", show=True), fake, paths) == 2
    assert "sudo leandc setup --show" in capsys.readouterr().err


# ---------------------------------------------------------------- start ----

def test_start_writes_the_unit_and_enables_it(fake, paths, capsys):
    install_protected_config(fake, paths)
    assert service.run_command(args("start"), fake, paths) == 0
    unit = paths.unit.read_text()
    assert "User=alice" in unit and "SupplementaryGroups=leandc" in unit
    assert f"ExecStart=/usr/local/bin/leandc serve --config {paths.config}" in unit
    assert "KillMode=control-group" in unit and "TimeoutStopSec=15" in unit and "WantedBy=multi-user.target" in unit
    assert mode_of(paths.unit) == 0o644 and not (paths.unit.parent / "leandc.service.new").exists()
    systemctl = [c for c in fake.calls if c[0] == "systemctl" and c[1] in ("daemon-reload", "enable", "restart")]
    assert systemctl == [["systemctl", "daemon-reload"], ["systemctl", "enable", "leandc.service"],
                         ["systemctl", "restart", "leandc.service"]]
    assert "running as alice" in capsys.readouterr().out


def test_start_with_another_username_rewrites_the_unit(fake, paths):
    install_protected_config(fake, paths)
    assert service.run_command(args("start"), fake, paths) == 0
    assert service.run_command(args("start", username="bob"), fake, paths) == 0
    assert "User=bob" in paths.unit.read_text() and "User=alice" not in paths.unit.read_text()


@pytest.mark.parametrize("sudo, name, fragment", [
    (None, None, "no user given"),
    ("alice", "root", "root cannot be the runner"),
    ("alice", "nobody-here", "does not exist"),
    ("root", None, "root cannot be the runner"),
])
def test_start_refuses_a_bad_runner(fake, paths, capsys, sudo, name, fragment):
    install_protected_config(fake, paths)
    fake.sudo = sudo
    assert service.run_command(args("start", username=name), fake, paths) == 2
    assert fragment in capsys.readouterr().err
    assert not paths.unit.exists()


def test_start_refuses_without_config_group_or_protection(fake, paths, capsys):
    assert service.run_command(args("start"), fake, paths) == 2
    assert "run sudo leandc setup first" in capsys.readouterr().err
    install_protected_config(fake, paths)
    fake.owners[paths.config] = 1001          # the runner owns the file: it could edit the config
    assert service.run_command(args("start"), fake, paths) == 2
    assert "not owned by root" in capsys.readouterr().err
    fake.owners.clear()
    fake.modes[paths.config] = 0o644          # readable by everyone: the secret would leak
    assert service.run_command(args("start"), fake, paths) == 2
    assert "read by everyone" in capsys.readouterr().err
    fake.modes[paths.config] = 0o660          # writable by the group
    assert service.run_command(args("start"), fake, paths) == 2
    assert "written by group or others" in capsys.readouterr().err
    fake.modes[paths.config] = 0o640
    fake.groups.clear()
    assert service.run_command(args("start"), fake, paths) == 2
    assert not paths.unit.exists()


def test_start_refuses_an_invalid_config(fake, paths, capsys):
    install_protected_config(fake, paths, 'transport = "stdio"\n')
    assert service.run_command(args("start"), fake, paths) == 2
    assert "cannot run as a service" in capsys.readouterr().err
    assert not paths.unit.exists()


def test_start_without_the_command_on_path(fake, paths, capsys):
    install_protected_config(fake, paths)
    fake.found.pop("leandc")
    assert service.run_command(args("start"), fake, paths) == 2
    assert "not found on PATH" in capsys.readouterr().err


def test_start_reports_a_service_that_does_not_come_up(fake, paths, capsys):
    install_protected_config(fake, paths)
    fake.active = "failed"
    assert service.run_command(args("start"), fake, paths) == 1
    assert "journalctl -u leandc.service" in capsys.readouterr().err


def test_start_reports_a_failing_systemctl(fake, paths, capsys):
    install_protected_config(fake, paths)
    fake.fail.add(("systemctl", "enable"))
    assert service.run_command(args("start"), fake, paths) == 2
    assert "systemctl enable leandc.service failed" in capsys.readouterr().err


# ----------------------------------------------------------------- stop ----

def test_stop_disables_the_service(fake, paths, capsys):
    install_protected_config(fake, paths)
    paths.unit.write_text("unit")
    assert service.run_command(args("stop"), fake, paths) == 0
    assert ["systemctl", "disable", "--now", "leandc.service"] in fake.calls
    assert "not start at boot" in capsys.readouterr().out


def test_stop_when_nothing_is_installed(fake, paths, capsys):
    assert service.run_command(args("stop"), fake, paths) == 0
    assert not fake.calls and "not installed" in capsys.readouterr().out


def test_stop_needs_root(fake, paths, capsys):
    fake.euid_value = 1000
    assert service.run_command(args("stop"), fake, paths) == 2
    assert "needs root" in capsys.readouterr().err


# --------------------------------------------------------------- status ----

SHOWN = "LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=enabled\nUser=alice\n"


def test_status_for_an_ordinary_user(fake, paths, capsys):
    install_protected_config(fake, paths)
    fake.euid_value = 1001
    fake.show = SHOWN
    assert service.run_command(args("status"), fake, paths) == 0
    out = capsys.readouterr().out
    assert "service:  active (running)" in out and "boot:     enabled" in out and "runner:   alice" in out
    assert "config protected: yes" in out and "run sudo leandc status" in out and "8765" not in out


def test_status_with_sudo_adds_transport_and_port(fake, paths, capsys):
    install_protected_config(fake, paths)
    fake.show = SHOWN
    assert service.run_command(args("status"), fake, paths) == 0
    assert "listen:   127.0.0.1:8765 (http)" in capsys.readouterr().out


def test_status_not_installed_and_unprotected(fake, paths, capsys):
    install_protected_config(fake, paths)
    fake.euid_value = 1001
    fake.show = "LoadState=not-found\n"
    fake.owners[paths.config] = 1001
    assert service.run_command(args("status"), fake, paths) == 0
    out = capsys.readouterr().out
    assert "service:  not installed" in out and "config protected: no (the config file is not owned by root)" in out


def test_status_when_the_config_folder_cannot_be_inspected(fake, paths, capsys):
    fake.euid_value = 1001
    fake.show = SHOWN
    fake.stat_errors[paths.config] = PermissionError()
    assert service.run_command(args("status"), fake, paths) == 0
    assert "config protected: unknown (this user cannot inspect the config folder)" in capsys.readouterr().out


def test_status_without_a_config(fake, paths, capsys):
    fake.euid_value = 1001
    fake.show = "LoadState=not-found\n"
    fake.stat_errors[paths.config] = FileNotFoundError()
    assert service.run_command(args("status"), fake, paths) == 0
    assert "config protected: unknown (no config yet)" in capsys.readouterr().out


# ------------------------------------------------------- platform, wiring ----

def test_not_linux_and_no_systemd(fake, paths, capsys):
    fake.linux = False
    for cmd in ("start", "stop", "status", "setup"):
        assert service.run_command(args(cmd), fake, paths) == 2
        assert "Linux with systemd and on macOS only" in capsys.readouterr().err
    fake.linux, fake.systemd = True, False
    assert service.run_command(args("status"), fake, paths) == 2
    assert "systemd was not found" in capsys.readouterr().err


def test_the_new_subcommands_are_wired_into_main(fake, monkeypatch, capsys):
    monkeypatch.setattr(service, "System", lambda: fake)
    fake.euid_value = 1001
    fake.show = "LoadState=not-found\n"
    assert server.main(["status"]) == 0
    assert "service:  not installed" in capsys.readouterr().out
    assert server.main(["setup", "--show"]) == 2
    with pytest.raises(SystemExit):
        server.main(["start", "alice", "extra"])


def test_the_unit_template_keeps_its_stop_and_restart_rules():
    unit = service.UNIT.format(user="u", group="g", binary="/b/leandc", config="/c/config.toml")
    for line in ("KillMode=control-group", "TimeoutStopSec=15", "Restart=on-failure", "RestartSec=3",
                 "After=network-online.target", "WantedBy=multi-user.target"):
        assert line in unit


def test_the_real_system_class_basics(tmp_path):
    import pwd

    real = service.System()
    me = pwd.getpwuid(os.getuid())
    assert real.user(me.pw_name)[0] == os.getuid()
    assert real.user("no-such-user-xyz") is None
    assert real.group_gid("no-such-group-xyz") is None
    assert real.run(["no-such-command-xyz"]).returncode == 127
    assert real.run(["echo", "hi"]).stdout.strip() == "hi"
    assert real.stat(tmp_path).st_uid == os.getuid()
    assert real.which("no-such-command-xyz") is None


# ------------------------------------------------------------- the Mac ----

@pytest.fixture
def mac(fake, paths):
    fake.linux, fake.mac, fake.systemd = False, True, False
    fake.job = "system/net.leandc.service = {\n\tstate = running\n\tpid = 4242\n}\n"
    paths.log.parent.mkdir(parents=True)
    return fake


def test_mac_start_writes_a_plist_and_loads_it(mac, paths, capsys):
    import plistlib

    install_protected_config(mac, paths)
    assert service.run_command(args("start"), mac, paths) == 0
    plist = plistlib.loads(paths.unit.read_bytes())
    assert plist["Label"] == "net.leandc.service"
    assert plist["ProgramArguments"] == ["/usr/local/bin/leandc", "serve", "--config", str(paths.config)]
    assert plist["UserName"] == "alice" and plist["GroupName"] == "leandc"
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(paths.log)
    assert plist["EnvironmentVariables"]["PATH"].split(":")[-4:] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    assert mode_of(paths.unit) == 0o644 and (paths.unit.with_name(paths.unit.name + ".new"), 0, 0) in mac.chowns
    launchctl = [c for c in mac.calls if c[0] == "launchctl" and c[1] in ("bootout", "bootstrap")]
    assert launchctl == [["launchctl", "bootout", "system/net.leandc.service"],
                         ["launchctl", "bootstrap", "system", str(paths.unit)]]
    assert not any(c[0] == "systemctl" for c in mac.calls)
    assert "running as alice" in capsys.readouterr().out


def test_mac_start_prepares_state_folder_and_log_for_the_runner(mac, paths):
    install_protected_config(mac, paths)
    assert service.run_command(args("start"), mac, paths) == 0
    assert paths.state_dir.is_dir() and paths.log.is_file() and mode_of(paths.log) == 0o640
    assert (paths.state_dir, 1001, 990) in mac.chowns and (paths.log, 1001, 990) in mac.chowns


def test_mac_start_with_another_username_rewrites_the_plist(mac, paths):
    import plistlib

    install_protected_config(mac, paths)
    assert service.run_command(args("start"), mac, paths) == 0
    assert service.run_command(args("start", username="bob"), mac, paths) == 0
    assert plistlib.loads(paths.unit.read_bytes())["UserName"] == "bob"


def test_mac_start_reports_a_failing_bootstrap(mac, paths, capsys):
    install_protected_config(mac, paths)
    mac.fail.add(("launchctl", "bootstrap"))
    assert service.run_command(args("start"), mac, paths) == 2
    assert "launchctl bootstrap failed" in capsys.readouterr().err


def test_mac_start_says_so_when_the_job_does_not_run(mac, paths, capsys):
    install_protected_config(mac, paths)
    mac.job = ""
    assert service.run_command(args("start"), mac, paths) == 1
    assert str(paths.log) in capsys.readouterr().err


def test_mac_setup_creates_the_group_with_dseditgroup(mac, paths):
    mac.groups.clear()
    mac.on_edit = fill_host
    assert service.run_command(args("setup"), mac, paths) == 0
    assert ["dseditgroup", "-o", "create", "-r", "leandc service", "leandc"] in mac.calls
    assert not any(c[0] == "groupadd" for c in mac.calls)


def test_mac_stop_unloads_and_removes_the_plist(mac, paths, capsys):
    install_protected_config(mac, paths)
    assert service.run_command(args("start"), mac, paths) == 0
    mac.calls.clear()
    assert service.run_command(args("stop"), mac, paths) == 0
    assert ["launchctl", "bootout", "system/net.leandc.service"] in mac.calls
    assert not paths.unit.exists()
    assert "will not start at boot" in capsys.readouterr().out
    assert service.run_command(args("stop"), mac, paths) == 0  # again: nothing to do
    assert "not installed" in capsys.readouterr().out


def test_mac_stop_needs_root(mac, paths, capsys):
    mac.euid_value = 1001
    assert service.run_command(args("stop"), mac, paths) == 2
    assert "stop needs root" in capsys.readouterr().err


def test_mac_status_running(mac, paths, capsys):
    install_protected_config(mac, paths)
    assert service.run_command(args("start"), mac, paths) == 0
    capsys.readouterr()
    mac.euid_value = 1001
    assert service.run_command(args("status"), mac, paths) == 0
    out = capsys.readouterr().out
    assert "service:  running (pid 4242)" in out and "boot:     enabled" in out and "runner:   alice" in out
    assert f"log:      {paths.log}" in out and "config protected: yes" in out


def test_mac_status_without_root_falls_back_to_a_process_search(mac, paths, capsys):
    install_protected_config(mac, paths)
    assert service.run_command(args("start"), mac, paths) == 0
    capsys.readouterr()
    mac.euid_value = 1001
    mac.job = ""                     # launchctl refuses a normal user
    mac.pgrep = 0
    assert service.run_command(args("status"), mac, paths) == 0
    assert "service:  running" in capsys.readouterr().out
    mac.pgrep = 1
    assert service.run_command(args("status"), mac, paths) == 0
    assert "service:  not running" in capsys.readouterr().out


def test_mac_status_when_stopped(mac, paths, capsys):
    install_protected_config(mac, paths)
    mac.euid_value = 1001
    assert service.run_command(args("status"), mac, paths) == 0
    out = capsys.readouterr().out
    assert "service:  stopped (not installed)" in out and "boot:     disabled" in out


def test_default_plist_path_depends_on_the_platform(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    assert service.Paths().unit == Path("/Library/LaunchDaemons/net.leandc.service.plist")
    monkeypatch.setattr(service.sys, "platform", "linux")
    assert service.Paths().unit == Path("/etc/systemd/system/leandc.service")
