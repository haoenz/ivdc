"""磁盘读写：原子写、行读写、清单、临时目录清理、目录删除。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ivdc.errors import StorageError
from ivdc.fs import STATUS_OK, FileRecord, IvdcLayout, Manifest
from ivdc.store import (
    append_line,
    atomic_write_text,
    clear_tmp,
    dir_stats,
    load_manifest,
    read_lines,
    read_nonempty_lines,
    remove_tree,
    resolve_log_path,
    save_manifest,
    write_lines,
)


@pytest.fixture
def layout(tmp_path: Path) -> IvdcLayout:
    root = tmp_path / "videos"
    root.mkdir()
    return IvdcLayout(root)


# ---- 原子写 ---------------------------------------------------------------


def test_atomic_write_replaces_content(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_creates_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "a.txt"
    atomic_write_text(target, "x")
    assert target.read_text(encoding="utf-8") == "x"


def test_atomic_write_leaves_no_tmp_behind(tmp_path: Path) -> None:
    atomic_write_text(tmp_path / "a.txt", "x")
    assert [item.name for item in tmp_path.iterdir()] == ["a.txt"]


def test_atomic_write_cleans_tmp_when_replace_fails(tmp_path: Path, monkeypatch) -> None:
    def boom(source: str, target: str) -> None:
        raise OSError("replace 失败")

    monkeypatch.setattr("ivdc.store.os.replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(tmp_path / "a.txt", "x")
    assert list(tmp_path.iterdir()) == []


# ---- 行读写 ---------------------------------------------------------------


def test_read_lines_strips_bom(tmp_path: Path) -> None:
    path = tmp_path / "t.txt"
    path.write_bytes("标题\n".encode("utf-8-sig") + b"two\n")
    assert read_lines(path) == ["标题", "two"]


def test_read_lines_on_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_lines(tmp_path / "nope.txt")


def test_read_nonempty_lines_drops_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "t.txt"
    path.write_text("a\n\n   \nb\n", encoding="utf-8")
    assert read_nonempty_lines(path) == ["a", "b"]


def test_write_lines_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "t.txt"
    write_lines(path, ["一", "二"])
    assert path.read_text(encoding="utf-8") == "一\n二\n"
    assert read_lines(path) == ["一", "二"]


def test_write_lines_empty_list_writes_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "t.txt"
    path.write_text("旧\n", encoding="utf-8")
    write_lines(path, [])
    assert path.read_text(encoding="utf-8") == ""


def test_append_line_creates_and_appends(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "a.log"
    append_line(path, "one")
    append_line(path, "two")
    assert path.read_text(encoding="utf-8") == "one\ntwo\n"


# ---- 清单 -----------------------------------------------------------------


def test_manifest_round_trip(layout: IvdcLayout) -> None:
    manifest = Manifest()
    manifest.record(FileRecord(name="a.mp4", status=STATUS_OK, new_bytes=12))
    save_manifest(layout, manifest)

    restored = load_manifest(layout)
    assert restored.is_done("a.mp4") is True
    assert restored.get("a.mp4").new_bytes == 12


def test_load_manifest_on_missing_or_corrupt_file(layout: IvdcLayout) -> None:
    assert load_manifest(layout).entries == {}
    layout.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    layout.manifest_path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(StorageError, match="manifest 无效"):
        load_manifest(layout)


# ---- 临时目录 -------------------------------------------------------------


def test_clear_tmp_only_touches_state_directory(layout: IvdcLayout) -> None:
    layout.tmp_dir.mkdir(parents=True)
    (layout.tmp_dir / "a.mp4").write_text("partial", encoding="utf-8")
    (layout.tmp_dir / "b.mp4").write_text("partial", encoding="utf-8")
    # 下载任务的临时文件就在工作目录里，绝不能被顺手删掉（按 *.tmp 通配清理会误删）
    stray = layout.root / "pending_downloads.txt.tmp"
    stray.write_text("keep me", encoding="utf-8")

    result = clear_tmp(layout)

    assert (result.removed, result.failed) == (2, 0)
    assert list(layout.tmp_dir.iterdir()) == []
    assert stray.exists()


def test_clear_tmp_without_directory_returns_zero(layout: IvdcLayout) -> None:
    assert clear_tmp(layout).removed == 0


def test_clear_tmp_counts_what_it_could_not_delete(layout: IvdcLayout, monkeypatch) -> None:
    layout.tmp_dir.mkdir(parents=True)
    (layout.tmp_dir / "busy.mp4").write_text("partial", encoding="utf-8")
    (layout.tmp_dir / "free.mp4").write_text("partial", encoding="utf-8")

    real_unlink = Path.unlink

    def flaky(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "busy.mp4":
            raise OSError("文件被占用")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", flaky)

    result = clear_tmp(layout)

    assert (result.removed, result.failed) == (1, 1)
    assert (layout.tmp_dir / "busy.mp4").exists()


# ---- 目录删除 -------------------------------------------------------------


def test_remove_tree_reports_whether_it_succeeded(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "d"
    target.mkdir()
    (target / "a").write_text("x", encoding="utf-8")

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("目录被占用")

    assert remove_tree(tmp_path / "missing") is True  # 本来就没有，算成功
    monkeypatch.setattr("ivdc.store.shutil.rmtree", boom)
    assert remove_tree(target) is False
    assert target.exists()


def test_dir_stats_counts_files_and_bytes(tmp_path: Path) -> None:
    target = tmp_path / "d"
    (target / "sub").mkdir(parents=True)
    (target / "a").write_text("12345", encoding="utf-8")
    (target / "sub" / "b").write_text("12", encoding="utf-8")

    assert dir_stats(target) == (2, 7)
    assert dir_stats(target / "a") == (1, 5)
    assert dir_stats(tmp_path / "missing") == (0, 0)


# ---- 日志路径 -------------------------------------------------------------


def test_resolve_log_path_default_is_per_day(layout: IvdcLayout) -> None:
    assert resolve_log_path(layout, None, day=date(2026, 9, 11)) == (
        layout.root / ".ivdc" / "logs" / "2026-09-11.log"
    )


def test_resolve_log_path_relative_is_resolved_against_cwd(
    layout: IvdcLayout, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert resolve_log_path(layout, Path("custom.log")) == tmp_path / "custom.log"


def test_resolve_log_path_absolute_is_kept(layout: IvdcLayout, tmp_path: Path) -> None:
    absolute = tmp_path / "elsewhere" / "x.log"
    assert resolve_log_path(layout, absolute) == absolute


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"version": 99},
        {"entries": []},
        {"entries": {"a": {"name": "a", "status": "unknown"}}},
        {"entries": {"a": {"name": "a", "status": "ok", "seconds": float("nan")}}},
        {"entries": {"a": {"name": "a", "status": "ok", "old_bytes": "1"}}},
        {"entries": {"a": {"name": "b", "status": "ok"}}},
    ],
)
def test_invalid_manifest_is_preserved(layout, payload):
    import json

    layout.state_dir.mkdir()
    text = json.dumps(payload)
    layout.manifest_path.write_text(text, encoding="utf-8")
    with pytest.raises(StorageError):
        load_manifest(layout)
    assert layout.manifest_path.read_text(encoding="utf-8") == text


def test_read_permission_failure_is_not_empty(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(PermissionError):
        read_lines(tmp_path / "tasks.txt")


def test_atomic_write_failure_keeps_existing_contents(tmp_path, monkeypatch):
    path = tmp_path / "tasks.txt"
    path.write_text("keep", encoding="utf-8")

    def denied(*args):
        raise PermissionError("denied")

    monkeypatch.setattr("ivdc.store.os.replace", denied)
    with pytest.raises(PermissionError):
        atomic_write_text(path, "new")
    assert path.read_text(encoding="utf-8") == "keep"
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_writes_use_unique_temporary_files(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "shared.json"
    values = [str(index) * 10000 for index in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda text: atomic_write_text(path, text), values))
    assert path.read_text(encoding="utf-8") in values
    assert list(tmp_path.iterdir()) == [path]
