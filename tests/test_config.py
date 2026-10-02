"""用户配置的严格校验、平台路径与显式初始化。"""

from __future__ import annotations

import json

import pytest

from ivdc.config import (
    CONFIG_ENV_VAR,
    DEFAULT_IMPERSONATE_TARGET,
    Config,
    default_config_path,
    ensure_config_file,
    load_config,
)
from ivdc.errors import SetupError
from ivdc.quality import DEFAULT_QUALITY_PROFILES, QUALITY_SPECS, QualityLevels


def test_env_var_overrides_default_location(tmp_path, monkeypatch) -> None:
    target = tmp_path / "custom.json"
    monkeypatch.setenv(CONFIG_ENV_VAR, str(target))
    assert default_config_path() == target


def test_missing_file_falls_back_to_defaults(tmp_path) -> None:
    config = load_config(tmp_path / "nope.json")
    assert config == Config()
    assert config.impersonate_target == DEFAULT_IMPERSONATE_TARGET
    assert config.impersonate_domains == ()


def test_parses_domains_and_target(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {"impersonate_domains": ["a.com", " b.com "], "impersonate_target": "chrome-120"}
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.impersonate_domains == ("a.com", "b.com")
    assert config.impersonate_target == "chrome-120"


def test_string_domain_is_rejected(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"impersonate_domains": "only.com"}), encoding="utf-8")
    with pytest.raises(SetupError, match="字符串数组"):
        load_config(path)


def test_corrupt_file_reports_error(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(SetupError):
        load_config(path)


def test_non_mapping_payload_reports_error(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(SetupError):
        load_config(path)


def test_ensure_config_file_writes_defaults_once(tmp_path) -> None:
    target = tmp_path / "nested" / "config.json"
    created = ensure_config_file(target)
    assert created == target
    assert target.exists()
    assert load_config(target) == Config()

    target.write_text(json.dumps({"impersonate_domains": ["x.com"]}), encoding="utf-8")
    ensure_config_file(target)
    assert load_config(target).impersonate_domains == ("x.com",)


@pytest.mark.parametrize("payload", [{}, {"impersonate_domains": []}])
def test_empty_domain_values_mean_no_impersonation(tmp_path, payload) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_config(path).impersonate_domains == ()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"impersonate_domains": None},
        {"impersonate_domains": [1]},
        {"impersonate_target": []},
        {"unexpected": 1},
    ],
)
def test_invalid_config_is_not_a_default_object(tmp_path, payload):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SetupError):
        load_config(path)


@pytest.mark.parametrize(
    ("platform", "relative"),
    [
        ("linux", ".config/ivdc/config.json"),
        ("darwin", "Library/Application Support/ivdc/config.json"),
        ("win32", "AppData/Roaming/ivdc/config.json"),
    ],
)
def test_platform_config_location(tmp_path, monkeypatch, platform, relative):
    from pathlib import Path

    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr("ivdc.config.sys.platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert default_config_path() == tmp_path / relative


def test_linux_honours_xdg_and_ignores_windows_appdata(tmp_path, monkeypatch):
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    monkeypatch.setattr("ivdc.config.sys.platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "win"))
    assert default_config_path() == tmp_path / "xdg/ivdc/config.json"


def test_legacy_config_keys_require_explicit_migration(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"impersonateDomains": []}', encoding="utf-8")
    with pytest.raises(SetupError, match="MIGRATION.md"):
        load_config(path)


def test_config_example_matches_defaults():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "config.example.json"
    assert load_config(path) == Config()


def test_partial_quality_overrides_keep_other_defaults_and_support_bom(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"quality_profiles": {"libx265": {"high": 20}}}), encoding="utf-8-sig"
    )
    profiles = load_config(path).quality_profiles
    assert profiles["libx265"] == QualityLevels(20, 28, 32)
    assert profiles["hevc_nvenc"] == DEFAULT_QUALITY_PROFILES["hevc_nvenc"]
    assert Config().quality_profiles["libx265"].high == 24
    with pytest.raises(TypeError):
        profiles["libx265"] = QualityLevels(1, 2, 3)


@pytest.mark.parametrize(
    "profiles",
    [
        None,
        [],
        {"x265": {}},
        {"libx265": None},
        {"libx265": []},
        {"libx265": {"hig": 20}},
        {"libx265": {"high": True}},
        {"libx265": {"high": "20"}},
        {"libx265": {"high": 20.5}},
        {"libx265": {"high": 28}},
        {"libx265": {"high": 30}},
        {"libx265": {"low": 27}},
        {"libx265": {"medium": None}},
    ],
)
def test_invalid_quality_profiles_report_config_context(tmp_path, profiles):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"quality_profiles": profiles}))
    with pytest.raises(SetupError, match="quality_profiles") as error:
        load_config(path)
    assert str(path) in str(error.value)


@pytest.mark.parametrize("encoder", QUALITY_SPECS)
@pytest.mark.parametrize("boundary", ["minimum", "maximum"])
def test_quality_bounds_are_specific_to_actual_encoder(tmp_path, encoder, boundary):
    spec = QUALITY_SPECS[encoder]
    level = "high" if boundary == "minimum" else "low"
    value = getattr(spec, boundary)
    path = tmp_path / "config.json"
    payload = {"quality_profiles": {encoder: {level: value}}}
    path.write_text(json.dumps(payload))
    assert getattr(load_config(path).quality_profiles[encoder], level) == value
    payload["quality_profiles"][encoder][level] += -1 if boundary == "minimum" else 1
    path.write_text(json.dumps(payload))
    with pytest.raises(SetupError, match=encoder):
        load_config(path)
