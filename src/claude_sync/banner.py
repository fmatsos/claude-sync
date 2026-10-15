"""Version lookup and the one-line header shown by `claude-sync init`."""

from __future__ import annotations

from importlib import metadata

from rich.console import Console


def get_version() -> str:
    try:
        return metadata.version("claude-sync")
    except metadata.PackageNotFoundError:
        return "dev"


def show_banner(console: Console, version: str) -> None:
    console.print(f"claude-sync v{version}", highlight=False)
