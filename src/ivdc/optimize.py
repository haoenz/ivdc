"""``ivdc opt``：用 ffmpeg 批量压制视频（x265 / AV1 / VP9，可选 CUDA）。

并行用 ``ThreadPoolExecutor`` + 一把 ``threading.Lock``：ffmpeg 跑在子进程里，GIL 不是
瓶颈，所以不需要进程池。整片与分片两条路径共用同一个 :meth:`_Optimizer._encode` 与
:meth:`_Optimizer._on_ffmpeg_line`，进度解析只有一份实现。

磁盘上有两条不变式（细节见 SPEC.md §2）：

- **成片的名字与位置和输入一致**——新内容先落 ``.ivdc/tmp``，成功后再改名回原位；
- **失败的文件保留备份与分片**，下次运行从断点续压。
"""

from __future__ import annotations

import math
import os
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from rich.console import Console
from rich.progress import TaskID

from ivdc.console import (
    DEBUG_PREFIX,
    STYLE_DEBUG,
    STYLE_DONE,
    STYLE_ERROR,
    STYLE_FAIL,
    STYLE_INFO,
    STYLE_NOTICE,
    STYLE_PLAIN,
    STYLE_SKIP,
    STYLE_WARN,
    emit,
)
from ivdc.encode import EncodeConfig, build_ffmpeg_args, resolve_encode_config
from ivdc.exitcodes import EXIT_ENV, EXIT_FAILURE, EXIT_OK
from ivdc.formatting import (
    format_done_line,
    format_fail_line,
    format_log_line,
    progress_percent,
    shorten_label,
)
from ivdc.fs import (
    CONCAT_LIST_NAME,
    STATE_DIR_NAME,
    STATUS_FAILED,
    STATUS_OK,
    FileRecord,
    IvdcLayout,
    Manifest,
    concat_lines,
)
from ivdc.naming import new_tag
from ivdc.parse import (
    OUT_TIME_KEY,
    is_progress_line,
    parse_out_time,
    parse_progress_line,
    strip_fraction,
)
from ivdc.plan import Segment, plan_segments
from ivdc.probe import MediaInfo
from ivdc.probe_runtime import (
    MissingToolsError,
    Toolchain,
    find_toolchain,
    is_media_file,
    probe_environment,
    probe_many,
)
from ivdc.progress import ProgressDisplay
from ivdc.recovery import recover_backups
from ivdc.runner import CommandResult, capture_command, stream_command, terminate_all
from ivdc.store import (
    append_line,
    clear_tmp,
    load_manifest,
    now_iso,
    remove_tree,
    resolve_log_path,
    save_manifest,
    write_lines,
)

_SEGMENT_LABEL_LIMIT = 16

DEFAULT_PATTERN = "*.mp4"
DEFAULT_CODEC = "x265"
DEFAULT_CUDA = "decode"
DEFAULT_THROTTLE = 2
DEFAULT_SEGMENT_MINUTES = 0

__all__ = [
    "DEFAULT_CODEC",
    "DEFAULT_CUDA",
    "DEFAULT_PATTERN",
    "DEFAULT_SEGMENT_MINUTES",
    "DEFAULT_THROTTLE",
    "OptOptions",
    "collect_files",
    "run_optimize",
]


class _EncodeError(RuntimeError):
    """一个文件的压制没能完成（可恢复，不中止整轮）。"""


@dataclass(frozen=True)
class OptOptions:
    """``ivdc opt`` 的全部选项。"""

    path: Path
    pattern: str = DEFAULT_PATTERN
    codec: str = DEFAULT_CODEC
    cuda: str = DEFAULT_CUDA
    crf: int | None = None
    throttle: int = DEFAULT_THROTTLE
    segment_minutes: int = DEFAULT_SEGMENT_MINUTES
    log_file: Path | None = None
    keep_backup: bool = True
    mask: bool = False
    debug: bool = False
    what_if: bool = False

    @property
    def segmented(self) -> bool:
        return self.segment_minutes > 0


@dataclass
class _Entry:
    """一个待压制的文件。"""

    name: str
    path: Path
    index: int
    duration: float
    codec: str
    label: str


