"""5A triage local fixes L-1..L-7 and L-9 (T-L1..T-L7)."""

from __future__ import annotations

import os
import pathlib
import time

import anyio
import httpx
import pytest
import respx

from bullhorn_mcp.auth import BullhornAuth
from bullhorn_mcp.identity import deploy, sessions
from bullhorn_mcp.identity.principal import current_identity
from bullhorn_mcp.identity.session_store import EncryptedFileSessionStore, MemorySessionStore, SessionKeys, new_id
from bullhorn_mcp.identity.verifiers import JwksTokenVerifier

from . import _identity_helpers
from ._identity_helpers import ALICE, ISSUER, JWKS, RESOURCE, TK1, admin_config, caller, link, make_jwt, pk
from .test_oauth_routes import APP

shared = _identity_helpers.shared  # fixture


class TestL1:
    def test_oversized_confirm_body_refused_before_buffering(self, shared):
        async def flow():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=APP), base_url=_identity_helpers.BASE) as c:
                c.cookies.set("__Host-bhmcp_login", "A" * 43)
                big = await c.post("/oauth/bullhorn/confirm", content=b"link=" + b"x" * 10_000,
                                   headers={"content-type": "application/x-www-form-urlencoded"})
                assert big.status_code == 413

                async def chunks():
                    for _ in range(10):
                        yield b"x" * 1000

                streamed = await c.post("/oauth/bullhorn/confirm", content=chunks(),
                                        headers={"content-type": "application/x-www-form-urlencoded"})
                assert streamed.status_code == 413

        anyio.run(flow)


class _FakeJwksClient:
    def __init__(self):
        self.calls = 0

    def get_jwk_set(self, refresh=False):
        from jwt import PyJWKSet

        self.calls += 1
        return PyJWKSet.from_dict(JWKS)


class TestL2:
    def test_unknown_kid_refetches_at_most_once_per_minute(self, monkeypatch):
        fake = _FakeJwksClient()
        v = JwksTokenVerifier(allowed_issuers=frozenset({ISSUER}), resource_server_url=RESOURCE, jwks_client=fake)
        assert anyio.run(v.verify_token, make_jwt(ALICE)) is not None and fake.calls == 1
        for _ in range(5):  # unknown kids within the minute never trigger another fetch
            assert anyio.run(v.verify_token, make_jwt(ALICE, kid="rotated")) is None
        assert fake.calls == 1
        assert anyio.run(v.verify_token, make_jwt(ALICE)) is not None and fake.calls == 1  # known kid: cached
        clock = time.monotonic() + 61
        monkeypatch.setattr("bullhorn_mcp.identity.verifiers.time.monotonic", lambda: clock)
        for _ in range(5):
            anyio.run(v.verify_token, make_jwt(ALICE, kid="rotated"))
        assert fake.calls == 2  # exactly one refetch once the minute has passed


class TestL3:
    def test_memory_pending_pruned(self, monkeypatch):
        m = MemorySessionStore()
        old = new_id()
        m.put_login(old, {"state": "s"})
        m._pending[("login", old)] = (time.time() - 4000, m._pending[("login", old)][1])
        m.put_login(new_id(), {"state": "t"})  # every put prunes
        assert m.get_login(old) is None and len(m._pending) == 1

    def test_encrypted_pending_pruned(self, tmp_path):
        store = EncryptedFileSessionStore(tmp_path, SessionKeys({"k1": os.urandom(32)}, "k1"))
        old = new_id()
        store.put_login(old, {"state": "s"})
        path = store._pending_path("login", old)
        stale = time.time() - 4000
        os.utime(path, (stale, stale))
        assert store.prune() == 1 and store.get_login(old) is None

    def test_locks_are_bounded(self, monkeypatch):
        monkeypatch.setattr(sessions, "MAX_LOCKS", 5)
        sessions.reset_caches()
        held = sessions.principal_lock(TK1, pk("held"))
        with held:
            for i in range(50):
                sessions.principal_lock(TK1, pk(f"u{i}"))
            assert len(sessions._locks) <= 6 and (TK1, pk("held")) in sessions._locks
        sessions.reset_caches()


class TestL4:
    def test_too_long_session_dir_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(deploy, "MAX_PATH_CHARS", len(str(tmp_path)) + 20)
        with pytest.raises(deploy.DeploymentError, match="too long"):
            deploy.parse_admin_config(admin_config(tmp_path))


class TestL5:
    def test_absolute_lifetime(self, shared):
        rec = link(shared.store, TK1, ALICE)
        rec.created_at = time.time() - 31 * 86_400
        shared.store.put(rec)
        with caller(ALICE):
            assert current_identity().access_tier == "workspace_only"
        assert shared.store.get(TK1, pk(ALICE)) is None

    @pytest.mark.parametrize("days", [0, 91, "30", True])
    def test_max_session_days_bounds(self, tmp_path, days):
        cfg = admin_config(tmp_path)
        cfg["session_store"]["max_session_days"] = days
        with pytest.raises(deploy.DeploymentError, match="max_session_days"):
            deploy.parse_admin_config(cfg)

    def test_configured_lifetime(self, tmp_path):
        cfg = admin_config(tmp_path)
        cfg["session_store"]["max_session_days"] = 90
        assert deploy.parse_admin_config(cfg).session_max_days == 90

    def test_more_than_one_worker_refused(self, shared):
        with pytest.raises(deploy.DeploymentError, match="one worker"):
            deploy.run_args({"WEB_CONCURRENCY": "2"})
        assert deploy.run_args({"WEB_CONCURRENCY": "1"}) == {"transport": "streamable-http"}


class TestL7:
    @respx.mock
    def test_plain_http_bullhorn_host_not_trusted(self, sample_config):
        respx.get(f"{sample_config.auth_url}/oauth/authorize").mock(
            return_value=httpx.Response(302, headers={"location": "http://auth-west.bullhornstaffing.com/cb?code=c1"})
        )
        auth = BullhornAuth(sample_config)
        assert auth._get_auth_code() == "c1" and auth._regional_auth_url is None

    @respx.mock
    def test_plain_http_redirect_not_followed(self, sample_config):
        respx.get(f"{sample_config.auth_url}/oauth/authorize").mock(
            return_value=httpx.Response(307, headers={"location": "http://auth-west.bullhornstaffing.com/oauth/authorize"})
        )
        follow = respx.get("http://auth-west.bullhornstaffing.com/oauth/authorize").mock(return_value=httpx.Response(200))
        with pytest.raises(Exception, match="Failed to get auth code"):
            BullhornAuth(sample_config)._get_auth_code()
        assert not follow.called


def test_l9_changes_py_is_crlf():
    data = (pathlib.Path(__file__).parents[1] / "src" / "bullhorn_mcp" / "tenant" / "changes.py").read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n") > 0
