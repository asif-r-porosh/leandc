"""Command-name counts for the usage log (N5): names only, never arguments."""
import pytest

from leandc.cmdnames import command_names


@pytest.mark.parametrize(
    "cmd, expected",
    [
        ("ls -la", {"ls": 1}),
        ("ls | grep foo | wc -l", {"ls": 1, "grep": 1, "wc": 1}),
        ("cd /x && make && ./build.sh; echo done", {"cd": 1, "make": 1, "build.sh": 1, "echo": 1}),
        ("ls; ls; ls", {"ls": 3}),
        ("FOO=bar /usr/bin/env python3 -c 'import os'", {"python3": 1}),
        ('grep -r "TOKEN=abc rm" . > out.txt 2>&1', {"grep": 1}),
        ("cat <<'EOF'\nsecret line\nrm -rf /\nEOF\nls", {"cat": 1, "ls": 1}),
        ("for f in a b c; do echo $f; done", {"echo": 1}),
        ("if [ -f x ]; then cat x; else echo no; fi", {"[": 1, "cat": 1, "echo": 1}),
        ("ssh host 'ls /tmp' && scp a b", {"ssh": 1, "scp": 1}),
        ("echo $(date) `uname`", {"echo": 1, "date": 1, "uname": 1}),
        ("$FOO run", {}),
        ("", {}),
    ],
)
def test_command_names(cmd, expected):
    assert command_names(cmd) == expected


def test_never_returns_arguments_or_odd_names():
    got = command_names("curl -H 'Authorization: Bearer abc123' https://x.test | jq .a")
    assert got == {"curl": 1, "jq": 1}
    assert command_names("x" * 200) == {}  # absurd names are dropped
    many = ";".join(f"cmd{i}" for i in range(50))
    assert len(command_names(many)) <= 20
