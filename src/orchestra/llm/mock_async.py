import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Callable

from orchestra.llm.base_async import AsyncLLMClient
from orchestra.llm.usage import LLMResponse, LLMUsage


class MockAsyncLLMClient(AsyncLLMClient):
    def __init__(
        self,
        responses: dict[str, list[str] | Callable[[dict[str, str]], str]],
        *,
        delay_seconds: float = 0.0,
    ) -> None:
        self.responses = {
            key: value if callable(value) else deque(value)
            for key, value in responses.items()
        }
        self.delay_seconds = delay_seconds
        self.calls: list[dict] = []
        self.active_calls = 0
        self.max_active_calls = 0
        self.call_counts = defaultdict(int)

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
        contract_id = metadata.get("contract_id", "default")
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            source = self.responses.get(contract_id) or self.responses.get("default")
            if source is None:
                raise RuntimeError(f"No mock response for contract {contract_id}")
            text = source(metadata) if callable(source) else source.popleft()
            self.call_counts[contract_id] += 1
            self.calls.append(
                {"contract_id": contract_id, "messages": messages, "metadata": metadata}
            )
            prompt_tokens = sum(len(item["content"]) for item in messages) // 4
            completion_tokens = len(text) // 4
            return LLMResponse(
                text=text,
                model=model,
                latency_ms=int((time.perf_counter() - started) * 1000),
                usage=LLMUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                ),
                provider_request_id="mock",
            )
        finally:
            self.active_calls -= 1
