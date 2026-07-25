from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread

import pytest

from conftest import compile_c, requires_c_compiler, requires_native_compiler
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.backend.native.llvmir import generate_native_llvm_ir
from sonalgebraic.driver.compiler import build_exe
from sonalgebraic.frontend.parser import parse_program
from sonalgebraic.packaging.module_compiler import compile_project
from sonalgebraic.packaging.slib import build_slib
from sonalgebraic.packaging.toolchain import host_target


class _NetHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/hello":
            body = b"hello net"
            self.send_response(200)
        else:
            body = b"missing"
            self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length)
        if self.path == "/post":
            body = b"post:" + payload
        elif self.path == "/request":
            body = b"request:POST:" + payload
        else:
            body = b"missing"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


@contextmanager
def local_http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NetHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def net_source(port: int) -> str:
    base = f"http://127.0.0.1:{port}"
    return f'''10 USE SYS.NET AS N
20 USE SYS.STRING AS S
30 DIM body AS STRING AS VAR
40 DIM status AS NUM AS LONG AS VAR
50 DIM posted AS STRING AS VAR
60 DIM requested AS STRING AS VAR
70 DIM request_status AS NUM AS LONG AS VAR
80 DIM encoded AS STRING AS VAR
90 DIM headers AS STRING AS VAR
100 DIM error AS STRING AS VAR
110 DIM failed AS STRING AS VAR
120 DIM has_headers AS BOOL AS VAR
130 DIM has_error AS BOOL AS VAR
140 SUB main AS PUBLIC AS VOID
150 body = N.GET("{base}/hello")
160 status = N.STATUS("{base}/hello")
170 posted = N.POST("{base}/post", "payload", "text/plain")
180 requested = N.REQUEST_TIMEOUT("POST", "{base}/request", "abc", "X-Test: yes\\r\\n", 5000)
190 request_status = N.REQUEST_STATUS_TIMEOUT("POST", "{base}/request", "abc", "", 5000)
200 headers = N.LAST_HEADERS()
210 has_headers = S.FIND(headers, "Content-Type") >= 0
220 encoded = N.URLENCODE("a b+c")
230 failed = N.REQUEST_TIMEOUT("GET", "not-a-url", "", "", 100)
240 error = N.LAST_ERROR()
250 has_error = S.LENGTH(error) > 0
260 PRINT F"status={{status}}"
270 PRINT body
280 PRINT F"post={{posted}}"
290 PRINT F"request={{requested}}"
300 PRINT F"request_status={{request_status}}"
310 PRINT F"has_headers={{has_headers}}"
320 PRINT F"has_error={{has_error}}"
330 PRINT F"encoded={{encoded}}"
340 .ENDSUB
350 CALL main
360 END
'''


_EXPECTED = [
    "status=200",
    "hello net",
    "post=post:payload",
    "request=request:POST:abc",
    "request_status=200",
    "has_headers=1",
    "has_error=1",
    "encoded=a%20b%2Bc",
]


