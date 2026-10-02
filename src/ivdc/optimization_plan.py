"""只读任务规划，包含工作目录中暂时缺席的可恢复源文件。"""

from dataclasses import dataclass
from pathlib import Path

from ivdc.encode import EncodingConfig
from ivdc.errors import MediaError, SetupError
from ivdc.fs import STATE_DIR_NAME, IvdcLayout
from ivdc.naming import new_tag
from ivdc.plan import Segment, plan_segments
from ivdc.probe import MediaInfo
from ivdc.probe_runtime import Toolchain, probe_many
from ivdc.recovery import pending_backups
from ivdc.runner import ProcessRunner

DEFAULT_PATTERN = "*.mp4"
DEFAULT_CODEC = "x265"
DEFAULT_CUDA = "decode"
DEFAULT_WORKERS = 2
DEFAULT_SEGMENT_MINUTES = 0


@dataclass(frozen=True)
class OptimizeOptions:
    path: Path
    pattern: str = DEFAULT_PATTERN
    codec: str = DEFAULT_CODEC
    cuda: str = DEFAULT_CUDA
    crf: int | None = None
    workers: int = DEFAULT_WORKERS
    segment_minutes: int = DEFAULT_SEGMENT_MINUTES
    log_file: Path | None = None
    keep_backup: bool = True
    mask: bool = False
    debug: bool = False
    dry_run: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.workers <= 64 or not 0 <= self.segment_minutes <= 1440:
            raise SetupError("workers 必须在 1–64 内，segment_minutes 必须在 0–1440 内")
        if self.crf is not None and not 0 <= self.crf <= 63:
            raise SetupError("crf 必须在 0–63 内")


@dataclass(frozen=True)
class VideoTask:
    name: str
    path: Path
    label: str
    duration: float
    segments: tuple[Segment, ...] = ()
    error: str = ""
    media: MediaInfo | None = None


@dataclass(frozen=True)
class OptimizationPlan:
    tasks: tuple[VideoTask, ...]
    skipped: tuple[str, ...]
    recoveries: tuple[str, ...]
    temporary_files: tuple[Path, ...]


def collect_files(root: Path, pattern: str) -> list[Path]:
    return sorted(
        path
        for path in root.glob(pattern)
        if path.is_file()
        and not path.is_symlink()
        and STATE_DIR_NAME not in path.relative_to(root).parts
    )


def build_plan(
    options: OptimizeOptions,
    layout: IvdcLayout,
    config: EncodingConfig,
    toolchain: Toolchain,
    runner: ProcessRunner,
) -> OptimizationPlan:
    candidates = {
        path.relative_to(layout.root).as_posix(): path
        for path in collect_files(layout.root, options.pattern)
    }
    recoveries = pending_backups(layout)
    for name, backup in recoveries.items():
        if Path(name).full_match(options.pattern):
            candidates[name] = backup
    infos = probe_many(toolchain, list(candidates.values()), options.workers, runner)
    tasks: list[VideoTask] = []
    skipped: list[str] = []
    for name, source in sorted(candidates.items()):
        label = new_tag() if options.mask else name
        info = infos[source]
        if isinstance(info, MediaError):
            tasks.append(VideoTask(name, layout.root / name, label, 0, error=str(info)))
            continue
        if info.codec_name == config.codec_name:
            skipped.append(label)
            continue
        segments: tuple[Segment, ...] = ()
        error = ""
        if (
            info.stream_types.count("video") != 1
            or any(kind not in ("video", "audio", "subtitle") for kind in info.stream_types)
            or any("attached_pic" in stream.dispositions for stream in info.streams)
        ):
            error = "仅支持单视频流及音轨、字幕；额外视频、封面、数据或附件流无法安全保留"
        duration = info.duration
        if options.segment_minutes and not error:
            duration = info.video_duration or info.duration
            try:
                segments = tuple(
                    plan_segments(
                        duration,
                        options.segment_minutes * 60,
                        minimum_tail=1 / info.frame_rate if info.frame_rate > 0 else 0,
                    )
                )
            except ValueError as exc:
                error = f"无法规划分片: {exc}"
        tasks.append(VideoTask(name, layout.root / name, label, duration, segments, error, info))
    temporary = tuple(sorted(p for p in layout.tmp_dir.rglob("*") if p.is_file()))
    return OptimizationPlan(tuple(tasks), tuple(skipped), tuple(recoveries), temporary)
