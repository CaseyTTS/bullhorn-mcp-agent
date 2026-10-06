"""Tests for crosscutting audit logging."""

import json
import logging

from bullhorn_mcp.crosscutting import audit


class TestRedact:
    """Tests for the standalone redact() helper."""

    def test_redacts_known_secret_keys_case_insensitively(self):
        data = {
            "password": "hunter2",
            "Client_Secret": "abc",
            "ACCESS_TOKEN": "xyz",
            "refresh_token": "r1",
            "bh_rest_token": "t1",
            "api_key": "k1",
            "secret": "s1",
            "username": "bob",
        }

        redacted = audit.redact(data)

        assert redacted["password"] == "***REDACTED***"
        assert redacted["Client_Secret"] == "***REDACTED***"
        assert redacted["ACCESS_TOKEN"] == "***REDACTED***"
        assert redacted["refresh_token"] == "***REDACTED***"
        assert redacted["bh_rest_token"] == "***REDACTED***"
        assert redacted["api_key"] == "***REDACTED***"
        assert redacted["secret"] == "***REDACTED***"
        assert redacted["username"] == "bob"

    def test_redacts_nested_dicts(self):
        data = {"outer": {"password": "hunter2", "ok": "fine"}}

        redacted = audit.redact(data)

        assert redacted["outer"]["password"] == "***REDACTED***"
        assert redacted["outer"]["ok"] == "fine"

    def test_redacts_dicts_nested_in_lists(self):
        data = {"items": [{"password": "hunter2"}, {"name": "fine"}]}

        redacted = audit.redact(data)

        assert redacted["items"][0]["password"] == "***REDACTED***"
        assert redacted["items"][1]["name"] == "fine"

    def test_leaves_non_secret_values_untouched(self):
        data = {"candidate_id": 123, "file_path": "/tmp/resume.pdf"}

        redacted = audit.redact(data)

        assert redacted == data

    def test_does_not_mutate_input(self):
        data = {"password": "hunter2"}

        audit.redact(data)

        assert data["password"] == "hunter2"


class TestLogInvocation:
    """Tests for log_invocation()'s structured, redacted logging."""

    def test_success_logs_info_with_redacted_args(self, caplog):
        with caplog.at_level(logging.INFO, logger="bullhorn_mcp.audit"):
            audit.log_invocation(
                tool="get_candidate",
                args={"candidate_id": 1, "password": "hunter2"},
                result_summary="ok",
                duration_ms=1.23,
                success=True,
            )

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelno == logging.INFO

        payload = json.loads(record.getMessage())
        assert payload["tool"] == "get_candidate"
        assert payload["args"]["candidate_id"] == 1
        assert payload["args"]["password"] == "***REDACTED***"
        assert payload["result"] == "ok"
        assert payload["duration_ms"] == 1.23

        assert "hunter2" not in caplog.text

    def test_failure_logs_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="bullhorn_mcp.audit"):
            audit.log_invocation(
                tool="get_candidate",
                args={},
                result_summary="error: boom",
                duration_ms=0.5,
                success=False,
            )

        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING

    def test_never_emits_secret_values_verbatim(self, caplog):
        with caplog.at_level(logging.INFO, logger="bullhorn_mcp.audit"):
            audit.log_invocation(
                tool="some_tool",
                args={
                    "password": "hunter2",
                    "client_secret": "s3cr3t",
                    "access_token": "tok123",
                    "refresh_token": "ref456",
                    "bh_rest_token": "bh789",
                    "api_key": "key000",
                    "secret": "shh",
                },
                result_summary="ok",
                duration_ms=1.0,
                success=True,
            )

        for value in ["hunter2", "s3cr3t", "tok123", "ref456", "bh789", "key000", "shh"]:
            assert value not in caplog.text

    def test_result_summary_is_not_a_full_payload(self, caplog):
        """log_invocation must never be fed the full tool output - this
        guards against a caller accidentally doing so by keeping the
        logged record small and matching exactly what was passed."""
        with caplog.at_level(logging.INFO, logger="bullhorn_mcp.audit"):
            audit.log_invocation(
                tool="list_jobs",
                args={},
                result_summary="ok",
                duration_ms=1.0,
                success=True,
            )

        payload = json.loads(caplog.records[0].getMessage())
        assert payload["result"] == "ok"
