"""The LLM reviewer for calls that need approval."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import outrigger
from outrigger import reviewer, settings

from helpers import call, connect, serve_one, start

TOOLS = [
    {"name": "click", "policy": "confirm", "description": "Click by selector",
     "parameters": {"type": "object", "properties": {"selector": {"type": "string"}}}},
    {"name": "get_page", "policy": "auto", "description": "Read page"},
]


class FakeLLM:
    def __init__(self, verdict=None, reason="ok", exc=None, parsed=None):
        self.verdict, self.reason, self.exc, self.parsed, self.calls = verdict, reason, exc, parsed, []

    def complete_structured(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        parsed = self.parsed if self.parsed is not None else {"verdict": self.verdict, "reason": self.reason}
        return SimpleNamespace(parsed=parsed, text=json.dumps(parsed))


async def setup(aiohttp_client, llm):
    reviewer._llm = llm
    c = await start(aiohttp_client)
    ws, _ = await connect(c, tools=TOOLS)
    outrigger._pre_llm_call(session_id="S1", turn_id="T1", user_message="click the Go button please")
    return ws


async def test_approve_travels_with_call(aiohttp_client):
    llm = FakeLLM("approve", "User asked to click Go.")
    ws = await setup(aiohttp_client, llm)
    server = asyncio.create_task(serve_one(ws, lambda m: {"ok": True, "content": "clicked"}))
    out = await call({"tool": "click", "args": {"selector": "#go"}})
    msg = await server
    assert out["ok"] and msg["review"] == {"verdict": "approve", "reason": "User asked to click Go."}
    kw = llm.calls[0]
    assert kw["task"] == reviewer.TASK and kw["json_schema"] == reviewer.SCHEMA
    body = kw["input"][0]["text"]
    assert body.startswith("<untrusted>") and "click the Go button please" in body and "#go" in body
    assert "click the Go button" not in kw["instructions"]  # untrusted data stays out of instructions


async def test_deny_blocks_before_client_when_set_to_block(aiohttp_client, tmp_path):
    settings.SETTINGS["review_deny"] = "block"
    ws = await setup(aiohttp_client, FakeLLM("deny", "Sends cookies to another site."))
    out = await call({"tool": "click", "args": {"selector": "#go"}})
    assert out["error"] == "outrigger_review_denied" and "cookies" in out["message"]
    with pytest.raises(asyncio.TimeoutError):
        await ws.receive_json(timeout=0.2)  # client never saw a call
    entry = json.loads((tmp_path / "plugin-data/outrigger/audit.jsonl").read_text().splitlines()[-1])
    assert entry["outcome"] == "rejected:outrigger_review_denied" and entry["review"]["verdict"] == "deny"


@pytest.mark.parametrize("llm", [
    FakeLLM(exc=TimeoutError("slow")),
    FakeLLM(parsed={}),
    FakeLLM(parsed={"verdict": "yes please", "reason": "x"}),
])
async def test_reviewer_failure_escalates(aiohttp_client, llm):
    ws = await setup(aiohttp_client, llm)
    server = asyncio.create_task(serve_one(ws, lambda m: {"ok": True, "content": "x"}))
    await call({"tool": "click", "args": {"selector": "#go"}})
    assert (await server)["review"]["verdict"] == "escalate"


async def test_auto_tools_are_not_reviewed(aiohttp_client):
    llm = FakeLLM("approve")
    ws = await setup(aiohttp_client, llm)
    server = asyncio.create_task(serve_one(ws, lambda m: {"ok": True, "content": "x"}))
    await call({"tool": "get_page", "args": {}})
    assert "review" not in (await server) and llm.calls == []


async def test_review_off(aiohttp_client):
    settings.SETTINGS["review"] = "off"
    llm = FakeLLM("deny")
    ws = await setup(aiohttp_client, llm)
    server = asyncio.create_task(serve_one(ws, lambda m: {"ok": True, "content": "x"}))
    assert (await call({"tool": "click", "args": {"selector": "#go"}}))["ok"]
    assert "review" not in (await server) and llm.calls == []


async def test_operator_policy_appended_to_instructions(aiohttp_client):
    settings.SETTINGS["review_policy"] = "Always ESCALATE on bank.example."
    llm = FakeLLM("approve")
    ws = await setup(aiohttp_client, llm)
    asyncio.create_task(serve_one(ws, lambda m: {"ok": True, "content": "x"}))
    await call({"tool": "click", "args": {"selector": "#go"}})
    assert llm.calls[0]["instructions"].endswith("Always ESCALATE on bank.example.")


async def test_intent_cleared_at_turn_end(aiohttp_client):
    await setup(aiohttp_client, FakeLLM("approve"))
    outrigger._on_session_end(session_id="S1")
    from outrigger.registry import REGISTRY
    assert "S1" not in REGISTRY.intent


def test_register_inherits_approval_slot_and_falls_back():
    calls = []

    class Ctx:
        llm = object()
        def register_auxiliary_task(self, name, **kw):
            calls.append(kw)
            if "inherit_from" in kw:
                raise TypeError("unexpected keyword 'inherit_from'")

    reviewer.configure(Ctx())
    assert calls[0]["inherit_from"] == "approval" and "inherit_from" not in calls[1]
    assert reviewer._llm is Ctx.llm


async def test_deny_goes_to_user_by_default(aiohttp_client, tmp_path):
    ws = await setup(aiohttp_client, FakeLLM("deny", "Looks off-task."))
    server = asyncio.create_task(serve_one(ws, lambda m: {"ok": False, "error": "The user denied this action."}))
    out = await call({"tool": "click", "args": {"selector": "#more"}})
    msg = await server
    assert msg["review"] == {"verdict": "deny", "reason": "Looks off-task."}
    assert out["message"] == "The user denied this action."
    entry = json.loads((tmp_path / "plugin-data/outrigger/audit.jsonl").read_text().splitlines()[-1])
    assert entry["review"]["verdict"] == "deny" and entry["outcome"] == "error"


async def test_reviewer_sees_earlier_actions_this_turn(aiohttp_client):
    llm = FakeLLM("approve")
    ws = await setup(aiohttp_client, llm)
    for sel in ("#post1", "#more"):
        asyncio.create_task(serve_one(ws, lambda m: {"ok": True, "content": "x"}))
        await call({"tool": "click", "args": {"selector": sel}})
    second = json.loads(llm.calls[1]["input"][0]["text"].removeprefix("<untrusted>\n").removesuffix("\n</untrusted>"))
    assert [a["args"] for a in second["earlier_actions_this_turn"]] == ['{"selector": "#post1"}']
    assert json.loads(llm.calls[0]["input"][0]["text"][12:-13])["earlier_actions_this_turn"] == []
    outrigger._on_session_end(session_id="S1")
    from outrigger.registry import REGISTRY
    assert REGISTRY.recent_actions("S1") == []
