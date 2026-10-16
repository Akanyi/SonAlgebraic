"""托管 socket 句柄：`TCP_LISTENER` / `NET_STREAM` / `UDP_SOCKET` 的所有权规则与真实回收。

socket 比文件更狠的地方在于**内核资源**：漏关一个监听端不是「进程退出时顺手清掉」那么
轻，端口还占着；而提前关掉另一个端会让对方读到连接被重置。所以三件事必须一起成立：

1. 语义层按 kind 走同一套托管协议（独占、裸/托管不互转、借用只读、关闭后失效）；
2. `ACCEPT_ASYNC` 这类「把句柄存进待完成操作」的调用不能接受托管句柄——Promise 完成前
   作用域析构就会把它关掉，运行时用的是一句悬垂的 token；
3. 作用域退出和异常展开两条路径都要真的把 socket 关掉，且各关一次。

第 3 条只能在编译运行时看：`sa_net_*_drop` 是 runtime 的真函数，谁调用它、调用几次，
文字断言证明不了。这里用 malloc 计数桩兜底——socket 槽位本身不分配，但连接建立过程中
的缓冲区会，净分配不为 0 就说明这条路径上有东西没被回收。

恢复说明：原文件在 10 月初丢失，测试侧没有字节码可比对。这份是按幸存的 pytest 节点名
（含每个参数化用例的源码与期望诊断）配合恢复后的 `analysis/builtin_effects.py`、
`analysis/typesys.py` 的 `SYS.NET` 表重建的，不是逐字找回。详见 `.recovery/RECOVERY.md`。
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from conftest import build_temp, compile_c, requires_gcc
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.analysis.typesys import runtime_features_for_program
from sonalgebraic.backend.c_runtime import RUNTIME_HEADER, RUNTIME_SOURCE
from sonalgebraic.backend.native import generate_native_llvm_ir
from sonalgebraic.core.errors import SonCompileError
from sonalgebraic.frontend.parser import parse_program
from test_coroutines import _LEAK_SHIM
from test_native_coroutines import _WRAP, requires_wrap

_BACKENDS = [pytest.param("c", marks=requires_gcc), pytest.param("native", marks=requires_wrap)]

_HEADER = "10 USE SYS.NET AS N\n"

_LIVE_REPORT = r'''
#include <stdio.h>
extern long sa_test_live;
static void sa_probe_report(void) { fprintf(stderr, "SA_LIVE=%ld\n", sa_test_live); }
__attribute__((constructor)) static void sa_probe_register(void) { atexit(sa_probe_report); }
'''


def _program(fragment: str, local: str) -> str:
    """`local` 一行声明 + 片段一起放进 main 体内，行号从 40 起。

    声明留在 SUB 里而不是写成全局：托管资源的 `f=` / `m=` 一律拒绝全局源
    （其他 SUB 可能在背后改它，冻结管不到）。
    """
    lines = [f"{40 + index * 10} {line.strip()}"
             for index, line in enumerate(f"{local}\n{fragment}".strip().splitlines())]
    return (_HEADER + "20 SUB main AS PUBLIC AS VOID\n" + "\n".join(lines)
            + "\n900 .ENDSUB\n910 CALL main\n920 END\n")


_ERROR_CASES = [
    # `=` 在托管句柄上是多一个持有者，析构会关两遍
    ("DIM other AS MANAGED HANDLE AS TCP_LISTENER\nother = listener", "独占资源不能用 = 复制"),
    # ACCEPT_ASYNC 把句柄存进待完成的 Promise：托管值会在等待期间被作用域析构掉
    ("DIM pending AS PROMISE OF HANDLE AS NET_STREAM\npending = N.ACCEPT_ASYNC(listener)", "不能逃逸"),
    # 裸句柄与托管句柄在 C 里是同一个整数，只能靠类型层拦
    ("DIM raw AS HANDLE AS TCP_LISTENER\nraw = listener", "不能隐式互转"),
    # 关闭状态跨语句保留：CLOSE 之后再查询拿到的是失效 token
    ("PRINT N.TCP_LISTENER_CLOSE(listener)\nPRINT N.LOCAL_PORT(listener)", "已关闭"),
]


@pytest.mark.parametrize(("body", "needle"), _ERROR_CASES)
def test_managed_socket_rules(body: str, needle: str) -> None:
    """四条规则：独占、异步逃逸、裸/托管边界、关闭后失效。

    第三条在 runtime 里没有任何表示层依据（两边都是 `SaHandle`），前两条的代价分别是
    「关两遍」和「运行时用悬垂 token」，最后一条会让内核句柄被重复使用后打到错的连接上。
    """
    source = _program(body, "DIM listener AS MANAGED HANDLE AS TCP_LISTENER")
    with pytest.raises(SonCompileError, match=needle):
        check_program(parse_program(source))


_LISTENER_SOURCE = (
    _HEADER
    + "20 USE SYS.STRING AS S\n"
      "30 SUB main AS PUBLIC AS VOID\n"
      "40 SCOPE\n"
      "50 DIM listener AS MANAGED HANDLE AS TCP_LISTENER\n"
      "60 DIM client AS MANAGED HANDLE AS NET_STREAM\n"
      "70 DIM server AS MANAGED HANDLE AS NET_STREAM\n"
      "80 DIM port AS NUM AS LONG AS VAR\n"
      '90 listener = N.TCP_LISTEN("127.0.0.1", 0, 4)\n'
      "100 port = N.LOCAL_PORT(listener)\n"
      '110 client = N.TCP_CONNECT("127.0.0.1", port, 5000)\n'
      "120 server = N.TCP_ACCEPT(listener, 5000)\n"
      '130 PRINT N.STREAM_SEND(server, "pong")\n'
      "140 PRINT S.LENGTH(N.STREAM_RECV(client, 4))\n"
      "150 PRINT port > 0\n"
      "160 .ENDSCOPE\n"
      '170 PRINT "closed"\n'
      "180 .ENDSUB\n"
      "190 CALL main\n"
      "200 END\n"
)

_LISTENER_EXPECTED = ["4", "4", "1", "closed"]


_UNWIND_SOURCE = (
    _HEADER
    + "20 DIM trap AS ERROR AS VAR\n"
      "30 SUB main AS PUBLIC AS VOID\n"
      "40 TRY CALL risky TRACEBACK ERROR AS trap\n"
      "50 CATCH ERR_PROBE AS caught\n"
      '60 PRINT "caught"\n'
      "70 .ENDTRY\n"
      "80 .ENDSUB\n"
      "90 SUB risky AS PRIVATE AS VOID\n"
      "100 SCOPE\n"
      "110 DIM socket AS MANAGED HANDLE AS UDP_SOCKET\n"
      "120 DIM ok AS BOOL AS VAR\n"
      "130 socket = N.UDP_OPEN()\n"
      '140 ok = N.UDP_BIND(socket, "127.0.0.1", 0)\n'
      "150 PRINT ok\n"
      '160 THROW NEW ERR_PROBE, "failure"\n'
      "170 .ENDSCOPE\n"
      "180 .ENDSUB\n"
      "190 CALL main\n"
      "200 END\n"
)

_UNWIND_EXPECTED = ["1", "caught"]


def _feature_prefix(source: str) -> str:
    """native 侧要知道开哪些 runtime 块：从语义层算出的 feature 集合逐条发 #define。"""
    checked = check_program(parse_program(source))
    features = runtime_features_for_program(checked.program, checked.uses)
    return "".join(f"#define SA_ENABLE_{feature.upper()}\n" for feature in sorted(features))