_SOCKET_SOURCE = '''10 USE SYS.NET AS N
20 USE SYS.BINARY AS B
30 USE SYS.STRING AS S
40 DIM listener AS HANDLE AS TCP_LISTENER AS VAR
50 DIM client AS HANDLE AS NET_STREAM AS VAR
60 DIM server AS HANDLE AS NET_STREAM AS VAR
70 DIM udp_receiver AS HANDLE AS UDP_SOCKET AS VAR
80 DIM udp_sender AS HANDLE AS UDP_SOCKET AS VAR
90 DIM udp_connected AS HANDLE AS UDP_SOCKET AS VAR
100 DIM packet AS HANDLE AS BUFFER AS VAR
110 DIM received AS HANDLE AS BUFFER AS VAR
120 DIM udp_packet AS HANDLE AS BUFFER AS VAR
130 DIM port AS NUM AS LONG AS VAR
140 DIM sent AS NUM AS LONG AS VAR
150 DIM reply AS STRING AS VAR
160 DIM udp_text AS STRING AS VAR
170 DIM dns AS STRING AS VAR
180 DIM ok AS BOOL AS VAR
190 SUB main AS PUBLIC AS VOID
200 listener = N.TCP_LISTEN("127.0.0.1", 0, 4)
210 port = N.LOCAL_PORT(listener)
220 client = N.TCP_CONNECT("127.0.0.1", port, 5000)
230 server = N.TCP_ACCEPT(listener, 5000)
240 packet = B.HEX_DECODE("004100FF7F80")
250 sent = N.STREAM_SEND_BUFFER(client, packet, 0, B.LENGTH(packet))
260 received = N.STREAM_RECV_BUFFER(server, 32)
270 PRINT F"tcp={B.HEX_ENCODE(received)} sent={sent} peer={N.LAST_PEER_HOST()} peer_port={N.LAST_PEER_PORT() > 0}"
280 sent = N.STREAM_SEND(server, "pong")
290 reply = N.STREAM_RECV(client, 32)
300 PRINT F"reply={reply} sent={sent} port={port > 0}"
310 ok = N.STREAM_CLOSE(server)
320 ok = N.STREAM_CLOSE(client)
330 ok = N.TCP_LISTENER_CLOSE(listener)
340 udp_receiver = N.UDP_OPEN()
350 udp_sender = N.UDP_OPEN()
360 ok = N.UDP_BIND(udp_receiver, "127.0.0.1", 0)
370 port = N.UDP_LOCAL_PORT(udp_receiver)
380 sent = N.UDP_SEND_BUFFER_TO(udp_sender, "127.0.0.1", port, packet, 0, B.LENGTH(packet))
390 udp_packet = N.UDP_RECV_BUFFER(udp_receiver, 32)
400 PRINT F"udp={B.HEX_ENCODE(udp_packet)} sent={sent} peer={N.LAST_PEER_HOST()} peer_port={N.LAST_PEER_PORT() > 0}"
410 udp_connected = N.UDP_OPEN()
420 ok = N.UDP_CONNECT(udp_connected, "127.0.0.1", port)
430 sent = N.UDP_SEND(udp_connected, "hello udp")
440 udp_text = N.UDP_RECV(udp_receiver, 32)
450 dns = N.DNS("localhost")
460 PRINT F"udp_text={udp_text} dns={S.LENGTH(dns) > 0} port={port > 0}"
470 ok = N.UDP_CLOSE(udp_connected)
480 ok = N.UDP_CLOSE(udp_sender)
490 ok = N.UDP_CLOSE(udp_receiver)
500 ok = B.CLOSE(udp_packet)
510 ok = B.CLOSE(received)
520 ok = B.CLOSE(packet)
530 .ENDSUB
540 CALL main
550 END
'''


_SOCKET_EXPECTED = [
    "tcp=004100FF7F80 sent=6 peer=127.0.0.1 peer_port=1",
    "reply=pong sent=4 port=1",
    "udp=004100FF7F80 sent=6 peer=127.0.0.1 peer_port=1",
    "udp_text=hello udp dns=1 port=1",
]


_TLS_SOURCE = '''10 USE SYS.NET AS N
20 DIM stream AS HANDLE AS NET_STREAM AS VAR
30 DIM ok AS BOOL AS VAR
40 SUB main AS PUBLIC AS VOID
50 stream = N.TLS_CONNECT("example.com", 443, 5000)
60 IF stream <> NULL THEN
70 ok = N.STREAM_CLOSE(stream)
80 .ENDIF
90 .ENDSUB
100 CALL main
110 END
'''


def net_temp(prefix: str) -> TemporaryDirectory[str]:
    root = Path("build") / "net-tests"
    root.mkdir(parents=True, exist_ok=True)
    return TemporaryDirectory(prefix=prefix, dir=root)


