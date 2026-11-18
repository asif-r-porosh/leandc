# LeanDC

**A Lightweight MCP Server for Local & Remote Development**

LeanDC is a small MCP (Model Context Protocol) tool server. It gives an MCP client five tools to run commands, read, write and edit files, and manage long-running jobs, on a Mac or a Linux machine. It was inspired by [Desktop Commander MCP](https://github.com/wonderwhy-er/DesktopCommanderMCP) and benchmarked against it (see [Benchmark](#benchmark)).

Five tools: **`run` | `job` | `read` | `write` | `edit`**

Two ways to use it:

- **Local (stdio):** the client starts LeanDC itself on the same machine. Nothing is exposed to the network.
- **Remote (HTTPS):** LeanDC runs as a service on a Mac or a Linux machine, and the client reaches it through a URL.

Tested with the Claude desktop app (local) and a claude.ai custom connector (remote), on macOS (Apple Silicon and Intel) and Ubuntu. Any client that speaks MCP over stdio or streamable HTTP should work; no other client has been tested.

## Requirements

- Python 3.12 or newer
- [pipx](https://pipx.pypa.io): `brew install pipx` on macOS, `sudo apt install pipx` on Ubuntu
- Bash
- For remote use: any machine the client can reach, Linux or Mac

## Install

```bash
pipx install git+https://github.com/asif-r-porosh/leandc
```

This installs `leandc` (the server) and `leandc-cli` (the [client](clients/leandc-cli/README.md)). To update, run `pipx install --force git+https://github.com/asif-r-porosh/leandc` again.

## Local use (stdio)

No config file is needed. The client starts LeanDC itself.

1. Find the full path of the program: `which leandc` (for example `/Users/USER/.local/bin/leandc`).
2. Add a server entry to the client's MCP config with that path as `command`. In the Claude desktop app: Claude menu (macOS top menu bar) → `Settings` → `Developer` → `Edit Config`, then add or merge this into `claude_desktop_config.json`:

   ```json
   "mcpServers": {
       "leandc": {
         "command": "/Users/USER/.local/bin/leandc",
         "args": ["serve", "--transport", "stdio"]
       }
     }
   ```

   Other clients take the same two values, `command` and `args`; the client's own documentation says where they go.
3. Restart the client fully (Claude desktop app: Cmd-Q, not just closing the window). Ask the client to run `uname -a`. If the tools appear, the setup works.

To change defaults, copy [`src/leandc/config.example.toml`](src/leandc/config.example.toml) to a private place (`chmod 600`), edit it, and add `"--config", "/path/to/config.toml"` to `args`.

## Run as a service

A service keeps LeanDC running in the background and starts it at boot, with nobody logged in. The same commands work on Linux (systemd) and on a Mac (launchd):

```bash
sudo pipx install --global git+https://github.com/asif-r-porosh/leandc
sudo leandc setup     # opens the config in an editor; a fresh secret is already filled in
sudo leandc start     # starts now and at every boot
leandc status         # no sudo needed
sudo leandc stop      # stops now and at boot
```

- **Mac:** `sudo` does not see a pipx in `~/.local/bin`, so give the full path: `sudo /path/to/pipx install --global ...`.
- **Ubuntu 24.04:** the pipx from apt (1.4.3) is too old for `--global` (needs 1.5.0). Use the standalone program instead: `curl -fsSL -o pipx.pyz https://github.com/pypa/pipx/releases/latest/download/pipx.pyz`, then `sudo python3 pipx.pyz install --global ...`.

### What the defaults do

If the commands above are run as they are, the install puts the program files under root's ownership, so nobody without sudo can change the code that `sudo leandc` runs. `setup` then creates the config `/etc/leandc/config.toml`, owned by root and holding a fresh secret, together with a group named `leandc`. The service can read the config and cannot change it. The editor is `$VISUAL`, `$EDITOR`, nano or vi, and a file is installed only after it passes the same checks the server runs. `sudo leandc setup --show` prints the config with the secret hidden.

`start` runs the service as the user who ran `sudo`, writes the service file (Linux: `/etc/systemd/system/leandc.service`; Mac: `/Library/LaunchDaemons/net.leandc.service.plist`), and starts the service now and at every boot. State is kept in `/var/lib/leandc`. `stop` stops the service and removes the boot start. `status` needs no sudo and shows whether the service runs, which user runs it and whether the config is protected; with sudo it adds the transport and port. Logs: Linux `journalctl -u leandc`; Mac `/var/log/leandc.log` (not rotated).

### When the defaults are not wanted

The recommended way to run as a different user is a separate limited user: `sudo leandc start alice`. How to create one is described under [Security](#security).

A user install (without `--global`) is fine for stdio. For the service, `--global` is recommended, because root runs the commands.

To skip the service entirely, run `leandc serve --config /path/to/config.toml` under any supervisor (systemd, launchd, tmux). The recommended way is to keep the config private (`chmod 600`) and, for `http`, to put the secret in `LEANDC_PATH_SECRET` instead of the file.

## Remote use (HTTPS)

1. **Choose the hostname** the client will call, for example `leandc.example.com`.
2. **Start the service** as above. In `setup`, set `allowed_hosts` to that hostname and keep `transport = "http"`:

   ```toml
   transport = "http"

   [http]
   host = "127.0.0.1"
   port = 8765
   path_secret = "<the_generated_secret>"        # filled in by setup
   allowed_hosts = ["leandc.example.com"]
   ```

3. **Give the machine an https address.** LeanDC itself speaks plain HTTP on `127.0.0.1:8765`, so something in front of it has to provide https. Two ways work equally well.

   A tunnel, such as Cloudflare Tunnel, needs no open ports and no public IP. Point the tunnel at `http://127.0.0.1:8765`. Calls of up to 118 seconds were checked through a Cloudflare Tunnel.

   A reverse proxy, such as Caddy or nginx, needs a machine with a public DNS name and ports 80 and 443 open. A Caddy example:

   ```
   leandc.example.com {
       reverse_proxy 127.0.0.1:8765 {
           flush_interval -1
       }
   }
   ```

   Use `flush_interval -1`, and add no `log` directive for the site, so the secret in the URL is not logged.

   Two notes. `run` waits at most **120 seconds** and then returns a job id, so anything in front of LeanDC must allow requests at least that long. With Cloudflare, use a one-level hostname (the free certificate covers one level only). Cloudflare's proxy also stops waiting for an answer after its read timeout, which its documentation gives as 120 seconds by default (older pages say 100). A `run` that waits the full 120 seconds sits right at that limit; a `timeout` of 90 or less, followed by `job` for the rest, stays clear of it. With a Cloudflare DNS record in front of a reverse proxy, a DNS-only record skips the proxy and its limit.
4. **Add the URL to the client:** `https://leandc.example.com/<the_generated_secret>/mcp`. The secret is the `path_secret` line in `/etc/leandc/config.toml` (`sudo cat` shows it). In claude.ai: Settings → Connectors → Add custom connector, paste the URL, leave OAuth empty, and ask Claude to run `hostname`. Other clients take the same URL as a streamable HTTP server.

## Security

The only protection is one long secret in the URL plus the allowed-hosts check. Anyone who learns the URL gets a shell as the user that runs LeanDC. Treat the URL like a password.

**Recommended: run the service as a separate limited user**, not the user that installed LeanDC. A leaked URL or a bad command then reaches only what that user can reach. On Linux, create the user and start the service as it. The command adds no extra groups; pick a name other than `leandc`, because `setup` creates a group of that name:

```bash
sudo useradd --create-home --shell /bin/bash leandcrun
sudo leandc start leandcrun
```

On a Mac, create a standard (non-admin) user in System Settings → Users & Groups, then run `sudo leandc start thatuser`. On either system, give that user no private SSH keys, no ssh agent and no `sudo`.

**Install with sudo (`--global`)**, so the program files belong to root and nothing a user or a tricked client can reach is able to change the code that `sudo leandc` runs.

**Remote reach.** LeanDC does not inspect or block commands. `run` can use `ssh`, `scp`, `rsync`, `sshpass` or any other tool to reach other hosts, with whatever keys, agent or credentials that user has. On a machine holding SSH keys or an ssh agent, a leaked URL or a prompt injection therefore extends to every host the keys can reach. Use a disposable server that holds no production secrets, kept off networks that should stay out of reach. On a Mac used through stdio, review any `ssh`, `scp` or `rsync` call before approving the call in the client. LeanDC has no built-in approval prompt for such commands.

LeanDC's own HTTP access log is off, so the secret is not written to the service log. To rotate the secret: `leandc secret`, put the new value into the config with `sudo leandc setup`, restart the service (Linux: `sudo systemctl restart leandc`; macOS: `sudo leandc start`), and update the client URL.

See [SECURITY.md](SECURITY.md) for the reasoning.

## Benchmark

Desktop Commander MCP is a well-known tool server for the same job, so LeanDC was measured against it.

| | Desktop Commander 0.3.1 | LeanDC |
|---|---|---|
| Tools | 25 | 5 |
| Tool descriptions | about 13,500 tokens | about 2,100 tokens |

In a benchmark of 11 development tasks run in Claude Code, LeanDC used 37% to 90% fewer tokens, and every run passed the task check. Method and results: [bench/](https://github.com/asif-r-porosh/leandc/blob/main/bench/README.md).

Desktop Commander covers more ground. Pick it when its wider feature set is needed; pick LeanDC when five tools are enough and a light footprint matters.

The benchmark ran in Claude Code and reflects those conditions only, not a projection for other clients. It measures tokens only and makes no claim about cost or about any plan's usage allowance.

## Reference

### Commands

- `leandc serve`: start the server; stdio when `transport = "stdio"`, HTTP when `transport = "http"` (`--transport stdio` or `--transport http` overrides the choice)
- `leandc check`: self-check; the response ends with `self-check passed` if the check passes
- `leandc secret`: print a new random URL secret
- `leandc setup`, `start`, `stop`, `status`: the service commands above
- `leandc version`

### Tools

| tool | does |
|---|---|
| `run(cmd, cwd?, env?, timeout?, max_chars?)` | bash, non-login, no rc files, stdin closed. Waits up to `timeout` (30 s default, max 120), then returns a job id and keeps running. Output is cleaned (ANSI, progress bars, repeated lines) and capped (8000 chars, max 30000, keeping the head and the tail). |
| `job(id?, action?, offset?, limit?, tail?, grep?, wait?)` | list, read, wait for, or kill a job. 1 MB rolling buffer per job, 24 h expiry. Jobs are lost on restart, and the server says so. |
| `read(path, offset?, limit?)` | numbered lines, 300 default and 1000 max, `next offset` in the header, a negative offset reads from the end. |
| `write(path, content, mode?)` | create, overwrite or append. `mode` is required if the file exists and is not empty. 800 lines max per call. Atomic. |
| `edit(path, old, new, replace_all?)` | exact match that must be unique; returns the changed region with line numbers; on a miss shows the closest match. |

Unknown parameters are rejected, and every error is one line: `ERROR <CODE>: what. next step.` `cwd` and `env` are passed on every call and are not remembered between calls.

The usage log (`usage.jsonl` in the state folder, rotated by size) holds metadata only: time, tool, duration, exit code, sizes, an 8-character hash of the path, and for `run` the command names used with counts, such as `{"ls":3,"grep":1}`. The log never records arguments, paths or content. Use the log to tune the caps.

### Configuration

`src/leandc/config.example.toml` lists every key with the default. Unknown keys are rejected at startup, and the server never changes the config file itself. `run`, `write` and `edit` can still edit a config that the runner owns; the service config in `/etc/leandc` is owned by root and locked. `LEANDC_CONFIG` or `--config` selects the file; `LEANDC_PATH_SECRET` can supply the secret instead of the file. The `[caps]` section holds the output, read, timeout and buffer limits. `[http] allowed_origins` lists the browser origins the HTTP endpoint accepts; the default is the two sites the tested connector comes from, and a list in the config replaces the default.

### Where things are

- Program: kept by pipx in its own folder (`pipx environment` shows where); the commands sit in `~/.local/bin`, or in `/usr/local/bin` after `--global`
- Config: none by default for stdio; `--config FILE` or `$LEANDC_CONFIG` selects one. The service config is `/etc/leandc/config.toml`
- State (job buffers, usage log): `~/.local/state/leandc`, or `/var/lib/leandc` for the service

### Mac service details

launchd starts the job with a short `PATH`; the plist sets Homebrew's folders, `/usr/local/bin` and the system folders. `setup` creates the group with `dseditgroup`, and the daemon runs with that group, so the runner reads the config and cannot change it. `stop` unloads the plist and removes it, which is what keeps the service from starting at boot.

### Uninstall

Run `pipx uninstall leandc` (after `--global`: `sudo pipx uninstall --global leandc`). Delete `~/.config/leandc` if one was made, and `~/.local/state/leandc`.

If the service was set up, first run `sudo leandc stop`, then remove what `setup` made:

- Linux: delete `/etc/systemd/system/leandc.service` and `/etc/leandc`, then `sudo groupdel leandc`.
- Mac: delete `/etc/leandc`, `/var/lib/leandc` and `/var/log/leandc.log`, then `sudo dseditgroup -o delete leandc`.

### Troubleshooting

- **`leandc check` shows FAIL:** the output says what is missing, usually Python older than 3.12 (install a newer Python, then `pipx install --force --python /path/to/python3.12 git+https://github.com/asif-r-porosh/leandc`) or no `bash`.
- **Tools do not appear in the client:** the command in the client config must be the full absolute path from `which leandc`, and the client must be fully restarted.
- **The client cannot connect:** check the endpoint from the server (use the real secret and hostname):
  - a valid MCP request to `/<the_generated_secret>/mcp` answers `200`
  - a bare GET to that path answers `400` (a real MCP request is required)
  - a wrong path answers `404`: the secret in the URL does not match the config
  - a Host header not in `allowed_hosts` answers `421`
  - a browser Origin header not in `allowed_origins` answers `403` (requests without an Origin header are not affected)
  - nothing at all points to DNS, the tunnel or proxy, or the service (`leandc status`)
- **A command seems to hang:** after 30 s (or the `timeout` set) `run` returns a job id; follow the job with `job`.
- **Command flags behave unexpectedly:** the userland differs (macOS is BSD, Linux is GNU). The `run` description names the flavor.

## Develop

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -e '.[dev]'
.venv/bin/python -m pytest  # about 35 s
```

Tests need bash and `pgrep`. The MCP SDK is pinned (`mcp==2.3.0`) and the middleware API relied on by LeanDC is provisional, so re-run the suite after any SDK bump, then regenerate `requirements.txt`.

---

RnD: Used Claude Sonnet 5.5
