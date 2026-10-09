"""Confine, then exec: ``python -m orchestra.sandbox.exec --policy <json> -- <command...>``.

Kept free of orchestra imports beyond ``landlock`` so the launcher itself reads
nothing it will not be allowed to read afterwards.
"""

from __future__ import annotations

import json
import os
import sys

from orchestra.sandbox.landlock import Policy, apply


def main(argv: list[str]) -> int:
    if "--" not in argv or argv[:1] != ["--policy"]:
        print("usage: python -m orchestra.sandbox.exec --policy <file> -- <command...>", file=sys.stderr)
        return 2
    policy_path = argv[1]
    cmd = argv[argv.index("--") + 1:]
    with open(policy_path, encoding="utf-8") as fh:
        policy = Policy.from_dict(json.load(fh))
    apply(policy)
    # the launcher's own import path is not the confined command's
    orig = os.environ.pop("ADAMAS_SANDBOX_ORIG_PYTHONPATH", None)
    if orig is not None:
        if orig:
            os.environ["PYTHONPATH"] = orig
        else:
            os.environ.pop("PYTHONPATH", None)
    os.execvp(cmd[0], cmd)
    return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
