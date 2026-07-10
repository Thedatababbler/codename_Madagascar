import asyncio
import os
import time
from typing import Any

import httpx

from orchestra.llm.base_async import AsyncLLMClient
from orchestra.llm.usage import LLMResponse, LLMUsage


class OpenAICompatibleAsyncClient(AsyncLLMClient):
    def __init__(
        self, *, base_url: str | None = None, api_key: str | None = None
    ) -> None:
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.base_url or not self.api_key:
            raise ValueError("OPENAI_BASE_URL and OPENAI_API_KEY are required")
        self.client = httpx.AsyncClient()

    async def _post(
        self, payload: dict[str, Any], timeout_seconds: float
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self.client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                    timeout=timeout_seconds,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                return response
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 429 and exc.response.status_code < 500:
                    raise
                last_error = exc
            if attempt < 2:
                await asyncio.sleep(2**attempt)
        assert last_error is not None
        raise last_error

    async def generate(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int,
        timeout_seconds: float,
        metadata: dict[str, str],
    ) -> LLMResponse:
        started = time.perf_counter()
        response = await self._post(
            {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout_seconds,
        )
        data = response.json()
        usage = data.get("usage") or {}
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        cached = int(
            (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
        )
        return LLMResponse(
            text=data["choices"][0]["message"].get("content") or "",
            model=model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            usage=LLMUsage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                cached_tokens=cached,
                total_tokens=int(usage.get("total_tokens", prompt + completion) or 0),
            ),
            provider_request_id=data.get("id"),
        )
