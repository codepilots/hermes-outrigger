"""Claims-pass hardening follow-ups (behavior-changing remediation pass).

H-01: a capture-write OSError must not escape _save_capture -> _format_ok -> _run
and swallow the audit entry; the call completes as ok with image_error instead.
H-02: non-dict top-level JSON in the token store must take the documented
empty-store path (fail closed), not raise AttributeError.

State-neutral: HERMES_HOME/tmp paths come from the autouse conftest fixture;
the tokens._cache module global is saved/reset/restored around every H-02 test.
"""
import asyncio
import base64
import copy
import json
import logging
import os
import stat
from pathlib import Path

import pytest

from outrigger import dispatch, tokens

from helpers import call, connect, serve_one, start

LOG = "hermes.plugins.outrigger"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
PNG_B64 = base64.b64encode(PNG).decode()
IMAGE = {"mime": "image/png", "data": PNG_B64}


def _captures(tmp_path):
    return tmp_path / "plugin-data" / "outrigger" / "captures"


def _occupy_captures(tmp_path):
    """captures path held by a regular file: mkdir raises FileExistsError (an OSError)."""
    folder = _captures(tmp_path)
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.write_text("not a directory")
    return folder


def _audit_entries(tmp_path):
    p = tmp_path / "plugin-data" / "outrigger" / "audit.jsonl"
    assert p.exists(), "expected an audit entry to be written"
    return [json.loads(line) for line in p.read_text().splitlines()]


# ================================================================ H-01: capture-write guard


def test_capture_path_occupied_by_file_returns_image_error(tmp_path):
    """(a) A regular file at the captures path must yield image_error, never raise."""
    _occupy_captures(tmp_path)
    out = dispatch._save_capture(dict(IMAGE))
    assert set(out) == {"image_error"}, out
    assert out["image_error"].startswith("capture write failed:")
    assert "FileExistsError" in out["image_error"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_unwritable_captures_dir_returns_image_error(tmp_path):
    """(b) chmod 000 captures dir: os.open raises PermissionError -> image_error."""
    folder = _captures(tmp_path)
    folder.mkdir(parents=True)
    os.chmod(folder, 0o000)
    try:
        out = dispatch._save_capture(dict(IMAGE))
    finally:
        os.chmod(folder, 0o700)  # restore so tmp_path teardown can clean up
    assert set(out) == {"image_error"}, out
    assert out["image_error"].startswith("capture write failed:")
    assert "PermissionError" in out["image_error"]


def test_capture_failure_logs_warning_with_path_and_reason(tmp_path, caplog):
    """(c) The failure is loud on the outrigger logger: path + reason, never image bytes."""
    folder = _occupy_captures(tmp_path)
    caplog.set_level(logging.WARNING, logger=LOG)
    out = dispatch._save_capture(dict(IMAGE))
    assert "image_error" in out
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING and r.name == LOG]
    assert loud, "capture write failure must log a loud warning"
    msg = loud[-1].getMessage()
    assert str(folder) in msg, "the warning must name the captures path"
    assert "File exists" in msg, "the warning must carry the failure reason"
    for r in loud:
        assert PNG_B64 not in r.getMessage(), "image bytes must never reach the log"


def test_capture_success_shape_mode_and_bytes(tmp_path):
    """(d) Success path preserved: same return shape, exact bytes, 0600 creation."""
    out = dispatch._save_capture(dict(IMAGE))
    assert set(out) == {"image_path", "bytes", "hint"}, out
    p = Path(out["image_path"])
    assert p.read_bytes() == PNG
    assert out["bytes"] == len(PNG)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert "vision_analyze" in out["hint"]


def test_capture_retention_prunes_oldest(tmp_path):
    """(d) Retention preserved: only the newest KEEP_CAPTURES files survive."""
    folder = _captures(tmp_path)
    folder.mkdir(parents=True)
    olds = []
    for i in range(dispatch.KEEP_CAPTURES + 5):
        f = folder / f"old-{i:03d}.png"
        f.write_bytes(PNG)
        os.utime(f, ns=(10**9 + i, 10**9 + i))  # staggered, all older than the new capture
        olds.append(f)
    out = dispatch._save_capture(dict(IMAGE))
    assert "image_path" in out, out
    remaining = set(folder.iterdir())
    assert len(remaining) == dispatch.KEEP_CAPTURES
    assert Path(out["image_path"]) in remaining
    for f in olds[:6]:  # 25 old + 1 new = 26 -> the 6 oldest go
        assert f not in remaining, f
    for f in olds[6:]:
        assert f in remaining, f


