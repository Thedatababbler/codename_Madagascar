import json
import re

from pydantic import BaseModel

from orchestra.ir.artifacts import PAYLOAD_SCHEMAS
from orchestra.schemas.artifacts import CodeArtifact

_CODE_BLOCK = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def parse_output(parser_id: str, text: str, output_schema: str, source_node: str) -> BaseModel:
    schema = PAYLOAD_SCHEMAS[output_schema]
    if parser_id == "python_code":
        blocks = _CODE_BLOCK.findall(text)
        code = blocks[-1].strip() if blocks else text.strip()
        return CodeArtifact(code=code, source_node=source_node)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("No JSON object found")
    return schema.model_validate(json.loads(text[start : end + 1]))
