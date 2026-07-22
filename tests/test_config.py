"""RED-first tests for cc_mock_server.config.

Covers every Cross-Cutting Decision field (plan.md D1-D9) and the
precedence contract: CLI > env(CC_MOCK_*) > YAML > default.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cc_mock_server.config import Config, is_loopback, load_config
from cc_mock_server.enums import (
    AgentMode,
    FilterMode,
    Mode,
    ReplayMissStrategy,
    TimeoutFallback,
)


class TestDefaults:
    def test_all_fields_have_documented_defaults(self):
        cfg = Config()

        assert cfg.proxy_port == 8080
        assert cfg.control_port == 8081
        assert cfg.control_bind == "127.0.0.1"
        assert cfg.mode == Mode.LIVE
        assert cfg.agent_url is None
        assert cfg.agent_mode == AgentMode.PENDING
        assert cfg.agent_timeout == 10.0
        assert cfg.timeout_fallback == TimeoutFallback.RETURN_ERROR
        assert cfg.replay_miss_strategy == ReplayMissStrategy.PASS_THROUGH
        assert cfg.min_confidence == 0.6
        assert cfg.max_pending == 100
        assert cfg.filter_mode == FilterMode.WHITELIST
        assert cfg.filter_domains == []
        assert cfg.recordings_dir == Path("recordings")


class TestEnumValidation:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("mode", "bogus"),
            ("agent_mode", "bogus"),
            ("timeout_fallback", "bogus"),
            ("replay_miss_strategy", "bogus"),
            ("filter_mode", "bogus"),
        ],
    )
    def test_invalid_enum_value_raises(self, field: str, value: str):
        with pytest.raises(ValidationError):
            Config(**{field: value})


class TestFieldValidators:
    def test_agent_timeout_zero_raises(self):
        with pytest.raises(ValidationError):
            Config(agent_timeout=0)

    def test_agent_timeout_negative_raises(self):
        with pytest.raises(ValidationError):
            Config(agent_timeout=-5)

    def test_min_confidence_above_one_raises(self):
        with pytest.raises(ValidationError):
            Config(min_confidence=1.1)

    def test_min_confidence_below_zero_raises(self):
        with pytest.raises(ValidationError):
            Config(min_confidence=-0.1)

    def test_min_confidence_boundary_values_ok(self):
        assert Config(min_confidence=0.0).min_confidence == 0.0
        assert Config(min_confidence=1.0).min_confidence == 1.0

    def test_max_pending_zero_raises(self):
        with pytest.raises(ValidationError):
            Config(max_pending=0)

    def test_max_pending_negative_raises(self):
        with pytest.raises(ValidationError):
            Config(max_pending=-1)

    def test_filter_domains_normalized_to_lowercase(self):
        cfg = Config(filter_domains=["Example.COM", "api.OpenAI.com"])
        assert cfg.filter_domains == ["example.com", "api.openai.com"]

    def test_agent_mode_sync_requires_agent_url(self):
        with pytest.raises(ValidationError):
            Config(agent_mode="sync", agent_url=None)

    def test_agent_mode_sync_with_agent_url_ok(self):
        cfg = Config(agent_mode="sync", agent_url="http://127.0.0.1:9000/agent")
        assert cfg.agent_mode == AgentMode.SYNC
        assert cfg.agent_url == "http://127.0.0.1:9000/agent"

    def test_agent_mode_pending_without_agent_url_ok(self):
        cfg = Config(agent_mode="pending", agent_url=None)
        assert cfg.agent_url is None


class TestIsLoopback:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8000",
            "https://127.0.0.1",
            "http://localhost:8000/agent",
            "http://[::1]:8000",
            "127.0.0.1:8000",
            "localhost",
        ],
    )
    def test_positive_cases(self, url: str):
        assert is_loopback(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com",
            "https://api.openai.com/v1/chat",
            "http://192.168.1.10:9000",
            "http://my-agent-host:9000",
            "",
        ],
    )
    def test_negative_cases(self, url: str):
        assert is_loopback(url) is False


class TestLoadConfigPrecedence:
    def test_default_only(self):
        cfg = load_config(cli_overrides=None, yaml_path=None, env={})
        assert cfg.proxy_port == 8080

    def test_yaml_overrides_default(self, tmp_yaml_config):
        path = tmp_yaml_config("proxy_port: 9001\n")
        cfg = load_config(cli_overrides=None, yaml_path=path, env={})
        assert cfg.proxy_port == 9001

    def test_env_overrides_yaml(self, tmp_yaml_config):
        path = tmp_yaml_config("proxy_port: 9001\n")
        cfg = load_config(
            cli_overrides=None,
            yaml_path=path,
            env={"CC_MOCK_PROXY_PORT": "9100"},
        )
        assert cfg.proxy_port == 9100

    def test_cli_overrides_env(self, tmp_yaml_config):
        path = tmp_yaml_config("proxy_port: 9001\n")
        cfg = load_config(
            cli_overrides={"proxy_port": 9500},
            yaml_path=path,
            env={"CC_MOCK_PROXY_PORT": "9100"},
        )
        assert cfg.proxy_port == 9500

    def test_cli_none_values_do_not_override_lower_precedence(self):
        cfg = load_config(
            cli_overrides={"proxy_port": None},
            yaml_path=None,
            env={"CC_MOCK_PROXY_PORT": "9100"},
        )
        assert cfg.proxy_port == 9100

    def test_env_prefix_is_case_sensitive_and_scoped(self):
        cfg = load_config(
            cli_overrides=None,
            yaml_path=None,
            env={"CC_MOCK_MODE": "replay", "UNRELATED_VAR": "ignored"},
        )
        assert cfg.mode == Mode.REPLAY

    def test_env_filter_domains_parsed_as_comma_list_and_normalized(self):
        cfg = load_config(
            cli_overrides=None,
            yaml_path=None,
            env={"CC_MOCK_FILTER_DOMAINS": "Example.com, api.OpenAI.com"},
        )
        assert cfg.filter_domains == ["example.com", "api.openai.com"]

    def test_missing_yaml_path_is_not_an_error(self, tmp_path: Path):
        missing = tmp_path / "does-not-exist.yaml"
        cfg = load_config(cli_overrides=None, yaml_path=missing, env={})
        assert cfg.proxy_port == 8080

    def test_full_precedence_chain(self, tmp_yaml_config):
        path = tmp_yaml_config(
            "proxy_port: 9001\ncontrol_port: 9002\nmode: replay\n"
        )
        cfg = load_config(
            cli_overrides={"mode": "live"},
            yaml_path=path,
            env={"CC_MOCK_CONTROL_PORT": "9200"},
        )
        # CLI wins for `mode`, env wins for `control_port`, YAML wins for
        # `proxy_port` (untouched by env/CLI), default wins for the rest.
        assert cfg.mode == Mode.LIVE
        assert cfg.control_port == 9200
        assert cfg.proxy_port == 9001
        assert cfg.control_bind == "127.0.0.1"
