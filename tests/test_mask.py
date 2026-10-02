"""匿名展示覆盖错误、第三方诊断和 traceback；业务记录保留原始上下文。"""

import logging
import shutil
from io import StringIO

import pytest

from ivdc.cli import execute_download, execute_optimize
from ivdc.download import (
    DownloadEvents,
    DownloadFailed,
    DownloadOptions,
    DownloadResult,
    run_download,
)
from ivdc.errors import SetupError, StorageError
from ivdc.exitcodes import EXIT_ENV, EXIT_FAILURE, EXIT_OK
from ivdc.fs import IvdcLayout
from ivdc.optimization_plan import OptimizationPlan, OptimizeOptions
from ivdc.progress import ProgressDisplay
from ivdc.runner import CommandError
from ivdc.store import load_manifest
from ivdc.views import OptimizationDisplay, diagnostics
from ivdc.ytdlp import DownloadedFile, YtdlpLogger, finalize_downloads

SECRET = "PRIVATE_SENTINEL"
URL = f"https://private.invalid/watch?token={SECRET}"
DETAILS = f"{SECRET}.mp4: {URL}"
MODES = [(True, False), (True, True), (False, False), (False, True)]
requires_media = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="需要真实 ffmpeg 与 ffprobe"
)


@pytest.mark.parametrize(("mask", "debug"), MODES)
def test_download_failure_and_adapter_diagnostics_obey_mask(tmp_path, console_pair, mask, debug):
    tasks, done = tmp_path / "tasks.txt", tmp_path / "done.txt"
    original = f"{SECRET} {URL}\n"
    tasks.write_text(original)
    console, output = console_pair

    def fetch(task, reporter):
        adapter = YtdlpLogger()
        adapter.warning(DETAILS)
        adapter.error(DETAILS)
        adapter.debug(DETAILS)
        raise DownloadFailed(DETAILS)

    assert (
        execute_download(
            DownloadOptions(tasks, done, mask=mask, debug=debug), console, fetcher=fetch
        )
        == EXIT_FAILURE
    )
    assert "下载失败" in output.getvalue()
    assert (SECRET in output.getvalue()) is not mask
    assert tasks.read_text() == original
    assert not done.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["tasks.txt"]


def test_download_business_result_keeps_original_error_when_masked(tmp_path):
    tasks = tmp_path / "tasks.txt"
    tasks.write_text(f"{SECRET} {URL}\n")
    errors = []

    def fetch(task, reporter):
        raise DownloadFailed(DETAILS)

    result = run_download(
        DownloadOptions(tasks, tmp_path / "done.txt", mask=True),
        DownloadEvents(failed=lambda label, error: errors.append((label, error))),
        fetcher=fetch,
    )
    assert result.failures == (DETAILS,)
    assert SECRET not in errors[0][0]
    assert errors[0][1] == DETAILS


@pytest.mark.parametrize("debug", [False, True])
def test_successful_download_rename_warning_is_masked_but_done_record_is_real(
    tmp_path, console_pair, debug
):
    tasks, done = tmp_path / "tasks.txt", tmp_path / "done.txt"
    tasks.write_text(f"{SECRET} {URL}\n")
    source, occupied = tmp_path / f"{SECRET}-raw.mp4", tmp_path / f"{SECRET}.mp4"
    source.write_bytes(b"download")
    occupied.write_bytes(b"existing")

    def fetch(task, reporter):
        return finalize_downloads([DownloadedFile(task.title, source)])

    console, output = console_pair
    assert (
        execute_download(
            DownloadOptions(tasks, done, mask=True, debug=debug), console, fetcher=fetch
        )
        == EXIT_OK
    )
    assert SECRET not in output.getvalue()
    assert "诊断详情已隐藏" in output.getvalue()
    assert done.read_text() == f"{SECRET} {URL}\n"
    assert tasks.read_text() == ""
    assert source.read_bytes() == b"download"
    assert occupied.read_bytes() == b"existing"


