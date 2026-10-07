"""Phase 4B review triage: regression tests for the blocking fixes B-1..B-6 (B-7 is a line-ending check)."""

import json
import logging
import threading
from pathlib import Path

import httpx
import pytest
import respx

from bullhorn_mcp.auth import AuthenticationError
from bullhorn_mcp.writes import ledger as ledger_mod
from bullhorn_mcp.writes import pipeline
from bullhorn_mcp.writes.ledger import IdempotencyKey, Ledger
from bullhorn_mcp.writes.pipeline import confirm_write, create_note

from ._notes_helpers import (
    CURRENT_USER_ID,
    NOTE_ACTION,
    NOTE_ID,
    SECRET_COMMENT,
    TOKEN,
    context,
    hv_b11_verified,  # noqa: F401 - fixture
    make_client,
    mock_targets,
    mock_write,
    readback,
    valid_store,
    write_calls,
    write_env,
)
from ._tenant_helpers import NOW, REST_URL, propose_and_commit

CREATED = {"changedEntityId": NOTE_ID, "changeType": "INSERT"}


def _create(ctx, **kw):
    args = {"target_type": "candidate", "target_id": 100, "action_type": "Screen Call", "comments": SECRET_COMMENT}
    args.update(kw)
    return create_note(ctx, **args)


def _journal(store):
    path = store.root / "writes" / "journal.jsonl"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _key(op_kw=None, actor="admin@example.com"):
    kw = {"target_type": "candidate", "target_id": 100, "action_type": "Screen Call", "comments": SECRET_COMMENT,
          "associations": None, "idempotency_key": None}
    kw.update(op_kw or {})
    op, errors = pipeline.parse_op(**kw)
    assert not errors
    return pipeline._key_for(op, actor)


@pytest.fixture
def store(tmp_path):
    return valid_store(tmp_path)


def direct(store, **extra):
    return context(write_env(store, BULLHORN_NOTE_CREATE_MODE="direct", **extra))


# ---------------------------------------------------------------------- #
# B-1: confirm_write gate -> denied
# ---------------------------------------------------------------------- #


class TestB1:
    @respx.mock
    def test_confirm_without_note_action_mapping_is_denied(self, tmp_path, hv_b11_verified):  # noqa: F811
        store = valid_store(tmp_path, actions=[NOTE_ACTION])
        ctx = context(write_env(store))
        mock_targets()
        p = _create(ctx)
        mock_write()
        propose_and_commit(store, [{"op": "deactivate_value_mapping", "key": "note.action.screen"}])
        result = confirm_write(ctx, p["operation_id"], p["preview_hash"], "approve")
        assert result["status"] == "denied" and result["reason"] == "gate_failed"
        assert result["missing_requirements"] == ["value_mapping:note.action:note_action"]
        assert not write_calls()

    @respx.mock
    def test_direct_without_note_action_mapping_is_denied(self, tmp_path, hv_b11_verified):  # noqa: F811
        store = valid_store(tmp_path, actions=[])
        mock_targets()
        mock_write()
        result = _create(direct(store), dry_run=False)
        assert result["status"] == "denied" and result["missing_requirements"] == ["value_mapping:note.action:note_action"]
        assert not write_calls()


# ---------------------------------------------------------------------- #
# B-2: HV-B11 / commentingPerson / NoteEntity
# ---------------------------------------------------------------------- #


