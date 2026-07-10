import asyncio

from pydantic import BaseModel

from orchestra.adapters.livecodebench.loader import PrivateTestRepository
from orchestra.runtime.state import RuntimeState
from orchestra.sandbox.lcb_official import FinalLCBWorker


class FinalEvaluation(BaseModel):
    schema_version: str = "1.0"
    question_id: str
    passed: bool
    evaluator_commit: str
    release_version: str = "release_v6"
    pass_at_1: float
    per_test_timeout_seconds: float
    max_worker_wall_seconds: float


class FinalLCBEvaluator:
    """Freeze-gated adapter around the isolated private-final worker."""

    def __init__(
        self,
        *,
        private_repository: PrivateTestRepository,
        evaluator_commit: str,
        worker: FinalLCBWorker,
        sandbox_semaphore: asyncio.Semaphore,
        per_test_timeout_seconds: float = 6,
    ) -> None:
        self.private_repository = private_repository
        self.evaluator_commit = evaluator_commit
        self.worker = worker
        self.sandbox_semaphore = sandbox_semaphore
        self.per_test_timeout_seconds = per_test_timeout_seconds

    async def evaluate_frozen_run(
        self,
        *,
        runtime_state: RuntimeState,
        final_code: str,
        lcb_problem_ref: str,
    ) -> FinalEvaluation:
        if not runtime_state.frozen or runtime_state.final_output_artifact_id is None:
            raise RuntimeError("Private evaluation requires FINAL_OUTPUT_FROZEN")
        hidden = self.private_repository.get_for_final_evaluation(lcb_problem_ref)
        async with self.sandbox_semaphore:
            result = await self.worker.evaluate(
                question_id=lcb_problem_ref,
                hidden=hidden,
                code=final_code,
                per_test_timeout_seconds=self.per_test_timeout_seconds,
            )
        return FinalEvaluation(
            question_id=lcb_problem_ref,
            passed=result.passed,
            evaluator_commit=self.evaluator_commit,
            pass_at_1=result.pass_at_1,
            per_test_timeout_seconds=self.per_test_timeout_seconds,
            max_worker_wall_seconds=self.worker.runner.max_worker_wall_seconds,
        )
