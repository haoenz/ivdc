"""``ivdc opt`` 集成测试：真的调用 ffmpeg 压出文件来。

覆盖的行为规格（SPEC 第 3、5 节）：成片名字与位置不变、备份保留策略、
失败回滚、跳过已达标编码、分片拼接与断点续压、--what-if 一个文件都不动。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from ivdc.exitcodes import EXIT_ENV, EXIT_FAILURE, EXIT_OK
from ivdc.fs import IvdcLayout
from ivdc.optimize import OptOptions, collect_files, run_optimize

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

pytestmark = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="集成测试需要 ffmpeg 与 ffprobe"
)

SAMPLE = "样本 一.mp4"


def _make_video(path: Path, seconds: int) -> None:
    assert FFMPEG is not None
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=160x120:rate=10:duration={seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
    )


def _probe(path: Path, entries: str, stream: str | None = None) -> str:
    assert FFPROBE is not None
    argv = [FFPROBE, "-v", "error"]
    if stream:
        argv += ["-select_streams", stream]
    argv += ["-show_entries", entries, "-of", "default=nw=1:nk=1", str(path)]
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout.strip()


def _codec(path: Path) -> str:
    return _probe(path, "stream=codec_name", "v:0")


def _duration(path: Path) -> float:
    return float(_probe(path, "format=duration"))


def _options(root: Path, **kwargs: object) -> OptOptions:
    kwargs.setdefault("codec", "x265")
    kwargs.setdefault("cuda", "off")
    kwargs.setdefault("throttle", 1)
    return OptOptions(path=root, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def videos(tmp_path: Path) -> Path:
    root = tmp_path / "videos"
    root.mkdir()
    return root


@pytest.fixture
def sample(videos: Path) -> Path:
    path = videos / SAMPLE
    _make_video(path, 3)
    return path


# ---- 文件筛选 -------------------------------------------------------------


def test_collect_files_excludes_state_directory(videos: Path) -> None:
    (videos / "a.mp4").write_text("x", encoding="utf-8")
    layout = IvdcLayout(videos)
    layout.tmp_dir.mkdir(parents=True)
    (layout.tmp_dir / "a.mp4").write_text("x", encoding="utf-8")

    assert collect_files(videos, "*.mp4") == [videos / "a.mp4"]


def test_collect_files_sorts_by_name(videos: Path) -> None:
    for name in ("b.mp4", "a.mp4", "c.mp4"):
        (videos / name).write_text("x", encoding="utf-8")
    assert [item.name for item in collect_files(videos, "*.mp4")] == [
        "a.mp4",
        "b.mp4",
        "c.mp4",
    ]


def test_opt_on_empty_directory_reports_and_succeeds(
    console_pair: tuple[Console, StringIO], videos: Path
) -> None:
    console, stream = console_pair
    assert run_optimize(_options(videos), console) == EXIT_OK
    assert "未找到匹配的文件" in stream.getvalue()
    assert not (videos / ".ivdc").exists()


def test_opt_on_missing_directory_returns_env_error(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    console, _ = console_pair
    assert run_optimize(_options(tmp_path / "nope"), console) == EXIT_ENV


# ---- 整片压制 -------------------------------------------------------------


def test_whole_file_encode_keeps_name_location_and_backup(
    console_pair: tuple[Console, StringIO], sample: Path, videos: Path
) -> None:
    original_size = sample.stat().st_size
    console, stream = console_pair

    code = run_optimize(_options(videos), console)

    assert code == EXIT_OK
    assert _codec(sample) == "hevc"
    assert _duration(sample) == pytest.approx(3.0, abs=0.2)
    assert "✓" in stream.getvalue()

    layout = IvdcLayout(videos)
    assert (layout.backups_dir / SAMPLE).stat().st_size == original_size
    assert layout.state_dir.is_dir()
    assert not layout.tmp(SAMPLE).exists()
    # 工作目录里只有成片，没有中间产物
    assert sorted(item.name for item in videos.iterdir()) == [".ivdc", SAMPLE]


def test_log_and_manifest_are_written(
    console_pair: tuple[Console, StringIO], sample: Path, videos: Path
) -> None:
    console, _ = console_pair
    run_optimize(_options(videos), console)

    layout = IvdcLayout(videos)
    logs = list(layout.logs_dir.glob("*.log"))
    assert len(logs) == 1
    assert SAMPLE in logs[0].read_text(encoding="utf-8")

    from ivdc.store import load_manifest

    record = load_manifest(layout).get(SAMPLE)
    assert record is not None
    assert record.status == "ok"
    assert record.codec == "hevc"
    assert record.new_bytes > 0


def test_second_run_skips_files_already_in_target_codec(
    console_pair: tuple[Console, StringIO], sample: Path, videos: Path
) -> None:
    console, _ = console_pair
    run_optimize(_options(videos), console)

    second, stream = console_pair
    assert run_optimize(_options(videos), second) == EXIT_OK
    output = stream.getvalue()
    assert "跳过: 样本 一.mp4 (已是 hevc 编码)" in output
    assert "无需处理" in output


def test_no_keep_backup_removes_the_backup(
    console_pair: tuple[Console, StringIO], sample: Path, videos: Path
) -> None:
    console, _ = console_pair
    run_optimize(_options(videos, keep_backup=False), console)
    assert not IvdcLayout(videos).backup(SAMPLE).exists()


def test_mask_mode_hides_the_real_name_on_screen(
    console_pair: tuple[Console, StringIO], sample: Path, videos: Path
) -> None:
    console, stream = console_pair
    run_optimize(_options(videos, mask=True), console)

    output = stream.getvalue()
    assert SAMPLE not in output
    assert "跳过" not in output
    # 日志里始终记真实文件名，--mask 只影响屏幕
    log = next(IvdcLayout(videos).logs_dir.glob("*.log")).read_text(encoding="utf-8")
    assert SAMPLE in log


def test_what_if_changes_nothing(
    console_pair: tuple[Console, StringIO], sample: Path, videos: Path
) -> None:
    before = sample.stat().st_size
    console, stream = console_pair

    assert run_optimize(_options(videos, what_if=True), console) == EXIT_OK

    assert "将要处理 1 个文件" in stream.getvalue()
    assert sample.stat().st_size == before
    assert _codec(sample) == "h264"
    assert not (videos / ".ivdc").exists()


def test_corrupt_input_rolls_back_and_fails(
    console_pair: tuple[Console, StringIO], videos: Path
) -> None:
    broken = videos / "坏了.mp4"
    broken.write_text("not a video at all", encoding="utf-8")
    console, stream = console_pair

    code = run_optimize(_options(videos), console)

    assert code == EXIT_FAILURE
    assert broken.read_text(encoding="utf-8") == "not a video at all"
    assert "压制失败" in stream.getvalue()
    layout = IvdcLayout(videos)
    assert not layout.backup("坏了.mp4").exists()
    assert not layout.tmp("坏了.mp4").exists()


def test_orphaned_backup_is_restored_before_processing(
    console_pair: tuple[Console, StringIO], sample: Path, videos: Path
) -> None:
    """模拟上次被 Ctrl+C 打断：源文件在备份里，成片不在。"""
    layout = IvdcLayout(videos)
    layout.backups_dir.mkdir(parents=True)
    os.replace(sample, layout.backup(SAMPLE))
    console, stream = console_pair

    code = run_optimize(_options(videos), console)

    assert code == EXIT_OK
    assert "恢复上次中断的源文件" in stream.getvalue()
    assert sample.exists()
    assert _codec(sample) == "hevc"


def test_two_files_are_processed_independently(
    console_pair: tuple[Console, StringIO], videos: Path
) -> None:
    first = videos / "a.mp4"
    second = videos / "b.mp4"
    _make_video(first, 3)
    _make_video(second, 3)
    console, _ = console_pair

    assert run_optimize(_options(videos, throttle=2), console) == EXIT_OK
    assert _codec(first) == "hevc"
    assert _codec(second) == "hevc"


# ---- 分片压制 -------------------------------------------------------------


def test_segmented_encode_splits_and_concatenates(
    console_pair: tuple[Console, StringIO], videos: Path
) -> None:
    sample = videos / "长片.mp4"
    _make_video(sample, 70)
    console, stream = console_pair

    code = run_optimize(_options(videos, segment_minutes=1, throttle=2), console)

    assert code == EXIT_OK
    assert _codec(sample) == "hevc"
    assert _duration(sample) == pytest.approx(70.0, abs=0.5)
    layout = IvdcLayout(videos)
    assert not layout.seg_dir("长片.mp4").exists()
    assert not layout.tmp("长片.mp4").exists()
    assert "✓" in stream.getvalue()


def test_segmented_resume_reuses_valid_segments(
    console_pair: tuple[Console, StringIO], videos: Path
) -> None:
    """断点续压：已有的有效分片不该重压，只补缺的那片，然后拼接。"""
    sample = videos / "长片.mp4"
    _make_video(sample, 70)
    layout = IvdcLayout(videos)
    layout.backups_dir.mkdir(parents=True)
    layout.tmp_dir.mkdir(parents=True)
    layout.seg_dir("长片.mp4").mkdir(parents=True)
    os.replace(sample, layout.backup("长片.mp4"))

    assert FFMPEG is not None
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-ss",
            "0",
            "-i",
            str(layout.backup("长片.mp4")),
            "-t",
            "60",
            "-c:v",
            "libx265",
            "-tag:v",
            "hvc1",
            "-crf",
            "28",
            "-f",
            "mp4",
            str(layout.seg("长片.mp4", 0)),
            "-y",
        ],
        check=True,
    )

    console, stream = console_pair
    code = run_optimize(
        _options(videos, segment_minutes=1, debug=True, throttle=1), console
    )

    assert code == EXIT_OK
    output = stream.getvalue()
    assert "恢复上次中断的源文件" in output
    assert "(seg 2)" in output  # 只压了第 2 片
    assert "(seg 1)" not in output  # 第 1 片被复用
    assert _codec(sample) == "hevc"
    assert _duration(sample) == pytest.approx(70.0, abs=0.5)
    assert not layout.seg_dir("长片.mp4").exists()


def test_segmented_failure_keeps_segments_for_resume(
    console_pair: tuple[Console, StringIO], videos: Path
) -> None:
    """拼接失败时，分片与备份都要留着，供下次续压。"""
    sample = videos / "残片.mp4"
    _make_video(sample, 70)
    layout = IvdcLayout(videos)
    layout.backups_dir.mkdir(parents=True)
    layout.tmp_dir.mkdir(parents=True)
    layout.seg_dir("残片.mp4").mkdir(parents=True)

    assert FFMPEG is not None
    # 两片先压好，让这次运行直接进拼接阶段
    for index, (start, length) in enumerate(((0, 60), (60, 10))):
        subprocess.run(
            [
                FFMPEG, "-v", "error", "-ss", str(start), "-i", str(sample),
                "-t", str(length), "-c:v", "libx265", "-tag:v", "hvc1", "-crf", "28",
                "-f", "mp4", str(layout.seg("残片.mp4", index)), "-y",
            ],
            check=True,
        )
    # 把临时输出路径占成目录，迫使拼接失败
    layout.tmp("残片.mp4").mkdir()

    console, stream = console_pair
    code = run_optimize(_options(videos, segment_minutes=1, throttle=1), console)

    assert code == EXIT_FAILURE
    assert "保留分片以供下次恢复" in stream.getvalue()
    assert layout.seg("残片.mp4", 0).exists()
    assert layout.seg("残片.mp4", 1).exists()
    assert layout.backup("残片.mp4").exists()
    assert not sample.exists()
