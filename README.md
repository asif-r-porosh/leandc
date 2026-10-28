# leandc

Lean desktop commander: an MCP server that gives Claude five tools (`run`, `job`, `read`, `write`, `edit`) on a machine you control. One codebase, two targets:

- **vm**: disposable Ubuntu x86_64 box, HTTPS behind Caddy, added to claude.ai as a custom connector.
- **mac**: stdio only (the Claude desktop app launches it); nothing listens on the network.

Plan and open work: `BACKLOG.md`. `leandc-spec.md` is the history of how the design was reached; where they disagree, BACKLOG wins.

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

leandc uses only what the OS ships (`ls`, `sed`, `grep`, ...). It installs nothing; Claude is told the OS and userland flavor (macOS BSD or Ubuntu GNU) in the `run` description and writes commands to match.

Prerequisites: **Python 3.12 or newer** with `venv` and `pip` (Ubuntu 24.04: `sudo apt install python3 python3-venv`; macOS: any python.org, pyenv or Homebrew 3.12+). `bash` is the only command leandc insists on.

```
git clone <your repo> leandc && cd leandc
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -e .
mkdir -p ~/.config/leandc && cp config.example.toml ~/.config/leandc/config.toml    # edit; chmod 600
.venv/bin/leandc check --config ~/.config/leandc/config.toml
```

`leandc check` must end with `self-check passed`. `serve` runs the same check and refuses to start on a FAIL. (Where the checkout and venv should live is still open: BACKLOG N6.)

### Mac (stdio)

Config: `target = "mac"`. In the Claude desktop app's MCP config add a server whose command is the absolute path of `.venv/bin/leandc` with args `serve --config /abs/path/config.toml`. Nothing else is needed; it never listens.

### VM (HTTPS)

1. DNS: a **one-level** hostname (for example `leandc-yourname.example.com`), **DNS only / grey cloud** (the orange proxy cuts requests at about 100 s). Check the VM's own public IP on the VM before creating the record.
2. Caddy: `deploy/Caddyfile.snippet`. Ports 80 and 443 open.
3. Config: `target = "vm"`, `[http] allowed_hosts = ["<that hostname>"]`, `path_secret` from `leandc secret`.
4. systemd: `deploy/leandc.service` (edit user and paths), `systemctl enable --now leandc`; logs with `journalctl -u leandc`.
5. claude.ai: Settings > Connectors > Add custom connector, URL `https://<hostname>/<secret>/mcp`, leave OAuth empty.

Security: one long secret in the URL path plus an allowed-hosts check. Anyone who learns the URL gets a shell on that VM, so keep it disposable with no production secrets. Caddy only logs the path if you add a `log` directive for that site; don't.

## Develop

```
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest      # about 35 s
```

Tests need bash and `pgrep`. The SDK is pinned (`mcp==2.3.0`); the middleware API it relies on is provisional, so re-run the suite after any SDK bump, then regenerate `requirements.txt`.
