"""Tests for MCP server tools."""

import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from bullhorn_mcp import server
from bullhorn_mcp.auth import AuthenticationError
from bullhorn_mcp.client import BullhornAPIError


@pytest.fixture
def mock_client(sample_job, sample_candidate):
    """Create a mock Bullhorn client."""
    client = Mock()
    client.search.return_value = [sample_job]
    client.query.return_value = [sample_job]
    client.get.return_value = sample_job
    return client


@pytest.fixture(autouse=True)
def reset_client():
    """Reset the global client before each test."""
    server._client = None
    yield
    server._client = None


class TestListJobs:
    """Tests for list_jobs tool."""

    def test_list_jobs_basic(self, mock_client, sample_job):
        """Test basic job listing."""
        with patch.object(server, "get_client", return_value=mock_client):
            result = server.list_jobs()

        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["title"] == "Software Engineer"
        mock_client.search.assert_called_once()

    def test_list_jobs_with_query(self, mock_client):
        """Test job listing with query parameter."""
        with patch.object(server, "get_client", return_value=mock_client):
            server.list_jobs(query="title:Engineer")

        call_args = mock_client.search.call_args
        assert "title:Engineer" in call_args.kwargs["query"]

    def test_list_jobs_with_status(self, mock_client):
        """Test job listing with status filter."""
        with patch.object(server, "get_client", return_value=mock_client):
            server.list_jobs(status="Open")

        call_args = mock_client.search.call_args
        assert 'status:"Open"' in call_args.kwargs["query"]

    def test_list_jobs_with_limit(self, mock_client):
        """Test job listing with custom limit."""
        with patch.object(server, "get_client", return_value=mock_client):
            server.list_jobs(limit=50)

        call_args = mock_client.search.call_args
        assert call_args.kwargs["count"] == 50

    def test_list_jobs_error_handling(self, mock_client):
        """Test error handling in list_jobs."""
        mock_client.search.side_effect = BullhornAPIError("API Error")

        with patch.object(server, "get_client", return_value=mock_client):
            result = server.list_jobs()

        assert "ERROR:" in result
        assert "API Error" in result


class TestListCandidates:
    """Tests for list_candidates tool."""

    def test_list_candidates_basic(self, mock_client, sample_candidate):
        """Test basic candidate listing."""
        mock_client.search.return_value = [sample_candidate]

        with patch.object(server, "get_client", return_value=mock_client):
            result = server.list_candidates()

        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["firstName"] == "John"

    def test_list_candidates_with_query(self, mock_client):
        """Test candidate listing with query."""
        with patch.object(server, "get_client", return_value=mock_client):
            server.list_candidates(query="skillSet:Python")

        call_args = mock_client.search.call_args
        assert "skillSet:Python" in call_args.kwargs["query"]

    def test_list_candidates_auth_error(self, mock_client):
        """Test authentication error handling."""
        mock_client.search.side_effect = AuthenticationError("Auth failed")

        with patch.object(server, "get_client", return_value=mock_client):
            result = server.list_candidates()

        assert "ERROR:" in result
        assert "Auth failed" in result


class TestGetJob:
    """Tests for get_job tool."""

    def test_get_job_by_id(self, mock_client, sample_job):
        """Test getting a job by ID."""
        with patch.object(server, "get_client", return_value=mock_client):
            result = server.get_job(job_id=12345)

        data = json.loads(result)
        assert data["id"] == 12345
        mock_client.get.assert_called_with(
            entity="JobOrder", entity_id=12345, fields=None
        )

    def test_get_job_with_fields(self, mock_client):
        """Test getting a job with custom fields."""
        with patch.object(server, "get_client", return_value=mock_client):
            server.get_job(job_id=12345, fields="id,title,salary")

        mock_client.get.assert_called_with(
            entity="JobOrder", entity_id=12345, fields="id,title,salary"
        )


class TestGetCandidate:
    """Tests for get_candidate tool."""

    def test_get_candidate_by_id(self, mock_client, sample_candidate):
        """Test getting a candidate by ID."""
        mock_client.get.return_value = sample_candidate

        with patch.object(server, "get_client", return_value=mock_client):
            result = server.get_candidate(candidate_id=67890)

        data = json.loads(result)
        assert data["firstName"] == "John"
        assert data["lastName"] == "Smith"


