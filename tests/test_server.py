"""End-to-end through a real MCP client (in-memory transport): tool set, D22/D23/D26/D29, usage log."""
import json
from dataclasses import replace

import pytest
from mcp import Client

from leandc.config import Caps, Config
from leandc.server import build_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def cfg(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    return Config(default_cwd=str(work), state_dir=str(tmp_path / "state"), caps=Caps())


@pytest.fixture
async def client(cfg):
    app = build_server(cfg, {"bash": "5.2", "python": "3.12"})
    async with Client(app.server) as c:
        c.app = app
        yield c


def text(res) -> str:
    return "".join(b.text for b in res.content if getattr(b, "type", "") == "text")


async def call(c, name, **args):
    return await c.call_tool(name, args)


async def test_tool_set_and_descriptions(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"run", "job", "read", "write", "edit"}
    run_desc = tools["run"].description
    assert "target=vm" in run_desc and "bash 5.2" in run_desc and "output cap 8000" in run_desc  # D29
    for t in tools.values():
        assert t.output_schema is None  # plain text results only


async def test_run_roundtrip(client, cfg):
    res = await call(client, "run", cmd="echo hi")
    assert not res.is_error
    assert text(res).splitlines()[0].startswith("exit=0 cwd=")
    assert text(res).splitlines()[1] == "hi"


async def test_write_read_edit_roundtrip(client, cfg):
    p = f"{cfg.default_cwd}/a.txt"
    assert not (await call(client, "write", path=p, content="one\ntwo\nthree\n")).is_error
    r = await call(client, "read", path=p)
    assert "two" in text(r)
    e = await call(client, "edit", path=p, old="two", new="TWO")
    assert not e.is_error and "TWO" in text(e)
    assert "TWO" in text(await call(client, "read", path=p))


async def test_unknown_parameter_rejected(client):  # D22
    res = await call(client, "run", cmd="echo hi", timout=5)
    assert res.is_error
    t = text(res)
    assert t.startswith("ERROR BAD_PARAM:") and "timout" in t and "timeout" in t
    assert "\n" not in t


async def test_missing_required_is_one_line(client):  # D23
    res = await call(client, "run")
    assert res.is_error
    t = text(res)
    assert t.startswith("ERROR BAD_PARAM:") and "cmd" in t
    assert "\n" not in t and "pydantic" not in t and "http" not in t


async def test_wrong_type_is_one_line(client):
    res = await call(client, "run", cmd="echo hi", timeout="soon")
    assert res.is_error
    t = text(res)
    assert t.startswith("ERROR BAD_PARAM:") and "timeout" in t and "\n" not in t


async def test_tool_error_text_reaches_model(client, cfg):  # D26
    res = await call(client, "read", path=f"{cfg.default_cwd}/nope.txt")
    assert res.is_error
    t = text(res)
    assert t.startswith("ERROR NOT_FOUND:")
    assert "Error executing tool" not in t


async def test_masked_crash_becomes_internal(client, monkeypatch):
    import leandc.server as srv

    def boom(*a, **k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(srv, "read_file", boom)
    res = await call(client, "read", path="/etc/hostname")
    assert res.is_error
    t = text(res)
    assert t.startswith("ERROR INTERNAL:") and "secret detail" not in t


async def test_usage_log_is_metadata_only(client, cfg):
    p = f"{cfg.default_cwd}/log.txt"
    await call(client, "write", path=p, content="TOPSECRET-CONTENT\n")
    await call(client, "run", cmd="echo TOPSECRET-COMMAND")
    await call(client, "read", path=f"{cfg.default_cwd}/missing")
    raw = client.app.usage.file.read_text()
    assert "TOPSECRET" not in raw and cfg.default_cwd not in raw and "echo" not in raw
    rows = [json.loads(line) for line in raw.splitlines()]
    assert [r["tool"] for r in rows] == ["write", "run", "read"]
    assert rows[1]["exit"] == 0 and rows[1]["ok"] is True
    assert rows[2]["ok"] is False and rows[2]["code"] == "NOT_FOUND"
    assert rows[0]["path"] and len(rows[0]["path"]) == 8


async def test_parallel_calls(client):
    import anyio

    results = []

    async def one(i):
        results.append(text(await call(client, "run", cmd=f"sleep 0.5; echo n{i}")))

    async with anyio.create_task_group() as tg:
        for i in range(5):
            tg.start_soon(one, i)
    assert sorted(r.splitlines()[1] for r in results) == [f"n{i}" for i in range(5)]


async def test_long_command_becomes_job_and_job_reads(client):
    res = await call(client, "run", cmd="echo start; sleep 3; echo done", timeout=1)
    t = text(res)
    assert t.startswith("running job=")
    jid = t.split("job=")[1].split()[0]
    out = await call(client, "job", id=jid, wait=10)
    assert "done" in text(out) and "exit=0" in text(out)


def test_info_line_states_os_and_flavor(cfg):
    from leandc.server import info_line

    line = info_line(cfg, {"os": "macOS 15.1", "flavor": "BSD", "bash": "3.2", "python": "3.12"})
    assert "macOS 15.1, BSD userland" in line and "bash 3.2" in line and "GNU" not in line


def test_selfcheck_only_insists_on_bash(cfg):
    from dataclasses import replace as rep

    from leandc.selfcheck import run_check

    ok = run_check(cfg)
    assert not ok.failed and ok.facts["flavor"] in {"GNU", "BSD", "busybox", "unknown"} and ok.facts["os"]
    odd = run_check(rep(cfg, recommended_commands=("definitely-not-installed",)))
    assert not odd.failed and not odd.warned  # an absent optional tool is information, never a warning
    bad = run_check(rep(cfg, required_commands=("bash", "definitely-not-installed")))
    assert bad.failed
