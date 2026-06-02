"""Tests for transient-error retry/backoff around model calls — no real API.

Covers the retryable/non-retryable classification, the backoff schedule, the
generic _call_with_retries loop, and that _execute survives a flaky client (a
rate-limit/network blip mid-turn does not abort the session).
"""

import os
import sys
import types

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from anthropic import APIConnectionError, RateLimitError, InternalServerError, BadRequestError

from src import dm_agent
from src.dm_agent import DMAgent, _is_retryable, _retry_delay, _retry_after
from src.game_state import GameState, Character


_REQ = httpx.Request("POST", "http://test")


def _conn():
    return APIConnectionError(request=_REQ)


def _rate(retry_after=None):
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    return RateLimitError("rate", response=httpx.Response(429, request=_REQ, headers=headers), body=None)


def _server(code=503):
    return InternalServerError("oops", response=httpx.Response(code, request=_REQ), body=None)


def _bad():
    return BadRequestError("bad", response=httpx.Response(400, request=_REQ), body=None)


def _agent() -> DMAgent:
    gs = GameState(location="x")
    gs.party["aldric"] = Character(name="Aldric")
    agent = DMAgent(gs, client=object())   # client unused by the retry loop
    agent._sleep = lambda d: None          # never actually wait in tests
    return agent


# --- classification -----------------------------------------------------------

def test_is_retryable_transient_errors():
    assert _is_retryable(_conn()) is True
    assert _is_retryable(_rate()) is True
    assert _is_retryable(_server(500)) is True
    assert _is_retryable(_server(503)) is True


def test_is_retryable_rejects_client_errors_and_others():
    assert _is_retryable(_bad()) is False           # 400 — deterministic
    assert _is_retryable(ValueError("nope")) is False


# --- backoff schedule ---------------------------------------------------------

def test_retry_delay_honors_retry_after_header():
    assert _retry_after(_rate(retry_after=2)) == 2.0
    assert _retry_delay(1, _rate(retry_after=2)) == 2.0


def test_retry_delay_exponential_with_jitter_is_bounded():
    # attempt 3 → base 1*2^2 = 4, jittered to 50–100% → [2, 4]
    for _ in range(50):
        d = _retry_delay(3, _conn())
        assert 2.0 <= d <= 4.0


def test_retry_delay_capped():
    # a huge attempt would overflow the base but must clamp to the cap
    assert _retry_delay(20, _conn()) <= dm_agent._RETRY_CAP


# --- the retry loop -----------------------------------------------------------

def test_call_with_retries_succeeds_after_transient_failures():
    agent = _agent()
    sleeps = []
    agent._sleep = sleeps.append
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _conn()
        return "ok"

    assert agent._call_with_retries(fn) == "ok"
    assert calls["n"] == 3            # 1 initial + 2 retries
    assert len(sleeps) == 2           # slept before each retry


def test_call_with_retries_reraises_after_exhaustion():
    agent = _agent()
    agent.max_api_retries = 2
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _rate()

    with pytest.raises(RateLimitError):
        agent._call_with_retries(fn)
    assert calls["n"] == 3            # initial + 2 retries, then give up


def test_call_with_retries_does_not_retry_client_error():
    agent = _agent()
    sleeps = []
    agent._sleep = sleeps.append
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _bad()

    with pytest.raises(BadRequestError):
        agent._call_with_retries(fn)
    assert calls["n"] == 1            # surfaced immediately
    assert sleeps == []


def test_on_retry_hook_invoked_with_details():
    agent = _agent()
    seen = []
    agent.on_retry = lambda attempt, delay, exc: seen.append((attempt, type(exc).__name__))
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _server(500)
        return "ok"

    agent._call_with_retries(fn)
    assert seen == [(1, "InternalServerError")]


# --- integration: _execute survives a flaky client ----------------------------

class _FakeUsage:
    input_tokens = 10
    output_tokens = 5
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _FakeResp:
    """A terminal (non-tool) response carrying one text block."""
    stop_reason = "end_turn"
    usage = _FakeUsage()

    def __init__(self, text):
        self.content = [types.SimpleNamespace(type="text", text=text)]


class _FlakyClient:
    """messages.create raises a transient error `fail_times`, then returns a response."""
    def __init__(self, fail_times, text="The torch gutters."):
        self.fail_times = fail_times
        self.text = text
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise _conn()
        return _FakeResp(self.text)


def test_execute_retries_through_a_blip():
    gs = GameState(location="x")
    gs.party["aldric"] = Character(name="Aldric")
    agent = DMAgent(gs, client=_FlakyClient(fail_times=2))
    agent._sleep = lambda d: None

    out = agent._execute("[Tool-use phase] do a thing", capture_narration=True)

    assert out == "The torch gutters."
    assert agent.client.calls == 3        # 2 failures + 1 success, no abort
