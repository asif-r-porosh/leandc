# leandc-cli

A small command-line client for a [leandc](../../README.md) server. One file, Python standard library only.

leandc speaks MCP. Some places can run scripts but have no MCP support, for example a chat sandbox or a CI job. `leandc-cli.py` reaches a leandc server over HTTPS from such a place: run commands, read, write and edit files, and copy files in both directions.

## Getting the client

- **Installed:** `pipx install git+https://github.com/asif-r-porosh/leandc` provides the `leandc-cli` command together with the `leandc` server.
- **Copied:** the source is the single file `src/leandc/client.py` in this repository. A copy saved as `leandc-cli.py` on another machine (for example a chat sandbox) runs with `python3 leandc-cli.py`. No other file is needed.

The two forms behave the same. The examples below use the copied form; the installed command takes the same arguments.

## Requirements

- Python 3. The script is written for 3.8 and newer, and has been tested on 3.12 and 3.13.
- A leandc server in HTTP mode (`transport = "http"`) that is reachable over HTTPS from the machine running the script.

## Setup

1. Copy `leandc-cli.json.example` to `leandc-cli.json`, next to `leandc-cli.py`. An installed `leandc-cli` finds the file at `~/.config/leandc-cli/leandc-cli.json`.
2. Put the connector URL in `url`. The URL has the form `https://<host>/<secret>/mcp`.
3. Keep the file private (`chmod 600`). The secret is part of the URL. The file is git-ignored in this repository.
4. Check the connection: `python3 leandc-cli.py ping`

The file is searched in this order: `--config PATH`, the environment variable `LEANDC_CLI_CONFIG`, next to the script, the current folder, `~/.config/leandc-cli/leandc-cli.json`. `LEANDC_CLI_URL` and `LEANDC_CLI_FALLBACK_URL` override the URL settings.

| Key | Meaning |
|---|---|
| `url` | The leandc connector URL. Required. |
| `fallback_url` | A second URL, tried only when the first one cannot be reached. Optional. |
| `local_root` | The local folder that relative local paths resolve under. Default: the folder the command runs from. |
| `project` | An optional subfolder added to relative remote paths. |
| `timeout` | Seconds before a call is abandoned. Default 90. One call waits at most `timeout` minus 10 s, up to 120 s. |

Keys starting with `_` are ignored. Unknown keys are rejected.

## Commands

Quote the whole command or text. Relative remote paths sit under the default folder set in leandc. Absolute and `~/` paths are used as given.

| Command | Does |
|---|---|
| `ping` | Check the connection. |
| `help` | Print the usage text and the server's tool list. |
| `run "ls -la" [--cwd D] [--timeout S] [--max-chars N] [--env K=V]` | Run a shell command. `run -` reads the script from stdin. |
| `job [ID] [--action output\|status\|kill] [--tail N] [--grep RX] [--wait S]` | List jobs, or read, wait for or stop one. |
| `read PATH [--offset N] [--limit N]` | Show a text file with line numbers. |
| `write PATH [--mode create\|overwrite\|append] [--content TEXT \| --file LOCAL]` | Write a text file. Without content, stdin is used. |
| `edit PATH --old TEXT --new TEXT [--all]` | Replace exact text. `--old-file` and `--new-file` read the text from files. |
| `push LOCAL [REMOTE] [--max-bytes N]` | Send a file or folder, byte for byte. |
| `pull REMOTE [LOCAL] [--max-bytes N]` | Fetch a file or folder, byte for byte. |

- A `run` that takes longer than the wait returns `running job=<id>` and keeps running on the server. Follow the job with `job <id> --wait 60`.
- `push` and `pull` carry any file type through `run` as gzip and base64, then compare a sha256 hash. A line ending in `[verified]` means the copy matched. The limit is 5 MiB per file, changed with `--max-bytes`. Folders skip `.git`, `node_modules`, `__pycache__` and similar.
- `read` is for looking: at most 1000 lines per call, and long lines are cut. Use `pull` for an exact copy.

## Output and exit codes

The text from the server goes to stdout. Messages from `leandc-cli.py` itself go to stderr and start with `leandc-cli:`. The URL secret is hidden in output and messages (shown as `<secret>` at the spot where the secret would appear).

| Exit code | Meaning |
|---|---|
| 0 | Done. |
| 1 | The server reported a problem. The text starts with `ERROR <CODE>:` and a hint. |
| 2 | The call could not be made: config, network or usage. |

## Tests

```
python3 test_leandc-cli.py
```

This runs the unit tests, the packaging checks and the tests against fake servers. The client under test is `src/leandc/client.py`. The tests against a real server run when `LEANDC_CLI_TEST_CONFIG` points to a config file for a scratch leandc:

```
LEANDC_CLI_TEST_CONFIG=/path/to/leandc-cli.json python3 test_leandc-cli.py
```

Those tests write and delete files in a folder with a random name under the server's default folder. Use a throwaway server, not one that holds real work.

## Other files in this folder

- `LEANDC-CLI-GUIDE.md`: a short usage guide written for an LLM agent.
- `BRIEF_FOR_ZAI.md`: a brief for the z.ai chat sandbox.
