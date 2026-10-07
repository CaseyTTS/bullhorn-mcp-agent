"""Phase 6 M1 security regressions (§7: SM-1..SM-8; SR-37..SR-39; AC-2, AC-6, AC-7, AC-8, AC-10)."""

from __future__ import annotations

import json
import pathlib
import re
import threading
from unittest.mock import Mock, patch

import httpx
import pytest
import respx

from bullhorn_mcp import server
from bullhorn_mcp.identity import deploy, sessions
from bullhorn_mcp.identity.principal import TIER2_ALLOWLIST
from bullhorn_mcp.metrics import catalog as C
from bullhorn_mcp.metrics import tier2_policy as P
from bullhorn_mcp.tools import metrics as metric_tools
from bullhorn_mcp.tools import setup as setup_tools

from ._identity_helpers import ADMIN, ALICE, BOB, REST_1, SVC, TK1, caller, link, no_caller
from ._phase5c_helpers import FULL_CONFIG, make_client
from .test_phase5c_security_tools import FORBIDDEN_PARAMS, _activity, _find, install_shared, root_capture  # noqa: F401
from .test_phase6_m1_metrics import (
    ID_BASE,
    SENTINEL_CLIENT,
    SENTINEL_EMAIL,
    SENTINEL_NAME,
    SENTINEL_NOTE,
    dataset,
)

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "bullhorn_mcp"
SVC_TOKEN = "bh-token-SERVICE-zzzzzzzz"
ALICE_TOKEN = "bh-token-ALICE-xxxxxxxx"
Q1 = {"date_from": "2026-01-01", "date_to": "2026-04-01"}
METRICS_SCHEMA = {
    "properties": {
        "metrics": {"items": {"type": "string"}, "title": "Metrics", "type": "array"},
        "date_from": {"title": "Date From", "type": "string"},
        "date_to": {"title": "Date To", "type": "string"},
        "period": {"default": "month", "title": "Period", "type": "string"},
    },
    "required": ["metrics", "date_from", "date_to"],
    "title": "get_recruiting_metricsArguments",
    "type": "object",
}


@pytest.fixture
def shared6(tmp_path, monkeypatch):
    """Tenant one with a configured (read-only) service identity."""
    s = install_shared(tmp_path, monkeypatch, FULL_CONFIG, service=True)
    yield s
    deploy.reset()
    sessions.reset_caches()


@pytest.fixture
def shared6_nosvc(tmp_path, monkeypatch):
    s = install_shared(tmp_path, monkeypatch, FULL_CONFIG)
    yield s
    deploy.reset()
    sessions.reset_caches()


def _metrics(**kw):
    return metric_tools.get_recruiting_metrics(**{"metrics": ["client_submissions", "jobs_created"], **Q1, **kw})


def _json(text: str):
    return json.loads(text)


def _service():
    return patch.object(sessions, "service_client", Mock(return_value=make_client(SVC_TOKEN, REST_1)))


def _no_service():
    return patch.object(sessions, "service_client", side_effect=AssertionError("service identity"))


# ---------------------------------------------------------------------- #
# Surface (AC-2, SM-1)
# ---------------------------------------------------------------------- #


class TestSurface:
    def test_exactly_23_tools(self):
        assert len(server.mcp._tool_manager._tools) == 23 and "get_recruiting_metrics" in server.mcp._tool_manager._tools

    def test_schema_pin(self):
        assert server.mcp._tool_manager._tools["get_recruiting_metrics"].parameters == METRICS_SCHEMA

    def test_no_identity_or_tier_params(self):
        assert not set(METRICS_SCHEMA["properties"]) & FORBIDDEN_PARAMS

    def test_on_tier2_allowlist_exactly(self):
        assert TIER2_ALLOWLIST == frozenset({"bullhorn_session", "setup_status", "get_recruiting_metrics"})


# ---------------------------------------------------------------------- #
# Tier gating (SM-1, SM-4, SM-8)
# ---------------------------------------------------------------------- #