def test_socket_api_generates_c_and_native_ir() -> None:
    checked = check_program(parse_program(_SOCKET_SOURCE))
    c = compile_c(_SOCKET_SOURCE)
    ir = generate_native_llvm_ir(checked)
    assert "sa_net_tcp_listen" in c
    assert "sa_net_udp_send_buffer_to" in c
    assert "declare i64 @sa_net_tcp_accept(i64, i64)" in ir
    assert "declare i64 @sa_net_udp_recv_buffer(i64, i64)" in ir


def test_tls_stream_enables_dedicated_runtime_feature() -> None:
    checked = check_program(parse_program(_TLS_SOURCE))
    c = compile_c(_TLS_SOURCE)
    ir = generate_native_llvm_ir(checked)
    assert "#define SA_ENABLE_NET" in c
    assert "#define SA_ENABLE_TLS" in c
    assert "sa_net_tls_connect" in c
    assert "declare i64 @sa_net_tls_connect(ptr, i64, i64)" in ir


def test_net_null_handle_lowers_to_integer_zero() -> None:
    source = '''10 USE SYS.NET AS N
20 DIM sent AS NUM AS LONG AS VAR
30 SUB main AS PUBLIC AS VOID
40 sent = N.STREAM_SEND(NULL, "x")
50 .ENDSUB
60 CALL main
70 END
'''
    checked = check_program(parse_program(source))
    c = compile_c(source)
    ir = generate_native_llvm_ir(checked)
    assert 'sa_net_stream_send(0, "x")' in c
    assert "call i64 @sa_net_stream_send(i64 0" in ir


