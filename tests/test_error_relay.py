"""F-07: client tool-result error payloads must be bounded + sanitized like ok content.

The error field of a `result` message is attacker-controlled detail (up to MAX_MSG
= 4 MiB). outrigger_-prefixed strings and non-string JSON values used to reach the
model verbatim via dispatch._err. These tests pin the cap, coercion, control-char
stripping, ingress validation, and the preservation of short gateway codes and the
ok path.
"""
import asyncio
import json

import pytest

from outrigger import settings
from outrigger.registry import REGISTRY

pytestmark = pytest.mark.asyncio

from helpers import TOOLS, call, connect, serve_one, start  # noqa: E402


def _all_strings(obj):
    """Every string anywhere in a parsed JSON value (dicts/lists included)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _all_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _all_strings(v)


# ---- (a) oversized outrigger_-prefixed error string is capped


async def test_outrigger_prefixed_error_string_is_capped(aiohttp_client):
    settings.SETTINGS["max_result_chars"] = 500
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    payload = "outrigger_x" + "A" * (2 * 1024 * 1024)  # 2 MiB, under MAX_MSG
    asyncio.create_task(serve_one(ws, lambda m: {"ok": False, "error": payload}))
    out = await call({"tool": "big", "args": {}})
    assert out["ok"] is False
    assert len(out["error"]) <= 500
    assert out["error"].startswith("outrigger_x")
    assert len(json.dumps(out)) <= 1000


async def test_error_cap_respects_tool_decl(aiohttp_client):
    # Same bound as the ok path: min(decl.max_result_chars, settings.max_result_chars).
    tiny = {"name": "tiny", "description": "Small cap", "policy": "auto", "max_result_chars": 20}
    c = await start(aiohttp_client)
    ws, _ = await connect(c, tools=TOOLS + [tiny])
    asyncio.create_task(serve_one(ws, lambda m: {"ok": False, "error": "outrigger_" + "x" * 5000}))
    out = await call({"tool": "tiny", "args": {}})
    assert out["ok"] is False
    assert len(out["error"]) <= 20


async def test_error_at_cap_boundary_unchanged(aiohttp_client):
    settings.SETTINGS["max_result_chars"] = 100
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    payload = "outrigger_" + "x" * 90  # exactly 100 chars
    asyncio.create_task(serve_one(ws, lambda m: {"ok": False, "error": payload}))
    out = await call({"tool": "big", "args": {}})
    assert out["ok"] is False and out["error"] == payload


# ---- (b) non-string errors are coerced to bounded strings


async def test_dict_error_is_coerced_to_bounded_string(aiohttp_client):
    settings.SETTINGS["max_result_chars"] = 300
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    payload = {"detail": "B" * (1024 * 1024), "nested": {"k": ["C" * 1000] * 50}}
    asyncio.create_task(serve_one(ws, lambda m: {"ok": False, "error": payload}))
    out = await call({"tool": "big", "args": {}})
    assert out["ok"] is False
    assert isinstance(out["error"], str)
    assert len(out["error"]) <= 300
    assert isinstance(out["message"], str) and len(out["message"]) <= 2000
    assert len(json.dumps(out)) <= 2500


async def test_list_error_is_coerced_to_bounded_string(aiohttp_client):
    settings.SETTINGS["max_result_chars"] = 300
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    payload = ["D" * 1000] * 2000  # ~2 MB list
    asyncio.create_task(serve_one(ws, lambda m: {"ok": False, "error": payload}))
    out = await call({"tool": "big", "args": {}})
    assert out["ok"] is False
    assert isinstance(out["error"], str)
    assert len(json.dumps(out)) <= 2500


# ---- (c) control characters are stripped on the error path


async def test_control_chars_stripped_from_error_string(aiohttp_client):
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    payload = "outrigger_x\x00\x07\x1b[31mRED\x7f\x08tail"
    asyncio.create_task(serve_one(ws, lambda m: {"ok": False, "error": payload}))
    out = await call({"tool": "big", "args": {}})
    assert out["ok"] is False
    for ch in "\x00\x07\x1b\x7f\x08":
        assert ch not in out["error"]
    assert "RED" in out["error"] and "tail" in out["error"]


async def test_control_chars_stripped_from_non_string_error(aiohttp_client):
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    payload = {"msg": "bad\x00\x1btext", "list": ["a\x07b"]}
    asyncio.create_task(serve_one(ws, lambda m: {"ok": False, "error": payload}))
    out = await call({"tool": "big", "args": {}})
    assert out["ok"] is False
    assert all("\x00" not in s and "\x1b" not in s and "\x07" not in s
               for s in _all_strings(out))
    assert isinstance(out["error"], str)


# ---- (d) legitimate short gateway codes keep their meaning (preservation)


@pytest.mark.parametrize("code", ["outrigger_cancelled", "outrigger_timeout",
                                  "outrigger_replaced", "outrigger_shutdown",
                                  "outrigger_disconnected"])
async def test_short_gateway_codes_intact(aiohttp_client, tmp_path, code):
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    asyncio.create_task(serve_one(ws, lambda m: {"ok": False, "error": code}))
    out = await call({"tool": "big", "args": {}})
    assert out["ok"] is False and out["error"] == code and out["message"] == ""
    audit = (tmp_path / "plugin-data/outrigger/audit.jsonl").read_text()
    expected = "cancelled" if code == "outrigger_cancelled" else "error"
    assert f'"outcome": "{expected}"' in audit


async def test_unicode_error_preserved(aiohttp_client):
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    payload = "outrigger_café 🚀 错误"
    asyncio.create_task(serve_one(ws, lambda m: {"ok": False, "error": payload}))
    out = await call({"tool": "big", "args": {}})
    assert out["ok"] is False and out["error"] == payload


# ---- (e) the ok path is unchanged (preservation)


async def test_ok_path_unchanged(aiohttp_client):
    settings.SETTINGS["max_result_chars"] = 10
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    asyncio.create_task(serve_one(ws, lambda m: {"ok": True, "content": "a\x00b" + "z" * 50}))
    out = await call({"tool": "big", "args": {}})
    assert out["ok"] is True and out["untrusted"] is True
    assert out["truncated"] and len(out["content"]) == 10 and "\x00" not in out["content"]


async def test_ok_true_ignores_error_field(aiohttp_client):
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    asyncio.create_task(serve_one(ws, lambda m: {
        "ok": True, "content": "fine", "error": "outrigger_" + "x" * 100000}))
    out = await call({"tool": "big", "args": {}})
    assert out["ok"] is True and out["content"] == "fine"


# ---- ws ingress: error is type/length-bounded before REGISTRY.resolve


async def test_ws_ingress_bounds_error_string(aiohttp_client, monkeypatch):
    settings.SETTINGS["max_result_chars"] = 100
    captured = {}
    real_resolve = REGISTRY.resolve

    def spy(client, call_id, msg):
        captured.update(msg)
        return real_resolve(client, call_id, msg)

    monkeypatch.setattr(REGISTRY, "resolve", spy)
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    asyncio.create_task(serve_one(ws, lambda m: {"ok": False, "error": "outrigger_" + "E" * 100000}))
    out = await call({"tool": "big", "args": {}})
    assert isinstance(captured["error"], str)
    assert len(captured["error"]) <= 100
    assert out["ok"] is False and len(out["error"]) <= 100


async def test_ws_ingress_coerces_and_bounds_non_string_error(aiohttp_client, monkeypatch):
    settings.SETTINGS["max_result_chars"] = 100
    captured = {}
    real_resolve = REGISTRY.resolve

    def spy(client, call_id, msg):
        captured.update(msg)
        return real_resolve(client, call_id, msg)

    monkeypatch.setattr(REGISTRY, "resolve", spy)
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    asyncio.create_task(serve_one(ws, lambda m: {"ok": False, "error": {"a": "x" * 50000}}))
    out = await call({"tool": "big", "args": {}})
    assert isinstance(captured["error"], str)
    assert len(captured["error"]) <= 100
    assert out["ok"] is False and isinstance(out["error"], str)
    assert len(json.dumps(out)) <= 2500