class TestTierGating:
    @respx.mock
    def test_sm8_other_tools_still_denied_to_workspace_only(self, shared6):
        from .test_phase5a_security_tools import NON_ALLOWLISTED

        calls = {**NON_ALLOWLISTED, "find_records": _find, "get_activity": _activity}
        assert len(calls) == 20 and "get_recruiting_metrics" not in calls
        with _no_service(), caller(ALICE):
            for name, call in calls.items():
                assert call() == f"ERROR: permission denied for {name}: bullhorn_auth_required", name
        assert not respx.calls

    @respx.mock
    def test_service_principal_denied(self, shared6):
        with _no_service(), caller(SVC):
            assert _metrics() == "ERROR: permission denied for get_recruiting_metrics: service_identity_read_only"
        assert not respx.calls

    @respx.mock
    def test_no_identity(self, shared6):
        with no_caller():
            assert _metrics().endswith("identity_required")
        assert not respx.calls

    @respx.mock
    def test_sm1_claims_cannot_escalate(self, shared6):
        """An unlinked caller asserting tier-like claims stays Tier 2 (aggregate-only, service identity)."""
        fake = dataset()
        fake.mock(REST_1)
        with _service(), caller(ALICE, access_tier="bullhorn_user", tier="local", role="service"):
            out = _json(_metrics())
        assert out["tier"] == "workspace_only" and "provenance" not in out
        P.validate_output(out, C.load())
        assert set(fake.tokens) == {SVC_TOKEN}

    @respx.mock
    def test_no_service_principal_configured(self, shared6_nosvc):
        """The tenant has no service credentials: unavailable, no Bullhorn call."""
        with caller(ALICE):
            out = _json(_metrics())
        assert out == {"status": "unavailable", "tier": "workspace_only", "error": "service_identity_not_configured"}
        assert not respx.calls

    def test_sm4_service_identity_only_in_tier2_module(self):
        users = sorted(
            str(p.relative_to(SRC)).replace("\\", "/")
            for p in SRC.rglob("*.py")
            if re.search(r"\bservice_client\b", p.read_text(encoding="utf-8"))
        )
        assert users == ["identity/sessions.py", "metrics/tier2.py"]
        for name in ("compute.py", "tier2_policy.py", "catalog.py"):
            assert "sessions" not in (SRC / "metrics" / name).read_text(encoding="utf-8")
        assert "service_client" not in (SRC / "tools" / "metrics.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------- #
# SR-39: Tier 1 runs on the caller's session only (AC-6, SM-5)
# ---------------------------------------------------------------------- #


class TestTier1CallerSession:
    @respx.mock
    def test_linked_user_uses_own_token(self, shared6):
        link(shared6.store, TK1, ALICE, token=ALICE_TOKEN)
        fake = dataset()
        fake.mock(REST_1)
        with _no_service(), caller(ALICE):
            out = _json(_metrics(metrics=["client_submissions", "submission_to_interview"]))
        assert out["status"] == "ok" and out["tier"] == "bullhorn_user" and "request_hash" in out["provenance"]
        assert [c["value"] for c in out["metrics"]["client_submissions"]["cells"]] == [30, 25, 11]
        assert fake.tokens and set(fake.tokens) == {ALICE_TOKEN}
        assert all(c.request.headers["BhRestToken"] == ALICE_TOKEN for c in respx.calls)


# ---------------------------------------------------------------------- #
# AC-7: Tier 2 under the service token only; concurrent A (Tier 1) / B (Tier 2)
# ---------------------------------------------------------------------- #


class TestConcurrentTiers:
    def test_each_request_carries_the_right_token(self, shared6):
        link(shared6.store, TK1, ALICE, token=ALICE_TOKEN)  # BOB stays unlinked: workspace_only
        fake = dataset()
        results: dict[str, list[dict]] = {"A": [], "B": []}
        errors: list[BaseException] = []

        def worker(subject, tag):
            try:
                with caller(subject):
                    for _ in range(3):
                        results[tag].append(_json(_metrics()))
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        with respx.mock, _service():
            fake.mock(REST_1)
            threads = [threading.Thread(target=worker, args=(ALICE, "A")), threading.Thread(target=worker, args=(BOB, "B"))]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        assert not errors
        assert set(fake.tokens) == {ALICE_TOKEN, SVC_TOKEN}
        assert fake.tokens.count(ALICE_TOKEN) == fake.tokens.count(SVC_TOKEN) == 3 * 2 * 3  # runs x concepts x months
        assert all(r["tier"] == "bullhorn_user" and "provenance" in r for r in results["A"])
        for r in results["B"]:
            P.validate_output(r, C.load())
            assert r["metrics"]["jobs_created"]["cells"][1]["suppressed"] is True


# ---------------------------------------------------------------------- #
# SR-37 / SR-38 at the tool boundary: suppression, allowlist, sentinels (AC-8, AC-10, SM-2)
# ---------------------------------------------------------------------- #


class TestTier2Sentinels:
    @respx.mock
    def test_outputs_errors_logs_and_audit_clean(self, shared6, root_capture, caplog):  # noqa: F811
        fake = dataset()
        fake.mock(REST_1)
        outputs: list[str] = []
        with caplog.at_level(0), _service(), caller(ALICE):
            outputs.append(_metrics(metrics=C.load().metrics and list(C.load().metrics)))
            outputs.append(_metrics(period="quarter"))
            outputs.append(_metrics(metrics=[SENTINEL_NAME], date_from=SENTINEL_EMAIL))
            respx.reset()
            respx.get(url__regex=re.escape(REST_1) + r"/query/[A-Za-z]+").mock(
                return_value=httpx.Response(500, text=f"boom {SENTINEL_NAME} {SENTINEL_EMAIL} {SENTINEL_CLIENT} {ID_BASE + 1}")
            )
            outputs.append(_metrics())
        assert _json(outputs[3]) == {"status": "error", "tier": "workspace_only", "error": "bullhorn_error"}
        assert _json(outputs[2])["status"] == "rejected_validation"
        logs = "\n".join(r.getMessage() for r in caplog.records) + root_capture.getvalue() + caplog.text
        blob = "\n".join(outputs)
        for sentinel in (SENTINEL_NAME, SENTINEL_EMAIL, SENTINEL_CLIENT, SENTINEL_NOTE, str(ID_BASE)[:6], SVC_TOKEN):
            assert sentinel not in blob, sentinel
            assert sentinel not in logs, sentinel
        for forbidden in ("activity_id", "request_hash", "provenance", "links", "cursor", "total", "events", "warnings"):
            assert forbidden not in blob, forbidden
        for text in outputs[:2]:
            P.validate_output(_json(text), C.load())
        audits = [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith('{"tool": "get_recruiting_metrics"')]
        assert len(audits) == 4
        for rec in audits:
            assert rec["args"]["tier"] == "workspace_only" and rec["args"]["execution"] == "service"
            assert rec["args"]["date_from"]["type"] == "string" and "request_hmac" in rec["args"]
        assert audits[2]["args"]["metrics"] == [] and audits[2]["args"]["invalid_items"] == 1


# ---------------------------------------------------------------------- #
# k is set only by a setup admin (P-1, AC-8)
# ---------------------------------------------------------------------- #


class TestExecutionTierM1A:
    def test_m1a_b_default_unchanged(self, shared6):
        from bullhorn_mcp.tenant.state import compute_setup_state

        link(shared6.store, TK1, ALICE, token=ALICE_TOKEN)
        with caller(BOB):  # workspace_only: today's caller-tier row 1
            assert compute_setup_state().missing_requirements == compute_setup_state(execution_tier=None).missing_requirements
            assert compute_setup_state().state == "disconnected"
            assert compute_setup_state(execution_tier="service").state != "disconnected"
        with caller(ALICE):
            assert compute_setup_state() == compute_setup_state(execution_tier=None)

    @pytest.mark.parametrize("value", ["bullhorn_user", "workspace_only", "local", "SERVICE", "", 1])
    def test_m1a_c_other_values_rejected(self, value, tmp_path):
        from bullhorn_mcp.activity import service as A
        from bullhorn_mcp.reads import records as R
        from bullhorn_mcp.tenant.state import compute_setup_state

        with pytest.raises(ValueError):
            compute_setup_state(execution_tier=value)
        with pytest.raises(ValueError):
            R.check_setup(Mock(), execution_tier=value)
        from ._phase5c_helpers import context, tenant_store

        with pytest.raises(ValueError):
            A.get_activity(context(tenant_store(tmp_path)), {"concepts": ["job_created"], **Q1}, execution_tier=value)

    def test_m1a_d_only_tier2_passes_service(self):
        service_users, passers = set(), set()
        for p in SRC.rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            rel = str(p.relative_to(SRC)).replace("\\", "/")
            if re.search(r"(?<![`\w])execution_tier\s*=\s*[\"']", text):
                service_users.add(rel)
            if re.search(r"(?<![`\w])execution_tier\s*=(?!=)", text):
                passers.add(rel)
        assert service_users == {"metrics/tier2.py"}
        assert passers == {"metrics/tier2.py", "reads/records.py", "activity/service.py"}  # the approved passthroughs
        for rel in ("tools/records.py", "tools/setup.py", "tools/metrics.py"):
            assert "execution_tier" not in (SRC / rel).read_text(encoding="utf-8")

    @respx.mock
    def test_m1a_e_workspace_only_still_denied_for_5c_tools(self, shared6):
        from bullhorn_mcp.tenant import state

        with patch.object(state, "_shared_row1", side_effect=AssertionError("setup gate reached")), _no_service(), caller(ALICE):
            assert _find() == "ERROR: permission denied for find_records: bullhorn_auth_required"
            assert _activity() == "ERROR: permission denied for get_activity: bullhorn_auth_required"
        assert not respx.calls


class TestCohortAdminOnly:
    @respx.mock
    def test_non_admin_cannot_change_k(self, shared6, tmp_path):
        link(shared6.store, TK1, ALICE, token=ALICE_TOKEN)
        doc = tmp_path / "k.yaml"
        doc.write_text("format: x\n", encoding="utf-8")
        with caller(ALICE):
            out = setup_tools.propose_mapping_changes([{"op": "import_document", "path": str(doc)}])
        assert "setup admin" in out and "proposal_id" not in out
        with caller(BOB):
            assert setup_tools.propose_mapping_changes([{"op": "import_document", "path": str(doc)}]).startswith("ERROR")
        assert ADMIN  # the configured setup admin is a different principal
