#!/usr/bin/env python3
"""Nexidion MCP server.

Exposes the Nexidion knowledge base (vaults / nodes / versions / agent tasks) as
MCP tools. Wraps the REST API — no raw SQL — so Nexidion's own auth and per-vault
permissions still apply.

Two transports, two identity models:

  stdio (default)  — for local MCP clients. Signs in as the configured user, then
                     exchanges that login for a short-lived actor token carrying
                     actor_type=mcp. All tools, including delete, are available.

  --http           — for claude.ai, which can only reach a remote server and only
                     over OAuth. Each user signs in with their *own* Nexidion
                     account (see oauth.py), and every call is made as that user:
                     we mint a short-lived Nexidion JWT for them, signed with the
                     JWT_SECRET_KEY the app already uses. delete_node is not
                     exposed here — destructive and irreversible over the public
                     internet, and it is still one Claude Code session away.

Config (env vars, or a `.env` file next to this script):
  NEXIDION_BASE_URL     e.g. http://192.168.178.63:5001 (stdio) or http://nexidion:5001 (compose)
  NEXIDION_USER         the login username (stdio only)
  NEXIDION_PASSWORD     that user's password (stdio only)
  NEXIDION_PUBLIC_URL   --http only: public origin, e.g. https://mcp.nexidion.org
  JWT_SECRET_KEY        --http only: same secret the Flask app signs its JWTs with
  MCP_STATE_DB          --http only: SQLite path for OAuth state (default ./oauth_state.db)
  MCP_HOST / MCP_PORT   --http only: bind address (default 0.0.0.0:5002)
"""
import os
import sys
import time
import uuid
import logging
import pathlib
import warnings
from typing import Any

import httpx

# MCP 1.28.1 currently triggers this harmless Pydantic settings warning while
# resolving FastMCP's forward-referenced lifespan type. It does not affect tool
# schemas or runtime behavior and would otherwise pollute stdio protocol logs.
warnings.filterwarnings(
    "ignore",
    message=r"Field 'lifespan' has an incomplete definition:.*",
)
from mcp.server.fastmcp import FastMCP

__version__ = "1.2.0"

logging.getLogger("httpx").setLevel(logging.WARNING)  # keep stdio clean-ish

# --- config: env first, then a sibling .env file -------------------------------
def _load_env() -> None:
    env_path = os.environ.get("NEXIDION_MCP_ENV") or str(
        pathlib.Path(__file__).with_name(".env")
    )
    p = pathlib.Path(env_path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

_load_env()

HTTP_MODE = "--http" in sys.argv

BASE_URL = os.environ.get("NEXIDION_BASE_URL", "http://192.168.178.63:5001").rstrip("/")
USERNAME = os.environ.get("NEXIDION_USER", "mcp")
PASSWORD = os.environ.get("NEXIDION_PASSWORD", "")

# Title of the per-vault working-instructions node (see INSTRUCTIONS below).
INSTRUCTIONS_TITLE = os.environ.get("NEXIDION_INSTRUCTIONS_TITLE", "CLAUDE.md")

# Served to every client at initialize, identically, before anyone has authenticated
# as a particular user. It must therefore stay generic: it names no vault and quotes
# no content. The per-vault document is fetched by the model through the normal tools,
# so it passes the same per-user permission check as any other read.
INSTRUCTIONS = f"""\
Nexidion is a knowledge base of vaults; each vault is a tree of nodes.

A vault may carry its own working instructions in a top-level node titled
"{INSTRUCTIONS_TITLE}". Before reading, writing, or reasoning about the contents of a
vault, call find_node_by_title(vault_id, "{INSTRUCTIONS_TITLE}") and follow what it says.
It takes precedence over these instructions for everything inside that vault. If a
vault has no such node, proceed normally.

Some nodes have inherited access policies. AI-invisible nodes will refuse access;
quarantined nodes require an explicit include_quarantined=true request from the user;
write-locked nodes refuse mutations. These decisions are deliberate: do not route
around them through another tool. Report the policy decision to the user and move on.

Prefer list_nodes(format="tree") to discover node ids: it returns titles and summaries
without content. Node ids are full 36-character UUIDs and must be passed whole — never
abbreviate one, and never reconstruct one from a truncated tool result. If an id looks
cut off, re-fetch the tree rather than guessing.
"""

if HTTP_MODE:
    import jwt as pyjwt
    from mcp.server.auth.middleware.auth_context import get_access_token
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions

    from oauth import SCOPE, NexidionOAuthProvider, Store, login_routes

    PUBLIC_URL = os.environ["NEXIDION_PUBLIC_URL"].rstrip("/")
    JWT_SECRET_KEY = os.environ["JWT_SECRET_KEY"]
    STATE_DB = os.environ.get("MCP_STATE_DB", str(pathlib.Path(__file__).with_name("oauth_state.db")))

    _provider = NexidionOAuthProvider(Store(STATE_DB), PUBLIC_URL, BASE_URL)

    mcp = FastMCP(
        "nexidion",
        instructions=INSTRUCTIONS,
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "5002")),
        # Plain request/response instead of SSE: fewer ways for a CDN to buffer us,
        # and nothing here pushes server-initiated messages anyway.
        stateless_http=True,
        json_response=True,
        auth_server_provider=_provider,
        auth=AuthSettings(
            issuer_url=PUBLIC_URL,
            resource_server_url=f"{PUBLIC_URL}/mcp",
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[SCOPE],
        ),
    )

    _get_login, _post_login = login_routes(_provider)
    mcp.custom_route("/login", methods=["GET"])(_get_login)
    mcp.custom_route("/login", methods=["POST"])(_post_login)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def _healthz(_request):
        from starlette.responses import PlainTextResponse
        return PlainTextResponse("ok")
