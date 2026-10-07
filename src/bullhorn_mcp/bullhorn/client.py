"""Bullhorn REST API client."""

import httpx
import mimetypes
import re
from pathlib import Path
from typing import Any

from ..auth import BullhornAuth
from .errors import BullhornAPIError


# Default fields for common entities
DEFAULT_FIELDS = {
    "JobOrder": "id,title,status,employmentType,dateAdded,startDate,salary,clientCorporation,owner,description,numOpenings,isOpen",
    "Candidate": "id,firstName,lastName,email,phone,status,dateAdded,occupation,skillSet,owner",
    "Placement": "id,candidate,jobOrder,status,dateBegin,dateEnd,salary,payRate",
    "ClientCorporation": "id,name,status,phone,address",
    "ClientContact": "id,firstName,lastName,email,phone,clientCorporation",
}

_ENTITY_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}", re.ASCII)


def _check_entity(entity: object) -> str:
    """Reject anything but a plain Bullhorn entity name before it reaches a URL path."""
    if type(entity) is not str or _ENTITY_NAME_RE.fullmatch(entity) is None:
        raise BullhornAPIError("Invalid entity name")
    return entity


def _check_entity_id(entity_id: object) -> int:
    """Reject anything but an int (not bool) entity id before it reaches a URL path."""
    if type(entity_id) is not int:
        raise BullhornAPIError("Invalid entity id")
    return entity_id