class TestB2:
    @respx.mock
    def test_verified_body_has_resolved_commenting_person(self, store, hv_b11_verified):  # noqa: F811
        mock_targets()
        put, _ = mock_write()
        calls = []

        def resolver():
            calls.append(1)
            return CURRENT_USER_ID

        ctx = context(write_env(store, BULLHORN_NOTE_CREATE_MODE="direct"), current_user=resolver)
        result = _create(ctx, dry_run=False)
        assert result["status"] == "committed"
        assert json.loads(put.calls[0].request.content)["commentingPerson"] == {"id": CURRENT_USER_ID}
        _create(ctx, dry_run=False, comments="another note")
        assert len(calls) == 1  # cached per session

    def test_commenting_person_not_injectable(self, store, hv_b11_verified):  # noqa: F811
        with respx.mock(assert_all_called=False) as router:
            result = _create(context(write_env(store)), associations=[{"type": "job", "id": 1, "commentingPerson": {"id": 1}}])
            assert not router.calls
        assert result["status"] == "rejected_validation"
        with pytest.raises(TypeError):
            create_note(context(write_env(store)), "candidate", 100, "Screen Call", "x", commentingPerson={"id": 1})

    @pytest.mark.parametrize("target_type", ["candidate", "client_contact"])
    @pytest.mark.parametrize("resolver", [None, lambda: CURRENT_USER_ID])
    def test_unresolved_guard_rejects_person_targets(self, store, target_type, resolver):
        # Production default: HV_B11_VERIFIED is False (a resolver alone does not enable the write).
        assert pipeline.HV_B11_VERIFIED is False
        ctx = context(write_env(store, BULLHORN_NOTE_CREATE_MODE="direct"), current_user=resolver)
        with respx.mock(assert_all_called=False) as router:
            for dry_run in (True, False):
                result = _create(ctx, target_type=target_type, dry_run=dry_run)
                assert result["status"] == "rejected_validation"
                assert [e["code"] for e in result["errors"]] == ["unsupported_association"]
            assert not router.calls

    def test_unresolved_guard_rejects_person_associations(self, store):
        with respx.mock(assert_all_called=False) as router:
            result = _create(context(write_env(store)), associations=[{"type": "candidate", "id": 101}])
            assert not router.calls
        assert {e["code"] for e in result["errors"]} == {"unsupported_association"}

    @respx.mock
    def test_readback_without_note_entity_is_partial(self, store, hv_b11_verified):  # noqa: F811
        mock_targets()
        mock_write(read=readback(entities=[]))
        result = _create(direct(store), dry_run=False)
        assert result["status"] == "partially_committed"
        assert [a["status"] for a in result["associations"]] == ["note_entity_absent"]

    @respx.mock
    def test_readback_note_entities_unusable_is_partial(self, store, hv_b11_verified):  # noqa: F811
        mock_targets()
        data = readback()
        data["entities"] = "garbage"
        mock_write(read=data)
        result = _create(direct(store), dry_run=False)
        assert result["status"] == "partially_committed" and result["associations"][0]["status"] == "unverified"

    @respx.mock
    def test_resolver_failure_stops_before_write(self, store, hv_b11_verified):  # noqa: F811
        mock_targets()
        mock_write()

        def broken():
            raise RuntimeError("no user")

        result = _create(context(write_env(store, BULLHORN_NOTE_CREATE_MODE="direct"), current_user=broken), dry_run=False)
        assert result["status"] == "rejected_target" and not write_calls()


# ---------------------------------------------------------------------- #
# B-3: generation-numbered ledger
# ---------------------------------------------------------------------- #


def _failed_generation(ledger, key):
    assert ledger.begin(key, NOW, operation_id="o", correlation_id="c").status == "new"
    ledger.finish(key, 1, NOW, "failed")


