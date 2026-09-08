import subprocess

from orchestra.control.fast_loop.workspace import is_cache_path, write_cache_exclude


def test_cache_paths_recognised():
    assert is_cache_path("hl7/__pycache__/__init__.cpython-311.pyc")
    assert is_cache_path("pkg/mod.pyc")
    assert not is_cache_path("hl7/__init__.py")
    assert not is_cache_path("docs/__pycache__.md")


def test_exclude_keeps_caches_out_of_status(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    write_cache_exclude(tmp_path)
    (tmp_path / "pkg").mkdir(); (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "__pycache__").mkdir(); (tmp_path / "pkg" / "__pycache__" / "x.pyc").write_bytes(b"x")
    out = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "__init__.py" in out and "__pycache__" not in out
