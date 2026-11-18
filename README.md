# LeanDC

**A Lightweight MCP Server for Local & Remote Development**

LeanDC is a lightweight MCP (Model Context Protocol) tool server. It gives an MCP client five tools to run commands, read, write and edit files, and manage long-running jobs, on a Mac or on a remote Linux machine.

Inspired by [Desktop Commander MCP](https://github.com/wonderwhy-er/DesktopCommanderMCP), LeanDC is built to keep token use low, with compact tool descriptions and trimmed output.

Tested with the Claude desktop app (local setup) and a claude.ai custom connector (remote setup), on macOS (Apple Silicon and Intel) and Ubuntu. Any client that speaks MCP over stdio or streamable HTTP should be able to use LeanDC, but no other client has been tested.

Five tools: **`run` | `job` | `read` | `write` | `edit`**

Two scenarios covered:

- **Local (stdio)**: the client starts LeanDC on the same machine; the tools act on that machine.
- **Remote (HTTPS)**: LeanDC runs as a web service on a remote Linux machine; the client calls it through a URL. (Needs a TLS reverse proxy on that machine)

## Why LeanDC?

Desktop Commander MCP is a capable tool server that already provides such capabilities. The broader scope of Desktop Commander 0.3.1 comes with 25 tools and about 13,500 tokens of tool descriptions.

LeanDC takes a narrower approach: 5 tools and about 2,100 tokens of tool descriptions. LeanDC covers the common cases (run commands, read, write, edit, background jobs) while keeping the tool overhead low.

To see what that does to token use, a benchmark ran 11 development tasks in Claude Code. LeanDC used fewer tokens than Desktop Commander 0.3.1 on all 11 tasks, from 37% to 90% fewer, and every run passed the task check.

Benchmark methodology and results: [bench/](https://github.com/asif-r-porosh/leandc/blob/main/bench/README.md)

The benchmark was run with Claude Code. The results reflect the tested conditions and should not be read as a projection of real-world use in any other client. The benchmark measures tokens only and makes no claim about cost or about any plan's usage allowance.

## Requirements

- Python 3.12 or newer
- [pipx](https://pipx.pypa.io) (`brew install pipx` on macOS, `sudo apt install pipx` on Ubuntu). The service commands below need pipx 1.5.0 or newer for `--global`; Ubuntu 24.04 ships 1.4.3 (see the Linux service section)
- Bash
- For remote: a Linux server with a public DNS name, ports 80 and 443 open, and a TLS reverse proxy

## Install

```bash
pipx install git+https://github.com/asif-r-porosh/leandc
```

The command installs two programs: `leandc` (the server) and `leandc-cli` (the [client](clients/leandc-cli/README.md)). To update, run `pipx install --force git+https://github.com/asif-r-porosh/leandc` again.

## Local setup (stdio)

No config file is needed. The client starts LeanDC itself.

1. Find the full path of the program: `which leandc` (for example `/Users/USER/.local/bin/leandc`).
2. Add a server entry to the MCP client's config, with that path as `command`. In the Claude desktop app: Claude menu (macOS top menu bar) → `Settings` → `Developer` → `Edit Config`, then add or merge the following into `claude_desktop_config.json`:

   ```json
   "mcpServers": {
       "leandc": {
         "command": "/Users/USER/.local/bin/leandc",
         "args": ["serve", "--transport", "stdio"]
       }
     }
   ```

   _Replace the path with the output of `which leandc`._ Other clients take the same two values, `command` and `args`; see the client's own documentation for where they go.
3. Restart the client fully (in the Claude desktop app: Cmd-Q, not just closing the window). Ask the client to run `uname -a`; if the tools appear, the setup works.

To change defaults, copy [`src/leandc/config.example.toml`](src/leandc/config.example.toml) to a private place (`chmod 600`), edit it, and add `"--config", "/path/to/config.toml"` to `args`. The `[http]` section is ignored for stdio.

## Run as a service

The same four commands work on Linux (systemd) and on a Mac (launchd): `sudo leandc setup`, `sudo leandc start [username]`, `sudo leandc stop` and `leandc status`. The service starts at boot with nobody logged in. The config is `/etc/leandc/config.toml`, owned by root: the user the service runs as can read the config and cannot change it.

Two safety points:

- Install with sudo (`--global`). The program files then belong to root, so nothing a user or a tricked client can reach is able to change the code that `sudo leandc` runs as root.
- Run the service as a separate limited user, not the one that installed LeanDC. A leaked connector URL or a bad command then reaches only what that user can reach. Give that user no private SSH keys, no ssh agent and no `sudo`.

### As a service on Linux (systemd)

`--global` needs pipx 1.5.0 or newer. Ubuntu 24.04 ships pipx 1.4.3: use the standalone program instead (`curl -fsSL -o pipx.pyz https://github.com/pypa/pipx/releases/latest/download/pipx.pyz`, then `sudo python3 pipx.pyz install --global ...` in place of `sudo pipx install --global ...`).

Create the limited user first. The command adds no extra groups. Pick a name other than `leandc`, because `setup` creates a group of that name:

```bash
sudo useradd --create-home --shell /bin/bash leandcrun
```

```bash
sudo pipx install --global git+https://github.com/asif-r-porosh/leandc
sudo leandc setup      # edits the config in an editor; a fresh secret is already filled in
sudo leandc start leandcrun   # without a name, runs as the sudo user
leandc status
sudo leandc stop       # stops now and at boot
```

- `setup` opens `$VISUAL`, `$EDITOR`, nano or vi, checks the file on save and installs only a valid one. The HTTP settings need `allowed_hosts` set to the public hostname. `setup --show` prints the config with the secret hidden.
- `start` and `stop` last across reboots. The unit file is `/etc/systemd/system/leandc.service`. Logs: `journalctl -u leandc`.
- `status` needs no sudo. With sudo, the output adds the transport and port.
- Removal by hand: `sudo leandc stop`, delete the unit file and `/etc/leandc`, then `sudo groupdel leandc`.

### As a service on macOS (launchd)

The service is a launchd daemon that runs as the user given to `start`. Create a standard (non-admin) user in System Settings → Users & Groups for that purpose.

```bash
sudo /path/to/pipx install --global git+https://github.com/asif-r-porosh/leandc
sudo leandc setup
sudo leandc start      # runs as the sudo user; `sudo leandc start alice` picks another user
leandc status
sudo leandc stop       # stops now and at boot
```

- `sudo` does not see a pipx that lives in `~/.local/bin`: give the full path of pipx, as above.
- `setup` creates the group `leandc` with `dseditgroup`, and the daemon runs with that group, so the runner reads the config and cannot change it.
- The plist is `/Library/LaunchDaemons/net.leandc.service.plist`. `start` writes it; `stop` unloads it and removes it, which is what keeps it from starting at boot.
- State sits in `/var/lib/leandc` and the output in `/var/log/leandc.log` (launchd has no journal). The log is not rotated.
- launchd starts the job with a short `PATH`; the plist sets Homebrew's folders, `/usr/local/bin` and the system folders.
- Removal by hand: `sudo leandc stop`, delete `/etc/leandc`, `/var/lib/leandc` and `/var/log/leandc.log`, then `sudo dseditgroup -o delete leandc`.

## Remote setup (HTTPS)

1. **Hostname.** Create a hostname, for example `leandc.example.com`, pointing to the server's public IP. The hostname must be DNS only. NB: `run` waits at most **120 seconds** and then returns a job id; any proxy or service in front of LeanDC must allow requests at least that long.

2. **Service.** Install and start the service as in [As a service on Linux (systemd)](#as-a-service-on-linux-systemd). In `setup`, keep `transport = "http"` and set `allowed_hosts` to exactly the hostname the client will call:

   ```toml
   transport = "http"

   [http]
   host = "127.0.0.1"
   port = 8765
   path_secret = "<the_generated_secret>"        # filled in by setup
   allowed_hosts = ["leandc.example.com"]
   ```

3. **TLS proxy.** Reverse proxy the hostname to `127.0.0.1:8765` with nginx, Caddy or any other. For Caddy:

   ```
   leandc.example.com {
       reverse_proxy 127.0.0.1:8765 {
           flush_interval -1
       }
   }
   ```

   Use `flush_interval -1`, and do not add a `log` directive for the site, so the secret in the URL is not logged. With Cloudflare, use a one-level hostname (the free SSL certificate covers one level only) and a DNS-only record: the orange-cloud proxy cuts requests at about 100 seconds, and LeanDC allows 120-second calls.

4. **Client.** The URL is `https://leandc.example.com/<the_generated_secret>/mcp`. The secret is the `path_secret` line in `/etc/leandc/config.toml` (`sudo cat`). In claude.ai: Settings → Connectors → Add custom connector, paste the URL, leave OAuth empty, and ask Claude to run `hostname`. Other clients take the same URL as a streamable HTTP server.

Checking the endpoint from the server (use the real secret and hostname):

- a valid MCP request to `/<the_generated_secret>/mcp` answers `200`
- a bare GET to that path answers `400` (a real MCP request is required)
- a wrong path answers `404`
- a Host header not in `allowed_hosts` answers `421`
- a browser Origin header not in `allowed_origins` answers `403` (requests without an Origin header are not affected)

## Security

The only protection is one long secret in the URL path plus the allowed-hosts check. Anyone who learns the URL gets a shell as that user on that machine, so use a disposable server that holds no production secrets, and treat the connector URL like a password. LeanDC's own HTTP access log is off, so the secret is not written to the service log. To rotate the secret: `leandc secret`, put the new value into the config with `sudo leandc setup`, restart the service (Linux: `sudo systemctl restart leandc`; macOS: `sudo leandc start`), and update the client URL.

**Remote reach.** LeanDC does not inspect or block commands. `run` can use `ssh`, `scp`, `rsync`, `sshpass` or any other tool to reach other hosts, with whatever keys, agent or credentials that user has. On a machine holding SSH keys or an ssh agent, a leaked connector URL or a prompt injection therefore extends to every host the keys can reach. On a server, use a dedicated user with no private keys and no agent forwarding, and keep the server off networks that should stay out of reach. On a Mac, review any `ssh`, `scp` or `rsync` call before approving the call in the client. LeanDC has no built-in approval prompt for such commands.

See [SECURITY.md](SECURITY.md) for the reasoning.

## Layout

- Program: kept by pipx in its own folder (`pipx environment` shows where); the commands sit in `~/.local/bin`, or in `/usr/local/bin` after `--global`
- Config: none by default; `--config FILE` or `$LEANDC_CONFIG` selects one. The service config is `/etc/leandc/config.toml`
- State (job buffers, usage log): `~/.local/state/leandc`, or `/var/lib/leandc` for the service
- MCP clients run the full path printed by `which leandc`

## Commands

- `leandc serve`: start the server; stdio when `transport = "stdio"`, HTTP behind the TLS proxy when `transport = "http"` (`--transport stdio` or `--transport http` overrides the choice)
- `leandc check`: self-check; the response ends with `self-check passed` if the check passes.
- `leandc secret`: print a new random URL secret
- `leandc setup`, `start`, `stop`, `status`: the service commands above
- `leandc version`

## Tools

| tool | does |
|---|---|
| `run(cmd, cwd?, env?, timeout?, max_chars?)` | bash, non-login, no rc files, stdin closed. Waits up to `timeout` (30 s default, max 120), then returns a job id and keeps running. Output is cleaned (ANSI, progress bars, repeated lines) and capped (8000 chars, max 30000, keeping the head and the tail). |
| `job(id?, action?, offset?, limit?, tail?, grep?, wait?)` | list, read, wait for, or kill a job. 1 MB rolling buffer per job, 24 h expiry. Jobs are lost on restart, and the server says so. |
| `read(path, offset?, limit?)` | numbered lines, 300 default and 1000 max, `next offset` in the header, a negative offset reads from the end. |
| `write(path, content, mode?)` | create, overwrite or append. `mode` is required if the file exists and is not empty. 800 lines max per call. Atomic. |
| `edit(path, old, new, replace_all?)` | exact match that must be unique; returns the changed region with line numbers; on a miss shows the closest match. |

Unknown parameters are rejected, and every error is one line: `ERROR <CODE>: what. next step.` `cwd` and `env` are passed on every call and are not remembered between calls.

The usage log (`usage.jsonl` in the state folder, rotated by size) holds metadata only: time, tool, duration, exit code, sizes, an 8-character hash of the path, and for `run` the command names used with counts, such as `{"ls":3,"grep":1}`. The log never records arguments, paths or content. Use the log to tune the caps.

## Configuration

`src/leandc/config.example.toml` lists every key with the default. Unknown keys are rejected at startup, and the server never changes the config file itself. `run`, `write` and `edit` can still edit a config that the runner owns; the service config in `/etc/leandc` is owned by root and locked. `LEANDC_CONFIG` or `--config` selects the file; `LEANDC_PATH_SECRET` can supply the secret instead of the file. The `[caps]` section holds the output, read, timeout and buffer limits. `[http] allowed_origins` lists the browser origins the HTTP endpoint accepts; the default is the two sites the tested connector comes from, and a list in the config replaces the default.

## Uninstall

`pipx uninstall leandc` (after `--global`: `sudo pipx uninstall --global leandc`). Delete `~/.config/leandc` if one was made, and `~/.local/state/leandc`. On a machine that runs the service, first follow "Removal by hand" under the service section for the platform.

## Troubleshooting

- **`leandc check` shows FAIL:** the output says what is missing, usually Python older than 3.12 (install a newer Python, then `pipx install --force --python /path/to/python3.12 git+https://github.com/asif-r-porosh/leandc`) or no `bash`.
- **Tools do not appear in the client:** the command in the client config must be the full absolute path from `which leandc`, and the client must be fully restarted.
- **The client cannot connect:** run the endpoint checks above. 421 means the hostname is not in `allowed_hosts`; 404 means the secret in the URL does not match the config; 403 means the request carries an Origin that is not in `allowed_origins`; nothing at all means DNS, the proxy, or the service (`leandc status`).
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
