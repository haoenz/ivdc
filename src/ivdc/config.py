"""平台配置目录、严格 JSON 校验和显式初始化。"""

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ivdc.errors import SetupError
from ivdc.quality import DEFAULT_QUALITY_PROFILES, QualityLevels, parse_quality_profiles
from ivdc.store import atomic_write_text

CONFIG_ENV_VAR = "IVDC_CONFIG"
DEFAULT_IMPERSONATE_TARGET = "chrome-136"
DEFAULT_CONFIG_TEXT = (
    json.dumps(
        {
            "impersonate_domains": [],
            "impersonate_target": DEFAULT_IMPERSONATE_TARGET,
            "quality_profiles": {
                encoder: asdict(levels) for encoder, levels in DEFAULT_QUALITY_PROFILES.items()
            },
        },
        ensure_ascii=False,
        indent=2,
    )
    + "\n"
)


@dataclass(frozen=True)
class Config:
    impersonate_domains: tuple[str, ...] = ()
    impersonate_target: str = DEFAULT_IMPERSONATE_TARGET
    quality_profiles: Mapping[str, QualityLevels] = field(
        default_factory=lambda: DEFAULT_QUALITY_PROFILES
    )


def default_config_path() -> Path:
    if override := os.environ.get(CONFIG_ENV_VAR):
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return base / "ivdc" / "config.json"


def load_config(path: Path | None = None) -> Config:
    target = path or default_config_path()
    try:
        text = target.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return Config()
    except (OSError, UnicodeError) as exc:
        raise SetupError(f"无法读取配置 {target}: {exc}") from exc
    try:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("根节点必须是对象")
        unknown = payload.keys() - {"impersonate_domains", "impersonate_target", "quality_profiles"}
        if unknown:
            raise ValueError(f"未知配置键: {', '.join(sorted(unknown))}；请参阅 MIGRATION.md")
        domains = payload.get("impersonate_domains", [])
        if not isinstance(domains, list) or any(
            not isinstance(item, str) or not item.strip() for item in domains
        ):
            raise ValueError("impersonate_domains 必须是非空字符串数组")
        target_name = payload.get("impersonate_target", DEFAULT_IMPERSONATE_TARGET)
        if not isinstance(target_name, str) or not target_name.strip():
            raise ValueError("impersonate_target 必须是非空字符串")
        profiles = parse_quality_profiles(payload.get("quality_profiles", {}))
        return Config(tuple(item.strip() for item in domains), target_name.strip(), profiles)
    except ValueError as exc:
        raise SetupError(f"配置无效 {target}: {exc}") from exc


def ensure_config_file(path: Path | None = None) -> Path:
    target = path or default_config_path()
    if not target.exists():
        atomic_write_text(target, DEFAULT_CONFIG_TEXT)
    return target
