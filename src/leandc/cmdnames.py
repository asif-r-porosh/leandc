"""Which commands did a `run` call use? Names only, never arguments (BACKLOG N5).

Deliberately rough: it reads the command position of each pipeline/list segment, skips
quoted strings, heredoc bodies, VAR=value prefixes and redirections, and keeps only
plain-looking names. Anything it cannot read is simply not counted.
"""
from __future__ import annotations

import re

_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1[^\n]*\n(?:.*?\n)?\s*\2[ \t]*(?=\n|$)", re.S)
_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_SPLIT = re.compile(r"\|\||&&|[|;&\n(){}`]|\$\(")
_NAME = re.compile(r"^(?:\[\[?|[A-Za-z_][\w.+-]*)$")
_SKIP_WORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "time", "env", "command", "exec", "nohup", "builtin"}
_SKIP_SEGMENT = {"for", "case", "select", "esac", "function", "in"}
MAX_NAMES = 20


def command_names(cmd: str) -> dict[str, int]:
    text = _HEREDOC.sub(" ", cmd)
    text = _QUOTED.sub("Q", text)
    counts: dict[str, int] = {}
    for seg in _SPLIT.split(text):
        words = seg.split()
        i = 0
        while i < len(words):
            w = words[i]
            if w.rsplit("/", 1)[-1] in _SKIP_WORDS or re.match(r"^[A-Za-z_]\w*=", w) or re.match(r"^\d*[<>]", w):
                i += 1
                if re.match(r"^\d*[<>]+$", w):
                    i += 1  # the redirection target is a separate word: `> file`
                continue
            break
        if i >= len(words) or words[i] in _SKIP_SEGMENT:
            continue
        name = words[i].rsplit("/", 1)[-1]
        if _NAME.match(name) and len(name) <= 32 and (name in counts or len(counts) < MAX_NAMES):
            counts[name] = counts.get(name, 0) + 1
    return counts
