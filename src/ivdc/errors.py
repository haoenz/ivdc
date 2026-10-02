"""业务异常；退出码与终端提示由 CLI 决定。"""


class SetupError(ValueError):
    """参数、配置或必需工具不可用。"""


class StorageError(RuntimeError):
    """持久化数据损坏或写入失败，不能作为空数据继续。"""


class MediaError(RuntimeError):
    """单项媒体探测、编码或提交失败。"""
