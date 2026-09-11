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

    report = recover_backups(layout, keep_segments=True)

    assert report.restored == ["a.mp4"]
    assert (layout.root / "a.mp4").read_text(encoding="utf-8") == "x"
    assert not (layout.backups_dir / "a.mp4").exists()


def test_recover_backups_keeps_segments_when_segmented(layout: IvdcLayout) -> None:
    layout.backups_dir.mkdir(parents=True)
    (layout.backups_dir / "a.mp4").write_text("x", encoding="utf-8")
    layout.seg_dir("a.mp4").mkdir(parents=True)
    (layout.seg("a.mp4", 0)).write_text("seg", encoding="utf-8")

    recover_backups(layout, keep_segments=True)

    assert layout.seg_dir("a.mp4").is_dir()


def test_recover_backups_discards_segments_when_not_segmented(layout: IvdcLayout) -> None:
    layout.backups_dir.mkdir(parents=True)
    (layout.backups_dir / "a.mp4").write_text("x", encoding="utf-8")
    layout.seg_dir("a.mp4").mkdir(parents=True)
    (layout.seg("a.mp4", 0)).write_text("seg", encoding="utf-8")

    report = recover_backups(layout, keep_segments=False)

    assert report.discarded_segments == ["a.mp4"]
    assert not layout.seg_dir("a.mp4").exists()


def test_recover_backups_leaves_finished_files_alone(layout: IvdcLayout) -> None:
    layout.backups_dir.mkdir(parents=True)
    (layout.backups_dir / "a.mp4").write_text("backup", encoding="utf-8")
    (layout.root / "a.mp4").write_text("final", encoding="utf-8")

    report = recover_backups(layout, keep_segments=True)

    assert report.restored == []
    assert (layout.root / "a.mp4").read_text(encoding="utf-8") == "final"
    assert (layout.backups_dir / "a.mp4").exists()


def test_recover_backups_without_state_dir_is_a_noop(layout: IvdcLayout) -> None:
    assert recover_backups(layout, keep_segments=True).restored == []


def test_restore_failure_is_recorded_not_swallowed(layout: IvdcLayout, monkeypatch) -> None:
    """还原失败意味着工作目录里那个文件是缺的，必须出现在报告里。"""
    layout.backups_dir.mkdir(parents=True)
    (layout.backups_dir / "a.mp4").write_text("x", encoding="utf-8")

    def boom(source: object, target: object) -> None:
        raise OSError("文件被占用")

    monkeypatch.setattr("ivdc.recovery.os.replace", boom)

    report = recover_backups(layout, keep_segments=True)

    assert report.restored == []
    assert len(report.failures) == 1
    assert "还原备份 a.mp4 失败" in report.failures[0]
    assert (layout.backups_dir / "a.mp4").exists()


def test_failed_segment_cleanup_is_recorded(layout: IvdcLayout, monkeypatch) -> None:
    layout.backups_dir.mkdir(parents=True)
    (layout.backups_dir / "a.mp4").write_text("x", encoding="utf-8")
    layout.seg_dir("a.mp4").mkdir(parents=True)
    monkeypatch.setattr("ivdc.recovery.remove_tree", lambda path: False)

    report = recover_backups(layout, keep_segments=False)

    assert report.discarded_segments == []
    assert len(report.failures) == 1
    assert layout.seg_dir("a.mp4").is_dir()
