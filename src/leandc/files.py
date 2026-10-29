"""read, write and edit. All functions are synchronous and
are meant to be called through `asyncio.to_thread` so the server loop never blocks.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import stat
import tempfile
from collections import deque

from .config import Config
from .errors import fail

_UMASK = os.umask(0)
os.umask(_UMASK)

_EDIT_MAX_BYTES = 20 * 1024 * 1024
_SNIFF = 8192


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def resolve(cfg: Config, path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        fail("BAD_PARAM", "path is empty", "Pass an absolute path")
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(cfg.default_cwd, p)
    p = os.path.normpath(p)
    if cfg.allowed_roots:
        real = os.path.realpath(p)
        roots = [os.path.realpath(r) for r in cfg.allowed_roots]
        if not any(real == r or real.startswith(r.rstrip("/") + "/") for r in roots):
            fail("DENIED", f"{p} is outside the allowed roots", f"Allowed: {', '.join(roots)}")
    return p


def _stat(p: str) -> os.stat_result:
    try:
        return os.stat(p)
    except FileNotFoundError:
        fail("NOT_FOUND", f"no such file or directory: {p}", "Check the path; use run with `ls` or `find` to look around")
    except NotADirectoryError:
        fail("NOT_FOUND", f"a parent of {p} is not a directory", "Check the path")
    except PermissionError:
        fail("DENIED", f"permission denied: {p}", "The server user cannot read it")


def _line_count(p: str) -> int:
    n = 0
    last = b"\n"
    with open(p, "rb") as fh:
        while chunk := fh.read(1 << 20):
            n += chunk.count(b"\n")
            last = chunk[-1:]
    return n + (0 if last == b"\n" else 1)


# ---------------------------------------------------------------- read ----

def read_file(cfg: Config, path: str, offset: int | None = None, limit: int | None = None) -> str:
    caps = cfg.caps
    p = resolve(cfg, path)
    st = _stat(p)
    if stat.S_ISDIR(st.st_mode):
        fail("IS_DIR", f"{p} is a directory", f"Use run with `ls -la {p}`")
    if not stat.S_ISREG(st.st_mode):
        fail("BAD_PARAM", f"{p} is not a regular file", "Use run to inspect it")

    offset = 0 if offset is None else offset
    if limit is not None and not 1 <= limit <= caps.read_max_lines:
        fail("BAD_PARAM", f"limit must be 1 to {caps.read_max_lines}", f"Got {limit}")
    if offset < -caps.read_max_lines:
        fail("BAD_PARAM", f"tail is at most {caps.read_max_lines} lines (offset >= -{caps.read_max_lines})", f"Got {offset}")
    if st.st_size == 0:
        return f"{p} empty (0 bytes)"

    try:
        fh = open(p, "rb")
    except PermissionError:
        fail("DENIED", f"permission denied: {p}", "The server user cannot read it")
    with fh:
        if b"\x00" in fh.read(_SNIFF):
            fail(
                "BINARY",
                f"{p} looks binary ({human_size(st.st_size)})",
                "read is text only; use run with `file`, `xxd | head`, or a format-specific tool",
            )
        fh.seek(0)

        tail = offset < 0
        want = (min(-offset, limit) if limit else -offset) if tail else (limit or caps.read_default_lines)
        window: deque[tuple[int, bytes]] | list[tuple[int, bytes]]
        window = deque(maxlen=want) if tail else []
        total = 0
        for raw in fh:
            total += 1
            if tail:
                window.append((total, raw))
            elif offset < total <= offset + want:
                window.append((total, raw))

    if not tail and offset >= total:
        fail("BAD_PARAM", f"offset {offset} is past the end ({total} lines)", f"Use offset 0 to {total - 1}, or a negative offset to tail")

    shown: list[str] = []
    chars = 0
    last_no = 0
    first_no = window[0][0] if window else 0
    for no, raw in window:
        text = raw.rstrip(b"\n").removesuffix(b"\r").decode("utf-8", "replace")
        if len(text) > caps.read_line_chars:
            text = text[: caps.read_line_chars] + f"...[+{len(text) - caps.read_line_chars} chars]"
        line = f"{no}\t{text}"
        if chars + len(line) + 1 > caps.read_max_chars and shown:
            break
        shown.append(line)
        chars += len(line) + 1
        last_no = no

    header = f"{p} lines {first_no}-{last_no} of {total} ({human_size(st.st_size)})"
    if last_no < total:
        header += f" next offset={last_no}"
    return header + "\n" + "\n".join(shown)


# --------------------------------------------------------------- write ----

def _atomic_write(target: str, data: bytes, mode: int | None) -> None:
    directory = os.path.dirname(target) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".leandc-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode if mode is not None else (0o666 & ~_UMASK))
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _count_lines(text: str) -> int:
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def write_file(cfg: Config, path: str, content: str, mode: str | None = None) -> str:
    caps = cfg.caps
    if mode not in (None, "create", "overwrite", "append"):
        fail("BAD_PARAM", f"mode '{mode}' is not valid", "Use create, overwrite or append")
    if not isinstance(content, str):
        fail("BAD_PARAM", "content must be a string")
    lines = _count_lines(content)
    if lines > caps.write_max_lines:
        fail(
            "TOO_LARGE",
            f"content is {lines} lines; the limit is {caps.write_max_lines} per call",
            "Split it: write the first part with mode=create (or overwrite), then the rest with mode=append",
        )
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        fail("BAD_PARAM", "content is not valid Unicode text")

    p = resolve(cfg, path)
    exists = os.path.lexists(p)
    st = None
    if exists:
        st = _stat(p)
        if stat.S_ISDIR(st.st_mode):
            fail("IS_DIR", f"{p} is a directory", "Pass a file path")
    nonempty = bool(st and st.st_size > 0)
    if nonempty and mode in (None, "create"):
        n = _line_count(p)
        fail(
            "EXISTS",
            f"{p} already exists ({n} lines, {human_size(st.st_size)})",
            "Pass mode=overwrite to replace it or mode=append to add to the end",
        )
    mode = mode or "create"
    target = os.path.realpath(p) if os.path.islink(p) else p

    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        if mode == "append":
            with open(target, "ab") as fh:
                fh.write(data)
            total = _line_count(target)
            h = hashlib.sha256()
            with open(target, "rb") as fh:
                while chunk := fh.read(1 << 20):
                    h.update(chunk)
            digest = h.hexdigest()[:12]
            size = os.path.getsize(target)
            return f"wrote {p} mode=append +{len(data)}B total_lines={total} size={size} sha256={digest}"
        keep = stat.S_IMODE(st.st_mode) if st else None
        _atomic_write(target, data, keep)
    except PermissionError:
        fail("DENIED", f"permission denied writing {p}", "The server user cannot write there")
    except IsADirectoryError:
        fail("IS_DIR", f"{p} is a directory", "Pass a file path")
    except NotADirectoryError:
        fail("BAD_PARAM", f"a parent of {p} is a file, not a directory", "Choose another path")
    return f"wrote {p} mode={mode} {len(data)}B total_lines={lines} sha256={hashlib.sha256(data).hexdigest()[:12]}"


# ---------------------------------------------------------------- edit ----

def _closest(text: str, old: str, diff_chars: int) -> str | None:
    lines = text.split("\n")
    k = old.count("\n") + 1
    if k > len(lines):
        return None
    candidates = ["\n".join(lines[i : i + k]) for i in range(len(lines) - k + 1)]
    best = difflib.get_close_matches(old, candidates, n=1, cutoff=0.6)
    if not best:
        return None
    idx = candidates.index(best[0])
    diff = "\n".join(
        difflib.unified_diff(best[0].split("\n"), old.split("\n"), "file", "old", lineterm="", n=0)
    )
    if len(diff) > diff_chars:
        diff = diff[:diff_chars] + "..."
    return f"Closest match at lines {idx + 1}-{idx + k}:\n{diff}"


def _regions(new_text: str, spans: list[tuple[int, int]], context: int = 3, max_regions: int = 3, max_chars: int = 3000) -> str:
    lines = new_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    ranges: list[list[int]] = []
    for start, end in spans:
        lo, hi = max(start - context, 1), min(end + context, len(lines))
        if ranges and lo <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], hi)
        else:
            ranges.append([lo, hi])
    out: list[str] = []
    used = 0
    shown = 0
    for lo, hi in ranges:
        if shown >= max_regions or used > max_chars:
            break
        chunk = [f"{n}\t{lines[n - 1]}" for n in range(lo, hi + 1)]
        text = "\n".join(chunk)
        out.append(text)
        used += len(text)
        shown += 1
    result = "\n...\n".join(out)
    if shown < len(ranges):
        result += f"\n[+{len(ranges) - shown} more changed regions not shown]"
    return result


def edit_file(cfg: Config, path: str, old: str, new: str, replace_all: bool = False) -> str:
    p = resolve(cfg, path)
    st = _stat(p)
    if stat.S_ISDIR(st.st_mode):
        fail("IS_DIR", f"{p} is a directory", "Pass a file path")
    if st.st_size > _EDIT_MAX_BYTES:
        fail("TOO_LARGE", f"{p} is {human_size(st.st_size)}; edit works up to {human_size(_EDIT_MAX_BYTES)}", "Use run with sed or a script")
    if not isinstance(old, str) or old == "":
        fail("BAD_PARAM", "old must be a non-empty string")
    if not isinstance(new, str):
        fail("BAD_PARAM", "new must be a string")

    try:
        with open(p, "rb") as fh:
            raw = fh.read()
    except PermissionError:
        fail("DENIED", f"permission denied: {p}", "The server user cannot read it")
    if b"\x00" in raw[:_SNIFF]:
        fail("BINARY", f"{p} looks binary", "edit is text only")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        fail("BAD_PARAM", f"{p} is not valid UTF-8", "Use run with a byte-safe tool")

    crlf = b"\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n")
    if crlf:
        text = text.replace("\r\n", "\n")
    old = old.replace("\r\n", "\n")
    new = new.replace("\r\n", "\n")
    if old == new:
        fail("BAD_PARAM", "old and new are identical", "Nothing to change")

    count = text.count(old)
    if count == 0:
        hint = _closest(text, old, cfg.caps.edit_diff_chars)
        fail("NO_MATCH", f"old text not found in {p}", hint or "No similar text found; read the region again")
    if count > 1 and not replace_all:
        positions, start = [], 0
        while len(positions) < 5 and (i := text.find(old, start)) != -1:
            positions.append(text.count("\n", 0, i) + 1)
            start = i + 1
        fail(
            "MULTI_MATCH",
            f"old text occurs {count} times in {p} (first lines: {', '.join(map(str, positions))})",
            "Add surrounding context to make it unique, or pass replace_all=true",
        )

    out: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = 0
    new_len = 0
    replaced = 0
    while True:
        i = text.find(old, pos)
        if i == -1 or (replaced and not replace_all):
            break
        piece = text[pos:i]
        out.append(piece)
        new_len += len(piece)
        out.append(new)
        replaced += 1
        spans.append((new_len, new_len + len(new)))
        new_len += len(new)
        pos = i + len(old)
    out.append(text[pos:])
    new_text = "".join(out)

    line_spans: list[tuple[int, int]] = []
    for a, b in spans:
        first = new_text.count("\n", 0, a) + 1
        last = first + new.count("\n")
        line_spans.append((first, last))

    final = new_text.replace("\n", "\r\n") if crlf else new_text
    target = os.path.realpath(p) if os.path.islink(p) else p
    try:
        _atomic_write(target, final.encode("utf-8"), stat.S_IMODE(st.st_mode))
    except PermissionError:
        fail("DENIED", f"permission denied writing {p}", "The server user cannot write there")

    total = _count_lines(new_text)
    head = f"edited {p}: {replaced} replacement{'s' if replaced != 1 else ''}, file now {total} lines"
    return head + "\n" + _regions(new_text, line_spans)
