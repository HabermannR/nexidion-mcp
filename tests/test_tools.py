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
