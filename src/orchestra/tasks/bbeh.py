"""Minimal BBEH task adapter (single-task, no decomposition)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from orchestra.ir.artifacts import ArtifactEnvelope, create_artifact
from orchestra.schemas.artifacts import ProblemArtifact
from orchestra.tasks.base import TaskEvaluationResult


class BBEHEvaluator:
    """Port of the EvoMAS BBEH fuzzy matcher (no reference leakage to agents)."""

    @staticmethod
    def strip_latex(response: str) -> str:
        if response.startswith("$") and response.endswith("$"):
            response = response[1:-1]
        if "boxed{" in response and response.endswith("}"):
            response = response[0:-1].split("boxed{")[1]
        if "text{" in response and response.endswith("}"):
            response = response[0:-1].split("text{")[1]
        if "texttt{" in response and response.endswith("}"):
            response = response[0:-1].split("texttt{")[1]
        return response

    @staticmethod
    def extract_answer(sample: str) -> str:
        answer_prefixes = [
            "The answer is:",
            "The final answer is ",
            "The final answer is: ",
            "The answer is ",
        ]
        answer = sample
        for answer_prefix in answer_prefixes:
            if answer_prefix in answer:
                answer = answer.split(answer_prefix)[-1].strip()
        if answer.endswith("."):
            answer = answer[:-1]
        return BBEHEvaluator.strip_latex(answer)

    @staticmethod
    def fuzzy_match(prediction: str, reference: str) -> bool:
        if prediction == reference:
            return True
        if len(prediction) == 3 and prediction[0] == "(" and prediction[-1] == ")":
            return prediction[1] == reference
        if len(reference) == 3 and reference[0] == "(" and reference[-1] == ")":
            return reference[1] == prediction
        try:
            if float(prediction) == float(reference):
                return True
        except ValueError:
            pass
        if prediction.replace("'", "") == reference.replace("'", ""):
            return True
        if f"[{reference}]" == prediction or f"[{prediction}]" == reference:
            return True
        if prediction.endswith("?") and prediction[:-1] == reference:
            return True
        return False

    @classmethod
    def preprocess_sample(cls, sample: str) -> str:
        prediction = cls.extract_answer(sample.strip()).lower()
        prediction = prediction.replace(", ", ",").replace("**", "")
        prediction = prediction.split("\n")[0]
        prediction = prediction[0:-1] if prediction.endswith(".") else prediction
        return prediction

    @staticmethod
    def preprocess_reference(reference: str) -> str:
        reference = reference.strip().lower()
        return reference.replace(", ", ",")

    @classmethod
    def evaluate_correctness(cls, sample: str, reference: str) -> bool:
        return cls.fuzzy_match(
            cls.preprocess_sample(sample), cls.preprocess_reference(reference)
        )


class BBEHTaskAdapter:
    task_type = "bbeh"

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self._index: dict[str, dict[str, Any]] | None = None

    def _load_index(self) -> dict[str, dict[str, Any]]:
        if self._index is not None:
            return self._index
        index: dict[str, dict[str, Any]] = {}
        # Support either flat jsonl or EvoMAS-style subset folders.
        jsonl = self.data_dir / "instances.jsonl"
        if jsonl.exists():
            for line in jsonl.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                task_id = str(item.get("task_id") or item.get("id"))
                index[task_id] = item
        else:
            for path in sorted(self.data_dir.glob("**/test.json")):
                subset = path.parent.name
                payload = json.loads(path.read_text(encoding="utf-8"))
                for item in payload:
                    local_id = str(item["id"])
                    task_id = f"{subset}:{local_id}"
                    index[task_id] = {
                        **item,
                        "task_id": task_id,
                        "subset": subset,
                    }
                    # Also allow bare numeric ids within a single-subset dir.
                    index.setdefault(local_id, index[task_id])
        self._index = index
        return index

    def list_task_ids(self) -> list[str]:
        return sorted(k for k in self._load_index() if ":" in k or k.startswith("bbeh_"))

    def load_instance(self, task_id: str) -> dict[str, Any]:
        index = self._load_index()
        if task_id not in index:
            raise KeyError(f"Unknown BBEH task id: {task_id}")
        return dict(index[task_id])

    def build_problem_artifact(self, instance: dict[str, Any]) -> ArtifactEnvelope:
        task_id = str(instance.get("task_id") or instance["id"])
        # Reference answer intentionally omitted from agent-visible artifact.
        problem = ProblemArtifact(
            question_id=task_id,
            title=str(instance.get("subset") or instance.get("source") or "bbeh"),
            statement=str(instance["query"]),
            difficulty=str(instance.get("difficulty") or "unknown"),
            platform="bbeh",
        )
        return create_artifact(
            problem, producer_node_id="__input__", task_id=task_id
        )

    def build_instruction(self, instance: dict[str, Any]) -> str:
        return (
            "Solve the following problem. Use tools when helpful. "
            "Return only the final answer via final_answer.\n\n"
            f"{instance['query']}"
        )

    def extract_final_answer(self, final_artifact: ArtifactEnvelope | None) -> str | None:
        if final_artifact is None:
            return None
        payload = final_artifact.payload
        if "answer" in payload:
            return str(payload["answer"])
        return None

    def evaluate(
        self,
        *,
        instance: dict[str, Any],
        final_artifact: ArtifactEnvelope | None,
        execution_success: bool,
    ) -> TaskEvaluationResult:
        task_id = str(instance.get("task_id") or instance["id"])
        reference = str(instance.get("gt") or instance.get("reference") or "")
        extracted = self.extract_final_answer(final_artifact)
        answer_correct = None
        if execution_success and extracted is not None and reference:
            answer_correct = BBEHEvaluator.evaluate_correctness(extracted, reference)
        return TaskEvaluationResult(
            task_id=task_id,
            execution_success=execution_success,
            answer_correct=answer_correct,
            extracted_answer=extracted,
            reference_answer=reference,
            details={
                "subset": instance.get("subset") or instance.get("source"),
                "extraction_status": (
                    None
                    if final_artifact is None
                    else final_artifact.payload.get("extraction_status")
                ),
            },
        )


_DIFFICULTY_HINT = re.compile(r"easy|medium|hard", re.I)
