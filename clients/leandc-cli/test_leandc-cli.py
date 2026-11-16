"""Tests for leandc-cli.py. Standard library only.

  python3 test_leandc-cli.py                       fake-server and unit tests only
  LEANDC_CLI_TEST_CONFIG=/path/leandc-cli.json python3 test_leandc-cli.py
                                           also runs the tests against a real leandc (HTTP mode).
                                           That leandc must be a scratch install: the tests write files
                                           under the config's project folder and run shell commands.
"""
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LC = os.path.join(HERE, "..", "..", "src", "leandc", "client.py")   # the client is one file inside the package
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("leandc_cli", LC)
lcmod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lcmod)

REAL = os.environ.get("LEANDC_CLI_TEST_CONFIG")


def run_lc(config, *args, stdin=None, env=None):
    cmd = [sys.executable, "-I", LC, "--config", config] + list(args)
    full_env = dict(os.environ)
    full_env.pop("LEANDC_CLI_URL", None)
    full_env.pop("LEANDC_CLI_FALLBACK_URL", None)
    full_env.pop("LEANDC_CLI_CONFIG", None)
    if env:
        full_env.update(env)
    p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=120, env=full_env)
    return p.returncode, p.stdout, p.stderr


def write_config(directory, **kw):
    path = os.path.join(directory, "cfg-%d.json" % random.randint(0, 10 ** 9))
    with open(path, "w") as fh:
        json.dump(kw, fh)
    return path


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeServer(object):
    """Accepts connections and applies a behaviour: 'hang', 'close' or ('http', status, body)."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.requests = 0
        self.stop = False
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        self.sock.settimeout(0.2)
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(3)
            try:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.requests += 1
                if self.behaviour == "hang":
                    time.sleep(6)
                elif isinstance(self.behaviour, tuple):
                    _, status, body = self.behaviour
                    raw = body.encode()
                    conn.sendall(("HTTP/1.1 %d X\r\nContent-Length: %d\r\nContent-Type: text/plain\r\n\r\n"
                                  % (status, len(raw))).encode() + raw)
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self.stop = True
        self.sock.close()


class UnitTests(unittest.TestCase):
    def test_split_chunks_roundtrip_and_limit(self):
        for n_lines in (0, 1, 699, 700, 701, 1400, 1401, 2500):
            for tail in ("", "x"):
                text = "".join("line %d\n" % i for i in range(n_lines)) + tail
                chunks = lcmod.split_chunks(text, 700)
                self.assertEqual("".join(chunks), text)
                for ch in chunks:
                    # leandc counts lines as newlines plus one for an unfinished last line
                    count = ch.count("\n") + (1 if ch and not ch.endswith("\n") else 0)
                    self.assertLessEqual(count, 800)
                    self.assertLessEqual(count, 700)

    def test_parse_reply_sse_with_comments_and_notifications(self):
        body = (": keepalive\n\nevent: message\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/x\"}\n\n"
                "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":7,\"result\":{\"ok\":true}}\n\n").encode()
        msg = lcmod.parse_reply("text/event-stream; charset=utf-8", body, 7)
        self.assertEqual(msg["result"], {"ok": True})

    def test_parse_reply_json_and_missing(self):
        self.assertEqual(lcmod.parse_reply("application/json", b'{"id":1,"result":{}}', 1)["id"], 1)
        with self.assertRaises(lcmod.LcError):
            lcmod.parse_reply("application/json", b'{"id":2,"result":{}}', 1)

    def test_remote_path(self):
        cfg = lcmod.Config.__new__(lcmod.Config)
        cfg.project = "proj"
        self.assertEqual(lcmod.remote_path(cfg, "a/b.py"), "proj/a/b.py")
        self.assertEqual(lcmod.remote_path(cfg, "./a"), "proj/a")
        self.assertEqual(lcmod.remote_path(cfg, "."), "proj")
        self.assertEqual(lcmod.remote_path(cfg, "/etc/x"), "/etc/x")
        with self.assertRaises(lcmod.LcError):
            lcmod.remote_path(cfg, "../x")

    def test_sh_path(self):
        self.assertEqual(lcmod.sh_path("a b/c"), "'a b/c'")
        self.assertEqual(lcmod.sh_path("~"), '"$HOME"')
        self.assertEqual(lcmod.sh_path("~/my dir/x"), '"$HOME"/\'my dir/x\'')
        self.assertEqual(lcmod.sh_path("/tmp/it's"), "'/tmp/it'\"'\"'s'")

    def test_config_rejects_unknown_key_and_missing_url(self):
        with tempfile.TemporaryDirectory() as d:
            bad = write_config(d, url="http://127.0.0.1:1/x", surprise=1)
            with self.assertRaises(lcmod.LcError):
                lcmod.Config(bad)
            with self.assertRaises(lcmod.LcError):
                lcmod.Config(write_config(d, project="p"))

    def test_default_config_in_user_folder(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as empty:
            folder = os.path.join(home, ".config", "leandc-cli")
            os.makedirs(folder)
            path = os.path.join(folder, "leandc-cli.json")
            with open(path, "w") as fh:
                json.dump({"url": "http://127.0.0.1:1/x"}, fh)
            old = os.getcwd()
            try:
                os.chdir(empty)
                with mock.patch.dict(os.environ, {"HOME": home}):
                    os.environ.pop("LEANDC_CLI_CONFIG", None)
                    self.assertEqual(os.path.realpath(lcmod.find_config(None)), os.path.realpath(path))
                    # a file in the current folder wins over the user folder
                    here = os.path.join(empty, "leandc-cli.json")
                    with open(here, "w") as fh:
                        json.dump({"url": "http://127.0.0.1:2/y"}, fh)
                    self.assertEqual(os.path.realpath(lcmod.find_config(None)), os.path.realpath(here))
            finally:
                os.chdir(old)

    def test_help_names_the_command_it_was_started_as(self):
        from unittest import mock
        with mock.patch.object(sys, "argv", ["/x/bin/leandc-cli"]):
            self.assertEqual(lcmod.command_name(), "leandc-cli")
        with mock.patch.object(sys, "argv", ["/x/leandc-cli.py"]):
            self.assertEqual(lcmod.command_name(), "leandc-cli.py")
        with mock.patch.object(sys, "argv", ["/x/python -m unittest"]):
            self.assertEqual(lcmod.command_name(), "leandc-cli.py")


class PackagingTests(unittest.TestCase):
    """The client is one file inside the package, and the package installs both commands."""

    def test_client_imports_nothing_from_leandc(self):
        import ast
        with open(LC, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name.split(".")[0], "leandc", "client.py must stay a standalone file")
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "client.py must not use relative imports")
                self.assertNotEqual((node.module or "").split(".")[0], "leandc", "client.py must stay a standalone file")

    def test_pyproject_lists_both_commands(self):
        path = os.path.join(HERE, "..", "..", "pyproject.toml")
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('leandc = "leandc.server:main"', text)
        self.assertIn('leandc-cli = "leandc.client:main"', text)


class FakeServerTests(unittest.TestCase):
    SECRET = "S3cretSecretSecretSecret99"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_http_404_is_reported_without_secret(self):
        srv = FakeServer(("http", 404, "Not Found " + self.SECRET))
        self.addCleanup(srv.close)
        cfg = write_config(self.tmp.name, url="http://127.0.0.1:%d/%s/mcp" % (srv.port, self.SECRET))
        code, out, err = run_lc(cfg, "ping")
        self.assertEqual(code, 2)
        self.assertIn("HTTP 404", err)
        self.assertIn("wrong URL or secret", err)
        self.assertNotIn(self.SECRET, out + err)

    def test_timeout_message_and_exit(self):
        srv = FakeServer("hang")
        self.addCleanup(srv.close)
        cfg = write_config(self.tmp.name, url="http://127.0.0.1:%d/%s/mcp" % (srv.port, self.SECRET), timeout=2)
        started = time.time()
        code, out, err = run_lc(cfg, "ping")
        self.assertEqual(code, 2)
        self.assertIn("no reply within 2 s", err)
        self.assertLess(time.time() - started, 5)
        self.assertNotIn(self.SECRET, out + err)

    def test_no_fallback_after_request_was_sent(self):
        primary = FakeServer("close")        # accepts, reads the request, drops the connection
        backup = FakeServer(("http", 200, "{}"))
        self.addCleanup(primary.close)
        self.addCleanup(backup.close)
        cfg = write_config(self.tmp.name,
                           url="http://127.0.0.1:%d/%s/mcp" % (primary.port, self.SECRET),
                           fallback_url="http://127.0.0.1:%d/%s/mcp" % (backup.port, self.SECRET))
        code, out, err = run_lc(cfg, "ping")
        self.assertEqual(code, 2)
        self.assertIn("not retried", err)
        self.assertEqual(backup.requests, 0)
        self.assertNotIn(self.SECRET, out + err)

    def test_fallback_when_primary_cannot_connect(self):
        backup = FakeServer(("http", 404, "nope"))   # reachable; the 404 proves the fallback was tried
        self.addCleanup(backup.close)
        dead = free_port()
        cfg = write_config(self.tmp.name,
                           url="http://127.0.0.1:%d/%s/mcp" % (dead, self.SECRET),
                           fallback_url="http://127.0.0.1:%d/%s/mcp" % (backup.port, self.SECRET))
        code, out, err = run_lc(cfg, "ping")
        self.assertEqual(code, 2)
        self.assertEqual(backup.requests, 1)
        self.assertIn("HTTP 404", err)
        self.assertNotIn(self.SECRET, out + err)

    def test_nothing_listening_and_no_fallback(self):
        cfg = write_config(self.tmp.name, url="http://127.0.0.1:%d/%s/mcp" % (free_port(), self.SECRET))
        code, out, err = run_lc(cfg, "ping")
        self.assertEqual(code, 2)
        self.assertIn("cannot connect", err)
        self.assertNotIn(self.SECRET, out + err)


@unittest.skipUnless(REAL, "set LEANDC_CLI_TEST_CONFIG to test against a real scratch leandc")
class RealServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg_path = REAL
        with open(REAL) as fh:
            cls.cfg = json.load(fh)
        parts = cls.cfg["url"].split("/")
        cls.secret = parts[3]
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = cls.tmp.name
        cls.run_id = "t%d" % random.randint(0, 10 ** 6)
        cls.cfg2 = write_config(cls.root, **dict(cls.cfg, local_root=cls.root))
        cls.local = lambda self, name: os.path.join(cls.root, name)

    @classmethod
    def tearDownClass(cls):
        run_lc(cls.cfg2, "run", "rm -rf %s" % cls.run_id)
        cls.tmp.cleanup()

    def lc(self, *args, **kw):
        code, out, err = run_lc(self.cfg2, *args, **kw)
        self.assertNotIn(self.secret, out + err, "secret leaked in output")
        return code, out, err

    def make(self, name, text):
        path = os.path.join(self.root, self.run_id, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        return path

    # --- the commands, once each --------------------------------------------------
    def test_ping(self):
        code, out, err = self.lc("ping")
        self.assertEqual(code, 0)
        self.assertIn("ok:", out)

    def test_help_lists_tools(self):
        code, out, err = self.lc("help")
        self.assertEqual(code, 0)
        for tool in ("[run]", "[job]", "[read]", "[write]", "[edit]"):
            self.assertIn(tool, out)

    def test_run_and_cwd_and_env(self):
        code, out, _ = self.lc("run", "echo $FOO; pwd", "--env", "FOO=bar")
        self.assertEqual(code, 0)
        self.assertIn("bar", out)
        self.assertIn("cwd=/", out)   # the reply header names the folder the command ran in

    def test_run_stdin_script(self):
        code, out, _ = self.lc("run", "-", stdin="echo one\necho two\n")
        self.assertEqual(code, 0)
        self.assertIn("one", out)
        self.assertIn("two", out)

    def test_job_flow(self):
        code, out, _ = self.lc("run", "sleep 3; echo finished", "--timeout", "1")
        self.assertEqual(code, 0)
        self.assertIn("running job=", out)
        job_id = out.split("running job=")[1].split()[0]
        code, out, _ = self.lc("job", job_id, "--wait", "10")
        self.assertEqual(code, 0)
        self.assertIn("finished", out)
        code, out, _ = self.lc("job")
        self.assertEqual(code, 0)
        self.assertIn(job_id, out)

    def test_write_read_edit(self):
        p = self.run_id + "/w.txt"
        self.assertEqual(self.lc("write", p, "--content", "one\ntwo\nthree\n")[0], 0)
        code, out, _ = self.lc("read", p)
        self.assertIn("2\ttwo", out)
        code, out, _ = self.lc("edit", p, "--old", "two", "--new", "TWO")
        self.assertEqual(code, 0)
        self.assertIn("TWO", out)
        code, out, _ = self.lc("write", p, "--content", "again")       # exists: leandc wants a mode
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("ERROR EXISTS:"))
        self.assertEqual(self.lc("write", p, "--content", "again", "--mode", "overwrite")[0], 0)

    def test_edit_from_files_multiline(self):
        p = self.run_id + "/e.txt"
        self.lc("write", p, "--content", "a\nb\nc\n")
        old = self.make("old.txt", "b\nc\n")
        new = self.make("new.txt", "B\nC\nD\n")
        code, out, _ = self.lc("edit", p, "--old-file", old, "--new-file", new)
        self.assertEqual(code, 0)
        code, out, _ = self.lc("read", p)
        self.assertIn("4\tD", out)

    def test_error_replies_give_exit_1_and_text(self):
        code, out, err = self.lc("read", self.run_id + "/missing.txt")
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("ERROR NOT_FOUND:"))

    def test_wait_is_clamped(self):
        code, out, err = self.lc("run", "echo hi", "--timeout", "500")
        self.assertEqual(code, 0)
        self.assertIn("wait limited to 80 s", err)

    # --- push and pull: exact bytes -------------------------------------------------
    def make_bytes(self, name, data):
        path = os.path.join(self.root, self.run_id, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def roundtrip(self, name, data, extra=()):
        src = self.make_bytes(name, data)
        rel = os.path.relpath(src, self.root)
        code, out, err = self.lc("push", rel, *extra)
        self.assertEqual(code, 0, out + err)
        self.assertIn("[verified]", out)
        back = os.path.join("back", name)
        code, out, err = self.lc("pull", rel, back, *extra)
        self.assertEqual(code, 0, out + err)
        self.assertIn("[verified]", out)
        with open(os.path.join(self.root, back), "rb") as fh:
            self.assertEqual(fh.read(), data)

    def test_roundtrip_line_boundaries(self):
        for n in (1, 299, 300, 301, 699, 700, 701, 799, 800, 801, 999, 1000, 1001, 2500):
            data = "".join("line %d some padding text to make the line longer %s\n" % (i, "x" * 30)
                           for i in range(n)).encode()
            self.roundtrip("b%d.txt" % n, data)

    def test_roundtrip_empty_file(self):
        self.roundtrip("empty.txt", b"")

    def test_roundtrip_unicode(self):
        self.roundtrip("uni.txt", "caf\u00e9 \u4e16\u754c \U0001F600\nsecond\n".encode())

    def test_roundtrip_crlf(self):
        self.roundtrip("crlf.txt", b"a\r\nb\r\nc\r\n")

    def test_roundtrip_no_final_newline(self):
        self.roundtrip("nonl.txt", b"abc\ndef")

    def test_roundtrip_very_long_line(self):
        self.roundtrip("long.txt", b"short\n" + b"y" * 50000 + b"\nend")

    def test_roundtrip_binary_and_repeated_data(self):
        self.roundtrip("rnd.bin", os.urandom(70000))                # incompressible, several pull pieces
        self.roundtrip("zeros.bin", b"\x00" * 300000)               # compresses to almost nothing
        self.roundtrip("rep.txt", b"same line\n" * 20000)           # repeated lines (leandc collapses them in run output)
        self.roundtrip("nul.bin", b"\x00\x01\xff" * 1000)

    def test_roundtrip_1mb_random(self):
        self.roundtrip("mb.bin", os.urandom(1024 * 1024))

    def test_roundtrip_1mb_text(self):
        data = "".join("def f%d(x):\n    return x * %d  # comment\n\n" % (i, i) for i in range(40000)).encode()
        self.assertGreater(len(data), 1024 * 1024)
        self.roundtrip("mb.py", data)

    def test_size_cap_both_ways(self):
        src = self.make_bytes("cap.bin", os.urandom(3000))
        rel = os.path.relpath(src, self.root)
        code, out, err = self.lc("push", rel, "--max-bytes", "1000")
        self.assertEqual(code, 1)
        self.assertIn("over the limit of 1000", out)
        self.assertEqual(self.lc("push", rel)[0], 0)
        code, out, err = self.lc("pull", rel, "back/cap.bin", "--max-bytes", "1000")
        self.assertEqual(code, 1)
        self.assertIn("over the limit of 1000", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "back", "cap.bin")))

    def test_no_temp_files_left_on_remote(self):
        src = self.make_bytes("tidy.txt", b"hello\n")
        rel = os.path.relpath(src, self.root)
        self.assertEqual(self.lc("push", rel)[0], 0)
        self.assertEqual(self.lc("pull", rel, "back/tidy.txt")[0], 0)
        code, out, _ = self.lc("run", "ls -a %s; ls /tmp | grep -c '^leandc-cli\\.' || true" % os.path.dirname(rel))
        self.assertNotIn(".leandc-cli-up", out)
        self.assertNotIn(".leandc-cli-new", out)
        self.assertTrue(out.strip().endswith("0"), out)

    def test_pull_missing_file_and_directory_target(self):
        code, out, err = self.lc("pull", self.run_id + "/nothing.txt")
        self.assertEqual(code, 1)
        self.assertIn("not a file", out)

    def test_overwrite_existing_remote_file(self):
        src = self.make_bytes("over.txt", b"first version\n")
        rel = os.path.relpath(src, self.root)
        self.assertEqual(self.lc("push", rel)[0], 0)
        self.make_bytes("over.txt", b"second\n")
        self.assertEqual(self.lc("push", rel)[0], 0)
        self.assertEqual(self.lc("pull", rel, "back/over.txt")[0], 0)
        with open(os.path.join(self.root, "back", "over.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"second\n")

    def test_push_and_pull_folder(self):
        base = self.run_id + "/tree"
        self.make("tree/a.txt", "A\n")
        self.make("tree/sub/b.txt", "B\n")
        self.make_bytes("tree/sub/c.bin", b"\x00\xff\x10")
        self.make("tree/.git/config", "ignored\n")
        self.make("tree/__pycache__/x.pyc", "ignored\n")
        code, out, err = self.lc("push", base)
        self.assertEqual(code, 0, out + err)
        self.assertNotIn(".git", out)
        self.assertNotIn("__pycache__", out)
        code, out, err = self.lc("pull", base, "treeback")
        self.assertEqual(code, 0, out + err)
        with open(os.path.join(self.root, "treeback", "sub", "b.txt")) as fh:
            self.assertEqual(fh.read(), "B\n")
        with open(os.path.join(self.root, "treeback", "sub", "c.bin"), "rb") as fh:
            self.assertEqual(fh.read(), b"\x00\xff\x10")
        self.assertFalse(os.path.exists(os.path.join(self.root, "treeback", ".git")))

    # --- failures that must not leak the secret -------------------------------------
    def test_wrong_secret(self):
        bad = dict(self.cfg)
        bad["url"] = "/".join(bad["url"].split("/")[:3] + ["W" * len(self.secret), "mcp"])
        path = write_config(self.root, **bad)
        code, out, err = run_lc(path, "ping")
        self.assertEqual(code, 2)
        self.assertIn("HTTP 404", err)
        self.assertNotIn("W" * 10, err)
        self.assertNotIn(self.secret, out + err)

    def test_wrong_host_header(self):
        bad = dict(self.cfg)
        bad["url"] = bad["url"].replace("127.0.0.1", "localhost")
        path = write_config(self.root, **bad)
        code, out, err = run_lc(path, "ping")
        self.assertEqual(code, 2, out + err)
        self.assertIn("HTTP", err)
        self.assertNotIn(self.secret, out + err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
