"""Server wiring: five tools, one middleware, two entry points (D1, D3, D22-D30).

No `from __future__ import annotations` here: the SDK builds each tool's input
schema from the real annotations on the functions below.
"""
import argparse
import asyncio
import contextlib
import inspect
import json
import logging
import re
import secrets
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextContent

from . import __version__
from .cmdnames import command_names
from .config import Config, ConfigError, load_config
from .errors import code_of, message
from .files import edit_file, read_file, write_file
from .jobs import JobManager, build_env
from .procs import job_command, run_command
from .selfcheck import run_check
from .usage_log import UsageLog

log = logging.getLogger("leandc")

MIN_SECRET_LEN = 24


# ------------------------------------------------------------ descriptions ----

def info_line(cfg: Config, facts: dict[str, str]) -> str:
    """The one-line environment summary carried in the `run` description (D29)."""
    caps = cfg.caps
    bits = [f"leandc {__version__} target={cfg.target}", f"default cwd {cfg.default_cwd}"]
    if facts.get("bash"):
        bits.append(f"bash {facts['bash']}")
    if facts.get("python"):
        bits.append(f"python {facts['python']}")
    if facts.get("os"):
        bits.insert(1, f"{facts['os']}, {facts.get('flavor', '?')} userland")
    have = [c for c in cfg.recommended_commands if shutil.which(c, path=build_env(cfg, None)["PATH"])]
    if have:
        bits.append("also installed: " + " ".join(have))
    bits.append(f"output cap {caps.run_default_chars} (max {caps.run_max_chars}) chars")
    return "; ".join(bits) + "."


def describe_run(cfg: Config, facts: dict[str, str]) -> str:
    caps = cfg.caps
    return (
        "Run a shell command (bash, non-interactive, stdin closed, no rc files). "
        f"{info_line(cfg, facts)} "
        "Output is cleaned (ANSI, progress bars, repeated lines) and capped at max_chars, keeping head and tail. "
        f"timeout = seconds to wait (default {caps.sync_default_s}, max {caps.sync_max_s}); if the command is still running "
        "you get a job id and it keeps running: use the job tool. "
        "Result starts with `exit=<code> cwd=<dir>`. cwd is per call and not remembered between calls. "
        "Use the commands this OS ships and write them for the userland named above."
    )


DESC_JOB = (
    "List jobs, or read, wait for, or kill one. id comes from a run that outlived its timeout. "
    "action: output (default), status, kill. Read with tail=N, or offset/limit, and grep=<regex>; "
    "wait=<seconds, max 120> returns when the job exits. Jobs do not survive a service restart; output is kept 24 h."
)
DESC_READ = (
    "Read a text file with line numbers. Default 300 lines (max 1000); the header gives `next offset` when more remains; "
    "negative offset reads from the end. Text only: no images, PDFs, spreadsheets."
)
DESC_WRITE = (
    "Write a text file. Parent directories are created; the write is atomic. mode: create | overwrite | append, "
    "required when the file exists and is not empty. Max 800 lines per call: split larger content into create + append. "
    "Content is not echoed back."
)
DESC_EDIT = (
    "Replace exact text in a file. old must match exactly once unless replace_all is true. "
    "Returns the changed region with line numbers, so no re-read is needed. On a miss it shows the closest match."
)


# -------------------------------------------------------------- middleware ----

def _as_dict(result: Any) -> dict[str, Any] | None:
    """call_next hands back the wire form (a dict); accept the model form too."""
    if isinstance(result, dict):
        return result
    if isinstance(result, CallToolResult):
        return result.model_dump(by_alias=True, exclude_none=True, mode="json")
    return None


def _result_text(result: Any) -> str | None:
    d = _as_dict(result)
    if d is None:
        return None
    for block in d.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text", "")
    return None


def _is_error(result: Any) -> bool:
    d = _as_dict(result)
    return bool(d and d.get("isError"))


def _error_result(text: str) -> dict[str, Any]:
    res = CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)
    return res.model_dump(by_alias=True, exclude_none=True, mode="json")


_VALIDATION = re.compile(r"\d+ validation errors? for ")


