"""展示层使用有类型的进度键，文件名中的分隔字符不能混淆任务。"""

from ivdc.fs import FileRecord
from ivdc.optimization_plan import OptimizationPlan, OptimizeOptions, VideoTask
from ivdc.progress import ProgressDisplay
from ivdc.views import OptimizationDisplay


def test_file_name_prefix_cannot_remove_another_progress_task(console_pair, tmp_path):
    console, _ = console_pair
    with ProgressDisplay(console) as display:
        view = OptimizationDisplay(OptimizeOptions(tmp_path), console, display)
        tasks = tuple(
            VideoTask(name, tmp_path / name, name, 1) for name in ("a.mp4", "a.mp4:other.mp4")
        )
        view.planned(OptimizationPlan(tasks, (), (), ()), "hevc")
        view.finished(FileRecord("a.mp4", "ok"))
        view.progress(("a.mp4:other.mp4", None), 0.5)
        assert ("a.mp4:other.mp4", None) in view.tasks
        assert ("a.mp4", None) not in view.tasks