async def test_image_call_with_broken_captures_is_ok_and_audited(aiohttp_client, tmp_path):
    """(e) End-to-end: an image result under a broken captures path completes as ok
    JSON carrying image_error, AND the call is audited with outcome ok."""
    _occupy_captures(tmp_path)
    c = await start(aiohttp_client)
    ws, _ = await connect(c)
    asyncio.create_task(serve_one(
        ws, lambda m: {"ok": True, "content": {"note": "shot taken", "image": dict(IMAGE)}}))
    out = await call({"tool": "echo", "args": {"text": "hi"}})
    assert out["ok"] is True, out
    content = json.loads(out["content"])
    assert content["note"] == "shot taken"
    assert "image_error" in content and "image_path" not in content
    entry = _audit_entries(tmp_path)[-1]
    assert entry["outcome"] == "ok"
    assert entry["tool"] == "echo" and entry["session"] == "S1"


# ================================================================ H-02: non-dict token-store JSON


class TestH02NonDictTokenStore:
    """Every corrupt top-level JSON shape must take the empty-store path (fail closed:
    no token verifies, no exception). tokens._cache is a module global: save/reset/restore
    per test so nothing leaks into sibling files."""

    @pytest.fixture(autouse=True)
    def _neutral_token_cache(self):
        saved = copy.deepcopy(tokens._cache)
        tokens._cache.update(key=None, data={"tokens": {}})
        yield
        tokens._cache.clear()
        tokens._cache.update(saved)

    def _write_store(self, text):
        p = tokens.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    @pytest.mark.parametrize("shape", ["[1,2,3]", '"str"', "42", "null"])
    def test_non_dict_json_is_empty_store_and_fails_closed(self, tmp_path, shape):
        self._write_store(shape)
        assert tokens._load() == {"tokens": {}}
        assert tokens.verify("otr_deadbeef_" + "a" * 32) is None  # syntactically valid token

    @pytest.mark.parametrize("shape", ["[1,2,3]", '"str"', "42", "null"])
    def test_previously_valid_token_stops_verifying(self, tmp_path, shape):
        tid, tok = tokens.mint("ann")
        assert tokens.verify(tok) is not None
        self._write_store(shape)  # store corrupted under a live token
        assert tokens._load() == {"tokens": {}}
        assert tokens.verify(tok) is None

    # ------------------------------------------------ preservation (must hold before and after)

    def test_valid_store_still_loads_and_verifies(self, tmp_path):
        tid, tok = tokens.mint("ann", user_name="Ann")
        assert tokens.verify(tok) == {"id": tid, "user_id": "ann", "user_name": "Ann",
                                      "label": "", "admin": False}

    def test_dict_shaped_corruption_is_empty_store(self, tmp_path):
        self._write_store('{"other": 1}')
        assert tokens._load() == {"tokens": {}}
        assert tokens.verify("otr_deadbeef_" + "a" * 32) is None

    def test_missing_file_is_empty_store(self, tmp_path):
        assert not tokens.path().exists()
        assert tokens._load() == {"tokens": {}}
        assert tokens.verify("otr_deadbeef_" + "a" * 32) is None

    def test_mtime_cache_hit_and_invalidation(self, tmp_path):
        tid, tok = tokens.mint("bob")
        first = tokens._load()
        key = tokens._cache["key"]
        assert tokens._load() is first, "unchanged mtime must serve the cached store"
        assert tokens._cache["key"] == key
        data = json.loads(tokens.path().read_text())  # another process revokes it
        data["tokens"][tid]["revoked"] = 1
        tokens.path().write_text(json.dumps(data))
        os.utime(tokens.path(), ns=(1, 10**18))  # make sure the mtime differs
        assert tokens.verify(tok) is None, "changed mtime must re-read the store"
