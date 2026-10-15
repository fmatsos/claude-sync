"""Push/pull orchestration and the safety invariants.

Invariants enforced here, not in the CLI:
- ``~/.claude/projects`` is never mutated in place — remapping happens in a
  staging temp dir, and the only writes to real session dirs go through
  rsync, which never deletes: ``-u`` (newer file wins) by default, or a plain
  overwrite with ``full=True`` — then every replaced file is kept in
  ``<home>/.claude/sync-backup/<stamp>`` on the receiving machine.
- A session file written within the last ``BUSY_SECONDS`` (on either machine)
  is a moving target: it is left out of the transfer and reported, unless
  ``include_busy=True``.
- The first pull that would merge into existing local sessions offers a
  tarball backup first (via the ``confirm`` callback).
"""

from __future__ import annotations

import difflib
import os
import tarfile
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from claude_sync.config import Config, Remote, resolve_remote
from claude_sync.projects import (
    GitState,
    OutsideReport,
    cluster_prefixes,
    find_outside_home,
    git_warnings,
)
from claude_sync.remap import PathMapper, encode_path, remap_file, remap_tree
from claude_sync.transport import Transport, recent_files

_PULL_MARKER = ".claude-sync-pulled"

BUSY_SECONDS = 120  # a session file written this recently is still being appended to


class SyncError(Exception):
    """A sync could not proceed."""


@dataclass
class ChangeSummary:
    new: int = 0
    updated: int = 0


@dataclass
class SyncReport:
    changes: list[str]  # rsync --itemize-changes lines
    dry_run: bool
    backup: Path | None = None
    skipped: list[str] = field(default_factory=list)  # dest was newer (--verbose)
    git: list[tuple[str, GitState]] = field(default_factory=list)  # advisory
    busy: list[str] = field(default_factory=list)  # left out: written to just now
    backup_dir: str | None = None  # --full: where replaced files were kept
    summary: dict[str, ChangeSummary] = field(init=False)

    def __post_init__(self) -> None:
        self.summary = summarize_changes(self.changes)

    @property
    def total_files(self) -> int:
        return sum(s.new + s.updated for s in self.summary.values())


def summarize_changes(changes: list[str]) -> dict[str, ChangeSummary]:
    """Group rsync itemized file changes by top-level project directory."""
    per_project: dict[str, ChangeSummary] = {}
    for line in changes:
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        flags, path = parts
        if len(flags) < 2 or flags[1] != "f":  # only files, not dirs/links
            continue
        project = path.split("/", 1)[0]
        summary = per_project.setdefault(project, ChangeSummary())
        if "+" in flags:  # e.g. ">f+++++++++" = file did not exist on target
            summary.new += 1
        else:
            summary.updated += 1
    return per_project


def _noop(_msg: str) -> None:
    pass


def _changed_files(lines: list[str]) -> list[str]:
    """File paths from rsync itemized lines (dirs and links are skipped)."""
    files = []
    for line in lines:
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and len(parts[0]) >= 2 and parts[0][1] == "f":
            files.append(parts[1])
    return files


def _translate(rel: str, mapper: PathMapper) -> str:
    """``<project>/<file>`` with the project dir name remapped."""
    project, _, rest = rel.partition("/")
    return f"{mapper.remap_dirname(project)}/{rest}"


def _backup_dir(home: str | Path) -> str:
    return f"{str(home).rstrip('/')}/.claude/sync-backup/{datetime.now():%Y%m%d-%H%M%S}"


def _copy(
    transport: Transport,
    src: str,
    dst: str,
    *,
    dry_run: bool,
    full: bool,
    busy: set[str],
    backup_dir: str,
) -> tuple[list[str], list[str]]:
    """One transfer: ``(itemized changes, busy files held back)``.

    Only busy files the transfer would actually have touched are held back
    and reported — an unchanged active session is not worth mentioning."""
    held: list[str] = []
    if busy:
        planned = transport.rsync(src, dst, dry_run=True, newer_wins=not full)
        held = sorted(set(_changed_files(planned)) & busy)
    changes = transport.rsync(
        src,
        dst,
        dry_run=dry_run,
        newer_wins=not full,
        backup_dir=backup_dir if full else None,
        exclude=held,
    )
    return changes, held


def _report_backup(changes: list[str], full: bool, dry_run: bool, backup_dir: str) -> str | None:
    """The backup dir, when a --full run actually replaced existing files."""
    if not full or dry_run:
        return None
    replaced = any(s.updated for s in summarize_changes(changes).values())
    return backup_dir if replaced else None


