import io

from rich.console import Console

from claude_sync.banner import show_banner


def test_banner_is_a_single_plain_line():
    buf = io.StringIO()
    show_banner(Console(file=buf, width=100, force_terminal=True), "1.2.3")
    assert "claude-sync v1.2.3" in buf.getvalue()
    assert len(buf.getvalue().strip().splitlines()) == 1
