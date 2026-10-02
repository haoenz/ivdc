"""压制运行的协调：规划、任务调度、文件事务与结果记录。"""

import logging
import time
from collections.abc import Callable
from concurrent.futures import as_completed
from dataclasses import dataclass, replace
from itertools import chain

from ivdc.config import load_config
from ivdc.encode import resolve_encode_config
from ivdc.encoding import Encoder, ProgressCallback, SegmentResult
from ivdc.errors import MediaError, SetupError
from ivdc.fs import STATUS_FAILED, STATUS_OK, FileRecord, IvdcLayout
from ivdc.optimization_plan import (
    OptimizationPlan,
    OptimizeOptions,
    VideoTask,
    build_plan,
    collect_files,
)
from ivdc.probe_runtime import find_toolchain, probe_environment
from ivdc.records import ResultRecorder
from ivdc.recovery import FileTransaction, pending_backups, recover_backups
from ivdc.runner import CommandError, ProcessRunner, worker_pool
from ivdc.segment_state import SegmentContext, prepare_segments
from ivdc.store import (
    clear_tmp,
    load_manifest,
    now_iso,
    remove_tree,
    resolve_log_path,
    validate_log_path,
    validate_state_directory,
)


@dataclass(frozen=True)
class OptimizationEvents:
    planned: Callable[[OptimizationPlan, str], None] = lambda plan, codec: None
    progress: ProgressCallback = lambda key, seconds: None
    finished: Callable[[FileRecord], None] = lambda record: None
    notice: Callable[[str], None] = lambda message: None


@dataclass(frozen=True)
class OptimizationResult:
    plan: OptimizationPlan
    records: tuple[FileRecord, ...] = ()
    failures: tuple[str, ...] = ()

    @property
    def failed(self) -> bool:
        return bool(self.failures) or any(record.status == STATUS_FAILED for record in self.records)


def _record(
    task: VideoTask, codec: str, started: float, *, error: str = "", sizes: tuple[int, int] = (0, 0)
) -> FileRecord:
    return FileRecord(
        task.name,
        STATUS_FAILED if error else STATUS_OK,
        codec,
        *sizes,
        time.monotonic() - started,
        now_iso(),
        error,
    )


def _whole(task: VideoTask, encoder: Encoder, transaction: FileTransaction) -> FileRecord:
    started = time.monotonic()
    try:
        encoder.runner.check_cancelled()
        transaction.prepare()
        encoder.encode(task, transaction.output)
        encoder.runner.check_cancelled()
        sizes = transaction.commit()
        return _record(task, encoder.config.codec_name, started, sizes=sizes)
    except (OSError, MediaError, CommandError) as exc:
        return _record(task, encoder.config.codec_name, started, error=str(exc))
    finally:
        transaction.rollback()