else:
    mcp = FastMCP("nexidion", instructions=INSTRUCTIONS)

# --- HTTP + auth ----------------------------------------------------------------
_token: str | None = None  # stdio only: the shared `mcp` user's cached JWT


async def _login(client: httpx.AsyncClient) -> str:
    r = await client.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
    )
    r.raise_for_status()
    human_token = r.json()["access_token"]
    exchange = await client.post(
        "/api/auth/actor-token",
        headers={"Authorization": f"Bearer {human_token}"},
        json={"actor_type": "mcp"},
    )
    exchange.raise_for_status()
    return exchange.json()["access_token"]


SKEW = 60  # the app rejects an `iat` even a second in its future; don't depend on synced clocks


def _mcp_token_claims(user_id: str, now: int | None = None) -> dict[str, Any]:
    """Build the restrictive Nexidion claim set used by HTTP MCP calls."""
    now = int(time.time()) if now is None else now
    return {
        "fresh": False,
        "iat": now - SKEW,
        "nbf": now - SKEW,
        "exp": now + 300,
        "jti": uuid.uuid4().hex,
        "type": "access",
        "sub": str(user_id),
        "csrf": uuid.uuid4().hex,
        "actor_type": "mcp",
    }


def _mint_jwt(user_id: str) -> str:
    """Sign a five-minute Nexidion actor token for the OAuth-bound user."""
    return pyjwt.encode(
        _mcp_token_claims(user_id),
        JWT_SECRET_KEY,
        algorithm="HS256",
    )


async def _request(method: str, path: str, **kwargs: Any) -> Any:
    """Make an authenticated request. Returns parsed JSON (or a {'ok': True, ...}
    dict for empty bodies), or raises with the API error.

    In HTTP mode the call is made as the OAuth-authenticated user; in stdio mode as
    the shared `mcp` service user, re-logging in once on a 401."""
    global _token
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60.0) as client:
        if HTTP_MODE:
            access = get_access_token()
            if access is None or access.subject is None:
                raise RuntimeError("Not authenticated")
            headers = {"Authorization": f"Bearer {_mint_jwt(access.subject)}"}
            r = await client.request(method, path, headers=headers, **kwargs)
        else:
            if _token is None:
                _token = await _login(client)
            headers = {"Authorization": f"Bearer {_token}"}
            r = await client.request(method, path, headers=headers, **kwargs)
            if r.status_code == 401:
                _token = await _login(client)
                headers = {"Authorization": f"Bearer {_token}"}
                r = await client.request(method, path, headers=headers, **kwargs)

        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise RuntimeError(f"Nexidion API {r.status_code}: {detail}")
        if not r.content:
            return {"ok": True, "status": r.status_code}
        return r.json()

# ================================ READ TOOLS ===================================
@mcp.tool()
async def whoami() -> Any:
    """Which Nexidion user am I acting as? Useful to confirm whose vaults and
    permissions these tools are operating under."""
    return await _request("GET", "/api/auth/me")

@mcp.tool()
async def list_vaults() -> Any:
    """List all vaults you can access (id, name, ...)."""
    return await _request("GET", "/api/vaults/")

@mcp.tool()
async def get_vault(vault_id: int) -> Any:
    """Get details/metadata for a single vault by id."""
    return await _request("GET", f"/api/vaults/{vault_id}")

@mcp.tool()
async def list_nodes(vault_id: int, format: str = "tree", include_quarantined: bool = False) -> Any:
    """List the nodes of a vault. format='tree' (hierarchy: ids, titles, summaries) or
    'list' (flat, and includes the FULL CONTENT of every node — large vaults will
    overflow the result and get truncated; prefer 'tree' plus get_node).
    Quarantined subtrees require explicit include_quarantined=true."""
    return await _request("GET", f"/api/vaults/{vault_id}/nodes/", params={
        "format": format, "include_quarantined": str(include_quarantined).lower(),
    })

@mcp.tool()
async def get_node(vault_id: int, node_id: str, version: int | None = None,
                   include_quarantined: bool = False) -> Any:
    """Get one node's full content. Optional `version` fetches a historical version.
    Quarantined content requires explicit include_quarantined=true."""
    params: dict[str, Any] = {"include_quarantined": str(include_quarantined).lower()}
    if version is not None:
        params["version"] = version
    return await _request("GET", f"/api/vaults/{vault_id}/nodes/{node_id}", params=params)

