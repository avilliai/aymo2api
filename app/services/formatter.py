"""Convert upstream stream lines into OpenAI-compatible responses.

The Aymo client (``aymo_client.stream_chat``) normalises aymo.ai's
``event: chunk`` SSE into plain OpenAI ``data: {...}`` chunk lines carrying
``choices[0].delta.content`` plus a trailing ``finishReason``/``usage``
metadata line.  This formatter therefore only needs to understand the
OpenAI-style ``data:`` protocol, but the generic Vercel data-stream parsing
is kept so the same helpers can be reused for other upstreams.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncGenerator


def _make_chunk_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def _parse_protocol_line(raw_line: str) -> tuple[str | None, dict[str, Any] | None]:
    line = raw_line.strip()
    if not line:
        return None, None

    if line.startswith("data:"):
        payload = line[5:].strip()
        if payload in {"", "[DONE]"}:
            return None, None
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            return payload, None
        if isinstance(value, str):
            return value, None
        if isinstance(value, dict):
            choices = value.get("choices") or []
            if choices:
                delta = (choices[0].get("delta") or {}).get("content")
                if isinstance(delta, str):
                    return delta, value
            for key in ("content", "text", "delta"):
                if isinstance(value.get(key), str):
                    return value[key], value
            return None, value

    if ":" not in line:
        return line, None

    code, payload = line.split(":", 1)
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        value = payload

    # Vercel AI SDK data stream protocol:
    # 0=text delta, 3=error, f=message id, e/d=finish metadata.
    if code == "0":
        if isinstance(value, str):
            return value, None
        if isinstance(value, dict):
            for key in ("text", "content", "delta"):
                if isinstance(value.get(key), str):
                    return value[key], value
                if isinstance(value.get(key), dict) and isinstance(value[key].get("text"), str):
                    return value[key]["text"], value
        return str(value), None
    if code == "3":
        if isinstance(value, dict):
            message = value.get("message") or value.get("error") or value
        else:
            message = value
        raise RuntimeError(str(message))
    if code in {"e", "d", "f"}:
        return None, value if isinstance(value, dict) else None

    if isinstance(value, dict):
        for key in ("text", "content", "delta"):
            if isinstance(value.get(key), str) and value.get("type") in {
                None,
                "text",
                "text-delta",
            }:
                return value[key], value
    return None, value if isinstance(value, dict) else None


def _parse_aymo_line(raw_line: str) -> str | None:
    text, _ = _parse_protocol_line(raw_line)
    return text


# Backward-compatible name used by the Anthropic formatter copied from arting2api.
_parse_arting_line = _parse_aymo_line
_parse_notto_line = _parse_aymo_line


async def to_openai_stream(
    upstream_gen: AsyncGenerator[str, None],
    model: str,
    chunk_id: str | None = None,
) -> AsyncGenerator[str, None]:
    chunk_id = chunk_id or _make_chunk_id()
    created = int(time.time())
    role_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\n"

    finish_reason = "stop"
    usage: dict[str, Any] | None = None
    async for raw_line in upstream_gen:
        delta_text, metadata = _parse_protocol_line(raw_line)
        if metadata:
            finish_reason = str(metadata.get("finishReason") or finish_reason)
            upstream_usage = metadata.get("usage")
            if isinstance(upstream_usage, dict):
                usage = upstream_usage
        if delta_text is None:
            continue
        chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {"index": 0, "delta": {"content": delta_text}, "finish_reason": None}
            ],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    stop_chunk: dict[str, Any] = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }
    if usage:
        stop_chunk["usage"] = {
            "prompt_tokens": int(usage.get("promptTokens") or 0),
            "completion_tokens": int(usage.get("completionTokens") or 0),
            "total_tokens": int(usage.get("promptTokens") or 0)
            + int(usage.get("completionTokens") or 0),
        }
    yield f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


async def to_openai_json(
    upstream_gen: AsyncGenerator[str, None],
    model: str,
) -> dict[str, Any]:
    parts: list[str] = []
    finish_reason = "stop"
    usage: dict[str, Any] = {}
    async for raw_line in upstream_gen:
        delta, metadata = _parse_protocol_line(raw_line)
        if delta is not None:
            parts.append(delta)
        if metadata:
            finish_reason = str(metadata.get("finishReason") or finish_reason)
            if isinstance(metadata.get("usage"), dict):
                usage = metadata["usage"]

    full_content = "".join(parts)
    prompt_tokens = int(usage.get("promptTokens") or 0)
    completion_tokens = int(usage.get("completionTokens") or max(1, len(full_content) // 4))
    return {
        "id": _make_chunk_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": full_content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
