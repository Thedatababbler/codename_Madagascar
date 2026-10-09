"""adamas_testkit tools (author fake-object fix, test A2 part 1)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "orchestra" / "harness" / "testkit"))

from adamas_testkit import fake_command, local_git_repo, local_http_server, local_tcp_server, temp_home  # noqa: E402


def test_http_server_plain_and_streamed():
    with local_http_server({"/a.txt": "hello", "/big.bin": {"body": b"x" * 50000, "stream": True}}) as srv:
        assert urllib.request.urlopen(srv.url + "/a.txt").read() == b"hello"
        assert urllib.request.urlopen(srv.url + "/big.bin").read() == b"x" * 50000
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(srv.url + "/missing")
    assert [r["path"] for r in srv.requests] == ["/a.txt", "/big.bin", "/missing"]


def test_http_server_with_requests_stream():
    requests = pytest.importorskip("requests")
    with local_http_server({"/t.zip": b"PK\x03\x04data"}, stream=True) as srv:
        r = requests.get(srv.url + "/t.zip", stream=True)
        assert b"".join(r.iter_content(4)) == b"PK\x03\x04data"


def test_git_repo_clone_branch_tag(tmp_path):
    with local_git_repo({"cookiecutter.json": "{}"}, commits=[{"README": "v2"}], branches={"dev": {"dev.txt": "d"}}, tags=["v1.0"]) as repo:
        dest = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", repo.url, str(dest)], check=True)
        assert (dest / "cookiecutter.json").is_file() and (dest / "README").read_text() == "v2"
        subprocess.run(["git", "-C", str(dest), "checkout", "-q", "dev"], check=True)
        assert (dest / "dev.txt").is_file()
        tags = subprocess.run(["git", "-C", str(dest), "tag"], capture_output=True, text=True, check=True).stdout.split()
        assert tags == ["v1.0"]


def test_tcp_server_scripted_dialogue():
    with local_tcp_server([(b"A1 LOGIN", b"A1 OK done\r\n"), (None, b"* BYE\r\n")], greeting=b"* OK ready\r\n") as (host, port):
        s = socket.create_connection((host, port), timeout=5)
        f = s.makefile("rwb")
        assert f.readline() == b"* OK ready\r\n"
        f.write(b"A1 LOGIN u p\r\n")
        f.flush()
        assert f.readline() == b"A1 OK done\r\n"
        f.write(b"A2 LOGOUT\r\n")
        f.flush()
        assert f.readline() == b"* BYE\r\n"
        s.close()


def test_temp_home_restores():
    before = os.environ.get("HOME")
    with temp_home() as home:
        assert os.environ["HOME"] == str(home) and Path(os.environ["XDG_CONFIG_HOME"]).is_dir()
    assert os.environ.get("HOME") == before


def test_fake_command_prepends_and_keeps_path():
    before = os.environ["PATH"]
    with fake_command("git", 'echo "fake git $*"') as cmd:
        assert shutil.which("git") == str(cmd)
        assert os.environ["PATH"].endswith(before)
        out = subprocess.run(["git", "clone", "x"], capture_output=True, text=True).stdout
        assert out.strip() == "fake git clone x"
        assert shutil.which("sh")                      # real tools still found
        assert cmd.read_text().startswith("#!/")
    assert os.environ["PATH"] == before
