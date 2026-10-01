"""The gate's imports stage configures Django settings when Django is installed (and only then).

djangorestframework-simplejwt reads ``settings.SIMPLE_JWT`` and
``AUTH_USER_MODEL`` while its modules import; the dataset reference does
the same, so a bare interpreter could never import it and the imports stage
graded a reference-faithful implementation as broken (EXP-20261001-01,
simplejwt). The helper is emitted into the check script and is a no-op
without Django.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from orchestra.codeprojecteval import harness as harness_module


def _helper_source() -> str:
    src = Path(harness_module.__file__).read_text(encoding="utf-8")
    start = src.index("def _configure_django_if_present(root):")
    end = src.index("def _check_cross_imports(root, packages):")
    return src[start:end]


def test_helper_is_called_before_the_imports_stage() -> None:
    src = Path(harness_module.__file__).read_text(encoding="utf-8")
    call = src.index("    _configure_django_if_present(root)\n")
    assert call < src.index("    for mod in modules:\n        try:\n            importlib.import_module(mod)")
    assert "find_spec(\"django\")" in _helper_source()
    assert "settings.configure(" in _helper_source() or "_dj_settings.configure(" in _helper_source()


def test_helper_is_a_no_op_without_django() -> None:
    script = "import sys\nfrom pathlib import Path\n" + _helper_source() + textwrap.dedent(
        """
        _configure_django_if_present(".")
        print("no-op ok" if "django" not in sys.modules else "django imported")
        """
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0 and "no-op ok" in proc.stdout, proc.stderr
