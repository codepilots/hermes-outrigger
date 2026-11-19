"""Independent-review follow-up: tokens.py regexes use \\Z (F-11 consistency).

TOKEN_RE/USER_RE previously anchored with $, accepting a trailing newline.
Practically unexploitable (Bearer path strips; header values cannot carry LF;
USER_RE guards operator-CLI mint only) but normalized for consistency.
"""
from outrigger import tokens


def test_token_re_rejects_trailing_newline():
    good = "otr_" + "a" * 8 + "_" + "b" * 32
    assert tokens.TOKEN_RE.match(good)
    assert tokens.TOKEN_RE.match(good + "\n") is None
    assert tokens.TOKEN_RE.match(good + "\r\n") is None


def test_user_re_rejects_trailing_newline():
    assert tokens.USER_RE.match("ann")
    assert tokens.USER_RE.match("ann\n") is None
    assert tokens.USER_RE.match("\nann") is None


def test_user_re_bounds_unchanged():
    assert tokens.USER_RE.match("a" * 64)
    assert tokens.USER_RE.match("a" * 65) is None