@pytest.mark.parametrize("command", ["opt", "dl"])
@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        (SetupError, EXIT_ENV),
        (StorageError, EXIT_FAILURE),
        (CommandError, EXIT_FAILURE),
        (PermissionError, EXIT_FAILURE),
        (UnicodeError, EXIT_FAILURE),
        (RuntimeError, EXIT_FAILURE),
    ],
)
def test_cli_exception_boundary_hides_details_and_traceback(
    tmp_path, monkeypatch, console_pair, command, debug, error_type, expected
):
    def fail(*args, **kwargs):
        raise error_type(DETAILS)

    console, output = console_pair
    if command == "opt":
        monkeypatch.setattr("ivdc.cli.run_optimize", fail)
        code = execute_optimize(OptimizeOptions(tmp_path, mask=True, debug=debug), console)
    else:
        monkeypatch.setattr("ivdc.cli.run_download", fail)
        code = execute_download(
            DownloadOptions(tmp_path / "tasks", tmp_path / "done", mask=True, debug=debug), console
        )
    assert code == expected
    assert SECRET not in output.getvalue()
    assert "已隐藏" in output.getvalue()
    assert "Traceback" not in output.getvalue()


@pytest.mark.parametrize("debug", [False, True])
def test_done_write_failure_keeps_task_and_hides_path(tmp_path, monkeypatch, console_pair, debug):
    tasks, done = tmp_path / "tasks.txt", tmp_path / f"{SECRET}.txt"
    original = f"{SECRET} {URL}\n"
    tasks.write_text(original)

    def denied(*args):
        raise PermissionError(DETAILS)

    monkeypatch.setattr("ivdc.download.append_line", denied)
    console, output = console_pair
    assert (
        execute_download(
            DownloadOptions(tasks, done, mask=True, debug=debug),
            console,
            fetcher=lambda task, reporter: DownloadResult(SECRET, 1),
        )
        == EXIT_FAILURE
    )
    assert SECRET not in output.getvalue()
    assert tasks.read_text() == original
    assert not done.exists()


@pytest.mark.parametrize("file_first", [False, True])
def test_console_filter_does_not_modify_file_records_and_restores_logger(
    tmp_path, console_pair, file_first
):
    console, output = console_pair
    logger = logging.getLogger("ivdc")
    root = logging.getLogger()
    previous = (logger.level, logger.propagate, tuple(logger.handlers))
    log_path = tmp_path / "diagnostics.log"
    root_output = StringIO()
    root_handler = logging.StreamHandler(root_output)
    root.addHandler(root_handler)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    try:
        if file_first:
            logger.addHandler(handler)
        with (
            pytest.raises(RuntimeError, match="scope failed"),
            diagnostics(console, True, mask=True),
        ):
            if not file_first:
                logger.addHandler(handler)
            logging.getLogger("ivdc.ytdlp").warning("warning: %s", DETAILS, stack_info=True)
            try:
                raise ValueError(DETAILS)
            except ValueError:
                logging.getLogger("ivdc.runner").exception("error: %s", DETAILS)
            logging.getLogger("ivdc.runner").debug("command: %s", DETAILS)
            raise RuntimeError("scope failed")
    finally:
        logger.removeHandler(handler)
        handler.close()
        root.removeHandler(root_handler)
        root_handler.close()
    assert SECRET not in output.getvalue()
    assert "Traceback" not in output.getvalue()
    assert "Stack" not in output.getvalue()
    assert root_output.getvalue() == ""
    assert (logger.level, logger.propagate, tuple(logger.handlers)) == previous
    content = log_path.read_text()
    assert f"warning: {DETAILS}" in content
    assert f"ValueError: {DETAILS}" in content
    assert f"command: {DETAILS}" in content
    assert "Traceback" in content


def test_unmasked_diagnostics_still_include_exception_context(console_pair):
    console, output = console_pair
    with diagnostics(console, True):
        try:
            raise ValueError(DETAILS)
        except ValueError:
            logging.getLogger("ivdc.runner").exception("command: %s", DETAILS)
    assert SECRET in output.getvalue()
    assert "Traceback" in output.getvalue()


def test_empty_plan_does_not_expose_user_filter(console_pair, tmp_path):
    console, output = console_pair
    with ProgressDisplay(console) as progress:
        view = OptimizationDisplay(
            OptimizeOptions(tmp_path, pattern=f"{SECRET}*.mp4", mask=True, dry_run=True),
            console,
            progress,
        )
        view.planned(OptimizationPlan((), (), (), ()), "hevc")
    assert SECRET not in output.getvalue()
    assert "未找到匹配" in output.getvalue()


