# Security

## What leandc is

leandc gives an MCP client a shell (`run`) and file tools (`read`, `write`, `edit`), all running as the OS user that started the server. There is no sandbox. Anyone who can call the tools can do whatever that user can do on that machine.

## Supported versions

Only the latest release and the `main` branch receive security fixes.

## Reporting a vulnerability

Use GitHub private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability**. Please do not open a public issue for a vulnerability.

## How access is controlled

- **stdio** (`transport = "stdio"`): the server never listens on a network port. Whoever can start the MCP client can use the tools.
- **HTTP** (`transport = "http"`): the only lock is a secret in the URL path, `/<secret>/mcp`. The secret must be at least 24 characters and must not contain `/`. Generate one with `leandc secret`.
  - The `Host` header must match `allowed_hosts`, with DNS-rebinding protection on. The allowed browser origins default to `https://claude.ai` and `https://claude.com` and are set with `allowed_origins` in the `[http]` section.
  - leandc speaks plain HTTP on the local address. A TLS proxy or tunnel has to be the public face, because the secret is part of the URL.
  - leandc's own HTTP access log is off, so the secret is not written to the service log.
  - Treat the URL like a password. To rotate: run `leandc secret`, put the new value in the config (or in `LEANDC_PATH_SECRET`), restart the service, and update every client.

## What leandc does not do

- **No command filtering.** `run` does not inspect or block commands, and can use `ssh`, `scp`, `rsync` or any other tool to reach other hosts, with whatever keys, agent or credentials the server user has.
- **`allowed_roots` is a guardrail, not a boundary.** The setting limits `read`, `write` and `edit` and does not apply to `run`.
- **The config file is an ordinary file.** The file is read at startup and no tool changes the running configuration. Still, `write`, `edit` and `run` can modify the file on disk, and the next restart applies the change.
- **No approval prompt.** leandc has no built-in confirmation step for any call.

## Recommended setup

- Run the server as a dedicated OS user with no `sudo` or admin rights, no private SSH keys and no agent forwarding.
- Prefer a disposable server that holds no production secrets, kept off networks that should stay out of reach.
- On a Mac used through stdio, review any `ssh`, `scp` or `rsync` call before approving the call in the client.
- See the "Run as a service" section of the [README](README.md#run-as-a-service) for running the server as a dedicated user.

## Usage log

The usage log holds metadata only: time, tool, duration, outcome, sizes, a short hash of the path, and the names (never the arguments) of the commands used in `run`. Logging is switched off with `[log] enabled = false`.
