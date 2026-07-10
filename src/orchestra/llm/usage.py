from pydantic import BaseModel


class LLMUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float | None = None


class LLMResponse(BaseModel):
    text: str
    model: str
    latency_ms: int
    usage: LLMUsage
    provider_request_id: str | None = None

    @property
    def raw_response_id(self) -> str | None:
        return self.provider_request_id
