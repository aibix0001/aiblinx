"""Async wrapper over any OpenAI-compatible chat + embeddings endpoint.

Works with Ollama, vLLM, LocalAI, llama.cpp's server, or a hosted provider — the
endpoint and credentials come from ``LLM_BASE_URL`` / ``LLM_TOKEN``, so switching
providers is a config change, not a code change. A small semaphore caps in-flight
requests (throughput protection, e.g. not thrashing a single GPU); quota
backoff for hosted providers is the SDK's retry on 429/5xx (``LLM_MAX_RETRIES``).

Chat and embedding use separate client instances so the two can point at
different endpoints (``LLM_EMBED_BASE_URL``). When that var is unset, the embed
client falls back to ``LLM_BASE_URL``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import httpx
from openai import AsyncOpenAI

from ..config import Settings, get_settings


class LLMClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._sem = asyncio.Semaphore(self.settings.llm_concurrency)
        # Two clients so chat and embed can point at different endpoints
        # (e.g. a vLLM serving chat on one port, an embedding model on another).
        # The SDK retries 429 and 5xx with exponential backoff and honours
        # Retry-After, which is what hosted free tiers need.
        limits = {
            "timeout": self.settings.llm_timeout_s,
            "max_retries": self.settings.llm_max_retries,
        }
        self._chat_client = AsyncOpenAI(
            base_url=self.settings.llm_base_url,
            # SDK requires a non-empty key; a local Ollama ignores it, and hosted
            # endpoints 401 clearly if LLM_TOKEN was left unset.
            api_key=self.settings.llm_token or "unset",
            **limits,
        )
        self._embed_client = AsyncOpenAI(
            base_url=self.settings.llm_embed_base_url or self.settings.llm_base_url,
            api_key=self.settings.llm_token or "unset",
            **limits,
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        # NVIDIA NeMo Retriever embedders require input_type (+ truncate); plain
        # OpenAI / Ollama embedders reject them, so only send when configured.
        kwargs: dict[str, Any] = {}
        if self.settings.llm_embed_input_type:
            kwargs["extra_body"] = {
                "input_type": self.settings.llm_embed_input_type,
                "truncate": "END",
            }
        async with self._sem:
            try:
                resp = await self._embed_client.embeddings.create(
                    model=self.settings.llm_embed_model,
                    input=list(texts),
                    **kwargs,
                )
                return [item.embedding for item in resp.data]
            except AttributeError:
                # llama.cpp's native server returns a bare JSON array
                # [{ "index": 0, "embedding": [[...]] }] rather than OpenAI's
                # { "data": [{ "embedding": [...] }] }.  Fall back to a raw
                # HTTP call and unwrap the extra nesting layer.
                embed_url = str(self._embed_client.base_url) + "/embeddings"
                async with httpx.AsyncClient() as http:
                    raw = await http.post(
                        embed_url,
                        json={
                            "model": self.settings.llm_embed_model,
                            "input": list(texts),
                            **kwargs.get("extra_body", {}),
                        },
                    )
                    raw.raise_for_status()
                    items = raw.json()
                    return [item["embedding"][0] for item in items]

    async def chat(self, messages: list[dict[str, str]], **kwargs: object) -> str:
        async with self._sem:
            resp = await self._chat_client.chat.completions.create(
                model=self.settings.llm_chat_model,
                messages=messages,  # type: ignore[arg-type]
                **kwargs,
            )
        return resp.choices[0].message.content or ""

    async def aclose(self) -> None:
        await self._chat_client.close()
        await self._embed_client.close()
