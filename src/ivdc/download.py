"""``ivdc dl``：按任务清单逐条下载（yt-dlp 库模式）。

三条关键路径都走 yt-dlp 的结构化接口，不解析子进程输出：

- 进度：``progress_hooks`` 直接给 ``downloaded_bytes`` / ``total_bytes``；
- 元数据：``extract_info()`` 返回结构化 dict；
- 文件名：统一走 :func:`~ivdc.naming.sanitize_filename`。

清单里的标题和 ``extract_info()`` 探测到的标题**都要**过
:func:`~ivdc.naming.sanitize_filename`：标题里出现 ``: / ?`` 时落盘会失败
（见 SPEC.md §6.2）。

清单的写回语义：**先写 ``.tmp`` 再原子替换**，中途崩溃不会把剩余任务截断。
"""

from __future__ import annotations

import os
import time
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator

from rich.console import Console

from ivdc.config import Config, ensure_config_file, load_config
from ivdc.console import (
    DEBUG_PREFIX,
    STYLE_DEBUG,
    STYLE_DONE,
    STYLE_ERROR,
    STYLE_FAIL,
    STYLE_INFO,
    STYLE_WARN,
    emit,
)
from ivdc.exitcodes import EXIT_ENV, EXIT_FAILURE, EXIT_OK
from ivdc.formatting import (
    format_download_line,
    format_download_status,
    format_media_size,
    progress_percent,
    shorten_label,
)
from ivdc.naming import new_tag, sanitize_filename
from ivdc.parse import TaskLine, parse_task_line
from ivdc.progress import ProgressDisplay
from ivdc.store import append_line, read_nonempty_lines, write_lines

__all__ = [
    "DlOptions",
    "DownloadFailed",
    "DownloadResult",
    "Fetcher",
    "OnError",
    "TaskReporter",
    "run_download",
]

# yt-dlp 自己给调试行加的 "[debug] " 前缀要去掉，换成本工具统一的日志标签。
_YTDLP_DEBUG_PREFIX = "[debug] "
_YTDLP_LABEL = "[yt-dlp] "
_OUTTMPL = "%(title)s.%(ext)s"


class DownloadFailed(RuntimeError):
    """一条任务下载失败。"""


@dataclass(frozen=True)
class DownloadResult:
    """一条任务下载成功的结果。"""

    title: str
    size_bytes: int


@dataclass(frozen=True)
class TaskReporter:
    """fetcher 与界面之间的窄接口（测试里换成假的就能脱离网络跑）。"""

    progress: Callable[[int, str], None]
    log: Callable[[str, str], None]

    def report(self, percent: int, status: str) -> None:
        self.progress(percent, status)

    def warn(self, message: str) -> None:
        self.log(message, STYLE_WARN)

    def debug(self, message: str) -> None:
        self.log(message, STYLE_DEBUG)


Fetcher = Callable[[TaskLine, TaskReporter], DownloadResult]


class OnError(str, Enum):
    """下载失败后的行为（``--on-error``）。"""

    stop = "stop"
    skip = "skip"


@dataclass(frozen=True)
class DlOptions:
    """``ivdc dl`` 的全部选项。"""

    tasks: Path
    done: Path
    max_count: int | None = None
    on_error: OnError = OnError.stop
    mask: bool = False
    debug: bool = False


