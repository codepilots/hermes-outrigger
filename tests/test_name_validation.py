"""F-11 regression: tool names and chat ids must reject a trailing newline.

_NAME_RE (protocol.py) and _CHAT_ID_RE (ws.py) were anchored with `$` and
applied via .match, which in Python accepts one trailing newline
('run_js\\n', 'home\\n'). The anchors are now `\\Z`: the whole string must
belong to the character class. These tests pin both halves of the
invariant — whitespace-suffixed/prefixed values rejected, valid names and
ids (including exact length boundaries) unchanged.
"""
import pytest

from outrigger import platform
from outrigger.protocol import ProtocolError, ToolDecl

from helpers import TOOLS, start


def parse_name(name):
    return ToolDecl.parse({"name": name, "policy": "auto"})


# ------------------------------------------------ tool names (ToolDecl.parse)

def test_tool_name_trailing_newline_rejected():
    with pytest.raises(ProtocolError) as e:
        parse_name("run_js\n")
    assert e.value.code == "bad_tool"


def test_tool_name_trailing_newline_at_max_length_rejected():
    with pytest.raises(ProtocolError) as e:
        parse_name("a" * 64 + "\n")
    assert e.value.code == "bad_tool"


@pytest.mark.parametrize("bad", [
    "\nrun_js", "run_js ", "run_js\r", "run_js\r\n", "run_js\n\n",
    "run_js\t", "\trun_js", "run_js\u00a0", "run_js\u2003",
    "run_js\u2028", "run_js\u2029", "run_js\x0b", "run_js\x0c", "run_js\x85",
])
def test_tool_name_whitespace_rejected(bad):
    with pytest.raises(ProtocolError) as e:
        parse_name(bad)
    assert e.value.code == "bad_tool"


@pytest.mark.parametrize("name", ["run_js", "home", "a.b-c_d", "_x", "A", "a" * 64])
def test_valid_tool_names_still_accepted(name):
    assert parse_name(name).name == name


@pytest.mark.parametrize("bad", ["", "9lives", "-x", ".x", "a" * 65, "run js", "run/js"])
def test_tool_name_charset_and_length_bounds_unchanged(bad):
    with pytest.raises(ProtocolError) as e:
        parse_name(bad)
    assert e.value.code == "bad_tool"


# ------------------------------------------------ chat ids (live ws, chat.send)

async def chat_ws(aiohttp_client, name="f11"):
    c = await start(aiohttp_client)
    ws = await c.ws_connect("/v1/outrigger/ws", protocols=("hermes-outrigger.v1",))
    await ws.send_json({"type": "hello", "protocol": 2, "client": {"name": name},
                        "session_id": "S1", "tools": TOOLS, "capabilities": ["chat"]})
    welcome = await ws.receive_json()
    assert welcome["type"] == "welcome", welcome
    return ws


async def chat_code(ws, chat_id):
    """Send chat.send and return the error frame's code (validation fires first)."""
    await ws.send_json({"type": "chat.send", "chat_id": chat_id, "text": "hi"})
    return (await ws.receive_json(timeout=2))["code"]


async def test_chat_id_trailing_newline_rejected(aiohttp_client):
    ws = await chat_ws(aiohttp_client)
    assert await chat_code(ws, "home\n") == "bad_chat"


async def test_chat_id_trailing_newline_at_max_length_rejected(aiohttp_client):
    ws = await chat_ws(aiohttp_client)
    assert await chat_code(ws, "c" * 128 + "\n") == "bad_chat"


@pytest.mark.parametrize("bad", [
    "\nhome", "home ", "home\r", "home\r\n", "home\n\n", "home\t", "\thome",
    "home\u00a0", "home\u2003", "home\u2028", "home\u2029", "home\x0b", "home\x85",
])
async def test_chat_id_whitespace_rejected(aiohttp_client, bad):
    ws = await chat_ws(aiohttp_client)
    assert await chat_code(ws, bad) == "bad_chat"


@pytest.mark.parametrize("cid", ["home", "a.b-c_d", "conv-1", "c" * 128])
async def test_valid_chat_ids_still_accepted(aiohttp_client, monkeypatch, cid):
    # Deterministic next gate: with no adapter, an id that PASSES chat_id
    # validation reaches platform_unavailable — never bad_chat.
    monkeypatch.setattr(platform, "ADAPTER", None)
    ws = await chat_ws(aiohttp_client)
    assert await chat_code(ws, cid) == "platform_unavailable"


@pytest.mark.parametrize("bad", ["", "c" * 129, "home space", "home/slash"])
async def test_chat_id_charset_and_length_bounds_unchanged(aiohttp_client, bad):
    ws = await chat_ws(aiohttp_client)
    assert await chat_code(ws, bad) == "bad_chat"


# ------------------------------------------------ tool names over the wire (hello)

async def test_hello_tool_name_trailing_newline_rejected(aiohttp_client):
    c = await start(aiohttp_client)
    ws = await c.ws_connect("/v1/outrigger/ws", protocols=("hermes-outrigger.v1",))
    await ws.send_json({"type": "hello", "protocol": 1, "client": {"name": "f11"},
                        "session_id": "S1", "tools": [{"name": "run_js\n", "policy": "auto"}]})
    err = await ws.receive_json(timeout=2)
    assert err["type"] == "error" and err["code"] == "bad_tool"


async def test_hello_valid_tool_name_still_accepted(aiohttp_client):
    c = await start(aiohttp_client)
    ws = await c.ws_connect("/v1/outrigger/ws", protocols=("hermes-outrigger.v1",))
    await ws.send_json({"type": "hello", "protocol": 1, "client": {"name": "f11"},
                        "session_id": "S1", "tools": [{"name": "a.b-c_d", "policy": "auto"}]})
    assert (await ws.receive_json(timeout=2))["type"] == "welcome"
