"""HTTP client for aymo.ai's temporary-chat streaming endpoint.

Chat generation is driven through the same route the web app uses for private
("temporary") chats:

    POST https://aymo.ai/api/organizations/{orgId}/messages/temporary

which accepts the full conversation as ``{content, modelId, history, ...}`` and
returns a ``text/event-stream`` of ``event: chunk`` / ``event: done`` events.
This adapter normalises those events into OpenAI-style ``data:`` chunk lines so
the shared formatter can render OpenAI/Anthropic responses.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncGenerator

from app.services.token_manager import APP_BASE_URL, TokenManager

logger = logging.getLogger(__name__)


class AymoUpstreamError(RuntimeError):
    """An upstream HTTP error carrying its status for retry decisions."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# Model ids exposed by https://backend.aymo.ai/api/model-list.  ``free`` marks
# models whose ``availableOnPlans`` includes the free tier (usable by pooled
# free accounts).  ``auto`` lets aymo pick a model.
_FREE = {
    "auto",
    "gpt-5-nano",
    "gemini-2.5-flash-lite",
    "gemma-4-31b-it",
    "tencent/hy3",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "mistralai/mistral-nemo",
    "qwen/qwen3-next-80b-a3b-thinking",
    "qwen/qwen3-coder-30b-a3b-instruct",
    "xiaomimimo/mimo-v2.5",
    "deepseek/deepseek-v4-flash",
}

MODEL_IDS = [
    "auto",
    "gpt-5-nano",
    "gpt-4o-mini",
    "gpt-5.4-nano",
    "gpt-5-mini-2025-08-07",
    "gpt-5.4-mini-2026-03-17",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-haiku-4-5",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemma-4-31b-it",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.1-pro-preview",
    "tencent/hy3",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct-fp8",
    "mistralai/mistral-nemo",
    "qwen/qwen3-next-80b-a3b-thinking",
    "qwen/qwen3-coder-30b-a3b-instruct",
    "qwen/qwen3-vl-235b-a22b-thinking",
    "qwen/qwen3.6-27b",
    "qwen/qwen3-coder-next",
    "qwen/qwen3.7-max",
    "deepseek/deepseek-r1-0528",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v3.2",
    "xiaomimimo/mimo-v2.5-pro",
    "xiaomimimo/mimo-v2.5",
    "moonshotai/kimi-k3",
    "moonshotai/kimi-k2.6",
    "moonshotai/kimi-k2.5",
    "minimax/minimax-m2.7",
    "minimax/minimax-m3",
    "zai-org/glm-5.2",
    "zai-org/glm-5.1",
    "zai-org/glm-4.7-flash",
    "sonar",
    "grok-4.3",
    "grok-4.5",
]

# Convenience aliases → canonical model ids.
MODEL_ALIASES = {
    "default": "gpt-5-nano",
    "gpt-4o": "gpt-4o-mini",
    "gpt-5-nano": "gpt-5-nano",
    "claude-sonnet": "claude-sonnet-5",
    "claude-opus": "claude-opus-4-8",
    "claude-haiku": "claude-haiku-4-5",
    "gemini-flash": "gemini-2.5-flash",
    "gemini-pro": "gemini-2.5-pro",
    "deepseek-chat": "deepseek/deepseek-v3.2",
    "deepseek-reasoner": "deepseek/deepseek-r1-0528",
    "deepseek-r1": "deepseek/deepseek-r1-0528",
    "kimi-k3": "moonshotai/kimi-k3",
    "grok": "grok-4.5",
    "glm": "zai-org/glm-5.2",
    "qwen-max": "qwen/qwen3.7-max",
}

AVAILABLE_MODELS = [
    {
        "id": model_id,
        "object": "model",
        "owned_by": "aymo",
        "free_tier": model_id in _FREE,
    }
    for model_id in MODEL_IDS
] + [
    {
        "id": alias,
        "object": "model",
        "owned_by": "aymo2api",
        "upstream_id": target,
        "free_tier": target in _FREE,
    }
    for alias, target in MODEL_ALIASES.items()
    if alias != "default"
]

DEFAULT_MODEL = os.getenv("AYMO_DEFAULT_MODEL", "gpt-5-nano")


def resolve_model(model: str) -> str:
    value = (model or "default").strip()
    if value in MODEL_ALIASES:
        return MODEL_ALIASES[value]
    if value in MODEL_IDS:
        return value
    # tolerate provider-prefixed or shortened ids
    for model_id in MODEL_IDS:
        if model_id == value or model_id.endswith(f"/{value}"):
            return model_id
    return DEFAULT_MODEL


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return "" if content is None else str(content)


