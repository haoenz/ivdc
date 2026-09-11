"""子进程调用的统一封装（这是唯一执行外部命令的地方）。

两种用法：

- ``capture_command``：短命令取输出（探测 ffmpeg 能力、ffprobe 取元数据）。
- ``stream_command``：逐行回调（ffmpeg 的 ``-progress`` 流）。

两者都在 ``KeyboardInterrupt`` 时终止子进程再向上抛，避免留下还在跑的 ffmpeg。
"""

from __future__ import annotations

import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

__all__ = [
    "CommandResult",
    "active_count",
    "capture_command",
    "stream_command",
    "terminate_all",
]

_ENCODING = "utf-8"
_INTERRUPT_GRACE_SECONDS = 5.0

# 正在运行的外部命令。Ctrl+C 时主线程调 terminate_all() 把它们全部杀掉，
# 否则工作线程会一直卡在读管道上，线程池的 shutdown(wait=True) 就得干等。
_ACTIVE: set[subprocess.Popen[str]] = set()
_ACTIVE_LOCK = threading.Lock()


@dataclass(frozen=True)
class CommandResult:
    """一次外部命令的结果。

    ``stdout`` / ``stderr`` 只在 ``capture_command`` 下有值；``lines`` 只在
    ``stream_command`` 下有值（含合并进来的 stderr 行）。
    """

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    lines: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_INTERRUPT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=_INTERRUPT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - 极端情况
            pass


def terminate_all() -> int:
    """终止所有正在运行的外部命令，返回终止的进程数。

    Ctrl+C 之后调用：工作线程卡在管道读取上不会收到 KeyboardInterrupt，
    必须由主线程主动把子进程杀掉，它们才会退出、线程池才会收拢。
    """
    with _ACTIVE_LOCK:
        processes = list(_ACTIVE)
    for process in processes:
        _terminate(process)
    return len(processes)


def active_count() -> int:
    """当前在跑的外部命令数量。生产代码不读它，是给测试与排障用的观测接缝。"""
    with _ACTIVE_LOCK:
        return len(_ACTIVE)


def capture_command(
    argv: Sequence[str | Path],
    *,
    timeout: float | None = None,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """执行命令并一次性取回输出（不抛异常，失败体现为 returncode）。"""
    command = [str(item) for item in argv]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding=_ENCODING,
            errors="replace",
            timeout=timeout,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CommandResult(tuple(command), returncode=-1, stderr=str(exc))
    return CommandResult(
        argv=tuple(command),
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def stream_command(
    argv: Sequence[str | Path],
    *,
    on_output: Callable[[str], None] | None = None,
    keep_last: int = 40,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """执行命令并逐行回调输出（stderr 合并进 stdout，报错文本与进度行按到达顺序处理）。

    ``on_output`` 为 None 时把所有行收集进 ``CommandResult.lines``；给了回调时只保留
    末尾 ``keep_last`` 行——长任务会产生上万行进度输出，但失败排查只关心最后几行。
    """
    command = [str(item) for item in argv]
    collected: list[str] = []
    recent_lines: deque[str] = deque(maxlen=max(1, keep_last))
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding=_ENCODING,
        errors="replace",
        bufsize=1,
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
    )
    with _ACTIVE_LOCK:
        _ACTIVE.add(process)
    try:
        assert process.stdout is not None  # noqa: S101 - Popen 已指定 PIPE
        for raw in process.stdout:
            line = raw.rstrip("\r\n")
            recent_lines.append(line)
            if on_output is None:
                collected.append(line)
            else:
                on_output(line)
    except KeyboardInterrupt:
        _terminate(process)
        raise
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(process)
        if process.poll() is None:
            _terminate(process)
            process.wait()
    return CommandResult(
        argv=tuple(command),
        returncode=process.returncode if process.returncode is not None else -1,
        lines=tuple(collected) if on_output is None else tuple(recent_lines),
    )
