"""``ivdc opt`` 集成测试：真的调用 ffmpeg 压出文件来。

覆盖的行为规格（SPEC 第 3、5 节）：成片名字与位置不变、备份保留策略、
失败回滚、跳过已达标编码、分片拼接与断点续压、--dry-run 一个文件都不动。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from ivdc.cli import execute_optimize
from ivdc.exitcodes import EXIT_ENV, EXIT_FAILURE, EXIT_OK
from ivdc.fs import IvdcLayout
from ivdc.optimization_plan import OptimizeOptions, collect_files

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="集成测试需要 ffmpeg 与 ffprobe"),
]

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


def _options(root: Path, **kwargs: object) -> OptimizeOptions:
    kwargs.setdefault("codec", "x265")
    kwargs.setdefault("cuda", "off")
    kwargs.setdefault("workers", 1)
    return OptimizeOptions(path=root, **kwargs)  # type: ignore[arg-type]


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
    assert execute_optimize(_options(videos), console) == EXIT_OK
    assert "未找到匹配的文件" in stream.getvalue()
    assert not (videos / ".ivdc").exists()


def test_opt_on_missing_directory_returns_env_error(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    console, _ = console_pair
    assert execute_optimize(_options(tmp_path / "nope"), console) == EXIT_ENV


# ---- 整片压制 -------------------------------------------------------------


def test_whole_file_encode_keeps_name_location_and_backup(
    console_pair: tuple[Console, StringIO], sample: Path, videos: Path
) -> None:
    original_size = sample.stat().st_size
    console, stream = console_pair

    code = execute_optimize(_options(videos), console)

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
    execute_optimize(_options(videos), console)

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
    execute_optimize(_options(videos), console)

    second, stream = console_pair
    assert execute_optimize(_options(videos), second) == EXIT_OK
    output = stream.getvalue()
    assert "跳过: 样本 一.mp4 (已是 hevc 编码)" in output
    assert "无需处理" in output


def test_no_keep_backup_removes_the_backup(
    console_pair: tuple[Console, StringIO], sample: Path, videos: Path
) -> None:
    console, _ = console_pair
    execute_optimize(_options(videos, keep_backup=False), console)
    assert not IvdcLayout(videos).backup(SAMPLE).exists()


def test_mask_mode_hides_the_real_name_on_screen(
    console_pair: tuple[Console, StringIO], sample: Path, videos: Path
) -> None:
    console, stream = console_pair
    execute_optimize(_options(videos, mask=True), console)

    output = stream.getvalue()
    assert SAMPLE not in output
    assert "跳过" not in output
    # 日志里始终记真实文件名，--mask 只影响屏幕
    log = next(IvdcLayout(videos).logs_dir.glob("*.log")).read_text(encoding="utf-8")
    assert SAMPLE in log


def test_dry_run_changes_nothing(
    console_pair: tuple[Console, StringIO], sample: Path, videos: Path
) -> None:
    before = sample.stat().st_size
    console, stream = console_pair

    assert execute_optimize(_options(videos, dry_run=True), console) == EXIT_OK

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

    code = execute_optimize(_options(videos), console)

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

    code = execute_optimize(_options(videos), console)

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

    assert execute_optimize(_options(videos, workers=2), console) == EXIT_OK
    assert _codec(first) == "hevc"
    assert _codec(second) == "hevc"


# ---- 分片压制 -------------------------------------------------------------


def test_segmented_encode_splits_and_concatenates(
    console_pair: tuple[Console, StringIO], videos: Path
) -> None:
    sample = videos / "长片.mp4"
    _make_video(sample, 70)
    console, stream = console_pair

    code = execute_optimize(_options(videos, segment_minutes=1, workers=2), console)

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
    code = execute_optimize(_options(videos, segment_minutes=1, debug=True, workers=1), console)

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
                FFMPEG,
                "-v",
                "error",
                "-ss",
                str(start),
                "-i",
                str(sample),
                "-t",
                str(length),
                "-c:v",
                "libx265",
                "-tag:v",
                "hvc1",
                "-crf",
                "28",
                "-f",
                "mp4",
                str(layout.seg("残片.mp4", index)),
                "-y",
            ],
            check=True,
        )
    # 把临时输出路径占成目录，迫使拼接失败
    layout.tmp("残片.mp4").mkdir()

    console, stream = console_pair
    code = execute_optimize(_options(videos, segment_minutes=1, workers=1), console)

    assert code == EXIT_FAILURE
    assert "保留分片以供下次恢复" in stream.getvalue()
    assert layout.seg("残片.mp4", 0).exists()
    assert layout.seg("残片.mp4", 1).exists()
    assert layout.backup("残片.mp4").exists()
    assert sample.exists()
    assert _codec(sample) == "h264"


@pytest.mark.parametrize("segment_minutes", [0, 1])
def test_dry_run_with_recovery_residue_performs_zero_writes(
    console_pair, sample, videos, tmp_path, monkeypatch, segment_minutes
):
    layout = IvdcLayout(videos)
    layout.backups_dir.mkdir(parents=True)
    os.replace(sample, layout.backup(SAMPLE))
    layout.seg_dir(SAMPLE).mkdir(parents=True)
    layout.seg(SAMPLE, 0).write_bytes(b"recoverable residue")
    layout.tmp_dir.mkdir()
    layout.tmp(SAMPLE).write_bytes(b"partial")
    layout.manifest_path.write_text('{"version":1,"entries":{}}', encoding="utf-8")
    config = tmp_path / "config.json"
    monkeypatch.setenv("IVDC_CONFIG", str(config))
    before = {
        p.relative_to(tmp_path): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    directories = {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_dir()}

    def forbidden(*args, **kwargs):
        pytest.fail("dry-run attempted filesystem mutation")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "unlink", forbidden)
    monkeypatch.setattr(os, "replace", forbidden)
    console, stream = console_pair
    assert (
        execute_optimize(
            _options(
                videos,
                dry_run=True,
                debug=True,
                segment_minutes=segment_minutes,
                log_file=tmp_path / "custom.log",
            ),
            console,
        )
        == EXIT_OK
    )
    after = {
        p.relative_to(tmp_path): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert after == before
    assert {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_dir()} == directories
    assert not sample.exists()
    assert not config.exists()
    assert "将恢复备份" in stream.getvalue()
    assert "将要处理 1 个文件" in stream.getvalue()
    assert "将清理临时文件 1 个" in stream.getvalue()


@pytest.mark.parametrize("segment_minutes", [0, 1])
def test_bad_item_is_failure_and_does_not_block_good_item(
    console_pair, sample, videos, segment_minutes
):
    broken = videos / "bad.mp4"
    broken.write_bytes(b"bad")
    console, output = console_pair
    assert (
        execute_optimize(_options(videos, workers=2, segment_minutes=segment_minutes), console)
        == EXIT_FAILURE
    )
    assert _codec(sample) == "hevc"
    assert broken.read_bytes() == b"bad"
    from ivdc.store import load_manifest

    manifest = load_manifest(IvdcLayout(videos))
    assert manifest.get("bad.mp4").status == "failed"
    assert manifest.get(SAMPLE).status == "ok"
    assert "成功 1 个，失败 1 个" in output.getvalue()


@pytest.mark.parametrize("segment_minutes", [0, 1])
def test_commit_failure_rolls_back_without_overwriting_prior_backup(
    console_pair, sample, videos, monkeypatch, segment_minutes
):
    original = sample.read_bytes()
    layout = IvdcLayout(videos)
    layout.backups_dir.mkdir(parents=True)
    layout.backup(SAMPLE).write_bytes(b"older protected backup")
    replace = os.replace

    def fail_commit(source, target):
        if Path(source) == layout.tmp(SAMPLE) and Path(target) == sample:
            raise PermissionError("commit denied")
        return replace(source, target)

    monkeypatch.setattr(os, "replace", fail_commit)
    console, output = console_pair
    assert (
        execute_optimize(_options(videos, segment_minutes=segment_minutes), console) == EXIT_FAILURE
    )
    assert sample.read_bytes() == original
    assert layout.backup(SAMPLE).read_bytes() == b"older protected backup"
    assert not layout.tmp(SAMPLE).exists()
    assert "commit denied" in output.getvalue()
    if segment_minutes:
        assert layout.seg(SAMPLE, 0).is_file()


def test_segment_preparation_failure_is_recorded(console_pair, sample, videos, monkeypatch):
    from ivdc.recovery import FileTransaction

    original = FileTransaction.prepare

    def fail_one(self):
        if self.name == SAMPLE:
            raise PermissionError("stage denied")
        original(self)

    second = videos / "second.mp4"
    _make_video(second, 1)
    monkeypatch.setattr(FileTransaction, "prepare", fail_one)
    console, output = console_pair
    assert execute_optimize(_options(videos, segment_minutes=1), console) == EXIT_FAILURE
    assert _codec(second) == "hevc"
    assert _codec(sample) == "h264"
    assert "stage denied" in output.getvalue()


def test_changed_segment_settings_preserve_old_resumable_data(console_pair, sample, videos):
    import json

    layout = IvdcLayout(videos)
    directory = layout.seg_dir(SAMPLE)
    directory.mkdir(parents=True)
    layout.seg(SAMPLE, 0).write_bytes(b"old recoverable segment")
    (directory / "resume.json").write_text(json.dumps({"codec": "vp9"}), encoding="utf-8")
    console, _ = console_pair
    assert execute_optimize(_options(videos, segment_minutes=1), console) == EXIT_OK
    archives = list(layout.segs_dir.glob("*.resume-*"))
    assert len(archives) == 1
    assert (archives[0] / "seg_000.mp4").read_bytes() == b"old recoverable segment"
    assert _codec(sample) == "hevc"


def test_nested_same_names_keep_output_and_backup_locations(console_pair, videos):
    for directory in (videos / "one", videos / "two"):
        directory.mkdir()
        _make_video(directory / "a.mp4", 1)
    console, _ = console_pair
    assert execute_optimize(_options(videos, pattern="**/*.mp4", workers=2), console) == EXIT_OK
    for directory in ("one", "two"):
        assert _codec(videos / directory / "a.mp4") == "hevc"
        assert _codec(IvdcLayout(videos).backup(f"{directory}/a.mp4")) == "h264"


def test_manifest_write_failure_is_round_failure_and_keeps_media(
    console_pair, sample, videos, monkeypatch
):
    def denied(*args):
        raise PermissionError("manifest denied")

    monkeypatch.setattr("ivdc.records.save_manifest", denied)
    console, output = console_pair
    assert execute_optimize(_options(videos), console) == EXIT_FAILURE
    assert _codec(sample) == "hevc"
    assert _codec(IvdcLayout(videos).backup(SAMPLE)) == "h264"
    assert "manifest denied" in output.getvalue()


def test_segment_worker_exception_cancels_round_and_keeps_source(
    console_pair, sample, videos, monkeypatch
):
    from ivdc.encoding import Encoder

    original = sample.read_bytes()

    def fail(*args):
        raise RuntimeError("unexpected worker failure")

    monkeypatch.setattr(Encoder, "segment", fail)
    console, output = console_pair
    assert execute_optimize(_options(videos, segment_minutes=1), console) == EXIT_FAILURE
    assert sample.read_bytes() == original
    assert IvdcLayout(videos).backup(SAMPLE).read_bytes() == original
    assert "unexpected worker failure" in output.getvalue()


def test_retry_after_commit_failure_reuses_completed_segments(
    console_pair, sample, videos, monkeypatch
):
    from ivdc.encoding import Encoder

    layout = IvdcLayout(videos)
    replace = os.replace

    def denied(source, target):
        if Path(source) == layout.tmp(SAMPLE) and Path(target) == sample:
            raise PermissionError("retry later")
        return replace(source, target)

    console, _ = console_pair
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", denied)
        assert execute_optimize(_options(videos, segment_minutes=1), console) == EXIT_FAILURE
    completed = layout.seg(SAMPLE, 0).read_bytes()

    def must_reuse(*args):
        pytest.fail("completed segment was encoded again")

    monkeypatch.setattr(Encoder, "encode", must_reuse)
    assert execute_optimize(_options(videos, segment_minutes=1), console) == EXIT_OK
    assert completed
    assert _codec(sample) == "hevc"
    assert not layout.seg_dir(SAMPLE).exists()


@pytest.mark.parametrize(("codec", "expected"), [("vp9", "vp9"), ("av1", "av1")])
def test_software_codecs_with_real_ffmpeg(console_pair, videos, codec, expected):
    path = videos / "tiny.mp4"
    _make_video(path, 1)
    console, output = console_pair
    assert execute_optimize(_options(videos, codec=codec), console) == EXIT_OK, output.getvalue()
    assert _codec(path) == expected
    assert _duration(path) == pytest.approx(1, abs=0.2)


def test_unusable_existing_segment_is_archived_not_overwritten(console_pair, sample, videos):
    layout = IvdcLayout(videos)
    layout.seg_dir(SAMPLE).mkdir(parents=True)
    _make_video(layout.seg(SAMPLE, 0), 1)
    original = layout.seg(SAMPLE, 0).read_bytes()
    console, _ = console_pair
    assert execute_optimize(_options(videos, segment_minutes=1), console) == EXIT_OK
    archived = list(layout.segs_dir.glob("*.retained-*/seg_000.mp4"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == original
    assert _codec(sample) == "hevc"


@pytest.mark.parametrize("segment_minutes", [0, 1])
def test_encoding_failure_isolated_from_other_valid_files(
    console_pair, sample, videos, monkeypatch, segment_minutes
):
    from ivdc.encoding import Encoder
    from ivdc.errors import MediaError

    good = videos / "good.mp4"
    _make_video(good, 1)
    original = sample.read_bytes()
    encode = Encoder.encode

    def fail_one(self, task, target, segment=None):
        if task.name == SAMPLE:
            target.write_bytes(b"partial")
            raise MediaError("encoder failed")
        return encode(self, task, target, segment)

    monkeypatch.setattr(Encoder, "encode", fail_one)
    console, output = console_pair
    assert (
        execute_optimize(_options(videos, workers=2, segment_minutes=segment_minutes), console)
        == EXIT_FAILURE
    )
    assert sample.read_bytes() == original
    assert _codec(good) == "hevc"
    layout = IvdcLayout(videos)
    assert layout.backup(SAMPLE).read_bytes() == original
    assert not layout.tmp(SAMPLE).exists()
    assert not layout.seg(SAMPLE, 0).exists()
    assert "成功 1 个，失败 1 个" in output.getvalue()
