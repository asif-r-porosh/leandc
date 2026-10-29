"""Output shaping: strip noise, collapse repeats, then cap by size.

The server shapes by size, not by matching commands: pipes, xargs and
`bash -c` hide the real command. Everything done here is declared in the result.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[@-Z\\-_]"  # two-byte escapes
)
_REPEAT_MIN = 3  # collapse runs of at least this many identical lines


@dataclass
class Shaped:
    text: str
    notes: list[str] = field(default_factory=list)
    omitted_lines: int = 0
    omitted_chars: int = 0

    def note_line(self) -> str:
        return f"[shaped: {', '.join(self.notes)}]" if self.notes else ""


def clean(raw: str) -> tuple[str, list[str]]:
    """Strip ANSI, collapse carriage-return progress, collapse repeated lines."""
    notes: list[str] = []
    text = raw
    if "\x1b" in text:
        stripped = _ANSI.sub("", text)
        if stripped != text:
            notes.append("ansi stripped")
        text = stripped

    text = text.replace("\r\n", "\n")
    if "\r" in text:
        progress = 0
        out_lines = []
        for line in text.split("\n"):
            if "\r" in line:
                parts = [p for p in line.split("\r") if p != ""]
                progress += max(len(parts) - 1, 0)
                line = parts[-1] if parts else ""
            out_lines.append(line)
        if progress:
            notes.append(f"{progress} progress updates collapsed")
        text = "\n".join(out_lines)

    lines = text.split("\n")
    if len(lines) >= _REPEAT_MIN:
        result: list[str] = []
        collapsed = 0
        i = 0
        while i < len(lines):
            j = i
            while j + 1 < len(lines) and lines[j + 1] == lines[i]:
                j += 1
            run = j - i + 1
            if run >= _REPEAT_MIN and lines[i] != "":
                result.append(lines[i])
                result.append(f"[previous line repeated {run - 1} more times]")
                collapsed += run - 1
            else:
                result.extend(lines[i : j + 1])
            i = j + 1
        if collapsed:
            notes.append(f"{collapsed} repeated lines collapsed")
            text = "\n".join(result)
    return text, notes


def cap(text: str, max_chars: int, head_frac: float = 0.25) -> Shaped:
    """Keep about 25% head and 75% tail, cutting on line boundaries when possible."""
    if len(text) <= max_chars:
        return Shaped(text)
    head_budget = int(max_chars * head_frac)
    tail_budget = max_chars - head_budget

    head = text[:head_budget]
    nl = head.rfind("\n")
    if nl > 0:
        head = head[: nl + 1]

    tail = text[len(text) - tail_budget :]
    nl = tail.find("\n")
    if 0 <= nl < len(tail) - 1:
        tail = tail[nl + 1 :]

    middle = text[len(head) : len(text) - len(tail)]
    omitted_lines = middle.count("\n") + (0 if middle.endswith("\n") or not middle else 1)
    omitted_chars = len(middle)
    marker = f"[... {omitted_lines} lines, {omitted_chars} chars omitted ...]"
    sep_a = "" if head.endswith("\n") or not head else "\n"
    sep_b = "" if tail.startswith("\n") else "\n"
    shaped = Shaped(head + sep_a + marker + sep_b + tail, [], omitted_lines, omitted_chars)
    shaped.notes.append(f"omitted {omitted_lines} lines / {omitted_chars} chars from the middle")
    return shaped


def shape_output(raw: str, max_chars: int) -> Shaped:
    text, notes = clean(raw)
    capped = cap(text, max_chars)
    capped.notes = notes + capped.notes
    return capped
