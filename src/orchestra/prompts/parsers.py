import json
import re
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from orchestra.ir.artifacts import PAYLOAD_SCHEMAS
from orchestra.schemas.artifacts import CodeArtifact

_CODE_BLOCK = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_JSON_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _is_list_annotation(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is list:
        return True
    if origin is Union:
        return any(_is_list_annotation(arg) for arg in get_args(annotation))
    return False


def _string_to_list(value: str) -> list[str]:
    text = value.strip()
    if not text:
        return []
    if "\n" in text:
        items = []
        for line in text.splitlines():
            cleaned = line.strip().lstrip("-•*").strip()
            if cleaned:
                items.append(cleaned)
        return items or [text]
    return [text]


def _normalize_payload(schema: type[BaseModel], data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    for name, field in schema.model_fields.items():
        if name not in normalized:
            continue
        value = normalized[name]
        if _is_list_annotation(field.annotation) and isinstance(value, str):
            normalized[name] = _string_to_list(value)
    return normalized


def _extract_json_object(text: str) -> dict[str, Any]:
    fenced = _JSON_FENCE.findall(text)
    candidates = [*fenced, text]
    last_error: Exception | None = None
    for candidate in candidates:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            payload = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(payload, dict):
            return payload
    if last_error is not None:
        raise ValueError(f"Invalid JSON object: {last_error}") from last_error
    raise ValueError("No JSON object found")


def parse_output(parser_id: str, text: str, output_schema: str, source_node: str) -> BaseModel:
    schema = PAYLOAD_SCHEMAS[output_schema]
    if parser_id == "python_code":
        blocks = _CODE_BLOCK.findall(text)
        code = blocks[-1].strip() if blocks else text.strip()
        return CodeArtifact(code=code, source_node=source_node)
    if parser_id == "final_answer":
        from orchestra.schemas.artifacts import FinalAnswerArtifact

        answer = text.strip().splitlines()[0].strip() if text.strip() else ""
        status = "ok" if answer else "empty"
        return FinalAnswerArtifact(
            answer=answer,
            raw_output=text,
            source_node=source_node,
            extraction_status=status,
        )
    payload = _normalize_payload(schema, _extract_json_object(text))
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"ValidationError: {exc}") from exc
