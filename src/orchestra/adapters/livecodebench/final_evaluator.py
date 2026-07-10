import asyncio
import json
import sys
from pathlib import Path

from pydantic import BaseModel

from orchestra.adapters.livecodebench.loader import PrivateTestRepository
from orchestra.runtime.state import RuntimeState


class FinalEvaluation(BaseModel):
    schema_version: str = "1.0"
    question_id: str
    passed: bool
    evaluator_commit: str
    timeout_seconds: int
    release_version: str = "release_v6"
    pass_at_1: float = 0.0


class FinalLCBEvaluator:
    """Hidden evaluator with a narrow result surface: pass/fail only."""

    def __init__(
        self,
        *,
        repository_path: str | Path,
        private_repository: PrivateTestRepository,
        evaluator_commit: str,
        timeout_seconds: int = 10,
    ) -> None:
        repo = str(Path(repository_path).resolve())
        if repo not in sys.path:
            sys.path.insert(0, repo)
        from lcb_runner.evaluation.compute_code_generation_metrics import check_correctness

        self.check_correctness = check_correctness
        self.private_repository = private_repository
        self.evaluator_commit = evaluator_commit
        self.timeout_seconds = timeout_seconds

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
        tests = [*hidden.public_tests, *hidden.private_tests]
        sample = {
            "input_output": json.dumps(
                {
                    "inputs": [test.input for test in tests],
                    "outputs": [test.output for test in tests],
                    "fn_name": hidden.function_name,
                }
            )
        }
        results, _metadata = await asyncio.to_thread(
            self.check_correctness,
            sample,
            final_code,
            self.timeout_seconds,
            False,
        )
        passed = bool(results) and all(value is True or value == 1 for value in results)
        return FinalEvaluation(
            question_id=lcb_problem_ref,
            passed=passed,
            evaluator_commit=self.evaluator_commit,
            timeout_seconds=self.timeout_seconds,
            pass_at_1=float(passed),
        )
