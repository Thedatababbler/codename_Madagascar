import asyncio

from pydantic import BaseModel

from orchestra.adapters.livecodebench.loader import PrivateTestRepository
from orchestra.runtime.state import RuntimeState
from orchestra.sandbox.lcb_official import FinalLCBWorker
from orchestra.sandbox.lcb_protocol import FinalEvaluationStatus


class FinalEvaluation(BaseModel):
    schema_version: str = "1.0"
    question_id: str
    passed: bool
    status: FinalEvaluationStatus
    evaluator_commit: str
    release_version: str = "release_v6"
    pass_at_1: float
    per_test_timeout_seconds: float
    max_worker_wall_seconds: float
    infra_retries: int = 0


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
        max_infra_retries: int = 1,
    ) -> None:
        self.private_repository = private_repository
        self.evaluator_commit = evaluator_commit
        self.worker = worker
        self.sandbox_semaphore = sandbox_semaphore
        self.per_test_timeout_seconds = per_test_timeout_seconds
        self.max_infra_retries = max_infra_retries

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
        retries = 0
        while True:
            async with self.sandbox_semaphore:
                result = await self.worker.evaluate(
                    question_id=lcb_problem_ref,
                    hidden=hidden,
                    code=final_code,
                    per_test_timeout_seconds=self.per_test_timeout_seconds,
                )
            if result.status is not FinalEvaluationStatus.INFRA_ERROR:
                break
            if retries >= self.max_infra_retries:
                break
            retries += 1
        return FinalEvaluation(
            question_id=lcb_problem_ref,
            passed=result.status is FinalEvaluationStatus.PASSED,
            status=result.status,
            evaluator_commit=self.evaluator_commit,
            pass_at_1=result.pass_at_1
            if result.status is FinalEvaluationStatus.PASSED
            else 0.0,
            per_test_timeout_seconds=self.per_test_timeout_seconds,
            max_worker_wall_seconds=self.worker.runner.max_worker_wall_seconds,
            infra_retries=retries,
        )
