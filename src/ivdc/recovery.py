"""中断残留的恢复策略。

一次运行被打断（Ctrl+C、断电、任务被杀）会留下两类残留：源文件已经移进
``.ivdc/backups/`` 而成片还没落地，以及 ``.ivdc/segs/`` 下的半成品分片。这里负责
把前者还原回工作目录，并决定后者是留（分片模式，下次续压）还是丢（整片模式已无用）。

单独成模块而不是留在 :mod:`ivdc.store`：它是一条带安全语义的恢复策略，与那边的
字节级读写工具不是一回事。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ivdc.fs import IvdcLayout
from ivdc.store import remove_tree

__all__ = ["RecoveryReport", "recover_backups"]


@dataclass
class RecoveryReport:
    """启动时对上次中断残留的处理结果。

    ``failures`` 里的每条都已带上下文，调用方直接打印即可——还原失败意味着工作目录
    里那个文件仍然是缺席状态，必须让用户看见，不能静默跳过。
    """

    restored: list[str] = field(default_factory=list)
    discarded_segments: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def recover_backups(layout: IvdcLayout, *, keep_segments: bool) -> RecoveryReport:
    """把上次中断留下的备份还原回工作目录。

    备份还在而成片不在，说明上次是在「源文件已移入备份、输出还没落地」之间被中断的。
    还原之后工作目录里永远只见成片，不会留下容易让人误判的 ``.original`` 之类的残骸。

    分片模式下保留分片目录供下次续压；整片模式下分片已经没用，顺手清掉。
    """
    report = RecoveryReport()
    backups_dir = layout.backups_dir
    if not backups_dir.is_dir():
        return report

    for entry in sorted(backups_dir.iterdir()):
        if not entry.is_file():
            continue
        name = entry.name
        final_path = layout.root / name
        if final_path.exists():
            continue
        try:
            os.replace(entry, final_path)
        except OSError as exc:
            report.failures.append(f"还原备份 {name} 失败: {exc}")
            continue
        report.restored.append(name)
        seg_dir = layout.seg_dir(name)
        if not keep_segments and seg_dir.is_dir():
            if remove_tree(seg_dir):
                report.discarded_segments.append(name)
            else:
                report.failures.append(f"删除无用的分片目录 {name} 失败，仍留在 {seg_dir}")
    return report
