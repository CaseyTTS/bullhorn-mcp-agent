"""Tests for Bullhorn API client."""

import time
import pytest
import httpx
import respx
from unittest.mock import Mock, PropertyMock
from bullhorn_mcp.auth import BullhornAuth, BullhornSession
from bullhorn_mcp.client import BullhornClient, BullhornAPIError, DEFAULT_FIELDS


@pytest.fixture
def mock_auth(mock_session):
    """Create a mock auth object with a valid session."""
    auth = Mock(spec=BullhornAuth)
    type(auth).session = PropertyMock(return_value=mock_session)
    return auth


class TestBullhornClient:
    """Tests for BullhornClient class."""

    @respx.mock
    def test_search_jobs(self, mock_auth, mock_session, sample_job):
        """Test searching for jobs."""
        respx.get(f"{mock_session.rest_url}/search/JobOrder").mock(
            return_value=httpx.Response(
                200,
                json={"data": [sample_job]},
            )
        )

        client = BullhornClient(mock_auth)
        results = client.search("JobOrder", "isOpen:1", count=10)

        assert len(results) == 1
        assert results[0]["id"] == 12345
        assert results[0]["title"] == "Software Engineer"

    @respx.mock
    def test_search_candidates(self, mock_auth, mock_session, sample_candidate):
        """Test searching for candidates."""
        respx.get(f"{mock_session.rest_url}/search/Candidate").mock(
            return_value=httpx.Response(
                200,
                json={"data": [sample_candidate]},
            )
        )

        client = BullhornClient(mock_auth)
        results = client.search("Candidate", "lastName:Smith")

        assert len(results) == 1
        assert results[0]["firstName"] == "John"
        assert results[0]["lastName"] == "Smith"

    @respx.mock
    def test_search_with_custom_fields(self, mock_auth, mock_session):
        """Test search with custom fields."""
        route = respx.get(f"{mock_session.rest_url}/search/JobOrder").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        client = BullhornClient(mock_auth)
        client.search("JobOrder", "isOpen:1", fields="id,title,salary")

        # Check that custom fields were passed
        assert "fields=id%2Ctitle%2Csalary" in str(route.calls[0].request.url)

    @respx.mock
    def test_search_with_sort(self, mock_auth, mock_session):
        """Test search with sort parameter."""
        route = respx.get(f"{mock_session.rest_url}/search/JobOrder").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        client = BullhornClient(mock_auth)
        client.search("JobOrder", "isOpen:1", sort="-dateAdded")

        assert "sort=-dateAdded" in str(route.calls[0].request.url)

    @respx.mock
    def test_search_count_limit(self, mock_auth, mock_session):
        """Test that count is limited to 500."""
        route = respx.get(f"{mock_session.rest_url}/search/JobOrder").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        client = BullhornClient(mock_auth)
        client.search("JobOrder", "isOpen:1", count=1000)  # Request more than max

        # Should be capped at 500
        assert "count=500" in str(route.calls[0].request.url)

    @respx.mock
    def test_query_entities(self, mock_auth, mock_session, sample_job):
        """Test querying entities with WHERE clause."""
        respx.get(f"{mock_session.rest_url}/query/JobOrder").mock(
            return_value=httpx.Response(
                200,
                json={"data": [sample_job]},
            )
        )

        client = BullhornClient(mock_auth)
        results = client.query("JobOrder", "salary > 100000")

        assert len(results) == 1
        assert results[0]["salary"] == 150000

    @respx.mock
    def test_query_with_order_by(self, mock_auth, mock_session):
        """Test query with orderBy parameter."""
        route = respx.get(f"{mock_session.rest_url}/query/JobOrder").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        client = BullhornClient(mock_auth)
        client.query("JobOrder", "isOpen=true", order_by="-dateAdded")

        assert "orderBy=-dateAdded" in str(route.calls[0].request.url)

    @respx.mock
    def test_get_entity_by_id(self, mock_auth, mock_session, sample_job):
        """Test getting a single entity by ID."""
        respx.get(f"{mock_session.rest_url}/entity/JobOrder/12345").mock(
            return_value=httpx.Response(
                200,
                json={"data": sample_job},
            )
        )

        client = BullhornClient(mock_auth)
        result = client.get("JobOrder", 12345)

        assert result["id"] == 12345
        assert result["title"] == "Software Engineer"

    @respx.mock
    def test_get_entity_with_custom_fields(self, mock_auth, mock_session):
        """Test getting entity with custom fields."""
        route = respx.get(f"{mock_session.rest_url}/entity/Candidate/67890").mock(
            return_value=httpx.Response(200, json={"data": {}})
        )

        client = BullhornClient(mock_auth)
        client.get("Candidate", 67890, fields="id,firstName,lastName,email")

        assert "fields=id%2CfirstName%2ClastName%2Cemail" in str(route.calls[0].request.url)

    @respx.mock
    def test_get_meta(self, mock_auth, mock_session):
        """Test getting entity metadata."""
        meta_response = {
            "entity": "JobOrder",
            "fields": [
                {"name": "id", "type": "Integer"},
                {"name": "title", "type": "String"},
            ],
        }
        respx.get(f"{mock_session.rest_url}/meta/JobOrder").mock(
            return_value=httpx.Response(200, json=meta_response)
        )

        client = BullhornClient(mock_auth)
        result = client.get_meta("JobOrder")

        assert result["entity"] == "JobOrder"
        assert len(result["fields"]) == 2

    @respx.mock
    def test_api_error_handling(self, mock_auth, mock_session):
        """Test handling of API errors."""
        respx.get(f"{mock_session.rest_url}/search/JobOrder").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )

        client = BullhornClient(mock_auth)

        with pytest.raises(BullhornAPIError) as exc_info:
            client.search("JobOrder", "isOpen:1")

        assert "500" in str(exc_info.value)

    @respx.mock
    def test_session_refresh_on_401(self, mock_auth, mock_session, sample_job):
        """Test that 401 triggers session refresh and retry."""
        # First call returns 401, second succeeds
        route = respx.get(f"{mock_session.rest_url}/search/JobOrder")
        route.side_effect = [
            httpx.Response(401, text="Unauthorized"),
            httpx.Response(200, json={"data": [sample_job]}),
        ]

        client = BullhornClient(mock_auth)
        results = client.search("JobOrder", "isOpen:1")

        # Should have refreshed session and retried
        assert mock_auth._refresh_session.called
        assert len(results) == 1


