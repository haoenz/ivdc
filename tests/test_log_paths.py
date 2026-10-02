"""日志路径冲突必须在任何写入和媒体处理之前被拒绝。"""

import os
from pathlib import Path

import pytest

from ivdc.errors import SetupError
from ivdc.fs import IvdcLayout
from ivdc.optimization_plan import OptimizeOptions
from ivdc.optimize import run_optimize
from ivdc.store import validate_log_path


@pytest.fixture
def layout(tmp_path):
    root = tmp_path / "videos"
    root.mkdir()
    layout = IvdcLayout(root)
    (root / "source.mp4").write_bytes(b"original video")
    layout.backups_dir.mkdir(parents=True)
    layout.backup("source.mp4").write_bytes(b"earlier backup")
    layout.backup("orphan.mp4").write_bytes(b"recoverable source")
    layout.backup("restored/sub.mp4").parent.mkdir()
    layout.backup("restored/sub.mp4").write_bytes(b"nested recoverable source")
    layout.tmp_dir.mkdir()
    layout.tmp("source.mp4").write_bytes(b"temporary output")
    layout.seg_dir("source.mp4").mkdir(parents=True)
    layout.seg("source.mp4", 0).write_bytes(b"recoverable segment")
    layout.manifest_path.write_text('{"version":1,"entries":{}}')
    return layout


def snapshot(root):
    return {
        path.relative_to(root): (
            path.lstat().st_mtime_ns,
            os.readlink(path)
            if path.is_symlink()
            else path.read_bytes()
            if path.is_file()
            else None,
        )
        for path in root.rglob("*")
    }


def assert_rejected_without_writes(layout, log, monkeypatch, *, dry_run=False):
    before = snapshot(layout.root.parent)

    def forbidden(*args, **kwargs):
        pytest.fail("log conflict must be rejected before processing or writing")

    with monkeypatch.context() as patch:
        patch.setattr("ivdc.optimize.find_toolchain", forbidden)
        patch.setattr("ivdc.optimize.recover_backups", forbidden)
        patch.setattr("ivdc.optimize.clear_tmp", forbidden)
        patch.setattr("ivdc.records.ResultRecorder.__enter__", forbidden)
        with pytest.raises(SetupError, match="日志"):
            run_optimize(OptimizeOptions(layout.root, log_file=log, dry_run=dry_run))
    assert snapshot(layout.root.parent) == before
    assert not (layout.root / "orphan.mp4").exists()
    assert not (layout.root / "restored").exists()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    "relative",
    [
        "source.mp4",
        "orphan.mp4",
        "orphan.mp4/log.txt",
        "restored",
        ".ivdc",
        ".ivdc/backups/source.mp4",
        ".ivdc/backups/new/nothing.mp4",
        ".ivdc/tmp/new.log",
        ".ivdc/segs/source.mp4/seg_000.mp4",
        ".ivdc/segs/source.mp4/seg_000.complete.json",
        ".ivdc/segs/source.mp4/resume.json",
        ".ivdc/manifest.json",
        ".ivdc/manifest.json/log.txt",
        ".ivdc/logs",
        ".ivdc/unreserved.log",
    ],
)
def test_conflicting_log_paths_are_rejected_without_mutation(
    layout, monkeypatch, relative, dry_run
):
    assert_rejected_without_writes(layout, layout.root / relative, monkeypatch, dry_run=dry_run)


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
@pytest.mark.parametrize(
    "relative",
    [
        "source.mp4",
        ".ivdc/backups/source.mp4",
        ".ivdc/manifest.json",
        ".ivdc/segs/source.mp4/seg_000.mp4",
        ".ivdc/tmp/source.mp4",
    ],
)
def test_file_aliases_cannot_bypass_conflict_check(layout, monkeypatch, kind, relative):
    alias = layout.root.parent / "alias.log"
    original = layout.root / relative
    if kind == "symlink":
        alias.symlink_to(original)
    else:
        alias.hardlink_to(original)
    assert_rejected_without_writes(layout, alias, monkeypatch)


def test_parent_symlink_to_backup_directory_is_rejected(layout, monkeypatch):
    alias = layout.root.parent / "backup-alias"
    alias.symlink_to(layout.backups_dir, target_is_directory=True)
    assert_rejected_without_writes(layout, alias / "new.log", monkeypatch)


def test_relative_path_with_parent_components_is_rejected(layout, monkeypatch):
    monkeypatch.chdir(layout.root)
    assert_rejected_without_writes(
        layout,
        Path(".ivdc/backups/../backups/new.mp4"),
        monkeypatch,
    )


def test_future_state_conflict_is_rejected_before_state_directory_exists(tmp_path, monkeypatch):
    layout = IvdcLayout(tmp_path / "videos")
    layout.root.mkdir()
    (layout.root / "source.mp4").write_bytes(b"original")
    assert_rejected_without_writes(layout, layout.backup("source.mp4"), monkeypatch)
    assert not layout.state_dir.exists()


def test_default_log_hardlinked_to_source_is_rejected(layout, monkeypatch):
    from ivdc.store import resolve_log_path

    log = resolve_log_path(layout, None)
    log.parent.mkdir()
    log.hardlink_to(layout.root / "source.mp4")
    assert_rejected_without_writes(layout, None, monkeypatch)


def test_valid_paths_are_checked_without_creating_anything(layout):
    before = snapshot(layout.root.parent)
    for path in (
        layout.logs_dir / "custom.log",
        layout.root / "custom.log",
        layout.root.parent / "videos-logs" / "run.log",
    ):
        assert validate_log_path(layout, path, [layout.root / "source.mp4"]) == path.resolve()
    assert snapshot(layout.root.parent) == before


def test_alias_to_an_ordinary_existing_log_is_allowed(layout):
    log = layout.root.parent / "existing.log"
    log.write_text("previous records\n")
    alias = layout.root.parent / "alias.log"
    alias.symlink_to(log)
    before = snapshot(layout.root.parent)
    assert validate_log_path(layout, alias, [layout.root / "source.mp4"]) == log
    assert snapshot(layout.root.parent) == before
