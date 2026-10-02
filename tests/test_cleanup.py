"""``ivdc clean``：类别选择、--dry-run、以及「备份是唯一一份」时的保护。"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from rich.console import Console

from ivdc.cleanup import CleanOptions
from ivdc.cli import execute_clean
from ivdc.exitcodes import EXIT_ENV, EXIT_FAILURE, EXIT_OK
from ivdc.fs import IvdcLayout


def _layout(tmp_path: Path) -> IvdcLayout:
    root = tmp_path / "videos"
    root.mkdir()
    return IvdcLayout(root)


def _populate(layout: IvdcLayout) -> None:
    layout.backups_dir.mkdir(parents=True)
    (layout.backups_dir / "a.mp4").write_text("backup", encoding="utf-8")
    (layout.root / "a.mp4").write_text("final", encoding="utf-8")
    layout.seg_dir("b.mp4").mkdir(parents=True)
    (layout.seg("b.mp4", 0)).write_text("seg", encoding="utf-8")
    layout.tmp_dir.mkdir(parents=True)
    (layout.tmp_dir / "b.mp4").write_text("partial", encoding="utf-8")
    layout.logs_dir.mkdir(parents=True)
    (layout.logs_dir / "2026-09-11.log").write_text("log", encoding="utf-8")


def test_without_kinds_is_an_env_error(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    console, _ = console_pair
    assert execute_clean(CleanOptions(path=tmp_path), console) == EXIT_ENV


def test_missing_directory_is_an_env_error(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    console, _ = console_pair
    options = CleanOptions(path=tmp_path / "nope", kinds=("tmp",))
    assert execute_clean(options, console) == EXIT_ENV


def test_removes_only_the_selected_kinds(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    layout = _layout(tmp_path)
    _populate(layout)
    console, _ = console_pair

    code = execute_clean(CleanOptions(path=layout.root, kinds=("tmp", "logs")), console)

    assert code == EXIT_OK
    assert list(layout.tmp_dir.iterdir()) == []
    assert list(layout.logs_dir.iterdir()) == []
    assert (layout.backups_dir / "a.mp4").exists()
    assert layout.seg_dir("b.mp4").is_dir()


def test_all_clears_every_selected_directory(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    layout = _layout(tmp_path)
    _populate(layout)
    console, stream = console_pair

    code = execute_clean(
        CleanOptions(path=layout.root, kinds=("backups", "segs", "tmp", "logs")), console
    )

    assert code == EXIT_OK
    assert list(layout.backups_dir.iterdir()) == []
    assert list(layout.segs_dir.iterdir()) == []
    assert "共释放" in stream.getvalue()


def test_dry_run_lists_but_deletes_nothing(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    layout = _layout(tmp_path)
    _populate(layout)
    console, stream = console_pair

    code = execute_clean(
        CleanOptions(path=layout.root, kinds=("backups", "segs", "tmp", "logs"), dry_run=True),
        console,
    )

    assert code == EXIT_OK
    assert "未删除任何文件" in stream.getvalue()
    assert (layout.backups_dir / "a.mp4").exists()
    assert layout.seg_dir("b.mp4").is_dir()
    assert (layout.tmp_dir / "b.mp4").exists()


def test_backup_of_a_missing_source_is_protected(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    """源文件不在工作目录时，备份是唯一一份数据——绝不能被清掉。"""
    layout = _layout(tmp_path)
    layout.backups_dir.mkdir(parents=True)
    (layout.backups_dir / "orphan.mp4").write_text("only copy", encoding="utf-8")
    console, stream = console_pair

    code = execute_clean(CleanOptions(path=layout.root, kinds=("backups",)), console)

    assert code == EXIT_OK
    assert (layout.backups_dir / "orphan.mp4").exists()
    assert "跳过 1 个备份" in stream.getvalue()


def test_empty_state_directory_reports_nothing_to_do(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    layout = _layout(tmp_path)
    console, stream = console_pair

    assert execute_clean(CleanOptions(path=layout.root, kinds=("tmp",)), console) == EXIT_OK
    assert "没有需要清理的内容" in stream.getvalue()


def test_files_outside_the_state_directory_are_never_touched(
    console_pair: tuple[Console, StringIO], tmp_path: Path
) -> None:
    layout = _layout(tmp_path)
    _populate(layout)
    stray = layout.root / "tbd.txt.tmp"
    stray.write_text("keep", encoding="utf-8")
    console, _ = console_pair

    execute_clean(CleanOptions(path=layout.root, kinds=("backups", "segs", "tmp", "logs")), console)

    assert stray.exists()
    assert (layout.root / "a.mp4").exists()


def test_undeletable_units_are_reported_and_fail_the_run(
    console_pair: tuple[Console, StringIO], tmp_path: Path, monkeypatch
) -> None:
    """删不掉的项必须说出来：报告里的「已清理」要和磁盘上的实际情况一致。"""
    layout = _layout(tmp_path)
    _populate(layout)
    console, stream = console_pair
    monkeypatch.setattr("ivdc.cleanup.remove_tree", lambda path: False)

    code = execute_clean(CleanOptions(path=layout.root, kinds=("segs", "tmp")), console)

    assert code == EXIT_FAILURE
    output = stream.getvalue()
    assert "已清理 [segs] 0 项" in output
    assert "未能删除" in output
    assert "已清理 [tmp] 1 项" in output
    assert layout.seg_dir("b.mp4").is_dir()
    assert not (layout.tmp_dir / "b.mp4").exists()


def test_state_symlink_cannot_delete_outside_files(console_pair, tmp_path):
    layout = _layout(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "valuable").write_bytes(b"keep")
    layout.state_dir.mkdir()
    layout.tmp_dir.symlink_to(outside, target_is_directory=True)
    console, _ = console_pair
    assert execute_clean(CleanOptions(layout.root, ("tmp",)), console) == EXIT_ENV
    assert (outside / "valuable").read_bytes() == b"keep"
