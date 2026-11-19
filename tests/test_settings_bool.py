"""F-10 follow-up: bool-typed settings must coerce string/integer config values.

A quoted YAML string like allow_keyless_local: "false" is truthy in Python; raw
truthiness would leave a security refusal switch ON (fail open). Bool settings
must normalize true/false-ish strings and 0/1, and log loud + fall back to the
declared default on unrecognized values.
"""
import logging
import types

import pytest

from outrigger import settings, ws


def _load(vals):
    settings.SETTINGS.clear()
    settings.SETTINGS.update(settings.DEFAULTS)
    settings.load(lambda k, d: vals.get(k, d))


def _local_request():
    return types.SimpleNamespace(remote="127.0.0.1", headers={}, app={})


@pytest.mark.parametrize("raw", ["false", "False", "FALSE", " false ", "0", "no", "off", ""])
def test_keyless_false_strings_coerce_false(raw):
    _load({"allow_keyless_local": raw})
    assert settings.get("allow_keyless_local") is False


@pytest.mark.parametrize("raw", ["true", "True", "YES", "on", "1"])
def test_keyless_true_strings_coerce_true(raw):
    _load({"allow_keyless_local": raw})
    assert settings.get("allow_keyless_local") is True


def test_real_bools_preserved():
    _load({"allow_keyless_local": False, "allow_remote": True})
    assert settings.get("allow_keyless_local") is False
    assert settings.get("allow_remote") is True


def test_int_zero_one_coerce():
    _load({"allow_keyless_local": 0, "allow_remote": 1})
    assert settings.get("allow_keyless_local") is False
    assert settings.get("allow_remote") is True


def test_unrecognized_falls_back_to_default_loud(caplog):
    with caplog.at_level(logging.WARNING, logger="hermes.plugins.outrigger"):
        _load({"allow_keyless_local": "maybe"})
    assert settings.get("allow_keyless_local") is settings.DEFAULTS["allow_keyless_local"]
    assert any("allow_keyless_local" in r.getMessage() for r in caplog.records)


def test_remote_false_string_coerce_false():
    # pre-existing latent pattern: allow_remote "false" must not enable remote
    _load({"allow_remote": "false", "owner_key_remote": "false"})
    assert settings.get("allow_remote") is False
    assert settings.get("owner_key_remote") is False


def test_absent_keeps_default():
    _load({})
    assert settings.get("allow_keyless_local") is True
    assert settings.get("allow_remote") is False


def test_authorize_refuses_keyless_when_string_false():
    _load({"allow_keyless_local": "false", "auth_token": ""})
    resp = ws.authorize(_local_request())
    assert resp is not None and resp.status == 403


def test_authorize_admits_keyless_when_string_true():
    _load({"allow_keyless_local": "true", "auth_token": ""})
    assert ws.authorize(_local_request()) is None


def test_non_bool_settings_untouched():
    _load({"max_result_chars": "20000", "review_deny": "block"})
    # non-bool settings keep their raw value (no bool coercion applied)
    assert settings.get("review_deny") == "block"
