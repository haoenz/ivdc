"""控制台输出：不解释 rich 标记。"""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from ivdc.console import STYLE_WARN, emit


def test_emit_does_not_interpret_rich_markup() -> None:
    stream = StringIO()
    console = Console(file=stream, width=200)
    emit(console, "跳过: [b]a.mp4[/b]", STYLE_WARN)
    assert "[b]a.mp4[/b]" in stream.getvalue()
