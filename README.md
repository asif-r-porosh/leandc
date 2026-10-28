# leandc

Lean desktop commander: an MCP server that gives Claude five tools (`run`, `job`, `read`, `write`, `edit`) on a machine you control. One codebase, two targets:

- **vm**: disposable Ubuntu x86_64 box, HTTPS behind Caddy, added to claude.ai as a custom connector.
- **mac**: stdio only (the Claude desktop app launches it); nothing listens on the network.

Why and how it was designed: `leandc-spec.md` (decision log D1-D30, evidence from the Desktop Commander history, stub results, build results). Read section 2 and 12b first.

## Tools

| tool | does |
|---|---|
| `run(cmd, cwd?, env?, timeout?, max_chars?)` | bash, non-login, no rc files, stdin closed. Waits up to `timeout` (30 s, max 120), then returns a job id and keeps running. Output cleaned (ANSI, progress bars, repeated lines) and capped (8000 chars, max 30000, head 25% / tail 75%). |
| `job(id?, action?, offset?, limit?, tail?, grep?, wait?)` | list, read, wait for, or kill a job. 1 MB rolling buffer, 24 h expiry, lost on restart (and says so). |
| `read(path, offset?, limit?)` | numbered lines, 300 default / 1000 max, `next offset` in the header, negative offset tails. |
| `write(path, content, mode?)` | create / overwrite / append; `mode` required if the file exists and is not empty; 800 lines max per call; atomic. |
| `edit(path, old, new, replace_all?)` | exact unique match; returns the changed region with line numbers; on a miss shows the closest match. |

Unknown parameters are rejected, every error is one line (`ERROR <CODE>: what. next step.`), and the usage log holds metadata only (never commands, paths or content).

## Install

Needs Homebrew (https://brew.sh; on Ubuntu first `sudo apt install build-essential procps curl file git`).

```
git clone <your repo> leandc && cd leandc
./bootstrap.sh                      # brew bundle, uv python 3.12, builds the bin dir of GNU/modern symlinks
uv sync --python 3.12
cp config.example.toml ~/.config/leandc/config.toml    # then edit; chmod 600
uv run leandc check --config ~/.config/leandc/config.toml
```

`leandc check` must end with `self-check passed` (warnings are fine). Any FAIL line says what to fix. `serve` runs the same check and refuses to start on a FAIL.

### Mac (stdio)

Config: `target = "mac"`, `bin_dir` from bootstrap. In the Claude desktop app's MCP config add a server whose command is the absolute path of `.venv/bin/leandc` with args `serve --config /abs/path/config.toml`. Nothing else is needed; it never listens.

### VM (HTTPS)

1. DNS: a **one-level** hostname (for example `leandc-yourname.example.com`), **DNS only / grey cloud** (the orange proxy cuts requests at about 100 s). Check the VM's own public IP on the VM before creating the record.
2. Caddy: `deploy/Caddyfile.snippet`. Ports 80 and 443 open.
3. Config: `target = "vm"`, `[http] allowed_hosts = ["<that hostname>"]`, `path_secret` from `leandc secret`.
4. systemd: `deploy/leandc.service` (edit user and paths), `systemctl enable --now leandc`; logs with `journalctl -u leandc`.
5. claude.ai: Settings > Connectors > Add custom connector, URL `https://<hostname>/<secret>/mcp`, leave OAuth empty.

Security of v0.1: a secret URL path plus an allowed-hosts check. Anyone who learns the URL gets a shell on that VM, so keep it disposable with no production secrets. OAuth is the next gate (spec G4). The secret also appears in Caddy's access log.

## Develop

```
uv run pytest            # 116 tests, about 35 s
```

Tests need bash, GNU sed/grep/find, `rg`, `pgrep`. The SDK is pinned (`mcp==2.3.0`); the middleware API it relies on is provisional, so re-run the suite after any SDK bump.

## Suggested commits (one per step, in order)

1. `core: config, errors, output shaping, usage log` : `pyproject.toml`, `uv.lock`, `src/leandc/{__init__,config,errors,shaping,usage_log}.py`, `tests/test_core.py`, `.gitignore`
2. `files: read, write, edit tools` : `src/leandc/files.py`, `tests/test_files.py`
3. `process: run and job tools with server-owned job manager` : `src/leandc/{jobs,procs}.py`, `tests/test_procs.py`
4. `server: MCP wiring, middleware, self-check, stdio and HTTP entry points` : `src/leandc/{server,selfcheck}.py`, `tests/{test_server,test_transports}.py`
5. `packaging: Brewfile, bootstrap, example config, deploy files` : `Brewfile`, `bootstrap.sh`, `config.example.toml`, `deploy/`, `tests/test_packaging.py`
6. `docs: README and spec v0.6` : `README.md`, `leandc-spec.md`
