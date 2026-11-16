#!/usr/bin/env python3
"""leandc-cli: a small command-line client for a leandc MCP server.

One file, Python standard library only (written for Python 3.8 and newer).
Each call does the MCP handshake over HTTP, calls one leandc tool and prints leandc's text.

Config: leandc-cli.json next to this file (or --config PATH, or $LEANDC_CLI_CONFIG). See leandc-cli.json.example.
Commands: ping, help, run, job, read, write, edit, push, pull. Run `leandc-cli.py help` for details.

Exit codes: 0 ok, 1 leandc reported an error, 2 leandc-cli could not do the call (config, network, usage).
leandc's text goes to stdout. leandc-cli's own messages go to stderr and start with "leandc-cli:".
"""
import argparse
import base64
import gzip
import hashlib
import http.client
import json
import os
import posixpath
import re
import shlex
import socket
import ssl
import sys
import zlib
from urllib.parse import urlsplit

VERSION = "0.1.0"
PROTOCOL = "2025-06-18"
WRITE_CHUNK_LINES = 700    # leandc accepts at most 800 lines per write call
MAX_BYTES_DEFAULT = 5 * 1024 * 1024   # largest file push or pull will move (--max-bytes changes it)
PULL_CHUNK = 20480         # raw bytes per pull call; its base64 is 27308 chars, under leandc's 30000 cap
PUSH_LINE = 76             # base64 line length when uploading
UPLOAD_SUFFIX = ".leandc-cli-up"   # temp file next to the destination while an upload is in flight
NEW_SUFFIX = ".leandc-cli-new"
LIST_MAX_CHARS = 30000     # largest run output leandc allows
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".idea"}
SKIP_FILES = {".DS_Store"}
CONFIG_KEYS = {"url", "fallback_url", "project", "local_root", "timeout"}
ERROR_RE = re.compile(r"^ERROR [A-Z_]+:")


class LcError(Exception):
    def __init__(self, message, code=2):
        Exception.__init__(self, message)
        self.code = code


# ---------------------------------------------------------------- config ----

def find_config(explicit):
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (explicit, os.environ.get("LEANDC_CLI_CONFIG"), os.path.join(here, "leandc-cli.json"), "leandc-cli.json"):
        if path and os.path.isfile(path):
            return path
    return None


class Config(object):
    def __init__(self, path):
        data = {}
        if path:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError) as exc:
                raise LcError("cannot read config %s: %s" % (path, exc))
            if not isinstance(data, dict):
                raise LcError("config %s must be a JSON object" % path)
            unknown = sorted(k for k in data if k not in CONFIG_KEYS and not k.startswith("_"))
            if unknown:
                raise LcError("config %s has unknown key(s): %s (allowed: %s)"
                              % (path, ", ".join(unknown), ", ".join(sorted(CONFIG_KEYS))))
        self.url = os.environ.get("LEANDC_CLI_URL") or data.get("url") or ""
        self.fallback_url = os.environ.get("LEANDC_CLI_FALLBACK_URL") or data.get("fallback_url") or ""
        self.project = str(data.get("project") or "").strip("/")
        self.local_root = os.path.abspath(os.path.expanduser(str(data.get("local_root") or ".")))
        try:
            self.timeout = int(data.get("timeout") or 90)
        except (TypeError, ValueError):
            raise LcError("config timeout must be a whole number of seconds")
        if not self.url:
            raise LcError("no url: create leandc-cli.json (see leandc-cli.json.example) or set LEANDC_CLI_URL")
        # Longest wait we ask leandc for in one call; keeps replies inside the socket timeout and
        # below the ~100 s limit of a Cloudflare proxy.
        self.max_wait = max(1, min(120, self.timeout - 10))
        self._secrets = []
        for url in (self.url, self.fallback_url):
            if url:
                for seg in urlsplit(url).path.split("/"):
                    if len(seg) >= 8 and seg != "mcp":
                        self._secrets.append(seg)

    def redact(self, text):
        text = str(text)
        for secret in self._secrets:
            text = text.replace(secret, "<secret>")
        return text


