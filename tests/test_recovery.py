"""中断残留的恢复策略：备份还原、分片去留。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ivdc.fs import IvdcLayout
from ivdc.recovery import recover_backups


@pytest.fixture
def layout(tmp_path: Path) -> IvdcLayout:
    root = tmp_path / "videos"
    root.mkdir()
    return IvdcLayout(root)


def test_recover_backups_restores_missing_final(layout: IvdcLayout) -> None:
    layout.backups_dir.mkdir(parents=True)
    (layout.backups_dir / "a.mp4").write_text("x", encoding="utf-8")

    report = recover_backups(layout)

    assert report.restored == ["a.mp4"]
    assert (layout.root / "a.mp4").read_text(encoding="utf-8") == "x"
    assert (layout.backups_dir / "a.mp4").read_text(encoding="utf-8") == "x"


def test_recover_backups_keeps_segments_when_segmented(layout: IvdcLayout) -> None:
    layout.backups_dir.mkdir(parents=True)
    (layout.backups_dir / "a.mp4").write_text("x", encoding="utf-8")
    layout.seg_dir("a.mp4").mkdir(parents=True)
    (layout.seg("a.mp4", 0)).write_text("seg", encoding="utf-8")

    recover_backups(layout)

    assert layout.seg_dir("a.mp4").is_dir()


def test_recovery_never_discards_existing_segments(layout: IvdcLayout) -> None:
    layout.backups_dir.mkdir(parents=True)
    (layout.backups_dir / "a.mp4").write_text("x", encoding="utf-8")
    layout.seg_dir("a.mp4").mkdir(parents=True)
    (layout.seg("a.mp4", 0)).write_text("seg", encoding="utf-8")

    report = recover_backups(layout)

    assert report.restored == ["a.mp4"]
    assert layout.seg("a.mp4", 0).read_text(encoding="utf-8") == "seg"


def test_recover_backups_leaves_finished_files_alone(layout: IvdcLayout) -> None:
    layout.backups_dir.mkdir(parents=True)
    (layout.backups_dir / "a.mp4").write_text("backup", encoding="utf-8")
    (layout.root / "a.mp4").write_text("final", encoding="utf-8")

    report = recover_backups(layout)

    assert report.restored == []
    assert (layout.root / "a.mp4").read_text(encoding="utf-8") == "final"
    assert (layout.backups_dir / "a.mp4").exists()


def test_recover_backups_without_state_dir_is_a_noop(layout: IvdcLayout) -> None:
    assert recover_backups(layout).restored == []


def test_restore_failure_is_recorded_not_swallowed(layout: IvdcLayout, monkeypatch) -> None:
    """还原失败意味着工作目录里那个文件是缺的，必须出现在报告里。"""
    layout.backups_dir.mkdir(parents=True)
    (layout.backups_dir / "a.mp4").write_text("x", encoding="utf-8")

    def boom(source: object, target: object) -> None:
        raise OSError("文件被占用")

    monkeypatch.setattr("ivdc.recovery.os.replace", boom)

    report = recover_backups(layout)

    assert report.restored == []
    assert len(report.failures) == 1
    assert "还原备份 a.mp4 失败" in report.failures[0]
    assert (layout.backups_dir / "a.mp4").exists()


def test_recovery_does_not_invoke_segment_cleanup(layout: IvdcLayout, monkeypatch) -> None:
    layout.backups_dir.mkdir(parents=True)
    (layout.backups_dir / "a.mp4").write_text("x", encoding="utf-8")
    layout.seg_dir("a.mp4").mkdir(parents=True)
    monkeypatch.setattr("ivdc.store.remove_tree", lambda path: pytest.fail("不能清理可恢复分片"))

    report = recover_backups(layout)

    assert report.failures == []
    assert report.restored == ["a.mp4"]
    assert layout.seg_dir("a.mp4").is_dir()


def test_interrupted_backup_copy_is_not_recovered_as_a_source(layout):
    layout.backups_dir.mkdir(parents=True)
    temporary = layout.backups_dir / ".ivdc-copy-incomplete.tmp"
    temporary.write_bytes(b"partial")
    report = recover_backups(layout)
    assert report.restored == []
    assert temporary.read_bytes() == b"partial"
    assert not (layout.root / temporary.name).exists()


def test_backup_path_directory_does_not_allow_unprotected_commit(layout):
    from ivdc.recovery import FileTransaction

    source = layout.root / "a.mp4"
    source.write_bytes(b"source")
    layout.backup("a.mp4").mkdir(parents=True)
    with pytest.raises(IsADirectoryError):
        FileTransaction(layout, "a.mp4", True).prepare()
    assert source.read_bytes() == b"source"
