# LeanDC - Lean Desktop Commander MCP

It gives AI chat access to the computer to read and write files, run commands, view results, and continue -- all dev tools in one chat. A connector also enables remote-machine work.

Tested with the Claude desktop app (**Chat**, not Claude Code), macOS (Apple Silicon) and Ubuntu, this tool is inspired by [Desktop Commander MCP](https://github.com/wonderwhy-er/DesktopCommanderMCP) but offers a leaner toolset for lazy programming. Compact definitions and trimmed output reduce token use per session.

Five tools: **`Run` | `Job` | `Read` | `Write` | `Edit`**

Two scenarios covered:
- **Local (stdio)**: use it from the Claude desktop app (Chat) on macOS to use the tools on that Mac.
- **Remote (HTTPS)**: run the MCP as a web service on a remote Ubuntu machine; ask Claude in the Chat app to use the tools on the remote machine using a connector. (Needs a TLS reverse proxy on Ubuntu)


## Why

Against Desktop Commander 0.3.1, over 9 scripted tasks (3 runs each, all passed), the weighted total was 70% fewer tokens and 26% lower cost with a warm prompt cache. Most of the savings come from the smaller tool schema (about 2,100 tokens vs. about 13,500 per session) and smaller tool results. The tokens Claude writes (file content, commands) are unchanged. Method, caveats, and how to rerun: [bench/](https://github.com/asif-r-porosh/leandc/blob/main/bench/README.md)

## Requirements
- Python 3.12 or newer with `venv` and `pip`
- Bash
- For remote: Ubuntu server with public DNS name, ports 80 and 443 open, and a TLS reverse proxy

## Setup
1. Run
```bash
curl -fsSL https://raw.githubusercontent.com/asif-r-porosh/leandc/refs/heads/main/install.sh | bash
```
Or, clone/download [repo](https://github.com/asif-r-porosh/leandc), `cd` into `leandc` and `./manual-install.sh`

```bash
git clone https://github.com/asif-r-porosh/leandc.git
cd leandc
./manual-install.sh
```

**Override default installation (options)**

- `LEANDC_HOME` (install dir)
- `LEANDC_CONFIG` (config path)
- `LEANDC_PYTHON` (which interpreter)


> The Installation -
a. creates a private virtualenv `~/.local/share/leandc/venv`
b. installs the pinned requirements and a copy of leandc
c. creates  `~/.config/leandc/config.toml`
d. runs the self-check and prints the command to use.

2. On macOS (local stdio): edit `~/.config/leandc/config.toml` to change default values; set `target = "mac"`, leave the `[http]` section
3. In the Claude desktop app (Chat), click on the Claude menu (macOS top menu bar) → `Settings` → `Developer` → `Edit Config`. Locate the file `claude_desktop_config.json` and add/merge the following in the file
```json
"mcpServers": {
    "leandc": {
      "command": "/Users/USER/.local/share/leandc/venv/bin/leandc",
      "args": ["serve", "--config", "/Users/USER/.config/leandc/config.toml"]
    }
  }
```
* _Replace **USER** with the OS username._

Quit and fully reopen the app (Cmd-Q, not just closing the window). Ask Claude to run `uname -a`; if the tools appear, it works.
4. On the remote Ubuntu server -
a. use instruction 1 to install.
b. create a hostname e.g. `leandc.example.com` pointing to the server's public IP. It must be DNS only. NB: `run` waits at most **120 seconds** and then returns a job id; any proxy or service in front of leandc must allow requests at least that long
c. generate a secret with `~/.local/share/leandc/venv/bin/leandc secret`, then edit ~/.config/leandc/config.toml -
```bash
target = "vm"

[http]
host = "127.0.0.1"
port = 8765
path_secret = "<the_generated_secret>"
allowed_hosts = ["leandc.example.com"]   # exactly the hostname Claude will call
```
Keep the file at mode `600`. `leandc check --config ~/.config/leandc/config.toml` must pass.
d. TLS proxy: reverse proxy host:port from nginx/caddy or any other alternative. for caddy, use `flush_interval -1`; reload; do not add a log directive for this site.
e. run it by `~/.local/share/leandc/venv/bin/leandc serve`

To make the service persistent, the supplied `systemd` unit can be used: [deploy/leandc.service](https://raw.githubusercontent.com/asif-r-porosh/leandc/refs/heads/main/deploy/leandc.service). It should already be in `~/leandc/deploy/leandc.service` if `./manual-install.sh` was used. Then -
```bash
sed -e "s|/home/youruser|$HOME|g" -e "s|youruser|$(id -un)|g" deploy/leandc.service | sudo tee /etc/systemd/system/leandc.service >/dev/null
sudo systemctl daemon-reload && sudo systemctl enable --now leandc
systemctl status leandc --no-pager | head -5
```

It restarts on failure and starts at boot. Logs: `journalctl -u leandc`.
Leave `KillMode` at its default (`control-group`): `leandc` kills its own jobs on stop, and stray children must not outlive the service.

5. In claude.ai, Settings → Connectors → Add custom connector, URL `https://leandc.example.com/<the_generated_secret>/mcp`; leave OAuth empty; ask Claude to run hostname.
* _<the_generated_secret> from 4.c_


Checking the endpoint from the server (use the real secret and hostname): a valid MCP request to `/<the_generated_secret>/mcp` answers 200; a bare GET to it answers 400 (it needs a real MCP request); a wrong path answers 404; a Host header not in `allowed_hosts` answers `421`.



## Security.

The only protection is one long secret in the URL path plus the allowed-hosts check. Anyone who learns the URL gets a shell as that user on that machine, so use a disposable server that holds no production secrets, and treat the connector URL like a password. leandc's own HTTP access log is off, so the secret is not written to the service log. To rotate it: leandc secret, edit the config, sudo systemctl restart leandc, update the connector URL.

**Remote reach.** leandc does not inspect or block commands. `run` can use `ssh`, `scp`, `rsync`, `sshpass` or any other tool to reach other hosts, with whatever keys, agent or credentials that user has. On a machine holding SSH keys or an ssh agent, a leaked connector URL or a prompt injection therefore extends to every host those keys can reach. On a server, use a dedicated user with no private keys and no agent forwarding, and keep it off networks you do not want it to reach. On a Mac, review any `ssh`, `scp` or `rsync` call before approving it in the app. leandc has no built-in approval prompt for these commands.

## Layout
Program location: `~/.local/share/leandc`
Config in `~/.config/leandc`
Logs and job buffers in `~/.local/state/leandc`
MCP clients run `~/.local/share/leandc/venv/bin/leandc`

## Commands
- `~/.local/share/leandc/venv/bin/leandc check`: self-check; the response ends with `self-check passed` if the check passes.
- `~/.local/share/leandc/venv/bin/leandc serve`: to serve on HTTPS

## Tools

| tool | does |
|---|---|
| `run(cmd, cwd?, env?, timeout?, max_chars?)` | bash, non-login, no rc files, stdin closed. Waits up to `timeout` (30 s default, max 120), then returns a job id and keeps running. Output is cleaned (ANSI, progress bars, repeated lines) and capped (8000 chars, max 30000, keeping the head and the tail). |
| `job(id?, action?, offset?, limit?, tail?, grep?, wait?)` | list, read, wait for, or kill a job. 1 MB rolling buffer per job, 24 h expiry. Jobs are lost on restart, and the server says so. |
| `read(path, offset?, limit?)` | numbered lines, 300 default and 1000 max, `next offset` in the header, a negative offset reads from the end. |
| `write(path, content, mode?)` | create, overwrite or append. `mode` is required if the file exists and is not empty. 800 lines max per call. Atomic. |
| `edit(path, old, new, replace_all?)` | exact match that must be unique; returns the changed region with line numbers; on a miss shows the closest match. |

Unknown parameters are rejected, and every error is one line: `ERROR <CODE>: what. next step.` `cwd` and `env` are passed on every call and are not remembered between calls.

The usage log (`~/.local/state/leandc/usage.jsonl`, rotated by size) holds metadata only: time, tool, duration, exit code, sizes, an 8-character hash of the path, and for `run` the command names used with counts, such as `{"ls":3,"grep":1}`. It never records arguments, paths or content. Use it to tune the caps.

## Configuration

`config.example.toml` lists every key with its default. Unknown keys are rejected at startup, and no tool can change the config file. `LEANDC_CONFIG` or `--config` selects the file; `LEANDC_PATH_SECRET` can supply the secret instead of the file. The `[caps]` section holds the output, read, timeout and buffer limits.

## Uninstall


Delete `~/.local/share/leandc`, `~/.config/leandc` and `~/.local/state/leandc`. On a server first run `sudo systemctl disable --now leandc && sudo rm /etc/systemd/system/leandc.service` (if `systemd` unit is used).

## Troubleshooting

- **`leandc check` shows FAIL:** it says what is missing, usually Python older than 3.12 (rerun `install.sh` with `LEANDC_PYTHON=/path/to/python3.12`) or no `bash`.
- **Tools do not appear in the desktop app:** the command in the app config must be the full absolute path of `venv/bin/leandc`, and the app must be fully restarted.
- **The connector cannot connect:** run the endpoint checks above. 421 means the hostname is not in `allowed_hosts`; 404 means the secret in the URL does not match the config; nothing at all means DNS, the proxy, or the service (`systemctl status leandc`).
- **A command seems to hang:** after 30 s (or the `timeout` you set) `run` returns a job id; follow it with `job`.
- **Command flags behave unexpectedly:** the userland differs (macOS is BSD, Linux is GNU). The `run` description names the flavor.

## Develop

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -e '.[dev]'
.venv/bin/python -m pytest      # about 35 s
```

Tests need bash and `pgrep`. The MCP SDK is pinned (`mcp==2.3.0`) and the middleware API it relies on is provisional, so re-run the suite after any SDK bump, then regenerate `requirements.txt`.

---
RnD: Used Claude Sonnet 5.5
