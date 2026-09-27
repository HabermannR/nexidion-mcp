# Changelog

## 1.3.0

- `search` requests verbatim snippets: each hit carries `matches` (exact text,
  character offsets, heading path, version), `match_sources` and `summary_only`.
  **Behaviour change:** full node content is no longer returned by default; pass
  `include_content=true` for the previous shape. New filters: `subtree_root_id`,
  `content_kind`, `authority`.
- Add `list_node_assets` and `get_node_asset`: images embedded in a node, returned
  as MCP image content (downscaled to `max_px`) through the node's access policy.
- Add `get_context_bundle`: a node's bounded graph neighbourhood with provenance.
- Add `search_context_bundle`: search seeds plus their neighbourhoods in one call.
- OAuth: dynamic client registration only accepts redirect URIs on allowed hosts
  (`MCP_ALLOWED_REDIRECT_HOSTS`, default `claude.ai,claude.com,chatgpt.com`,
  loopback always allowed, `*` disables). Previously anyone could register a
  client with their own redirect URI and phish a user's tokens via the login link.
- OAuth login page names the requesting client and the host that will receive
  access, and HTML-escapes everything it renders.
- Server instructions mention the new tools.
- Tests: tool contracts for the new tools, a schema regression test that every
  read tool exposes `include_quarantined`, and OAuth registration/login-page tests.
- Requires the matching Nexidion backend endpoints (`/nodes/<id>/assets`,
  `/nodes/<id>/context`, `/nodes/context-search`, `full-search?snippets=true`).

## 1.2.0

- Mark HTTP and stdio requests as AI-mediated MCP actors.
- Exchange stdio login tokens for short-lived Nexidion actor tokens.
- Respect AI invisibility, quarantine opt-in, and inherited write locks.
- Forward quarantine opt-in through node and task retrieval tools.
- Add automated authentication and tool-contract tests.
