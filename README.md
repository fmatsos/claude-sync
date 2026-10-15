# claude-sync

Move [Claude Code](https://claude.com/claude-code) sessions between my laptop and my always-on VM
over SSH and rsync — no cloud, no accounts.

Derived from [claude-hop](https://github.com/SeiduTakizawa/Claude-hop) by Vasileios Papadimitriou
(MIT), which does the hard part: remapping the absolute paths that make sessions invisible on
another machine. This fork keeps that engine and adds an overwrite mode, a smarter guard for
sessions that are being written, and drops the upstream distribution machinery (PyPI upgrade,
install script, demo assets). See [CHANGELOG.md](CHANGELOG.md).

## Install

Private repository, installed from a local checkout:

```sh
uv tool install --editable ~/src/github.com/fmatsos/claude-sync   # puts `claude-sync` in ~/.local/bin
claude-sync init        # asks for the SSH host (an alias from ~/.ssh/config works), detects the rest
```

Requirements: Python ≥ 3.11 and `rsync` on the machine you run it from; `sshd` and `rsync` on the
other one (key-based SSH). Nothing needs to be installed on the VM: this machine is the operator,
remotes only need `sshd` and `rsync`.

## Use

Run it from the laptop. `push` sends the laptop's sessions to the remote, `pull` brings the
remote's sessions back.

```sh
claude-sync push                 # laptop → VM, --diff (default)
claude-sync pull                 # VM → laptop, --diff
claude-sync pull --full          # VM → laptop, overwrite every differing file
claude-sync push . --dry-run     # only the project in the current directory, preview only
claude-sync diff                 # what would move, in both directions
```

Then `claude --resume` on the other machine, from the same project directory.

| Command | What it does |
|---|---|
| `init` | Interactive setup: SSH host, connection test, remote home detection. |
| `status [REMOTE]` | Local projects and the name each gets on the remote. |
| `diff [REMOTE]` | What would sync, in both directions, without writing anything. |
| `push [REMOTE] [PROJECTS]` | Send local sessions to the remote. `--all` pushes to every remote. |
| `pull [REMOTE] [PROJECTS]` | Fetch the remote's sessions. `--all` pulls from every remote. |
| `doctor [REMOTE]` | SSH, rsync, config, clock skew, active sessions, stale temp dirs. |
| `remotes list/add/remove/rename/migrate` | Manage the remotes. |

`push` and `pull` accept `--diff` / `--full`, `--dry-run` (`-n`), `--yes` (`-y`), `--force`,
`--verbose` (`-v`) and `--all`.

### `--diff` and `--full`

- **`--diff` (default)** — copy what is new or changed, and keep a destination file that is newer
  than the source (`rsync -u`). Safe to run at any time.
- **`--full`** — overwrite every differing file on the destination with the source version, even
  when the destination is newer. Every replaced file is first copied to
  `<home>/.claude/sync-backup/<date>/` **on the receiving machine** (purge it from time to time).
  The `[sync]` extras (history, agents, skills) always merge, never overwrite.

Neither mode ever deletes: a session that exists only on the destination stays where it is.

### Sessions that are being written

Claude Code appends to the session file as it works, so a file written in the last 2 minutes — on
either machine — is a moving target. Such files are **left out of the transfer and listed**, the
rest goes through. A busy file that needed no transfer anyway is not mentioned. `--force` includes
them. `doctor` warns when local sessions are active.

This replaces upstream's "refuse while any `claude` process runs" check, which blocked every sync
on a laptop with Claude Code open and never looked at the other machine (where a Remote Control
session runs all night).

## How the remapping works

Claude Code stores sessions in `~/.claude/projects/<encoded-path>/<id>.jsonl`, where the encoded
path is the project's absolute path with every non-alphanumeric character replaced by `-`. The
absolute paths also appear inside the JSONL (`cwd`, file paths in tool calls). claude-sync rewrites
both while syncing, mapping your home here to your home there (detected at `init`). When both
machines use the same user and the same project paths, the remap is a no-op — the recommended
setup. Projects outside `$HOME` need a `[mappings]` prefix (`status` flags them).

## Configuration

`~/.config/claude-sync/config.toml` (written by `init`):

```toml
[remotes.vm]
host = "claude-box"        # SSH alias or hostname; "" = a local directory
home = "/home/franck"      # auto-detected at init

[sync]
include_history = false    # also ~/.claude/history.jsonl (remapped, always merged)
include_agents = false
include_skills = false

[mappings]
"/home/franck/work/blog" = "/srv/blog"   # only for projects at different paths
```

Environment: `CLAUDE_SYNC_CONFIG` (config path), `CLAUDE_SYNC_HOME` (treat a directory as the
local home — tests and the e2e harness).

## Safety model

- Never `--delete`. A sync can only add or update.
- `~/.claude/projects/` is only touched by the final rsync; remapping happens in a temp dir.
- `--full` backs up what it replaces; the first `pull` into existing sessions offers a tarball
  backup in `~/.claude/backups/`.
- Newer-wins merging relies on clocks: `doctor` warns above 30 s of skew with a remote.
- Transcripts can contain secrets read by tools. They travel over SSH only; do not put them in a
  repository or a shared folder.

## Limitations

- A resumed conversation continues in a *new* session file, so both the original and the
  continuation exist after syncing back. That is Claude Code's session model.
- Text-level path rewriting also rewrites a transcript's prose that mentions the other machine's
  home. Session files that are not valid UTF-8 are copied verbatim.
- Not covered: file state (use Git), running processes, native Windows (WSL works).

## Development

```sh
uv sync && uv run pytest      # unit + integration tests (rsync needed)
uv run ruff check .
make e2e                      # two-container SSH round trip (Docker)
```

## License

MIT. Copyright (c) 2026 Vasileios Papadimitriou (claude-hop) and Franck Matsos.