# ------------------------------------------------------------- transport ----

class Endpoint(object):
    def __init__(self, url):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise LcError("url must start with http:// or https:// and contain a host")
        self.tls = parts.scheme == "https"
        self.host = parts.hostname
        self.port = parts.port or (443 if self.tls else 80)
        self.path = (parts.path or "/") + ("?" + parts.query if parts.query else "")

    def connect(self, timeout):
        if self.tls:
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=timeout,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        conn.connect()
        return conn

    def label(self):
        return "%s:%s" % (self.host, self.port)


def parse_reply(content_type, raw, want_id):
    """Return the JSON-RPC message with id want_id from a JSON or SSE (text/event-stream) body."""
    text = raw.decode("utf-8", "replace")
    messages = []
    if "text/event-stream" in (content_type or ""):
        data = []
        for line in text.splitlines() + [""]:
            if line == "":
                if data:
                    try:
                        messages.append(json.loads("\n".join(data)))
                    except ValueError:
                        pass
                    data = []
            elif line.startswith("data:"):
                chunk = line[5:]
                data.append(chunk[1:] if chunk.startswith(" ") else chunk)
    elif text.strip():
        try:
            messages.append(json.loads(text))
        except ValueError:
            raise LcError("server sent a reply that is not JSON")
    for msg in messages:
        if isinstance(msg, dict) and msg.get("id") == want_id:
            return msg
    raise LcError("server reply did not contain an answer to the request")


class Client(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.endpoints = [Endpoint(cfg.url)]
        if cfg.fallback_url:
            self.endpoints.append(Endpoint(cfg.fallback_url))
        self.ep = None
        self.sid = None
        self.next_id = 0
        self.server_info = {}

    def _connect(self):
        if self.ep is not None:            # an endpoint already worked: stay on it, no fallback
            try:
                return self.ep.connect(self.cfg.timeout)
            except (OSError, ssl.SSLError) as exc:
                raise LcError("cannot connect to %s: %s" % (self.ep.label(), self.cfg.redact(exc)))
        last = None
        for ep in self.endpoints:          # first call only: fall back when connecting fails
            try:
                conn = ep.connect(self.cfg.timeout)
                self.ep = ep
                return conn
            except (OSError, ssl.SSLError) as exc:
                last = "%s: %s" % (ep.label(), self.cfg.redact(exc))
        raise LcError("cannot connect (%s)" % last)

    def _post(self, payload):
        conn = self._connect()
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
            headers["MCP-Protocol-Version"] = PROTOCOL
        try:
            conn.request("POST", self.ep.path, body=json.dumps(payload).encode("utf-8"), headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except socket.timeout:
            raise LcError("no reply within %d s. The command may still be running; check with `job`."
                          % self.cfg.timeout)
        except (OSError, http.client.HTTPException) as exc:
            raise LcError("request failed after connecting (not retried; the call may have run): %s"
                          % self.cfg.redact(exc))
        finally:
            conn.close()
        if resp.status >= 400:
            snippet = self.cfg.redact(raw.decode("utf-8", "replace").strip().replace("\n", " "))[:200]
            hint = " (wrong URL or secret?)" if resp.status == 404 else ""
            raise LcError("server answered HTTP %d%s %s" % (resp.status, hint, snippet))
        return resp.getheader("Mcp-Session-Id"), resp.getheader("Content-Type"), raw

    def rpc(self, method, params):
        self.next_id += 1
        rid = self.next_id
        sid, ctype, raw = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if sid and not self.sid:
            self.sid = sid
        msg = parse_reply(ctype, raw, rid)
        if "error" in msg:
            err = msg["error"] or {}
            raise LcError("server error %s: %s" % (err.get("code"), self.cfg.redact(err.get("message"))))
        return msg.get("result") or {}

    def notify(self, method):
        self._post({"jsonrpc": "2.0", "method": method, "params": {}})

    def handshake(self):
        result = self.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                         "clientInfo": {"name": "leandc-cli", "version": VERSION}})
        self.server_info = result.get("serverInfo") or {}
        self.notify("notifications/initialized")

    def call(self, tool, arguments):
        """Return (text, is_error) for one tool call."""
        result = self.rpc("tools/call", {"name": tool, "arguments": arguments})
        text = "\n".join(b.get("text", "") for b in (result.get("content") or [])
                         if isinstance(b, dict) and b.get("type") == "text")
        return text, bool(result.get("isError")) or bool(ERROR_RE.match(text))

    def list_tools(self):
        return self.rpc("tools/list", {}).get("tools") or []


