"""Phase 4B safe-write pipeline: preview (AC-7), confirm (AC-8), validation (AC-9), idempotency (AC-10),
read-back (AC-11), direct mode (AC-12), audit/journal (AC-13), default-off scope (AC-14), guards (AC-19), A1/C2."""

import datetime as dt
import json
import logging
import threading

import httpx
import pytest
import respx

from bullhorn_mcp.tenant.state import ConnectionCheck
from bullhorn_mcp.writes import pipeline
from bullhorn_mcp.writes.ledger import Ledger
from bullhorn_mcp.writes.pipeline import confirm_write, create_note

from ._notes_helpers import (
    CANDIDATE,
    CANDIDATE_2,
    CONTACT,
    JOB,
    NOTE_ACTION,
    NOTE_ID,
    SECRET_COMMENT,
    TOKEN,
    context,
    hv_b11_verified,  # noqa: F401 - fixture
    mock_targets,
    mock_write,
    readback,
    valid_store,
    write_calls,
    write_env,
)
from ._tenant_helpers import NOW, REST_URL, tree_bytes


def _preview(ctx, **kw):
    args = {"target_type": "candidate", "target_id": 100, "action_type": "Screen Call", "comments": SECRET_COMMENT}
    args.update(kw)
    return create_note(ctx, **args)


@pytest.fixture(autouse=True)
def _hv_b11(hv_b11_verified):  # noqa: F811 - these tests exercise the write path with HV-B11 mocked as verified
    yield


@pytest.fixture
def store(tmp_path):
    return valid_store(tmp_path)


@pytest.fixture
def ctx(store):
    return context(write_env(store))


def _journal(store):
    path = store.root / "writes" / "journal.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


# ---------------------------------------------------------------------- #
# AC-7: preview
# ---------------------------------------------------------------------- #


class TestPreview:
    @respx.mock
    def test_preview_only_gets(self, ctx, store):
        mock_targets()
        result = _preview(ctx, associations=[{"type": "job", "id": 200}, {"type": "candidate", "id": 101}])
        assert result["status"] == "previewed", result
        assert len(result["operation_id"]) == 32 and len(result["preview_hash"]) == 64
        assert respx.calls and all(c.request.method == "GET" for c in respx.calls)
        assert all("/search/" not in str(c.request.url) and "/query/" not in str(c.request.url) for c in respx.calls)
        assert any(w.startswith("duplicate_probe_skipped") for w in result["warnings"])
        body = result["preview"]["request"]["body"]
        assert body == {
            "action": "Screen Call",
            "comments": SECRET_COMMENT,
            "personReference": {"id": 100},
            "jobOrder": {"id": 200},
            "commentingPerson": {"id": 7},
        }
        assert [p["mechanism"] for p in result["preview"]["association_plan"]][2] == "PUT /entity/Note/{id}/candidates/{ids}"
        assert result["requires_confirmation"] is True
        assert (store.root / "writes" / "pending" / f"{result['operation_id']}.json").exists()
        assert not (store.root / "writes" / "ledger").exists()

    @respx.mock
    def test_preview_writes_only_pending_and_journal(self, ctx, store):
        mock_targets()
        before = tree_bytes(store.root)
        _preview(ctx)
        after = tree_bytes(store.root)
        changed = {k for k in after if before.get(k) != after[k]}
        assert changed and all(k.replace("\\", "/").startswith(("writes/pending/", "writes/journal.jsonl")) for k in changed)

    @respx.mock
    def test_hash_is_deterministic_and_text_unchanged(self, ctx):
        mock_targets()
        text = "  Line one\r\n\tindented  "
        a = _preview(ctx, comments=text)
        b = _preview(ctx, comments=text)
        assert a["preview_hash"] == b["preview_hash"] and a["operation_id"] != b["operation_id"]
        assert a["preview"]["request"]["body"]["comments"] == text

    def test_dry_run_false_without_direct_mode_refused(self, ctx):
        with respx.mock(assert_all_called=False) as router:
            result = _preview(ctx, dry_run=False)
            assert result["status"] == "refused" and result["errors"][0]["code"] == "confirmation_required"
            assert not router.calls


# ---------------------------------------------------------------------- #
# AC-9 / AC-19: validation and guards (nothing is built or sent)
# ---------------------------------------------------------------------- #


