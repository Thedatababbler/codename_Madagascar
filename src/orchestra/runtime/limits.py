import asyncio

from pydantic import BaseModel


class RuntimeLimits(BaseModel):
    max_parallel_benchmark_tasks: int = 4
    max_parallel_nodes_per_task: int = 4
    max_parallel_llm_calls: int = 8
    max_parallel_sandboxes: int = 2


class RuntimeSemaphores:
    def __init__(self, limits: RuntimeLimits) -> None:
        self.task = asyncio.Semaphore(limits.max_parallel_benchmark_tasks)
        self.llm = asyncio.Semaphore(limits.max_parallel_llm_calls)
        self.sandbox = asyncio.Semaphore(limits.max_parallel_sandboxes)
