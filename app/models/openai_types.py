"""
OpenAI 兼容的请求 / 响应 Pydantic 模型。
"""

from typing import Any, Literal
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class ContentPartText(BaseModel):
    type: Literal["text"]
    text: str


class ContentPartImageUrl(BaseModel):
    type: Literal["image_url"]
    image_url: dict[str, str]


class ToolCallFunction(BaseModel):
    """The function portion of an OpenAI assistant tool call.

    Tool calls are returned by this adapter as a text protocol, but clients
    commonly send the previous assistant message back verbatim on the next
    turn.  Keeping the shape here prevents Pydantic from silently dropping
    that context.
    """

    name: str
    arguments: str | dict[str, Any] = ""


class ToolCall(BaseModel):
    id: str = ""
    type: str = "function"
    function: ToolCallFunction


class Message(BaseModel):
    role: Literal["system", "developer", "user", "assistant", "tool", "function"]
    content: str | list[ContentPartText | ContentPartImageUrl | dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None

    def to_text(self) -> str:
        """将 content（可能是字符串或块列表）统一转为纯文本。"""
        prefix: list[str] = []
        if isinstance(self.content, str):
            text = self.content
        elif isinstance(self.content, list):
            parts: list[str] = []
            for part in self.content:
                if isinstance(part, ContentPartText):
                    parts.append(part.text)
                elif isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text") or ""))
            text = "\n".join(parts)
        else:
            text = ""

        # Preserve assistant tool-call context for providers that send a
        # follow-up request containing the prior assistant turn.  The Aymo
        # adapter consumes this as ordinary text, which is preferable to
        # silently omitting the call altogether.
        for call in self.tool_calls or []:
            function = call.function
            args = function.arguments or "{}"
            if isinstance(args, dict):
                import json

                args = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
            prefix.append(f"[之前调用工具 {function.name}，参数: {args}]")
        if prefix:
            text = "\n".join(prefix + ([text] if text else []))
        return text


class ChatCompletionRequest(BaseModel):
    model: str = "gpt-5-nano"
    messages: list[Message]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    stop: str | list[str] | None = None
    user: str | None = None
    # 工具调用（透传字段，暂不执行）
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict | None = None
    enable_search: bool = False
    stream_options: dict[str, Any] | None = None

    def build_prompt(self) -> str:
        """
        将多轮消息拼成单条 text。
        规则：system 消息作为前缀；其余角色逐条拼接。
        """
        parts: list[str] = []
        for msg in self.messages:
            text = msg.to_text()
            if not text:
                continue
            if msg.role == "system":
                parts.insert(0, f"[系统指令]\n{text}\n")
            elif msg.role == "user":
                parts.append(f"User: {text}")
            elif msg.role == "assistant":
                parts.append(f"Assistant: {text}")
            else:
                parts.append(text)
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# 响应模型（供文档 / 类型检查用，实际返回 dict 更灵活）
# ---------------------------------------------------------------------------

class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChoiceMessage(BaseModel):
    role: str
    content: str


class Choice(BaseModel):
    index: int
    message: ChoiceMessage
    finish_reason: str | None = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: UsageInfo = Field(default_factory=UsageInfo)


# ---------------------------------------------------------------------------
# 模型列表响应
# ---------------------------------------------------------------------------

class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "aymo"


class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelInfo]
