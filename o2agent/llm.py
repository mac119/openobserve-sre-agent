"""Pluggable LLM provider abstraction.

The agent talks to `LLMProvider`, never to a concrete SDK. To add ChatGPT,
Anthropic-native, etc., implement a new provider and register it in `build()`.

The message/tool-call format follows the OpenAI chat.completions convention,
which the agent loop treats as the canonical internal shape.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings


@dataclass
class LLMResponse:
    """Normalized model response."""

    content: str | None
    tool_calls: list[dict] = field(default_factory=list)  # OpenAI-shaped
    finish_reason: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str | None = None
    raw: dict | None = None


class LLMProvider(ABC):
    """Provider interface. All providers speak OpenAI-shaped messages/tools."""

    @abstractmethod
    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        model: str | None = None,
    ) -> LLMResponse:
        ...


class OpenAICompatProvider(LLMProvider):
    """Works with any OpenAI-compatible /chat/completions endpoint
    (LiteLLM gateway, OpenAI, DashScope compat mode, etc.)."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60):
        self._base = base_url.rstrip("/")
        self._model = model
        self._http = httpx.Client(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        model: str | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": model or self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        resp = self._http.post(f"{self._base}/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        msg = choice.get("message", {})
        usage = data.get("usage") or {}
        return LLMResponse(
            content=msg.get("content"),
            tool_calls=msg.get("tool_calls") or [],
            finish_reason=choice.get("finish_reason"),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            model=data.get("model"),
            raw=data,
        )

    def close(self) -> None:
        self._http.close()


def build(settings: Settings) -> LLMProvider:
    """Factory: pick a provider by config. Extend here for new providers."""
    provider = (settings.llm_provider or "openai").lower()
    if provider in ("openai", "litellm", "dashscope", "openai-compat"):
        if not settings.llm_api_key or not settings.llm_base_url:
            raise RuntimeError("LLM_BASE_URL and LLM_API_KEY must be set to use the agent loop.")
        return OpenAICompatProvider(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
        )
    raise RuntimeError(f"unknown LLM_PROVIDER: {provider!r}")