def _one_line_validation(text: str, tool: str, valid: tuple[str, ...]) -> str:
    """Turn a pydantic dump into `field: problem; field: problem` (D23)."""
    lines = text.splitlines()
    problems: list[str] = []
    for i, line in enumerate(lines):
        if i == 0 or not line or line[0] == " ":
            continue
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        problems.append(f"{line.strip()}: {nxt.split(' [type=')[0]}")
    what = "; ".join(problems) if problems else "arguments do not match the tool schema"
    return message("BAD_PARAM", what, f"{tool} accepts: {', '.join(valid)}")


def _normalize(tool: str, result: Any, valid: tuple[str, ...]) -> Any:
    """Make error results one clean accurate message (D23, D26)."""
    if not _is_error(result):
        return result
    text = _result_text(result)
    if text is None:
        return result
    prefix = f"Error executing tool {tool}"
    if text == prefix:
        return _error_result(
            message("INTERNAL", f"{tool} crashed inside the server", "Details are in the server log; retry once, then report it")
        )
    if text.startswith(prefix + ": "):
        text = text[len(prefix) + 2 :]
    if _VALIDATION.match(text):
        text = _one_line_validation(text, tool, valid)
    return _error_result(text)


def make_middleware(valid_params: dict[str, tuple[str, ...]], usage: UsageLog):
    async def middleware(ctx, call_next):
        if ctx.method != "tools/call":
            return await call_next(ctx)
        params = ctx.params or {}
        tool = params.get("name")
        valid = valid_params.get(tool)
        if valid is None:
            return await call_next(ctx)  # unknown tool: the SDK answers
        args = params.get("arguments") or {}
        started = time.monotonic()

        unknown = sorted(k for k in args if k not in valid)
        if unknown:
            result: Any = _error_result(
                message("BAD_PARAM", f"unknown parameter(s): {', '.join(unknown)}", f"{tool} accepts: {', '.join(valid)}")
            )
        else:
            try:
                result = _normalize(tool, await call_next(ctx), valid)
            except (asyncio.CancelledError, MCPError):
                raise
            except Exception:  # noqa: BLE001
                log.exception("tool %s failed outside the tool body", tool)
                result = _error_result(message("INTERNAL", f"{tool} failed inside the server", "Details are in the server log"))

        _record(usage, tool, args, result, time.monotonic() - started)
        return result

    return middleware


def _record(usage: UsageLog, tool: str, args: dict[str, Any], result: Any, dur: float) -> None:
    text = _result_text(result) or ""
    is_err = _is_error(result)
    exit_code = None
    m = re.match(r"exit=(-?\d+)\b", text)
    if m:
        exit_code = int(m.group(1))
    path = args.get("path")
    cmd = args.get("cmd")
    usage.record(
        tool,
        dur,
        ok=not is_err,
        code=code_of(text) if is_err else None,
        exit_code=exit_code,
        in_bytes=len(json.dumps(args, default=str)),
        out_bytes=len(text.encode("utf-8", "replace")),
        path=path if isinstance(path, str) else None,
        cmds=command_names(cmd) if tool == "run" and isinstance(cmd, str) else None,
    )


# ------------------------------------------------------------------- build ----

@dataclass
class App:
    server: MCPServer
    cfg: Config
    jobs: JobManager
    usage: UsageLog


