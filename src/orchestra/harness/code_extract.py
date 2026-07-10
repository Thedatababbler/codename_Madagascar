import re

_PYTHON_BLOCK = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_code(text: str) -> str:
    matches = _PYTHON_BLOCK.findall(text)
    if matches:
        return matches[-1].strip()
    stripped = text.strip()
    if stripped.startswith("```") or stripped.endswith("```"):
        raise ValueError("Malformed code block")
    return stripped
