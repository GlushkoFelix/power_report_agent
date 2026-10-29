"""多厂商（OpenAI 兼容）适配测试（交付物 9）。

本批的目标是**换厂商只改 `.env`**，因此这里断言的是"厂商无关性"：

1. 配置层：`LLM_API_KEY` 优先、旧名 `DASHSCOPE_API_KEY` 仍生效、`llm_configured` 口径一致；
2. 健康回报：`provider` 如实、`protocol` 恒为 `openai-compatible`、`key_source` 可见；
3. 模型分级按**能力位**而不是厂商名（换厂商不需要改业务代码）；
4. `json_mode` 被端点拒绝（400 且提示 response_format）时**降级重试一次**，关掉开关则如实报错。
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from app.core import llm_client
from app.core.config import Settings
from app.core.errors import BizError
from app.core.llm_client import OpenAICompatibleLLM, ScriptedLLM, llm_status
from app.services.analysis_service import _model_for


# ══════════════════════════════════════════════════════════════════════════
#  ① 配置层
# ══════════════════════════════════════════════════════════════════════════

def _settings(**overrides: Any) -> Settings:
    """构造不读 `.env`/环境变量的配置对象（本文件只验证字段语义）。"""
    return Settings(_env_file=None, jwt_secret_key="x" * 32, **overrides)


def test_api_key_precedence_and_backward_compat() -> None:
    """`LLM_API_KEY` 优先；只填旧名 `DASHSCOPE_API_KEY` 时仍然生效（向后兼容）。"""
    both = _settings(llm_api_key="sk-new", dashscope_api_key="sk-old")
    assert both.api_key == "sk-new" and both.llm_configured is True

    legacy = _settings(dashscope_api_key="sk-old")
    assert legacy.api_key == "sk-old" and legacy.llm_configured is True

    blank = _settings()
    assert blank.api_key == "" and blank.llm_configured is False
    # 只有空白字符也算未配置（避免 `.env` 里留空格被当成"已配置"）
    assert _settings(llm_api_key="   ").llm_configured is False


def test_placeholder_values_treated_as_unconfigured() -> None:
    """模板占位值（`.env.example` 原样复制）不当作真实 Key：否则每条分析都会以 E_LLM 失败。"""
    for placeholder in ("sk-xxxxxx", "xxxxxx", "请填入", "CHANGEME", "your-api-key"):
        assert _settings(llm_api_key=placeholder).llm_configured is False, placeholder
        assert _settings(dashscope_api_key=placeholder).llm_configured is False, placeholder
    # 真实形态的 Key 不受影响（含短横线、长度足够）
    assert _settings(llm_api_key="sk-ws-abc123def456").llm_configured is True


def test_default_provider_is_dashscope() -> None:
    """默认（不配任何厂商变量）仍指向百炼公共端点，保证既有部署零改动。"""
    settings = _settings()
    assert settings.llm_provider_label == "dashscope"
    assert "dashscope.aliyuncs.com" in settings.llm_base_url


# ══════════════════════════════════════════════════════════════════════════
#  ② 健康回报
# ══════════════════════════════════════════════════════════════════════════

def test_llm_status_reports_unconfigured(monkeypatch) -> None:
    """未配 Key → 如实回报离线脚本模型。

    **显式构造"无 Key"配置**：本机 `.env` 可能已配真实模型（本项目实测就是），
    不能让"环境有没有配 Key"决定断言结果，否则同一份代码在配了 Key 的机器上必挂。
    """
    monkeypatch.setattr(llm_client, "get_settings", lambda: _settings())
    llm_client.set_llm(None)
    try:
        status = llm_status()
        assert status["provider"] == "scripted"
        assert status["protocol"] == "openai-compatible"
        assert status["impl"] == "scripted"
        assert status["configured"] is False
        assert status["key_source"] is None
    finally:
        llm_client.set_llm(None)  # 还原，避免污染后续用例


def test_llm_status_reports_provider_and_protocol(monkeypatch) -> None:
    """配了厂商后：provider 如实回报，协议恒为 openai-compatible，key_source 可见。"""
    settings = _settings(
        llm_api_key="sk-demo", llm_provider_label="deepseek",
        llm_base_url="https://api.deepseek.com/v1",
    )
    monkeypatch.setattr(llm_client, "get_settings", lambda: settings)
    llm_client.set_llm(None)
    try:
        status = llm_status()
    finally:
        llm_client.set_llm(None)

    assert status["provider"] == "deepseek"
    assert status["protocol"] == "openai-compatible"
    assert status["impl"] == "deepseek"
    assert status["configured"] is True
    assert status["key_source"] == "LLM_API_KEY"
    assert status["base_url"] == "https://api.deepseek.com/v1"


def test_get_llm_builds_openai_compatible_client(monkeypatch) -> None:
    """配置了 Key → 构造的客户端名叫 provider 标签（不参与协议分支）。"""
    settings = _settings(
        dashscope_api_key="sk-legacy", llm_provider_label="moonshot",
        llm_model_main="kimi-k2",
    )
    monkeypatch.setattr(llm_client, "get_settings", lambda: settings)
    llm_client.set_llm(None)
    try:
        client = llm_client.get_llm()
    finally:
        llm_client.set_llm(None)
    assert isinstance(client, OpenAICompatibleLLM)
    assert client.name == "moonshot"


# ══════════════════════════════════════════════════════════════════════════
#  ③ 模型分级按能力位
# ══════════════════════════════════════════════════════════════════════════

def test_model_tiering_follows_capability_not_vendor_name() -> None:
    """任何 OpenAI 兼容客户端都支持分级（换厂商无需改代码）；脚本模型不支持。"""
    for provider in ("dashscope", "deepseek", "moonshot", "ollama"):
        client = OpenAICompatibleLLM("k", "http://localhost:11434/v1", "m", provider=provider)
        assert _model_for(client, "light-model") == "light-model"
    assert _model_for(ScriptedLLM(), "light-model") is None


# ══════════════════════════════════════════════════════════════════════════
#  ④ json_mode 降级
# ══════════════════════════════════════════════════════════════════════════

class _FakeResponse:
    def __init__(self, status_code: int, *, text: str = "", body: dict | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self._body = body or {}

    def json(self) -> dict:
        return self._body


class _FakeAsyncClient:
    """记录 payload 的假 httpx 客户端（第一次返回 400 拒绝 response_format，之后 200）。"""

    payloads: list[dict] = []
    reject_first = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def post(self, url: str, headers: dict | None = None, json: dict | None = None):
        # 深拷贝：客户端在降级路径上会就地 `payload.pop("response_format")`，
        # 若按引用记录，历史调用记录会被"事后改写"，断言就失去意义
        type(self).payloads.append(copy.deepcopy(json or {}))
        if type(self).reject_first and len(type(self).payloads) == 1:
            return _FakeResponse(
                400,
                text='{"error":{"message":"Unrecognized request argument supplied: response_format"}}',
            )
        return _FakeResponse(
            200,
            body={
                "model": "fake-model",
                "choices": [{"message": {"content": '{"sql": "SELECT 1"}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )


def _patch_httpx(monkeypatch, *, reject_first: bool) -> None:
    _FakeAsyncClient.payloads = []
    _FakeAsyncClient.reject_first = reject_first
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", _FakeAsyncClient)


def _client() -> OpenAICompatibleLLM:
    return OpenAICompatibleLLM("sk-test", "http://localhost:9999/v1", "m", provider="local")


async def _noop() -> None:  # pragma: no cover - 占位，供 anyio 标记
    return None


@pytest.mark.anyio
async def test_json_mode_falls_back_when_endpoint_rejects(monkeypatch) -> None:
    """端点不认 `response_format` → 去掉该参数重试一次并成功（换厂商不因参数被卡死）。"""
    _patch_httpx(monkeypatch, reject_first=True)
    reply = await _client().chat(
        [{"role": "user", "content": "hi"}], purpose="gen_sql", json_mode=True
    )
    assert reply.text == '{"sql": "SELECT 1"}'
    assert reply.prompt_tokens == 10 and reply.completion_tokens == 5
    assert len(_FakeAsyncClient.payloads) == 2
    assert "response_format" in _FakeAsyncClient.payloads[0]
    assert "response_format" not in _FakeAsyncClient.payloads[1]  # 降级后不再发送


@pytest.mark.anyio
async def test_network_error_reports_neutral_hint(monkeypatch) -> None:
    """端点不可达：如实报网络异常 + 中性提示（不写死某个厂商的变量名）。"""

    class _Boom(_FakeAsyncClient):
        async def post(self, *args: Any, **kwargs: Any):
            raise llm_client.httpx.ConnectError("connection refused")

    monkeypatch.setattr(llm_client.httpx, "AsyncClient", _Boom)
    with pytest.raises(BizError) as excinfo:
        await _client().chat([{"role": "user", "content": "hi"}], purpose="gen_sql")
    assert excinfo.value.error_code == "E_LLM"
    assert excinfo.value.data["provider"] == "local"
    assert "LLM_BASE_URL" in excinfo.value.data["hint"]


@pytest.mark.anyio
async def test_json_mode_fallback_can_be_disabled(monkeypatch) -> None:
    """关掉降级开关时：如实抛 `E_LLM`（含厂商与建议），不静默吞掉。"""
    _patch_httpx(monkeypatch, reject_first=True)
    monkeypatch.setattr(
        llm_client, "get_settings",
        lambda: SimpleNamespace(llm_temperature=0.0, llm_json_mode_fallback=False),
    )
    with pytest.raises(BizError) as excinfo:
        await _client().chat(
            [{"role": "user", "content": "hi"}], purpose="gen_sql", json_mode=True
        )
    assert excinfo.value.error_code == "E_LLM"
    assert excinfo.value.data["provider"] == "local"
    assert len(_FakeAsyncClient.payloads) == 1  # 只尝试一次


# ══════════════════════════════════════════════════════════════════════════
#  ⑤ 非 JSON 响应（上游网关抖动）—— 问题 P-039
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_non_json_response_reports_e_llm_not_crash(monkeypatch) -> None:
    """**2xx 但正文不是 JSON**（网关空响应 / 错误页）→ 如实报 `E_LLM`，不让 `JSONDecodeError` 裸穿。

    实测（问题 `P-039`，2026-10-14 单批 120 题）：端点抖动时会返回非 JSON 正文，
    旧代码在 `response.json()` 处抛 `JSONDecodeError` → 编排层兜成 **`E_INTERNAL`**（有题 2.8s 就"失败"），
    既误导归因（看着像代码 bug 或模型问题），又把"上游可重试的一次抖动"变成"整题失败"。
    """

    class _NonJsonResponse(_FakeResponse):
        def __init__(self) -> None:
            super().__init__(200, text="<html>502 Bad Gateway</html>")

        def json(self) -> dict:  # type: ignore[override]
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    class _BadJson(_FakeAsyncClient):
        async def post(self, *args: Any, **kwargs: Any):
            return _NonJsonResponse()

    monkeypatch.setattr(llm_client.httpx, "AsyncClient", _BadJson)
    with pytest.raises(BizError) as excinfo:
        await _client().chat([{"role": "user", "content": "hi"}], purpose="gen_sql")

    assert excinfo.value.error_code == "E_LLM", "必须是 E_LLM（可重试语义），不能是 E_INTERNAL"
    assert "非 JSON" in excinfo.value.message
    assert excinfo.value.data["provider"] == "local"
    assert "502 Bad Gateway" in excinfo.value.data["body_head"]
