# -*- coding: utf-8 -*-
"""启动脚本自己清掉上一次留下的残骸。

用户的原话："would be great that every time we start the app it kills
everything that still running that are not necessary anymore, some forgotten
old related stuff that were running"。

脚本本来就能发现这种情况，只是把 taskkill 那一行打出来让用户自己复制。这里
钉的是它现在自己动手，以及——更要紧的——它**不**动手的那一种：占着端口的
要是别人的程序，它只报告，不杀。一个"为了占用端口去杀陌生进程"的启动脚本
比它要解决的问题更糟。

这些测试真的起进程、真的占端口、真的调那几个 shell 函数。
"""

import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "start-local.sh"

def a_real_bash() -> str:
    """Git Bash，不是 WSL 的那个。

    Windows 上 ``shutil.which("bash")`` 先找到的是 System32 里的 WSL 转发器，
    它跑不了这个脚本（``execvpe(/bin/bash) failed``）。
    """
    candidates = [
        "C:/Program Files/Git/bin/bash.exe",
        "C:/Program Files/Git/usr/bin/bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("bash")
    if found and "System32" not in found and "system32" not in found:
        return found
    return ""


BASH = a_real_bash()

pytestmark = pytest.mark.skipif(not BASH, reason="these exercise the bash launcher")


def functions() -> str:
    """把脚本里那几个函数抠出来——直接 source 整个脚本会把服务都起起来。"""
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("# --- leftovers from a previous run")
    end = text.index("\necho\necho \"MoneyPrinterTurbo")
    return text[start:end]


STUBS = """
CURRENT_DIR='{root}'
PYTHON_BIN='{python}'
FAILED=0
ok()     {{ echo "OK $*"; }}
warn()   {{ echo "WARN $*"; }}
fail()   {{ echo "FAIL $*"; }}
detail() {{ echo "    $*"; }}
http_ok() {{
  "$PYTHON_BIN" - "$1" <<'PY' 2>/dev/null
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as r:
        sys.exit(0 if 200 <= r.status < 300 else 1)
except Exception:
    sys.exit(1)
PY
}}
"""


def run_bash(body: str) -> str:
    script = (
        STUBS.format(root=str(ROOT).replace("\\", "/"), python=sys.executable.replace("\\", "/"))
        + functions()
        + body
    )
    done = subprocess.run(
        [BASH, "-s"], input=script, capture_output=True, text=True, timeout=120
    )
    return done.stdout + done.stderr


def a_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


LISTENER = textwrap.dedent(
    """
    import socket, sys, time
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", int(sys.argv[1])))
    s.listen(5)
    # 占着端口，什么都不回答——这正是"卡住的服务"的样子。
    time.sleep(300)
    """
)


@pytest.fixture
def squatter():
    """起一个占着端口、不回答任何请求的进程。"""
    started = []

    def start(port: int, *, pretend_to_be_ours: bool):
        argv = [sys.executable, "-c", LISTENER, str(port)]
        if pretend_to_be_ours:
            # 命令行里带上这个项目的标记，但跑的还是上面那个无害的监听器。
            argv.append("local_services/genserver/server.py")
        proc = subprocess.Popen(argv)
        started.append(proc)
        for _ in range(50):
            with socket.socket() as probe:
                probe.settimeout(0.2)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    return proc
            time.sleep(0.1)
        raise AssertionError("the squatter never took the port")

    yield start
    for proc in started:
        proc.kill()
        proc.wait(timeout=10)


class TestFindingIt:
    def test_it_finds_who_owns_the_port(self, squatter):
        """找的是**真正占着端口的那个**，不一定是我们启动的那个 pid。

        Windows 上 .venv 里的 python.exe 是个跳板，它再去起真的解释器，
        所以听端口的是子进程。脚本自己的注释里就记着这件事，而这正是
        停进程的时候要用 ``taskkill //T``（连子进程一起）的理由。
        """
        port = a_free_port()
        squatter(port, pretend_to_be_ours=False)
        out = run_bash(
            f'pid=$(pid_on_port {port}); echo "PID:[$pid]"; echo "CMD:$(command_of $pid)"'
        )
        assert "PID:[]" not in out, "nothing was found on the port"
        # 找到的那个进程的命令行里带着这个端口号，那就是我们起的那个。
        assert str(port) in out.split("CMD:", 1)[1]

    def test_a_free_port_has_nobody(self):
        out = run_bash(f'echo "PID:[$(pid_on_port {a_free_port()})]"')
        assert "PID:[]" in out


class TestWhatItRefusesToKill:
    def test_somebody_else_s_program_is_reported_not_killed(self, squatter):
        """占着端口的要是别人的程序，杀它比端口被占更糟。"""
        port = a_free_port()
        proc = squatter(port, pretend_to_be_ours=False)
        out = run_bash(f'reap_stale {port} "http://127.0.0.1:{port}/health" "x"')
        assert "not ours" in out
        assert "will not kill a process it does not recognise" in out
        assert proc.poll() is None  # 还活着

    def test_keep_stale_turns_it_off(self, squatter):
        port = a_free_port()
        proc = squatter(port, pretend_to_be_ours=True)
        out = run_bash(
            f'KEEP_STALE=1; reap_stale {port} "http://127.0.0.1:{port}/health" "x"'
        )
        assert out.strip() == ""
        assert proc.poll() is None

    def test_a_service_that_answers_is_left_alone(self, squatter):
        """"已经起来的服务原样不动"——这条规矩没有被改掉。"""
        port = a_free_port()
        server = subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    """
                    import sys
                    from http.server import BaseHTTPRequestHandler, HTTPServer
                    class H(BaseHTTPRequestHandler):
                        def do_GET(self):
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json")
                            self.end_headers()
                            self.wfile.write(b'{"ok": true}')
                        def log_message(self, *a): pass
                    HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
                    """
                ),
                str(port),
                "local_services/genserver/server.py",
            ]
        )
        try:
            for _ in range(60):
                with socket.socket() as probe:
                    probe.settimeout(0.2)
                    if probe.connect_ex(("127.0.0.1", port)) == 0:
                        break
                time.sleep(0.1)
            out = run_bash(
                f'reap_stale {port} "http://127.0.0.1:{port}/health" "genserver" "ok"'
            )
            assert "stopping it" not in out
            assert server.poll() is None
        finally:
            server.kill()
            server.wait(timeout=10)


class TestWhatItDoesKill:
    def test_our_own_wedged_process_is_stopped(self, squatter):
        port = a_free_port()
        proc = squatter(port, pretend_to_be_ours=True)
        out = run_bash(
            f'reap_stale {port} "http://127.0.0.1:{port}/health" "genserver"'
        )
        assert "from an earlier run" in out
        assert f"freed :{port}" in out
        for _ in range(50):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert proc.poll() is not None, "the wedged process is still running"
