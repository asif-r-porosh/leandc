"""Configuration: a TOML file read at startup. No tool can change it."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Caps:
    read_default_lines: int = 300
    read_max_lines: int = 1000
    read_max_chars: int = 40_000
    read_line_chars: int = 2_000
    run_default_chars: int = 8_000
    run_max_chars: int = 30_000
    sync_default_s: int = 30
    sync_max_s: int = 120
    job_wait_max_s: int = 120
    job_buffer_bytes: int = 1_000_000
    job_expiry_h: int = 24
    write_max_lines: int = 800
    edit_diff_chars: int = 400


@dataclass(frozen=True)
class HttpConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    path_secret: str = ""
    allowed_hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class LogConfig:
    enabled: bool = True
    max_bytes: int = 1_000_000
    keep: int = 3


@dataclass(frozen=True)
class Config:
    target: str = "vm"
    default_cwd: str = field(default_factory=lambda: str(Path.home()))
    shell: str = "bash"
    state_dir: str = field(default_factory=lambda: str(Path.home() / ".local" / "state" / "leandc"))
    allowed_roots: tuple[str, ...] = ()
    required_commands: tuple[str, ...] = ("bash",)
    recommended_commands: tuple[str, ...] = ("git", "rg", "fd", "eza", "bat", "jq")
    caps: Caps = field(default_factory=Caps)
    http: HttpConfig = field(default_factory=HttpConfig)
    log: LogConfig = field(default_factory=LogConfig)


_SECTIONS: dict[str, type] = {"caps": Caps, "http": HttpConfig, "log": LogConfig}
_TUPLE_KEYS = {"allowed_roots", "required_commands", "recommended_commands", "allowed_hosts"}


def _expand(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value))


def _build(cls: type, raw: dict[str, Any], where: str) -> Any:
    names = {f.name for f in fields(cls)}
    unknown = sorted(set(raw) - names)
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(unknown)}")
    kwargs: dict[str, Any] = {}
    defaults = cls()
    for key, value in raw.items():
        if key in _SECTIONS:
            continue
        current = getattr(defaults, key)
        if key in _TUPLE_KEYS:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"{where}.{key} must be a list of strings")
            value = tuple(_expand(v) if key == "allowed_roots" else v for v in value)
        elif isinstance(current, bool):
            if not isinstance(value, bool):
                raise ConfigError(f"{where}.{key} must be true or false")
        elif isinstance(current, int):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ConfigError(f"{where}.{key} must be a non-negative integer")
        elif isinstance(current, str) or current is None:
            if not isinstance(value, str):
                raise ConfigError(f"{where}.{key} must be a string")
            if key in {"default_cwd", "state_dir"}:
                value = _expand(value)
        kwargs[key] = value
    return replace(defaults, **kwargs)


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load config from `path`, else $LEANDC_CONFIG, else defaults."""
    path = path or os.environ.get("LEANDC_CONFIG")
    if not path:
        return Config()
    try:
        with open(_expand(str(path)), "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config file is not valid TOML: {exc}") from None

    top = {k: v for k, v in raw.items() if k not in _SECTIONS}
    cfg = _build(Config, top, "config")
    parts: dict[str, Any] = {}
    for name, cls in _SECTIONS.items():
        section = raw.get(name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"[{name}] must be a table")
        parts[name] = _build(cls, section, name)
    cfg = replace(cfg, **parts)
    if cfg.target not in {"vm", "mac"}:
        raise ConfigError("target must be 'vm' or 'mac'")
    if cfg.caps.run_default_chars > cfg.caps.run_max_chars:
        raise ConfigError("caps.run_default_chars exceeds caps.run_max_chars")
    if cfg.caps.sync_default_s > cfg.caps.sync_max_s:
        raise ConfigError("caps.sync_default_s exceeds caps.sync_max_s")
    return cfg
