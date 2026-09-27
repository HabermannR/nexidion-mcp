import asyncio

import server


def _capture(monkeypatch):
    calls = []

    async def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"ok": True}

    monkeypatch.setattr(server, "_request", fake_request)
    return calls


def test_mcp_claims_preserve_subject_and_mark_actor():
    claims = server._mcp_token_claims("42", now=1_000)
    assert claims["sub"] == "42"
    assert claims["actor_type"] == "mcp"
    assert claims["iat"] == 940
    assert claims["nbf"] == 940
    assert claims["exp"] == 1_300
    assert claims["type"] == "access"


def test_quarantine_flag_is_forwarded_for_node_reads(monkeypatch):
    calls = _capture(monkeypatch)

    async def exercise():
        await server.list_nodes(7, include_quarantined=True)
        await server.get_node(7, "node-id", include_quarantined=True)
        await server.bulk_get_nodes(7, ["node-id"], include_quarantined=True)

    asyncio.run(exercise())

    assert calls[0][2]["params"]["include_quarantined"] == "true"
    assert calls[1][2]["params"]["include_quarantined"] == "true"
    assert calls[2][2]["json"]["include_quarantined"] is True


def test_quarantine_flag_is_forwarded_for_task_reads(monkeypatch):
    calls = _capture(monkeypatch)

    async def exercise():
        await server.list_tasks(7, include_quarantined=True)
        await server.get_task("task-id", include_quarantined=True)

    asyncio.run(exercise())

    assert calls[0][2]["params"]["include_quarantined"] == "true"
    assert calls[1][2]["params"]["include_quarantined"] == "true"


def test_search_requests_verbatim_snippets_without_full_content(monkeypatch):
    calls = _capture(monkeypatch)

    asyncio.run(server.search(7, "impeller", subtree_root_id="root-id"))

    params = calls[0][2]["params"]
    assert calls[0][1] == "/api/vaults/7/nodes/full-search"
    assert params["snippets"] == "true"
    assert params["include_content"] == "false"
    assert params["subtree_root_id"] == "root-id"
    assert "content_kind" not in params


def test_get_node_asset_returns_an_image_through_the_node(monkeypatch):
    calls = []

    async def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return b"\x89PNG\r\n\x1a\nfake", "image/png"

    monkeypatch.setattr(server, "_request", fake_request)
    image = asyncio.run(server.get_node_asset(7, "node-id", "asset-id", max_px=99999))

    method, path, kwargs = calls[0]
    assert path == "/api/vaults/7/nodes/node-id/assets/asset-id"
    assert kwargs["raw"] is True
    assert kwargs["params"]["representation"] == "preview"
    assert kwargs["params"]["max_px"] == 2048
    content = image.to_image_content()
    assert content.mimeType == "image/png"


def test_context_bundle_forwards_bounds(monkeypatch):
    calls = _capture(monkeypatch)

    asyncio.run(server.get_context_bundle(7, "node-id", depth=2, max_items=5,
                                          include_backlinks=False, include_quarantined=True))

    method, path, kwargs = calls[0]
    assert path == "/api/vaults/7/nodes/node-id/context"
    assert kwargs["params"]["depth"] == 2
    assert kwargs["params"]["max_items"] == 5
    assert kwargs["params"]["include_backlinks"] == "false"
    assert kwargs["params"]["include_quarantined"] == "true"


def test_every_read_tool_exposes_the_quarantine_override_in_its_schema():
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    readers = ["list_nodes", "get_node", "search", "find_node_by_title", "get_node_versions",
               "get_version", "bulk_get_nodes", "list_tasks", "get_task", "list_node_assets",
               "get_node_asset", "get_context_bundle", "search_context_bundle"]
    for name in readers:
        assert "include_quarantined" in tools[name].inputSchema["properties"], name


def test_search_context_bundle_forwards_query_and_bounds(monkeypatch):
    calls = _capture(monkeypatch)

    asyncio.run(server.search_context_bundle(7, "flood", seed_limit=3, depth=2))

    method, path, kwargs = calls[0]
    assert path == "/api/vaults/7/nodes/context-search"
    assert kwargs["params"]["q"] == "flood"
    assert kwargs["params"]["seed_limit"] == 3
    assert kwargs["params"]["depth"] == 2
    assert "subtree_root_id" not in kwargs["params"]