class TestValidation:
    @pytest.mark.parametrize(
        "kw,code",
        [
            ({"comments": "x" * 10_001}, "comments_too_long"),
            ({"comments": "bad\x00char"}, "comments_control_characters"),
            ({"comments": "bell\x07"}, "comments_control_characters"),
            ({"comments": ""}, "invalid_comments"),
            ({"target_id": 0}, "invalid_id"),
            ({"target_id": -5}, "invalid_id"),
            ({"target_id": "100"}, "invalid_id"),
            ({"target_id": True}, "invalid_id"),
            ({"associations": [{"type": "job", "id": "200"}]}, "invalid_id"),
            ({"associations": [{"type": "job", "id": 0}]}, "invalid_id"),
            ({"associations": [{"type": "candidate", "id": i} for i in range(1, 12)]}, "too_many_associations"),
            ({"associations": [{"type": "job", "id": 1, "extra": 1}]}, "invalid_association"),
            ({"associations": [{"type": "candidate", "id": 100}]}, "duplicate_association"),
            ({"target_type": "person"}, "invalid_target_type"),
            ({"idempotency_key": "has space"}, "invalid_idempotency_key"),
        ],
    )
    def test_rejected_before_any_request(self, ctx, store, kw, code, monkeypatch):
        built = []
        monkeypatch.setattr(pipeline, "_body_and_plan", lambda op: built.append(op))
        with respx.mock(assert_all_called=False) as router:
            result = _preview(ctx, **kw)
            assert not router.calls
        assert result["status"] == "rejected_validation"
        assert code in [e["code"] for e in result["errors"]]
        assert not built
        assert not (store.root / "writes" / "pending").exists()

    def test_unknown_action_rejected_never_substituted(self, ctx, monkeypatch):
        built = []
        monkeypatch.setattr(pipeline, "_body_and_plan", lambda op: built.append(op))
        with respx.mock(assert_all_called=False) as router:
            result = _preview(ctx, action_type="screen call")
            assert not router.calls
        assert result["status"] == "rejected_validation"
        err = result["errors"][0]
        assert err["code"] == "unknown_action_type"
        assert err["valid_values"] == ["Screen Call", "Left Message", "Client Call"]
        assert err["suggestions"] == ["Screen Call"]
        assert not built

    def test_suggestion_by_semantic_tag(self, ctx):
        with respx.mock(assert_all_called=False):
            result = _preview(ctx, action_type="candidate_screen")
        assert set(result["errors"][0]["suggestions"]) == {"Screen Call", "Left Message"}

    @pytest.mark.parametrize("atype", ["placement", "client_corporation", "submission"])
    def test_unsupported_association_no_http(self, ctx, atype):
        with respx.mock(assert_all_called=False) as router:
            result = _preview(ctx, associations=[{"type": atype, "id": 5}])
            assert not router.calls
        assert result["status"] == "rejected_validation"
        assert [e["code"] for e in result["errors"]] == ["unsupported_association"]

    def test_second_job_unsupported(self, ctx):
        with respx.mock(assert_all_called=False) as router:
            result = _preview(ctx, associations=[{"type": "job", "id": 200}, {"type": "job", "id": 201}])
            assert not router.calls
        assert [e["code"] for e in result["errors"]] == ["unsupported_association"]

    @pytest.mark.parametrize("ttype", ["job", "placement", "client_corporation", "submission"])
    def test_unsupported_target_no_http(self, ctx, ttype):
        with respx.mock(assert_all_called=False) as router:
            result = _preview(ctx, target_type=ttype)
            assert not router.calls
        assert result["status"] == "rejected_validation" and result["errors"][0]["code"] == "unsupported_target"

    def test_errors_aggregated(self, ctx):
        with respx.mock(assert_all_called=False):
            result = _preview(ctx, action_type="Nope", associations=[{"type": "placement", "id": 1}])
        assert {e["code"] for e in result["errors"]} == {"unsupported_association", "unknown_action_type"}

    @respx.mock
    @pytest.mark.parametrize("record", [dict(CANDIDATE, isDeleted=True), dict(CANDIDATE, id=999)])
    def test_deleted_or_missing_target(self, ctx, record):
        respx.get(f"{REST_URL}/entity/Candidate/100").mock(return_value=httpx.Response(200, json={"data": record}))
        result = _preview(ctx)
        assert result["status"] == "rejected_target"
        assert not write_calls()

    @respx.mock
    def test_target_api_error_redacted(self, ctx):
        respx.get(f"{REST_URL}/entity/Candidate/100").mock(
            return_value=httpx.Response(404, text='{"errorMessage":"nope","BhRestToken":"' + TOKEN + '"}')
        )
        result = _preview(ctx)
        assert result["status"] == "rejected_target" and result["errors"][0]["code"] == "target_not_found"
        assert TOKEN not in json.dumps(result) and len(result["errors"][0]["message"]) <= 300