# ------------------------------------------------------------------ paths ----

def remote_path(cfg, path):
    """Path as leandc should see it: absolute and ~ paths pass through; others go under the optional project subfolder."""
    if path.startswith("/") or path.startswith("~"):
        return path
    norm = posixpath.normpath(path)
    if norm == ".." or norm.startswith("../"):
        raise LcError("path leaves the project: %s" % path)
    if norm == ".":
        return cfg.project or "."
    return posixpath.join(cfg.project, norm) if cfg.project else norm


def local_path(cfg, path):
    path = os.path.expanduser(path)
    return path if os.path.isabs(path) else os.path.join(cfg.local_root, path)


def emit(text, is_error):
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 1 if is_error else 0


def split_chunks(text, n):
    """Cut text into pieces of at most n lines each; joining the pieces gives back the exact text."""
    if text == "":
        return [""]
    pieces = text.split("\n")
    lines = [p + "\n" for p in pieces[:-1]] + ([pieces[-1]] if pieces[-1] else [])
    return ["".join(lines[i:i + n]) for i in range(0, len(lines), n)]


def read_local_text(path):
    """Text of a local file, or None when it is not UTF-8 text."""
    with open(path, "rb") as fh:
        data = fh.read()
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# -------------------------------------------------------------- commands ----

def cmd_ping(c, a):
    name = c.server_info.get("name") or "server"
    ver = c.server_info.get("version")
    print("ok: %s%s at %s" % (name, " " + ver if ver else "", c.ep.label()))
    return 0


def cmd_help(c, a):
    print(USAGE.strip("\n"))
    print("\nTools on this server:")
    for tool in c.list_tools():
        schema = tool.get("inputSchema") or {}
        required = set(schema.get("required") or [])
        params = []
        for name, spec in (schema.get("properties") or {}).items():
            params.append("%s%s" % (name, "*" if name in required else ""))
        print("\n[%s] args: %s" % (tool.get("name"), ", ".join(params)))
        print(tool.get("description", ""))
    print("\n(* = required)")
    return 0


def cmd_run(c, a):
    cmd = sys.stdin.read() if a.cmd == ["-"] else " ".join(a.cmd)
    args = {"cmd": cmd}
    cwd = remote_path(c.cfg, a.cwd) if a.cwd else (c.cfg.project or None)
    if cwd:
        args["cwd"] = cwd
    if a.timeout is not None:
        args["timeout"] = clamp_wait(c, a.timeout)
    if a.max_chars is not None:
        args["max_chars"] = a.max_chars
    if a.env:
        env = {}
        for item in a.env:
            if "=" not in item:
                raise LcError("--env needs NAME=VALUE, got %r" % item)
            key, value = item.split("=", 1)
            env[key] = value
        args["env"] = env
    return emit(*c.call("run", args))


def clamp_wait(c, seconds):
    if seconds > c.cfg.max_wait:
        sys.stderr.write("leandc-cli: wait limited to %d s (config timeout %d s)\n" % (c.cfg.max_wait, c.cfg.timeout))
        return c.cfg.max_wait
    return seconds


def cmd_job(c, a):
    args = {}
    for key in ("id", "action", "offset", "limit", "tail", "grep"):
        value = getattr(a, key)
        if value is not None:
            args[key] = value
    if a.wait is not None:
        args["wait"] = clamp_wait(c, a.wait)
    return emit(*c.call("job", args))


