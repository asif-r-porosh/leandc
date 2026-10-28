"""bootstrap.sh, example config, and the self-check through a freshly built bin dir."""
import os
import subprocess
import sys
from pathlib import Path

from leandc.config import load_config

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_loads():
    cfg = load_config(ROOT / "config.example.toml")
    assert cfg.target == "vm" and cfg.http.port == 8765 and cfg.http.allowed_hosts == ("leandc.example.com",)
    assert cfg.caps.run_default_chars == 8000


def test_bootstrap_links_then_check_passes(tmp_path):
    bin_dir = tmp_path / "bin"
    p = subprocess.run(
        [str(ROOT / "bootstrap.sh"), "--links-only"],
        env={**os.environ, "LEANDC_BIN": str(bin_dir)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert p.returncode == 0, p.stderr
    assert (bin_dir / "sed").is_symlink() and (bin_dir / "bash").is_symlink()
    work = tmp_path / "work"
    work.mkdir()
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'default_cwd = "{work}"\nstate_dir = "{tmp_path / "st"}"\nbin_dir = "{bin_dir}"\n')
    chk = subprocess.run(
        [sys.executable, "-m", "leandc.server", "check", "--config", str(cfg)], capture_output=True, text=True, timeout=60
    )
    assert chk.returncode == 0, chk.stdout
    assert f"ok   bin_dir        {bin_dir}" in chk.stdout


def test_check_fails_when_required_command_missing(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'default_cwd = "{work}"\nstate_dir = "{tmp_path / "st"}"\nrequired_commands = ["bash", "definitely-not-a-command"]\n'
    )
    chk = subprocess.run(
        [sys.executable, "-m", "leandc.server", "check", "--config", str(cfg)], capture_output=True, text=True, timeout=60
    )
    assert chk.returncode == 1 and "FAIL definitely-not-a-command" in chk.stdout
    serve = subprocess.run(
        [sys.executable, "-m", "leandc.server", "serve", "--transport", "stdio", "--config", str(cfg)],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert serve.returncode == 2 and "refusing to start" in serve.stderr