class TestPagination:
    """Tests for search and query pagination."""

    @respx.mock
    def test_search_with_start_offset(self, mock_auth, mock_session):
        """Test search with start parameter for pagination."""
        route = respx.get(f"{mock_session.rest_url}/search/JobOrder").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        client = BullhornClient(mock_auth)
        client.search("JobOrder", "isOpen:1", start=50)

        assert "start=50" in str(route.calls[0].request.url)

    @respx.mock
    def test_query_with_start_offset(self, mock_auth, mock_session):
        """Test query with start parameter for pagination."""
        route = respx.get(f"{mock_session.rest_url}/query/JobOrder").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        client = BullhornClient(mock_auth)
        client.query("JobOrder", "salary > 100000", start=100)

        assert "start=100" in str(route.calls[0].request.url)

    @respx.mock
    def test_search_pagination_combined(self, mock_auth, mock_session):
        """Test search with both start and count for pagination."""
        route = respx.get(f"{mock_session.rest_url}/search/Candidate").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        client = BullhornClient(mock_auth)
        client.search("Candidate", "status:Active", count=25, start=75)

        url = str(route.calls[0].request.url)
        assert "start=75" in url
        assert "count=25" in url


class TestDefaultFields:
    """Tests for default field constants."""

    def test_job_order_defaults(self):
        """Test JobOrder default fields."""
        assert "id" in DEFAULT_FIELDS["JobOrder"]
        assert "title" in DEFAULT_FIELDS["JobOrder"]
        assert "status" in DEFAULT_FIELDS["JobOrder"]
        assert "salary" in DEFAULT_FIELDS["JobOrder"]

    def test_candidate_defaults(self):
        """Test Candidate default fields."""
        assert "id" in DEFAULT_FIELDS["Candidate"]
        assert "firstName" in DEFAULT_FIELDS["Candidate"]
        assert "lastName" in DEFAULT_FIELDS["Candidate"]
        assert "email" in DEFAULT_FIELDS["Candidate"]

    def test_placement_defaults(self):
        """Test Placement default fields."""
        assert "id" in DEFAULT_FIELDS["Placement"]
        assert "candidate" in DEFAULT_FIELDS["Placement"]
        assert "jobOrder" in DEFAULT_FIELDS["Placement"]

    def test_client_corporation_defaults(self):
        """Test ClientCorporation default fields."""
        assert "id" in DEFAULT_FIELDS["ClientCorporation"]
        assert "name" in DEFAULT_FIELDS["ClientCorporation"]
        assert "status" in DEFAULT_FIELDS["ClientCorporation"]

    def test_client_contact_defaults(self):
        """Test ClientContact default fields."""
        assert "id" in DEFAULT_FIELDS["ClientContact"]
        assert "firstName" in DEFAULT_FIELDS["ClientContact"]
        assert "clientCorporation" in DEFAULT_FIELDS["ClientContact"]


