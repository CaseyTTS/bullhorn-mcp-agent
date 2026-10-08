"""Phase 5A deployment modes, admin config and startup refusals (AC-4, SR-2, A3-5, SA3-6, HV-M7)."""

from __future__ import annotations

import copy
import logging

import pytest
import yaml

from bullhorn_mcp.auth.secrets import CredentialSource, SecretRef, SecretRefError, is_reference
from bullhorn_mcp.identity import deploy
from bullhorn_mcp.identity.session_store import KeyringSessionStore, MemorySessionStore

from ._identity_helpers import (
    CLIENT_SECRET,
    CLIENT_SECRET_ENV,
    HOST,
    SESSION_KEYS_ENV,
    TK1,
    TK2,
    admin_config,
    session_keys_text,
)


@pytest.fixture(autouse=True)
def _reset():
    deploy.reset()
    yield
    deploy.reset()


def _write(tmp_path, cfg, name="admin.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return str(path)


def _env(path, **extra):
    env = {deploy.ADMIN_CONFIG_ENV_VAR: path, SESSION_KEYS_ENV: session_keys_text(), CLIENT_SECRET_ENV: CLIENT_SECRET}
    env.update(extra)
    return env


def _refused(cfg, tmp_path, **env_extra):
    with pytest.raises(deploy.DeploymentError) as info:
        deploy.startup(_env(_write(tmp_path, cfg), **env_extra), creds=CredentialSource(_env("", **env_extra)))
    return str(info.value)


class TestLocalDefault:
    def test_no_admin_config_is_local(self):
        assert deploy.startup({}) is deploy.LOCAL
        assert deploy.fastmcp_auth_kwargs({}) == {}
        assert deploy.run_args({}) == {}
        assert not deploy.is_shared()

    def test_local_config_file(self, tmp_path):
        dep = deploy.startup({deploy.ADMIN_CONFIG_ENV_VAR: _write(tmp_path, {"mode": "local"})})
        assert dep.mode == "local" and not deploy.is_shared()

    def test_local_config_rejects_other_keys(self, tmp_path):
        with pytest.raises(deploy.DeploymentError):
            deploy.startup({deploy.ADMIN_CONFIG_ENV_VAR: _write(tmp_path, {"mode": "local", "tenants": {}})})

    @pytest.mark.parametrize("transport", ["streamable-http", "sse"])
    def test_local_with_http_transport_refused(self, transport):
        with pytest.raises(deploy.DeploymentError, match="local mode runs only on stdio"):
            deploy.run_args({deploy.TRANSPORT_ENV_VAR: transport})

    def test_local_stdio_explicit_is_fine(self):
        assert deploy.run_args({deploy.TRANSPORT_ENV_VAR: "stdio"}) == {}

    def test_server_main_runs_stdio_in_local_mode(self, monkeypatch):
        from bullhorn_mcp import server

        calls = []
        monkeypatch.setattr(server.mcp, "run", lambda **kw: calls.append(kw))
        server.main()
        assert calls == [{}]


class TestSharedStartup:
    def test_valid_shared_startup(self, tmp_path):
        path = _write(tmp_path, admin_config(tmp_path))
        env = _env(path)
        kwargs = deploy.fastmcp_auth_kwargs(env)
        assert deploy.is_shared()
        assert kwargs["stateless_http"] is True
        assert kwargs["token_verifier"] is not None
        assert kwargs["auth"].validate_token_resource is True
        assert str(kwargs["auth"].resource_server_url) == "https://mcp.example.test/mcp"
        ts = kwargs["transport_security"]
        assert ts.enable_dns_rebinding_protection is True and ts.allowed_hosts == [HOST]  # HV-M7
        assert kwargs["host"] == "127.0.0.1"
        assert deploy.run_args({}) == {"transport": "streamable-http"}

    @pytest.mark.parametrize("transport", ["stdio", "sse"])
    def test_shared_on_stdio_refused(self, tmp_path, transport):
        deploy.startup(_env(_write(tmp_path, admin_config(tmp_path))), creds=CredentialSource(_env("")))
        with pytest.raises(deploy.DeploymentError, match="streamable-http"):
            deploy.run_args({deploy.TRANSPORT_ENV_VAR: transport})

    def test_missing_session_keys_refused(self, tmp_path):
        env = _env(_write(tmp_path, admin_config(tmp_path)))
        del env[SESSION_KEYS_ENV]
        with pytest.raises(deploy.DeploymentError, match="session_store"):
            deploy.startup(env, creds=CredentialSource(env))
        assert not deploy.is_shared()

    def test_bad_session_key_length_refused(self, tmp_path):
        msg = _refused(admin_config(tmp_path), tmp_path, **{SESSION_KEYS_ENV: session_keys_text(k1=b"\x01" * 16)})
        assert "32 bytes" in msg

    @pytest.mark.parametrize("missing", ["auth", "session_store", "server", "tenants", "service_base_url"])
    def test_shared_without_required_section_refused(self, tmp_path, missing):
        cfg = admin_config(tmp_path)
        del cfg[missing]
        assert "missing required key" in _refused(cfg, tmp_path)

    def test_shared_without_verifier_refused(self, tmp_path):
        cfg = admin_config(tmp_path)
        del cfg["auth"]["verifier"]
        _refused(cfg, tmp_path)

    def test_keyring_store_refused_in_shared_mode(self, tmp_path):
        dep = deploy.parse_admin_config(admin_config(tmp_path))
        store = MemorySessionStore()
        store.local_only = True  # what KeyringSessionStore declares
        assert KeyringSessionStore.local_only is True
        with pytest.raises(deploy.DeploymentError, match="local-only"):
            deploy.activate(dep, session_store=store)
        with pytest.raises(deploy.DeploymentError):
            deploy.activate(dep, session_store=None)

    def test_legacy_identity_env_vars_warned_and_ignored(self, tmp_path, caplog):
        env = _env(_write(tmp_path, admin_config(tmp_path)), BULLHORN_MCP_ACTOR="someone")
        with caplog.at_level(logging.WARNING, logger="bullhorn_mcp.identity"):
            deploy.startup(env, creds=CredentialSource(env))
        assert "BULLHORN_MCP_ACTOR" in caplog.text and "ignored" in caplog.text


class TestConfigConstraints:
    """A3-5: strict schema; secrets by reference only; https base; allowed_hosts required."""

    def _mutate(self, tmp_path, fn):
        cfg = copy.deepcopy(admin_config(tmp_path))
        fn(cfg)
        return _refused(cfg, tmp_path)

    def test_literal_client_secret_refused_and_not_echoed(self, tmp_path):
        def fn(cfg):
            cfg["tenants"][TK1]["bullhorn_oauth"]["client_secret"] = "literal-secret-value-xyz"

        msg = self._mutate(tmp_path, fn)
        assert "literal secret" in msg and "literal-secret-value-xyz" not in msg

    @pytest.mark.parametrize(
        "path",
        [
            ("tenants", TK1, "bullhorn_oauth", "client_secret_ref"),
            ("session_store", "keys_ref"),
        ],
    )
    def test_literal_value_under_ref_refused(self, tmp_path, path):
        def fn(cfg):
            node = cfg
            for p in path[:-1]:
                node = node[p]
            node[path[-1]] = "literal-secret-value-xyz"

        msg = self._mutate(tmp_path, fn)
        assert "reference" in msg and "literal-secret-value-xyz" not in msg

    def test_literal_keys_refused(self, tmp_path):
        msg = self._mutate(tmp_path, lambda cfg: cfg["session_store"].update(keys="AAAA"))
        assert "literal secret" in msg

    @pytest.mark.parametrize("url", ["http://mcp.example.test", "https://user:pw@mcp.example.test", "https://mcp.example.test/x"])
    def test_service_base_url_must_be_https_origin(self, tmp_path, url):
        self._mutate(tmp_path, lambda cfg: cfg.update(service_base_url=url))

    def test_allowed_hosts_required(self, tmp_path):
        assert "allowed_hosts" in self._mutate(tmp_path, lambda cfg: cfg["server"].update(allowed_hosts=[]))

    def test_unknown_key_refused(self, tmp_path):
        assert "unknown key" in self._mutate(tmp_path, lambda cfg: cfg.update(surprise=1))

    def test_wrong_type_refused(self, tmp_path):
        self._mutate(tmp_path, lambda cfg: cfg["server"].update(port="8000"))

    def test_untrusted_bullhorn_auth_url_refused(self, tmp_path):
        self._mutate(tmp_path, lambda cfg: cfg["tenants"][TK1]["bullhorn_oauth"].update(auth_url="https://bullhornstaffing.com.attacker.example"))

    def test_redirect_uri_must_be_the_service_callback(self, tmp_path):
        self._mutate(tmp_path, lambda cfg: cfg["tenants"][TK1]["bullhorn_oauth"].update(redirect_uri="https://evil.example/cb"))

    def test_multiple_tenants_without_claim_refused(self, tmp_path):
        cfg = admin_config(tmp_path, tenants=2)
        assert "tenant_claim" in _refused(cfg, tmp_path)

    def test_duplicate_alias_refused(self, tmp_path):
        cfg = admin_config(tmp_path, tenants=2, tenant_claim="bh_tenant")
        cfg["tenants"][TK2]["alias"] = "one"
        assert "unique" in _refused(cfg, tmp_path)

    def test_bad_tenant_key_refused(self, tmp_path):
        cfg = admin_config(tmp_path)
        cfg["tenants"]["acme"] = cfg["tenants"].pop(TK1)
        assert "64 lowercase hex" in _refused(cfg, tmp_path)

    def test_session_dir_inside_setup_store_refused(self, tmp_path):
        cfg = admin_config(tmp_path)
        inner = tmp_path / "store-one" / "sessions"
        inner.mkdir()
        cfg["session_store"]["dir"] = str(inner)
        assert "overlap" in _refused(cfg, tmp_path)

    def test_symmetric_jwt_algorithm_refused(self, tmp_path):
        cfg = admin_config(tmp_path)
        cfg["auth"]["verifier"]["algorithms"] = ["HS256"]
        _refused(cfg, tmp_path)

    def test_service_principal_cannot_be_admin(self, tmp_path):
        cfg = admin_config(tmp_path)
        cfg["roles"]["setup_admins"].append(cfg["service_principals"][0])
        _refused(cfg, tmp_path)

    def test_duplicate_yaml_key_refused(self, tmp_path):
        path = tmp_path / "dup.yaml"
        path.write_text("mode: shared\nmode: local\n", encoding="utf-8")
        with pytest.raises(deploy.DeploymentError, match="duplicate"):
            deploy.startup({deploy.ADMIN_CONFIG_ENV_VAR: str(path)})

    def test_problems_are_bounded(self, tmp_path):
        cfg = admin_config(tmp_path)
        for i in range(80):
            cfg[f"junk{i}"] = i
        msg = _refused(cfg, tmp_path)
        assert "more" in msg and len(msg) < 20_000

    def test_example_config_is_synthetic_and_parses(self):
        from pathlib import Path

        text = (Path(__file__).parents[1] / "docs" / "deployment" / "admin_config.example.yaml").read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        assert data["mode"] == "shared"
        assert "example" in data["service_base_url"]
        for tenant in data["tenants"].values():
            assert is_reference(tenant["bullhorn_oauth"]["client_secret_ref"])


class TestSecrets:
    def test_env_and_file_refs(self, tmp_path):
        secret_file = tmp_path / "s.txt"
        secret_file.write_text("from-file\n", encoding="utf-8")
        src = CredentialSource({"X_SECRET": "from-env"})
        assert src.resolve("env:X_SECRET") == "from-env"
        assert src.resolve(f"file:{secret_file}") == "from-file"

    @pytest.mark.parametrize("ref", ["plain-literal", "env:", "env:1BAD", "file:relative/path", "vault:x", ""])
    def test_malformed_refs_refused(self, ref):
        assert not is_reference(ref)
        with pytest.raises(SecretRefError):
            SecretRef(ref)

    def test_missing_values_never_echo(self, tmp_path):
        src = CredentialSource({})
        with pytest.raises(SecretRefError) as info:
            src.resolve("env:NOT_SET_ANYWHERE")
        assert "NOT_SET_ANYWHERE" not in str(info.value)
        with pytest.raises(SecretRefError):
            src.resolve(f"file:{tmp_path / 'missing.txt'}")

    def test_ref_repr_has_no_value(self):
        assert "env:A_B" in repr(SecretRef("env:A_B"))


def test_platform_port_overrides_config_port():
    assert deploy.platform_port({}, 8000) == 8000
    assert deploy.platform_port({"PORT": " "}, 8000) == 8000
    assert deploy.platform_port({"PORT": "10000"}, 8000) == 10000
    for bad in ("0", "70000", "abc", "-1", "80.5"):
        with pytest.raises(deploy.DeploymentError):
            deploy.platform_port({"PORT": bad}, 8000)


def test_healthz_is_unauthenticated_and_returns_ok():
    from starlette.testclient import TestClient

    from bullhorn_mcp import server

    response = TestClient(server.mcp.streamable_http_app()).get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"