def _outside_home_error(
    report: OutsideReport, remote: Remote, local_home: Path, *, pull: bool
) -> SyncError:
    """Refusal message with a ready-to-paste mapping snippet built from
    VALIDATED paths only; unvalidated projects are listed, never suggested."""
    verb = "pull" if pull else "push"
    lines = [
        f"{len(report.names)} project(s) are outside the home directory — "
        f"the generic home remap can't place them for {remote.name!r}:"
    ]
    for name in sorted(report.validated):
        lines.append(f"  {name}")
    for name in report.undetermined:
        lines.append(f"  {name}  (path could not be determined)")
    for name in report.ambiguous:
        lines.append(f"  {name}  (session cwd doesn't match the directory name — resolve manually)")
    if report.validated:
        lines += ["", "add a mapping, e.g.:", ""]
        lines.append(f"[remotes.{remote.name}.mappings]")
        for prefix in cluster_prefixes(report.validated.values()):
            base = os.path.basename(prefix)
            if pull:  # validated paths are remote-side: map a local prefix to them
                lines.append(f'"{str(local_home).rstrip("/")}/{base}" = "{prefix}"')
            else:  # validated paths are local: land them under the remote home
                lines.append(f'"{prefix}" = "{remote.home.rstrip("/")}/{base}"')
        lines.append("")
    lines.append(f"or re-run with --force to {verb} them unchanged.")
    return SyncError("\n".join(lines))


def _skipped_newer(transport: Transport, src: str, dst: str) -> list[str]:
    """Files a just-finished -u merge skipped because dst was newer.

    Run AFTER the real transfer: a dry-run with the same flags minus -u
    would now transfer exactly the files -u refused (everything else was
    just synced and compares equal)."""
    return _changed_files(transport.rsync(src, dst, dry_run=True, newer_wins=False))


def _projects_dir(local_home: Path) -> Path:
    return local_home / ".claude" / "projects"


def resolve_projects(selectors: Sequence[str]) -> list[str]:
    """Turn CLI project selectors into encoded project directory names.

    A selector starting with "-" is an encoded name as shown by ``status``;
    anything else is a filesystem path. Relative paths resolve against the
    current directory; symlinks are kept as typed, because Claude Code keys
    sessions by the literal cwd. Order is preserved, duplicates dropped.
    """
    names: dict[str, None] = {}
    for sel in selectors:
        sel = sel.strip()
        if not sel:
            continue
        if sel.startswith("-"):
            names.setdefault(sel)
            continue
        path = sel if os.path.isabs(sel) else os.path.join(os.getcwd(), sel)
        names.setdefault(encode_path(os.path.normpath(path)))
    return list(names)


def _unknown_projects_error(missing: list[str], known: set[str]) -> SyncError:
    parts = []
    for name in missing:
        close = difflib.get_close_matches(name, known, n=1)
        parts.append(name + (f" (did you mean {close[0]}?)" if close else ""))
    return SyncError(
        "unknown local project(s): " + "; ".join(parts) + " — run claude-sync status for the list"
    )


