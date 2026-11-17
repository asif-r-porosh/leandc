"""Real transports: stdio subprocess and streamable HTTP on localhost (secret path)."""
import socket
import subprocess
import sys
import time

import http.client
import json

import pytest
from mcp import Client, StdioServerParameters

pytestmark = pytest.mark.anyio

SECRET = "s" * 30


@pytest.fixture
def anyio_backend():
    return "asyncio"


def text(res) -> str:
    return "".join(b.text for b in res.content if getattr(b, "type", "") == "text")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_config(tmp_path, body: str) -> str:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    p = tmp_path / "config.toml"
    p.write_text(f'default_cwd = "{work}"\nstate_dir = "{tmp_path / "state"}"\n{body}')
    return str(p)


async def test_stdio_roundtrip(tmp_path):
    cfg = write_config(tmp_path, 'transport = "stdio"\n')
    params = StdioServerParameters(command=sys.executable, args=["-m", "leandc.server", "serve", "--config", cfg])
    async with Client(params) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        assert tools == {"run", "job", "read", "write", "edit"}
        res = await c.call_tool("run", {"cmd": "echo over-stdio"})
        assert "over-stdio" in text(res)
        bad = await c.call_tool("run", {"cmd": "x", "nope": 1})
        assert bad.is_error and text(bad).startswith("ERROR BAD_PARAM")


@pytest.fixture
def http_server(tmp_path):
    port = free_port()
    cfg = write_config(
        tmp_path,
        f'transport = "http"\n[http]\nport = {port}\npath_secret = "{SECRET}"\nallowed_hosts = ["127.0.0.1:{port}"]\n',
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "leandc.server", "serve", "--config", cfg],
        stdout=subprocess.PIPE,  # uvicorn writes its access log to stdout: look at both streams
        stderr=subprocess.STDOUT,
        text=True,
    )
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError(proc.stdout.read())
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("server did not start")
    yield port, proc
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()


async def test_http_roundtrip(http_server):
    port, _ = http_server
    async with Client(f"http://127.0.0.1:{port}/{SECRET}/mcp") as c:
        assert {t.name for t in (await c.list_tools()).tools} == {"run", "job", "read", "write", "edit"}
        res = await c.call_tool("run", {"cmd": "echo over-http"})
        assert "over-http" in text(res)


def _post(port: int, path: str, host: str | None) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("POST", path, skip_host=True)
    conn.putheader("Host", host or f"127.0.0.1:{port}")
    conn.putheader("Accept", "application/json, text/event-stream")
    conn.putheader("Content-Type", "application/json")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    conn.putheader("Content-Length", str(len(body)))
    conn.endheaders(body)
    status = conn.getresponse().status
    conn.close()
    return status


def test_http_wrong_host_and_path(http_server):
    port, _ = http_server
    assert _post(port, f"/{SECRET}/mcp", "evil.example") == 421
    assert _post(port, "/wrong/mcp", None) == 404


def test_http_refuses_weak_secret(tmp_path):
    cfg = write_config(tmp_path, 'transport = "http"\n[http]\npath_secret = "short"\nallowed_hosts = ["x.test"]\n')
    p = subprocess.run([sys.executable, "-m", "leandc.server", "serve", "--config", cfg], capture_output=True, text=True, timeout=30)
    assert p.returncode == 2 and "path_secret" in p.stderr


def test_http_refuses_empty_allowed_hosts(tmp_path):
    cfg = write_config(tmp_path, f'transport = "http"\n[http]\npath_secret = "{SECRET}"\n')
    p = subprocess.run([sys.executable, "-m", "leandc.server", "serve", "--config", cfg], capture_output=True, text=True, timeout=30)
    assert p.returncode == 2 and "allowed_hosts" in p.stderr


def test_bad_config_key_is_reported(tmp_path):
    cfg = write_config(tmp_path, "bogus = 1\n")
    p = subprocess.run([sys.executable, "-m", "leandc.server", "check", "--config", cfg], capture_output=True, text=True, timeout=30)
    assert p.returncode == 2 and "bogus" in p.stderr


def test_check_command_passes_here(tmp_path):
    cfg = write_config(tmp_path, "")
    p = subprocess.run([sys.executable, "-m", "leandc.server", "check", "--config", cfg], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and "self-check" in p.stdout


async def test_http_log_never_contains_secret(http_server):
    port, proc = http_server
    async with Client(f"http://127.0.0.1:{port}/{SECRET}/mcp") as c:
        await c.call_tool("run", {"cmd": "echo x"})
    _post(port, f"/{SECRET}/mcp", "evil.example")
    proc.terminate()
    err = proc.communicate(timeout=10)[0]
    assert SECRET not in err and "listening" in err
    assert "POST /" not in err, "access log must stay off (it would print the secret path)"


def test_http_stop_wait_is_bounded_below_the_unit_limit(tmp_path, monkeypatch):
    """Open connections get a few seconds at stop, never the whole systemd stop window (15 s)."""
    import uvicorn
    from leandc import server, service
    from leandc.config import load_config

    seen = {}

    class FakeServer:
        def __init__(self, config):
            seen["config"] = config

        async def serve(self):
            return None

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    port = free_port()
    cfg_path = write_config(
        tmp_path,
        f'transport = "http"\n[http]\nport = {port}\npath_secret = "{SECRET}"\nallowed_hosts = ["127.0.0.1:{port}"]\n',
    )
    assert server.serve(load_config(cfg_path), "http") == 0
    wait = seen["config"].timeout_graceful_shutdown
    assert wait == server.GRACEFUL_STOP_SECONDS
    limit = int(service.UNIT.split("TimeoutStopSec=")[1].split()[0])
    assert 0 < wait < limit


def test_the_run_description_names_the_transport_that_runs_not_the_one_in_the_config(tmp_path, monkeypatch):
    """`--transport stdio` on a config that says http (and the reverse) must not leave a wrong label."""
    import uvicorn
    from types import SimpleNamespace
    from leandc import server
    from leandc.config import load_config

    seen = []

    def fake_build(cfg, facts):
        seen.append(cfg.transport)
        return SimpleNamespace(server=SimpleNamespace(
            run=lambda kind: None,
            streamable_http_app=lambda **kw: None,
        ))

    class FakeServer:
        def __init__(self, config):
            pass

        async def serve(self):
            return None

    monkeypatch.setattr(server, "build_server", fake_build)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    port = free_port()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    http_cfg = load_config(write_config(
        tmp_path / "a",
        f'transport = "http"\n[http]\nport = {port}\npath_secret = "{SECRET}"\nallowed_hosts = ["127.0.0.1:{port}"]\n',
    ))
    assert server.serve(http_cfg, "stdio") == 0
    stdio_cfg = load_config(write_config(
        tmp_path / "b",
        f'transport = "stdio"\n[http]\nport = {port}\npath_secret = "{SECRET}"\nallowed_hosts = ["127.0.0.1:{port}"]\n',
    ))
    assert server.serve(stdio_cfg, "http") == 0
    assert seen == ["stdio", "http"]
