# Nexidion MCP 1.3.0

MCP server that exposes the Nexidion knowledge base as tools. Wraps the REST API —
no raw SQL — so Nexidion's own auth and per-vault permissions still apply.

It runs in two modes, with **two different identity models**:

| | stdio (Claude Code) | `--http` (claude.ai) |
|---|---|---|
| Where | your machine, over the LAN | `nexidion-mcp` container on the Pi |
| Reached at | subprocess on stdin/stdout | `https://mcp.nexidion.org/mcp` |
| Who it acts as | the configured Nexidion user | **the user who logged in**, individually |
| Auth | login followed by an MCP actor-token exchange | OAuth 2.1 + PKCE (see below) |
| Tools | all 22 | 21 — **no `delete_node`** |

`delete_node` is deliberately not reachable from the public connector: it is
irreversible, and it is still one Claude Code session away.

> [!IMPORTANT]
> The AI application you use must itself support MCP and local stdio servers.
> Running a local LLM API server (for example an OpenAI-compatible model endpoint)
> is not enough: an LLM API serves model inference, but it does not launch this
> process, speak MCP, or present MCP tools to the model. Use an MCP-capable client
> as the application around the model, and configure that client as shown below.

## Files
- `server.py` — the tools, plus both transports.
- `oauth.py` — OAuth authorization server for the HTTP mode (state in SQLite).
- `Dockerfile`, `requirements.txt` — the image the Pi runs.
- `.env` — stdio config: `NEXIDION_BASE_URL`, `NEXIDION_USER`, `NEXIDION_PASSWORD`
  (chmod 600, git-ignored).

## Fresh stdio installation

These instructions start with a machine that has no checkout or Python environment.
They configure the local stdio transport, which is the usual choice for desktop MCP
clients. The Nexidion web application must already be running at a URL this machine
can reach.

### 1. Prerequisites

Install:

- Git.
- Python 3.11 or newer, including `venv` and `pip`.
- An MCP-capable AI client that supports **local stdio servers**. Claude Code is one
  example. A client that supports only remote HTTP MCP cannot use this local setup.

Check the installations:

```bash
git --version
python3 --version
```

On Windows PowerShell, use `py --version` for Python instead.

### 2. Clone and install on Linux

Choose any permanent directory; the paths used later must match it exactly.

```bash
git clone https://github.com/HabermannR/nexidion-mcp.git
cd nexidion-mcp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If `python3 -m venv` is unavailable on Debian or Ubuntu, install the distribution's
`python3-venv` package and repeat the command.

### 3. Clone and install on Windows

In PowerShell:

```powershell
git clone https://github.com/HabermannR/nexidion-mcp.git
Set-Location nexidion-mcp
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If PowerShell blocks `Activate.ps1`, either permit locally created scripts for your
user (`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`) or skip activation and
run `.\.venv\Scripts\python.exe` explicitly in every command. Activation is only a
shell convenience; the MCP client configuration below always uses the environment's
Python executable directly.

### 4. Create and authorize the Nexidion MCP user

The stdio server logs in to Nexidion with a dedicated account. In the Nexidion web
application:

1. Sign in as an administrator and open **Admin → Users**.
2. Create a normal, non-administrator user, for example username `mcp`, with a unique
   password of at least eight characters. Do **not** use or create the special
   `llm_assistant` account; that account belongs to Nexidion's task runner and cannot
   log in through the normal authentication endpoint.
3. Open **Admin → Vaults**, expand **Access** for every vault this connector should
   use, select the new user, and choose **Add**. Do not grant access to vaults the
   client should not see.

The account's Nexidion vault permissions remain authoritative. MCP-specific node
visibility and write policies are also enforced after login.

### 5. Create `.env` from scratch

Create a file named `.env` in the repository root, beside `server.py`:

```dotenv
NEXIDION_BASE_URL=https://your-nexidion.example.com
NEXIDION_USER=mcp
NEXIDION_PASSWORD=replace-with-the-dedicated-users-password
```

Use the origin of the Nexidion **web application/API**, with no trailing `/api`.
For example, a LAN install might use `http://192.168.1.20:5001`. Do not put the MCP
connector URL here. The parser treats everything after `=` literally, so do not add
quotes or inline comments. Protect the file because it contains a password:

```bash
chmod 600 .env
```

On Windows, ensure the file is really named `.env`, not `.env.txt`. It is already
ignored by Git. You may instead set `NEXIDION_MCP_ENV` to the absolute path of a
credential file stored elsewhere.

### 6. Verify before configuring a client

From the repository root, with the virtual environment active:

```bash
python server.py --selftest
```

A working clean install prints output similar to:

```text
selftest OK: 3 vaults, user=mcp, base=https://your-nexidion.example.com
```

The count may differ. Zero vaults normally means the account still needs vault
access. An authentication error means the username/password is wrong; a connection
error means `NEXIDION_BASE_URL`, DNS, TLS, a firewall, or the Nexidion service needs
attention. This test verifies Python, dependencies, `.env`, login, actor-token
exchange, and API reachability without involving an MCP client.