@requires_media
@pytest.mark.integration
@pytest.mark.parametrize(("mask", "debug"), MODES)
@pytest.mark.parametrize("dry_run", [False, True])
def test_probe_errors_are_masked_without_changing_media_or_persistent_errors(
    tmp_path, console_pair, mask, debug, dry_run
):
    source = tmp_path / f"{SECRET}.mp4"
    source.write_bytes(b"not a video")
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in tmp_path.iterdir()}
    console, output = console_pair
    assert (
        execute_optimize(
            OptimizeOptions(tmp_path, cuda="off", mask=mask, debug=debug, dry_run=dry_run), console
        )
        == EXIT_FAILURE
    )
    assert (SECRET in output.getvalue()) is not mask
    assert source.read_bytes() == b"not a video"
    if dry_run:
        assert {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in tmp_path.iterdir()} == before
    else:
        layout = IvdcLayout(tmp_path)
        assert SECRET in next(layout.logs_dir.glob("*.log")).read_text()
        record = load_manifest(layout).get(source.name)
        assert record is not None and record.status == "failed"
        assert SECRET in record.message


@requires_media
@pytest.mark.integration
@pytest.mark.parametrize("debug", [False, True])
def test_recovery_failure_is_hidden_in_notices_and_summary_but_logged(
    tmp_path, monkeypatch, console_pair, debug
):
    layout = IvdcLayout(tmp_path)
    layout.backups_dir.mkdir(parents=True)
    backup = layout.backup(f"{SECRET}.mp4")
    backup.write_bytes(b"original")

    def denied(*args):
        raise PermissionError(DETAILS)

    monkeypatch.setattr("ivdc.recovery.atomic_copy", denied)
    console, output = console_pair
    assert (
        execute_optimize(OptimizeOptions(tmp_path, cuda="off", mask=True, debug=debug), console)
        == EXIT_FAILURE
    )
    assert SECRET not in output.getvalue()
    assert "恢复或清理失败" in output.getvalue()
    log = next(layout.logs_dir.glob("*.log")).read_text()
    assert DETAILS in log
    assert backup.read_bytes() == b"original"
    assert not (tmp_path / backup.name).exists()


@pytest.mark.parametrize("name", ["ivdc", "ivdc.ytdlp", "ivdc.external.nested"])
@pytest.mark.parametrize("debug", [False, True])
def test_existing_terminal_handlers_are_masked_and_restored(tmp_path, console_pair, name, debug):
    logger = logging.getLogger(name)
    original_state = logger.level, logger.propagate, tuple(logger.handlers)
    extra_output = StringIO()
    extra = logging.StreamHandler(extra_output)
    file = logging.FileHandler(tmp_path / "raw.log", encoding="utf-8")
    original_filter = logging.Filter()
    extra.addFilter(original_filter)
    logger.addHandler(extra)
    logger.addHandler(file)
    try:
        with pytest.raises(RuntimeError, match="leave scope"):
            with diagnostics(console_pair[0], debug, mask=True):
                try:
                    raise ValueError(DETAILS)
                except ValueError:
                    logger.exception("%s", DETAILS, stack_info=True)
                raise RuntimeError("leave scope")
        assert SECRET not in extra_output.getvalue()
        assert "已隐藏" in extra_output.getvalue()
        assert SECRET not in console_pair[1].getvalue()
        assert "Traceback" not in extra_output.getvalue()
        assert "Stack" not in extra_output.getvalue()
        assert DETAILS in (tmp_path / "raw.log").read_text()
        assert "Traceback" in (tmp_path / "raw.log").read_text()
        assert extra.filters == [original_filter]
        logger.warning("after scope: %s", DETAILS)
        assert DETAILS in extra_output.getvalue()
    finally:
        logger.removeHandler(extra)
        logger.removeHandler(file)
        extra.close()
        file.close()
    assert (logger.level, logger.propagate, tuple(logger.handlers)) == original_state
