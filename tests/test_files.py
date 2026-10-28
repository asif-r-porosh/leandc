import os
import stat

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from leandc.config import Caps, Config
from leandc import files


@pytest.fixture
def cfg(tmp_path):
    return Config(default_cwd=str(tmp_path), state_dir=str(tmp_path / "state"))


def err(callable_, *a, **k) -> str:
    with pytest.raises(ToolError) as exc:
        callable_(*a, **k)
    return str(exc.value)


def make(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content)
    return str(p)


# ----------------------------------------------------------------- read ----

def test_read_small_file(cfg, tmp_path):
    p = make(tmp_path, "a.txt", "one\ntwo\nthree\n")
    out = files.read_file(cfg, p)
    assert out.splitlines()[0] == f"{p} lines 1-3 of 3 (14 B)"
    assert "1\tone" in out and "3\tthree" in out
    assert "next offset" not in out


def test_read_pagination_next_offset(cfg, tmp_path):
    p = make(tmp_path, "a.txt", "".join(f"l{i}\n" for i in range(1, 11)))
    out = files.read_file(cfg, p, offset=0, limit=4)
    assert "lines 1-4 of 10" in out and "next offset=4" in out
    out2 = files.read_file(cfg, p, offset=4, limit=4)
    assert "lines 5-8 of 10" in out2 and "5\tl5" in out2
    out3 = files.read_file(cfg, p, offset=8, limit=4)
    assert "lines 9-10 of 10" in out3 and "next offset" not in out3


def test_read_tail(cfg, tmp_path):
    p = make(tmp_path, "a.txt", "".join(f"l{i}\n" for i in range(1, 101)))
    out = files.read_file(cfg, p, offset=-3)
    assert "lines 98-100 of 100" in out and "100\tl100" in out and "97\t" not in out


def test_read_default_is_300_lines(cfg, tmp_path):
    p = make(tmp_path, "big.txt", "".join(f"l{i}\n" for i in range(1, 1001)))
    out = files.read_file(cfg, p)
    assert "lines 1-300 of 1000" in out and "next offset=300" in out


def test_read_limit_above_max_rejected(cfg, tmp_path):
    p = make(tmp_path, "a.txt", "x\n")
    assert "BAD_PARAM" in err(files.read_file, cfg, p, limit=1001)


def test_read_no_trailing_newline(cfg, tmp_path):
    p = make(tmp_path, "a.txt", "a\nb")
    assert "lines 1-2 of 2" in files.read_file(cfg, p)


def test_read_char_cap_stops_early(tmp_path):
    cfg = Config(default_cwd=str(tmp_path), caps=Caps(read_max_chars=200))
    p = make(tmp_path, "w.txt", "".join("x" * 50 + "\n" for _ in range(20)))
    out = files.read_file(cfg, p)
    assert "next offset=" in out
    assert len(out) < 400


def test_read_long_line_cut(cfg, tmp_path):
    p = make(tmp_path, "l.txt", "y" * 5000 + "\n")
    out = files.read_file(cfg, p)
    assert "...[+3000 chars]" in out


def test_read_empty(cfg, tmp_path):
    p = make(tmp_path, "e.txt", "")
    assert files.read_file(cfg, p).endswith("empty (0 bytes)")


def test_read_not_found(cfg, tmp_path):
    assert err(files.read_file, cfg, str(tmp_path / "nope")).startswith("ERROR NOT_FOUND:")


def test_read_directory(cfg, tmp_path):
    msg = err(files.read_file, cfg, str(tmp_path))
    assert msg.startswith("ERROR IS_DIR:") and "eza" in msg


def test_read_binary_refused(cfg, tmp_path):
    p = tmp_path / "b.bin"
    p.write_bytes(b"\x00\x01\x02" * 100)
    assert err(files.read_file, cfg, str(p)).startswith("ERROR BINARY:")


def test_read_offset_past_end(cfg, tmp_path):
    p = make(tmp_path, "a.txt", "a\nb\n")
    assert "past the end" in err(files.read_file, cfg, p, offset=5)


def test_read_relative_path_uses_default_cwd(cfg, tmp_path):
    make(tmp_path, "rel.txt", "hi\n")
    assert str(tmp_path / "rel.txt") in files.read_file(cfg, "rel.txt")