# ---------------------------------------------------------------------- #
# AC-14 / D-4B-3 / A1-C2: permission
# ---------------------------------------------------------------------- #


class TestPermission:
    @respx.mock
    @pytest.mark.parametrize("direct", [False, True])
    def test_scope_unset_denied(self, store, direct):
        extra = {"BULLHORN_ENABLED_WRITE_SCOPES": None}
        if direct:
            extra["BULLHORN_NOTE_CREATE_MODE"] = "direct"
        mock_targets()
        result = _preview(context(write_env(store, **extra)), dry_run=not direct)
        assert result["status"] == "denied" and "scope:note.create" in result["missing_requirements"]
        assert not write_calls()
        assert not (store.root / "writes" / "pending").exists()

    @respx.mock
    def test_actor_unset_denied(self, store):
        mock_targets()
        e = write_env(store)
        del e["BULLHORN_MCP_ACTOR"]
        result = _preview(context(e))
        assert result["status"] == "denied" and "env:BULLHORN_MCP_ACTOR" in result["missing_requirements"]

    @respx.mock
    def test_no_note_action_mapping_denied_with_exact_requirement(self, tmp_path):
        store = valid_store(tmp_path, actions=[])
        mock_targets()
        result = _preview(context(write_env(store)))
        assert result["status"] == "denied"
        assert result["missing_requirements"] == ["value_mapping:note.action:note_action"]

    @respx.mock
    def test_setup_not_valid_denied(self, store):
        mock_targets()
        store.write_discovery({**store.read_discovery(), "drift_unresolved": True})
        result = _preview(context(write_env(store)))
        assert result["status"] == "denied" and "state:setup_revalidation_required" in result["missing_requirements"]

    @respx.mock
    def test_connection_failure_denied(self, store):
        mock_targets()
        result = _preview(context(write_env(store), connection=ConnectionCheck(ok=False, error="down")))
        assert result["status"] == "denied" and "state:disconnected" in result["missing_requirements"]


# ---------------------------------------------------------------------- #
# AC-8: confirm
# ---------------------------------------------------------------------- #


def _previewed(ctx, **kw):
    mock_targets()
    result = _preview(ctx, **kw)
    assert result["status"] == "previewed", result
    return result


