"""Phase 5B triage B-1: entity name / id validation in BullhornClient before any URL path is built (R-B1a..R-B1e)."""

import ast
import time
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import pytest
import respx

from bullhorn_mcp import server
from bullhorn_mcp.auth import BullhornAuth, BullhornSession
from bullhorn_mcp.bullhorn.client import BullhornAPIError, BullhornClient
from bullhorn_mcp.schema.bullhorn_catalog import load_bullhorn_catalog

REST_URL = "https://rest99.bullhornstaffing.com/rest-services/abc123"
CLIENT_SOURCE = Path(__file__).resolve().parents[1] / "src" / "bullhorn_mcp" / "bullhorn" / "client.py"

CORPUS = [
    "../settings/commentActionList",
    "x/../../settings/commentActionList",
    "%2e%2e%2fsettings",
    "..%2Fsettings",
    "..\\settings",
    "Job?x=1",
    "Job#a",
    "Job Order",
    " Job",
    "Job\n",
    "\uff2aobOrder",  # fullwidth J
    "J\u00f6b",
    "",
    "A" * 65,
    "1Job",
    None,
    123,
    ["JobOrder"],
]
IDS = [f"case{i:02d}" for i in range(len(CORPUS))]


def make_client() -> BullhornClient:
    auth = Mock(spec=BullhornAuth)
    auth.session = BullhornSession(bh_rest_token="tok", rest_url=REST_URL, expires_at=time.time() + 600)
    return BullhornClient(auth)


CALLS = {
    "search": lambda c, e: c.search(e, "id:1"),
    "query": lambda c, e: c.query(e, "id=1"),
    "get": lambda c, e: c.get(e, 1),
    "get_meta": lambda c, e: c.get_meta(e),
}


class TestRB1aClient:
    @pytest.mark.parametrize("method", list(CALLS))
    @pytest.mark.parametrize("entity", CORPUS, ids=IDS)
    def test_rejected_without_request(self, method, entity):
        with respx.mock(assert_all_called=False) as router:
            router.route().mock(return_value=httpx.Response(200, json={"data": []}))
            with pytest.raises(BullhornAPIError) as info:
                CALLS[method](make_client(), entity)
            assert not router.calls
        assert str(info.value) == "Invalid entity name"


class TestRB1bTools:
    @pytest.mark.parametrize("tool", ["search_entities", "query_entities"])
    @pytest.mark.parametrize("entity", CORPUS, ids=IDS)
    def test_tool_error_without_echo(self, tool, entity):
        kwargs = {"query": "id:1"} if tool == "search_entities" else {"where": "id=1"}
        with respx.mock(assert_all_called=False) as router:
            router.route().mock(return_value=httpx.Response(200, json={"data": []}))
            with patch.object(server, "get_client", return_value=make_client()):
                out = getattr(server, tool)(entity=entity, **kwargs)
            assert not router.calls
        assert out.startswith("ERROR:") and "Invalid entity name" in out
        if isinstance(entity, str) and entity:
            assert entity not in out


class TestRB1cEntityId:
    @pytest.mark.parametrize("entity_id", ["1/../../settings/x", True, False, 1.5, None, "1"])
    def test_rejected_without_request(self, entity_id):
        with respx.mock(assert_all_called=False) as router:
            router.route().mock(return_value=httpx.Response(200, json={"data": {}}))
            with pytest.raises(BullhornAPIError) as info:
                make_client().get("JobOrder", entity_id)
            assert not router.calls
        assert str(info.value) == "Invalid entity id"


class TestRB1dLegitimateNames:
    @pytest.mark.parametrize("entity", list(load_bullhorn_catalog().entities))
    def test_paths_unchanged(self, entity):
        with respx.mock(assert_all_called=False) as router:
            router.route().mock(return_value=httpx.Response(200, json={"data": []}))
            client = make_client()
            client.search(entity, "id:1")
            client.query(entity, "id=1")
            client.get(entity, 42)
            client.get_meta(entity)
            paths = [c.request.url.raw_path.split(b"?")[0] for c in router.calls]
        base = b"/rest-services/abc123"
        assert paths == [
            base + f"/search/{entity}".encode(),
            base + f"/query/{entity}".encode(),
            base + f"/entity/{entity}/42".encode(),
            base + f"/meta/{entity}".encode(),
        ]

    def test_candidate_id_paths_unaffected(self):
        with respx.mock(assert_all_called=False) as router:
            router.route().mock(return_value=httpx.Response(200, json={"data": []}))
            make_client().get_candidate_files(7)
            assert router.calls[0].request.url.raw_path.split(b"?")[0] == b"/rest-services/abc123/entity/Candidate/7/fileAttachments"
        with pytest.raises(ValueError):
            make_client().get_candidate_files(0)


class TestRB1eGrep:
    def test_every_interpolating_method_checks_first(self):
        tree = ast.parse(CLIENT_SOURCE.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BullhornClient")
        checked = 0
        for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
            interpolated = {
                v.value.id
                for js in ast.walk(fn)
                if isinstance(js, ast.JoinedStr) and js.values and isinstance(js.values[0], ast.Constant)
                and str(js.values[0].value).startswith("/")
                for v in js.values
                if isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name)
            }
            if not interpolated & {"entity", "entity_id"}:
                continue
            body = fn.body[1:] if isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant) else fn.body
            first = ast.unparse(body[0])
            assert first == "entity = _check_entity(entity)", fn.name
            if "entity_id" in interpolated:
                assert ast.unparse(body[1]) == "entity_id = _check_entity_id(entity_id)", fn.name
            checked += 1
        assert checked == 4
