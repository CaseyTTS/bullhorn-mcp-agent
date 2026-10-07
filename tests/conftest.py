"""Shared test fixtures."""

import pytest
from bullhorn_mcp.config import BullhornConfig
from bullhorn_mcp.auth import BullhornAuth, BullhornSession
from bullhorn_mcp.bullhorn.client import BullhornClient


@pytest.fixture
def sample_config():
    """Create a sample configuration for testing."""
    return BullhornConfig(
        client_id="test_client_id",
        client_secret="test_client_secret",
        username="test_user",
        password="test_password",
        auth_url="https://auth.bullhornstaffing.com",
        login_url="https://rest.bullhornstaffing.com",
    )


@pytest.fixture
def mock_session():
    """Create a mock Bullhorn session."""
    import time
    return BullhornSession(
        bh_rest_token="test_token_123",
        rest_url="https://rest99.bullhornstaffing.com/rest-services/abc123",  # No trailing slash
        expires_at=time.time() + 600,
    )


@pytest.fixture
def sample_job():
    """Sample job order data."""
    return {
        "id": 12345,
        "title": "Software Engineer",
        "status": "Open",
        "employmentType": "Direct Hire",
        "dateAdded": 1704067200000,
        "salary": 150000,
        "isOpen": True,
        "numOpenings": 2,
        "description": "We are looking for a skilled software engineer...",
    }


@pytest.fixture
def sample_candidate():
    """Sample candidate data."""
    return {
        "id": 67890,
        "firstName": "John",
        "lastName": "Smith",
        "email": "john.smith@example.com",
        "phone": "555-1234",
        "status": "Active",
        "dateAdded": 1704067200000,
        "occupation": "Software Developer",
    }
