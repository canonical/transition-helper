"""Provider-agnostic LLM client interface (LiteLLM behind it)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import LLMConfig


class LLMError(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    raw_arguments: str = ""
    parse_error: str | None = None


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens: int = 0
    raw: Any = None

    def assistant_message(self) -> dict:
        msg: dict = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.name,
                              "arguments": tc.raw_arguments or json.dumps(tc.arguments)}}
                for tc in self.tool_calls
            ]
        return msg


class LLMClient(Protocol):
    """messages/tools use the OpenAI chat format (as LiteLLM does)."""

    def chat(self, messages: list[dict], tools: list[dict],
             tool_choice: str | dict | None = None) -> LLMResponse: ...


def parse_arguments(raw: str | dict | None) -> tuple[dict, str | None]:
    if isinstance(raw, dict):
        return raw, None
    if not raw:
        return {}, None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        return {}, f"arguments are not valid JSON: {e}"
    if not isinstance(value, dict):
        return {}, "arguments must be a JSON object"
    return value, None


class LiteLLMClient:
    def __init__(self, cfg: LLMConfig, max_output_tokens: int = 8192):
        if not cfg.model:
            raise LLMError("llm.model is not set in the user config")
        self.cfg = cfg
        self.max_output_tokens = max_output_tokens

    def chat(self, messages: list[dict], tools: list[dict],
             tool_choice: str | dict | None = None) -> LLMResponse:
        try:
            import litellm
        except ImportError as e:
            raise LLMError("litellm is not installed") from e
        kwargs: dict = {"model": self.cfg.model, "messages": messages,
                        "max_tokens": self.max_output_tokens}
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
        if self.cfg.base_url:
            kwargs["api_base"] = self.cfg.base_url
        if self.cfg.api_key_env:
            key = self.cfg.api_key()
            if not key:
                raise LLMError(f"environment variable {self.cfg.api_key_env} is not set")
            kwargs["api_key"] = key
        try:
            resp = litellm.completion(**kwargs)
        except Exception as e:  # litellm maps provider errors to many exception types
            raise LLMError(f"{type(e).__name__}: {e}") from e
        choice = resp.choices[0].message
        calls = []
        for tc in getattr(choice, "tool_calls", None) or []:
            args, err = parse_arguments(tc.function.arguments)
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args,
                                  raw_arguments=tc.function.arguments
                                  if isinstance(tc.function.arguments, str)
                                  else json.dumps(tc.function.arguments),
                                  parse_error=err))
        usage = getattr(resp, "usage", None)
        tokens = int(getattr(usage, "total_tokens", 0) or 0) if usage else 0
        return LLMResponse(content=choice.content, tool_calls=calls, tokens=tokens, raw=resp)

    def ping(self) -> str:
        r = self.chat([{"role": "user", "content": "Reply with the single word: pong"}], [])
        return (r.content or "").strip()
