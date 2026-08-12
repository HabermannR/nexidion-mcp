# nexidion-mcp

MCP server that exposes the Nexidion knowledge base as tools. Wraps the REST API —
no raw SQL — so Nexidion's own auth and per-vault permissions still apply.

It runs in two modes, with **two different identity models**:

| | stdio (Claude Code) | `--http` (claude.ai) |
|---|---|---|
| Where | your machine, over the LAN | `nexidion-mcp` container on the Pi |
| Reached at | subprocess on stdin/stdout | `https://mcp.nexidion.org/mcp` |
| Who it acts as | the shared **`mcp`** service user (id 11) | **the user who logged in**, individually |
| Auth | password in `.env` | OAuth 2.1 + PKCE (see below) |
| Tools | all 18 | 17 — **no `delete_node`** |

`delete_node` is deliberately not reachable from the public connector: it is
irreversible, and it is still one Claude Code session away.

## Files
- `server.py` — the tools, plus both transports.
- `oauth.py` — OAuth authorization server for the HTTP mode (state in SQLite).
- `Dockerfile`, `requirements.txt` — the image the Pi runs.
- `.env` — stdio config: `NEXIDION_BASE_URL`, `NEXIDION_USER`, `NEXIDION_PASSWORD`
  (chmod 600, git-ignored).

## stdio (Claude Code) — already registered, user scope
```bash
claude mcp add --scope user nexidion -- \
  /home/rhab/projects/nexidion-mcp/.venv/bin/python \
  /home/rhab/projects/nexidion-mcp/server.py
```
Verify: `claude mcp list` / `claude mcp get nexidion`. In a session: `/mcp`.
**MCP tools load at session start — use a NEW session.**

Self-test against the live API: `.venv/bin/python server.py --selftest`.

## HTTP (claude.ai custom connector)

Add it in claude.ai under **Settings → Connectors → Add custom connector**, URL:

```
https://mcp.nexidion.org/mcp
```

Claude then registers itself (RFC 7591 dynamic client registration), redirects you
to a login page served by this server, and you sign in with **your own Nexidion
username and password**. Every tool call afterwards runs as that user.

### How the auth actually works
claude.ai will only talk to a remote MCP server over OAuth, so this server *is* an
OAuth authorization server — the MCP SDK supplies the `/authorize`, `/token` and
`/register` handlers, `oauth.py` supplies the storage, and `/login` supplies the
one thing OAuth can't know: who the human is. That is answered by posting the
credentials to Nexidion's existing `/api/auth/login`.

The catch: Nexidion's own JWTs expire after 8 hours, and we never store your
password, so we cannot re-login for you later. Instead the container is given the
app's `JWT_SECRET_KEY` and **mints a short-lived (5 min) Nexidion JWT per request**
for the user bound to the access token. That is why the connector stays connected
indefinitely, and why this container is as security-critical as the app itself:
holding that key, it could forge a token for any user. It only ever does so after
that user has proved their password.

Tokens: access 1 h, refresh 90 days and rotated on every use. Only SHA-256 hashes
are stored. `whoami` reports which Nexidion user Claude is currently acting as.

### Config (set by compose on the Pi, not by `.env`)
| var | value |
|---|---|
| `NEXIDION_BASE_URL` | `http://nexidion:5001` (compose network) |
| `NEXIDION_PUBLIC_URL` | `https://mcp.nexidion.org` |
| `JWT_SECRET_KEY` | same secret the Flask app signs with |
| `MCP_STATE_DB` | `/state/oauth_state.db` (volume `nexidion_mcp-state`) |

### Deploy
Cross-build on WSL, push to Hub, pin by digest — same rule as the app, so there is
always a digest to roll back to:
```bash
docker buildx build --builder nxbuilder --platform linux/arm64 \
  -f Dockerfile -t rhabermann/nexidion-mcp:vN --push .
docker buildx imagetools inspect rhabermann/nexidion-mcp:vN --format '{{.Manifest.Digest}}'
# then on the Pi: put that digest in ~/nexidion/docker-compose.yml
ssh rhab@192.168.178.63 'cd ~/nexidion && docker compose pull mcp && docker compose up -d --no-deps mcp'
```

Wiping `nexidion_mcp-state` forces every connector to re-authorize — that is the
kill switch if a token ever leaks:
```bash
docker compose rm -sf mcp && docker volume rm nexidion_mcp-state && docker compose up -d --no-deps mcp
```

## Tools
Reads: `whoami, list_vaults, get_vault, list_nodes, get_node, search (full-text),
find_node_by_title, get_node_versions, get_version, bulk_get_nodes, list_tasks, get_task`.
Writes: `create_node, update_node, move_node, set_summary, create_task` (queues the
Nexidion AI agent). `delete_node` (destructive) — **stdio only**.

## Permissions (`~/.claude/settings.json`)
Reads + non-destructive writes + `create_task` are allowlisted (`mcp__nexidion__*`).
`delete_node` is in `ask` → always prompts.

## Admin
- stdio base URL points at the Pi over the LAN (`http://192.168.178.63:5001`). For
  off-LAN use, switch `NEXIDION_BASE_URL` to `https://caddy.nexidion.org` in `.env`.
- The `mcp` user has **editor on all 10 vaults**. To revoke a vault, remove its
  `VaultAccess` row for user 11 (via the app's Admin UI, or the service layer).
- To rotate the `mcp` password: reset it for user `mcp` and update `.env`.
- Rotating `JWT_SECRET_KEY` logs everyone out of the app *and* invalidates the
  connector's minted JWTs — restart both containers together.