class TestEdgeCases:
    """Tests for edge cases and error scenarios."""

    @respx.mock
    def test_search_empty_results(self, mock_auth, mock_session):
        """Test search returns empty list when no results."""
        respx.get(f"{mock_session.rest_url}/search/JobOrder").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        client = BullhornClient(mock_auth)
        results = client.search("JobOrder", "title:NonexistentJob12345")

        assert results == []

    @respx.mock
    def test_get_entity_empty_data(self, mock_auth, mock_session):
        """Test get returns empty dict when entity not found."""
        respx.get(f"{mock_session.rest_url}/entity/JobOrder/99999").mock(
            return_value=httpx.Response(200, json={"data": {}})
        )

        client = BullhornClient(mock_auth)
        result = client.get("JobOrder", 99999)

        assert result == {}

    @respx.mock
    def test_query_count_capped_at_500(self, mock_auth, mock_session):
        """Test that query count is also capped at 500."""
        route = respx.get(f"{mock_session.rest_url}/query/JobOrder").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        client = BullhornClient(mock_auth)
        client.query("JobOrder", "isOpen=true", count=1000)

        assert "count=500" in str(route.calls[0].request.url)

    @respx.mock
    def test_search_unknown_entity_uses_id_field(self, mock_auth, mock_session):
        """Test that unknown entity types default to 'id' field."""
        route = respx.get(f"{mock_session.rest_url}/search/UnknownEntity").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        client = BullhornClient(mock_auth)
        client.search("UnknownEntity", "someField:value")

        assert "fields=id" in str(route.calls[0].request.url)

    @respx.mock
    def test_get_candidate_files(self, mock_auth, mock_session):
        respx.get(
            f"{mock_session.rest_url}/entity/Candidate/67890/fileAttachments"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": 111,
                            "name": "John_Smith_Resume.pdf",
                            "contentType": "application/pdf",
                        }
                    ]
                },
            )
        )

        client = BullhornClient(mock_auth)

        result = client.get_candidate_files(67890)

        assert len(result) == 1
        assert result[0]["name"] == "John_Smith_Resume.pdf"


    @respx.mock
    def test_upload_candidate_resume(
        self,
        mock_auth,
        mock_session,
        tmp_path,
    ):
        resume_file = tmp_path / "resume.pdf"
        resume_file.write_bytes(b"%PDF-1.4 fake resume")

        route = respx.put(
            f"{mock_session.rest_url}/file/Candidate/67890/raw"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "fileId": 222,
                    "fileName": "resume.pdf",
                },
            )
        )

        client = BullhornClient(mock_auth)

        result = client.upload_candidate_resume(
            candidate_id=67890,
            file_path=str(resume_file),
        )

        assert result["fileId"] == 222
        assert route.called

    @respx.mock
    def test_upload_candidate_resume_default_params_match_pre_phase1_wire_behavior(
        self,
        mock_auth,
        mock_session,
        tmp_path,
    ):
        """Calling with only candidate_id/file_path must send the exact same
        HTTP params as before file_type/external_id existed."""
        resume_file = tmp_path / "resume.pdf"
        resume_file.write_bytes(b"%PDF-1.4 fake resume")

        route = respx.put(
            f"{mock_session.rest_url}/file/Candidate/67890/raw"
        ).mock(return_value=httpx.Response(200, json={"fileId": 1}))

        client = BullhornClient(mock_auth)

        client.upload_candidate_resume(
            candidate_id=67890,
            file_path=str(resume_file),
        )

        request_url = str(route.calls[0].request.url)
        assert "externalID=Portfolio" in request_url
        assert "fileType=SAMPLE" in request_url

    @respx.mock
    def test_upload_candidate_resume_custom_file_type_and_external_id(
        self,
        mock_auth,
        mock_session,
        tmp_path,
    ):
        resume_file = tmp_path / "resume.pdf"
        resume_file.write_bytes(b"%PDF-1.4 fake resume")

        route = respx.put(
            f"{mock_session.rest_url}/file/Candidate/67890/raw"
        ).mock(return_value=httpx.Response(200, json={"fileId": 1}))

        client = BullhornClient(mock_auth)

        client.upload_candidate_resume(
            candidate_id=67890,
            file_path=str(resume_file),
            file_type="RESUME",
            external_id="Website",
        )

        request_url = str(route.calls[0].request.url)
        assert "externalID=Website" in request_url
        assert "fileType=RESUME" in request_url

    def test_upload_candidate_resume_invalid_candidate_id(self, mock_auth, tmp_path):
        resume_file = tmp_path / "resume.pdf"
        resume_file.write_bytes(b"content")

        client = BullhornClient(mock_auth)

        with pytest.raises(ValueError, match="candidate_id must be a positive integer"):
            client.upload_candidate_resume(candidate_id=0, file_path=str(resume_file))

    def test_upload_candidate_resume_missing_file(self, mock_auth):
        client = BullhornClient(mock_auth)

        with pytest.raises(ValueError, match="Resume file does not exist"):
            client.upload_candidate_resume(
                candidate_id=1, file_path=r"C:\does\not\exist.pdf"
            )


