"""yt-dlp 适配层：外部字典、可选 impersonate 和下载文件解析。"""

import importlib
import logging
import math
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import urlsplit

from ivdc.config import Config
from ivdc.download import DownloadFailed, DownloadProgress, DownloadResult, ProgressReporter
from ivdc.errors import SetupError
from ivdc.naming import sanitize_filename
from ivdc.parse import TaskLine

if TYPE_CHECKING:
    from yt_dlp.networking.impersonate import ImpersonateTarget

logger = logging.getLogger(__name__)


class Downloader(Protocol):
    def prepare_filename(self, info: Any, /) -> str: ...


@dataclass(frozen=True)
class DownloadedFile:
    title: str
    path: Path


def impersonate_options(url: str, config: Config) -> dict[str, ImpersonateTarget]:
    hostname = urlsplit(url).hostname or ""
    if not any(
        hostname == domain or hostname.endswith("." + domain)
        for domain in config.impersonate_domains
    ):
        return {}
    try:
        importlib.import_module("curl_cffi")
        from yt_dlp.networking.impersonate import ImpersonateTarget
    except ImportError:
        logger.warning("该站点配置了 impersonate，但缺少 curl_cffi 或 yt-dlp 支持，本次不做伪装。")
        return {}
    try:
        target = ImpersonateTarget.from_str(config.impersonate_target)
    except ValueError as exc:
        logger.warning(
            "无法解析 impersonate 目标 %r: %s，本次不做伪装。", config.impersonate_target, exc
        )
        return {}
    return {"impersonate": target}


def _entries(info: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    entries = info.get("entries")
    if entries is None:
        yield info
    else:
        for entry in entries:
            if isinstance(entry, dict):
                yield from _entries(entry)


def collect_downloads(ydl: Downloader, info: Mapping[str, Any]) -> list[DownloadedFile]:
    collected: list[DownloadedFile] = []
    seen: set[Path] = set()
    for item in _entries(info):
        title = str(item.get("title") or "video")
        final_path = item.get("filepath")
        paths = [Path(final_path)] if isinstance(final_path, str) else []
        if not paths or not paths[0].is_file():
            paths = [
                Path(record["filepath"])
                for record in item.get("requested_downloads") or []
                if isinstance(record, dict) and isinstance(record.get("filepath"), str)
            ]
        if not any(path.is_file() for path in paths):
            filename = ydl.prepare_filename(item)
            paths = [Path(filename)] if filename else []
        for path in paths:
            if path.is_file() and path not in seen:
                seen.add(path)
                collected.append(DownloadedFile(title, path))
    return collected


def finalize_downloads(
    downloads: list[DownloadedFile], title_override: str | None = None
) -> DownloadResult:
    total_bytes = 0
    for index, item in enumerate(downloads, start=1):
        title = title_override or item.title
        if title_override and len(downloads) > 1:
            title = f"{title_override} {index}"
        target = item.path.with_name(sanitize_filename(title) + item.path.suffix)
        if target != item.path:
            if target.exists():
                logger.warning("目标名已存在，保留原文件名: %s", item.path)
                target = item.path
            else:
                try:
                    os.rename(item.path, target)
                except OSError as exc:
                    logger.warning("重命名失败，保留原文件名 %s: %s", item.path, exc)
                    target = item.path
        try:
            total_bytes += target.stat().st_size
        except OSError as exc:
            raise DownloadFailed(f"无法读取下载文件 {target}: {exc}") from exc
    return DownloadResult(title_override or downloads[0].title, total_bytes)


def _number(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


class ProgressHook:
    def __init__(self, reporter: ProgressReporter) -> None:
        self.reporter = reporter

    def __call__(self, payload: Mapping[str, Any]) -> None:
        status = payload.get("status")
        if status not in ("downloading", "finished"):
            return
        self.reporter(
            DownloadProgress(
                downloaded=int(_number(payload.get("downloaded_bytes")) or 0),
                total=int(
                    _number(payload.get("total_bytes") or payload.get("total_bytes_estimate")) or 0
                ),
                speed=_number(payload.get("speed")),
                eta=_number(payload.get("eta")),
                finished=status == "finished",
            )
        )


class YtdlpLogger:
    def debug(self, message: str) -> None:
        logger.debug("[yt-dlp] %s", message.removeprefix("[debug] "))

    def info(self, message: str) -> None:
        logger.info("[yt-dlp] %s", message)

    def warning(self, message: str) -> None:
        logger.warning("[yt-dlp] %s", message)

    def error(self, message: str) -> None:
        logger.error("[yt-dlp] %s", message)


class YtdlpFetcher:
    def __init__(self, config: Config) -> None:
        try:
            import yt_dlp
            from yt_dlp.utils import DownloadError
        except ImportError as exc:
            raise SetupError("未找到 yt-dlp，请先安装项目依赖。") from exc
        self.download_error = DownloadError
        self.config = config
        self.module = yt_dlp

    def __call__(self, task: TaskLine, reporter: ProgressReporter) -> DownloadResult:
        options: dict[str, object] = {
            "quiet": True,
            "noprogress": True,
            "logger": YtdlpLogger(),
            "progress_hooks": [ProgressHook(reporter)],
            "outtmpl": "%(title)s [%(id)s].%(ext)s",
            "overwrites": False,
        }
        target = impersonate_options(task.url, self.config).get("impersonate")
        try:
            # yt-dlp 的动态参数和内部能力方法限于此协议边界。
            with self.module.YoutubeDL(cast(Any, options)) as ydl:
                if target is not None:
                    available = getattr(ydl, "_impersonate_target_available", None)
                    if available is not None and available(target):
                        ydl.params["impersonate"] = target
                    else:
                        logger.warning(
                            "当前 yt-dlp 不支持 impersonate 目标 %s，本次不做伪装。", target
                        )
                info = ydl.extract_info(task.url, download=True)
                if not isinstance(info, dict):
                    raise DownloadFailed("yt-dlp 未返回有效的下载信息")
                downloads = collect_downloads(ydl, info)
        except self.download_error as exc:
            raise DownloadFailed(str(exc).strip()) from exc
        except OSError as exc:
            raise DownloadFailed(f"下载文件操作失败: {exc}") from exc
        if not downloads:
            raise DownloadFailed("下载结束但没有找到输出文件")
        return finalize_downloads(downloads, task.title)