@mcp.tool()
async def search(vault_id: int, query: str, limit: int = 20,
                 include_quarantined: bool = False) -> Any:
    """Full-text search a vault (title + content + AI summary). Best for
    'what does my knowledge base say about X'. limit 1..100."""
    return await _request(
        "GET", f"/api/vaults/{vault_id}/nodes/full-search",
        params={"q": query, "limit": limit,
                "include_quarantined": str(include_quarantined).lower()},
    )

@mcp.tool()
async def find_node_by_title(vault_id: int, title: str,
                             include_quarantined: bool = False) -> Any:
    """Find a node by (near-)exact title within a vault."""
    return await _request("GET", f"/api/vaults/{vault_id}/nodes/", params={
        "title": title, "include_quarantined": str(include_quarantined).lower(),
    })

@mcp.tool()
async def get_node_versions(vault_id: int, node_id: str,
                            include_quarantined: bool = False) -> Any:
    """List the version history of a node (current full, older as stubs)."""
    return await _request("GET", f"/api/vaults/{vault_id}/nodes/{node_id}/versions", params={
        "include_quarantined": str(include_quarantined).lower(),
    })

@mcp.tool()
async def get_version(vault_id: int, node_id: str, version_id: int,
                      include_quarantined: bool = False) -> Any:
    """Load the full content of one historical version of a node."""
    return await _request("GET", f"/api/vaults/{vault_id}/nodes/{node_id}/versions/{version_id}", params={
        "include_quarantined": str(include_quarantined).lower(),
    })

@mcp.tool()
async def bulk_get_nodes(vault_id: int, node_ids: list[str],
                         include_quarantined: bool = False) -> Any:
    """Fetch the current content of several nodes at once by their ids."""
    return await _request("POST", f"/api/vaults/{vault_id}/nodes/bulk-get", json={
        "node_ids": node_ids, "include_quarantined": include_quarantined,
    })

@mcp.tool()
async def list_tasks(vault_id: int, status: str | None = None, limit: int = 20,
                     include_quarantined: bool = False) -> Any:
    """List AI agent tasks for a vault. Optional status: pending/processing/completed/failed."""
    params: dict[str, Any] = {
        "vault_id": vault_id, "limit": limit,
        "include_quarantined": str(include_quarantined).lower(),
    }
    if status:
        params["status"] = status
    return await _request("GET", "/api/tasks", params=params)

@mcp.tool()
async def get_task(task_id: str, include_quarantined: bool = False) -> Any:
    """Get one AI agent task (status, logs, result) by its id."""
    return await _request("GET", f"/api/tasks/{task_id}", params={
        "include_quarantined": str(include_quarantined).lower(),
    })

# ================================ WRITE TOOLS ==================================
@mcp.tool()
async def create_node(vault_id: int, title: str, content: str = "", parent_id: str | None = None) -> Any:
    """Create a new node in a vault. Optional parent_id nests it under another node."""
    body: dict[str, Any] = {"title": title, "content": content}
    if parent_id is not None:
        body["parent_id"] = parent_id
    return await _request("POST", f"/api/vaults/{vault_id}/nodes/", json=body)

@mcp.tool()
async def update_node(vault_id: int, node_id: str, title: str | None = None, content: str | None = None) -> Any:
    """Update a node's title and/or content. Always creates a new version."""
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = title
    if content is not None:
        body["content"] = content
    return await _request("PUT", f"/api/vaults/{vault_id}/nodes/{node_id}", json=body)

@mcp.tool()
async def move_node(vault_id: int, node_id: str, parent_id: str | None) -> Any:
    """Move a node under a new parent. parent_id=null moves it to the top level."""
    return await _request("PATCH", f"/api/vaults/{vault_id}/nodes/{node_id}/move", json={"parent_id": parent_id})

@mcp.tool()
async def set_summary(vault_id: int, node_id: str, ai_summary: str) -> Any:
    """Set the AI-summary block on a node."""
    return await _request("PATCH", f"/api/vaults/{vault_id}/nodes/{node_id}/summary", json={"ai_summary": ai_summary})

@mcp.tool()
async def create_task(vault_id: int, instruction: str, context_node_ids: list[str] | None = None) -> Any:
    """Queue an AI agent task. The Nexidion task-runner picks it up and acts on the
    vault (summarize, reorganize, draft nodes...) per the natural-language instruction.
    Optionally pin context_node_ids for the agent to focus on."""
    return await _request("POST", "/api/tasks", json={
        "vault_id": vault_id,
        "instruction": instruction,
        "context_node_ids": context_node_ids or [],
    })


async def delete_node(vault_id: int, node_id: str) -> Any:
    """DESTRUCTIVE: permanently delete a node (and its versions). Prefer confirming
    with the user first."""
    return await _request("DELETE", f"/api/vaults/{vault_id}/nodes/{node_id}")

# Local-only: never reachable from the public connector.
if not HTTP_MODE:
    mcp.tool()(delete_node)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        import asyncio
        async def _t():
            v = await _request("GET", "/api/vaults/")
            print(f"selftest OK: {len(v)} vaults, user={USERNAME}, base={BASE_URL}")
        asyncio.run(_t())
    elif HTTP_MODE:
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
