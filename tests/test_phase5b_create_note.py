"""Phase 5B: create_note action validation over adopted/deactivated mappings (AC-10, S-5B-2, S-5B-5)."""

import dataclasses

import pytest
import respx

from bullhorn_mcp.tenant.revalidation import NoteActionSource, NoteActionSources
from bullhorn_mcp.writes import pipeline
from bullhorn_mcp.writes.pipeline import create_note

from ._notes_helpers import OK, context, hv_b11_verified, write_env  # noqa: F401 - fixture
from ._tenant_helpers import NOW_TEXT, init_tenant, make_store, propose_and_commit, snapshot, tenant_payloads, write_snapshot

VALUES = ("Test Action A", "Test Action B", "Test Action C")


@pytest.fixture
def store(tmp_path):
    payloads = tenant_payloads()
    for f in payloads["Note"]["fields"]:
        if f["name"] == "action":
            f["options"] = [{"value": v, "label": v} for v in VALUES]
    snap = dataclasses.replace(
        snapshot(payloads, rest_fp=OK.rest_url_fingerprint),
        note_actions=NoteActionSources(
            NOW_TEXT, {"meta": NoteActionSource("verified", VALUES), "settings": NoteActionSource("unresolved")}
        ),
    )
    s = make_store(tmp_path)
    write_snapshot(s, snap)
    init_tenant(s, [{"op": "apply_discovered_note_actions", "values": ["Test Action A", "Test Action B"]}])
    key_b = next(r.key for r in s.read_version(1).value_mappings if r.values == ("Test Action B",))
    propose_and_commit(s, [{"op": "deactivate_value_mapping", "key": key_b}])
    return s


def _preview(store, action_type, monkeypatch):
    built = []
    monkeypatch.setattr(pipeline, "_body_and_plan", lambda op: built.append(op))
    with respx.mock(assert_all_called=False) as router:
        result = create_note(context(write_env(store)), target_type="candidate", target_id=100, action_type=action_type,
                             comments="Synthetic test comment")
        assert not router.calls
    assert not built  # no request body is ever built
    return result


def test_production_guard_unchanged():
    assert pipeline.HV_B11_VERIFIED is False


@pytest.mark.parametrize(
    "action_type",
    [
        "Test Action B",  # deactivated
        "Test Action C",  # discovered but never adopted
        "Never Mapped",
        "test action a",
        "TEST ACTION A",
        " Test Action A",
        "Test Action A ",
        "Test  Action A",
        "Test \u0410ction A",  # Cyrillic A
        "Test\u00a0Action A",
        "Test Action A\u200b",
        "Test Action A" + "x" * 30,
        "candidate_screen",
    ],
)
def test_rejected_never_substituted(store, action_type, monkeypatch, hv_b11_verified):  # noqa: F811
    result = _preview(store, action_type, monkeypatch)
    assert result["status"] == "rejected_validation"
    err = next(e for e in result["errors"] if e["code"] == "unknown_action_type")
    assert err["valid_values"] == ["Test Action A"]
    assert "preview" not in result and "operation_id" not in result


@pytest.mark.parametrize("value", ["", "   ", None, 5, ["Test Action A"], {"v": "Test Action A"}])
def test_non_string_rejected(store, value, monkeypatch, hv_b11_verified):  # noqa: F811
    result = _preview(store, value, monkeypatch)
    assert result["status"] == "rejected_validation"


def test_guard_on_in_production_denies_before_any_request(store, monkeypatch):
    result = _preview(store, "Test Action A", monkeypatch)
    assert result["status"] != "preview" and "preview" not in result
