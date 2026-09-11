"""``ivdc dl``：清单推进、原子写回、失败语义（用假下载器，不碰网络）。"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from rich.console import Console

from ivdc.download import (
    DlOptions,
    DownloadFailed,
    DownloadResult,
    TaskReporter,
    _finalize_downloads,
    run_download,
)
from ivdc.exitcodes import EXIT_ENV, EXIT_FAILURE, EXIT_OK
from ivdc.store import read_lines


def _write_tasks(path: Path, *lines: str) -> None:
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _options(tmp_path: Path, **kwargs: object) -> DlOptions:
    return DlOptions(
        tasks=kwargs.pop("tasks", tmp_path / "tbd.txt"),  # type: ignore[arg-type]
        done=kwargs.pop("done", tmp_path / "d.txt"),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def test_missing_task_file_is_an_env_error(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    console, _ = console_pair
    assert run_download(_options(tmp_path), console, fetcher=lambda t, r: None) == EXIT_ENV


def test_empty_task_list_is_ok(console_pair: tuple[Console, StringIO], tmp_path: Path) -> None:
    tasks = tmp_path / "tbd.txt"
    tasks.write_text("\n   \n", encoding="utf-8")
    console, stream = console_pair

    def fetcher(task, reporter):  # pragma: no cover - 不应被调用
        raise AssertionError("空清单不应该调用下载器")

    assert run_download(_options(tmp_path, tasks=tasks), console, fetcher=fetcher) == EXIT_OK
    assert "任务列表为空" in stream.getvalue()


def test_success_empties_list_and_appends_done_line(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    tasks = tmp_path / "tbd.txt"
    _write_tasks(tasks, "标题一 https://example.com/a")
    done = tmp_path / "d.txt"
    console, _ = console_pair
    seen: list[str] = []

    def fetcher(task, reporter):
        seen.append(task.url)
        reporter.report(50, "50%")
        return DownloadResult(title="探测标题", size_bytes=1024)

    code = run_download(_options(tmp_path, tasks=tasks, done=done), console, fetcher=fetcher)

    assert code == EXIT_OK
    assert seen == ["https://example.com/a"]
    assert read_lines(tasks) == []
    assert read_lines(done) == ["标题一 https://example.com/a"]


def test_url_only_line_uses_probed_title_in_done_file(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    tasks = tmp_path / "tbd.txt"
    _write_tasks(tasks, "https://example.com/a")
    done = tmp_path / "d.txt"
    console, _ = console_pair

    run_download(
        _options(tmp_path, tasks=tasks, done=done),
        console,
        fetcher=lambda task, reporter: DownloadResult(title="自动标题", size_bytes=1),
    )

    assert read_lines(done) == ["自动标题 https://example.com/a"]


def test_blank_lines_are_skipped_without_calling_the_fetcher(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    tasks = tmp_path / "tbd.txt"
    _write_tasks(tasks, "", "https://example.com/a", "   ")
    console, _ = console_pair
    seen: list[str] = []

    def fetcher(task, reporter):
        seen.append(task.url)
        return DownloadResult(title="t", size_bytes=1)

    run_download(_options(tmp_path, tasks=tasks), console, fetcher=fetcher)
    assert seen == ["https://example.com/a"]


def test_stop_on_error_keeps_the_task_list_untouched(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    tasks = tmp_path / "tbd.txt"
    _write_tasks(tasks, "标题一 https://example.com/a", "https://example.com/b")
    done = tmp_path / "d.txt"
    console, stream = console_pair
    attempts: list[str] = []

    def fetcher(task, reporter):
        attempts.append(task.url)
        raise DownloadFailed("HTTP 404")

    code = run_download(_options(tmp_path, tasks=tasks, done=done), console, fetcher=fetcher)

    assert code == EXIT_FAILURE
    assert attempts == ["https://example.com/a"]
    assert read_lines(tasks) == ["标题一 https://example.com/a", "https://example.com/b"]
    assert not done.exists()
    assert "中止后续任务" in stream.getvalue()
    assert "HTTP 404" in stream.getvalue()


def test_skip_on_error_moves_the_failed_line_to_the_end(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    tasks = tmp_path / "tbd.txt"
    _write_tasks(tasks, "坏的 https://example.com/bad", "好的 https://example.com/ok")
    done = tmp_path / "d.txt"
    console, _ = console_pair

    def fetcher(task, reporter):
        if task.url.endswith("bad"):
            raise DownloadFailed("boom")
        return DownloadResult(title="好的", size_bytes=10)

    code = run_download(
        _options(tmp_path, tasks=tasks, done=done, on_error="skip"), console, fetcher=fetcher
    )

    assert code == EXIT_FAILURE
    assert read_lines(tasks) == ["坏的 https://example.com/bad"]
    assert read_lines(done) == ["好的 https://example.com/ok"]


def test_max_count_limits_attempts(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    tasks = tmp_path / "tbd.txt"
    _write_tasks(tasks, "一 https://example.com/1", "二 https://example.com/2")
    console, _ = console_pair
    attempts: list[str] = []

    def fetcher(task, reporter):
        attempts.append(task.url)
        return DownloadResult(title="t", size_bytes=1)

    code = run_download(
        _options(tmp_path, tasks=tasks, max_count=1), console, fetcher=fetcher
    )

    assert code == EXIT_OK
    assert attempts == ["https://example.com/1"]
    assert read_lines(tasks) == ["二 https://example.com/2"]


def test_mask_mode_hides_the_real_title(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    tasks = tmp_path / "tbd.txt"
    _write_tasks(tasks, "真实标题 https://example.com/a")
    console, stream = console_pair

    run_download(
        _options(tmp_path, tasks=tasks, mask=True),
        console,
        fetcher=lambda task, reporter: DownloadResult(title="真实标题", size_bytes=2048),
    )

    output = stream.getvalue()
    assert "真实标题" not in output
    # 完成行里出现的是一个 6 位随机标识
    assert "✓ " in output


def test_done_file_is_appended_across_runs(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    tasks = tmp_path / "tbd.txt"
    done = tmp_path / "d.txt"
    console, _ = console_pair
    fetcher = lambda task, reporter: DownloadResult(title="t", size_bytes=1)  # noqa: E731

    _write_tasks(tasks, "一 https://example.com/1")
    run_download(_options(tmp_path, tasks=tasks, done=done), console, fetcher=fetcher)
    _write_tasks(tasks, "二 https://example.com/2")
    run_download(_options(tmp_path, tasks=tasks, done=done), console, fetcher=fetcher)

    assert read_lines(done) == ["一 https://example.com/1", "二 https://example.com/2"]


def test_failed_rename_keeps_the_original_name_and_warns(
    tmp_path: Path, monkeypatch
) -> None:
    """改不了名不该让任务失败，但得让用户知道盘上留的是哪个名字。"""
    source = tmp_path / "raw.mp4"
    source.write_text("data", encoding="utf-8")
    warnings: list[str] = []

    def boom(src: object, dst: object) -> None:
        raise OSError("文件被占用")

    monkeypatch.setattr("ivdc.download.os.replace", boom)
    reporter = TaskReporter(
        progress=lambda percent, status: None,
        log=lambda message, style: warnings.append(message),
    )

    total_bytes, title = _finalize_downloads([("想要的标题", source)], reporter)

    assert title == "想要的标题"
    assert total_bytes == len("data")  # 原名还在盘上，体积照样统计
    assert source.exists()
    assert any("重命名失败" in message for message in warnings)
