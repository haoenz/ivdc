"""百分比、终端摘要和日志格式。"""

from __future__ import annotations

from datetime import date

import pytest

from ivdc.formatting import (
    format_clock,
    format_done_line,
    format_download_line,
    format_download_status,
    format_fail_line,
    format_log_line,
    format_media_size,
    format_size_mib,
    progress_percent,
    shorten_label,
)


@pytest.mark.parametrize(
    ("elapsed", "total", "expected"),
    [
        (30.0, 100.0, 30),
        (30.9, 100.0, 30),
        (0.0, 100.0, 0),
        (100.0, 100.0, 100),
        (200.0, 100.0, 100),
        (-5.0, 100.0, 0),
        (10.0, 0.0, 0),
        (10.0, -1.0, 0),
    ],
)
def test_progress_percent(elapsed: float, total: float, expected: int) -> None:
    assert progress_percent(elapsed, total) == expected


@pytest.mark.parametrize(
    ("num_bytes", "expected"),
    [(0, "0"), (1024 * 1024, "1"), (1_294_000_000, "1,234"), (594_000_000, "566")],
)
def test_format_size_mib(num_bytes: int, expected: str) -> None:
    assert format_size_mib(num_bytes) == expected


def test_format_log_line_layout() -> None:
    line = format_log_line(date(2026, 9, 11), "a.mp4", 1_294_000_000, 594_000_000, 3.456)
    assert line == "26-09-11 a.mp4 | 1,234MB -> 566MB | 3.5m"


def test_format_fail_line() -> None:
    line = format_fail_line(date(2026, 9, 11), "a.mp4", "部分分片缺失")
    assert line == "26-09-11 a.mp4 | 压制失败: 部分分片缺失"


def test_format_done_line() -> None:
    line = format_done_line("a.mp4", 2 * 1024 * 1024, 1024 * 1024, 1.25)
    assert line == "✓ a.mp4 | 2MB -> 1MB | 1.2m"


def test_format_download_line_with_and_without_size() -> None:
    assert format_download_line("标题", "12.5MiB", 1.25) == "✓ 标题 | 12.5MiB | 1.2m"
    assert format_download_line("标题", "", 1.25) == "✓ 标题 | 1.2m"


@pytest.mark.parametrize(
    ("num_bytes", "expected"),
    [
        (0, "0B"),
        (512, "512B"),
        (1024, "1.0KiB"),
        (1536, "1.5KiB"),
        (1024**2, "1.0MiB"),
        (12 * 1024**2 + 512 * 1024, "12.5MiB"),
        (3 * 1024**3, "3.0GiB"),
        (1024**4, "1.0TiB"),
    ],
)
def test_format_media_size(num_bytes: int, expected: str) -> None:
    assert format_media_size(num_bytes) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "00:00"),
        (5, "00:05"),
        (65, "01:05"),
        (3599, "59:59"),
        (3600, "1:00:00"),
        (3723, "1:02:03"),
    ],
)
def test_format_clock(seconds: float, expected: str) -> None:
    assert format_clock(seconds) == expected


def test_format_clock_clamps_negative_values() -> None:
    assert format_clock(-10) == "00:00"


def test_format_download_status_variants() -> None:
    total = 12 * 1024**2 + 512 * 1024
    assert (
        format_download_status(1024**2, total, 1024**2, 12)
        == "1.0MiB/12.5MiB at 1.0MiB/s ETA 00:12"
    )
    assert format_download_status(1024, 0) == "1.0KiB"
    assert format_download_status(0, total, None, None) == "0B/12.5MiB"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("short.mp4", "short.mp4"),
        ("123456789012345", "123456789012345"),
        ("1234567890123456", "1234567890..456"),
        ("样本 一.mp4", "样本 一.mp4"),
    ],
)
def test_shorten_label(name: str, expected: str) -> None:
    assert shorten_label(name) == expected
