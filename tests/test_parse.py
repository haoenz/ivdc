"""任务清单行、ffmpeg 进度、ffprobe JSON 的解析覆盖。"""

from __future__ import annotations

import pytest

from ivdc.parse import (
    is_progress_line,
    parse_out_time,
    parse_progress_line,
    parse_task_line,
    strip_fraction,
)


def test_url_only() -> None:
    task = parse_task_line("https://example.com/video")
    assert task.title is None
    assert task.url == "https://example.com/video"


def test_title_then_url() -> None:
    task = parse_task_line("我的视频 https://example.com/v")
    assert task.title == "我的视频"
    assert task.url == "https://example.com/v"


def test_url_then_title_is_equivalent() -> None:
    task = parse_task_line("https://example.com/v 我的视频")
    assert task.title == "我的视频"
    assert task.url == "https://example.com/v"


def test_title_containing_spaces_is_preserved() -> None:
    task = parse_task_line("我的 视频  https://example.com/v")
    assert task.title == "我的 视频"
    assert task.url == "https://example.com/v"


def test_split_on_last_space_when_no_scheme() -> None:
    task = parse_task_line("标题 something.mp4")
    assert task.title == "标题"
    assert task.url == "something.mp4"


def test_single_token_is_url() -> None:
    task = parse_task_line("single-token")
    assert task.title is None
    assert task.url == "single-token"


def test_url_with_trailing_query_and_fragment() -> None:
    task = parse_task_line("标题 https://example.com/v?a=1&b=2#frag")
    assert task.title == "标题"
    assert task.url == "https://example.com/v?a=1&b=2#frag"


def test_blank_line_yields_empty_url() -> None:
    task = parse_task_line("   ")
    assert task.title is None
    assert task.url == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("00:00:00.000000", 0.0),
        ("00:01:23.456789", 83.456789),
        ("01:02:03.000000", 3723.0),
        ("25:00:00.000000", 90000.0),
    ],
)
def test_parse_out_time_valid(raw: str, expected: float) -> None:
    assert parse_out_time(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["N/A", "", "   ", "garbage", "1:2", "aa:bb:cc"])
def test_parse_out_time_invalid(raw: str) -> None:
    assert parse_out_time(raw) is None


def test_parse_progress_line() -> None:
    assert parse_progress_line("out_time=00:00:01.234567") == ("out_time", "00:00:01.234567")
    assert parse_progress_line("progress=continue") == ("progress", "continue")
    assert parse_progress_line("out_time_us=1234567") == ("out_time_us", "1234567")


def test_parse_progress_line_ignores_other_output() -> None:
    assert parse_progress_line("[download]  12.3% of 100MiB") is None
    assert parse_progress_line("") is None


def test_strip_fraction() -> None:
    assert strip_fraction("00:01:23.456789") == "00:01:23"
    assert strip_fraction("00:01:23") == "00:01:23"
    assert strip_fraction("  00:00:01.5  ") == "00:00:01"


def test_parse_progress_line_accepts_every_key_ffmpeg_emits() -> None:
    for key in (
        "frame",
        "fps",
        "bitrate",
        "total_size",
        "out_time_us",
        "out_time_ms",
        "out_time",
        "dup_frames",
        "drop_frames",
        "speed",
        "progress",
    ):
        assert parse_progress_line(f"{key}=1") is not None


@pytest.mark.parametrize("raw", ["out_time=00:00:01.0", " stream_0_0_q=0.0", "total_size=123"])
def test_is_progress_line_accepts_progress_output(raw: str) -> None:
    assert is_progress_line(raw) is True


@pytest.mark.parametrize(
    "raw",
    [
        "[download] 12% of 1MiB",
        "Error opening output file x.mp4",
        "",
        "  ",
        "file 'seg_000.mp4'",
    ],
)
def test_is_progress_line_rejects_error_text(raw: str) -> None:
    assert is_progress_line(raw) is False
