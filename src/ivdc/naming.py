"""文件名清洗与随机标识（纯逻辑，不碰 I/O）。

单一数据源：下载与压制两条命令共用这里的一套规则。
"""

from __future__ import annotations

import random
import re
import string

# 三类字符分开处理，顺序不能换：
#   1. 乱码控制字符（含 \x00 与分隔符 \x1c-\x1f）→ 下划线；制表/换行算真空白，留给下一步
#   2. 真空白（空格、制表、换行等）折叠成单个空格
#   3. Windows 非法标点 → 下划线
_CONTROL = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_ILLEGAL_PUNCT = re.compile(r'[<>:"/\\|?*]')

# Windows 保留设备名（不区分大小写，且带扩展名时同样保留，如 CON.txt）
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_FALLBACK_NAME = "_"
"""清洗后为空、或撞上保留设备名时使用的兜底名。"""

TAG_ALPHABET = string.ascii_lowercase + string.digits
TAG_LENGTH = 6

__all__ = ["TAG_ALPHABET", "TAG_LENGTH", "new_tag", "sanitize_filename"]


def new_tag(length: int = TAG_LENGTH, rng: random.Random | None = None) -> str:
    """生成小写字母 + 数字的随机串，``--mask`` 下用来代替真实文件名。

    默认取 ``SystemRandom``：不共享种子，并行场景下多个工作线程不会拿到同一序列。
    """
    generator: random.Random = rng or random.SystemRandom()
    return "".join(generator.choice(TAG_ALPHABET) for _ in range(length))


def sanitize_filename(name: str) -> str:
    """把任意标题清洗成 Windows 上合法的文件名主干（不含扩展名）。

    清单标题和下载器提供的标题使用相同规则，保留空格以便阅读。
    """
    cleaned = _CONTROL.sub("_", name)
    cleaned = _WHITESPACE.sub(" ", cleaned)
    cleaned = _ILLEGAL_PUNCT.sub("_", cleaned)
    cleaned = cleaned.strip()
    # Windows 不允许文件名以点或空格结尾
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        return _FALLBACK_NAME
    # CON / CON.txt / con.tar.gz 都是保留名，加前缀规避
    if cleaned.split(".", 1)[0].upper() in _RESERVED:
        cleaned = _FALLBACK_NAME + cleaned
    return cleaned