def _run_source(source: str, backend: str, opt: str, stem: str) -> subprocess.CompletedProcess[str]:
    """编译并运行：C 侧插 malloc 计数桩，native 侧用符号级 -wrap 插桩，两边都链 winsock。"""
    with build_temp(f"managed-socket-{stem}-{backend}-", "socket-tests") as temp:
        root = Path(temp)
        exe = root / ("case.exe" if sys.platform == "win32" else "case")
        net_libs = ["-lws2_32", "-lwinhttp"] if sys.platform == "win32" else []
        if backend == "c":
            c_path = root / f"{stem}.c"
            text = _LEAK_SHIM + "\n" + compile_c(source)
            text = text.replace("sa_program_end:", "sa_program_end: atexit(sa__rep);", 1)
            c_path.write_text(text, encoding="utf-8")
            gcc = shutil.which("gcc")
            assert gcc is not None
            command = [gcc, str(c_path), opt, "-std=c11", *net_libs, "-o", str(exe)]
        else:
            (root / f"{stem}.ll").write_text(
                generate_native_llvm_ir(check_program(parse_program(source))), encoding="utf-8")
            (root / "rt.c").write_text(_feature_prefix(source) + RUNTIME_HEADER + "\n" + RUNTIME_SOURCE,
                                       encoding="utf-8")
            (root / "wrap.c").write_text(_WRAP + _LIVE_REPORT, encoding="utf-8")
            flags = [f"-Wl,-wrap:{name}" if sys.platform == "win32" else f"-Wl,--wrap={name}"
                     for name in ("malloc", "calloc", "realloc", "free")]
            command = [shutil.which("clang"), "-fuse-ld=lld", str(root / f"{stem}.ll"),
                       str(root / "rt.c"), str(root / "wrap.c"), opt, "-fno-builtin",
                       "-D_CRT_SECURE_NO_WARNINGS", *flags, *net_libs, "-o", str(exe)]
        compiled = subprocess.run(command, text=True, capture_output=True, timeout=180)
        assert compiled.returncode == 0, compiled.stderr
        return subprocess.run([str(exe)], cwd=root, text=True, encoding="utf-8",
                              capture_output=True, timeout=60)


@pytest.mark.parametrize("backend", _BACKENDS)
@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_scope_exit_closes_listener_and_both_stream_ends(backend: str, opt: str) -> None:
    """三件一起放 SCOPE：退出时必须各关一次，之后端口能立刻被重新绑定。

    「能重新绑定」是这条测试真正的证人：漏关监听端的话新绑定会报地址被占用，
    而那个失败不会体现在任何我们打印出来的数字上。
    """
    result = _run_source(_LISTENER_SOURCE, backend, opt, "listener")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == _LISTENER_EXPECTED
    assert "SA_LIVE=0" in result.stderr, result.stderr


@pytest.mark.parametrize("backend", _BACKENDS)
@pytest.mark.parametrize("opt", ["-O0"])
def test_udp_socket_is_released_when_an_exception_unwinds(backend: str, opt: str) -> None:
    """异常展开也要走析构：从 SCOPE 里抛出去，socket 必须随展开一起回收。

    只在正常出口测的话，「作用域析构挂在块尾」这种实现能过——而异常路径压根到不了块尾。
    只在 -O0 跑：展开路径由 setjmp 帧决定，优化开关不改它，再来一遍只是多编译两次。
    """
    result = _run_source(_UNWIND_SOURCE, backend, opt, "unwind")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == _UNWIND_EXPECTED
    assert "SA_LIVE=0" in result.stderr, result.stderr
