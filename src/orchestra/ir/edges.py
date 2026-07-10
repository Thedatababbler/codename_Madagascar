from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class EdgeCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_field: str
    operator: Literal[
        "equals",
        "not_equals",
        "greater_than",
        "less_than",
        "is_true",
        "is_false",
    ]
    value: Any | None = None

    def evaluate(self, payload: dict[str, Any]) -> bool:
        current: Any = payload
        for part in self.source_field.split("."):
            if not isinstance(current, dict) or part not in current:
                return False
            current = current[part]
        if self.operator == "equals":
            return current == self.value
        if self.operator == "not_equals":
            return current != self.value
        if self.operator == "greater_than":
            return current > self.value
        if self.operator == "less_than":
            return current < self.value
        if self.operator == "is_true":
            return current is True
        return current is False


class EdgeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    edge_id: str
    source_node: str
    source_output: str
    destination_node: str
    destination_input: str
    payload_policy: Literal["full"] = "full"
    condition: EdgeCondition | None = None
