"""LLM wrapper offline tests: fake chat models through LLMClient(factory=...)."""
from __future__ import annotations

from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel

from retail_agent.agent.state import RateLimitWait
from retail_agent.config import ModelSpec
from retail_agent.llm.errors import (
    DeadlineExceededError, LLMOutputError, LLMRequestError, LLMUnavailableError, QuotaExhaustedError,
    classify_rate_limit, retry_after_seconds,
)
from retail_agent.llm.wrapper import Deadline, LLMClient, TurnBudget, TurnContext
from retail_agent.tracing import NullTracer


class ApiError(Exception):
    """Shaped like google.genai.errors.APIError."""

    def __init__(self, code: int, details: dict | None = None, headers: dict | None = None):
        self.code = code
        self.details = details or {}
        self.response = type("R", (), {"headers": headers or {}})()
        super().__init__(f"{code} {self.details}")


def per_minute(retry_delay: str | None = None, headers: dict | None = None) -> ApiError:
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}]
    if retry_delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay})
    return ApiError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": details}}, headers)


def daily() -> ApiError:
    return ApiError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}})


class Fake(BaseChatModel):
    script: Any = None
    calls: Any = None
    timeout_s: float = 0

    @property
    def _llm_type(self) -> str:
        return "fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(messages)
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return ChatResult(generations=[ChatGeneration(message=step)])

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, include_raw=False, **kwargs):
        outer = self

        class R:
            def invoke(self, messages):
                msg = outer.invoke(messages)
                try:
                    return {"raw": msg, "parsed": schema.model_validate_json(msg.content), "parsing_error": None}
                except Exception as e:  # noqa: BLE001
                    return {"raw": msg, "parsed": None, "parsing_error": e}
        return R()


def ai(text="ok", tin=10, tout=5) -> AIMessage:
    return AIMessage(content=text, usage_metadata={"input_tokens": tin, "output_tokens": tout, "total_tokens": tin + tout})


class Harness:
    def __init__(self, settings, scripts: dict[str, list]):
        self.scripts = scripts
        self.timeouts: list[tuple[str, float]] = []
        self.now = 0.0
        self.sleeps: list[float] = []
        self.settings = settings
        self.client = LLMClient(settings, factory=self._factory, sleep=self._sleep, rng=lambda: 1.0)

    def _factory(self, spec: ModelSpec, timeout: float):
        self.timeouts.append((spec.model, timeout))
        return Fake(script=self.scripts[spec.model], calls=[])

    def _sleep(self, s: float):
        self.sleeps.append(s)
        self.now += s

    def ctx(self, seconds=60.0, reserve=20.0) -> TurnContext:
        self.events: list = []
        return TurnContext(thread_id="t", turn_id="u", exec_id="e", intent="analysis",
                           deadline=Deadline(seconds, clock=lambda: self.now), synthesis_reserve_s=reserve,
                           budget=TurnBudget(tool_calls_limit=6), tracer=NullTracer(), emit=self.events.append)

    def call(self, ctx, role="expensive", **kw):
        return self.client.call_llm(role, [HumanMessage("hi")], ctx, node="analysis", **kw)


@pytest.fixture
def chain(settings):
    return [s.model for s in settings.roles["expensive"]]


def test_success_accounting(settings, chain):
    h = Harness(settings, {chain[0]: [ai("hello", 12, 3)]})
    ctx = h.ctx()
    r = h.call(ctx)
    assert (r.model, r.fallback_used, r.retry_count, r.tokens_in, r.tokens_out) == (chain[0], False, 0, 12, 3)
    assert ctx.budget.llm_calls == 1 and (ctx.budget.tokens_in, ctx.budget.tokens_out) == (12, 3)
    [rec] = ctx.tracer.records
    assert rec["event"] == "llm_call" and rec["attempt"] == 1 and rec["response"] == "hello"
    assert rec["prompt"] == [{"role": "human", "content": "hi"}] and rec["role"] == "expensive"


def test_retry_per_minute_honours_retry_after(settings, chain):
    h = Harness(settings, {chain[0]: [per_minute("7s"), ai()]})
    ctx = h.ctx()
    r = h.call(ctx)
    assert r.retry_count == 1 and h.sleeps == [7.0]
    assert [type(e) for e in h.events] == [RateLimitWait] and h.events[0].seconds == 7.0
    assert [(x["attempt"], x.get("error_type")) for x in ctx.tracer.records] == [(1, "llm_unavailable"), (2, None)]
    assert ctx.budget.llm_calls == 2


