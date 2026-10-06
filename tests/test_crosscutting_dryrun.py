"""Tests for dry-run preview rendering."""

from bullhorn_mcp.crosscutting import dryrun


class TestRenderPreview:
    def test_render_preview_shape(self):
        preview = dryrun.render_preview(
            "upload_candidate_resume",
            {"candidate_id": 1, "file_name": "resume.pdf"},
        )

        assert preview == {
            "dry_run": True,
            "tool": "upload_candidate_resume",
            "would_execute": {"candidate_id": 1, "file_name": "resume.pdf"},
        }

    def test_preview_reflects_arbitrary_would_execute_payload(self):
        payload = {"foo": "bar", "count": 3}

        preview = dryrun.render_preview("some_tool", payload)

        assert preview["would_execute"] == payload
        assert preview["tool"] == "some_tool"
        assert preview["dry_run"] is True
