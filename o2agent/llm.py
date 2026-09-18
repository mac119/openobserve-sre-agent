"""Pluggable LLM provider abstraction.

The agent talks to `LLMProvider`, never to a concrete SDK. To add ChatGPT,
Anthropic-native, etc., implement a new provider and register it in `build()`.

The message/tool-call format follows the OpenAI chat.completions convention,
which the agent loop treats as the canonical internal shape.
"""
from __future__ import annotations

import time
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

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60,
                 fallback_models: tuple[str, ...] = ()):
        self._base = base_url.rstrip("/")
        self._model = model
        self._fallbacks = tuple(fallback_models)
        self._http = httpx.Client(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def _post_once(self, payload: dict) -> LLMResponse:
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

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        model: str | None = None,
    ) -> LLMResponse:
        base_payload: dict[str, Any] = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            base_payload["tools"] = tools
            base_payload["tool_choice"] = "auto"

        # Try the requested/primary model first, then each fallback in order.
        # This survives per-model rate limits (429) and transient 5xx: when a
        # model is rate-limited, switch to the next instead of failing the turn.
        primary = model or self._model
        candidates: list[str] = [primary]
        for m in self._fallbacks:
            if m and m not in candidates:
                candidates.append(m)

        last_exc: Exception | None = None
        for idx, cand in enumerate(candidates):
            payload = dict(base_payload, model=cand)
            # brief retry with backoff on 429/5xx for THIS model before moving on
            for attempt in range(2):
                try:
                    return self._post_once(payload)
                except httpx.HTTPStatusError as e:
                    status = e.response.status_code if e.response is not None else None
                    last_exc = e
                    if status == 429 or (status is not None and 500 <= status < 600):
                        # transient: back off once, then fall through to next model
                        if attempt == 0:
                            time.sleep(0.8)
                            continue
                        break  # give up on this model, try the next candidate
                    raise  # non-retryable (4xx other than 429): surface immediately
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                    last_exc = e
                    if attempt == 0:
                        time.sleep(0.8)
                        continue
                    break
        # all candidates exhausted
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("LLM chat failed with no response and no exception")

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
            fallback_models=getattr(settings, "llm_fallback_models", ()),
        )
    raise RuntimeError(f"unknown LLM_PROVIDER: {provider!r}")