class TestConfirm:
    @respx.mock
    def test_approved_commits_once(self, ctx, store):
        p = _previewed(ctx, associations=[{"type": "job", "id": 200}, {"type": "candidate", "id": 101}])
        put, _ = mock_write(read=readback(job=200, candidates=[101]))
        assoc = respx.put(f"{REST_URL}/entity/Note/{NOTE_ID}/candidates/101").mock(return_value=httpx.Response(200, json={}))
        result = confirm_write(ctx, p["operation_id"], p["preview_hash"], "approve")
        assert result["status"] == "committed", result
        assert result["record_id"] == NOTE_ID and put.call_count == 1 and assoc.call_count == 1
        assert json.loads(put.calls[0].request.content) == p["preview"]["request"]["body"]
        assert [c.request.method for c in respx.calls].count("PUT") == 2
        assert not [c for c in respx.calls if c.request.method == "DELETE"]
        event = result["activity"][0]
        assert event["concept"] == "note_created" and event["origin"] == "written_by_mcp"
        assert event["source"] == {"canonical_entity": "note", "id": NOTE_ID}
        assert {a["status"] for a in result["associations"]} == {"present"}
        assert result["correlation_id"] == p["correlation_id"]

    @respx.mock
    def test_wrong_hash(self, ctx):
        p = _previewed(ctx)
        mock_write()
        result = confirm_write(ctx, p["operation_id"], "0" * 64, "approve")
        assert result["status"] == "refused" and result["reason"] == "hash_mismatch"
        assert not write_calls()

    @respx.mock
    def test_expired(self, store):
        p = _previewed(context(write_env(store)))
        mock_write()
        late = context(write_env(store), now=NOW + dt.timedelta(minutes=30))
        assert confirm_write(late, p["operation_id"], p["preview_hash"], "approve")["reason"] == "expired"
        assert not write_calls()

    @respx.mock
    def test_already_consumed(self, ctx):
        p = _previewed(ctx)
        put, _ = mock_write()
        assert confirm_write(ctx, p["operation_id"], p["preview_hash"], "approve")["status"] == "committed"
        again = confirm_write(ctx, p["operation_id"], p["preview_hash"], "approve")
        assert again["reason"] == "already_consumed" and put.call_count == 1

    @respx.mock
    def test_actor_unset(self, store, ctx):
        p = _previewed(ctx)
        mock_write()
        e = write_env(store)
        del e["BULLHORN_MCP_ACTOR"]
        assert confirm_write(context(e), p["operation_id"], p["preview_hash"], "approve")["reason"] == "actor_missing"
        assert not write_calls()

    @respx.mock
    def test_actor_not_an_approver(self, store, ctx):
        p = _previewed(ctx)
        mock_write()
        e = write_env(store, BULLHORN_WRITE_APPROVERS="boss@example.com, other@example.com")
        assert confirm_write(context(e), p["operation_id"], p["preview_hash"], "approve")["reason"] == "approver_not_allowed"
        assert not write_calls()

    @respx.mock
    def test_listed_approver_commits(self, store, ctx):
        p = _previewed(ctx)
        mock_write()
        e = write_env(store, BULLHORN_WRITE_APPROVERS="admin@example.com")
        assert confirm_write(context(e), p["operation_id"], p["preview_hash"], "approve")["status"] == "committed"

    @respx.mock
    def test_scope_disabled_after_preview(self, store, ctx):
        p = _previewed(ctx)
        mock_write()
        e = write_env(store, BULLHORN_ENABLED_WRITE_SCOPES="")
        assert confirm_write(context(e), p["operation_id"], p["preview_hash"], "approve")["reason"] == "scope_disabled"
        assert not write_calls()

    @respx.mock
    def test_setup_not_valid_after_preview(self, store, ctx):
        p = _previewed(ctx)
        mock_write()
        store.write_discovery({**store.read_discovery(), "drift_unresolved": True})
        result = confirm_write(ctx, p["operation_id"], p["preview_hash"], "approve")
        assert result["reason"] == "gate_failed" and "state:setup_revalidation_required" in result["missing_requirements"]
        assert not write_calls()

    @respx.mock
    def test_note_action_mapping_removed_after_preview(self, tmp_path):
        from ._tenant_helpers import propose_and_commit

        store = valid_store(tmp_path, actions=[NOTE_ACTION])
        ctx = context(write_env(store))
        p = _previewed(ctx)
        mock_write()
        propose_and_commit(store, [{"op": "deactivate_value_mapping", "key": "note.action.screen"}])
        result = confirm_write(ctx, p["operation_id"], p["preview_hash"], "approve")
        assert result["status"] == "denied" and result["reason"] == "gate_failed" and not write_calls()
        assert result["missing_requirements"] == ["value_mapping:note.action:note_action"]

    @respx.mock
    def test_note_action_mapping_missing_direct_mode_denied(self, tmp_path):
        store = valid_store(tmp_path, actions=[])
        mock_targets()
        mock_write()
        result = _preview(context(write_env(store, BULLHORN_NOTE_CREATE_MODE="direct")), dry_run=False)
        assert result["status"] == "denied" and not write_calls()
        assert result["missing_requirements"] == ["value_mapping:note.action:note_action"]

    @respx.mock
    def test_target_changed_is_stale(self, ctx):
        p = _previewed(ctx)
        mock_write()
        respx.get(f"{REST_URL}/entity/Candidate/100").mock(
            return_value=httpx.Response(200, json={"data": dict(CANDIDATE, status="Placed")})
        )
        result = confirm_write(ctx, p["operation_id"], p["preview_hash"], "approve")
        assert result["reason"] == "stale_preview" and not write_calls()

    @respx.mock
    def test_target_deleted_is_stale(self, ctx):
        p = _previewed(ctx)
        mock_write()
        respx.get(f"{REST_URL}/entity/Candidate/100").mock(
            return_value=httpx.Response(200, json={"data": dict(CANDIDATE, isDeleted=True)})
        )
        assert confirm_write(ctx, p["operation_id"], p["preview_hash"], "approve")["reason"] == "stale_preview"
        assert not write_calls()

    @respx.mock
    def test_tampered_pending_file_is_stale(self, ctx, store):
        p = _previewed(ctx)
        mock_write()
        path = store.root / "writes" / "pending" / f"{p['operation_id']}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["op"]["comments"] = "something else entirely"
        path.write_text(json.dumps(data), encoding="utf-8")
        assert confirm_write(ctx, p["operation_id"], p["preview_hash"], "approve")["reason"] == "stale_preview"
        assert not write_calls()

    @respx.mock
    def test_reject_writes_nothing(self, ctx, store):
        p = _previewed(ctx)
        mock_write()
        assert confirm_write(ctx, p["operation_id"], p["preview_hash"], "reject")["status"] == "rejected"
        assert confirm_write(ctx, p["operation_id"], p["preview_hash"], "approve")["reason"] == "already_consumed"
        assert not write_calls()
        assert [j["transition"] for j in _journal(store)] == ["previewed", "rejected"]

    @pytest.mark.parametrize(
        "op_id,decision,reason",
        [("x" * 32, "approve", "unknown_operation"), ("a" * 32, "approve", "unknown_operation"), ("a" * 32, "maybe", "invalid_decision"),
         ("../" * 11, "approve", "unknown_operation"), (None, "approve", "unknown_operation")],
    )
    def test_bad_inputs(self, ctx, op_id, decision, reason):
        with respx.mock(assert_all_called=False) as router:
            assert confirm_write(ctx, op_id, "0" * 64, decision)["reason"] == reason
            assert not router.calls

    @respx.mock
    def test_concurrent_confirms_one_put(self, ctx, store):
        p = _previewed(ctx)
        put, _ = mock_write()
        results = []
        barrier = threading.Barrier(4)

        def run():
            barrier.wait()
            results.append(confirm_write(context(write_env(store)), p["operation_id"], p["preview_hash"], "approve"))

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert put.call_count == 1
        assert sorted(r["status"] for r in results).count("committed") == 1