@dataclass
class _FfmpegProgress:
    """单条 ffmpeg 调用的进度去重状态（每个文件、每个分片各一份）。"""

    last_percent: int = -1
    status: str = ""


def collect_files(root: Path, pattern: str) -> list[Path]:
    """按过滤规则取待处理文件。

    显式排掉状态目录里的东西：``--filter '**/*.mp4'`` 这种写法会连 ``.ivdc/tmp``
    里的中间产物一起捞出来，那正是把中间产物收进 ``.ivdc/`` 想避免的事。
    """
    found = [
        path
        for path in root.glob(pattern)
        if path.is_file() and STATE_DIR_NAME not in path.parts
    ]
    return sorted(found, key=lambda item: item.name)


def run_optimize(options: OptOptions, console: Console) -> int:
    """执行一次批量压制，返回进程退出码。"""
    root = options.path.expanduser()
    if not root.is_dir():
        emit(console, f"路径不存在或不是目录: {root}", STYLE_ERROR)
        return EXIT_ENV

    try:
        toolchain = find_toolchain()
    except MissingToolsError as exc:
        emit(console, str(exc), STYLE_ERROR)
        return EXIT_ENV

    environment = probe_environment(toolchain)
    config = resolve_encode_config(
        options.codec,
        options.cuda,
        encoders_output=environment.encoders,
        hwaccels_output=environment.hwaccels,
        gpu_available=environment.gpu_available,
        crf=options.crf,
    )
    if config.error:
        emit(console, config.error, STYLE_ERROR)
        return EXIT_ENV
    for notice in config.notices:
        emit(console, notice, STYLE_NOTICE)

    layout = IvdcLayout(root)
    log_path = resolve_log_path(layout, options.log_file)
    manifest = load_manifest(layout)

    recovery = recover_backups(layout, keep_segments=options.segmented)
    for name in recovery.restored:
        emit(console, f"恢复上次中断的源文件: {name}", STYLE_WARN)
    for failure in recovery.failures:
        emit(console, failure, STYLE_WARN)
    cleared = clear_tmp(layout)
    if cleared.removed and options.debug:
        emit(console, f"{DEBUG_PREFIX}清理临时文件 {cleared.removed} 个", STYLE_DEBUG)
    if cleared.failed:
        emit(
            console,
            f"{cleared.failed} 个临时文件未能删除，仍留在 {layout.tmp_dir}。",
            STYLE_WARN,
        )

    candidates = collect_files(root, options.pattern)
    if not candidates:
        emit(console, f"未找到匹配的文件: {options.pattern}", STYLE_WARN)
        return EXIT_OK

    emit(console, "预处理: 并行计算所有视频的总时长以显示全局进度...", STYLE_INFO)
    infos = probe_many(toolchain, candidates, options.throttle)
    if options.debug:
        _emit_probe_debug(console, candidates, infos)

    tags = {path.name: new_tag() for path in candidates} if options.mask else {}
    entries = _build_entries(console, candidates, infos, config, tags)
    if not entries:
        emit(console, f"所有视频已是目标编码({config.codec_name})，无需处理。", STYLE_INFO)
        return EXIT_OK

    if options.what_if:
        _describe_plan(console, options, config, entries)
        return EXIT_OK

    display = ProgressDisplay(console, title_prefix="Optimizing")
    optimizer = _Optimizer(
        options=options,
        config=config,
        toolchain=toolchain,
        layout=layout,
        log_path=log_path,
        manifest=manifest,
        console=console,
        display=display,
    )
    if options.debug:
        display.log(f"{DEBUG_PREFIX}日志: {log_path}", STYLE_DEBUG)
    try:
        with display:
            records = optimizer.run(entries)
    except KeyboardInterrupt:
        terminated = terminate_all()
        display.log(
            f"已中断（终止 {terminated} 个 ffmpeg 进程）。未完成的文件保留备份与分片，"
            "下次运行会从断点继续。",
            STYLE_WARN,
        )
        return EXIT_FAILURE

    save_manifest(layout, manifest)
    succeeded = sum(1 for record in records if record.status == STATUS_OK)
    failed = len(records) - succeeded
    style = STYLE_WARN if failed else STYLE_DONE
    emit(console, f"完成: 成功 {succeeded} 个，失败 {failed} 个。", style)
    return EXIT_FAILURE if failed else EXIT_OK


