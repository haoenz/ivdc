"""一次运行拥有的子进程资源；探测、编码和拼接使用同一生命周期。"""

import logging
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Self

logger = logging.getLogger(__name__)


class CommandError(RuntimeError):
    """命令启动或等待失败，包含命令与原始异常上下文。"""


class RunCancelled(RuntimeError):
    """运行已取消，不再启动新命令。"""


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    lines: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class ProcessRunner:
    def __init__(self) -> None:
        self._processes: set[subprocess.Popen[str]] = set()
        self._lock = threading.Lock()
        self._cancelled = threading.Event()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.cancel()

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._processes)

    def check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise RunCancelled("运行已取消")

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait()

    def cancel(self) -> int:
        with self._lock:
            self._cancelled.set()
            processes = tuple(self._processes)
        # 先向所有进程发信号，避免逐个等待让其它编码继续运行。
        for process in processes:
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        for process in processes:
            self._terminate(process)
        return len(processes)

    @contextmanager
    def _process(
        self,
        argv: Sequence[str | Path],
        *,
        merged: bool = False,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Iterator[subprocess.Popen[str]]:
        command = tuple(str(item) for item in argv)
        logger.debug("执行: %s", " ".join(command))
        with self._lock:
            self.check_cancelled()
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT if merged else subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=cwd,
                    env=env,
                )
            except OSError as exc:
                raise CommandError(f"无法启动 {command!r}: {exc}") from exc
            self._processes.add(process)
        try:
            yield process
        finally:
            self._terminate(process)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
            with self._lock:
                self._processes.discard(process)

    def capture(
        self,
        argv: Sequence[str | Path],
        *,
        timeout: float | None = None,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        with self._process(argv, cwd=cwd, env=env) as process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                raise CommandError(f"命令超时 ({timeout}s): {tuple(map(str, argv))!r}") from exc
            self.check_cancelled()
            return CommandResult(tuple(map(str, argv)), process.returncode, stdout, stderr)

    def stream(
        self,
        argv: Sequence[str | Path],
        *,
        on_output: Callable[[str], None] | None = None,
        keep_last: int = 40,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        lines: list[str] | deque[str] = [] if on_output is None else deque(maxlen=max(1, keep_last))
        with self._process(argv, merged=True, cwd=cwd, env=env) as process:
            assert process.stdout is not None
            for raw in process.stdout:
                line = raw.rstrip("\r\n")
                lines.append(line)
                if on_output is not None:
                    on_output(line)
            process.wait()
            self.check_cancelled()
            return CommandResult(tuple(map(str, argv)), process.returncode, lines=tuple(lines))


@contextmanager
def worker_pool(runner: ProcessRunner, workers: int) -> Iterator[ThreadPoolExecutor]:
    """异常时先取消子进程和待执行任务，再等待线程退出。"""
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ivdc-worker")
    try:
        yield pool
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        runner.cancel()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
