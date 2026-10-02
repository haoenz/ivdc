"""命令行入口：参数校验与退出码。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

from ivdc import __version__
from ivdc.cli import app
from ivdc.exitcodes import EXIT_ENV, EXIT_OK
from ivdc.quality import Quality

runner = CliRunner()

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def test_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == EXIT_OK
    help_text = Text.from_ansi(result.stdout).plain
    for command in ("opt", "dl", "clean"):
        assert command in help_text


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == EXIT_OK
    assert __version__ in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["opt", "--codec", "h264"],
        ["opt", "--cuda", "full"],
        ["opt", "--workers", "99"],
        ["opt", "--quality", "ultra"],
        ["dl", "--on-error", "explode"],
    ],
)
def test_invalid_option_values_are_rejected(args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 2


def test_opt_on_missing_directory_returns_env_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["opt", "-p", str(tmp_path / "missing")])
    assert result.exit_code == EXIT_ENV


def test_clean_without_kinds_returns_env_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["clean", "-p", str(tmp_path)])
    assert result.exit_code == EXIT_ENV


def test_dl_on_missing_task_file_returns_env_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["dl", "--tasks", str(tmp_path / "nope.txt")])
    assert result.exit_code == EXIT_ENV


@pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg")
def test_opt_dry_run_on_empty_directory_is_ok(tmp_path: Path) -> None:
    result = runner.invoke(app, ["opt", "-p", str(tmp_path), "--dry-run"])
    assert result.exit_code == EXIT_OK


@pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg")
def test_clean_dry_run_on_untouched_directory_is_ok(tmp_path: Path) -> None:
    result = runner.invoke(app, ["clean", "-p", str(tmp_path), "--all", "--dry-run"])
    assert result.exit_code == EXIT_OK


@pytest.mark.parametrize("option", ["--what-if", "--throttle", "--crf"])
def test_removed_options_are_not_aliases(option):
    result = runner.invoke(app, ["opt", option])
    assert result.exit_code == 2


def test_help_exposes_python_options():
    result = runner.invoke(app, ["opt", "--help"])
    assert result.exit_code == EXIT_OK
    # CI 中 Typer 的 ANSI 样式可能将选项名拆开，按可见文本检查。
    help_text = Text.from_ansi(result.stdout).plain
    assert "--dry-run" in help_text
    assert "--workers" in help_text
    assert "--quality" in help_text
    assert "--crf" not in help_text


@pytest.mark.parametrize("quality", [None, *Quality])
def test_quality_cli_passes_selected_or_default_level(monkeypatch, quality):
    received = []

    def execute(options, console):
        received.append(options)
        return EXIT_OK

    monkeypatch.setattr("ivdc.cli.execute_optimize", execute)
    result = runner.invoke(app, ["opt", *(["--quality", quality.value] if quality else [])])
    assert result.exit_code == EXIT_OK, result.output
    assert received[0].quality is (quality or Quality.medium)
