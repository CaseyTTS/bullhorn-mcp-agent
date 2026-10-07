"""TrustedOriginPolicy (Phase 5A, D-5A-5; AC-18 / SR-15; closes DEBT-1)."""

from __future__ import annotations

import httpx
import pytest
import respx

from bullhorn_mcp.auth import BullhornAuth
from bullhorn_mcp.auth.trusted_origins import (
    TrustedOriginPolicy,
    is_trusted_bullhorn_host,
    is_trusted_bullhorn_url,
)


class TestHosts:
    @pytest.mark.parametrize(
        "netloc",
        [
            "auth.bullhornstaffing.com",
            "auth-west.bullhornstaffing.com",
            "AUTH-EMEA.BullhornStaffing.com",
            "rest99.bullhornstaffing.com",
            "a.b.bullhornstaffing.com",
            "auth.bullhornstaffing.com:443",
            "bullhornstaffing.com",
        ],
    )
    def test_accepted_at_label_boundary(self, netloc):
        assert is_trusted_bullhorn_host(netloc)

    @pytest.mark.parametrize(
        "netloc",
        [
            "bullhornstaffing.com.attacker.example",
            "evilbullhornstaffing.com",
            "auth.evilbullhornstaffing.com",
            "bullhornstaffing.co",
            "attacker.example",
            "user@auth.bullhornstaffing.com",
            "auth.bullhornstaffing.com@attacker.example",
            "attacker.example#.bullhornstaffing.com",
            "attacker.example/.bullhornstaffing.com",
            "attacker.example?.bullhornstaffing.com",
            "attacker%2Ebullhornstaffing.com",
            "auth.bullhornstaffing.com.",
            ".bullhornstaffing.com",
            "auth..bullhornstaffing.com",
            "-bad.bullhornstaffing.com",
            "auth.bullhornstaffing.com:99999",
            "auth.bullhornstaffing.com:abc",
            "auth.bullhornstaffing.com:",
            "аuth.bullhornstaffing.com",
            "auth.bullhornstaffing.com\r\n",
            "auth.bullhornstaffing.com evil",
            "[::1]",
            "",
            None,
            123,
        ],
    )
    def test_rejected(self, netloc):
        assert not is_trusted_bullhorn_host(netloc)


class TestUrls:
    def test_https_only(self):
        assert is_trusted_bullhorn_url("https://auth.bullhornstaffing.com/oauth/token")
        assert not is_trusted_bullhorn_url("http://auth.bullhornstaffing.com/oauth/token")
        assert not is_trusted_bullhorn_url("javascript:alert(1)")

    @pytest.mark.parametrize(
        "url",
        [
            "https://user:pw@auth.bullhornstaffing.com/",
            "https://auth.bullhornstaffing.com@attacker.example/",
            "https://bullhornstaffing.com.attacker.example/",
            "https://auth.bullhornstaffing.com/#frag",
            "https://auth.bullhornstaffing.com\\@attacker.example/",
            "//auth.bullhornstaffing.com/",
        ],
    )
    def test_url_tricks_rejected(self, url):
        assert not is_trusted_bullhorn_url(url)

    def test_custom_suffixes(self):
        policy = TrustedOriginPolicy.from_suffixes(["example.test"])
        assert policy.is_trusted_host("a.example.test")
        assert not policy.is_trusted_host("example.test.evil")
        with pytest.raises(ValueError):
            TrustedOriginPolicy.from_suffixes(["com"])
        with pytest.raises(ValueError):
            TrustedOriginPolicy.from_suffixes([])


class TestLegacyRedirectGuard:
    """The password-grant guard now uses the policy (D-5A-5): the substring bypass is closed."""

    @respx.mock
    def test_lookalike_host_with_code_does_not_become_the_regional_url(self, sample_config):
        respx.get(f"{sample_config.auth_url}/oauth/authorize").mock(
            return_value=httpx.Response(302, headers={"location": "https://bullhornstaffing.com.attacker.example/cb?code=c1"})
        )
        auth = BullhornAuth(sample_config)
        assert auth._get_auth_code() == "c1"
        assert auth._regional_auth_url is None  # previously the attacker host would have been adopted

    @respx.mock
    def test_lookalike_host_without_code_is_not_followed(self, sample_config):
        respx.get(f"{sample_config.auth_url}/oauth/authorize").mock(
            return_value=httpx.Response(307, headers={"location": "https://evilbullhornstaffing.com/oauth/authorize"})
        )
        follow = respx.get("https://evilbullhornstaffing.com/oauth/authorize").mock(return_value=httpx.Response(200))
        auth = BullhornAuth(sample_config)
        with pytest.raises(Exception, match="Failed to get auth code"):
            auth._get_auth_code()
        assert not follow.called