def _describe_plan(
    console: Console, options: OptOptions, config: EncodeConfig, entries: list[_Entry]
) -> None:
    """``--what-if``：只打印将要做什么，一个文件都不动。"""
    mode = f"分片 {options.segment_minutes} 分钟" if options.segmented else "整片编码"
    emit(
        console,
        f"将要处理 {len(entries)} 个文件（{mode}，编码 {config.codec_name}）:",
        STYLE_INFO,
    )
    for entry in entries:
        detail = f"{entry.duration:.1f}s"
        if options.segmented and entry.duration > 0:
            count = len(plan_segments(entry.duration, options.segment_minutes * 60))
            detail += f"，{count} 片"
        emit(console, f"  {entry.label}  {detail}", STYLE_PLAIN)
    emit(console, "（--what-if 不做任何修改）", STYLE_SKIP)


def _emit_probe_debug(
    console: Console, candidates: list[Path], infos: dict[Path, MediaInfo]
) -> None:
    """``--debug`` 下逐条打印探测结果。"""
    for path in candidates:
        info = infos[path]
        emit(
            console,
            f"{DEBUG_PREFIX}探测: {path.name} -> {info.duration:.3f} 秒, "
            f"编码={info.codec_name or '未知'}",
            STYLE_DEBUG,
        )


def _build_entries(
    console: Console,
    candidates: list[Path],
    infos: dict[Path, MediaInfo],
    config: EncodeConfig,
    tags: dict[str, str],
) -> list[_Entry]:
    """把候选文件筛成待压制条目；已是目标编码的跳过并打印原因。

    ``tags`` 是 ``--mask`` 下「真实文件名 → 随机串」的映射，为空时用真实文件名。
    """
    entries: list[_Entry] = []
    for index, path in enumerate(candidates, start=1):
        info = infos[path]
        label = tags.get(path.name, path.name)
        if info.codec_name == config.codec_name:
            emit(console, f"跳过: {label} (已是 {config.codec_name} 编码)", STYLE_SKIP)
            continue
        entries.append(
            _Entry(
                name=path.name,
                path=path,
                index=index,
                duration=info.duration,
                codec=info.codec_name,
                label=label,
            )
        )
    return entries


