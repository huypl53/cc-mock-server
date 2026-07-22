"""RED-first tests for cc_mock_server.cli / help (plan.md phase 7).

Every client subcommand is exercised against a real `pytest_httpserver`
instance standing in for the control API -- these tests assert the CLI
issues the correct HTTP method/path/JSON body, not that the real control
API behaves correctly (that's `tests/test_control_api.py`). `start` is
NOT run here (it blocks forever and owns a whole process/loop) -- only
its argument-to-`Config` mapping (`cli._build_start_config`) is unit
tested directly.

The drift test is the important structural guarantee for this file: it
proves `help.COMMANDS` (the markdown source) and `cli.build_arg_parser()`
(the actual argparse tree) can never silently diverge.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpserver import HTTPServer

from pathlib import Path

from cc_mock_server import cli, help as help_registry, installer
from cc_mock_server.enums import AgentMode, FilterMode, Mode, ReplayMissStrategy, TimeoutFallback


def _argv(httpserver: HTTPServer, command: str, *rest: str) -> list[str]:
    return [command, "--control-host", "127.0.0.1", "--control-port", str(httpserver.port), *rest]


# --------------------------------------------------------------------------
# client subcommands -> correct HTTP calls
# --------------------------------------------------------------------------


class TestStatusCommand:
    def test_status_calls_get_mock_status(self, httpserver: HTTPServer, capsys):
        httpserver.expect_request("/mock/status", method="GET").respond_with_json({"mode": "live"})

        exit_code = cli.main(_argv(httpserver, "status"))

        assert exit_code == 0
        assert len(httpserver.log) == 1
        assert httpserver.log[0][0].method == "GET"
        assert json.loads(capsys.readouterr().out) == {"mode": "live"}


class TestModeCommand:
    def test_mode_posts_requested_mode(self, httpserver: HTTPServer, capsys):
        httpserver.expect_request("/mock/mode", method="POST").respond_with_json({"mode": "replay"})

        exit_code = cli.main(_argv(httpserver, "mode", "replay"))

        assert exit_code == 0
        assert httpserver.log[0][0].get_json() == {"mode": "replay"}
        assert json.loads(capsys.readouterr().out) == {"mode": "replay"}


class TestFilterCommand:
    def test_filter_list_calls_get(self, httpserver: HTTPServer):
        httpserver.expect_request("/mock/filter", method="GET").respond_with_json(
            {"mode": "whitelist", "domains": []}
        )

        exit_code = cli.main(_argv(httpserver, "filter", "list"))

        assert exit_code == 0
        assert httpserver.log[0][0].method == "GET"

    def test_filter_add_posts_action_and_domain(self, httpserver: HTTPServer):
        httpserver.expect_request("/mock/filter", method="POST").respond_with_json(
            {"mode": "whitelist", "domains": ["x.example.com"]}
        )

        exit_code = cli.main(_argv(httpserver, "filter", "add", "x.example.com"))

        assert exit_code == 0
        assert httpserver.log[0][0].get_json() == {"action": "add", "domain": "x.example.com"}

    def test_filter_add_without_domain_is_a_client_side_error(self, httpserver: HTTPServer, capsys):
        exit_code = cli.main(_argv(httpserver, "filter", "add"))

        assert exit_code == 1
        assert len(httpserver.log) == 0
        assert "domain" in capsys.readouterr().err


class TestSelectDeselectCommands:
    def test_select_posts_pattern(self, httpserver: HTTPServer):
        httpserver.expect_request("/mock/select", method="POST").respond_with_json(
            {"selected": "api.example.com"}
        )

        exit_code = cli.main(_argv(httpserver, "select", "api.example.com"))

        assert exit_code == 0
        assert httpserver.log[0][0].get_json() == {"pattern": "api.example.com"}

    def test_deselect_deletes_by_pattern(self, httpserver: HTTPServer):
        httpserver.expect_request("/mock/select/api.example.com", method="DELETE").respond_with_json(
            {"deselected": "api.example.com"}
        )

        exit_code = cli.main(_argv(httpserver, "deselect", "api.example.com"))

        assert exit_code == 0
        assert httpserver.log[0][0].method == "DELETE"

    def test_deselect_quotes_patterns_containing_spaces(self, httpserver: HTTPServer):
        httpserver.expect_request(
            "/mock/select/GET api.example.com/v1/x", method="DELETE"
        ).respond_with_json({"deselected": "GET api.example.com/v1/x"})

        exit_code = cli.main(_argv(httpserver, "deselect", "GET api.example.com/v1/x"))

        assert exit_code == 0
        assert len(httpserver.log) == 1


class TestRespondCommand:
    def test_respond_posts_request_id_status_and_parsed_json_body(self, httpserver: HTTPServer):
        httpserver.expect_request("/mock/respond", method="POST").respond_with_json(
            {"request_id": "abc", "resolved": True}
        )

        exit_code = cli.main(
            _argv(httpserver, "respond", "--request-id", "abc", "--status", "201", "--json", '{"ok": true}')
        )

        assert exit_code == 0
        assert httpserver.log[0][0].get_json() == {"request_id": "abc", "status": 201, "body": {"ok": True}}

    def test_respond_defaults_status_to_200(self, httpserver: HTTPServer):
        httpserver.expect_request("/mock/respond", method="POST").respond_with_json(
            {"request_id": "abc", "resolved": True}
        )

        exit_code = cli.main(_argv(httpserver, "respond", "--request-id", "abc", "--json", "{}"))

        assert exit_code == 0
        assert httpserver.log[0][0].get_json()["status"] == 200

    def test_respond_invalid_json_is_a_client_side_error(self, httpserver: HTTPServer, capsys):
        exit_code = cli.main(
            _argv(httpserver, "respond", "--request-id", "abc", "--json", "{not json")
        )

        assert exit_code == 1
        assert len(httpserver.log) == 0
        assert "json" in capsys.readouterr().err.lower()


class TestRecordingsCommand:
    def test_recordings_lists_by_default(self, httpserver: HTTPServer):
        httpserver.expect_request("/mock/recordings", method="GET").respond_with_json({"recordings": []})

        exit_code = cli.main(_argv(httpserver, "recordings"))

        assert exit_code == 0
        assert httpserver.log[0][0].method == "GET"

    def test_recordings_delete_flag_deletes_by_id(self, httpserver: HTTPServer):
        httpserver.expect_request("/mock/recordings/rec-1", method="DELETE").respond_with_json(
            {"deleted": "rec-1"}
        )

        exit_code = cli.main(_argv(httpserver, "recordings", "--delete", "rec-1"))

        assert exit_code == 0
        assert httpserver.log[0][0].method == "DELETE"


class TestPendingCommand:
    def test_pending_calls_get(self, httpserver: HTTPServer):
        httpserver.expect_request("/mock/pending", method="GET").respond_with_json({"pending": []})

        exit_code = cli.main(_argv(httpserver, "pending"))

        assert exit_code == 0
        assert httpserver.log[0][0].method == "GET"


class TestUnreachableControlApi:
    def test_connection_error_is_reported_not_raised(self, capsys):
        exit_code = cli.main(["status", "--control-host", "127.0.0.1", "--control-port", "1"])

        assert exit_code == 1
        assert "control API" in capsys.readouterr().err


class TestErrorStatusPropagates:
    def test_404_response_yields_nonzero_exit(self, httpserver: HTTPServer):
        httpserver.expect_request("/mock/recordings/missing", method="DELETE").respond_with_json(
            {"detail": "not found"}, status=404
        )

        exit_code = cli.main(_argv(httpserver, "recordings", "--delete", "missing"))

        assert exit_code == 1


# --------------------------------------------------------------------------
# `start` argument -> Config mapping (no server actually started)
# --------------------------------------------------------------------------


class TestStartConfigMapping:
    def test_start_flags_map_onto_config_fields(self):
        parser = cli.build_arg_parser()
        args = parser.parse_args(
            [
                "start",
                "--proxy-port", "9000",
                "--control-port", "9001",
                "--mode", "replay",
                "--agent-url", "http://127.0.0.1:9999/cb",
                "--agent-mode", "sync",
                "--filter-mode", "blacklist",
                "--filter", "*.a.com",
                "--filter", "*.b.com",
                "--agent-timeout", "3.5",
                "--timeout-fallback", "pass_through",
                "--min-confidence", "0.75",
                "--recordings", "/tmp/recs",
            ]
        )

        config = cli._build_start_config(args)

        assert config.proxy_port == 9000
        assert config.control_port == 9001
        assert config.mode == Mode.REPLAY
        assert config.agent_url == "http://127.0.0.1:9999/cb"
        assert config.agent_mode == AgentMode.SYNC
        assert config.filter_mode == FilterMode.BLACKLIST
        assert config.filter_domains == ["*.a.com", "*.b.com"]
        assert config.agent_timeout == 3.5
        assert config.timeout_fallback == TimeoutFallback.PASS_THROUGH
        assert config.min_confidence == 0.75
        assert str(config.recordings_dir) == "/tmp/recs"

    def test_start_with_no_flags_uses_config_defaults(self):
        parser = cli.build_arg_parser()
        args = parser.parse_args(["start"])

        config = cli._build_start_config(args)

        assert config.proxy_port == 8080
        assert config.control_port == 8081
        assert config.mode == Mode.LIVE
        assert config.agent_mode == AgentMode.PENDING


# --------------------------------------------------------------------------
# --agent-help / --help
# --------------------------------------------------------------------------


class TestAgentHelp:
    def test_agent_help_contains_every_command_and_an_example(self, capsys):
        exit_code = cli.main(["--agent-help"])

        assert exit_code == 0
        output = capsys.readouterr().out
        for command in help_registry.COMMANDS:
            assert f"`{command.name}`" in output
            assert command.example.split()[0] in output

    def test_agent_help_documents_sync_callback_envelope(self, capsys):
        # The sync-mode callback must return {status, body, ...}; returning a
        # raw payload silently degrades to {}. --agent-help must warn about it.
        exit_code = cli.main(["--agent-help"])
        assert exit_code == 0
        output = capsys.readouterr().out
        assert "sync-mode callback contract" in output
        assert '"body"' in output
        assert "defaults to `{}`" in output  # the silent-drop pitfall

    def test_help_flag_exits_zero(self):
        with pytest.raises(SystemExit) as exc_info:
            cli.main(["--help"])
        assert exc_info.value.code == 0


class TestInitCommand:
    def test_init_default_global_writes_skill_and_claude_md(self, tmp_path, monkeypatch, capsys):
        home, cwd = tmp_path / "home", tmp_path / "proj"
        cwd.mkdir(parents=True)
        monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: home))
        monkeypatch.chdir(cwd)

        exit_code = cli.main(["init"])

        assert exit_code == 0
        skill = home / ".claude" / "skills" / "cc-mock" / "SKILL.md"
        assert skill.exists()
        assert (home / ".claude" / "CLAUDE.md").read_text().count(installer.START_MARKER) == 1
        out = capsys.readouterr().out
        assert str(skill) in out

    def test_init_project_scope_targets_cwd(self, tmp_path, monkeypatch):
        home, cwd = tmp_path / "home", tmp_path / "proj"
        cwd.mkdir(parents=True)
        monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: home))
        monkeypatch.chdir(cwd)

        assert cli.main(["init", "project"]) == 0
        assert (cwd / ".claude" / "skills" / "cc-mock" / "SKILL.md").exists()
        assert (cwd / "CLAUDE.md").exists()


class TestAgentHelpDriftGuard:
    def test_registry_commands_match_actual_argparse_subcommands(self):
        parser = cli.build_arg_parser()

        registry_names = set(help_registry.command_names())
        subparser_names = cli.get_subparser_names(parser)

        assert registry_names == subparser_names
        assert registry_names  # never silently empty
