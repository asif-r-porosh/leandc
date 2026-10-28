import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from leandc import errors
from leandc.config import Config, ConfigError, load_config
from leandc.shaping import cap, clean, shape_output
from leandc.usage_log import UsageLog, path_hash
from leandc.config import LogConfig


# ---- shaping -------------------------------------------------------------

def test_ansi_stripped_and_declared():
    text, notes = clean("\x1b[31mred\x1b[0m plain\n")
    assert text == "red plain\n"
    assert notes == ["ansi stripped"]


def test_carriage_return_progress_collapsed():
    raw = "start\n10%\r50%\r100%\ndone"
    text, notes = clean(raw)
    assert text == "start\n100%\ndone"
    assert any("progress" in n for n in notes)


def test_crlf_not_treated_as_progress():
    text, notes = clean("a\r\nb\r\n")
    assert text == "a\nb\n"
    assert not notes


def test_repeated_lines_collapsed_with_count():
    raw = "\n".join(["x"] * 10 + ["end"])
    text, notes = clean(raw)
    assert text == "x\n[previous line repeated 9 more times]\nend"
    assert notes == ["9 repeated lines collapsed"]


def test_two_repeats_kept():
    text, notes = clean("a\na\nb")
    assert text == "a\na\nb"
    assert not notes


def test_blank_lines_not_collapsed():
    raw = "a\n\n\n\n\nb"
    text, _ = clean(raw)
    assert text == raw


def test_cap_noop_when_small():
    s = cap("hello", 100)
    assert s.text == "hello" and s.omitted_lines == 0


def test_cap_keeps_head_and_tail_weighted_to_tail():
    lines = [f"line {i:04d}" for i in range(2000)]
    s = cap("\n".join(lines), 1000)
    assert s.text.startswith("line 0000")
    assert s.text.endswith("line 1999")
    assert "omitted" in s.text
    assert len(s.text) <= 1000 + 120  # marker allowance
    head, _, tail = s.text.partition("[... ")
    assert len(head) < 300
    assert s.omitted_lines > 1500


def test_cap_single_huge_line():
    s = cap("x" * 10_000, 1000)
    assert len(s.text) < 1200
    assert "omitted" in s.text


def test_shape_output_declares_everything():
    raw = "\x1b[1mhi\x1b[0m\n" + "\n".join(["rep"] * 5)
    s = shape_output(raw, 10_000)
    note = s.note_line()
    assert note.startswith("[shaped:") and "ansi stripped" in note and "repeated" in note


# ---- errors --------------------------------------------------------------

def test_error_message_format():
    with pytest.raises(ToolError) as exc:
        errors.fail("NOT_FOUND", "no such file /x.", "Check the path")
    assert str(exc.value) == "ERROR NOT_FOUND: no such file /x. Check the path."
    assert errors.code_of(str(exc.value)) == "NOT_FOUND"


def test_error_code_of_non_error():
    assert errors.code_of("exit=0 cwd=/") is None


# ---- config --------------------------------------------------------------

def test_defaults():
    cfg = Config()
    assert cfg.caps.run_default_chars == 8000
    assert cfg.caps.sync_max_s == 120
    assert cfg.caps.write_max_lines == 800


def test_load_config_overrides(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        'target = "mac"\ndefault_cwd = "/tmp"\nallowed_roots = ["/tmp"]\n'
        "[caps]\nrun_default_chars = 4000\n[http]\nport = 9000\npath_secret = \"abc\"\n"
        'allowed_hosts = ["h.example.com"]\n'
    )
    cfg = load_config(p)
    assert cfg.target == "mac" and cfg.default_cwd == "/tmp"
    assert cfg.caps.run_default_chars == 4000
    assert cfg.caps.run_max_chars == 30000  # untouched default
    assert cfg.http.port == 9000 and cfg.http.allowed_hosts == ("h.example.com",)
    assert cfg.allowed_roots == ("/tmp",)


def test_unknown_key_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("bogus = 1\n")
    with pytest.raises(ConfigError, match="bogus"):
        load_config(p)


def test_unknown_section_key_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[caps]\nnope = 1\n")
    with pytest.raises(ConfigError, match="nope"):
        load_config(p)


def test_bad_type_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[caps]\nrun_default_chars = "lots"\n')
    with pytest.raises(ConfigError, match="run_default_chars"):
        load_config(p)


def test_inconsistent_caps_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[caps]\nrun_default_chars = 50000\n")
    with pytest.raises(ConfigError, match="run_default_chars"):
        load_config(p)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


# ---- usage log -----------------------------------------------------------

def test_usage_log_metadata_only(tmp_path):
    log = UsageLog(LogConfig(), str(tmp_path))
    log.record("read", 0.5, True, in_bytes=12, out_bytes=3400, path="/secret/place/file.txt")
    entry = json.loads(log.file.read_text())
    assert entry["tool"] == "read" and entry["out"] == 3400
    assert entry["path"] == path_hash("/secret/place/file.txt")
    assert "/secret" not in log.file.read_text()


def test_usage_log_rotates(tmp_path):
    log = UsageLog(LogConfig(max_bytes=300, keep=2), str(tmp_path))
    for i in range(40):
        log.record("run", 0.1, True, exit_code=0, out_bytes=i)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert "usage.jsonl" in names and "usage.jsonl.1" in names
    assert "usage.jsonl.3" not in names


def test_usage_log_disabled(tmp_path):
    log = UsageLog(LogConfig(enabled=False), str(tmp_path / "x"))
    log.record("run", 0.1, True)
    assert not (tmp_path / "x").exists()
