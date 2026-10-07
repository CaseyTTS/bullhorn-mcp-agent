"""Phase 5B / P4B-8: the raw error body is private, and comments are scrubbed before secrets are redacted (D-5B-7, AC-14..AC-16)."""

import html
import json
import logging
from urllib.parse import quote, quote_plus

import httpx
import pytest
import respx

from bullhorn_mcp.bullhorn.errors import BullhornAPIError
from bullhorn_mcp.bullhorn.writes import (
    EntityWriter,
    raw_error_body,
    redact_secrets,
    safe_error_text,
    scrub_text,
)
from bullhorn_mcp.schema.errors import truncate_text
from bullhorn_mcp.writes.pipeline import create_note

from ._notes_helpers import TOKEN, context, hv_b11_verified, make_client, mock_targets, valid_store, write_env  # noqa: F401
from ._tenant_helpers import REST_URL

COMMENT = "password=Spring2024 synthetic interview recap, applicant prefers remote; follow up Tuesday"
MULTILINE = "Synthetic first line of the note\nsecond line mentions password=Hunter22x here\nshort\nfinal line quotes \"budget\" detail"


def fragments(text, n=8):
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def assert_no_fragment(comment, haystack):
    for line in comment.splitlines():
        for frag in fragments(line):
            assert frag not in haystack, frag


def _error_from(body, status=400):
    with respx.mock:
        respx.put(f"{REST_URL}/entity/Note").mock(return_value=httpx.Response(status, text=body))
        with pytest.raises(BullhornAPIError) as info:
            EntityWriter(make_client()).create("Note", {"comments": COMMENT})
    return info.value


class TestRawBodyIsPrivate:
    def test_str_repr_args_never_contain_body(self):
        body = json.dumps({"errorMessage": "bad value: " + COMMENT, "BhRestToken": TOKEN})
        exc = _error_from(body)
        rendered = str(exc) + repr(exc) + repr(exc.args) + "".join(str(a) for a in exc.args)
        assert rendered.count("API request failed: 400") >= 2
        assert "Spring2024" not in rendered and TOKEN not in rendered and "errorMessage" not in rendered
        assert_no_fragment(COMMENT, rendered)
        assert raw_error_body(exc) == body  # kept, privately, for safe_error_text

    def test_logging_the_exception_does_not_render_body(self, caplog):
        caplog.set_level(logging.DEBUG)
        exc = _error_from("bad value: " + COMMENT)
        try:
            raise exc
        except BullhornAPIError:
            logging.getLogger("bullhorn_mcp.test").exception("write failed")
        assert "Spring2024" not in caplog.text
        assert_no_fragment(COMMENT, caplog.text)

    def test_no_body_attribute_for_other_errors(self):
        assert raw_error_body(BullhornAPIError("x")) is None and raw_error_body("text") is None


class TestScrubBeforeRedact:
    @pytest.mark.parametrize(
        "make_body",
        [
            lambda c: json.dumps({"errorMessage": "bad value: " + c}),
            lambda c: "bad value: " + c,
            lambda c: "echo [" + c[5:40] + "] and [" + c[45:70] + "] end",  # split, mid-line pieces
            lambda c: json.dumps({"e": c}, ensure_ascii=True),
            lambda c: "q=" + quote(c),  # URL-encoded
            lambda c: "q=" + quote_plus(c),
            lambda c: "<p>" + html.escape(c) + "</p>",
            lambda c: "x" * 7000 + c,
            lambda c: c + "x" * 250_000,
            lambda c: "BhRestToken=" + TOKEN + "&msg=" + c,
        ],
        ids=["json", "plain", "split", "ascii-escaped", "url", "url-plus", "html", "long-prefix", "long-suffix", "with-token"],
    )
    @pytest.mark.parametrize("comment", [COMMENT, MULTILINE], ids=["secret-like", "multiline"])
    def test_no_comment_fragment_survives(self, make_body, comment):
        exc = _error_from(make_body(comment))
        out = safe_error_text(exc, scrub=comment)
        assert len(out) <= 300 and TOKEN not in out
        assert_no_fragment(comment, out)

    def test_marker_present(self):
        out = safe_error_text(_error_from("bad value: " + COMMENT), scrub=COMMENT)
        assert out.startswith("BullhornAPIError: API request failed: 400 - bad value: <comments:")

    def test_secret_like_comment_regression(self):
        """P4B-8: before 5B, redaction ran first and broke the echo, so scrubbing missed the rest of the line."""
        body = json.dumps({"errorMessage": "bad value: " + COMMENT})
        # The 4B order: the transport redacted the body, then the pipeline scrubbed, redacted and bounded.
        redacted_first = redact_secrets(f"BullhornAPIError: API request failed: 400 - {body}")
        legacy = redact_secrets(truncate_text(redact_secrets(scrub_text(redacted_first, COMMENT)), 300))
        fixed = safe_error_text(_error_from(body), scrub=COMMENT)
        assert "applicant prefers remote" in legacy  # the defect being closed
        assert "applicant prefers remote" not in fixed and "Spring2024" not in fixed

    @pytest.mark.parametrize(
        "text",
        ["plain error", "e" * 100_000, '{"BhRestToken":"' + TOKEN + '"}', "x" * 270 + "password=abcdefgh"],
        ids=["plain", "long", "token", "cut-secret"],
    )
    def test_no_raw_body_behaviour_unchanged(self, text):
        legacy = redact_secrets(truncate_text(redact_secrets(scrub_text(text[:201_000], None)), 300))
        assert safe_error_text(text) == legacy
        assert safe_error_text(ValueError(text)) == redact_secrets(
            truncate_text(redact_secrets(scrub_text(f"ValueError: {text}"[:201_000], None)), 300)
        )

    def test_read_errors_still_show_redacted_body(self):
        with respx.mock:
            respx.get(f"{REST_URL}/entity/JobOrder/7/notes").mock(
                return_value=httpx.Response(404, text='{"errorMessage":"nope","BhRestToken":"' + TOKEN + '"}')
            )
            with pytest.raises(BullhornAPIError) as info:
                EntityWriter(make_client()).fetch_to_many("JobOrder", 7, "notes", "id", 0, 5)
        out = safe_error_text(info.value)
        assert "nope" in out and TOKEN not in out and "404" in out


class TestEndToEnd:
    @respx.mock
    @pytest.mark.parametrize("comment", [COMMENT, MULTILINE], ids=["secret-like", "multiline"])
    def test_result_journal_and_logs(self, tmp_path, caplog, comment, hv_b11_verified):  # noqa: F811
        caplog.set_level(logging.DEBUG)
        store = valid_store(tmp_path)
        mock_targets()
        body = json.dumps({"errorMessage": "invalid comments: " + comment, "BhRestToken": TOKEN})
        respx.put(f"{REST_URL}/entity/Note").mock(return_value=httpx.Response(400, text=body))
        ctx = context(write_env(store, BULLHORN_NOTE_CREATE_MODE="direct"))
        result = create_note(ctx, target_type="candidate", target_id=100, action_type="Screen Call", comments=comment, dry_run=False)
        assert result["status"] == "failed"
        journal = (store.root / "writes" / "journal.jsonl").read_text(encoding="utf-8")
        haystack = json.dumps(result) + journal + caplog.text
        assert TOKEN not in haystack
        assert_no_fragment(comment, haystack)
        assert "<comments:" in result["errors"][0]["message"]
