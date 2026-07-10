import json
from pathlib import Path


def build_official_record(question_id: str, code: str) -> dict:
    return {"question_id": question_id, "code_list": [code]}


def export_predictions(records: list[dict], path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