class TestSearchEntities:
    """Tests for search_entities tool."""

    def test_search_placements(self, mock_client):
        """Test searching placements."""
        mock_client.search.return_value = [{"id": 1, "status": "Approved"}]

        with patch.object(server, "get_client", return_value=mock_client):
            result = server.search_entities(
                entity="Placement", query="status:Approved"
            )

        data = json.loads(result)
        assert data[0]["status"] == "Approved"
        mock_client.search.assert_called_with(
            entity="Placement",
            query="status:Approved",
            fields=None,
            count=20,
        )

    def test_search_with_limit(self, mock_client):
        """Test search with custom limit."""
        with patch.object(server, "get_client", return_value=mock_client):
            server.search_entities(
                entity="ClientCorporation", query="name:Acme*", limit=100
            )

        call_args = mock_client.search.call_args
        assert call_args.kwargs["count"] == 100


class TestQueryEntities:
    """Tests for query_entities tool."""

    def test_query_with_where(self, mock_client):
        """Test query with WHERE clause."""
        with patch.object(server, "get_client", return_value=mock_client):
            server.query_entities(
                entity="JobOrder", where="salary > 100000"
            )

        mock_client.query.assert_called_with(
            entity="JobOrder",
            where="salary > 100000",
            fields=None,
            count=20,
            order_by=None,
        )

    def test_query_with_order_by(self, mock_client):
        """Test query with ORDER BY."""
        with patch.object(server, "get_client", return_value=mock_client):
            server.query_entities(
                entity="Candidate",
                where="status='Active'",
                order_by="-dateAdded",
            )

        call_args = mock_client.query.call_args
        assert call_args.kwargs["order_by"] == "-dateAdded"


class TestFormatResponse:
    """Tests for response formatting."""

    def test_format_list(self):
        """Test formatting a list response."""
        data = [{"id": 1}, {"id": 2}]
        result = server.format_response(data)

        parsed = json.loads(result)
        assert len(parsed) == 2

    def test_format_dict(self):
        """Test formatting a dict response."""
        data = {"id": 1, "name": "Test"}
        result = server.format_response(data)

        parsed = json.loads(result)
        assert parsed["id"] == 1

    def test_format_with_datetime(self):
        """Test formatting handles non-serializable types."""
        from datetime import datetime

        data = {"date": datetime(2024, 1, 1)}
        # Should not raise an error
        result = server.format_response(data)
        assert "2024" in result


class TestCandidateFiles:
    """Tests for candidate file MCP tools."""

    def test_get_candidate_files(self, mock_client):
        mock_client.get_candidate_files.return_value = [
            {
                "id": 111,
                "name": "John_Smith_Resume.pdf",
                "contentType": "application/pdf",
            }
        ]

        with patch.object(server, "get_client", return_value=mock_client):
            result = server.get_candidate_files(candidate_id=67890)

        data = json.loads(result)

        assert len(data) == 1
        assert data[0]["name"] == "John_Smith_Resume.pdf"
        mock_client.get_candidate_files.assert_called_once_with(67890)

    def test_upload_candidate_resume(self, mock_client):
        mock_client.upload_candidate_resume.return_value = {
            "fileId": 222,
            "fileName": "resume.pdf",
        }

        with patch.object(server, "get_client", return_value=mock_client):
            result = server.upload_candidate_resume(
                candidate_id=67890,
                file_path=r"C:\temp\resume.pdf",
            )

        data = json.loads(result)

        assert data["fileId"] == 222
        mock_client.upload_candidate_resume.assert_called_once_with(
            candidate_id=67890,
            file_path=r"C:\temp\resume.pdf",
        )

