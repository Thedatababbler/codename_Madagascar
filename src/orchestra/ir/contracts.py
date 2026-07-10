import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class AgentContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_id: str
    role: str
    system_prompt_template: str
    user_prompt_template: str
    model: str
    temperature: float
    max_tokens: int
    timeout_seconds: float = 120
    allowed_tools: list[str] = Field(default_factory=list)
    input_schema: str
    output_schema: str
    parser_id: str
    memory_scope: Literal["local", "task"] = "local"


_ENV = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]+))?\}")


def _expand(value):
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.getenv(m.group(1), m.group(2) or m.group(0)), value)
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    return value


def load_contract(path: str | Path) -> AgentContract:
    return AgentContract.model_validate(
        _expand(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    )


def load_contracts(directory: str | Path) -> dict[str, AgentContract]:
    contracts = {}
    for path in sorted(Path(directory).glob("*.yaml")):
        contract = load_contract(path)
        if contract.contract_id in contracts:
            raise ValueError(f"Duplicate contract ID: {contract.contract_id}")
        contracts[contract.contract_id] = contract
    return contracts