class TestDescribeResumeUpload:
    """Tests for the read-only, network-free describe_resume_upload()."""

    def test_describes_upload_without_network_call(self, mock_auth, tmp_path):
        resume_file = tmp_path / "resume.pdf"
        resume_file.write_bytes(b"%PDF-1.4 some bytes here")

        client = BullhornClient(mock_auth)

        with respx.mock:
            # No routes registered - any network call would raise.
            result = client.describe_resume_upload(
                candidate_id=67890,
                file_path=str(resume_file),
            )

        assert result["candidate_id"] == 67890
        assert result["file_name"] == "resume.pdf"
        assert result["file_size_bytes"] == len(b"%PDF-1.4 some bytes here")
        assert result["content_type"] == "application/pdf"
        assert result["file_type"] == "SAMPLE"
        assert result["external_id"] == "Portfolio"

    def test_describes_upload_with_custom_file_type_and_external_id(self, mock_auth, tmp_path):
        resume_file = tmp_path / "resume.pdf"
        resume_file.write_bytes(b"content")

        client = BullhornClient(mock_auth)

        result = client.describe_resume_upload(
            candidate_id=1,
            file_path=str(resume_file),
            file_type="RESUME",
            external_id="Website",
        )

        assert result["file_type"] == "RESUME"
        assert result["external_id"] == "Website"

    def test_raises_same_value_error_for_invalid_candidate_id(self, mock_auth, tmp_path):
        resume_file = tmp_path / "resume.pdf"
        resume_file.write_bytes(b"content")

        client = BullhornClient(mock_auth)

        with pytest.raises(ValueError, match="candidate_id must be a positive integer"):
            client.describe_resume_upload(candidate_id=-1, file_path=str(resume_file))

    def test_raises_same_value_error_for_missing_file(self, mock_auth):
        client = BullhornClient(mock_auth)

        with pytest.raises(ValueError, match="Resume file does not exist"):
            client.describe_resume_upload(
                candidate_id=1, file_path=r"C:\does\not\exist.pdf"
            )