def run_download(
    options: DlOptions, console: Console, *, fetcher: Fetcher | None = None
) -> int:
    """按清单逐条下载，返回进程退出码。"""
    tasks_path = options.tasks
    if not tasks_path.is_file():
        emit(console, f"任务列表文件不存在: {tasks_path}", STYLE_ERROR)
        return EXIT_ENV

    config = load_config()
    fetch = fetcher
    if fetch is None:
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            emit(console, "未找到 yt-dlp，请先安装（uv add yt-dlp / pip install yt-dlp）。", STYLE_ERROR)
            return EXIT_ENV
        if options.debug:
            emit(console, f"{DEBUG_PREFIX}配置: {ensure_config_file()}", STYLE_DEBUG)
        fetch = _make_ytdlp_fetcher(config, debug=options.debug)

    pending = read_nonempty_lines(tasks_path)
    if not pending:
        emit(console, "任务列表为空，没有需要下载的内容。", STYLE_INFO)
        return EXIT_OK

    total = len(pending)
    completed = 0
    failed: list[str] = []
    attempts = 0
    display = ProgressDisplay(console, title_prefix="Downloading")

    def persist() -> None:
        """把清单写回磁盘：剩余任务在前，失败条目挪到末尾。

        每处理完一条就写一次（而不是循环结束再写），中途崩溃时磁盘上的清单与内存
        一致，下次运行接着下载。stop 模式下失败的那条故意不写回，留在原位。
        """
        write_lines(tasks_path, list(pending) + failed)

    try:
        with display:
            display.set_global(0, completed=0, total=total)
            while pending:
                if options.max_count is not None and attempts >= options.max_count:
                    break
                attempts += 1
                line = pending[0]
                task = parse_task_line(line)
                if not task.url:
                    pending = pending[1:]
                    persist()
                    continue

                label = new_tag() if options.mask else (task.title or task.url)
                task_id = display.add_task(shorten_label(label))
                reporter = TaskReporter(
                    progress=lambda percent, status, tid=task_id: display.update(
                        tid, completed=percent, status=status
                    ),
                    log=display.log,
                )
                started = time.monotonic()
                try:
                    result = fetch(task, reporter)
                except DownloadFailed as exc:
                    display.remove_task(task_id)
                    display.log(f"✗ {label} 下载失败: {exc}", STYLE_FAIL)
                    failed.append(line)
                    if options.on_error == OnError.stop:
                        display.log("中止后续任务。", STYLE_FAIL)
                        break
                    pending = pending[1:]
                    persist()
                    continue
                except KeyboardInterrupt:
                    display.remove_task(task_id)
                    raise

                display.remove_task(task_id)
                minutes = (time.monotonic() - started) / 60
                display.log(
                    format_download_line(
                        label, format_media_size(result.size_bytes), minutes
                    ),
                    STYLE_DONE,
                )
                done_title = task.title or result.title
                append_line(options.done, f"{done_title} {task.url}".strip())
                pending = pending[1:]
                persist()
                completed += 1
                display.set_global(
                    progress_percent(completed, total), completed=completed, total=total
                )
    except KeyboardInterrupt:
        display.log("已中断。完成的任务已经写回清单，下次运行接着下载。", STYLE_WARN)
        return EXIT_FAILURE

    style = STYLE_WARN if failed else STYLE_DONE
    emit(console, f"完成: 成功 {completed} 个，失败 {len(failed)} 个。", style)
    return EXIT_FAILURE if failed else EXIT_OK


# ---- yt-dlp 库模式的实现 -------------------------------------------------


def _impersonate_opts(
    url: str, config: Config, reporter: TaskReporter
) -> dict[str, object]:
    """需要 TLS 指纹伪装的站点才附加 impersonate 配置。

    库模式的 impersonate 依赖 ``curl_cffi`` 与 yt-dlp 的内部类，两者缺一都退化：
    打印一条提示然后照常下载，而不是直接失败。
    """
    if not config.impersonate_domains:
        return {}
    if not any(domain in url for domain in config.impersonate_domains):
        return {}
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
    except ImportError:
        reporter.warn("该站点配置了 impersonate，但当前 yt-dlp 版本不支持，本次不做伪装。")
        return {}
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        reporter.warn(
            f"该站点需要伪装 {config.impersonate_target}，但未安装 curl_cffi，"
            "本次不做伪装（需要时执行 pip install curl_cffi）。"
        )
        return {}
    try:
        target = ImpersonateTarget.from_str(config.impersonate_target)
    except Exception:  # noqa: BLE001 - 目标串非法时退回不伪装
        reporter.warn(f"无法解析 impersonate 目标 '{config.impersonate_target}'，本次不做伪装。")
        return {}
    if target is None:
        return {}
    reporter.debug(f"{DEBUG_PREFIX}impersonate: {target}")
    return {"impersonate": target}


def _iter_entries(info: dict) -> Iterator[dict]:
    """把一个 info（可能是播放列表）摊平成条目序列。"""
    entries = info.get("entries")
    if entries is None:
        yield info
        return
    for entry in entries:
        if entry:
            yield entry