def _prepare(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    """Split internal messages into (content, history) for the aymo endpoint.

    System/developer turns are folded into the outgoing prompt because the
    temporary endpoint has no dedicated system field.
    """
    system_parts: list[str] = []
    convo: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        text = _content_text(message.get("content"))
        if role in {"system", "developer"}:
            if text.strip():
                system_parts.append(text)
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        convo.append({"role": role, "content": text})

    if not convo:
        convo.append({"role": "user", "content": ""})

    content = convo[-1]["content"]
    history = convo[:-1]

    if system_parts:
        prefix = "\n\n".join(part for part in system_parts if part.strip())
        content = f"{prefix}\n\n{content}" if content else prefix
    return content, history


def _sse_data_line(delta: str) -> str:
    payload = {"choices": [{"delta": {"content": delta}}]}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n"


async def stream_chat(
    account: dict[str, Any],
    manager: TokenManager,
    messages: list[dict[str, Any]],
    model: str = DEFAULT_MODEL,
    files: list[str] | None = None,
    *,
    enable_search: bool = False,
) -> AsyncGenerator[str, None]:
    """Stream an aymo temporary-chat completion as OpenAI ``data:`` lines."""
    resolved_model = resolve_model(model)
    if files:
        logger.info("aymo temporary chat 暂不支持图片附件，已忽略 %d 张图片", len(files))

    include_reasoning = os.getenv("AYMO_INCLUDE_REASONING", "false").lower() in {
        "1", "true", "yes", "on",
    }

    try:
        session = manager.create_session(account)
    except Exception as exc:
        manager.restore_credit(account)
        manager.mark_request_error(account, exc)
        raise

    async with session:
        reservation_settled = False

        def refund_reservation() -> None:
            nonlocal reservation_settled
            if not reservation_settled:
                manager.restore_credit(account)
                reservation_settled = True

        try:
            content, history = _prepare(messages)
            body = {
                "content": content,
                "modelId": resolved_model,
                "history": history,
                "enableReasoning": bool(os.getenv("AYMO_ENABLE_REASONING", "false").lower() in {"1", "true", "yes", "on"}),
                "enableWebSearch": bool(enable_search),
            }

            response = None
            for attempt in range(2):
                if not await manager.ensure_session(account, session, force=attempt > 0):
                    raise AymoUpstreamError("aymo 会话失效且无法重新登录", 401)
                org_id = account.get("org_id") or await manager._fetch_org_id(account, session)
                if not org_id:
                    raise AymoUpstreamError("未能获取 organization id")

                url = f"{APP_BASE_URL}/api/organizations/{org_id}/messages/temporary"
                headers = manager.app_headers(
                    account, session, referer=f"{APP_BASE_URL}/chats", json_body=True
                )
                headers["accept"] = "text/event-stream"
                response = await session.post(
                    url,
                    headers=headers,
                    data=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                    stream=True,
                )
                if response.status_code != 401 or attempt:
                    break
                try:
                    response.close()
                except Exception:
                    pass

            if response is None:
                raise AymoUpstreamError("aymo 对话请求未返回响应")
            if response.status_code == 401:
                manager.invalidate_session(account, "aymo 会话返回 401")
                raise AymoUpstreamError("aymo 会话返回 401", 401)
            if response.status_code not in (200, 201):
                body_text = await response.acontent()
                raise AymoUpstreamError(
                    f"aymo 对话 HTTP {response.status_code}: "
                    f"{body_text.decode('utf-8', errors='replace')[:500]}",
                    response.status_code,
                )

            finish_reason = "stop"
            usage: dict[str, Any] | None = None
            event = "message"
            seen = 0

            async for raw in response.aiter_lines():
                line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
                if not line:
                    event = "message"
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                if event in {"error", "limit_exceeded"}:
                    message = data.get("message") if isinstance(data, dict) else str(data)
                    raise AymoUpstreamError(f"aymo 上游错误: {message}")

                if event == "done":
                    break

                # event == "chunk" (default)
                if not isinstance(data, dict):
                    continue
                delta = data.get("delta")
                if delta:
                    seen += 1
                    yield _sse_data_line(delta)
                reasoning = data.get("reasoningDelta")
                if reasoning and include_reasoning:
                    seen += 1
                    yield _sse_data_line(reasoning)
                if data.get("finishReason"):
                    finish_reason = str(data["finishReason"])
                if isinstance(data.get("usage"), dict):
                    usage = data["usage"]

            # trailing metadata line consumed by the formatter
            meta: dict[str, Any] = {"finishReason": finish_reason}
            if usage:
                meta["usage"] = usage
            yield f"data: {json.dumps(meta, ensure_ascii=False)}\n"

            if seen == 0 and not usage:
                raise AymoUpstreamError("aymo 对话响应为空")

            manager._merge_session_cookies(account, session)
            manager.commit_credit(account)
            reservation_settled = True
        except Exception as exc:
            refund_reservation()
            if isinstance(exc, AymoUpstreamError) and exc.status_code == 401:
                manager.invalidate_session(account, exc)
            manager.mark_request_error(account, exc)
            raise
        finally:
            if not reservation_settled:
                refund_reservation()