@pytest.mark.skipif(shutil.which("zig") is None, reason="POSIX 严格 C11 语法检查需要 zig")
def test_posix_net_runtime_compiles_as_strict_c11() -> None:
    source = '''10 USE SYS.NET AS N
20 DIM address AS STRING AS VAR
30 SUB main AS PUBLIC AS VOID
40 address = N.DNS("localhost")
50 .ENDSUB
60 CALL main
70 END
'''
    with net_temp("sonalgebraic-posix-syntax-") as temp:
        probe = Path(temp) / "probe.c"
        probe.write_text("int main(void) { return 0; }\n", encoding="ascii")
        probe_proc = subprocess.run(
            ["zig", "cc", "-target", "x86_64-linux-gnu", "-std=c11", "-fsyntax-only", "probe.c"],
            cwd=temp,
            text=True,
            capture_output=True,
        )
        if probe_proc.returncode != 0:
            pytest.skip("当前 Zig 安装无法编译最小 C 探针")
        c_path = Path(temp) / "net.c"
        c_path.write_text(compile_c(source), encoding="utf-8")
        proc = subprocess.run(
            ["zig", "cc", "-target", "x86_64-linux-gnu", "-std=c11", "-fsyntax-only", "net.c"],
            cwd=temp,
            text=True,
            capture_output=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr


def test_user_module_propagates_tls_runtime_feature() -> None:
    module_source = '''10 USE SYS.NET AS N
20 SUB connect(host AS STRING, port AS NUM AS LONG) AS PUBLIC AS HANDLE AS NET_STREAM
30 RETURN N.TLS_CONNECT(host, port, 5000)
40 .ENDSUB
'''
    main_source = '''10 USE TLSLIB AS T
20 DIM stream AS HANDLE AS NET_STREAM AS VAR
30 SUB main AS PUBLIC AS VOID
40 stream = CALL T.connect("example.com", 443)
50 .ENDSUB
60 CALL main
70 END
'''
    with net_temp("sonalgebraic-tls-module-") as temp:
        root = Path(temp)
        (root / "tlslib.sa").write_text(module_source, encoding="utf-8")
        main = root / "main.sa"
        main.write_text(main_source, encoding="utf-8")
        plan = compile_project(main, root / "out")
        runtime = plan.runtime_c.read_text(encoding="utf-8")
        assert "#define SA_ENABLE_NET" in runtime
        assert "#define SA_ENABLE_TLS" in runtime
        assert plan.modules["tlslib"].runtime_features == ["net", "tls"]
        if "windows" in host_target():
            assert "secur32" in plan.link_libs
            assert "ws2_32" in plan.link_libs
        else:
            assert "ssl" in plan.link_libs
            assert "crypto" in plan.link_libs


def test_source_slib_preserves_tls_runtime_feature() -> None:
    module_source = '''10 USE SYS.NET AS N
20 SUB connect(host AS STRING, port AS NUM AS LONG) AS PUBLIC AS HANDLE AS NET_STREAM
30 RETURN N.TLS_CONNECT(host, port, 5000)
40 .ENDSUB
'''
    main_source = '''10 USE TLSLIB AS T
20 DIM stream AS HANDLE AS NET_STREAM AS VAR
30 SUB main AS PUBLIC AS VOID
40 stream = CALL T.connect("example.com", 443)
50 .ENDSUB
60 CALL main
70 END
'''
    with net_temp("sonalgebraic-tls-slib-") as temp:
        root = Path(temp)
        module = root / "tlslib.sa"
        module.write_text(module_source, encoding="utf-8")
        build_slib(module, root / "tlslib.slib", module_name="TLSLIB")
        module.unlink()
        main = root / "main.sa"
        main.write_text(main_source, encoding="utf-8")
        plan = compile_project(main, root / "out")
        runtime = plan.runtime_c.read_text(encoding="utf-8")
        assert "#define SA_ENABLE_TLS" in runtime
        assert plan.modules["tlslib"].runtime_features == ["net", "tls"]
        if "windows" in host_target():
            assert "secur32" in plan.link_libs
        else:
            assert "ssl" in plan.link_libs
            assert "crypto" in plan.link_libs


def _run_socket_program(temp: str, backend: str) -> list[str]:
    src = Path(temp) / "socket.sa"
    src.write_text(_SOCKET_SOURCE, encoding="utf-8")
    exe = Path(temp) / f"socket_{backend}.exe"
    build_exe(src, exe, keep_c=False, backend=backend)
    proc = subprocess.run([str(exe)], text=True, capture_output=True, timeout=15)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


@requires_c_compiler
def test_c_backend_sys_net_get_and_status() -> None:
    with local_http_server() as port, net_temp("sonalgebraic-net-c-") as temp:
        src = Path(temp) / "net.sa"
        src.write_text(net_source(port), encoding="utf-8")
        exe = Path(temp) / "net_c.exe"
        build_exe(src, exe, keep_c=False, backend="c")
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == _EXPECTED


@requires_native_compiler
def test_native_backend_sys_net_get_and_status() -> None:
    with local_http_server() as port, net_temp("sonalgebraic-net-native-") as temp:
        src = Path(temp) / "net.sa"
        src.write_text(net_source(port), encoding="utf-8")
        exe = Path(temp) / "net_native.exe"
        build_exe(src, exe, keep_c=False, backend="native")
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == _EXPECTED


@requires_c_compiler
def test_c_backend_tcp_udp_loopback() -> None:
    with net_temp("sonalgebraic-socket-c-") as temp:
        assert _run_socket_program(temp, "c") == _SOCKET_EXPECTED


@requires_native_compiler
def test_native_backend_tcp_udp_loopback() -> None:
    with net_temp("sonalgebraic-socket-native-") as temp:
        assert _run_socket_program(temp, "native") == _SOCKET_EXPECTED


@requires_c_compiler
def test_c_backend_tls_stream_links() -> None:
    with net_temp("sonalgebraic-tls-c-") as temp:
        src = Path(temp) / "tls.sa"
        src.write_text(_TLS_SOURCE, encoding="utf-8")
        build_exe(src, Path(temp) / "tls_c.exe", keep_c=False, backend="c")


@requires_native_compiler
def test_native_backend_tls_stream_links() -> None:
    with net_temp("sonalgebraic-tls-native-") as temp:
        src = Path(temp) / "tls.sa"
        src.write_text(_TLS_SOURCE, encoding="utf-8")
        build_exe(src, Path(temp) / "tls_native.exe", keep_c=False, backend="native")
