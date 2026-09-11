"""分片规划：边界、浮点噪声与非法输入。"""

from __future__ import annotations

import pytest

from ivdc.plan import plan_segments


def test_exact_multiple() -> None:
    segments = plan_segments(180.0, 60.0)
    assert [s.start for s in segments] == [0.0, 60.0, 120.0]
    assert [s.duration for s in segments] == [60.0, 60.0, 60.0]


def test_tail_segment_takes_remainder() -> None:
    segments = plan_segments(70.0, 60.0)
    assert [(s.start, s.duration) for s in segments] == [(0.0, 60.0), (60.0, 10.0)]


def test_shorter_than_one_segment() -> None:
    segments = plan_segments(12.5, 60.0)
    assert len(segments) == 1
    assert segments[0].start == 0.0
    assert segments[0].duration == 12.5


def test_float_noise_does_not_create_tiny_tail() -> None:
    duration = 180.0 + 1e-9
    segments = plan_segments(duration, 60.0)
    assert len(segments) == 3
    assert sum(s.duration for s in segments) == pytest.approx(duration)


def test_indices_are_contiguous_from_zero() -> None:
    segments = plan_segments(1000.0, 300.0)
    assert [s.index for s in segments] == list(range(len(segments)))


def test_durations_cover_the_whole_file() -> None:
    duration = 1234.567
    segments = plan_segments(duration, 300.0)
    assert sum(s.duration for s in segments) == pytest.approx(duration)
    assert segments[-1].start + segments[-1].duration == pytest.approx(duration)


@pytest.mark.parametrize(
    ("duration", "segment_seconds"),
    [(0.0, 60.0), (-1.0, 60.0), (60.0, 0.0), (60.0, -5.0)],
)
def test_invalid_input_raises(duration: float, segment_seconds: float) -> None:
    with pytest.raises(ValueError):
        plan_segments(duration, segment_seconds)
