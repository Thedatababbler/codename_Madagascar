from abc import ABC, abstractmethod

from orchestra.llm.usage import LLMResponse


class AsyncLLMClient(ABC):
    @abstractmethod
    async def generate(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int,
        timeout_seconds: float,
        metadata: dict[str, str],
    ) -> LLMResponse: ...
