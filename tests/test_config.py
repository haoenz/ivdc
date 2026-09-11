"""用户配置的读取、回落与默认值写出。"""

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
        json.dumps({"impersonateDomains": ["a.com", " b.com "], "impersonateTarget": "chrome-120"}),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.impersonate_domains == ("a.com", "b.com")
    assert config.impersonate_target == "chrome-120"


def test_string_domain_is_accepted(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"impersonateDomains": "only.com"}), encoding="utf-8")
    assert load_config(path).impersonate_domains == ("only.com",)


def test_corrupt_file_falls_back_to_defaults(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    assert load_config(path) == Config()


def test_non_mapping_payload_falls_back_to_defaults(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_config(path) == Config()


def test_ensure_config_file_writes_defaults_once(tmp_path) -> None:
    target = tmp_path / "nested" / "config.json"
    created = ensure_config_file(target)
    assert created == target
    assert target.exists()
    assert load_config(target) == Config()

    target.write_text(json.dumps({"impersonateDomains": ["x.com"]}), encoding="utf-8")
    ensure_config_file(target)
    assert load_config(target).impersonate_domains == ("x.com",)


@pytest.mark.parametrize("payload", [None, {}, {"impersonateDomains": None}])
def test_empty_domain_values_mean_no_impersonation(tmp_path, payload) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_config(path).impersonate_domains == ()