class _Optimizer:
    """一次压制运行的调度与进度状态。

    ``_seconds`` 记的是「已经处理掉的秒数」：整片模式按文件为键、分片模式按分片为键，
    所以跨文件、跨分片的全局进度都准（已处理秒数 ÷ 总秒数）。清单与日志的持久化
    交给 :class:`_Recorder`，不走这里的锁。
    """

    def __init__(
        self,
        *,
        options: OptOptions,
        config: EncodeConfig,
        toolchain: Toolchain,
        layout: IvdcLayout,
        log_path: Path,
        manifest: Manifest,
        console: Console,
        display: ProgressDisplay,
    ) -> None:
        self.options = options
        self.config = config
        self.toolchain = toolchain
        self.layout = layout
        self.console = console
        self.display = display
        self._recorder = _Recorder(
            layout=layout,
            log_path=log_path,
            manifest=manifest,
            codec_name=config.codec_name,
            display=display,
        )
        self._lock = threading.Lock()
        self._seconds: dict[str, float] = {}
        self._completed = 0
        self._total_files = 0
        self._total_seconds = 0.0
        self._file_started: dict[str, float] = {}

    # ---- 调度 -------------------------------------------------------------

    def run(self, entries: list[_Entry]) -> list[FileRecord]:
        self._total_seconds = math.fsum(entry.duration for entry in entries)
        self._total_files = len(entries)
        self.display.set_global(0, completed=0, total=self._total_files)
        if self.options.segmented:
            return self._run_segmented(entries)
        return self._run_whole(entries)

    def _run_whole(self, entries: list[_Entry]) -> list[FileRecord]:
        records: list[FileRecord] = []
        pool = ThreadPoolExecutor(max_workers=max(1, self.options.throttle))
        futures: list[Future[FileRecord]] = [
            pool.submit(self._process_file, entry) for entry in entries
        ]
        try:
            for future in as_completed(futures):
                records.append(future.result())
        except KeyboardInterrupt:
            # 工作线程卡在管道读取上收不到 KeyboardInterrupt，必须由主线程杀掉子进程
            terminate_all()
            for future in futures:
                future.cancel()
            raise
        finally:
            pool.shutdown(wait=True)
        return records

    def _run_segmented(self, entries: list[_Entry]) -> list[FileRecord]:
        segment_seconds = self.options.segment_minutes * 60
        jobs: list[tuple[_Entry, Segment, int]] = []
        prepared: list[_Entry] = []

        # Phase 1（串行）：移走源文件、建分片目录、生成分片任务
        for entry in entries:
            if entry.duration <= 0:
                emit(self.console, f"无法获取时长，分片模式跳过: {entry.label}", STYLE_WARN)
                continue
            try:
                segments = plan_segments(entry.duration, segment_seconds)
            except ValueError as exc:
                emit(self.console, f"无法规划分片，跳过 {entry.label}: {exc}", STYLE_WARN)
                continue
            try:
                self.layout.seg_dir(entry.name).mkdir(parents=True, exist_ok=True)
                self._stage_source(entry)
            except OSError as exc:
                emit(self.console, f"准备分片失败，跳过 {entry.label}: {exc}", STYLE_WARN)
                continue
            self._file_started[entry.name] = time.monotonic()
            prepared.append(entry)
            jobs.extend((entry, segment, len(segments)) for segment in segments)

        self._total_files = len(prepared)
        self.display.set_global(0, completed=0, total=len(prepared))

        if not jobs:
            emit(self.console, "无可处理分片（所有文件已完成或无法获取时长）。", STYLE_INFO)
            return []

        counter = {entry.name: 0 for entry in prepared}
        records: dict[str, FileRecord] = {}
        pool = ThreadPoolExecutor(max_workers=max(1, self.options.throttle))
        futures = [
            pool.submit(self._process_segment, entry, segment, count, counter)
            for entry, segment, count in jobs
        ]
        try:
            for future in as_completed(futures):
                record = future.result()
                if record is not None:
                    records[record.name] = record
        except KeyboardInterrupt:
            terminate_all()
            for future in futures:
                future.cancel()
            raise
        finally:
            pool.shutdown(wait=True)

        for entry in prepared:
            if entry.name not in records:
                records[entry.name] = self._record_failure(
                    entry, self._elapsed(entry.name), "未能完成拼接（部分分片缺失）"
                )
        return list(records.values())

    # ---- 磁盘暂存 ---------------------------------------------------------

    def _stage_source(self, entry: _Entry) -> Path:
        """把源文件移进备份目录，并备好备份与临时输出目录；返回备份路径。

        移走源文件是「成片名字与位置不变」这条不变式的前提：新内容先落到
        ``.ivdc/tmp``，成功后再改名回原位。中途被打断时备份还在，下次启动可还原。
        调用方负责捕获 ``OSError``。
        """
        backup = self.layout.backup(entry.name)
        backup.parent.mkdir(parents=True, exist_ok=True)
        self.layout.tmp_dir.mkdir(parents=True, exist_ok=True)
        os.replace(entry.path, backup)
        return backup

    # ---- 整片编码 ---------------------------------------------------------

    def _process_file(self, entry: _Entry) -> FileRecord:
        tmp = self.layout.tmp(entry.name)
        task_id = self.display.add_task(
            shorten_label(entry.label),
            total=100 if entry.duration > 0 else None,
        )
        started = time.monotonic()
        try:
            try:
                backup = self._stage_source(entry)
            except OSError as exc:
                return self._record_failure(entry, 0.0, f"重命名源文件失败: {exc}")

            try:
                self._encode(entry, backup, tmp, task_id, f"file:{entry.index}")
                new_bytes = tmp.stat().st_size
                os.replace(tmp, entry.path)
            except _EncodeError as exc:
                self._restore(backup, entry.path)
                self._discard(tmp)
                return self._record_failure(entry, time.monotonic() - started, str(exc))
            except BaseException:
                self._restore(backup, entry.path)
                self._discard(tmp)
                raise

            old_bytes = backup.stat().st_size if backup.exists() else 0
            if not self.options.keep_backup:
                self._discard(backup)
            return self._record_success(
                entry, time.monotonic() - started, old_bytes, new_bytes
            )
        finally:
            self.display.remove_task(task_id)

    # ---- 分片编码 ---------------------------------------------------------

    def _process_segment(
        self,
        entry: _Entry,
        segment: Segment,
        segment_count: int,
        counter: dict[str, int],
    ) -> FileRecord | None:
        segment_path = self.layout.seg(entry.name, segment.index)
        job_key = f"seg:{entry.index}:{segment.index}"
        task_id = self.display.add_task(
            f"{shorten_label(entry.label, _SEGMENT_LABEL_LIMIT)} "
            f"[seg {segment.index + 1}/{segment_count}]",
            total=100,
        )
        try:
            # 断点续压：分片已存在且 ffprobe 能解析就复用，直接跳过
            if not is_media_file(self.toolchain, segment_path):
                # 失败的（半截）分片先删掉再重压，留着只会被误当成有效分片
                self._discard(segment_path)
                self._encode(
                    entry,
                    self.layout.backup(entry.name),
                    segment_path,
                    task_id,
                    job_key,
                    segment,
                )
        except _EncodeError as exc:
            self._discard(segment_path)
            self.display.log(
                f"分片压制失败 ({entry.label} seg {segment.index + 1}): {exc}", STYLE_FAIL
            )
        except Exception as exc:  # noqa: BLE001 - 单个分片异常不该拖垮整轮
            self._discard(segment_path)
            self.display.log(
                f"分片压制异常 ({entry.label} seg {segment.index + 1}): {exc}", STYLE_WARN
            )
            if self.options.debug:
                self.display.log(traceback.format_exc(), STYLE_DEBUG)
        finally:
            self.display.remove_task(task_id)

        self._mark_progress(key=job_key, seconds=segment.duration)

        with self._lock:
            counter[entry.name] = counter.get(entry.name, 0) + 1
            finished = counter[entry.name]
        if finished < segment_count:
            return None
        # 最后一个分片完成的那个线程负责拼接，省掉一个调度阶段
        return self._finish_segmented_file(entry, segment_count)

    def _finish_segmented_file(self, entry: _Entry, segment_count: int) -> FileRecord:
        elapsed = self._elapsed(entry.name)
        backup = self.layout.backup(entry.name)
        tmp = self.layout.tmp(entry.name)
        new_bytes = 0
        old_bytes = 0
        try:
            for index in range(segment_count):
                if not self.layout.seg(entry.name, index).exists():
                    raise _EncodeError("部分分片缺失")
            seg_dir = self.layout.seg_dir(entry.name)
            list_path = seg_dir / CONCAT_LIST_NAME
            write_lines(list_path, concat_lines(segment_count))
            argv = [
                self.toolchain.ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c",
                "copy",
                "-f",
                "mp4",
                str(tmp),
                "-y",
            ]
            if self.options.debug:
                self.display.log(f"{DEBUG_PREFIX}{' '.join(argv)}", STYLE_DEBUG)
            result = capture_command(argv)
            if not result.ok or not tmp.exists():
                raise _EncodeError(_failure_reason(result) or "拼接未生成目标文件")
            new_bytes = tmp.stat().st_size
            old_bytes = backup.stat().st_size if backup.exists() else 0
            os.replace(tmp, entry.path)
            if not remove_tree(seg_dir):
                # 成片已经就位，分片只是残留；下次运行会按「已是目标编码」跳过这个文件
                self.display.log(
                    f"分片目录未能删除（{seg_dir}），可稍后用 `ivdc clean --segs` 清理。",
                    STYLE_WARN,
                )
            if not self.options.keep_backup:
                self._discard(backup)
        except (_EncodeError, OSError) as exc:
            self._discard(tmp)
            return self._record_failure(entry, elapsed, f"{exc}，保留分片以供下次恢复")

        return self._record_success(entry, elapsed, old_bytes, new_bytes)

    # ---- 共用编码 ---------------------------------------------------------

    def _encode(
        self,
        entry: _Entry,
        source: Path,
        target: Path,
        task_id: TaskID,
        progress_key: str,
        segment: Segment | None = None,
    ) -> None:
        """跑一次 ffmpeg；失败抛 :class:`_EncodeError`。"""
        duration = entry.duration if segment is None else segment.duration
        argv = [
            self.toolchain.ffmpeg,
            "-hide_banner",
            "-nostdin",
            *build_ffmpeg_args(
                self.config,
                str(source),
                str(target),
                start=None if segment is None else segment.start,
                duration=duration,
            ),
        ]
        if self.options.debug:
            suffix = "" if segment is None else f" (seg {segment.index + 1})"
            self.display.log(f"{DEBUG_PREFIX}{' '.join(argv)}{suffix}", STYLE_DEBUG)

        progress = _FfmpegProgress()
        result = stream_command(
            argv,
            on_output=lambda line: self._on_ffmpeg_line(
                line, task_id, progress_key, progress, duration
            ),
        )
        if not result.ok:
            raise _EncodeError(_failure_reason(result))
        if not target.exists():
            raise _EncodeError("ffmpeg 未生成目标文件")

    def _on_ffmpeg_line(
        self,
        line: str,
        task_id: TaskID,
        progress_key: str,
        progress: _FfmpegProgress,
        duration: float,
    ) -> None:
        """处理 ffmpeg 的一行输出：``out_time`` 刷新进度条，其余（``-v error`` 下没有别的）丢弃。"""
        parsed = parse_progress_line(line)
        if parsed is None:
            return
        key, value = parsed
        if key != OUT_TIME_KEY:
            return
        seconds = parse_out_time(value)
        if seconds is None:
            return
        clock = strip_fraction(value)
        if duration <= 0:
            # 时长未知，没有百分比可算，只能按时钟文本去重
            if clock != progress.status:
                progress.status = clock
                self.display.update(task_id, status=clock)
            return
        seconds = min(seconds, duration)
        percent = progress_percent(seconds, duration)
        if percent == progress.last_percent:
            return
        progress.last_percent = percent
        self.display.update(task_id, completed=percent, status=f"{clock} ({percent}%)")
        self._mark_progress(key=progress_key, seconds=seconds)

    # ---- 进度 -------------------------------------------------------------

    def _mark_progress(
        self, *, key: str | None = None, seconds: float = 0.0, complete: bool = False
    ) -> None:
        """更新已处理秒数 / 完成计数，然后刷新整体进度。

        整片模式每个文件一个键、分片模式每个分片一个键，所以两种模式下
        「已处理秒数 ÷ 总秒数」都是对的。
        """
        with self._lock:
            if key is not None:
                self._seconds[key] = max(self._seconds.get(key, 0.0), seconds)
            if complete:
                self._completed += 1
            current = math.fsum(self._seconds.values())
            percent = progress_percent(current, self._total_seconds)
            completed = self._completed
            total = self._total_files
        self.display.set_global(percent, completed=completed, total=total)

    def _mark_file_done(self, entry: _Entry) -> None:
        """一个文件处理结束：整片模式补上它的完整时长，然后完成计数 +1。

        分片模式不能再补时长——每个分片已经各自贡献过了，再补就重复计算。
        """
        if self.options.segmented:
            self._mark_progress(complete=True)
        else:
            self._mark_progress(
                key=f"file:{entry.index}", seconds=entry.duration, complete=True
            )

    # ---- 结果记账 ---------------------------------------------------------

    def _record_success(
        self, entry: _Entry, elapsed: float, old_bytes: int, new_bytes: int
    ) -> FileRecord:
        record = self._recorder.success(entry, elapsed, old_bytes, new_bytes)
        self._mark_file_done(entry)
        return record

    def _record_failure(self, entry: _Entry, elapsed: float, message: str) -> FileRecord:
        record = self._recorder.failure(entry, elapsed, message)
        self._mark_file_done(entry)
        return record

    # ---- 小工具 -----------------------------------------------------------

    def _elapsed(self, name: str) -> float:
        started = self._file_started.get(name)
        return 0.0 if started is None else time.monotonic() - started

    @staticmethod
    def _discard(path: Path) -> None:
        """尽力删掉一个中间产物。

        删除失败不致命，所以这里刻意保持静默：临时文件残留会被下次启动的
        ``clear_tmp`` 收走，半截分片残留会在重压时被 ffmpeg 覆盖。
        """
        with suppress(OSError):
            path.unlink()

    def _restore(self, backup: Path, source: Path) -> None:
        """把备份移回原位；失败必须提示——这一步失败意味着工作目录里那个文件是缺的。

        下次启动的 ``recover_backups`` 还会再试一次，所以只提示不中止。
        """
        if not (backup.exists() and not source.exists()):
            return
        try:
            os.replace(backup, source)
        except OSError as exc:
            self.display.log(
                f"还原备份失败 ({backup.name}): {exc}；源文件暂留在 {backup.parent}，"
                "下次运行会自动重试。",
                STYLE_WARN,
            )


