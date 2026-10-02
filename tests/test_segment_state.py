"""完成凭据绑定文件内容、任务身份和分片索引。"""

import json
import os

from ivdc.encode import EncodingConfig
from ivdc.fs import IvdcLayout
from ivdc.optimization_plan import VideoTask
from ivdc.plan import Segment
from ivdc.segment_state import SegmentContext, prepare_segments


def test_previous_stream_policy_archives_segments_without_deleting_them(tmp_path):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"original")
    layout = IvdcLayout(tmp_path)
    task = VideoTask(source.name, source, source.name, 1, (Segment(0, 0, 1),))
    config = EncodingConfig("x265", "hevc")
    prepare_segments(layout, config, task)
    metadata = layout.seg_dir(source.name) / "resume.json"
    previous = json.loads(metadata.read_text())
    del previous["stream_policy"]
    metadata.write_text(json.dumps(previous))
    layout.seg(source.name, 0).write_bytes(b"old segment")
    prepare_segments(layout, config, task)
    archived = list(layout.seg_dir(source.name).parent.glob("*.resume-*/seg_000.mp4"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == b"old segment"
    assert not layout.seg(source.name, 0).exists()


def test_same_size_and_timestamp_modification_invalidates_receipt(tmp_path):
    candidate, target = tmp_path / "partial.mp4", tmp_path / "seg_000.mp4"
    candidate.write_bytes(b"complete data")
    context = SegmentContext("task identity")
    context.publish(candidate, target, 0)
    assert context.completed(target, 0)
    stat = target.stat()
    target.write_bytes(b"modified data")
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert target.stat().st_size == stat.st_size
    assert not context.completed(target, 0)


def test_completion_receipt_does_not_transfer_to_another_task_or_index(tmp_path):
    candidate, target = tmp_path / "partial.mp4", tmp_path / "seg_000.mp4"
    candidate.write_bytes(b"complete")
    context = SegmentContext("original task")
    context.publish(candidate, target, 0)
    assert context.completed(target, 0)
    assert not context.completed(target, 1)
    assert not SegmentContext("changed settings").completed(target, 0)
