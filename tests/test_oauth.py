import asyncio

import pytest
from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import OAuthClientInformationFull

import oauth


def _client(*uris, name="Claude"):
    return OAuthClientInformationFull(client_id="c1", client_name=name, redirect_uris=list(uris))


@pytest.fixture()
def provider(tmp_path):
    return oauth.NexidionOAuthProvider(oauth.Store(str(tmp_path / "state.db")),
                                       "https://mcp.example", "http://nexidion")


@pytest.mark.parametrize("uri, ok", [
    ("https://claude.ai/api/mcp/auth_callback", True),
    ("https://claude.com/api/mcp/auth_callback", True),
    ("https://chatgpt.com/connector_platform_oauth_redirect", True),
    ("http://localhost:33418/callback", True),
    ("http://127.0.0.1:9000/cb", True),
    ("https://evil.example/steal", False),
    ("https://claude.ai.evil.example/cb", False),
    ("http://claude.ai/api/mcp/auth_callback", False),  # plain http off loopback
])
def test_redirect_allowlist(monkeypatch, uri, ok):
    monkeypatch.delenv("MCP_ALLOWED_REDIRECT_HOSTS", raising=False)
    assert oauth.redirect_uri_allowed(uri, oauth.allowed_redirect_hosts()) is ok


def test_registration_rejects_foreign_redirect(provider, monkeypatch):
    monkeypatch.delenv("MCP_ALLOWED_REDIRECT_HOSTS", raising=False)
    with pytest.raises(RegistrationError):
        asyncio.run(provider.register_client(_client("https://evil.example/cb")))
    assert provider.store.get_client("c1") is None

    asyncio.run(provider.register_client(_client("https://claude.ai/api/mcp/auth_callback")))
    assert provider.store.get_client("c1") is not None


def test_allowlist_can_be_configured_or_disabled(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_REDIRECT_HOSTS", "example.org")
    assert oauth.redirect_uri_allowed("https://app.example.org/cb", oauth.allowed_redirect_hosts())
    assert not oauth.redirect_uri_allowed("https://claude.ai/cb", oauth.allowed_redirect_hosts())
    monkeypatch.setenv("MCP_ALLOWED_REDIRECT_HOSTS", "*")
    assert oauth.redirect_uri_allowed("https://anything.test/cb", oauth.allowed_redirect_hosts())


def test_login_page_escapes_and_names_the_recipient():
    page = oauth.render_login('x"><script>', error="<b>bad</b>",
                              client="<img src=x>", target="claude.ai").body.decode()
    assert "<script>" not in page and "<img" not in page and "<b>bad" not in page
    assert "claude.ai" in page