def cmd_read(c, a):
    args = {"path": remote_path(c.cfg, a.path)}
    if a.offset is not None:
        args["offset"] = a.offset
    if a.limit is not None:
        args["limit"] = a.limit
    return emit(*c.call("read", args))


def cmd_write(c, a):
    if a.content is not None:
        text = a.content
    elif a.file:
        text = read_local_text(local_path(c.cfg, a.file))
        if text is None:
            raise LcError("%s is not UTF-8 text" % a.file)
    else:
        text = sys.stdin.read()
    path = remote_path(c.cfg, a.path)
    result = None
    for i, chunk in enumerate(split_chunks(text, WRITE_CHUNK_LINES)):
        args = {"path": path, "content": chunk}
        mode = a.mode if i == 0 else "append"
        if mode:
            args["mode"] = mode
        result = c.call("write", args)
        if result[1]:
            return emit(*result)
    return emit(*result)


def text_arg(value, file_arg, cfg, what):
    if file_arg:
        text = read_local_text(local_path(cfg, file_arg))
        if text is None:
            raise LcError("%s is not UTF-8 text" % file_arg)
        return text
    return value


def cmd_edit(c, a):
    old = text_arg(a.old, a.old_file, c.cfg, "old")
    new = text_arg(a.new, a.new_file, c.cfg, "new")
    if old is None or new is None:
        raise LcError("edit needs --old (or --old-file) and --new (or --new-file)")
    args = {"path": remote_path(c.cfg, a.path), "old": old, "new": new}
    if a.all:
        args["replace_all"] = True
    return emit(*c.call("edit", args))


def sh_path(path):
    """A remote path as a shell word. ~ and ~/x keep working."""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def run_step(c, script, what):
    """Run a helper script on the remote. Returns its output lines; raises LcError(code 1) when it fails."""
    out, is_err = c.call("run", {"cmd": script, "timeout": min(30, c.cfg.max_wait), "max_chars": LIST_MAX_CHARS})
    if is_err:
        raise LcError("%s: %s" % (what, out.strip()), 1)
    head, _, body = out.partition("\n")
    if head.startswith("running job="):
        raise LcError("%s: the remote step is still running (%s)" % (what, head.strip()), 1)
    if not head.startswith("exit=0"):
        raise LcError("%s: %s" % (what, (body or head).strip()), 1)
    return [ln for ln in body.split("\n") if ln.strip()]


SHA_SH = '(sha256sum "$p" 2>/dev/null || shasum -a 256 "$p") | cut -d" " -f1'


