"""用户配置：默认在 ``%APPDATA%\\ivdc\\config.json``（非 Windows 回落到家目录）。

配置不能放在包目录里：pipx / uv 安装出来的包位于虚拟环境内，升级时会整个被覆盖，
放在包里的配置会跟着一起消失（见 SPEC.md §6.4）。

JSON 里用 camelCase 键——这是外部格式，改键名会让磁盘上已有的配置读不出来；
代码内部一律用 snake_case 属性访问。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "CONFIG_ENV_VAR",
    "DEFAULT_CONFIG_TEXT",
    "DEFAULT_IMPERSONATE_TARGET",
    "KEY_IMPERSONATE_DOMAINS",
    "KEY_IMPERSONATE_TARGET",
    "Config",
    "default_config_path",
    "ensure_config_file",
    "load_config",
]

CONFIG_ENV_VAR = "IVDC_CONFIG"

# JSON 里的键名。默认模板由这些常量生成，改键名时不会再出现「解析代码改了、
# 写出的模板没改」的漂移。
KEY_IMPERSONATE_DOMAINS = "impersonateDomains"
KEY_IMPERSONATE_TARGET = "impersonateTarget"

DEFAULT_IMPERSONATE_TARGET = "chrome-136"

DEFAULT_CONFIG_TEXT = (
    json.dumps(
        {
            KEY_IMPERSONATE_DOMAINS: [],
            KEY_IMPERSONATE_TARGET: DEFAULT_IMPERSONATE_TARGET,
        },
        ensure_ascii=False,
        indent=2,
    )
    + "\n"
)


@dataclass(frozen=True)
class Config:
    """用户级配置。"""

    impersonate_domains: tuple[str, ...] = ()
    impersonate_target: str = DEFAULT_IMPERSONATE_TARGET

    def with_domains(self, domains: tuple[str, ...]) -> Config:
        return replace(self, impersonate_domains=domains)


def default_config_path() -> Path:
    """配置文件的默认位置；``IVDC_CONFIG`` 环境变量可覆盖（测试与多环境用）。"""
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override).expanduser()
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "ivdc" / "config.json"
    return Path.home() / ".config" / "ivdc" / "config.json"


def _parse(payload: Any) -> Config:
    if not isinstance(payload, Mapping):
        return Config()
    raw_domains = payload.get(KEY_IMPERSONATE_DOMAINS)
    domains: tuple[str, ...] = ()
    if isinstance(raw_domains, str):
        domains = (raw_domains,)
    elif isinstance(raw_domains, (list, tuple)):
        domains = tuple(str(item).strip() for item in raw_domains if str(item).strip())
    target = payload.get(KEY_IMPERSONATE_TARGET)
    return Config(
        impersonate_domains=domains,
        impersonate_target=str(target) if target else DEFAULT_IMPERSONATE_TARGET,
    )


def load_config(path: Path | None = None) -> Config:
    """读取配置；文件缺失或损坏时回落到默认值，绝不因此中断一次任务。"""
    target = path or default_config_path()
    try:
        text = target.read_text(encoding="utf-8-sig")
    except OSError:
        return Config()
    try:
        return _parse(json.loads(text))
    except (json.JSONDecodeError, ValueError):
        return Config()


def ensure_config_file(path: Path | None = None) -> Path:
    """确保配置文件存在（首次运行时写出带注释的默认值），返回其路径。"""
    target = path or default_config_path()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8", newline="\n")
    return target
