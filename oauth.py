"""OAuth 2.1 authorization server for the Nexidion MCP server.

Claude.ai only talks to *remote* MCP servers, and only over OAuth. So in HTTP mode
the MCP server is also its own authorization server: the SDK supplies the
/authorize, /token and /register handlers, and this module supplies the storage
plus the one thing the SDK cannot know — who the human is. That is answered by the
/login page here, which checks an ordinary Nexidion username + password against
Nexidion's existing /api/auth/login.

The issued access token carries `subject` = the Nexidion user id, so each tool call
runs as the user who actually logged in, with that user's vault permissions. (The
stdio path, by contrast, shares the single `mcp` service account.)

Clients, codes and tokens live in SQLite so they survive a container restart.
Tokens are stored only as SHA-256 hashes.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from typing import Any

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

SCOPE = "nexidion"

TXN_TTL = 600  # 10 min to get through the login form
AUTH_CODE_TTL = 300  # 5 min, single use
ACCESS_TOKEN_TTL = 3600  # 1 h — Claude silently refreshes
REFRESH_TOKEN_TTL = 90 * 24 * 3600  # 90 days, rotated on every use


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    """SQLite-backed OAuth state. Small and low-traffic, so a plain connection
    behind a lock is plenty — no need for an async driver."""

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                client_id  TEXT PRIMARY KEY,
                data       TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS txns (
                txn        TEXT PRIMARY KEY,
                data       TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS codes (
                code_hash  TEXT PRIMARY KEY,
                data       TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tokens (
                token_hash TEXT PRIMARY KEY,
                kind       TEXT NOT NULL,
                client_id  TEXT NOT NULL,
                subject    TEXT NOT NULL,
                scopes     TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            """
        )
        self._db.commit()

    # --- generic helpers ---
    def _put(self, sql: str, args: tuple[Any, ...]) -> None:
        with self._lock:
            self._db.execute(sql, args)
            self._db.commit()

    def _get(self, sql: str, args: tuple[Any, ...]) -> tuple[Any, ...] | None:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    def _delete(self, sql: str, args: tuple[Any, ...]) -> None:
        self._put(sql, args)

    def sweep(self) -> None:
        """Drop anything expired. Cheap; called on each authorize."""
        now = time.time()
        with self._lock:
            for table in ("txns", "codes", "tokens"):
                self._db.execute(f"DELETE FROM {table} WHERE expires_at < ?", (now,))
            self._db.commit()

    # --- clients ---
    def put_client(self, client: OAuthClientInformationFull) -> None:
        self._put(
            "INSERT OR REPLACE INTO clients (client_id, data) VALUES (?, ?)",
            (client.client_id, client.model_dump_json()),
        )

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self._get("SELECT data FROM clients WHERE client_id = ?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    # --- pending authorize transactions (one per /authorize hit) ---
    def put_txn(self, txn: str, payload: dict[str, Any]) -> None:
        self._put(
            "INSERT INTO txns (txn, data, expires_at) VALUES (?, ?, ?)",
            (txn, json.dumps(payload), time.time() + TXN_TTL),
        )

    def take_txn(self, txn: str) -> dict[str, Any] | None:
        """Load and consume — a login attempt burns the transaction."""
        row = self._get("SELECT data, expires_at FROM txns WHERE txn = ?", (txn,))
        if row is None:
            return None
        self._delete("DELETE FROM txns WHERE txn = ?", (txn,))
        data, expires_at = row
        return None if expires_at < time.time() else json.loads(data)

    def peek_txn(self, txn: str) -> dict[str, Any] | None:
        """Look without consuming — used to render the form on GET."""
        row = self._get("SELECT data, expires_at FROM txns WHERE txn = ?", (txn,))
        if row is None or row[1] < time.time():
            return None
        return json.loads(row[0])

    # --- authorization codes ---
    def put_code(self, code: AuthorizationCode) -> None:
        self._put(
            "INSERT INTO codes (code_hash, data, expires_at) VALUES (?, ?, ?)",
            (_hash(code.code), code.model_dump_json(), code.expires_at),
        )

    def get_code(self, code: str) -> AuthorizationCode | None:
        row = self._get("SELECT data FROM codes WHERE code_hash = ?", (_hash(code),))
        if row is None:
            return None
        obj = AuthorizationCode.model_validate_json(row[0])
        return None if obj.expires_at < time.time() else obj

    def take_code(self, code: str) -> None:
        self._delete("DELETE FROM codes WHERE code_hash = ?", (_hash(code),))

    # --- tokens ---
    def put_token(self, token: str, kind: str, client_id: str, subject: str, scopes: list[str], expires_at: float) -> None:
        self._put(
            "INSERT INTO tokens (token_hash, kind, client_id, subject, scopes, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (_hash(token), kind, client_id, subject, " ".join(scopes), expires_at),
        )

    def get_token(self, token: str, kind: str) -> tuple[str, str, list[str], float] | None:
        row = self._get(
            "SELECT client_id, subject, scopes, expires_at FROM tokens WHERE token_hash = ? AND kind = ?",
            (_hash(token), kind),
        )
        if row is None or row[3] < time.time():
            return None
        return row[0], row[1], row[2].split(), row[3]

    def revoke(self, token: str) -> None:
        self._delete("DELETE FROM tokens WHERE token_hash = ?", (_hash(token),))

    def revoke_subject(self, subject: str) -> None:
        self._delete("DELETE FROM tokens WHERE subject = ?", (subject,))


class NexidionOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, store: Store, public_url: str, nexidion_base_url: str) -> None:
        self.store = store
        self.public_url = public_url.rstrip("/")
        self.nexidion_base_url = nexidion_base_url.rstrip("/")

    # --- client registration (Claude registers itself via RFC 7591 DCR) ---
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.put_client(client_info)

    # --- authorize: park the request, send the human to our login page ---
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self.store.sweep()
        txn = secrets.token_urlsafe(32)
        self.store.put_txn(txn, {"client_id": client.client_id, "params": params.model_dump(mode="json")})
        return f"{self.public_url}/login?txn={txn}"

    async def complete_login(self, txn: str, username: str, password: str) -> tuple[str | None, str | None]:
        """Check the credentials against Nexidion. Returns (redirect_back_to_claude, None)
        on success, or (None, message_for_the_user) on any failure — the transaction is
        re-parked in that case so the human can just retry on the same page."""
        parked = self.store.take_txn(txn)
        if parked is None:
            raise AuthorizeError("invalid_request", "Login session expired — start again from Claude.")

        async with httpx.AsyncClient(base_url=self.nexidion_base_url, timeout=30.0) as http:
            r = await http.post("/api/auth/login", json={"username": username, "password": password})

        if r.status_code != 200:
            self.store.put_txn(txn, parked)  # let them try again
            if r.status_code == 401:
                return None, "Wrong username or password."
            if r.status_code == 429:
                # Nexidion rate-limits logins (20/min). Say so instead of 500-ing.
                return None, "Too many login attempts. Wait a minute and try again."
            return None, f"Nexidion rejected the login (HTTP {r.status_code})."

        user_id = r.json()["user"]["id"]

        params = AuthorizationParams.model_validate(parked["params"])
        code = secrets.token_urlsafe(32)
        self.store.put_code(
            AuthorizationCode(
                code=code,
                scopes=params.scopes or [SCOPE],
                expires_at=time.time() + AUTH_CODE_TTL,
                client_id=parked["client_id"],
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=params.resource,
                subject=str(user_id),
            )
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), None

    # --- code -> tokens ---
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self.store.get_code(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # The SDK has already verified the PKCE code_verifier against code_challenge.
        self.store.take_code(authorization_code.code)  # single use
        if authorization_code.subject is None:  # pragma: no cover — defensive
            raise TokenError("invalid_grant", "Authorization code is not bound to a user")
        return self._issue(client.client_id, authorization_code.subject, authorization_code.scopes)

    # --- refresh ---
    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = self.store.get_token(refresh_token, "refresh")
        if row is None:
            return None
        client_id, subject, scopes, expires_at = row
        if client_id != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token, client_id=client_id, scopes=scopes, expires_at=int(expires_at), subject=subject
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # OAuth 2.1 wants refresh tokens rotated for public clients: burn the old one.
        self.store.revoke(refresh_token.token)
        if refresh_token.subject is None:  # pragma: no cover — defensive
            raise TokenError("invalid_grant", "Refresh token is not bound to a user")
        return self._issue(client.client_id, refresh_token.subject, scopes or refresh_token.scopes)

    def _issue(self, client_id: str, subject: str, scopes: list[str]) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        self.store.put_token(access, "access", client_id, subject, scopes, now + ACCESS_TOKEN_TTL)
        self.store.put_token(refresh, "refresh", client_id, subject, scopes, now + REFRESH_TOKEN_TTL)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    # --- verify (called on every tool call) ---
    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self.store.get_token(token, "access")
        if row is None:
            return None
        client_id, subject, scopes, expires_at = row
        return AccessToken(
            token=token, client_id=client_id, scopes=scopes, expires_at=int(expires_at), subject=subject
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.revoke(token.token)


# --- the login page --------------------------------------------------------------
_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect Claude to Nexidion</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.5 system-ui, sans-serif; display: grid; place-items: center;
         min-height: 100vh; margin: 0; background: Canvas; color: CanvasText; }}
  form {{ width: min(22rem, 90vw); display: grid; gap: .75rem; }}
  h1 {{ font-size: 1.15rem; margin: 0 0 .25rem; }}
  p {{ margin: 0; opacity: .75; font-size: .875rem; }}
  input {{ font: inherit; padding: .55rem .7rem; border: 1px solid GrayText;
           border-radius: .4rem; background: Field; color: FieldText; }}
  button {{ font: inherit; padding: .55rem; border: 0; border-radius: .4rem;
            background: #d97757; color: #fff; cursor: pointer; }}
  .err {{ color: #c0392b; font-size: .875rem; }}
</style>
<form method="post" action="/login">
  <h1>Connect Claude to Nexidion</h1>
  <p>Claude will act on your vaults as you, with your permissions.</p>
  {error}
  <input type="hidden" name="txn" value="{txn}">
  <input name="username" placeholder="Username" autocomplete="username" autofocus required>
  <input name="password" type="password" placeholder="Password" autocomplete="current-password" required>
  <button type="submit">Sign in and authorize</button>
</form>
"""

_EXPIRED = """<!doctype html><meta charset="utf-8">
<title>Link expired</title>
<body style="font:16px/1.5 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0">
<p>This login link has expired. Start the connection again from Claude.</p>
"""


def render_login(txn: str, error: str | None = None) -> HTMLResponse:
    if error:
        return HTMLResponse(_PAGE.format(txn=txn, error=f'<p class="err">{error}</p>'), status_code=401)
    return HTMLResponse(_PAGE.format(txn=txn, error=""))


def login_routes(provider: NexidionOAuthProvider):
    """The two routes FastMCP mounts for the human half of the OAuth dance."""

    async def get_login(request: Request) -> Response:
        txn = request.query_params.get("txn", "")
        if not txn or provider.store.peek_txn(txn) is None:
            return HTMLResponse(_EXPIRED, status_code=400)
        return render_login(txn)

    async def post_login(request: Request) -> Response:
        form = await request.form()
        txn = str(form.get("txn", ""))
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        if not txn or provider.store.peek_txn(txn) is None:
            return HTMLResponse(_EXPIRED, status_code=400)
        try:
            redirect, error = await provider.complete_login(txn, username, password)
        except AuthorizeError:
            return HTMLResponse(_EXPIRED, status_code=400)
        if redirect is None:
            return render_login(txn, error or "Login failed.")
        return RedirectResponse(redirect, status_code=302)

    return get_login, post_login
