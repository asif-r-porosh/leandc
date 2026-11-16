# leandc-cli: guide for an LLM agent

`leandc-cli.py` is a command-line client for a remote machine, served by leandc. The client runs shell commands, reads and edits files, and copies files both ways. Usage: `python3 leandc-cli.py <command>`. The server address and secret are in `leandc-cli.json` next to the script. Never print, copy or quote `leandc-cli.json`.

## Output and exit codes

- Text from the remote machine goes to stdout. Messages from leandc-cli itself go to stderr and start with `leandc-cli:`.
- Exit 0: done.
- Exit 1: the remote side reported a problem. stdout starts with `ERROR <CODE>: ...` followed by a hint on the fix.
- Exit 2: leandc-cli could not make the call (bad config, network down, wrong usage). Read the `leandc-cli:` message on stderr.

## Commands

Quote the whole command or text. Relative remote paths sit under the default folder set in leandc. Absolute and `~/` paths are used as given.

| Task | Command |
|---|---|
| Check the connection | `python3 leandc-cli.py ping` |
| Run a command | `python3 leandc-cli.py run "ls -la"` |
| Run in another folder | `python3 leandc-cli.py run "make test" --cwd src` |
| Run a longer script | `python3 leandc-cli.py run - < script.sh` |
| Look at a file (numbered lines) | `python3 leandc-cli.py read src/app.py` |
| Look at the next part of a file | `python3 leandc-cli.py read src/app.py --offset 301` |
| Create a text file | `python3 leandc-cli.py write notes.txt --content "hello"` |
| Replace a text file | `python3 leandc-cli.py write notes.txt --mode overwrite --file local.txt` |
| Change part of a file | `python3 leandc-cli.py edit src/app.py --old "foo(" --new "bar("` |
| Send a file or folder | `python3 leandc-cli.py push src/app.py` or `python3 leandc-cli.py push src` |
| Fetch a file or folder | `python3 leandc-cli.py pull src/app.py` or `python3 leandc-cli.py pull src` |
| Server tool list and limits | `python3 leandc-cli.py help` |

## Rules that avoid mistakes

- Each `run` is a fresh shell. A `cd` or `export` does not carry over. Use `--cwd`, `--env NAME=VALUE`, or `cd x && command`.
- A `run` that takes longer than the wait returns `running job=<id>`. The command keeps running. Poll with `python3 leandc-cli.py job <id> --wait 60`. Use `--tail 50` or `--grep TEXT` to trim output, and `--action kill` to stop. Do not start the command again before checking the job.
- The same applies when leandc-cli reports `no reply within N s`: the command may still be running. Check `python3 leandc-cli.py job` (no id lists the jobs).
- Output is trimmed (head and tail kept, repeated lines collapsed). Ask for less, or use `--max-chars N`.
- `read` shows at most 1000 lines per call and cuts lines over 2000 characters. When `next offset=K` appears in the header, more follows. `read` is for looking. To get an exact copy of a file, use `pull`.
- `write` needs `--mode overwrite` or `--mode append` when the file already exists and has content; otherwise the reply is `ERROR EXISTS`. Large content is split into several calls automatically. Text only.
- `edit` needs `--old` to match exactly once. `NO_MATCH` means the text was not found. `MULTI_MATCH` means more than one match: lengthen the text, or add `--all` to replace every match. For multi-line text, use `--old-file` and `--new-file`.
- `push` and `pull` copy exact bytes (any file type, folders included) and check a sha256 hash. Each line of output ends with `[verified]` when the copy matched. Files over 5 MiB are refused; raise with `--max-bytes N`. Folders skip `.git`, `node_modules`, `__pycache__` and similar.
- `push LOCAL` and `pull REMOTE` use the same relative path on the other side by default. Give a second path to choose a different place: `pull src/app.py /tmp/app.py`.
- Local relative paths sit under `local_root` from `leandc-cli.json`.

## Common error codes

`NOT_FOUND` (no such path), `EXISTS` (file exists, pass a mode), `NO_MATCH` / `MULTI_MATCH` (edit text), `TOO_LARGE` (over a size limit), `BAD_PARAM` (wrong argument), `DENIED` (path not allowed), `JOB_EXPIRED` / `JOB_UNKNOWN` (job id is old or wrong).