def test_read_crlf_line_endings_stripped(cfg, tmp_path):
    p = tmp_path / "c.txt"
    p.write_bytes(b"a\r\nb\r\n")
    assert "1\ta\n2\tb" in files.read_file(cfg, str(p))


def test_allowed_roots_blocks_outside_and_symlink_escape(tmp_path):
    inside = tmp_path / "in"
    inside.mkdir()
    outside = tmp_path / "out"
    outside.mkdir()
    (outside / "secret.txt").write_text("s\n")
    (inside / "link").symlink_to(outside / "secret.txt")
    cfg = Config(default_cwd=str(inside), allowed_roots=(str(inside),))
    assert err(files.read_file, cfg, str(outside / "secret.txt")).startswith("ERROR DENIED:")
    assert err(files.read_file, cfg, str(inside / "link")).startswith("ERROR DENIED:")


# ---------------------------------------------------------------- write ----

def test_write_new_file_default_create(cfg, tmp_path):
    p = str(tmp_path / "sub" / "dir" / "n.txt")
    out = files.write_file(cfg, p, "a\nb\n")
    assert "mode=create" in out and "total_lines=2" in out and "sha256=" in out
    assert open(p).read() == "a\nb\n"
    assert "a\nb" not in out  # content never echoed


def test_write_existing_requires_mode(cfg, tmp_path):
    p = make(tmp_path, "x.txt", "old\n")
    msg = err(files.write_file, cfg, p, "new\n")
    assert msg.startswith("ERROR EXISTS:") and "1 lines" in msg and "overwrite" in msg
    assert open(p).read() == "old\n"  # untouched: the DC clobber bug


def test_write_explicit_create_on_existing_refused(cfg, tmp_path):
    p = make(tmp_path, "x.txt", "old\n")
    assert err(files.write_file, cfg, p, "new\n", "create").startswith("ERROR EXISTS:")


def test_write_existing_empty_file_needs_no_mode(cfg, tmp_path):
    p = make(tmp_path, "x.txt", "")
    files.write_file(cfg, p, "fill\n")
    assert open(p).read() == "fill\n"


def test_write_overwrite_keeps_permissions(cfg, tmp_path):
    p = make(tmp_path, "x.sh", "old\n")
    os.chmod(p, 0o750)
    files.write_file(cfg, p, "new\n", "overwrite")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o750
    assert open(p).read() == "new\n"


def test_write_append(cfg, tmp_path):
    p = make(tmp_path, "x.txt", "a\n")
    out = files.write_file(cfg, p, "b\nc\n", "append")
    assert "mode=append" in out and "total_lines=3" in out
    assert open(p).read() == "a\nb\nc\n"


def test_write_append_to_missing_creates(cfg, tmp_path):
    p = str(tmp_path / "new.txt")
    files.write_file(cfg, p, "z\n", "append")
    assert open(p).read() == "z\n"


def test_write_too_large(cfg, tmp_path):
    p = str(tmp_path / "big.txt")
    msg = err(files.write_file, cfg, p, "x\n" * 801)
    assert msg.startswith("ERROR TOO_LARGE:") and "append" in msg
    assert not os.path.exists(p)


def test_write_exactly_cap_ok(cfg, tmp_path):
    files.write_file(cfg, str(tmp_path / "ok.txt"), "x\n" * 800)


def test_write_bad_mode(cfg, tmp_path):
    assert err(files.write_file, cfg, str(tmp_path / "a"), "x", "replace").startswith("ERROR BAD_PARAM:")


def test_write_directory_target(cfg, tmp_path):
    assert err(files.write_file, cfg, str(tmp_path), "x").startswith("ERROR IS_DIR:")


def test_write_through_symlink_keeps_link(cfg, tmp_path):
    real = make(tmp_path, "real.txt", "old\n")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    files.write_file(cfg, str(link), "new\n", "overwrite")
    assert link.is_symlink() and open(real).read() == "new\n"


def test_write_leaves_no_temp_files(cfg, tmp_path):
    files.write_file(cfg, str(tmp_path / "t.txt"), "x\n")
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".leandc-")] == []


# ----------------------------------------------------------------- edit ----