def build_server(cfg: Config, facts: dict[str, str] | None = None) -> App:
    facts = facts or {}
    jobs = JobManager(cfg)
    usage = UsageLog(cfg.log, cfg.state_dir)

    @contextlib.asynccontextmanager
    async def lifespan(_server):
        await jobs.start()
        try:
            yield {}
        finally:
            await jobs.stop()

    server = MCPServer("leandc", lifespan=lifespan)

    @server.tool(name="run", description=describe_run(cfg, facts), structured_output=False)
    async def run(
        cmd: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
        max_chars: int | None = None,
    ) -> str:
        return await run_command(cfg, jobs, build_env, cmd, cwd, env, timeout, max_chars)

    @server.tool(name="job", description=DESC_JOB, structured_output=False)
    async def job(
        id: str | None = None,
        action: str | None = None,
        offset: int | None = None,
        limit: int | None = None,
        tail: int | None = None,
        grep: str | None = None,
        wait: int | None = None,
    ) -> str:
        return await job_command(cfg, jobs, id, action, offset, limit, tail, grep, wait)

    @server.tool(name="read", description=DESC_READ, structured_output=False)
    async def read(path: str, offset: int | None = None, limit: int | None = None) -> str:
        return await asyncio.to_thread(read_file, cfg, path, offset, limit)

    @server.tool(name="write", description=DESC_WRITE, structured_output=False)
    async def write(path: str, content: str, mode: str | None = None) -> str:
        return await asyncio.to_thread(write_file, cfg, path, content, mode)

    @server.tool(name="edit", description=DESC_EDIT, structured_output=False)
    async def edit(path: str, old: str, new: str, replace_all: bool = False) -> str:
        return await asyncio.to_thread(edit_file, cfg, path, old, new, replace_all)

    valid = {fn.__name__: tuple(inspect.signature(fn).parameters) for fn in (run, job, read, write, edit)}
    server.middleware.append(make_middleware(valid, usage))
    return App(server, cfg, jobs, usage)


# ------------------------------------------------------------------ entry ----

def _http_settings(cfg: Config) -> tuple[str, TransportSecuritySettings]:
    import os

    secret = os.environ.get("LEANDC_PATH_SECRET") or cfg.http.path_secret
    if len(secret) < MIN_SECRET_LEN or "/" in secret:
        raise ConfigError(
            f"http.path_secret must be at least {MIN_SECRET_LEN} characters without '/'; generate one with: leandc secret"
        )
    hosts: list[str] = []
    for h in cfg.http.allowed_hosts:
        hosts += [h, f"{h}:443"]
    if not hosts:
        raise ConfigError("http.allowed_hosts is empty: list the public hostname claude.ai will call")
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=["https://claude.ai", "https://claude.com"],
    )
    return secret, security


def serve(cfg: Config, transport: str, ignore_check: bool = False) -> int:
    report = run_check(cfg)
    print(report.render(), file=sys.stderr)
    if report.failed and not ignore_check:
        print("refusing to start: fix the FAIL lines above (or pass --ignore-check)", file=sys.stderr)
        return 2
    app = build_server(cfg, report.facts)
    if transport == "stdio":
        app.server.run("stdio")
        return 0
    secret, security = _http_settings(cfg)
    print(f"leandc listening on {cfg.http.host}:{cfg.http.port} path=/<secret>/mcp", file=sys.stderr)
    # We run uvicorn ourselves (MCPServer.run cannot turn the access log off): that log prints the
    # secret path on every request, and systemd would keep it in the journal.
    import uvicorn

    asgi = app.server.streamable_http_app(
        streamable_http_path=f"/{secret}/mcp",
        transport_security=security,
        host=cfg.http.host,
    )
    config = uvicorn.Config(asgi, host=cfg.http.host, port=cfg.http.port, log_level="info", access_log=False)
    asyncio.run(uvicorn.Server(config).serve())
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="leandc", description="lean desktop commander MCP server")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("serve", "run the server"), ("check", "run the self-check and exit")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--config", help="path to config.toml (default: $LEANDC_CONFIG, else built-in defaults)")
        if name == "serve":
            p.add_argument("--transport", choices=["stdio", "http"], help="default: stdio for target=mac, http for target=vm")
            p.add_argument("--ignore-check", action="store_true", help="start even if the self-check fails")
    sub.add_parser("secret", help="print a new random URL path secret")
    sub.add_parser("version", help="print the version")
    args = ap.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "version":
        print(__version__)
        return 0
    if args.cmd == "secret":
        print(secrets.token_urlsafe(32))
        return 0
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "check":
        report = run_check(cfg)
        print(report.render())
        return 1 if report.failed else 0
    transport = args.transport or ("stdio" if cfg.target == "mac" else "http")
    try:
        return serve(cfg, transport, args.ignore_check)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
