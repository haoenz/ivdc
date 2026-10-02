"""测试共用夹具。"""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console


@pytest.fixture(autouse=True)
def isolated_user_config(tmp_path, monkeypatch):
    """所有命令测试只读临时配置，不受开发者真实配置或环境覆盖影响。"""
    monkeypatch.setenv("IVDC_CONFIG", str(tmp_path / "user-config" / "config.json"))


@pytest.fixture
def console_pair() -> tuple[Console, StringIO]:
    """写入内存缓冲的 Console 与它的缓冲。

    非终端 Console 会让 :class:`~ivdc.progress.ProgressDisplay` 自动降级成纯文本，
    断言因此只需看文本输出。
    """
    stream = StringIO()
    return Console(file=stream, width=200), stream