# ---------------------------------------------------------------------- #
# AC-10: idempotency
# ---------------------------------------------------------------------- #


class TestIdempotency:
    def _commit(self, ctx, **kw):
        p = _previewed(ctx, **kw)
        return confirm_write(ctx, p["operation_id"], p["preview_hash"], "approve")

    @respx.mock
    def test_same_key_same_payload_duplicate(self, ctx):
        put, _ = mock_write()
        first = self._commit(ctx, idempotency_key="abc-1")
        assert first["status"] == "committed"
        second = _preview(ctx, idempotency_key="abc-1")
        assert second["status"] == "duplicate" and second["record_id"] == NOTE_ID
        assert put.call_count == 1

    @respx.mock
    def test_derived_key_duplicate_within_window_only(self, store):
        put, _ = mock_write()
        ctx = context(write_env(store))
        assert self._commit(ctx)["status"] == "committed"
        assert _preview(ctx)["status"] == "duplicate"
        later = context(write_env(store), now=NOW + dt.timedelta(hours=24, seconds=1))
        assert _preview(later)["status"] == "previewed"
        assert put.call_count == 1

    @respx.mock
    def test_same_key_different_payload_rejected(self, ctx):
        mock_write()
        assert self._commit(ctx, idempotency_key="abc-2")["status"] == "committed"
        other = _preview(ctx, idempotency_key="abc-2", comments="different text")
        assert other["status"] == "rejected_validation" and other["errors"][0]["code"] == "idempotency_key_conflict"

    @respx.mock
    def test_pending_entry_is_in_doubt(self, ctx, store):
        mock_write()
        p = _previewed(ctx, idempotency_key="abc-3")
        op = pipeline._op_from_stored(json.loads((store.root / "writes" / "pending" / f"{p['operation_id']}.json").read_text())["op"])
        key = pipeline._key_for(op, "admin@example.com")
        Ledger(store).begin(key, NOW, operation_id="x", correlation_id="y")  # a crash left it pending
        assert _preview(ctx, idempotency_key="abc-3")["status"] == "in_doubt"
        assert confirm_write(ctx, p["operation_id"], p["preview_hash"], "approve")["reason"] == "stale_preview"
        assert not write_calls()

    @respx.mock
    def test_failed_create_frees_key(self, ctx):
        respx.put(f"{REST_URL}/entity/Note").mock(
            side_effect=[httpx.Response(500, text="boom"), httpx.Response(200, json={"changedEntityId": NOTE_ID, "changeType": "INSERT"})]
        )
        respx.get(f"{REST_URL}/entity/Note/{NOTE_ID}").mock(return_value=httpx.Response(200, json={"data": readback()}))
        assert self._commit(ctx, idempotency_key="abc-4")["status"] == "failed"
        assert self._commit(ctx, idempotency_key="abc-4")["status"] == "committed"

    @respx.mock
    def test_transport_error_is_in_doubt(self, ctx):
        respx.put(f"{REST_URL}/entity/Note").mock(side_effect=httpx.ReadTimeout("timeout"))
        assert self._commit(ctx, idempotency_key="abc-5")["status"] == "in_doubt"
        assert _preview(ctx, idempotency_key="abc-5")["status"] == "in_doubt"

    @respx.mock
    def test_unexpected_create_response_is_in_doubt(self, ctx):
        mock_write(created={"changeType": "UPDATE"})
        assert self._commit(ctx)["status"] == "in_doubt"