class BullhornClient:
    """Client for interacting with Bullhorn REST API."""

    def __init__(self, auth: BullhornAuth):
        self.auth = auth

    def _request(
        self, method: str, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make authenticated request to Bullhorn API."""
        session = self.auth.session

        url = f"{session.rest_url}{endpoint}"
        headers = {"BhRestToken": session.bh_rest_token}

        with httpx.Client() as client:
            response = client.request(method, url, params=params, headers=headers)

            if response.status_code == 401:
                # Session expired, force refresh and retry
                self.auth._refresh_session()
                session = self.auth.session
                headers = {"BhRestToken": session.bh_rest_token}
                response = client.request(method, url, params=params, headers=headers)

            if response.status_code != 200:
                raise BullhornAPIError(
                    f"API request failed: {response.status_code} - {response.text}"
                )

            return response.json()

    def search(
        self,
        entity: str,
        query: str,
        fields: str | None = None,
        count: int = 20,
        start: int = 0,
        sort: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search entities using Lucene query syntax.

        Args:
            entity: Entity type (JobOrder, Candidate, etc.)
            query: Lucene query string (e.g., "isOpen:1 AND title:Engineer")
            fields: Comma-separated fields to return (default: entity-specific)
            count: Max results (1-500)
            start: Starting offset for pagination
            sort: Sort field with direction (e.g., "-dateAdded" for descending)

        Returns:
            List of matching entities
        """
        entity = _check_entity(entity)
        if fields is None:
            fields = DEFAULT_FIELDS.get(entity, "id")

        params = {
            "query": query,
            "fields": fields,
            "count": min(count, 500),
            "start": start,
        }

        if sort:
            params["sort"] = sort

        result = self._request("GET", f"/search/{entity}", params)
        return result.get("data", [])

    def query(
        self,
        entity: str,
        where: str,
        fields: str | None = None,
        count: int = 20,
        start: int = 0,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query entities using JPQL-like syntax.

        Args:
            entity: Entity type (JobOrder, Candidate, etc.)
            where: WHERE clause (e.g., "status='Active' AND salary > 50000")
            fields: Comma-separated fields to return
            count: Max results (1-500)
            start: Starting offset for pagination
            order_by: Order by clause (e.g., "-dateAdded")

        Returns:
            List of matching entities
        """
        entity = _check_entity(entity)
        if fields is None:
            fields = DEFAULT_FIELDS.get(entity, "id")

        params = {
            "where": where,
            "fields": fields,
            "count": min(count, 500),
            "start": start,
        }

        if order_by:
            params["orderBy"] = order_by

        result = self._request("GET", f"/query/{entity}", params)
        return result.get("data", [])

    def get(
        self, entity: str, entity_id: int, fields: str | None = None
    ) -> dict[str, Any]:
        """Get a single entity by ID.

        Args:
            entity: Entity type (JobOrder, Candidate, etc.)
            entity_id: Entity ID
            fields: Comma-separated fields to return

        Returns:
            Entity data
        """
        entity = _check_entity(entity)
        entity_id = _check_entity_id(entity_id)
        if fields is None:
            fields = DEFAULT_FIELDS.get(entity, "*")

        params = {"fields": fields}
        result = self._request("GET", f"/entity/{entity}/{entity_id}", params)
        return result.get("data", {})

    def get_meta(self, entity: str) -> dict[str, Any]:
        """Get metadata/schema for an entity type.

        Args:
            entity: Entity type (JobOrder, Candidate, etc.)

        Returns:
            Entity metadata including available fields
        """
        entity = _check_entity(entity)
        params = {"fields": "*"}
        return self._request("GET", f"/meta/{entity}", params)

    def get_candidate_files(self, candidate_id: int) -> list[dict[str, Any]]:
        """Get file attachments for a Bullhorn Candidate."""
        if candidate_id <= 0:
            raise ValueError("candidate_id must be a positive integer")

        result = self._request(
            "GET",
            f"/entity/Candidate/{candidate_id}/fileAttachments",
            params={"fields": "*"},
        )

        return result.get("data", [])

    def upload_candidate_resume(
        self,
        candidate_id: int,
        file_path: str,
        file_type: str = "SAMPLE",
        external_id: str = "Portfolio",
    ) -> dict[str, Any]:
        """Upload a resume file to a Bullhorn Candidate."""
        if candidate_id <= 0:
            raise ValueError("candidate_id must be a positive integer")

        path = Path(file_path)

        if not path.exists() or not path.is_file():
            raise ValueError(f"Resume file does not exist: {file_path}")

        session = self.auth.session

        url = (
            f"{session.rest_url}"
            f"/file/Candidate/{candidate_id}/raw"
        )

        headers = {
            "BhRestToken": session.bh_rest_token,
        }

        content_type = (
            mimetypes.guess_type(path.name)[0]
            or "application/octet-stream"
        )

        params = {
            "externalID": external_id,
            "fileType": file_type,
        }

        with path.open("rb") as file_handle:
            files = {
                "file": (
                    path.name,
                    file_handle,
                    content_type,
                )
            }

            with httpx.Client() as client:
                response = client.put(
                    url,
                    params=params,
                    headers=headers,
                    files=files,
                )

                if response.status_code == 401:
                    self.auth._refresh_session()
                    session = self.auth.session

                    headers = {
                        "BhRestToken": session.bh_rest_token,
                    }

                    url = (
                        f"{session.rest_url}"
                        f"/file/Candidate/{candidate_id}/raw"
                    )

                    file_handle.seek(0)

                    response = client.put(
                        url,
                        params=params,
                        headers=headers,
                        files=files,
                    )

                if response.status_code != 200:
                    raise BullhornAPIError(
                        "Resume upload failed: "
                        f"{response.status_code} - {response.text}"
                    )

                return response.json()

    def describe_resume_upload(
        self,
        candidate_id: int,
        file_path: str,
        file_type: str = "SAMPLE",
        external_id: str = "Portfolio",
    ) -> dict[str, Any]:
        """Describe what an upload_candidate_resume call would do.

        Performs the same validation as upload_candidate_resume (candidate_id
        must be positive, file must exist) and returns a descriptive dict -
        but makes no network call whatsoever. This powers dry-run previews so
        that a dry-run surfaces the same validation errors a live call would.
        """
        if candidate_id <= 0:
            raise ValueError("candidate_id must be a positive integer")

        path = Path(file_path)

        if not path.exists() or not path.is_file():
            raise ValueError(f"Resume file does not exist: {file_path}")

        content_type = (
            mimetypes.guess_type(path.name)[0]
            or "application/octet-stream"
        )

        return {
            "candidate_id": candidate_id,
            "file_name": path.name,
            "file_size_bytes": path.stat().st_size,
            "content_type": content_type,
            "file_type": file_type,
            "external_id": external_id,
        }