class TestConnectionStatus:
    """Tests for connection_status tool."""

    REQUIRED_VARS = [
        "BULLHORN_CLIENT_ID",
        "BULLHORN_CLIENT_SECRET",
        "BULLHORN_USERNAME",
        "BULLHORN_PASSWORD",
    ]

    def test_all_required_vars_missing(self, monkeypatch):
        """When no required env vars are set, status reports not configured."""
        for name in self.REQUIRED_VARS:
            monkeypatch.delenv(name, raising=False)

        result = server.connection_status()
        data = json.loads(result)

        assert data["configured"] is False
        assert data["connected"] is False
        assert sorted(data["missing_variables"]) == sorted(self.REQUIRED_VARS)

    def test_some_required_vars_missing(self, monkeypatch):
        """When only some required env vars are set, missing_variables lists exactly the gaps."""
        monkeypatch.setenv("BULLHORN_CLIENT_ID", "id123")
        monkeypatch.setenv("BULLHORN_USERNAME", "user123")
        monkeypatch.delenv("BULLHORN_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("BULLHORN_PASSWORD", raising=False)

        result = server.connection_status()
        data = json.loads(result)

        assert data["configured"] is False
        assert data["connected"] is False
        assert sorted(data["missing_variables"]) == sorted(
            ["BULLHORN_CLIENT_SECRET", "BULLHORN_PASSWORD"]
        )

    def test_all_vars_present_connection_succeeds(self, monkeypatch):
        """When all required env vars are set and the client connects, report success."""
        for name in self.REQUIRED_VARS:
            monkeypatch.setenv(name, "value")

        mock_session = Mock()
        mock_session.rest_url = "https://rest99.bullhornstaffing.com/rest-services/abc/"

        mock_client = Mock()
        mock_client.auth.session = mock_session

        with patch.object(server, "get_client", return_value=mock_client):
            result = server.connection_status()

        data = json.loads(result)

        assert data["configured"] is True
        assert data["connected"] is True
        assert data["rest_url"] == "https://rest99.bullhornstaffing.com/rest-services/abc/"

    def test_all_vars_present_connection_fails(self, monkeypatch):
        """When all required env vars are set but connecting raises, report failure with message."""
        for name in self.REQUIRED_VARS:
            monkeypatch.setenv(name, "value")

        with patch.object(
            server, "get_client", side_effect=Exception("boom: connection refused")
        ):
            result = server.connection_status()

        data = json.loads(result)

        assert data["configured"] is True
        assert data["connected"] is False
        assert "boom: connection refused" in data["message"]


class TestGetRecentPlacements:
    """Tests for get_recent_placements tool."""

    def test_days_too_low(self, mock_client):
        with patch.object(server, "get_client", return_value=mock_client):
            result = server.get_recent_placements(days=0)

        assert result == "ERROR: days must be between 1 and 365"
        mock_client.query.assert_not_called()

    def test_days_too_high(self, mock_client):
        with patch.object(server, "get_client", return_value=mock_client):
            result = server.get_recent_placements(days=366)

        assert result == "ERROR: days must be between 1 and 365"
        mock_client.query.assert_not_called()

    def test_limit_too_low(self, mock_client):
        with patch.object(server, "get_client", return_value=mock_client):
            result = server.get_recent_placements(limit=0)

        assert result == "ERROR: limit must be between 1 and 500"
        mock_client.query.assert_not_called()

    def test_limit_too_high(self, mock_client):
        with patch.object(server, "get_client", return_value=mock_client):
            result = server.get_recent_placements(limit=501)

        assert result == "ERROR: limit must be between 1 and 500"
        mock_client.query.assert_not_called()

    def test_valid_inputs_calls_client_query(self, mock_client):
        days = 30
        limit = 50

        before = datetime.now(timezone.utc) - timedelta(days=days)
        before_ms = int(before.timestamp() * 1000)

        with patch.object(server, "get_client", return_value=mock_client):
            server.get_recent_placements(days=days, limit=limit)

        after = datetime.now(timezone.utc) - timedelta(days=days)
        after_ms = int(after.timestamp() * 1000)

        mock_client.query.assert_called_once()
        call_kwargs = mock_client.query.call_args.kwargs

        assert call_kwargs["entity"] == "Placement"
        assert call_kwargs["fields"] is None
        assert call_kwargs["count"] == limit
        assert call_kwargs["order_by"] == "-dateAdded"

        where = call_kwargs["where"]
        assert where.startswith("dateAdded >= ")
        cutoff_ms = int(where.split(">=")[1].strip())
        assert before_ms <= cutoff_ms <= after_ms

    def test_auth_error_handling(self, mock_client):
        mock_client.query.side_effect = AuthenticationError("Auth failed")

        with patch.object(server, "get_client", return_value=mock_client):
            result = server.get_recent_placements()

        assert result == "ERROR: Auth failed"

    def test_api_error_handling(self, mock_client):
        mock_client.query.side_effect = BullhornAPIError("API Error")

        with patch.object(server, "get_client", return_value=mock_client):
            result = server.get_recent_placements()

        assert result == "ERROR: API Error"


class TestMCPServerSetup:
    """Tests for MCP server configuration."""

    def test_server_has_tools(self):
        """Test that all expected tools are registered."""
        tools = list(server.mcp._tool_manager._tools.keys())

        assert "list_jobs" in tools
        assert "list_candidates" in tools
        assert "get_job" in tools
        assert "get_candidate" in tools
        assert "search_entities" in tools
        assert "query_entities" in tools

    def test_server_name(self):
        """Test server name is set correctly."""
        assert server.mcp.name == "Bullhorn CRM"