def test_retry_after_header_and_backoff(settings, chain):
    h = Harness(settings, {chain[0]: [per_minute(headers={"retry-after": "3"}), ApiError(503), ai()]})
    r = h.call(h.ctx())
    assert r.retry_count == 2 and h.sleeps[0] == 3.0
    assert h.sleeps[1] == settings.llm.backoff_base_s * 2  # rng stub returns 1.0


def test_at_most_max_retries_then_fallback(settings, chain):
    n = settings.llm.max_retries + 1
    h = Harness(settings, {chain[0]: [ApiError(503)] * n, chain[1]: [ai()]})
    ctx = h.ctx(seconds=600)
    r = h.call(ctx)
    assert r.model == chain[1] and r.fallback_used and ctx.budget.fallback_used
    assert ctx.budget.llm_calls == n + 1 and len(h.sleeps) == settings.llm.max_retries
    assert ctx.tracer.records[-1]["fallback_used"] is True


def test_no_retry_on_4xx(settings, chain):
    h = Harness(settings, {chain[0]: [ApiError(400), ai()]})
    ctx = h.ctx()
    with pytest.raises(LLMRequestError):
        h.call(ctx)
    assert h.sleeps == [] and ctx.budget.llm_calls == 1
    assert ctx.tracer.records[0]["error_type"] == "llm_request"


def test_daily_quota_goes_to_fallback_without_retry(settings, chain):
    h = Harness(settings, {chain[0]: [daily()], chain[1]: [ai()]})
    ctx = h.ctx()
    r = h.call(ctx)
    assert r.model == chain[1] and r.retry_count == 0 and h.sleeps == []
    assert ctx.tracer.records[0]["error_type"] == "quota_exhausted"


def test_chain_exhausted_by_daily_quota(settings, chain):
    h = Harness(settings, {m: [daily()] for m in chain})
    with pytest.raises(QuotaExhaustedError) as e:
        h.call(h.ctx())
    assert e.value.models == tuple(chain) and e.value.primary_model == chain[0] and e.value.role == "expensive"
    assert chain[0] in str(e.value)


def test_chain_exhausted_by_outage(settings, chain):
    n = settings.llm.max_retries + 1
    h = Harness(settings, {m: [ApiError(500)] * n for m in chain})
    with pytest.raises(LLMUnavailableError):
        h.call(h.ctx(seconds=6000))


def test_deadline_caps_timeout(settings, chain):
    h = Harness(settings, {chain[0]: [ai()]})
    h.call(h.ctx(seconds=35, reserve=20))
    assert h.timeouts == [(chain[0], 15.0)]
    h = Harness(settings, {chain[0]: [ai()]})
    h.call(h.ctx(seconds=35, reserve=20), is_synthesis=True)
    assert h.timeouts == [(chain[0], settings.llm.call_timeout_s)]


def test_deadline_too_short_does_not_call(settings, chain):
    h = Harness(settings, {chain[0]: [ai()]})
    ctx = h.ctx(seconds=20, reserve=20)
    with pytest.raises(DeadlineExceededError):
        h.call(ctx)
    assert h.timeouts == [] and ctx.budget.llm_calls == 0 and ctx.tracer.records == []


def test_deadline_blocks_retry(settings, chain):
    # 25s left, reserve 20, wait 7s: retry would leave no room, so the wrapper moves to the next model.
    h = Harness(settings, {chain[0]: [per_minute("7s")], chain[1]: [ai()]})
    r = h.call(h.ctx(seconds=29, reserve=20))
    assert h.sleeps == [] and r.model == chain[1]


def test_deadline_blocks_retry_raises_when_chain_ends(settings):
    h = Harness(settings, {m.model: [per_minute("30s")] for m in settings.roles["cheap"]})
    with pytest.raises(LLMUnavailableError):
        h.call(h.ctx(seconds=25, reserve=20), role="cheap")
    assert h.sleeps == []


class Out(BaseModel):
    answer: str


def test_structured_output(settings, chain):
    h = Harness(settings, {chain[0]: [ai('{"answer": "42"}')]})
    ctx = h.ctx()
    r = h.call(ctx, schema=Out)
    assert r.parsed == Out(answer="42") and ctx.tracer.records[0]["response"] == {"answer": "42"}