class _Recorder:
    """结果记账：写 ``manifest.json``、写按天日志、并在屏幕上回显一行。

    单独成类是因为「记账」与「调度 / 编码」没有共享状态：它只需要布局、日志路径、
    清单与显示层，因此可以脱离线程池与 ffmpeg 单独构造和测试。
    """

    def __init__(
        self,
        *,
        layout: IvdcLayout,
        log_path: Path,
        manifest: Manifest,
        codec_name: str,
        display: ProgressDisplay,
    ) -> None:
        self._layout = layout
        self._log_path = log_path
        self._manifest = manifest
        self._codec_name = codec_name
        self._display = display
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()

    def success(
        self, entry: _Entry, elapsed: float, old_bytes: int, new_bytes: int
    ) -> FileRecord:
        """记录一个文件压制成功。"""
        minutes = elapsed / 60
        record = FileRecord(
            name=entry.name,
            status=STATUS_OK,
            codec=self._codec_name,
            old_bytes=old_bytes,
            new_bytes=new_bytes,
            seconds=elapsed,
            finished_at=now_iso(),
        )
        self._write_log(format_log_line(date.today(), entry.name, old_bytes, new_bytes, minutes))
        self._display.log(
            format_done_line(entry.label, old_bytes, new_bytes, minutes), STYLE_DONE
        )
        self._commit(record)
        return record

    def failure(self, entry: _Entry, elapsed: float, message: str) -> FileRecord:
        """记录一个文件压制失败。"""
        record = FileRecord(
            name=entry.name,
            status=STATUS_FAILED,
            codec=self._codec_name,
            seconds=elapsed,
            finished_at=now_iso(),
            message=message,
        )
        self._write_log(format_fail_line(date.today(), entry.name, message))
        self._display.log(f"压制失败 ({entry.label}): {message}", STYLE_FAIL)
        self._commit(record)
        return record

    def _commit(self, record: FileRecord) -> None:
        with self._lock:
            self._manifest.record(record)
            save_manifest(self._layout, self._manifest)

    def _write_log(self, line: str) -> None:
        with self._log_lock:
            try:
                append_line(self._log_path, line)
            except OSError as exc:
                self._display.log(f"日志写入失败: {exc}", STYLE_WARN)


def _failure_reason(result: CommandResult) -> str:
    """从尾部输出里挑出真正有用的报错文本。

    ffmpeg 的进度行与报错文本混在同一个管道里（stderr 被合并进 stdout），
    所以先按 ``key=value`` 的形状把进度行择掉，剩下的才是错因。
    """
    messages = [
        line.strip()
        for line in result.lines
        if line.strip() and not is_progress_line(line)
    ]
    if not messages:
        messages = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    if messages:
        return " ".join(messages[-2:])
    return f"ffmpeg 退出码 {result.returncode}"
