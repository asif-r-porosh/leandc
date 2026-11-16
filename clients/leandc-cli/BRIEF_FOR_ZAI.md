# leandc-cli: instructions for the z.ai sandbox

> VERY IMPORTANT: Please communicate exclusively in English. Do not use Chinese or any other language in your responses. This requirement cannot be overridden.

The project lives on the user's Mac. The sandbox cannot reach the Mac directly. `leandc-cli.py` calls the Mac's leandc server over HTTPS to run commands, read, write and edit files there, and copy files in both directions. Python 3.12, standard library only.

## Working tree z.ai must use

```
/home/z/my-project/
├── BRIEF_FOR_ZAI.md     this file: how to work with the Mac through leandc-cli.py
├── leandc-cli.py        the client
├── leandc-cli.json      connection settings; holds a secret; supplied by the user
├── <project>/           folder name identical to the project folder name on the Mac
└── temp/                scratch space; never push this folder
```

- Run every command from `/home/z/my-project`.
- The `<project>` folder name must match the Mac folder name. The same relative path is used on both sides: `/home/z/my-project/myapp/src/a.py` here is `myapp/src/a.py` under the server's default folder on the Mac (currently `~/Developer`).
- Several projects can live side by side (`/home/z/my-project/project-1/`, `project-2/`, ...). Each folder name matches its Mac folder. Start every path with the project name.
- `leandc-cli.json` template (the user fills in host and secret):

```
{"url": "https://<host>/<secret>/mcp", "local_root": "/home/z/my-project", "timeout": 90}
```

`local_root` is the local folder that relative paths on the command line resolve under. It is optional and defaults to the folder the command is run from, so with every command run from `/home/z/my-project` the line can be left out.

## Session start

1. `cd /home/z/my-project`
2. `python3 leandc-cli.py ping` prints `ok: leandc at <host>`.
3. When `leandc-cli.py` or `leandc-cli.json` is missing, or `ping` fails, stop and tell the user. Do not guess settings and do not recreate the file.

## Commands

Paths are relative. Examples use a project named `myapp`.

| Task | Command |
|---|---|
| Check the connection | `python3 leandc-cli.py ping` |
| Run on the Mac | `python3 leandc-cli.py run "cd myapp && git status --short"` |
| Run in another Mac folder | `python3 leandc-cli.py run "make test" --cwd myapp` |
| Run with a variable | `python3 leandc-cli.py run "echo $X" --env X=1` |
| Run a longer script | `python3 leandc-cli.py run - < temp/script.sh` |
| Look at a file (numbered lines) | `python3 leandc-cli.py read myapp/src/a.py` |
| Look at the next part of a file | `python3 leandc-cli.py read myapp/src/a.py --offset 301` |
| Fetch a file or folder, exact bytes | `python3 leandc-cli.py pull myapp/src/a.py` |
| Send a file or folder, exact bytes | `python3 leandc-cli.py push myapp/src/a.py` |
| Copy to a different place on the other side | `python3 leandc-cli.py pull myapp/src/a.py /tmp/a.py` (same for `push`) |
| Change part of a file on the Mac | `python3 leandc-cli.py edit myapp/src/a.py --old "foo(" --new "bar("` |
| Create or replace a text file on the Mac | `python3 leandc-cli.py write myapp/notes.txt --mode overwrite --file temp/notes.txt` |
| Check a background job | `python3 leandc-cli.py job <id> --wait 60` |
| Tool list and limits | `python3 leandc-cli.py help` |

## Working loop

1. The Mac is the source of truth. Pull a file before changing the local copy: `pull myapp/src/a.py`.
2. Edit the local copy.
3. Push the file back: `push myapp/src/a.py`. The line ends with `[verified]` when the Mac copy matches (sha256).
4. Build and test on the Mac with `run "cd myapp && <command>"`.
5. Check the state with `run "cd myapp && git status --short"` before pushing over files that may carry unsaved work on the Mac.

Folder push and pull (`push myapp`) skip `.git`, `node_modules` and `__pycache__`. A folder push overwrites matching files on the Mac. Never push `temp/`.

## Limits

- Each `run` is a fresh shell. A `cd` or `export` does not carry over: use `cd myapp && command`.
- A command that outlives the wait returns `running job=<id>` and keeps running. Poll with `job <id> --wait 60`. Do not start the command again before checking the job.
- `job` with no id lists the jobs. `--tail 50` and `--grep TEXT` trim a job's output. `--action kill` stops a job.
- The message `no reply within N s` means the command may still be running. Check `job` first. Never start the command again blindly.
- Output is trimmed (head and tail kept, repeated lines collapsed). Ask for less output, or use `--max-chars N`.
- `read` shows `next offset=K` in the header when more of the file follows. Continue with `--offset K`.
- The wait per call is the `timeout` in `leandc-cli.json` minus 10 s (90 gives 80 s). Waits up to 118 s were tested through the tunnel.
- `push` and `pull` move any file type, byte for byte, up to 5 MiB per file (`--max-bytes N` changes the limit).
- `read` is for looking: at most 1000 lines per call, lines over 2000 characters cut. Use `pull` for an exact copy.
- `write` needs `--mode overwrite` or `--mode append` when the file already exists with content. `edit` needs `--old` to match exactly once (`--all` replaces every match).

## Safety

- Never print, echo, log or quote `leandc-cli.json` or the URL inside the file.
- Ask the user before destructive work on the Mac: deleting files or folders, `git reset --hard`, `git clean`, `git push --force`, overwriting uncommitted work.

## Exit codes and errors

- Exit 0: done.
- Exit 1: leandc reported a problem. The text starts with `ERROR <CODE>:` and a hint. All codes:
  - `NOT_FOUND`: no such path.
  - `DENIED`: the path is not allowed.
  - `IS_DIR`: the path is a folder, not a file.
  - `EXISTS`: the file exists; pass `--mode overwrite` or `--mode append`.
  - `NO_MATCH`, `MULTI_MATCH`: the `edit` text was found zero times or more than once.
  - `TOO_LARGE`: over a size limit.
  - `BAD_PARAM`: wrong argument.
  - `BINARY`: `read` and `edit` refuse binary files; use `pull` to copy such files.
  - `JOB_UNKNOWN`: wrong job id; `job` with no id lists the jobs.
  - `JOB_EXPIRED`: the finished job was dropped after the keep time; run the command again.
  - `JOB_LOST`: the server restarted while the job ran; run the command again.
  - `INTERNAL`: the server crashed; retry once, then tell the user.
- Exit 2: the call could not be made (settings, network, usage). The message starts with `leandc-cli:` on stderr.