def test_structured_output_one_retry_then_error(settings, chain):
    h = Harness(settings, {chain[0]: [ai("nope"), ai('{"answer": "x"}')]})
    assert h.call(h.ctx(), schema=Out).retry_count == 1
    h = Harness(settings, {chain[0]: [ai("nope"), ai("still nope")]})
    ctx = h.ctx()
    with pytest.raises(LLMOutputError):
        h.call(ctx, schema=Out)
    assert [r["error_type"] for r in ctx.tracer.records] == ["llm_output", "llm_output"]


def test_tool_calls_are_traced(settings, chain):
    msg = AIMessage(content="", tool_calls=[{"name": "run_sql", "args": {"sql": "select 1"}, "id": "1"}])
    h = Harness(settings, {chain[0]: [msg]})
    ctx = h.ctx()
    r = h.call(ctx, tools=[Out], tool_choice="any")
    assert r.message.tool_calls and ctx.tracer.records[0]["response"]["tool_calls"][0]["name"] == "run_sql"


def test_schema_and_tools_exclusive(settings, chain):
    h = Harness(settings, {})
    with pytest.raises(ValueError):
        h.call(h.ctx(), schema=Out, tools=[Out])


def test_missing_key_skips_model(settings, chain):
    client = LLMClient(settings, factory=lambda spec, t: Fake(script=[ai()], calls=[]),
                       env={"GOOGLE_API_KEY": ""})
    h = Harness(settings, {})
    with pytest.raises(LLMUnavailableError):
        client.call_llm("cheap", [HumanMessage("x")], h.ctx(), node="n")


def test_budget_counters():
    b = TurnBudget(tool_calls_limit=2)
    assert b.try_consume_tool_call() and b.try_consume_tool_call() and not b.try_consume_tool_call()
    assert b.tool_calls_remaining == 0 and b.as_dict()["tool_calls_used"] == 2


def test_deadline_clock():
    t = [100.0]
    d = Deadline(10, clock=lambda: t[0])
    assert d.remaining() == 10 and not d.expired()
    t[0] = 111
    assert d.remaining() == -1 and d.expired()
    assert Deadline.at(105, clock=lambda: t[0]).expired() is True


def test_classify_helpers():
    assert classify_rate_limit(daily()) == "daily"
    assert classify_rate_limit(per_minute("37s")) == "per_minute"
    assert classify_rate_limit(ApiError(429)) == "per_minute"
    assert classify_rate_limit(ApiError(500)) is None
    assert classify_rate_limit(ApiError(429, {"error": {"code": "quota_exceeded"}})) == "daily"
    assert retry_after_seconds(per_minute("37s")) == 37.0
    assert retry_after_seconds(per_minute("1.5s")) == 1.5
    assert retry_after_seconds(ApiError(429, headers={"Retry-After": "4"})) == 4.0
    assert retry_after_seconds(ApiError(429)) is None
    wrapped = RuntimeError("wrapped")
    wrapped.__cause__ = daily()
    assert classify_rate_limit(wrapped) == "daily"


@pytest.mark.llm
def test_live_cheap_call(monkeypatch):
    from retail_agent.config import load_settings
    s = load_settings(env={"LLM_PROFILE": "free", **__import__("os").environ})
    h = Harness.__new__(Harness)
    h.now = 0.0
    h.events = []
    ctx = TurnContext(thread_id="t", turn_id="u", exec_id="e", intent=None, deadline=Deadline(30),
                      synthesis_reserve_s=5, budget=TurnBudget(tool_calls_limit=0), tracer=NullTracer())
    r = LLMClient(s).call_llm("cheap", [HumanMessage("Reply with the single word: pong")], ctx, node="test")
    assert "pong" in r.message.text.lower() and ctx.budget.llm_calls >= 1 and r.tokens_out > 0


def test_cheap_role_has_no_reserve_and_timeout_has_floor(settings, chain):
    h = Harness(settings, {m.model: [ai()] for m in settings.roles["cheap"]})
    h.call(h.ctx(seconds=30, reserve=20), role="cheap")
    assert h.timeouts[0][1] == 30.0
    h2 = Harness(settings, {chain[0]: [ai()]})
    h2.call(h2.ctx(seconds=25, reserve=20))          # 5 s left after the reserve: raised to the floor
    assert h2.timeouts == [(chain[0], settings.llm.min_call_timeout_s)]
