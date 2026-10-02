"""磁盘布局路径、分片命名与 manifest 序列化。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ivdc.fs import (
    CONCAT_LIST_NAME,
    MANIFEST_VERSION,
    STATE_DIR_NAME,
    STATUS_FAILED,
    STATUS_OK,
    FileRecord,
    IvdcLayout,
    Manifest,
    concat_lines,
    seg_filename,
)


@pytest.fixture
def layout() -> IvdcLayout:
    return IvdcLayout(Path("/videos"))


def test_state_dir_is_hidden_folder_in_video_dir(layout: IvdcLayout) -> None:
    assert STATE_DIR_NAME == ".ivdc"
    assert layout.state_dir == Path("/videos/.ivdc")


def test_subdirectory_paths(layout: IvdcLayout) -> None:
    assert layout.backups_dir == Path("/videos/.ivdc/backups")
    assert layout.segs_dir == Path("/videos/.ivdc/segs")
    assert layout.tmp_dir == Path("/videos/.ivdc/tmp")
    assert layout.logs_dir == Path("/videos/.ivdc/logs")
    assert layout.manifest_path == Path("/videos/.ivdc/manifest.json")


def test_per_file_paths(layout: IvdcLayout) -> None:
    assert layout.backup("a.mp4") == Path("/videos/.ivdc/backups/a.mp4")
    assert layout.tmp("a.mp4") == Path("/videos/.ivdc/tmp/a.mp4")
    assert layout.seg_dir("a.mp4") == Path("/videos/.ivdc/segs/a.mp4")
    assert layout.seg("a.mp4", 2) == Path("/videos/.ivdc/segs/a.mp4/seg_002.mp4")


def test_log_path_is_per_day(layout: IvdcLayout) -> None:
    assert layout.log(date(2026, 9, 11)) == Path("/videos/.ivdc/logs/2026-09-11.log")


def test_state_dir_never_collides_with_globbed_videos(layout: IvdcLayout) -> None:
    # 状态目录名以点开头，不会被 *.mp4 之类的过滤规则命中
    assert not Path(STATE_DIR_NAME).match("*.mp4")


@pytest.mark.parametrize(
    ("index", "expected"), [(0, "seg_000.mp4"), (12, "seg_012.mp4"), (999, "seg_999.mp4")]
)
def test_seg_filename(index: int, expected: str) -> None:
    assert seg_filename(index) == expected


def test_concat_lines(layout: IvdcLayout) -> None:
    assert concat_lines(3) == ["file 'seg_000.mp4'", "file 'seg_001.mp4'", "file 'seg_002.mp4'"]
    assert CONCAT_LIST_NAME == "concat_list.txt"


def test_manifest_round_trip_through_json() -> None:
    manifest = Manifest()
    manifest.record(
        FileRecord(
            name="a.mp4",
            status=STATUS_OK,
            codec="hevc",
            old_bytes=100,
            new_bytes=50,
            seconds=12.3456,
            finished_at="2026-09-11T12:00:00",
        )
    )

    payload = json.loads(json.dumps(manifest.as_dict()))
    assert payload["version"] == MANIFEST_VERSION

    restored = Manifest.from_dict(payload)
    record = restored.get("a.mp4")
    assert record is not None
    assert record.new_bytes == 50
    assert record.seconds == pytest.approx(12.346)
    assert restored.is_done("a.mp4") is True


def test_manifest_failed_record_is_not_done() -> None:
    manifest = Manifest()
    manifest.record(FileRecord(name="b.mp4", status=STATUS_FAILED, message="部分分片缺失"))
    assert manifest.is_done("b.mp4") is False
    assert manifest.get("b.mp4").message == "部分分片缺失"


def test_manifest_handles_empty_and_partial_payloads() -> None:
    assert Manifest.from_dict({}).entries == {}
    assert Manifest.from_dict({"entries": {}}).is_done("x.mp4") is False
    with pytest.raises(ValueError):
        Manifest.from_dict({"entries": {"x.mp4": {}}})
