#!/usr/bin/env python3
"""OAuth + MCP smoke test for the local Docker connector.

Required environment variables:
  NEXIDION_TEST_USERNAME
  NEXIDION_TEST_PASSWORD
Optional:
  NEXIDION_MCP_URL (default http://localhost:5002/mcp)
"""
from __future__ import annotations

import asyncio
import os
from urllib.parse import parse_qs, urlparse

import httpx
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken


class MemoryTokenStorage(TokenStorage):
    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


async def main() -> None:
    username = os.environ.get("NEXIDION_TEST_USERNAME")
    password = os.environ.get("NEXIDION_TEST_PASSWORD")
    if not username or not password:
        raise SystemExit("Set NEXIDION_TEST_USERNAME and NEXIDION_TEST_PASSWORD.")

    mcp_url = os.environ.get("NEXIDION_MCP_URL", "http://localhost:5002/mcp")
    callback: dict[str, str | None] = {}

    async def redirect_handler(authorization_url: str) -> None:
        async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
            response = await client.get(authorization_url)
            if response.status_code not in (302, 303, 307, 308):
                response.raise_for_status()
            login_url = response.headers["location"]
            txn = parse_qs(urlparse(login_url).query)["txn"][0]
            login = await client.post(
                login_url,
                data={"txn": txn, "username": username, "password": password},
            )
            if login.status_code not in (302, 303, 307, 308):
                login.raise_for_status()
            params = parse_qs(urlparse(login.headers["location"]).query)
            callback["code"] = params["code"][0]
            callback["state"] = params.get("state", [None])[0]

    async def callback_handler() -> tuple[str, str | None]:
        return str(callback["code"]), callback.get("state")

    oauth = OAuthClientProvider(
        mcp_url,
        OAuthClientMetadata(
            client_name="Nexidion local smoke test",
            redirect_uris=["http://127.0.0.1:8765/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="nexidion",
        ),
        MemoryTokenStorage(),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

    async with httpx.AsyncClient(auth=oauth, timeout=30.0) as http:
        async with streamable_http_client(mcp_url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                catalog = await session.list_tools()
                whoami = await session.call_tool("whoami", {})
                names = [tool.name for tool in catalog.tools]
                print(f"MCP initialized: {len(names)} tools")
                print("Tools: " + ", ".join(names))
                print("whoami: " + " ".join(block.text for block in whoami.content if hasattr(block, "text")))


if __name__ == "__main__":
    asyncio.run(main())