# ---------------------------------------------------------------------- #
# AC-11: read-back
# ---------------------------------------------------------------------- #


class TestReadBack:
    def _commit(self, ctx, read, assoc_status=200, **kw):
        mock_write(read=read)
        respx.put(f"{REST_URL}/entity/Note/{NOTE_ID}/candidates/101").mock(return_value=httpx.Response(assoc_status, json={}))
        p = _previewed(ctx, associations=[{"type": "job", "id": 200}, {"type": "candidate", "id": 101}], **kw)
        return confirm_write(ctx, p["operation_id"], p["preview_hash"], "approve")

    @respx.mock
    def test_all_present(self, ctx):
        assert self._commit(ctx, readback(job=200, candidates=[101]))["status"] == "committed"

    @respx.mock
    def test_one_missing_partial(self, ctx):
        result = self._commit(ctx, readback(job=200, candidates=[]))
        assert result["status"] == "partially_committed"
        statuses = {(a["type"], a["id"]): a["status"] for a in result["associations"]}
        assert statuses == {("candidate", 100): "present", ("job", 200): "present", ("candidate", 101): "missing"}

    @respx.mock
    def test_association_call_fails_partial(self, ctx):
        result = self._commit(ctx, readback(job=200, candidates=[]), assoc_status=400)
        assert result["status"] == "partially_committed"
        assert [a["status"] for a in result["associations"] if a["id"] == 101] == ["failed"]
        assert not [c for c in respx.calls if c.request.method == "DELETE"]

    @respx.mock
    def test_none_present_orphan(self, ctx, store):
        result = self._commit(ctx, readback(person=None, job=None, candidates=[]))
        assert result["status"] == "failed_orphan" and result["record_id"] == NOTE_ID
        assert not [c for c in respx.calls if c.request.method == "DELETE"]
        assert _journal(store)[-1]["transition"] == "failed_orphan"

    @respx.mock
    def test_readback_error_partial_unverified(self, ctx):
        created = {"changedEntityId": NOTE_ID, "changeType": "INSERT"}
        respx.put(f"{REST_URL}/entity/Note").mock(return_value=httpx.Response(200, json=created))
        respx.get(f"{REST_URL}/entity/Note/{NOTE_ID}").mock(return_value=httpx.Response(500, text="err"))
        p = _previewed(ctx)
        result = confirm_write(ctx, p["operation_id"], p["preview_hash"], "approve")
        assert result["status"] == "partially_committed" and result["associations"][0]["status"] == "unverified"

    @respx.mock
    def test_note_entity_absent_warns(self, ctx):
        result = self._commit(ctx, readback(job=200, candidates=[101], entities=[]))
        assert result["status"] == "partially_committed"
        assert {a["note_entity"] for a in result["associations"]} == {"absent"}
        assert {a["status"] for a in result["associations"]} == {"note_entity_absent"}
        assert any("NoteEntity" in w for w in result["warnings"])

    @respx.mock
    def test_readback_request_fields(self, ctx):
        _, get = mock_write()
        p = _previewed(ctx)
        confirm_write(ctx, p["operation_id"], p["preview_hash"], "approve")
        assert get.calls[0].request.url.params["fields"] == pipeline.READBACK_FIELDS