Do not use `python server.py` as a visual test: a healthy stdio server waits silently
for MCP protocol messages on standard input.

### 7. Register with Claude Code

Use absolute paths. On Linux:

```bash
claude mcp add --scope user nexidion -- \
  /absolute/path/to/nexidion-mcp/.venv/bin/python \
  /absolute/path/to/nexidion-mcp/server.py
```

On Windows PowerShell (quote paths that may contain spaces):

```powershell
claude mcp add --scope user nexidion -- `
  "C:\absolute\path\to\nexidion-mcp\.venv\Scripts\python.exe" `
  "C:\absolute\path\to\nexidion-mcp\server.py"
```

Then run `claude mcp list` and `claude mcp get nexidion`. Start a **new Claude Code
session** because MCP tools are loaded at session startup, run `/mcp`, and ask it to
call `whoami` and `list_vaults`. The reported user should be `mcp`, and only the
vaults granted above should appear.

### 8. Generic MCP client JSON

Clients commonly call the configuration property `mcpServers`, although the file
name and settings location vary by application. Consult your client's documentation
and confirm that it supports local stdio MCP servers. A Linux entry is:

```json
{
  "mcpServers": {
    "nexidion": {
      "command": "/absolute/path/to/nexidion-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/nexidion-mcp/server.py"]
    }
  }
}
```

The Windows equivalent requires doubled backslashes because this is JSON:

```json
{
  "mcpServers": {
    "nexidion": {
      "command": "C:\\absolute\\path\\to\\nexidion-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\absolute\\path\\to\\nexidion-mcp\\server.py"]
    }
  }
}
```

Restart the client completely after editing its configuration, then inspect its MCP
server/tool view and call `whoami` followed by `list_vaults`. Some clients use a
different schema or require an `env`/working-directory entry; the explicit Python and
script paths above avoid depending on activation or the working directory. The server
finds `.env` beside `server.py`.

The login token is immediately exchanged at `/api/auth/actor-token`; all subsequent
requests carry `actor_type=mcp`, so stdio cannot bypass AI visibility or write policy.

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

Every short-lived Nexidion JWT used for an HTTP or stdio tool call includes the
trusted `actor_type=mcp` claim. Nexidion therefore retains the user identity for vault
permissions and auditing while applying inherited node AI-access policies to the
request. Relevant read tools default to `include_quarantined=false`; explicit opt-in
never bypasses an AI-invisible policy.

## Tests

Install the pinned runtime and test dependencies in a clean environment, then run:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The committed tests cover stdio actor-token exchange, HTTP MCP claims, and policy
option forwarding. `local_smoke_test.py` exercises initialization and authentication
against an explicitly selected local Nexidion instance.

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

Only clients whose redirect URIs are on allowed hosts may register
(`MCP_ALLOWED_REDIRECT_HOSTS`, default `claude.ai,claude.com,chatgpt.com`; loopback is
always allowed; `*` disables the check). Add a host there before connecting another
web client.

Wiping `nexidion_mcp-state` forces every connector to re-authorize — that is the
kill switch if a token ever leaks:
```bash
docker compose rm -sf mcp && docker volume rm nexidion_mcp-state && docker compose up -d --no-deps mcp
```

## Tools
Reads: `whoami, list_vaults, get_vault, list_nodes, get_node, search (full-text),
find_node_by_title, get_node_versions, get_version, bulk_get_nodes, list_tasks, get_task`.

Agent-native retrieval (needs a Nexidion build that has the matching endpoints):
- `search` returns verbatim `matches` from each hit's current content (offsets,
  heading path, version) and flags `summary_only` hits; full content is omitted
  unless `include_content=true`.
- `list_node_assets` / `get_node_asset` list and return the images a node embeds,
  as real image content, under that node's access policy.
- `get_context_bundle` loads a node with its parent, children, outlinks and
  backlinks in one bounded call, each with relation, distance and link context.
- `search_context_bundle` does the same starting from a search query's top hits.
Writes: `create_node, update_node, move_node, set_summary, create_task` (queues the
Nexidion AI agent). `delete_node` (destructive) — **stdio only**.

## Claude Code permissions (optional)

Claude Code can apply separate tool-approval rules in its settings. Tool names use
the `mcp__nexidion__<tool>` form. If you customize those rules, consider allowing
the read and non-destructive write tools while keeping
`mcp__nexidion__delete_node` in `ask` because deletion is irreversible. Client-side
approval rules supplement, but do not replace, Nexidion's account and vault access
controls.

## Admin
- stdio base URL points at the Pi over the LAN (`http://192.168.178.63:5001`). For
  off-LAN use, switch `NEXIDION_BASE_URL` to `https://caddy.nexidion.org` in `.env`.
- The stdio account has only the vault access assigned in Nexidion. Revoke access
  through the Admin UI rather than relying on a hard-coded user ID.
- To rotate the stdio password, reset the configured account and update `.env`.
- Rotating `JWT_SECRET_KEY` logs everyone out of the app *and* invalidates the
  connector's minted JWTs — restart both containers together.
