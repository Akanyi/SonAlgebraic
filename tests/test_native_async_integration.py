"""native 协程与模块 ABI、按需 runtime 和本机网络调度的集成。"""
from pathlib import Path
import subprocess

import pytest

from conftest import build_temp, requires_native_compiler
from sonalgebraic.driver.compiler import build_exe
from test_coroutines import _APP_AWAIT, _APP_SYNC, _write_module_project
from test_native_coroutines import _numbered


def _run_native(root: Path, source: str) -> subprocess.CompletedProcess[str]:
    app = root / "app.sa"
    app.write_text(source, encoding="utf-8")
    exe = root / "app.exe"
    build_exe(app, exe, keep_c=False, backend="native")
    result = subprocess.run([str(exe)], text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result


@requires_native_compiler
@pytest.mark.parametrize("source,expected", [(_APP_SYNC, "49"), (_APP_AWAIT, "81")])
def test_native_async_cross_module_start_abi(source: str, expected: str) -> None:
    with build_temp("native-coro-module-", "coroutine-tests") as temp:
        root = Path(temp)
        _write_module_project(root, source)
        assert _run_native(root, source).stdout.strip() == expected


@requires_native_compiler
def test_native_async_module_bool_string_and_original_failure() -> None:
    module = _numbered('''
ASYNC SUB message(flag AS BOOL, text AS STRING) AS PUBLIC AS STRING
IF flag THEN
RETURN F"module {text}"
.ENDIF
THROW NEW ERR_MODULE, "module failure"
RETURN "unreachable"
.ENDSUB
''')
    source = _numbered('''
USE worker AS W
ASYNC SUB forwarded(flag AS BOOL) AS STRING
DIM text AS STRING AS VAR
text = AWAIT W.message(flag, F"value {42}")
RETURN text
.ENDSUB
SUB attempt() AS VOID
SYNC forwarded(FALSE)
.ENDSUB
SUB main AS PUBLIC AS VOID
DIM text AS STRING AS VAR
DIM trap AS ERROR AS VAR
text = SYNC forwarded(TRUE)
PRINT text
TRY CALL attempt() TRACEBACK ERROR AS trap
CATCH ERR_MODULE AS caught
PRINT caught
.ENDTRY
.ENDSUB
CALL main
END
''')
    with build_temp("native-coro-module-error-", "coroutine-tests") as temp:
        root = Path(temp)
        (root / "worker.sa").write_text(module, encoding="utf-8")
        assert _run_native(root, source).stdout.splitlines() == ["module value 42", "module failure"]


@requires_native_compiler
def test_native_async_localhost_connect_accept_recv_send() -> None:
    # 系统分配端口，两端都由同一事件循环推进；只启动 listener，不需要外部服务或重试抢端口。
    source = _numbered('''
USE SYS.NET AS N
ASYNC SUB serve(listener AS HANDLE AS TCP_LISTENER) AS VOID
DIM stream AS HANDLE AS NET_STREAM AS VAR
DIM text AS STRING AS VAR
DIM sent AS NUM AS LONG AS VAR
DIM closed AS BOOL AS VAR
stream = AWAIT N.ACCEPT_ASYNC(listener)
text = AWAIT N.RECV_ASYNC(stream, 1024)
sent = AWAIT N.SEND_ASYNC(stream, text)
closed = N.STREAM_CLOSE(stream)
.ENDSUB
ASYNC SUB fetch(port AS NUM AS LONG) AS STRING
DIM stream AS HANDLE AS NET_STREAM AS VAR
DIM text AS STRING AS VAR
DIM sent AS NUM AS LONG AS VAR
DIM closed AS BOOL AS VAR
stream = AWAIT N.CONNECT_ASYNC("127.0.0.1", port)
sent = AWAIT N.SEND_ASYNC(stream, "ping")
text = AWAIT N.RECV_ASYNC(stream, 1024)
closed = N.STREAM_CLOSE(stream)
RETURN text
.ENDSUB
SUB main AS PUBLIC AS VOID
DIM listener AS HANDLE AS TCP_LISTENER AS VAR
DIM pending AS PROMISE OF VOID AS VAR
DIM port AS NUM AS LONG AS VAR
DIM text AS STRING AS VAR
DIM closed AS BOOL AS VAR
listener = N.TCP_LISTEN("127.0.0.1", 0, 4)
port = N.LOCAL_PORT(listener)
pending = CALL serve(listener)
text = SYNC fetch(port)
SYNC pending
closed = N.TCP_LISTENER_CLOSE(listener)
PRINT text
.ENDSUB
CALL main
END
''')
    with build_temp("native-coro-net-", "coroutine-tests") as temp:
        assert _run_native(Path(temp), source).stdout.strip() == "ping"


@requires_native_compiler
def test_native_sync_net_promise_without_user_coroutine() -> None:
    source = _numbered('''
USE SYS.NET AS N
SUB attempt() AS VOID
DIM stream AS HANDLE AS NET_STREAM AS VAR
SYNC N.RECV_ASYNC(stream, 64)
.ENDSUB
SUB main AS PUBLIC AS VOID
DIM trap AS ERROR AS VAR
TRY CALL attempt() TRACEBACK ERROR AS trap
CATCH ERR_ASYNC AS caught
PRINT caught
.ENDTRY
.ENDSUB
CALL main
END
''')
    with build_temp("native-coro-net-error-", "coroutine-tests") as temp:
        result = _run_native(Path(temp), source)
        assert "invalid or closed NET_STREAM handle" in result.stdout