# ---------------------------------------------------------------------- #
# AC-12: direct mode
# ---------------------------------------------------------------------- #


class TestDirectMode:
    @respx.mock
    def test_direct_commits_in_one_call(self, store):
        mock_targets()
        put, _ = mock_write()
        result = _preview(context(write_env(store, BULLHORN_NOTE_CREATE_MODE="direct")), dry_run=False)
        assert result["status"] == "committed" and put.call_count == 1
        assert all(j.get("mode") == "direct" for j in _journal(store))
        assert not (store.root / "writes" / "pending").exists()

    @respx.mock
    def test_direct_requires_listed_approver(self, store):
        mock_targets()
        mock_write()
        e = write_env(store, BULLHORN_NOTE_CREATE_MODE="direct", BULLHORN_WRITE_APPROVERS="boss@example.com")
        result = _preview(context(e), dry_run=False)
        assert result["status"] == "denied" and not write_calls()


# ---------------------------------------------------------------------- #
# AC-13: audit and journal
# ---------------------------------------------------------------------- #


class TestAuditJournal:
    @respx.mock
    def test_no_comment_text_shared_correlation(self, ctx, store, caplog):
        caplog.set_level(logging.INFO, logger="bullhorn_mcp.audit")
        p = _previewed(ctx)
        mock_write()
        confirm_write(ctx, p["operation_id"], p["preview_hash"], "approve")
        lines = _journal(store)
        assert [j["transition"] for j in lines] == ["previewed", "confirmed", "committed"]
        assert {j["correlation_id"] for j in lines} == {p["correlation_id"]}
        assert all(j["comments_length"] == len(SECRET_COMMENT) and len(j["comments_sha256"]) == 64 for j in lines)
        raw = (store.root / "writes" / "journal.jsonl").read_text(encoding="utf-8")
        assert "Confidential" not in raw and "salary" not in raw
        messages = [r.getMessage() for r in caplog.records if r.name == "bullhorn_mcp.audit"]
        assert len(messages) == 3 and all("Confidential" not in m for m in messages)
        assert all(p["correlation_id"] in m for m in messages)
        assert lines[-1]["bullhorn_response"] == {"changedEntityId": NOTE_ID, "changeType": "INSERT"}

    @respx.mock
    def test_token_never_leaks(self, ctx, store, caplog):
        caplog.set_level(logging.INFO, logger="bullhorn_mcp.audit")
        p = _previewed(ctx)
        respx.put(f"{REST_URL}/entity/Note").mock(
            return_value=httpx.Response(400, text='{"errorMessage":"bad","BhRestToken":"leakme123' + "Z" * 30 + '","password":"hunter2"}')
        )
        result = confirm_write(ctx, p["operation_id"], p["preview_hash"], "approve")
        assert result["status"] == "failed"
        out = json.dumps(result) + (store.root / "writes" / "journal.jsonl").read_text() + "".join(
            r.getMessage() for r in caplog.records if r.name == "bullhorn_mcp.audit"
        )
        assert "leakme123" not in out and "hunter2" not in out and "***" in out
        assert len(result["errors"][0]["message"]) <= 300


@respx.mock
def test_identity_cards(ctx):
    mock_targets([CANDIDATE, CANDIDATE_2, CONTACT, JOB])
    result = create_note(
        ctx, "client_contact", 300, "Client Call", "hi",
        associations=[{"type": "job", "id": 200}, {"type": "candidate", "id": 101}],
    )
    assert result["targets"] == [
        {"type": "client_contact", "id": 300, "name": "Grace Hopper", "client_corporation_id": 9},
        {"type": "job", "id": 200, "title": "Engineer", "status": "Open", "client_corporation_id": 9, "owner_id": 8},
        {"type": "candidate", "id": 101, "name": "Alan Turing", "status": "New", "owner_id": 7},
    ]
