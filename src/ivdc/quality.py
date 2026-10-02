"""画质档位及实际编码器的数值策略；配置解析和参数装配共用。"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class Quality(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


@dataclass(frozen=True)
class QualityLevels:
    high: int
    medium: int
    low: int

    def value(self, quality: Quality) -> int:
        match quality:
            case Quality.high:
                return self.high
            case Quality.medium:
                return self.medium
            case Quality.low:
                return self.low


@dataclass(frozen=True)
class QualitySpec:
    parameter: str
    minimum: int
    maximum: int
    defaults: QualityLevels


# 初始档位是项目策略，不声称不同编码器之间画质等价；依据见 README。
# SVT 的 0 在 FFmpeg 7.1 中是未指定；NVENC 的 0 是自动，均不能作为固定档位。
QUALITY_SPECS: Mapping[str, QualitySpec] = MappingProxyType(
    {
        "libx265": QualitySpec("crf", 0, 51, QualityLevels(24, 28, 32)),
        "libsvtav1": QualitySpec("crf", 1, 63, QualityLevels(28, 35, 42)),
        "libvpx-vp9": QualitySpec("crf", 0, 63, QualityLevels(25, 31, 37)),
        "hevc_nvenc": QualitySpec("cq", 1, 51, QualityLevels(23, 28, 33)),
        "av1_nvenc": QualitySpec("cq", 1, 63, QualityLevels(25, 30, 35)),
    }
)
DEFAULT_QUALITY_PROFILES: Mapping[str, QualityLevels] = MappingProxyType(
    {encoder: spec.defaults for encoder, spec in QUALITY_SPECS.items()}
)


def validate_quality_levels(encoder: str, levels: QualityLevels) -> None:
    spec = QUALITY_SPECS[encoder]
    for quality in Quality:
        value = levels.value(quality)
        if type(value) is not int or not spec.minimum <= value <= spec.maximum:
            raise ValueError(
                f"quality_profiles.{encoder}.{quality} 必须是 {spec.minimum}–{spec.maximum} 的整数"
            )
    if not levels.high < levels.medium < levels.low:
        raise ValueError(
            f"quality_profiles.{encoder} 必须满足 high < medium < low（数值越小画质越高）"
        )


def parse_quality_profiles(payload: object) -> Mapping[str, QualityLevels]:
    """逐档覆盖默认值；所有显式配置都校验，包括当前未选中的编码器。"""
    if not isinstance(payload, dict):
        raise ValueError("quality_profiles 必须是对象")
    unknown = payload.keys() - QUALITY_SPECS.keys()
    if unknown:
        raise ValueError(f"quality_profiles 中未知编码器: {', '.join(sorted(unknown))}")
    profiles = dict(DEFAULT_QUALITY_PROFILES)
    for encoder, overrides in payload.items():
        if not isinstance(overrides, dict):
            raise ValueError(f"quality_profiles.{encoder} 必须是对象")
        unknown_levels = overrides.keys() - {quality.value for quality in Quality}
        if unknown_levels:
            raise ValueError(
                f"quality_profiles.{encoder} 中未知档位: {', '.join(sorted(unknown_levels))}"
            )
        defaults = profiles[encoder]
        levels = QualityLevels(
            overrides.get("high", defaults.high),
            overrides.get("medium", defaults.medium),
            overrides.get("low", defaults.low),
        )
        validate_quality_levels(encoder, levels)
        profiles[encoder] = levels
    return MappingProxyType(profiles)