def test_edit_unique_returns_region(cfg, tmp_path):
    p = make(tmp_path, "c.py", "".join(f"line{i}\n" for i in range(1, 21)))
    out = files.edit_file(cfg, p, "line10\n", "LINE TEN\nextra\n")
    assert "1 replacement, file now 21 lines" in out
    assert "10\tLINE TEN" in out and "11\textra" in out
    assert "7\tline7" in out and "14\tline13" in out  # 3 lines of context each side
    assert "line5" not in out
    assert open(p).read().count("LINE TEN") == 1


def test_edit_no_match_shows_closest(cfg, tmp_path):
    p = make(tmp_path, "c.py", "def foo():\n    return 1\n\ndef bar():\n    return 2\n")
    msg = err(files.edit_file, cfg, p, "def baar():\n    return 2", "x")
    assert msg.startswith("ERROR NO_MATCH:")
    assert "Closest match at lines 4-5" in msg
    assert len(msg) < 700
    assert open(p).read().startswith("def foo")  # unchanged


def test_edit_no_match_without_similar_text(cfg, tmp_path):
    p = make(tmp_path, "c.py", "alpha\nbeta\n")
    msg = err(files.edit_file, cfg, p, "zzzzzzzzzzzzzzzz", "x")
    assert "NO_MATCH" in msg and "No similar text" in msg


def test_edit_multi_match_reports_lines(cfg, tmp_path):
    p = make(tmp_path, "m.txt", "a\nfoo\nb\nfoo\nc\nfoo\n")
    msg = err(files.edit_file, cfg, p, "foo", "bar")
    assert msg.startswith("ERROR MULTI_MATCH:") and "3 times" in msg and "2, 4, 6" in msg
    assert open(p).read().count("foo") == 3


def test_edit_replace_all(cfg, tmp_path):
    p = make(tmp_path, "m.txt", "a\nfoo\nb\nfoo\nc\nfoo\n")
    out = files.edit_file(cfg, p, "foo", "bar", replace_all=True)
    assert "3 replacements" in out
    assert open(p).read() == "a\nbar\nb\nbar\nc\nbar\n"


def test_edit_preserves_crlf(cfg, tmp_path):
    p = tmp_path / "w.txt"
    p.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    files.edit_file(cfg, str(p), "two\nthree", "TWO\nTHREE")  # model sends LF
    assert p.read_bytes() == b"one\r\nTWO\r\nTHREE\r\n"


def test_edit_preserves_mode_and_symlink(cfg, tmp_path):
    real = make(tmp_path, "real.sh", "echo a\n")
    os.chmod(real, 0o755)
    link = tmp_path / "l.sh"
    link.symlink_to(real)
    files.edit_file(cfg, str(link), "echo a", "echo b")
    assert link.is_symlink()
    assert stat.S_IMODE(os.stat(real).st_mode) == 0o755
    assert open(real).read() == "echo b\n"


def test_edit_identical_and_empty_rejected(cfg, tmp_path):
    p = make(tmp_path, "a.txt", "x\n")
    assert "identical" in err(files.edit_file, cfg, p, "x", "x")
    assert "non-empty" in err(files.edit_file, cfg, p, "", "x")


def test_edit_deletion_shows_region(cfg, tmp_path):
    p = make(tmp_path, "a.txt", "".join(f"l{i}\n" for i in range(1, 11)))
    out = files.edit_file(cfg, p, "l5\n", "")
    assert "file now 9 lines" in out and "l6" in out and "l5" not in out.split("\n", 1)[1]


def test_edit_binary_and_missing(cfg, tmp_path):
    b = tmp_path / "b.bin"
    b.write_bytes(b"\x00abc")
    assert err(files.edit_file, cfg, str(b), "abc", "x").startswith("ERROR BINARY:")
    assert err(files.edit_file, cfg, str(tmp_path / "nope"), "a", "b").startswith("ERROR NOT_FOUND:")


def test_edit_non_utf8_refused(cfg, tmp_path):
    p = tmp_path / "l1.txt"
    p.write_bytes(b"caf\xe9\n")
    assert "not valid UTF-8" in err(files.edit_file, cfg, str(p), "caf", "x")


def test_edit_many_regions_limited(cfg, tmp_path):
    p = make(tmp_path, "r.txt", "".join(f"x{i}\nfiller\nfiller\nfiller\nfiller\nfiller\nfiller\nfiller\n" for i in range(10)))
    out = files.edit_file(cfg, p, "x", "y", replace_all=True)
    assert "10 replacements" in out and "more changed regions not shown" in out