def _collect_downloads(ydl: object, info: dict) -> list[tuple[str, Path]]:
    """取回这次实际落盘的文件（标题 + 路径）。"""
    collected: list[tuple[str, Path]] = []
    for item in _iter_entries(info):
        title = str(item.get("title") or "") or "video"
        paths: list[Path] = []
        for record in item.get("requested_downloads") or []:
            filepath = record.get("filepath")
            if filepath:
                paths.append(Path(filepath))
        if not paths:
            with suppress(Exception):
                paths = [Path(ydl.prepare_filename(item))]  # type: ignore[attr-defined]
        for path in paths:
            if path.exists():
                collected.append((title, path))
    return collected


def _finalize_downloads(
    downloads: list[tuple[str, Path]], reporter: TaskReporter
) -> tuple[int, str]:
    """把下载结果改名成清洗后的标题，返回 (总字节数, 第一个标题)。

    改名失败时退回原名继续统计——文件已经在盘上了，改不了名不该让整条任务失败，
    但必须让用户知道磁盘上留的是哪个名字。
    """
    total_bytes = 0
    first_title = downloads[0][0]
    for title, path in downloads:
        target = path.with_name(sanitize_filename(title) + path.suffix)
        if target != path:
            if target.exists():
                reporter.warn(f"目标名已存在，保留原文件名: {path.name}")
                target = path
            else:
                try:
                    os.replace(path, target)
                except OSError as exc:
                    reporter.warn(f"重命名失败，保留原文件名: {path.name}（{exc}）")
                    target = path
        try:
            total_bytes += target.stat().st_size
        except OSError as exc:
            reporter.warn(f"无法读取文件大小: {target.name}（{exc}）")
    return total_bytes, first_title


def _make_ytdlp_fetcher(config: Config, *, debug: bool) -> Fetcher:
    """构造真正的下载器（只在需要时才 import yt_dlp）。"""

    def fetch(task: TaskLine, reporter: TaskReporter) -> DownloadResult:
        import yt_dlp

        class _Logger:
            """yt-dlp 的输出全都走 logger，避免它直接写 stderr 打乱进度条。"""

            def debug(self, message: str) -> None:
                if message.startswith(_YTDLP_DEBUG_PREFIX):
                    message = message[len(_YTDLP_DEBUG_PREFIX) :]
                if debug:
                    reporter.debug(f"{_YTDLP_LABEL}{message}")

            def info(self, message: str) -> None:
                if debug:
                    reporter.debug(f"{_YTDLP_LABEL}{message}")

            def warning(self, message: str) -> None:
                if not debug:
                    return
                reporter.warn(f"{_YTDLP_LABEL}{message}")

            def error(self, message: str) -> None:
                if debug:
                    reporter.log(f"{_YTDLP_LABEL}{message}", STYLE_FAIL)

        last_percent = -1

        def hook(payload: dict) -> None:
            nonlocal last_percent
            status = payload.get("status")
            if status == "downloading":
                total = payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0
                done = payload.get("downloaded_bytes") or 0
                percent = progress_percent(done, total)
                if percent != last_percent:
                    last_percent = percent
                    reporter.report(
                        percent,
                        format_download_status(
                            done, total, payload.get("speed"), payload.get("eta")
                        ),
                    )
            elif status == "finished":
                reporter.report(100, "下载完成，处理中...")
            elif status == "error":
                reporter.report(0, "下载出错")

        options: dict[str, object] = {
            "quiet": False,
            "noprogress": True,
            "logger": _Logger(),
            "progress_hooks": [hook],
            "outtmpl": _OUTTMPL,
        }
        options.update(_impersonate_opts(task.url, config, reporter))
        if debug:
            reporter.debug(f"{DEBUG_PREFIX}yt-dlp {task.url} outtmpl={_OUTTMPL}")

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(task.url, download=True)
                if not isinstance(info, dict):
                    raise DownloadFailed("yt-dlp 未返回有效的下载信息")
                downloads = _collect_downloads(ydl, info)
        except yt_dlp.utils.DownloadError as exc:
            raise DownloadFailed(_short_error(exc)) from exc

        if not downloads:
            raise DownloadFailed("下载结束但没有找到输出文件")
        size_bytes, first_title = _finalize_downloads(downloads, reporter)
        return DownloadResult(title=task.title or first_title, size_bytes=size_bytes)

    return fetch


def _short_error(exc: BaseException) -> str:
    """把 yt-dlp 的多行错误压成一行。"""
    text = str(exc).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "yt-dlp 下载失败"
    cleaned = lines[0]
    for prefix in ("ERROR: ", "error: "):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    return cleaned
