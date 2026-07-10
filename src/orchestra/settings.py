import os
from pathlib import Path


def load_env_file(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:]
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    os.environ.setdefault("ANALYST_MODEL", "gpt-5-mini")
    os.environ.setdefault("CODER_MODEL", "gpt-5-mini")
    os.environ.setdefault("REPAIR_MODEL", "gpt-5-mini")
    os.environ.setdefault("LCB_DATA_DIR", "/root/data/livecodebench/code_generation_lite")
    os.environ.setdefault(
        "LCB_REPOSITORY_PATH",
        os.environ.get("LCB_REPO_PATH", "/root/projects/LiveCodeBench"),
    )