class TestB3:
    def test_deterministic_interleaving_one_new(self, store, monkeypatch):
        key = IdempotencyKey("caller", "k2", "p" * 64)
        ledger = Ledger(store)
        _failed_generation(ledger, key)
        stale_path = ledger._generation_path(key, 1)
        stale = ledger_mod.read_json(stale_path)
        a = ledger.begin(key, NOW, operation_id="", correlation_id="a")
        assert a.status == "new" and a.generation == 2
        # Writer B read the stale (failed) generation before A's create, then acts on it.
        original = ledger_mod.read_json
        calls = {"n": 0}

        def stale_first(path):
            calls["n"] += 1
            return stale if calls["n"] == 1 else original(path)

        monkeypatch.setattr(ledger_mod, "read_json", stale_first)
        b = ledger.begin(key, NOW, operation_id="", correlation_id="b")
        assert b.status == "in_doubt"

    def test_losing_exclusive_create_never_new(self, store, monkeypatch):
        key = IdempotencyKey("derived", "d" * 64, "d" * 64)
        ledger = Ledger(store)
        _failed_generation(ledger, key)
        real_current = ledger.current_generation
        winner = {}

        def current_then_race(k):
            n = real_current(k)
            if not winner:  # another writer creates generation n+1 right after we listed
                winner["v"] = Ledger(store).begin(k, NOW, operation_id="w", correlation_id="w")
            return n

        monkeypatch.setattr(ledger, "current_generation", current_then_race)
        assert winner == {} and ledger.begin(key, NOW, operation_id="l", correlation_id="l").status == "in_doubt"
        assert winner["v"].status == "new"

    @respx.mock
    def test_two_writers_after_failure_one_put(self, store, hv_b11_verified):  # noqa: F811
        mock_targets()
        put = respx.put(f"{REST_URL}/entity/Note").mock(
            side_effect=[httpx.Response(500, text="boom")] + [httpx.Response(200, json=CREATED)] * 5
        )
        respx.get(f"{REST_URL}/entity/Note/{NOTE_ID}").mock(return_value=httpx.Response(200, json={"data": readback()}))
        assert _create(direct(store), dry_run=False, idempotency_key="k2")["status"] == "failed"
        results = []
        barrier = threading.Barrier(2)

        def run():
            barrier.wait()
            results.append(_create(direct(store), dry_run=False, idempotency_key="k2")["status"])

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert put.call_count == 2  # the failed one + exactly one more
        assert results.count("committed") == 1

    def test_threaded_stress_fifty_iterations(self, store):
        ledger = Ledger(store)
        for i in range(50):
            key = IdempotencyKey("caller", f"stress-{i}", "s" * 64)
            _failed_generation(ledger, key)
            verdicts = []
            barrier = threading.Barrier(4)

            def run(key=key):
                barrier.wait()
                verdicts.append(Ledger(store).begin(key, NOW, operation_id="x", correlation_id="y").status)

            threads = [threading.Thread(target=run) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert verdicts.count("new") == 1, (i, verdicts)

    def test_no_replace_or_rename_in_ledger(self):
        source = Path(ledger_mod.__file__).read_text(encoding="utf-8")
        assert "os.replace" not in source and "os.rename" not in source


# ---------------------------------------------------------------------- #
# B-4: 200 with a non-JSON / non-object body
# ---------------------------------------------------------------------- #


class TestB4:
    @respx.mock
    @pytest.mark.parametrize("body", ['"ok"', "[]", "null", "<html>ok</html>"])
    def test_in_doubt_and_key_not_reusable(self, store, body, hv_b11_verified):  # noqa: F811
        mock_targets()
        put = respx.put(f"{REST_URL}/entity/Note").mock(return_value=httpx.Response(200, text=body))
        first = _create(direct(store), dry_run=False, idempotency_key="k1")
        assert first["status"] == "in_doubt"
        assert Ledger(store).lookup(_key({"idempotency_key": "k1"}), NOW).status == "in_doubt"
        assert json.loads(_journal(store).splitlines()[-1])["outcome"] == "in_doubt"
        assert _create(direct(store), dry_run=False, idempotency_key="k1")["status"] == "in_doubt"
        assert put.call_count == 1


# ---------------------------------------------------------------------- #
# B-5: redaction forms and comment scrubbing
# ---------------------------------------------------------------------- #

REDACTION_CORPUS = [
    ('{"errorMessage":"invalid BhRestToken","BhRestToken":"tok en value 1"}', "tok en value 1"),
    ("BhRestToken=tokvalue2 with space&x=1", "tokvalue2 with space"),
    ("BhRestToken%22%3A%22tokvalue3%20spaced%22", "tokvalue3"),
    ("BhRestToken%3Dtokvalue4%26x%3D1", "tokvalue4"),
    ('{\\"BhRestToken\\":\\"tok value 5\\"}', "tok value 5"),
    ("Bh-Rest-Token: tok value 6", "tok value 6"),
    ("bhresttoken=TOKVALUE7", "TOKVALUE7"),
    ('{"access_token" : "acc ess 8"}', "acc ess 8"),
    ("password='pa ss 9'", "pa ss 9"),
    ('BH_REST_TOKEN: "tok 10"', "tok 10"),
    ('{"refresh_token":"ref resh 11"}', "ref resh 11"),
    ('{"BhRestToken" : "short1"}', "short1"),
    ('{"bhRestToken":"' + TOKEN + '"}', TOKEN),
    ('{"password":"p@ss w0rd!"}', "w0rd"),
    ("Bh-Rest-Token: abcdefghijklmnopqrstuvwxyz0123", "abcdefghijklmnopqrstuvwxyz0123"),
]


class TestB5:
    @respx.mock
    @pytest.mark.parametrize("body,secret", REDACTION_CORPUS)
    def test_token_never_leaks(self, store, body, secret, caplog, hv_b11_verified):  # noqa: F811
        caplog.set_level(logging.DEBUG)
        mock_targets()
        respx.put(f"{REST_URL}/entity/Note").mock(return_value=httpx.Response(400, text=body))
        result = _create(direct(store), dry_run=False)
        assert result["status"] == "failed"
        assert secret not in json.dumps(result) + _journal(store) + caplog.text

    @respx.mock
    @pytest.mark.parametrize(
        "make_body",
        [
            lambda c: json.dumps({"errorMessage": "bad value: " + c}),
            lambda c: "bad value: " + c,
            lambda c: "x" * 7000 + c,
            lambda c: "second line only: " + c.splitlines()[1],
            lambda c: json.dumps({"errorMessage": c.splitlines()[0]}),
        ],
    )
    def test_comment_echo_scrubbed(self, store, make_body, caplog, hv_b11_verified):  # noqa: F811
        caplog.set_level(logging.INFO, logger="bullhorn_mcp.audit")
        comment = 'First "line" of the secret note\nSecond confidential line here\nshort\nFourth line, salary 123k'
        mock_targets()
        respx.put(f"{REST_URL}/entity/Note").mock(return_value=httpx.Response(400, text=make_body(comment)))
        result = _create(direct(store), dry_run=False, comments=comment)
        assert result["status"] == "failed"
        audit = "".join(r.getMessage() for r in caplog.records if r.name == "bullhorn_mcp.audit")
        haystack = json.dumps(result) + _journal(store) + audit
        for fragment in ("secret note", "confidential line", "salary 123k", "line\\\" of the"):
            assert fragment not in haystack, fragment
        message = result["errors"][0]["message"]
        assert "<comments:" in message or len(message) == 300

    @respx.mock
    def test_preview_may_echo_text(self, store, hv_b11_verified):  # noqa: F811
        mock_targets()
        result = _create(context(write_env(store)))
        assert result["preview"]["request"]["body"]["comments"] == SECRET_COMMENT
        assert "Confidential" not in _journal(store)


# ---------------------------------------------------------------------- #
# B-6: AuthenticationError during create
# ---------------------------------------------------------------------- #


class TestB6:
    @respx.mock
    def test_refresh_failure_is_failed_and_key_reusable(self, store, hv_b11_verified):  # noqa: F811
        mock_targets()
        client = make_client()
        client.auth._refresh_session.side_effect = AuthenticationError("refresh failed: BhRestToken=leakme" + "q" * 30)
        put = respx.put(f"{REST_URL}/entity/Note").mock(
            side_effect=[httpx.Response(401, text="expired"), httpx.Response(200, json=CREATED)]
        )
        respx.get(f"{REST_URL}/entity/Note/{NOTE_ID}").mock(return_value=httpx.Response(200, json={"data": readback()}))
        e = write_env(store, BULLHORN_NOTE_CREATE_MODE="direct")
        first = _create(context(e, client=client), dry_run=False, idempotency_key="auth-1")
        assert first["status"] == "failed" and first["errors"][0]["code"] == "authentication_error"
        key = _key({"idempotency_key": "auth-1"})
        assert Ledger(store).lookup(key, NOW).status == "new"
        lines = [json.loads(line) for line in _journal(store).splitlines()]
        assert lines[-1]["transition"] == "failed" and lines[-1]["outcome"] == "failed"
        assert "leakme" not in _journal(store) and "leakme" not in json.dumps(first)
        second = _create(context(e), dry_run=False, idempotency_key="auth-1")
        assert second["status"] == "committed" and put.call_count == 2
        assert Ledger(store).current_generation(key) == 2

    @respx.mock
    def test_unexpected_exception_after_send_is_in_doubt(self, store, hv_b11_verified, monkeypatch):  # noqa: F811
        mock_targets()

        class Exploding:
            def create(self, *a, **k):
                raise RuntimeError("socket closed")

        ctx = direct(store)
        ctx.writer = Exploding()
        assert _create(ctx, dry_run=False, idempotency_key="boom")["status"] == "in_doubt"
        assert Ledger(store).lookup(_key({"idempotency_key": "boom"}), NOW).status == "in_doubt"
