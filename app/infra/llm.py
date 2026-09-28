# -*- coding: utf-8 -*-
"""大模型客户端（模块 00 的补全件，模块 06 使用）：OpenAI 兼容接口 + 流式。

## 为什么用 `httpx` 手写而不是装 `openai` SDK

| 维度 | 手写（本文件） | `openai` SDK |
|---|---|---|
| 依赖 | **已有**（httpx 是 FastAPI 测试依赖，早已在 `requirements.txt`） | 需要新增依赖 |
| 我们要的 | 只有两个端点（chat/completions 的流式与非流式） | 一大套（embeddings / assistants / files…） |
| 项目规则 | "不引入未声明依赖" | 违反 |

`openai` SDK 本质上就是对 `POST {base}/chat/completions` 的封装，
而 DashScope 的兼容模式与 OpenAI 的**请求/响应格式一致**，所以手写成本很低。

## 流式的两个关键细节

1. **SSE 分帧必须按 `\n\n` 切**，不能按行切：`data:` 只是一帧里的一行，
   一帧可能包含多行（`event:` / `data:` / `id:`）。按行切会把一帧拆成几段，
   表现为"回答里偶尔多出半句乱码"。
2. **`stream_options: {"include_usage": true}`**：不加这个，流式响应里**没有
   `usage` 字段** —— 而 `qa_logs.token_usage` 是模块 06 的验收项（AC-06-06）。
   拿不到就只能在应用层估算 token 数，那是另一个不准的数字。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Sequence

from app.core.config import settings
from app.core.logging import logger


class LLMUnavailable(RuntimeError):
    """大模型不可用（未配置 / 网络失败 / 鉴权失败 / 限流）。"""


@dataclass(slots=True)
class TokenUsage:
    """Token 用量（`qa_logs.token_usage` 的口径）。"""

    prompt: int = 0
    completion: int = 0
    total: int = 0

    @property
    def empty(self) -> bool:
        return self.total == 0

    def as_dict(self) -> dict[str, int]:
        return {"prompt": self.prompt, "completion": self.completion,
                "total": self.total}


@dataclass(slots=True)
class ChatChunk:
    """流式返回的一段（文本 + 可能的用量）。"""

    text: str = ""
    usage: TokenUsage | None = None
    finish_reason: str | None = None


@dataclass(slots=True)
class ChatResult:
    """非流式返回（查询改写、意图识别用）。"""

    text: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)


def _usage_of(payload: dict[str, Any] | None) -> TokenUsage | None:
    """从响应里取 `usage`（没有返回 `None`，**不编造 0**）。

    编造 0 与"真的 0 token"无法区分，而看板上"这一轮消耗为 0"是个明显的异常信号。
    """
    if not payload:
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    return TokenUsage(prompt=int(usage.get("prompt_tokens") or 0),
                      completion=int(usage.get("completion_tokens") or 0),
                      total=int(usage.get("total_tokens") or 0))


class LLMClient:
    """进程级单例：一个 `httpx.AsyncClient` 复用连接池。"""

    def __init__(self) -> None:
        self._client: Any = None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> Any:
        """惰性建 `httpx.AsyncClient`（连接池复用；超时按整体预算给）。"""
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                base_url=settings.openai_api_base.rstrip("/"),
                headers={"Authorization": f"Bearer {settings.openai_api_key}",
                         "Content-Type": "application/json"},
                timeout=httpx.Timeout(settings.llm_timeout_seconds,
                                      connect=10.0))
        return self._client

    @staticmethod
    def require_configured() -> None:
        """没配密钥就**明确失败**，不要发出一个必然 401 的请求。

        提前失败的价值在于错误信息："未配置 OPENAI_API_KEY"能直接指向 `.env`；
        而 401 的错误信息是"Invalid API key"，会让人去怀疑密钥本身是不是过期了。
        """
        if not settings.llm_configured:
            raise LLMUnavailable("未配置大模型（OPENAI_API_KEY / OPENAI_API_BASE）")

    def _body(self, messages: Sequence[dict[str, str]], *, stream: bool,
              temperature: float | None, max_tokens: int | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": list(messages),
            "temperature": (settings.llm_temperature if temperature is None
                            else temperature),
            "max_tokens": max_tokens or settings.llm_max_tokens,
            "stream": stream,
        }
        if stream:
            # 见模块头 ②：不加这个，流式响应里没有 usage
            body["stream_options"] = {"include_usage": True}
        return body

    # ------------------------------------------------------------------ 流式
    async def stream_chat(self, messages: Sequence[dict[str, str]], *,
                          temperature: float | None = None,
                          max_tokens: int | None = None) -> AsyncIterator[ChatChunk]:
        """流式对话，逐段产出 `ChatChunk`（纯文本 + 最后一段带 `usage`）。"""
        self.require_configured()
        body = self._body(messages, stream=True, temperature=temperature,
                          max_tokens=max_tokens)
        try:
            async with self._http().stream("POST", "/chat/completions",
                                           json=body) as response:
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", "replace")
                    raise LLMUnavailable(
                        f"大模型返回 {response.status_code}：{detail[:300]}")
                async for chunk in self._iter_sse_text(response):
                    yield chunk
        except LLMUnavailable:
            raise
        except Exception as exc:                            # noqa: BLE001
            raise LLMUnavailable(f"大模型调用失败：{type(exc).__name__}: {exc}") from exc

    @staticmethod
    async def _iter_sse_text(response: Any) -> AsyncIterator[ChatChunk]:
        """把 SSE 字节流拆成 `ChatChunk`。

        按 `\\n\\n`（帧分隔）切，见模块头 ①。`[DONE]` 是 OpenAI 兼容协议的结束标记，
        收到它就该结束——而不是等 TCP 关闭（服务端可能保持长连接一段时间）。
        """
        buffer = ""
        async for raw in response.aiter_bytes():
            buffer += raw.decode("utf-8", "replace")
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                for line in frame.splitlines():
                    if not line.startswith("data:"):
                        continue
                    payload_text = line[5:].strip()
                    if payload_text == "[DONE]":
                        return
                    try:
                        payload = json.loads(payload_text)
                    except json.JSONDecodeError:
                        logger.warning("大模型流式帧不是合法 JSON，已跳过：%s",
                                       payload_text[:120])
                        continue
                    choices = payload.get("choices") or []
                    text = ""
                    finish = None
                    if choices:
                        delta = choices[0].get("delta") or {}
                        text = str(delta.get("content") or "")
                        finish = choices[0].get("finish_reason")
                    usage = _usage_of(payload)
                    if text or usage or finish:
                        yield ChatChunk(text=text, usage=usage, finish_reason=finish)

    # ------------------------------------------------------------------ 非流式
    async def chat(self, messages: Sequence[dict[str, str]], *,
                   temperature: float | None = None,
                   max_tokens: int | None = None) -> ChatResult:
        """非流式对话（查询改写这类"短输入短输出"的场景用它，省掉流式开销）。"""
        self.require_configured()
        body = self._body(messages, stream=False, temperature=temperature,
                          max_tokens=max_tokens)
        try:
            response = await self._http().post("/chat/completions", json=body)
        except Exception as exc:                            # noqa: BLE001
            raise LLMUnavailable(f"大模型调用失败：{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise LLMUnavailable(
                f"大模型返回 {response.status_code}：{response.text[:300]}")
        payload = response.json()
        choices = payload.get("choices") or []
        text = ""
        if choices:
            text = str((choices[0].get("message") or {}).get("content") or "")
        return ChatResult(text=text, usage=_usage_of(payload) or TokenUsage())

    def health(self) -> dict[str, Any]:
        """`/health` 用：**只报"配没配"与模型名，绝不回传密钥**。"""
        return {"configured": settings.llm_configured, "model": settings.llm_model,
                "base": settings.openai_api_base}


llm_client = LLMClient()

__all__ = ["LLMClient", "LLMUnavailable", "ChatChunk", "ChatResult", "TokenUsage",
           "llm_client"]