def push_file(c, local, remote, max_bytes):
    """Send one local file as gzip + base64 text, decode it on the remote. Returns (ok, message)."""
    try:
        size = os.path.getsize(local)
        if size > max_bytes:
            return False, "skipped %s (%d bytes, over the limit of %d; raise --max-bytes)" % (local, size, max_bytes)
        with open(local, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return False, "failed %s: %s" % (local, exc)
    want = hashlib.sha256(data).hexdigest()
    b64 = base64.b64encode(gzip.compress(data, 6)).decode("ascii")
    text = "\n".join(b64[i:i + PUSH_LINE] for i in range(0, len(b64), PUSH_LINE)) + "\n"
    part = remote + UPLOAD_SUFFIX
    try:
        for i, chunk in enumerate(split_chunks(text, WRITE_CHUNK_LINES)):
            out, is_err = c.call("write", {"path": part, "content": chunk, "mode": "overwrite" if i == 0 else "append"})
            if is_err:
                return False, "failed %s: %s" % (remote, out.strip())
        script = (
            'p=%s; u="$p%s"; n="$p%s"; '
            'dec() { base64 -d "$1" 2>/dev/null || base64 -D -i "$1"; }; '
            'if dec "$u" | gunzip -c > "$n"; then mv -f "$n" "$p"; '
            'else rm -f "$n" "$u"; echo "decode failed on the remote" >&2; exit 3; fi; '
            'rm -f "$u"; s=$( %s ); echo "LCSHA $s"'
        ) % (sh_path(remote), UPLOAD_SUFFIX, NEW_SUFFIX, SHA_SH)
        lines = run_step(c, script, "failed %s" % remote)
    except LcError as exc:
        return False, c.cfg.redact(exc)
    got = next((ln.split(" ", 1)[1].strip() for ln in lines if ln.startswith("LCSHA ")), "")
    if got == want:
        return True, "pushed %s (%d bytes) [verified]" % (remote, len(data))
    if not re.match(r"^[0-9a-f]{64}$", got):
        return True, "pushed %s (%d bytes) [could not verify: no sha tool on the remote]" % (remote, len(data))
    return False, "pushed %s but the remote copy differs (sha256 mismatch)" % remote


def cmd_push(c, a):
    local = local_path(c.cfg, a.local)
    if not os.path.exists(local):
        raise LcError("no such local path: %s" % a.local)
    rel = os.path.relpath(local, c.cfg.local_root)
    if a.remote:
        base = remote_path(c.cfg, a.remote)
    elif rel.startswith(".."):
        raise LcError("%s is outside local_root; pass a REMOTE path" % a.local)
    else:
        base = remote_path(c.cfg, "." if rel == "." else rel)
    failures = 0
    count = 0
    if os.path.isdir(local):
        for dirpath, dirnames, filenames in os.walk(local):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                if name in SKIP_FILES or name.endswith(".pyc"):
                    continue
                src = os.path.join(dirpath, name)
                dest = posixpath.join(base, *os.path.relpath(src, local).split(os.sep))
                ok, msg = push_file(c, src, dest, a.max_bytes)
                print(msg)
                count += 1
                failures += 0 if ok else 1
    else:
        ok, msg = push_file(c, local, base, a.max_bytes)
        print(msg)
        count, failures = 1, (0 if ok else 1)
    print("push done: %d file(s), %d problem(s)" % (count, failures))
    return 1 if failures else 0


def list_remote_files(c, remote):
    """Return (files, error). files is None when remote is a plain file."""
    cmd = ("p=%s; if [ -d \"$p\" ]; then cd \"$p\" && find . -type f -not -path '*/.git/*' "
           "-not -path '*/node_modules/*' -not -path '*/__pycache__/*' | sort; else echo '::FILE::'; fi"
           % sh_path(remote))
    out, is_err = c.call("run", {"cmd": cmd, "max_chars": LIST_MAX_CHARS, "timeout": min(30, c.cfg.max_wait)})
    if is_err or not out.startswith("exit=0"):
        return None, out
    body = [ln for ln in out.split("\n")[1:] if ln and not ln.startswith("[shaped:")]
    if any(ln.startswith("[...") and "omitted" in ln for ln in body):
        return None, "too many files to list; pull a subfolder"
    if body == ["::FILE::"]:
        return None, None
    return [ln[2:] if ln.startswith("./") else ln for ln in body], None


def pull_file(c, remote, dest, max_bytes):
    """Fetch one remote file exactly: gzip it on the remote, read the .gz as base64 pieces, unzip here."""
    script = (
        'p=%s; '
        'if [ ! -f "$p" ]; then echo "not a file: $p" >&2; exit 3; fi; '
        'n=$(wc -c < "$p" | tr -d " "); '
        'if [ "$n" -gt %d ]; then echo "file is $n bytes, over the limit of %d (raise --max-bytes)" >&2; exit 4; fi; '
        't=$(mktemp "${TMPDIR:-/tmp}/leandc-cli.XXXXXXXX") || exit 5; '
        'if ! gzip -c "$p" > "$t"; then rm -f "$t"; echo "gzip failed on the remote" >&2; exit 6; fi; '
        's=$( %s ); echo "LCPULL $t $(wc -c < "$t" | tr -d " ") $n $s"'
    ) % (sh_path(remote), max_bytes, max_bytes, SHA_SH)
    tmp = None
    try:
        lines = run_step(c, script, "failed %s" % remote)
        info = next((ln.split(" ") for ln in lines if ln.startswith("LCPULL ")), None)
        if not info or len(info) < 5:
            return False, "failed %s: unexpected reply from the remote" % remote
        sha, raw_size, gz_size = info[-1], int(info[-2]), int(info[-3])
        tmp = " ".join(info[1:-3])
        parts, got, i = [], 0, 0
        while got < gz_size:
            cmd = 'dd if=%s bs=%d skip=%d count=1 2>/dev/null | base64 | tr -d "\\n"' % (shlex.quote(tmp), PULL_CHUNK, i)
            piece = run_step(c, cmd, "failed %s (piece %d)" % (remote, i))
            if len(piece) != 1:
                return False, "failed %s: output was cut or reshaped by the server (piece %d)" % (remote, i)
            try:
                data = base64.b64decode(piece[0].strip(), validate=True)
            except ValueError:
                return False, "failed %s: piece %d is not valid base64" % (remote, i)
            if len(data) != min(PULL_CHUNK, gz_size - got):
                return False, "failed %s: piece %d has the wrong length" % (remote, i)
            parts.append(data)
            got += len(data)
            i += 1
        try:
            raw = gzip.decompress(b"".join(parts))
        except (OSError, EOFError, zlib.error) as exc:
            return False, "failed %s: cannot unzip what arrived (%s)" % (remote, exc)
    except LcError as exc:
        return False, c.cfg.redact(exc)
    finally:
        if tmp:
            try:
                c.call("run", {"cmd": "rm -f %s" % shlex.quote(tmp), "timeout": 10})
            except LcError:
                pass
    if len(raw) != raw_size:
        return False, "failed %s: got %d bytes, expected %d" % (remote, len(raw), raw_size)
    note = " [verified]"
    if re.match(r"^[0-9a-f]{64}$", sha):
        if hashlib.sha256(raw).hexdigest() != sha:
            return False, "failed %s: sha256 mismatch after transfer" % remote
    else:
        note = " [could not verify: no sha tool on the remote]"
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(dest + ".leandc-cli-part", "wb") as fh:
        fh.write(raw)
    os.replace(dest + ".leandc-cli-part", dest)
    return True, "pulled %s (%d bytes)%s" % (dest, len(raw), note)


def cmd_pull(c, a):
    remote = remote_path(c.cfg, a.remote)
    files, err = list_remote_files(c, remote)
    if err:
        return emit(err, True)
    if a.local:
        dest_base = local_path(c.cfg, a.local)
    elif a.remote.startswith("/") or a.remote.startswith("~"):
        dest_base = os.path.join(c.cfg.local_root, posixpath.basename(a.remote.rstrip("/")))
    else:
        dest_base = os.path.join(c.cfg.local_root, *posixpath.normpath(a.remote).split("/"))
    failures = 0
    if files is None:
        ok, msg = pull_file(c, remote, dest_base, a.max_bytes)
        print(msg)
        count, failures = 1, (0 if ok else 1)
    else:
        count = 0
        for rel in files:
            ok, msg = pull_file(c, posixpath.join(remote, rel), os.path.join(dest_base, *rel.split("/")), a.max_bytes)
            print(msg)
            count += 1
            failures += 0 if ok else 1
    print("pull done: %d file(s), %d problem(s)" % (count, failures))
    return 1 if failures else 0


# ------------------------------------------------------------------- main ----

USAGE = """
leandc-cli: client for a leandc server. Quote the whole command or text.

  leandc-cli.py ping                              check the connection
  leandc-cli.py help                              this text and the server's tool list
  leandc-cli.py run "ls -la" [--cwd D] [--timeout S] [--max-chars N] [--env K=V]
        a script can come from stdin:  leandc-cli.py run -
  leandc-cli.py job [ID] [--action output|status|kill] [--tail N] [--grep RX] [--wait S]
  leandc-cli.py read PATH [--offset N] [--limit N]
  leandc-cli.py write PATH [--mode create|overwrite|append] [--content TEXT | --file LOCAL]   (else stdin)
  leandc-cli.py edit PATH --old TEXT --new TEXT [--all]      (or --old-file / --new-file)
  leandc-cli.py push LOCAL [REMOTE] [--max-bytes N]   send a file or folder, byte for byte (gzip + base64)
  leandc-cli.py pull REMOTE [LOCAL] [--max-bytes N]   fetch a file or folder, byte for byte; checked by sha256

Relative remote paths sit under leandc's default folder (an optional "project" in leandc-cli.json adds a subfolder). Absolute and ~ paths are used as given.
"""


def build_parser():
    p = argparse.ArgumentParser(prog="leandc-cli.py", description="Client for a leandc MCP server.", add_help=True)
    p.add_argument("--config", help="path to leandc-cli.json (default: $LEANDC_CLI_CONFIG, leandc-cli.json next to this script, ./leandc-cli.json)")
    p.add_argument("--version", action="version", version="leandc-cli " + VERSION)
    sub = p.add_subparsers(dest="command", metavar="command")
    sub.required = True

    sub.add_parser("ping", help="check the connection")
    sub.add_parser("help", help="show commands and the server's tools")

    s = sub.add_parser("run", help="run a shell command")
    s.add_argument("cmd", nargs="+", help="the command (quote it); '-' reads a script from stdin")
    s.add_argument("--cwd")
    s.add_argument("--timeout", type=int, help="seconds to wait before leandc hands back a job id")
    s.add_argument("--max-chars", type=int, dest="max_chars")
    s.add_argument("--env", action="append", metavar="NAME=VALUE")

    s = sub.add_parser("job", help="list, read, wait for or kill a job")
    s.add_argument("id", nargs="?")
    s.add_argument("--action", choices=["output", "status", "kill"])
    s.add_argument("--offset", type=int)
    s.add_argument("--limit", type=int)
    s.add_argument("--tail", type=int)
    s.add_argument("--grep")
    s.add_argument("--wait", type=int, help="seconds to wait for the job to finish")

    s = sub.add_parser("read", help="read a text file with line numbers")
    s.add_argument("path")
    s.add_argument("--offset", type=int)
    s.add_argument("--limit", type=int)

    s = sub.add_parser("write", help="write a text file (content from --content, --file or stdin)")
    s.add_argument("path")
    s.add_argument("--mode", choices=["create", "overwrite", "append"])
    s.add_argument("--content")
    s.add_argument("--file", help="local file to send")

    s = sub.add_parser("edit", help="replace exact text in a file")
    s.add_argument("path")
    s.add_argument("--old")
    s.add_argument("--new")
    s.add_argument("--old-file", dest="old_file")
    s.add_argument("--new-file", dest="new_file")
    s.add_argument("--all", action="store_true", help="replace every occurrence")

    s = sub.add_parser("push", help="send a local file or folder")
    s.add_argument("local")
    s.add_argument("remote", nargs="?")
    s.add_argument("--max-bytes", type=int, dest="max_bytes", default=MAX_BYTES_DEFAULT, help="largest file to move (default 5 MiB)")

    s = sub.add_parser("pull", help="fetch a remote file or folder")
    s.add_argument("remote")
    s.add_argument("local", nargs="?")
    s.add_argument("--max-bytes", type=int, dest="max_bytes", default=MAX_BYTES_DEFAULT, help="largest file to move (default 5 MiB)")
    return p


COMMANDS = {"ping": cmd_ping, "help": cmd_help, "run": cmd_run, "job": cmd_job, "read": cmd_read,
            "write": cmd_write, "edit": cmd_edit, "push": cmd_push, "pull": cmd_pull}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    cfg = None
    try:
        cfg = Config(find_config(args.config))
        client = Client(cfg)
        client.handshake()
        return COMMANDS[args.command](client, args)
    except LcError as exc:
        msg = cfg.redact(exc) if cfg else str(exc)
        sys.stderr.write("leandc-cli: %s\n" % msg)
        return exc.code
    except KeyboardInterrupt:
        sys.stderr.write("leandc-cli: interrupted\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
