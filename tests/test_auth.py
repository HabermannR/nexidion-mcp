import importlib
import asyncio
import sys


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self):
        self.calls = []

    async def post(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if path == "/api/auth/login":
            return FakeResponse({"access_token": "human-token"})
        if path == "/api/auth/actor-token":
            return FakeResponse({"access_token": "mcp-token"})
        raise AssertionError(f"Unexpected path: {path}")


def test_stdio_login_exchanges_human_token_for_mcp_actor(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["server.py"])
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    client = FakeClient()

    token = asyncio.run(server._login(client))

    assert token == "mcp-token"
    assert client.calls[0] == (
        "/api/auth/login",
        {"json": {"username": server.USERNAME, "password": server.PASSWORD}},
    )
    assert client.calls[1] == (
        "/api/auth/actor-token",
        {
            "headers": {"Authorization": "Bearer human-token"},
            "json": {"actor_type": "mcp"},
        },
    )
