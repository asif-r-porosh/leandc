# leandc

A lean MCP server that gives Claude five tools (`run`, `job`, `read`, `write`, `edit`) to run commands and work with files on a machine you control. The tool definitions are small and the output is trimmed, so a session spends fewer tokens than with the larger tool sets.

Two ways to run it, from one codebase:

- **Local (stdio):** the Claude desktop app, or any MCP client, launches it on your own computer. Nothing listens on the network.
- **Remote (HTTPS):** it runs as a service on a Linux server you control, behind a TLS reverse proxy, and you add it to claude.ai as a custom connector.

## Why

Against Desktop Commander 0.3.1, over 9 scripted tasks (3 runs each, all passed), the weighted total was 70% fewer tokens and 26% lower cost with a warm prompt cache. Most of the saving is the smaller tool schema (about 2,100 tokens against about 13,500 per session) plus smaller tool results. The tokens Claude writes (file content, commands) are unchanged. Method, caveats and how to rerun: [`bench/`](bench/README.md).

## Requirements

- Python 3.12 or newer with `venv` and `pip`, and `bash`. leandc uses only the commands the OS ships (`ls`, `sed`, `grep`, ...) and installs nothing else; Claude is told the OS and userland flavor (macOS BSD or Linux GNU) so it writes matching commands.
- Tested on macOS (Apple silicon) and Ubuntu 24.04. Ubuntu: `sudo apt install python3 python3-venv`.
- Remote only: a Linux server with a public DNS name, ports 80 and 443 open, and a TLS reverse proxy (the example uses Caddy).

## Install

Run this on every machine that will host leandc (your computer, the server, or both):

```
git clone <repo-url> leandc && cd leandc
./install.sh
```

`install.sh` creates a private virtualenv in `~/.local/share/leandc/venv`, installs the pinned requirements and a copy of leandc (so the checkout can be moved or deleted), copies `config.example.toml` to `~/.config/leandc/config.toml` if you have no config yet (mode 600), runs the self-check, and prints the command to use. Re-run it to update. Options: `LEANDC_HOME` (install dir), `LEANDC_CONFIG` (config path), `LEANDC_PYTHON` (which interpreter).

Layout: program in `~/.local/share/leandc`, config in `~/.config/leandc`, logs and job buffers in `~/.local/state/leandc`. MCP clients just run the absolute path of `~/.local/share/leandc/venv/bin/leandc`; there is no standard install location beyond that.

`leandc check` runs the self-check and must end with `self-check passed`. `serve` runs the same check and refuses to start on a FAIL.

## Local setup (Claude desktop)

1. Install as above. In `~/.config/leandc/config.toml` set `target = "mac"` (stdio only; named for macOS, the tested local case) and leave the `[http]` section alone.
2. Add leandc to the Claude desktop app's config, `~/Library/Application Support/Claude/claude_desktop_config.json`. Merge this into the `mcpServers` object, with your own home directory written out in full:

```json
{
  "mcpServers": {
    "leandc": {
      "command": "/Users/YOU/.local/share/leandc/venv/bin/leandc",
      "args": ["serve", "--config", "/Users/YOU/.config/leandc/config.toml"]
    }
  }
}
```

3. Quit and reopen the app. Ask Claude to run `uname -a`; if the tools appear, it works.

An app launched from the Dock has a minimal PATH, so Claude sees the stock macOS tools (BSD userland, bash 3.2); the `run` description says so. Optional extras such as `rg`, `fd`, `jq` are listed only if they are on that PATH.

Other MCP clients that start stdio servers work the same way. For Claude Code: `claude mcp add leandc -- ~/.local/share/leandc/venv/bin/leandc serve --config ~/.config/leandc/config.toml`.

## Remote setup (a Linux server, HTTPS)

1. **Install** on the server, as the user that will run the service.
2. **DNS:** create a one-level hostname such as `leandc.example.com` pointing at the server's public IP (check the IP on the server itself first). It must be **DNS only**. If you use Cloudflare, that means grey cloud: the orange proxy cuts requests at about 100 seconds, and leandc allows 120-second calls.
3. **Config:** generate a secret with `~/.local/share/leandc/venv/bin/leandc secret`, then edit `~/.config/leandc/config.toml`:

   ```toml
   target = "vm"

   [http]
   host = "127.0.0.1"
   port = 8765
   path_secret = "<the generated secret>"
   allowed_hosts = ["leandc.example.com"]   # exactly the hostname Claude will call
   ```

   Keep the file at mode 600. `leandc check --config ~/.config/leandc/config.toml` must pass.
4. **TLS proxy:** add the site from `deploy/Caddyfile.snippet` to your Caddyfile (hostname, `reverse_proxy 127.0.0.1:8765`, `flush_interval -1`) and reload Caddy. Do not add a `log` directive for this site: the secret is part of the URL path.
5. **Service:** `deploy/leandc.service` is a template with `youruser` placeholders. Install it with your user and home filled in, then start it:

   ```
   sed -e "s|/home/youruser|$HOME|g" -e "s|youruser|$(id -un)|g" deploy/leandc.service | sudo tee /etc/systemd/system/leandc.service >/dev/null
   sudo systemctl daemon-reload && sudo systemctl enable --now leandc
   systemctl status leandc --no-pager | head -5
   ```

   It restarts on failure and starts at boot. Logs: `journalctl -u leandc`. Leave `KillMode` at its default (`control-group`): leandc kills its own jobs on stop, and stray children must not outlive the service.
6. **Connector:** in claude.ai, Settings > Connectors > Add custom connector, URL `https://leandc.example.com/<secret>/mcp`, leave OAuth empty. Ask Claude to run `hostname`.

Checking the endpoint from the server (use the real secret and hostname): a valid MCP request to `/<secret>/mcp` answers 200; a bare `GET` to it answers 400 (it needs a real MCP request); a wrong path answers 404; a `Host` header not in `allowed_hosts` answers 421.

**Security.** The only protection is one long secret in the URL path plus the allowed-hosts check. Anyone who learns the URL gets a shell as that user on that machine, so use a disposable server that holds no production secrets, and treat the connector URL like a password. leandc's own HTTP access log is off, so the secret is not written to the service log. To rotate it: `leandc secret`, edit the config, `sudo systemctl restart leandc`, update the connector URL.

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

## Update and uninstall

Update: `git pull && ./install.sh`, then restart (`sudo systemctl restart leandc` on a server; quit and reopen the Claude desktop app locally).

Uninstall: delete `~/.local/share/leandc`, `~/.config/leandc` and `~/.local/state/leandc`. On a server first run `sudo systemctl disable --now leandc && sudo rm /etc/systemd/system/leandc.service`.

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
