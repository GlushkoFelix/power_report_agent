"""生成阶段健壮性 + 事件推送兜底（2026-10-14，问题 `P-032` / `P-033` 的回归用例）。

四个真实教训固化在这里：
1. **空输出要重发一次**：聚合端点会偶发"思考吃光输出额度 → content 为空"（`E_LLM 模型输出为空`）。
   空输出不含语义，重发既安全又能救回整题；不重发则整题失败，并曾连带触发 P-033。
2. **生成要有软超时**：同端点出现过单次 53.9s 的长尾；生成不能像解读那样"跳过"，
   超时按失败处理（`E_LLM`），避免整轮干等。
3. **超时也要重发一次（C1，2026-10-16）**：慢时段实测有整题卡在 gen 上撞满超时
   （该轮 `llm_calls` 只留 `[slot]`，gen 那次直接消失）。超时多为"这一枪慢"而非"命中拥塞"，
   故**首次超时不再直接判死**：重发一次，两次都超时才失败。最坏耗时 = 2 × timeout。
4. **事件推送必须兜底**：图流结束后（失败兜底路径）再调 custom-stream 写入器会抛
   `RuntimeError: Called get_config outside of a runnable context` —— 必须回落到既有 SSE 回调，
   否则"失败留痕"本身会把题目打成 `E_EVAL_RUNNER` 0 秒失败（看起来像模型答错）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent.adapters import GraphContext
from app.core.errors import E_LLM, BizError
from app.core.llm_client import LLMReply, ScriptedLLM
from app.services import analysis_service as svc

pytestmark = pytest.mark.anyio

_MESSAGES = [{"role": "user", "content": "全期总用电量是多少？"}]


def _reply(text: str) -> LLMReply:
    return LLMReply(
        text=text, model="stub", purpose="gen_sql", prompt_tokens=10, completion_tokens=5, elapsed_ms=1
    )


class _EmptyThenOk:
    """第一次空输出、第二次给可用 JSON（复刻端点偶发行为）。"""

    name = "stub"
    configured = True
    supports_model_tiers = False

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, *, purpose="", model=None, temperature=None, json_mode=False):
        self.calls += 1
        if self.calls == 1:
            return _reply("")
        return _reply('{"route": "sql", "sql": "SELECT 1 AS x LIMIT 1"}')


class _SlowStub:
    """永不返回（用于验证软超时）。"""

    name = "stub"
    configured = True
    supports_model_tiers = False

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, *, purpose="", model=None, temperature=None, json_mode=False):
        self.calls += 1
        await asyncio.sleep(5)
        raise AssertionError("软超时未生效：不该走到这里")


class _TimeoutThenOk:
    """第一次调用超时（慢尾）、第二次给可用 JSON —— C1 的抢救路径。"""

    name = "stub"
    configured = True
    supports_model_tiers = False

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, *, purpose="", model=None, temperature=None, json_mode=False):
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(5)  # 会被 wait_for 掐断，复刻"这一枪慢"
        return _reply('{"route": "sql", "sql": "SELECT 1 AS x LIMIT 1"}')


async def test_generate_retries_once_on_empty_output() -> None:
    """空输出 → 重发一次；两次调用都要出现在返回序列里（逐次计费、逐次留痕）。"""
    llm = _EmptyThenOk()
    replies = await svc._chat_generate(llm, _MESSAGES)
    assert llm.calls == 2, "空输出必须重发一次"
    assert len(replies) == 2, "两次调用都要返回（llm_calls 需逐次留痕）"
    assert svc._safe_json(replies[-1].text, stage="gen_sql")["route"] == "sql"


async def test_generate_timeout_becomes_ellm(monkeypatch) -> None:
    """两次都超时 → 翻译成 `E_LLM`（可留痕的失败），而不是无限等待。"""
    monkeypatch.setattr(svc, "_generate_timeout", lambda: 0.05)
    llm = _SlowStub()
    with pytest.raises(BizError) as excinfo:
        await svc._chat_generate(llm, _MESSAGES)
    assert excinfo.value.error_code == E_LLM
    assert "超时" in excinfo.value.message
    assert llm.calls == 2, "首次超时必须重发一次，两次都超时才按失败上报"


async def test_generate_retries_once_on_first_timeout(monkeypatch) -> None:
    """**首次超时也重发**（C1）：慢尾只丢一次 gen，而不是整题失败。"""
    monkeypatch.setattr(svc, "_generate_timeout", lambda: 0.05)
    llm = _TimeoutThenOk()
    replies = await svc._chat_generate(llm, _MESSAGES, validate=svc._looks_like_generation)
    assert llm.calls == 2, "首次超时必须重发一次"
    assert len(replies) == 1, "超时的那次没有回复对象，只有被救回的那次留痕"
    assert svc._safe_json(replies[0].text, stage="gen_sql")["sql"].startswith("SELECT")


def test_emit_event_falls_back_when_writer_outside_context() -> None:
    """P-033 回归：图已结束后推送事件（writer 抛 RuntimeError）→ 回落既有回调、且不抛异常。"""

    def _broken_writer(event):  # noqa: ANN001
        raise RuntimeError("Called get_config outside of a runnable context")

    seen: list[dict] = []
    ctx = GraphContext(
        llm=ScriptedLLM(), settings=None, emit=seen.append,
        stream_writer=_broken_writer, _writer_probed=True,
    )
    ctx.emit_event({"event": "error", "data": {"code": E_LLM}})
    assert seen and seen[-1]["event"] == "error", "必须回落到既有 SSE 回调"
    assert ctx.stream_writer is None, "失效的写入器要被作废，避免每次重试"


class _IncompleteThenOk:
    """第一次返回"解析得动但缺 sql/code"的半成品、第二次给可用 JSON（复刻 q119/q110 的实测形态）。"""

    name = "stub"
    configured = True
    supports_model_tiers = False

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, *, purpose="", model=None, temperature=None, json_mode=False):
        self.calls += 1
        if self.calls == 1:
            return _reply('{"route": "sql", "reason": "先想一下"}')  # 半成品：没有 sql/code
        return _reply('{"route": "sql", "sql": "SELECT 1 AS x LIMIT 1"}')


async def test_generate_retries_on_incomplete_payload() -> None:
    """**半成品也要重发**：解析得动但缺 `sql`/`code` 的回复不含可执行语义（实测 q119/q110）。"""
    llm = _IncompleteThenOk()
    replies = await svc._chat_generate(llm, _MESSAGES, validate=svc._looks_like_generation)
    assert llm.calls == 2, "半成品必须重发一次"
    assert svc._safe_json(replies[-1].text, stage="gen_sql")["sql"].startswith("SELECT")
    assert len(replies) == 2, "两次调用都要留痕"


def test_looks_like_generation_accepts_sql_or_code() -> None:
    """可用性判定：`sql` / `code` 有一个非空才算可用；空串、半成品、非 JSON 都算不可用。"""
    assert svc._looks_like_generation('{"route": "sql", "sql": "SELECT 1"}') is True
    assert svc._looks_like_generation('{"route": "pandas", "code": "result = df"}') is True
    assert svc._looks_like_generation('{"route": "sql", "reason": "想一下"}') is False
    assert svc._looks_like_generation('{"route": "sql", "sql": "   "}') is False
    assert svc._looks_like_generation("") is False
    assert svc._looks_like_generation("这不是 JSON") is False


def test_emit_event_uses_writer_inside_context() -> None:
    """正常路径不变：图运行上下文内仍走 custom stream（真流式），不回落。"""
    seen: list[dict] = []
    written: list[dict] = []
    ctx = GraphContext(
        llm=ScriptedLLM(), settings=None, emit=seen.append,
        stream_writer=written.append, _writer_probed=True,
    )
    ctx.emit_event({"event": "table", "data": {"row_count": 1}})
    assert written and not seen
