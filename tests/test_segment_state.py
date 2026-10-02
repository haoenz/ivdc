"""完成凭据绑定文件内容、任务身份和分片索引。"""

import os

from ivdc.segment_state import SegmentContext


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
