"""The shipped example config must load with the shipped defaults."""
from pathlib import Path

from leandc.config import load_config

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_loads():
    cfg = load_config(ROOT / "config.example.toml")
    assert cfg.target == "vm" and cfg.http.port == 8765 and cfg.http.allowed_hosts == ("leandc.example.com",)
    assert cfg.caps.run_default_chars == 8000
    assert cfg.required_commands == ("bash",)
