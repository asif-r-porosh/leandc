"""One-line, accurate errors.

Every failure a tool can anticipate is raised through `fail()`. The SDK hides the
text of any other exception, so a bare exception would reach Claude as an opaque
"Error executing tool X" and cause blind retries.
"""
from __future__ import annotations

from typing import NoReturn

from mcp.server.mcpserver.exceptions import ToolError

CODES = frozenset(
    {
        "NOT_FOUND",
        "DENIED",
        "IS_DIR",
        "EXISTS",
        "NO_MATCH",
        "MULTI_MATCH",
        "TOO_LARGE",
        "BAD_PARAM",
        "BINARY",
        "JOB_LOST",
        "JOB_EXPIRED",
        "JOB_UNKNOWN",
        "INTERNAL",
    }
)


def message(code: str, what: str, hint: str = "") -> str:
    assert code in CODES, code
    what = what.rstrip(". ")
    text = f"ERROR {code}: {what}."
    if hint:
        text += f" {hint.rstrip()}"
        if not text.endswith((".", "?", "!")):
            text += "."
    return text


def fail(code: str, what: str, hint: str = "") -> NoReturn:
    raise ToolError(message(code, what, hint))


def code_of(text: str) -> str | None:
    """Extract the code from an `ERROR <CODE>:` message, for the usage log."""
    if text.startswith("ERROR "):
        head = text[6:].split(":", 1)[0]
        if head in CODES:
            return head
    return None