def _execute(
    plan: OptimizationPlan,
    options: OptimizeOptions,
    encoder: Encoder,
    recorder: ResultRecorder,
    events: OptimizationEvents,
) -> tuple[FileRecord, ...]:
    records: list[FileRecord] = []

    def finish(record: FileRecord) -> None:
        recorder.record(record)
        records.append(record)
        events.finished(record)

    # 只有进度回调来自工作线程；分片结果、提交与记账都由协调线程拥有。
    prepared: dict[str, tuple[VideoTask, FileTransaction, float]] = {}
    segment_results: dict[str, list[SegmentResult]] = {}
    contexts: dict[str, SegmentContext] = {}
    with worker_pool(encoder.runner, options.workers) as pool:
        whole_jobs = {}
        segment_jobs = {}
        for task in plan.tasks:
            encoder.runner.check_cancelled()
            started = time.monotonic()
            if task.error:
                finish(_record(task, encoder.config.codec_name, started, error=task.error))
                continue
            transaction = FileTransaction(encoder.layout, task.name, options.keep_backup)
            if not task.segments:
                whole_jobs[pool.submit(_whole, task, encoder, transaction)] = task
                continue
            try:
                transaction.prepare()
                contexts[task.name] = prepare_segments(encoder.layout, encoder.config, task)
            except (OSError, MediaError) as exc:
                transaction.rollback()
                finish(_record(task, encoder.config.codec_name, started, error=str(exc)))
                continue
            prepared[task.name] = (task, transaction, started)
            segment_results[task.name] = []
            for segment in task.segments:
                segment_jobs[pool.submit(encoder.segment, task, segment, contexts[task.name])] = (
                    task
                )
        try:
            for future in as_completed([*whole_jobs, *segment_jobs]):
                result = future.result()
                if isinstance(result, FileRecord):
                    finish(result)
                    continue
                task, transaction, started = prepared[result.name]
                if not result.error:
                    encoder.runner.check_cancelled()
                    target = encoder.layout.seg(task.name, result.index)
                    contexts[task.name].publish(result.path, target, result.index)
                    result = replace(result, path=target)
                results = segment_results[result.name]
                results.append(result)
                if len(results) != len(task.segments):
                    continue
                try:
                    encoder.concatenate(task, results, transaction.output)
                    encoder.runner.check_cancelled()
                    sizes = transaction.commit()
                except (OSError, MediaError, CommandError) as exc:
                    record = _record(
                        task,
                        encoder.config.codec_name,
                        started,
                        error=f"{exc}，保留分片以供下次恢复",
                    )
                else:
                    remove_tree(encoder.layout.seg_dir(task.name))
                    record = _record(task, encoder.config.codec_name, started, sizes=sizes)
                finish(record)
        finally:
            # 仅清理拼接临时文件；分片由线程池回收后仍保留以供恢复。
            for _, transaction, _ in prepared.values():
                transaction.rollback()
    return tuple(records)


def run_optimize(
    options: OptimizeOptions, events: OptimizationEvents | None = None
) -> OptimizationResult:
    events = events or OptimizationEvents()
    root = options.path.expanduser().resolve()
    if not root.is_dir():
        raise SetupError(f"路径不存在或不是目录: {root}")
    layout = IvdcLayout(root)
    validate_state_directory(layout)
    log_path = validate_log_path(
        layout,
        resolve_log_path(layout, options.log_file),
        chain(
            collect_files(root, options.pattern),
            (root / name for name in pending_backups(layout)),
        ),
    )
    # 在任何恢复或清理之前校验持久化数据。损坏时保留原文件并报告。
    manifest = load_manifest(layout)
    user_config = load_config()
    with ProcessRunner() as runner:
        toolchain = find_toolchain()
        environment = probe_environment(toolchain, runner)
        config = resolve_encode_config(
            options.codec,
            options.cuda,
            encoders_output=environment.encoders,
            hwaccels_output=environment.hwaccels,
            gpu_available=environment.gpu_available,
            quality=options.quality,
            quality_profiles=user_config.quality_profiles,
        )
        for notice in config.notices:
            events.notice(notice)
        events.notice(config.quality_description)
        plan = build_plan(options, layout, config, toolchain, runner)
        events.planned(plan, config.codec_name)
        if options.dry_run:
            return OptimizationResult(
                plan, failures=tuple(task.error for task in plan.tasks if task.error)
            )
        if not plan.tasks and not plan.recoveries and not plan.temporary_files:
            return OptimizationResult(plan)
        with ResultRecorder(layout, manifest, log_path, options.debug) as recorder:
            logging.getLogger(__name__).info("%s", config.quality_description)
            recovery = recover_backups(layout)
            for name in recovery.restored:
                events.notice(f"恢复上次中断的源文件: {name if not options.mask else '已恢复'}")
            for error in recovery.failures:
                events.notice(error)
            cleared = clear_tmp(layout)
            failures = list(recovery.failures)
            if cleared.failed:
                failures.append(f"{cleared.failed} 个临时文件未能删除")
            encoder = Encoder(toolchain, config, runner, layout, events.progress)
            records = _execute(plan, options, encoder, recorder, events)
            return OptimizationResult(plan, records, tuple(failures))