def push(
    cfg: Config,
    *,
    remote: str | None = None,
    local_home: str | Path | None = None,
    projects: Sequence[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    full: bool = False,
    include_busy: bool = False,
    verbose: bool = False,
    check_git: bool = False,
    log: Callable[[str], None] = _noop,
) -> SyncReport:
    """Remap local sessions into a staging dir and merge them onto the remote.

    ``remote`` names the target (resolved via config.resolve_remote).
    ``projects`` limits the push to the selected projects (paths or encoded
    names); the optional [sync] extras are skipped when a selection is given.
    ``full`` overwrites differing remote files instead of keeping the newer
    one (the replaced files are backed up on the remote); the [sync] extras
    always merge. ``include_busy`` also sends session files written to just now.
    """
    target = resolve_remote(cfg, remote)
    local_home = Path(local_home) if local_home else Path.home()
    src = _projects_dir(local_home)
    if not src.is_dir() or not any(src.iterdir()):
        raise SyncError(f"no local sessions at {src}")
    only: set[str] | None = None
    if projects:
        only = set(resolve_projects(projects))
        known = {p.name for p in src.iterdir() if p.is_dir()}
        missing = sorted(only - known)
        if missing:
            raise _unknown_projects_error(missing, known)
    mapper = PathMapper.for_push(local_home, target.home, cfg.merged_mappings(target.name))
    if not force:
        outside = find_outside_home(src, local_home, mapper, only)
        if outside:
            raise _outside_home_error(outside, target, local_home, pull=False)
    transport = Transport(target.host, target.home)
    busy: set[str] = set()
    if not include_busy:
        busy = {_translate(r, mapper) for r in recent_files(src, BUSY_SECONDS)}
        busy |= set(transport.recent_sessions(BUSY_SECONDS))
    backup_dir = _backup_dir(target.home)
    with tempfile.TemporaryDirectory(prefix="claude-sync-") as tmp:
        staging = Path(tmp) / "projects"
        log("remapping sessions into staging…")
        remap_tree(src, staging, mapper, only=only)
        if not dry_run:
            transport.ensure_remote_projects()
        log("syncing to remote…")
        changes, held = _copy(
            transport,
            f"{staging}/",
            transport.remote_projects_arg + "/",
            dry_run=dry_run,
            full=full,
            busy=busy,
            backup_dir=backup_dir,
        )
        replaced_in = _report_backup(changes, full, dry_run, backup_dir)
        skipped: list[str] = []
        if verbose and not dry_run and not full:
            skipped = _skipped_newer(transport, f"{staging}/", transport.remote_projects_arg + "/")
        if only is None:
            changes += _push_extras(cfg, transport, local_home, mapper, Path(tmp), dry_run, log)
    git_notes: list[tuple[str, GitState]] = []
    if check_git and not dry_run:
        git_notes = git_warnings(src, only)
    return SyncReport(
        changes=changes,
        dry_run=dry_run,
        skipped=skipped,
        git=git_notes,
        busy=held,
        backup_dir=replaced_in,
    )


def pull(
    cfg: Config,
    *,
    remote: str | None = None,
    local_home: str | Path | None = None,
    projects: Sequence[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    full: bool = False,
    include_busy: bool = False,
    verbose: bool = False,
    confirm: Callable[[str], bool] = lambda _msg: True,
    log: Callable[[str], None] = _noop,
) -> SyncReport:
    """Fetch remote sessions, remap them for this machine, and merge them in.

    ``remote`` names the source (resolved via config.resolve_remote).
    ``projects`` limits the pull to the selected projects, named by their
    *local* path or encoded name — the selector is translated through the
    mapping and matched against the remote's project list, so a project
    that doesn't exist locally yet is valid. The [sync] extras are skipped
    when a selection is given. ``full`` overwrites differing local files
    instead of keeping the newer one (the replaced files are backed up under
    ``~/.claude/sync-backup``); the extras always merge. ``include_busy``
    also merges session files written to just now.
    """
    target = resolve_remote(cfg, remote)
    mappings = cfg.merged_mappings(target.name)
    local_home = Path(local_home) if local_home else Path.home()
    dest = _projects_dir(local_home)
    transport = Transport(target.host, target.home)
    remote_names: list[str] | None = None
    if projects:
        push_mapper = PathMapper.for_push(local_home, target.home, mappings)
        remote_names = list(
            dict.fromkeys(push_mapper.remap_dirname(n) for n in resolve_projects(projects))
        )
        missing = [
            n
            for n in remote_names
            if not transport.remote_exists(f"{transport.remote_projects}/{n}")
        ]
        if missing:
            raise SyncError("not found on the remote: " + ", ".join(missing))
    elif not transport.remote_projects_exists():
        raise SyncError(f"no sessions on the remote ({transport.remote_projects} is missing)")
    mapper = PathMapper.for_pull(local_home, target.home, mappings)
    backup: Path | None = None
    with tempfile.TemporaryDirectory(prefix="claude-sync-") as tmp:
        raw = Path(tmp) / "raw"
        staging = Path(tmp) / "projects"
        raw.mkdir()
        log("fetching remote sessions…")
        if remote_names is None:
            transport.rsync(transport.remote_projects_arg + "/", f"{raw}/")
        else:
            for name in remote_names:
                target = raw / name
                target.mkdir(parents=True, exist_ok=True)
                transport.rsync(
                    transport.remote_arg(f"{transport.remote_projects}/{name}") + "/",
                    f"{target}/",
                )
        if not force:
            outside = find_outside_home(raw, local_home, mapper)
            if outside:
                raise _outside_home_error(outside, target, local_home, pull=True)
        log("remapping paths…")
        remap_tree(raw, staging, mapper)

        marker = local_home / ".claude" / _PULL_MARKER
        first_pull = not marker.exists()
        if (
            not dry_run
            and first_pull
            and dest.is_dir()
            and any(dest.iterdir())
            and confirm(f"First pull into existing sessions at {dest} — back them up first?")
        ):
            log("backing up local sessions…")
            backup = _make_backup(dest, local_home)

        if not dry_run:
            dest.mkdir(parents=True, exist_ok=True)
        log("merging into local sessions…")
        busy: set[str] = set()
        if not include_busy:
            busy = {_translate(r, mapper) for r in transport.recent_sessions(BUSY_SECONDS)}
            busy |= set(recent_files(dest, BUSY_SECONDS))
        backup_dir = _backup_dir(local_home)
        changes, held = _copy(
            transport,
            f"{staging}/",
            f"{dest}/",
            dry_run=dry_run,
            full=full,
            busy=busy,
            backup_dir=backup_dir,
        )
        replaced_in = _report_backup(changes, full, dry_run, backup_dir)
        skipped: list[str] = []
        if verbose and not dry_run and not full:
            skipped = _skipped_newer(transport, f"{staging}/", f"{dest}/")
        if remote_names is None:
            changes += _pull_extras(cfg, transport, local_home, mapper, Path(tmp), dry_run, log)
        if not dry_run:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
    return SyncReport(
        changes=changes,
        dry_run=dry_run,
        backup=backup,
        skipped=skipped,
        busy=held,
        backup_dir=replaced_in,
    )


def _relabel(lines: list[str], prefix: str) -> list[str]:
    """Prefix itemized rsync paths so summaries group them sensibly."""
    out = []
    for line in lines:
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            out.append(f"{parts[0]} {prefix}/{parts[1]}")
    return out


def _push_extras(
    cfg: Config,
    transport: Transport,
    local_home: Path,
    mapper: PathMapper,
    tmp: Path,
    dry_run: bool,
    log: Callable[[str], None],
) -> list[str]:
    """Optional [sync] items: history (remapped) and agents/skills (verbatim)."""
    changes: list[str] = []
    claude_dir = local_home / ".claude"
    history = claude_dir / "history.jsonl"
    if cfg.include_history and history.is_file():
        log("syncing history…")
        staged = tmp / "history.jsonl"
        remap_file(history, staged, mapper)
        changes += transport.rsync(
            str(staged),
            transport.remote_arg(transport.remote_claude_path("history.jsonl")),
            dry_run=dry_run,
        )
    for flag, name in ((cfg.include_agents, "agents"), (cfg.include_skills, "skills")):
        src = claude_dir / name
        if flag and src.is_dir():
            log(f"syncing {name}…")
            changes += _relabel(
                transport.rsync(
                    f"{src}/",
                    transport.remote_arg(transport.remote_claude_path(name)) + "/",
                    dry_run=dry_run,
                ),
                name,
            )
    return changes


def _pull_extras(
    cfg: Config,
    transport: Transport,
    local_home: Path,
    mapper: PathMapper,
    tmp: Path,
    dry_run: bool,
    log: Callable[[str], None],
) -> list[str]:
    changes: list[str] = []
    claude_dir = local_home / ".claude"
    remote_history = transport.remote_claude_path("history.jsonl")
    if cfg.include_history and transport.remote_exists(remote_history):
        log("fetching history…")
        raw = tmp / "history.raw.jsonl"
        staged = tmp / "history.jsonl"
        transport.rsync(transport.remote_arg(remote_history), str(raw))
        remap_file(raw, staged, mapper)
        if not dry_run:
            claude_dir.mkdir(parents=True, exist_ok=True)
        changes += transport.rsync(
            str(staged), str(claude_dir / "history.jsonl"), dry_run=dry_run
        )
    for flag, name in ((cfg.include_agents, "agents"), (cfg.include_skills, "skills")):
        remote_dir = transport.remote_claude_path(name)
        if flag and transport.remote_exists(remote_dir):
            log(f"fetching {name}…")
            changes += _relabel(
                transport.rsync(
                    transport.remote_arg(remote_dir) + "/",
                    f"{claude_dir / name}/",
                    dry_run=dry_run,
                ),
                name,
            )
    return changes


def _make_backup(projects: Path, local_home: Path) -> Path:
    backups = local_home / ".claude" / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = backups / f"claude-sync-projects-{stamp}.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        tar.add(projects, arcname="projects")
    return path
