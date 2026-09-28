"""SA 协程（ASYNC / AWAIT / SYNC / PROMISE）测试。

阶段 0：解析、语义约束、类型推断、native 状态机 IR。
阶段 1：codegen 结构断言 + 端到端编译运行 + 无泄漏——把无栈状态机、
事件循环、PROMISE 结果搬运、资源清理融合一起压实。
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

from conftest import build_temp, compile_c, expect_error, requires_c_compiler
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.backend.native import generate_native_llvm_ir
from sonalgebraic.core import ast
from sonalgebraic.core.errors import SonCompileError
from sonalgebraic.driver.compiler import build_exe
from sonalgebraic.frontend.parser import parse_program
from sonalgebraic.packaging.module_compiler import compile_project


def _exe_path(temp_dir: Path, stem: str) -> Path:
    return temp_dir / (f"{stem}.exe" if sys.platform == "win32" else stem)


# malloc/free 计数桩：程序退出时净分配必须为 0，否则协程帧或 PROMISE 漏回收。
# 与 test_e2e.py 的泄漏测试同款，MinGW gcc 上稳定。协程帧走 calloc（零初始化，让挂起中
# 被 drop 时的 cleanup 能安全 free 未到达的局部），所以 calloc 也要计数，否则净值会偏负。
_LEAK_SHIM = (
    # 计数桩先于 runtime 前导包含系统头，POSIX 特性宏必须抢在它们之前生效。
    "#ifndef _WIN32\n#ifndef _POSIX_C_SOURCE\n#define _POSIX_C_SOURCE 200112L\n#endif\n"
    "#ifndef _FILE_OFFSET_BITS\n#define _FILE_OFFSET_BITS 64\n#endif\n#endif\n"
    "#include <stdio.h>\n#include <stdlib.h>\n"
    "static long sa__live=0;\n"
    "static void* sa__m(size_t n){void* p=malloc(n); if(p)sa__live++; return p;}\n"
    "static void* sa__c(size_t n,size_t m){void* p=calloc(n,m); if(p)sa__live++; return p;}\n"
    "static void* sa__r(void* q,size_t n){ if(!q) return sa__m(n); return realloc(q,n);}\n"
    "static void sa__f(void* p){ if(p){sa__live--; free(p);}}\n"
    'static void sa__rep(void){ fprintf(stderr,"SA_LIVE=%ld\\n", sa__live);}\n'
    "#define malloc sa__m\n#define calloc sa__c\n#define realloc sa__r\n#define free sa__f\n"
)


# 递归 async fib：每层 AWAIT 两个子协程，fib(10)=55。跨 AWAIT 的局部 a/b 全提升进帧，
# 同时压满状态机切分（3 个恢复点）、事件循环调度、long 结果搬运、帧回收。
_FIB_SOURCE = (
    "10 ASYNC SUB fib(n AS NUM AS LONG) AS NUM AS LONG\n"
    "20 IF n < 2 THEN\n"
    "30 RETURN n\n"
    "40 .ENDIF\n"
    "50 DIM a AS NUM AS LONG AS VAR\n"
    "60 DIM b AS NUM AS LONG AS VAR\n"
    "70 a = AWAIT fib(n - 1)\n"
    "80 b = AWAIT fib(n - 2)\n"
    "90 RETURN a + b\n"
    "100 .ENDSUB\n"
    "110 SUB main AS PUBLIC AS VOID\n"
    "120 DIM r AS NUM AS LONG AS VAR\n"
    "130 r = SYNC fib(10)\n"
    "140 PRINT r\n"
    "150 .ENDSUB\n"
    "160 CALL main\n"
    "170 END\n"
)

# STRING 结果走 move：协程 RETURN 时把字符串交给 PROMISE slot，AWAIT/SYNC 接收端接管，
# 全程零深拷贝、零 double-free。这是资源清理融合最容易翻车的一条路径。
_STRING_SOURCE = (
    "10 ASYNC SUB make_msg() AS STRING\n"
    "20 DIM s AS STRING AS VAR\n"
    '30 s = "hello from coroutine"\n'
    "40 RETURN s\n"
    "50 .ENDSUB\n"
    "60 SUB main AS PUBLIC AS VOID\n"
    "70 DIM m AS STRING AS VAR\n"
    "80 m = SYNC make_msg()\n"
    "90 PRINT m\n"
    "100 .ENDSUB\n"
    "110 CALL main\n"
    "120 END\n"
)

# VOID async + 独立 AWAIT 语句：run() 里 `AWAIT tick()` 不取值，靠 fulfill_void/take_void
# 走完挂起—恢复，验证无返回值协程也能正确串起来。
_VOID_SOURCE = (
    "10 ASYNC SUB tick() AS VOID\n"
    '20 PRINT "tick"\n'
    "30 RETURN\n"
    "40 .ENDSUB\n"
    "50 ASYNC SUB run() AS VOID\n"
    "60 AWAIT tick()\n"
    '70 PRINT "after tick"\n'
    "80 RETURN\n"
    "90 .ENDSUB\n"
    "100 SUB main AS PUBLIC AS VOID\n"
    "110 SYNC run()\n"
    "120 .ENDSUB\n"
    "130 CALL main\n"
    "140 END\n"
)


def _build_and_run(source: str, temp: str, stem: str, opt: str = "-O2", leak: bool = False, net: bool = False) -> subprocess.CompletedProcess[str]:
    """把 SA 源码编到 exe 并跑，返回进程结果。leak=True 时插 malloc 计数桩；net=True 时链 winsock。

    net 场景（含 CONNECT_ASYNC 等）的 driver 编译由 build_exe 负责链 ws2_32，但这里要插 leak
    shim 就绕不开手动 gcc，得自己补上——否则 Windows 上 getaddrinfo/WSAPoll 全是未定义引用。
    """
    gcc = shutil.which("gcc")
    assert gcc is not None
    c_text = compile_c(source)
    if leak:
        c_text = _LEAK_SHIM + "\n" + c_text
        c_text = c_text.replace("sa_program_end:", "sa_program_end: atexit(sa__rep);", 1)
    c_path = Path(temp) / f"{stem}.c"
    c_path.write_text(c_text, encoding="utf-8")
    exe = _exe_path(Path(temp), stem)
    cmd = [gcc, str(c_path), opt, "-std=c11", "-o", str(exe), "-lm"]
    if net and sys.platform == "win32":
        # 照 driver 的 builtin_link_libs：runtime_slicer 注入整个 NET 块（socket + WinHTTP），
        # 哪怕只用到 socket 也得把两个库都链上。TLS 是独立 feature 块，没用到就不牵扯 secur32。
        cmd.extend(["-lwinhttp", "-lws2_32"])
    compile_proc = subprocess.run(cmd, text=True, capture_output=True)
    assert compile_proc.returncode == 0, compile_proc.stderr
    return subprocess.run([str(exe)], text=True, capture_output=True, timeout=60)


# --- 解析：ASYNC 头 / PROMISE OF / 三种取值 / 独立 AWAIT ---

def test_async_sub_header_is_flagged() -> None:
    prog = parse_program(
        "10 ASYNC SUB fetch(n AS NUM AS LONG) AS NUM AS LONG\n20 RETURN n\n30 .ENDSUB\n"
        "40 SUB main AS PUBLIC AS VOID\n50 .ENDSUB\n"
    )
    by_name = {s.name: s for s in prog.subs}
    assert by_name["fetch"].is_async is True
    assert by_name["main"].is_async is False


def test_promise_type_carries_result_in_inner() -> None:
    prog = parse_program(
        "10 SUB main AS PUBLIC AS VOID\n20 DIM p AS PROMISE OF NUM AS LONG AS VAR\n30 .ENDSUB\n"
    )
    decl = prog.subs[0].body[0]
    assert isinstance(decl, ast.LocalDeclaration)
    assert decl.type_spec.name == "PROMISE"
    assert decl.type_spec.inner == ast.TypeSpec("NUM", "LONG")


def test_three_ways_to_take_result_parse() -> None:
    prog = parse_program(
        "10 ASYNC SUB f() AS NUM AS LONG\n20 RETURN 1\n30 .ENDSUB\n"
        "40 SUB main AS PUBLIC AS VOID\n"
        "50 DIM p AS PROMISE OF NUM AS LONG AS VAR\n"
        "60 DIM x AS NUM AS LONG AS VAR\n"
        "70 p = CALL f()\n"
        "80 x = SYNC f()\n"
        "90 .ENDSUB\n"
    )
    body = prog.subs[1].body
    p_assign, x_assign = body[2], body[3]
    assert isinstance(p_assign, ast.Assign) and isinstance(p_assign.expr, ast.CallExpr)
    assert isinstance(x_assign, ast.Assign) and isinstance(x_assign.expr, ast.SyncExpr)


def test_bare_await_is_a_statement() -> None:
    prog = parse_program(
        "10 ASYNC SUB tick() AS VOID\n20 RETURN\n30 .ENDSUB\n"
        "40 ASYNC SUB run() AS VOID\n50 AWAIT tick()\n60 RETURN\n70 .ENDSUB\n"
        "80 SUB main AS PUBLIC AS VOID\n90 .ENDSUB\n"
    )
    stmt = prog.subs[1].body[0]
    assert isinstance(stmt, ast.AwaitStmt)
    assert isinstance(stmt.expr, ast.AwaitExpr)


def test_await_forbidden_mid_expression() -> None:
    # AWAIT / SYNC 是语句级关键字，嵌进表达式中间由 expr_parser 在解析期拦死
    expect_error(
        "10 SUB main AS PUBLIC AS VOID\n20 DIM x AS NUM AS LONG AS VAR\n30 x = 1 + AWAIT foo()\n40 .ENDSUB\n",
        "不能出现在表达式中间",
    )


# --- 语义：合法程序 ---

def test_accepts_promise_call_and_sync() -> None:
    check_program(parse_program(
        "10 ASYNC SUB fetch(n AS NUM AS LONG) AS NUM AS LONG\n20 RETURN n\n30 .ENDSUB\n"
        "40 SUB main AS PUBLIC AS VOID\n"
        "50 DIM p AS PROMISE OF NUM AS LONG AS VAR\n"
        "60 p = CALL fetch(1)\n"
        "70 DIM x AS NUM AS LONG AS VAR\n"
        "80 x = SYNC fetch(2)\n"
        "90 .ENDSUB\n"
    ))


def test_accepts_await_between_async_subs() -> None:
    check_program(parse_program(
        "10 ASYNC SUB a() AS NUM AS LONG\n20 RETURN 1\n30 .ENDSUB\n"
        "40 ASYNC SUB b() AS NUM AS LONG\n50 DIM x AS NUM AS LONG AS VAR\n60 x = AWAIT a()\n70 RETURN x\n80 .ENDSUB\n"
        "90 SUB main AS PUBLIC AS VOID\n100 DIM y AS NUM AS LONG AS VAR\n110 y = SYNC b()\n120 .ENDSUB\n"
    ))


def test_accepts_bare_await_of_void_async() -> None:
    check_program(parse_program(
        "10 ASYNC SUB tick() AS VOID\n20 RETURN\n30 .ENDSUB\n"
        "40 ASYNC SUB run() AS VOID\n50 AWAIT tick()\n60 RETURN\n70 .ENDSUB\n"
        "80 SUB main AS PUBLIC AS VOID\n90 SYNC run()\n100 .ENDSUB\n"
    ))


# --- 语义：约束拒绝 ---

def test_await_outside_async_is_rejected() -> None:
    expect_error(
        "10 ASYNC SUB a() AS NUM AS LONG\n20 RETURN 1\n30 .ENDSUB\n"
        "40 SUB main AS PUBLIC AS VOID\n50 DIM x AS NUM AS LONG AS VAR\n60 x = AWAIT a()\n70 .ENDSUB\n",
        "AWAIT 只能在 ASYNC SUB",
    )


def test_async_ref_param_is_rejected() -> None:
    expect_error(
        "10 ASYNC SUB a(x AS NUM AS LONG AS REF) AS NUM AS LONG\n20 RETURN 1\n30 .ENDSUB\n"
        "40 SUB main AS PUBLIC AS VOID\n50 .ENDSUB\n",
        "AS REF",
    )


def test_bare_call_of_async_is_rejected() -> None:
    expect_error(
        "10 ASYNC SUB a() AS NUM AS LONG\n20 RETURN 1\n30 .ENDSUB\n"
        "40 SUB main AS PUBLIC AS VOID\n50 CALL a()\n60 .ENDSUB\n",
        "独立 CALL",
    )


def test_sync_of_normal_sub_is_rejected() -> None:
    expect_error(
        "10 SUB a() AS NUM AS LONG\n20 RETURN 1\n30 .ENDSUB\n"
        "40 SUB main AS PUBLIC AS VOID\n50 DIM x AS NUM AS LONG AS VAR\n60 x = SYNC a()\n70 .ENDSUB\n",
        "ASYNC SUB",
    )


def test_await_of_non_promise_is_rejected() -> None:
    expect_error(
        "10 SUB main AS PUBLIC AS VOID\n20 DIM x AS NUM AS LONG AS VAR\n30 x = 5\n40 x = AWAIT x\n50 .ENDSUB\n",
        "AWAIT",
    )


def test_await_inside_try_is_rejected() -> None:
    expect_error(
        "10 SUB work() AS VOID\n20 RETURN\n30 .ENDSUB\n"
        "40 ASYNC SUB a() AS NUM AS LONG\n50 RETURN 1\n60 .ENDSUB\n"
        "70 ASYNC SUB b() AS NUM AS LONG\n80 DIM e AS ERROR AS VAR\n"
        "90 TRY CALL work() TRACEBACK ERROR AS e\n100 CATCH ANY AS ex\n"
        "110 DIM x AS NUM AS LONG AS VAR\n120 x = AWAIT a()\n130 .ENDTRY\n140 RETURN 0\n150 .ENDSUB\n"
        "160 SUB main AS PUBLIC AS VOID\n170 .ENDSUB\n",
        "TRY 块内",
    )


def test_try_call_of_async_is_rejected() -> None:
    expect_error(
        "10 ASYNC SUB a() AS NUM AS LONG\n20 RETURN 1\n30 .ENDSUB\n"
        "40 SUB main AS PUBLIC AS VOID\n50 DIM e AS ERROR AS VAR\n"
        "60 TRY CALL a() TRACEBACK ERROR AS e\n70 CATCH ANY AS ex\n80 PRINT \"x\"\n90 .ENDTRY\n100 .ENDSUB\n",
        "TRY CALL",
    )


# --- native 后端拒绝 ASYNC SUB（首期不接，纯 Python 校验、不需编译器） ---

def test_native_backend_supports_async() -> None:
    checked = check_program(parse_program(
        "10 ASYNC SUB a() AS NUM AS LONG\n20 RETURN 1\n30 .ENDSUB\n"
        "40 SUB main AS PUBLIC AS VOID\n50 DIM x AS NUM AS LONG AS VAR\n60 x = SYNC a()\n70 .ENDSUB\n"
    ))
    ir = generate_native_llvm_ir(checked)
    assert "define i64 @sa_a_start(" in ir
    assert "define void @sa_a_resume(" in ir
    assert "call void @sa_event_loop_run_until(" in ir


# --- 阶段 1：codegen 结构断言（生成的 C 长成无栈状态机的样子） ---

def test_async_sub_compiles_to_state_machine() -> None:
    """ASYNC SUB 编译成帧结构体 + resume + start 三件套，而非一个直跑函数。

    resume 顶部是 switch(state) 派发 goto，每个 AWAIT 落一个恢复点标签——
    这是无栈协程的骨架，断言它在场就等于断言状态机切分没退化成普通函数。
    """
    c = compile_c(_FIB_SOURCE)
    assert "SaCoro_sa_fib" in c              # 帧结构体
    assert "sa_fib_resume" in c              # 恢复函数
    assert "sa_fib_start" in c               # 启动器
    assert "switch (f->base.state)" in c     # 状态派发
    assert "sa_await_resume_1" in c          # 第 1 个挂起点恢复标签
    assert "sa_await_resume_2" in c          # 第 2 个挂起点
    assert "sa_coro_await(&f->base" in c     # 挂起：登记 waiter 后 return
    assert "sa_promise_fulfill_long" in c    # 终结：把结果交给自己的 promise
    assert "sa_promise_take_long" in c       # 恢复后从子 promise 取结果


def test_await_local_is_hoisted_into_frame() -> None:
    """跨 AWAIT 存活的局部必须提升进帧，用 f-> 访问而非栈变量。

    栈变量在 return 回调度器时就没了，恢复时读到的是垃圾。提升进帧是正确性关键，
    所以断言 a/b 变成 f->sa_a / f->sa_b。
    """
    c = compile_c(_FIB_SOURCE)
    assert "f->sa_a" in c
    assert "f->sa_b" in c


def test_string_result_uses_fulfill_str() -> None:
    """PROMISE OF STRING 走 move：RETURN 端 fulfill_str，接收端 take_str。"""
    c = compile_c(_STRING_SOURCE)
    assert "sa_promise_fulfill_str" in c
    assert "sa_promise_take_str" in c


def test_void_async_uses_fulfill_void() -> None:
    """VOID async 没有结果，用 fulfill_void/take_void 走完挂起—恢复。"""
    c = compile_c(_VOID_SOURCE)
    assert "sa_promise_fulfill_void" in c
    assert "sa_promise_take_void" in c


# --- 阶段 1：端到端编译运行（仅 C 后端；native 由上面的拒绝测试覆盖） ---

@requires_c_compiler
def test_e2e_recursive_async_fib() -> None:
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc")
    with TemporaryDirectory(prefix="sonalgebraic-coro-") as temp:
        proc = _build_and_run(_FIB_SOURCE, temp, "fib")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "55"


@requires_c_compiler
def test_e2e_async_string_move() -> None:
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc")
    with TemporaryDirectory(prefix="sonalgebraic-coro-") as temp:
        proc = _build_and_run(_STRING_SOURCE, temp, "strmove")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "hello from coroutine"


@requires_c_compiler
def test_e2e_void_async_with_bare_await() -> None:
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc")
    with TemporaryDirectory(prefix="sonalgebraic-coro-") as temp:
        proc = _build_and_run(_VOID_SOURCE, temp, "voidawait")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.split() == ["tick", "after", "tick"]


@requires_c_compiler
def test_e2e_async_fib_is_leak_free() -> None:
    """fib(10) 递归展开约 177 个协程帧，退出时必须全部回收。

    这是资源清理融合最狠的压力点：挂起不清、终结才清、清理引用 f-> 帧字段，
    任何一条路径漏 free 都会让 SA_LIVE 非零。-O0 编译避免优化掩盖真实分配。
    """
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc 做 malloc 计数插桩")
    with TemporaryDirectory(prefix="sonalgebraic-coro-") as temp:
        proc = _build_and_run(_FIB_SOURCE, temp, "fib_leak", opt="-O0", leak=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "55"
        assert "SA_LIVE=0" in proc.stderr, f"检测到内存泄漏: {proc.stderr}"


@requires_c_compiler
def test_e2e_async_string_move_is_leak_free() -> None:
    """STRING move 路径无泄漏：strdup 进 slot、take 走、帧局部清理不 double-free。"""
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc 做 malloc 计数插桩")
    with TemporaryDirectory(prefix="sonalgebraic-coro-") as temp:
        proc = _build_and_run(_STRING_SOURCE, temp, "strmove_leak", opt="-O0", leak=True)
        assert proc.returncode == 0, proc.stderr
        assert "SA_LIVE=0" in proc.stderr, f"检测到内存泄漏: {proc.stderr}"


@requires_c_compiler
def test_e2e_async_survives_o2() -> None:
    """-O2 下 resume 的 switch/goto 不可归约控制流 + landing setjmp 不崩。

    这正是 PRELUDE 记录的 MinGW -O2 崩溃场景，用 __builtin_setjmp 避开 SEH。
    """
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc")
    with TemporaryDirectory(prefix="sonalgebraic-coro-") as temp:
        proc = _build_and_run(_FIB_SOURCE, temp, "fib_o2", opt="-O2")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "55"


# --- 阶段 2：异步网络 I/O（SYS.NET 的 *_ASYNC 三件套 + poll 事件循环） ---

# echo server：ACCEPT_ASYNC 等连接、RECV_ASYNC 收、SEND_ASYNC 原样回、关闭。三个 AWAIT
# 全部作用于内置 net async 调用（返回 PROMISE OF X），走的是「登记 fd + poll 就绪 + syscall」
# 那条路，而非协程 resume。端口由测试注入空闲值。
_ECHO_SERVER_TEMPLATE = (
    "10 USE SYS.NET AS N\n"
    "20 ASYNC SUB accept_one(listener AS HANDLE AS TCP_LISTENER) AS VOID\n"
    "30 DIM conn AS HANDLE AS NET_STREAM AS VAR\n"
    "40 DIM msg AS STRING AS VAR\n"
    "50 DIM sent AS NUM AS LONG AS VAR\n"
    "60 DIM ok AS BOOL AS VAR\n"
    "70 conn = AWAIT N.ACCEPT_ASYNC(listener)\n"
    "80 msg = AWAIT N.RECV_ASYNC(conn, 1024)\n"
    "90 sent = AWAIT N.SEND_ASYNC(conn, msg)\n"
    "100 ok = N.STREAM_CLOSE(conn)\n"
    "110 RETURN\n"
    "120 .ENDSUB\n"
    "130 SUB main AS PUBLIC AS VOID\n"
    "140 DIM listener AS HANDLE AS TCP_LISTENER AS VAR\n"
    "150 DIM ok AS BOOL AS VAR\n"
    '160 listener = N.TCP_LISTEN("127.0.0.1", {port}, 4)\n'
    "170 SYNC accept_one(listener)\n"
    "180 ok = N.TCP_LISTENER_CLOSE(listener)\n"
    '190 PRINT "DONE"\n'
    "200 .ENDSUB\n"
    "210 CALL main\n"
    "220 END\n"
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# --- 阶段 2：语义 + 类型 ---

def test_await_of_net_async_call_is_accepted() -> None:
    """AWAIT 作用于 SYS.NET 的 *_ASYNC 调用（内置、返回 PROMISE）必须放行。

    这些不是用户 ASYNC SUB，subs 里查不到——语义层要靠「返回类型是不是 PROMISE」
    接住它们，而非只认用户协程。这条守的就是那个判断顺序不被改回去。
    """
    check_program(parse_program(
        "10 USE SYS.NET AS N\n"
        "20 ASYNC SUB serve(conn AS HANDLE AS NET_STREAM) AS VOID\n"
        "30 DIM msg AS STRING AS VAR\n"
        "40 msg = AWAIT N.RECV_ASYNC(conn, 512)\n"
        "50 RETURN\n"
        "60 .ENDSUB\n"
        "70 SUB main AS PUBLIC AS VOID\n80 .ENDSUB\n"
    ))


def test_net_async_call_type_is_promise_of_result() -> None:
    """ACCEPT_ASYNC 推断成 PROMISE OF NET_STREAM，RECV_ASYNC 成 PROMISE OF STRING。"""
    from sonalgebraic.analysis.typesys import NET_FUNCTIONS

    accept_ret = NET_FUNCTIONS["ACCEPT_ASYNC"][1]
    assert accept_ret.name == "PROMISE"
    assert accept_ret.inner == ast.TypeSpec("HANDLE", "NET_STREAM")
    recv_ret = NET_FUNCTIONS["RECV_ASYNC"][1]
    assert recv_ret.name == "PROMISE" and recv_ret.inner == ast.TypeSpec("STRING")
    send_ret = NET_FUNCTIONS["SEND_ASYNC"][1]
    assert send_ret.name == "PROMISE" and send_ret.inner == ast.TypeSpec("NUM", "LONG")


def test_net_async_triggers_async_runtime_feature() -> None:
    """用到 *_ASYNC 就自动开协程运行时——哪怕程序里没有用户 ASYNC SUB 也一样。"""
    from sonalgebraic.analysis.typesys import runtime_features_for_program

    checked = check_program(parse_program(_ECHO_SERVER_TEMPLATE.format(port=8099)))
    features = runtime_features_for_program(checked.program, checked.uses)
    assert "async" in features
    assert "net" in features


# --- 阶段 2：codegen 结构断言 ---

def test_net_async_emits_promise_primitives() -> None:
    """*_ASYNC 调用翻成 sa_net_*_promise，且注入了 poll 事件循环与 WSAPoll/poll 抽象。"""
    c = compile_c(_ECHO_SERVER_TEMPLATE.format(port=8099))
    assert "sa_net_accept_promise" in c
    assert "sa_net_recv_promise" in c
    assert "sa_net_send_promise" in c
    assert "sa_async_pump_io" in c          # 事件循环的 poll 分支
    assert "sa_async_poll_one" in c         # WSAPoll/poll 抽象


def test_native_backend_supports_async_net_server() -> None:
    """纯 IR 验证网络协程使用共享运行时，不启动监听或发起连接。"""
    checked = check_program(parse_program(_ECHO_SERVER_TEMPLATE.format(port=8099)))
    ir = generate_native_llvm_ir(checked)
    assert "define i64 @sa_accept_one_start(" in ir
    assert "call i64 @sa_net_accept_promise(" in ir
    assert "call i64 @sa_net_recv_promise(" in ir
    assert "call i64 @sa_net_send_promise(" in ir
    assert "call void @sa_coro_await(" in ir


# --- 阶段 2：C 端到端真实 socket；native 见 test_native_async_integration.py ---

@requires_c_compiler
def test_e2e_async_echo_server() -> None:
    """真实 socket 跑通异步三件套：客户端连入 -> 发数据 -> 收到原样回显 -> server 干净退出。

    server 的 stdout 是块缓冲，读不到端口，所以用注入的空闲端口 + 客户端重试连接来同步，
    不依赖 stdout。这正是单线程事件循环在真网络上的验收：accept 挂起 poll、连接唤醒、
    recv 挂起 poll、数据到达、send 回写、协程链终结。
    """
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc")
    port = _free_port()
    with TemporaryDirectory(prefix="sonalgebraic-echo-") as temp:
        src = Path(temp) / "echo.sa"
        src.write_text(_ECHO_SERVER_TEMPLATE.format(port=port), encoding="utf-8")
        exe = _exe_path(Path(temp), "echo")
        build_exe(src, exe, keep_c=False, backend="c")
        proc = subprocess.Popen([str(exe)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            # 重试连接直到 listen 就位（不能读 stdout，块缓冲）
            deadline = time.monotonic() + 20
            conn = None
            last: OSError | None = None
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise AssertionError(f"server 提前退出 rc={proc.returncode}")
                try:
                    conn = socket.create_connection(("127.0.0.1", port), timeout=2)
                    break
                except OSError as exc:
                    last = exc
                    time.sleep(0.1)
            assert conn is not None, f"20 秒内没连上 server: {last}"
            conn.sendall(b"hello async world")
            echo = conn.recv(1024)
            conn.close()
            stdout, stderr = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        assert echo == b"hello async world", f"echo 不匹配: {echo!r}"
        assert proc.returncode == 0, stderr
        assert "DONE" in stdout


def test_async_echo_example_stays_async() -> None:
    """examples/async/echo_server.sa 是异步 I/O 的样板，退化成同步就失去意义。"""
    source = (Path(__file__).resolve().parents[1] / "examples" / "async" / "echo_server.sa").read_text(encoding="utf-8")
    assert "ASYNC SUB" in source
    assert "AWAIT N.ACCEPT_ASYNC" in source
    assert "AWAIT N.RECV_ASYNC" in source
    assert "AWAIT N.SEND_ASYNC" in source


# --- 阶段 3：AWAIT-in-loop（循环体跨挂起，FOR 边界/步长必须提升进帧） ---

# sum_n：FOR i=1 TO n 里每轮 v = AWAIT identity(i) 累加。挂起点在循环体中间，resume 靠
# goto 跳回，上界 n / 步长 1 若留在栈上会被跳过初始化 → 垃圾值 → 死循环。提升进帧才对。
_LOOP_SUM_SOURCE = (
    "10 ASYNC SUB identity(n AS NUM AS LONG) AS NUM AS LONG\n"
    "20 RETURN n\n"
    "30 .ENDSUB\n"
    "40 ASYNC SUB sum_n(n AS NUM AS LONG) AS NUM AS LONG\n"
    "50 DIM total AS NUM AS LONG AS VAR\n"
    "60 DIM i AS NUM AS LONG AS VAR\n"
    "70 DIM v AS NUM AS LONG AS VAR\n"
    "80 total = 0\n"
    "90 FOR i = 1 TO n\n"
    "100 v = AWAIT identity(i)\n"
    "110 total = total + v\n"
    "120 .ENDFOR\n"
    "130 RETURN total\n"
    "140 .ENDSUB\n"
    "150 SUB main AS PUBLIC AS VOID\n"
    "160 DIM r AS NUM AS LONG AS VAR\n"
    "170 r = SYNC sum_n(5)\n"
    "180 PRINT r\n"
    "190 .ENDSUB\n"
    "200 CALL main\n"
    "210 END\n"
)

# 嵌套 FOR：外层(line 100)、内层(line 110)各有独立帧字段（按 line_no）。内层挂起 resume
# 时，内外层的上界都必须还有效——这是「一组字段/循环」不串味的关键。grid(4)=4×4=16。
_LOOP_NESTED_SOURCE = (
    "10 ASYNC SUB identity(n AS NUM AS LONG) AS NUM AS LONG\n"
    "20 RETURN n\n"
    "30 .ENDSUB\n"
    "40 ASYNC SUB grid(n AS NUM AS LONG) AS NUM AS LONG\n"
    "50 DIM total AS NUM AS LONG AS VAR\n"
    "60 DIM i AS NUM AS LONG AS VAR\n"
    "70 DIM j AS NUM AS LONG AS VAR\n"
    "80 DIM v AS NUM AS LONG AS VAR\n"
    "90 total = 0\n"
    "100 FOR i = 1 TO n\n"
    "110 FOR j = 1 TO n\n"
    "120 v = AWAIT identity(1)\n"
    "130 total = total + v\n"
    "140 .ENDFOR\n"
    "150 .ENDFOR\n"
    "160 RETURN total\n"
    "170 .ENDSUB\n"
    "180 SUB main AS PUBLIC AS VOID\n"
    "190 DIM r AS NUM AS LONG AS VAR\n"
    "200 r = SYNC grid(4)\n"
    "210 PRINT r\n"
    "220 .ENDSUB\n"
    "230 CALL main\n"
    "240 END\n"
)


def test_for_loop_bounds_hoisted_into_frame() -> None:
    """async sub 里 FOR 的上界/步长提升进帧字段（sa_floop_end/step_<行号>），而非栈局部。

    循环体含 AWAIT 时 resume 靠 goto 跳进体中间，C 里 goto 跳过声明会跳过初始化——栈上的
    上界/步长成垃圾值使循环失控。断言帧字段在场就等于断言这条正确性关键没退化回栈局部。
    """
    c = compile_c(_LOOP_SUM_SOURCE)
    assert "sa_floop_end_90" in c and "sa_floop_step_90" in c   # 帧结构体里声明
    assert "f->sa_floop_end_90" in c and "f->sa_floop_step_90" in c  # 循环里用帧字段访问


@requires_c_compiler
def test_e2e_async_await_in_loop() -> None:
    """FOR 循环体内 AWAIT，逐轮累加：sum_n(5)=15。修复前上界/步长是栈局部会死循环超时。"""
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc")
    with TemporaryDirectory(prefix="sonalgebraic-coro-") as temp:
        proc = _build_and_run(_LOOP_SUM_SOURCE, temp, "loopsum")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "15"


@requires_c_compiler
def test_e2e_async_nested_await_loops() -> None:
    """嵌套 FOR 里 AWAIT：内外层各有独立帧字段，内层挂起时内外层上界都得有效。grid(4)=16。"""
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc")
    with TemporaryDirectory(prefix="sonalgebraic-coro-") as temp:
        proc = _build_and_run(_LOOP_NESTED_SOURCE, temp, "loopgrid")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "16"


@requires_c_compiler
def test_e2e_async_await_loop_is_leak_free() -> None:
    """循环里反复 AWAIT 拉起/回收子协程帧，50 轮跑完必须零净分配。sum_n(50)=1275。"""
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc 做 malloc 计数插桩")
    src = _LOOP_SUM_SOURCE.replace("SYNC sum_n(5)", "SYNC sum_n(50)")
    with TemporaryDirectory(prefix="sonalgebraic-coro-") as temp:
        proc = _build_and_run(src, temp, "loopleak", opt="-O0", leak=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "1275"
        assert "SA_LIVE=0" in proc.stderr, f"检测到内存泄漏: {proc.stderr}"


# --- 阶段 3：多连接（FOR 循环里连续 AWAIT net async，一个 server 服务多个连接） ---

# serve_n：FOR i=1 TO count 循环里 accept/recv/send 三个 net AWAIT，每轮 conn/msg 帧字段
# 重新赋值，边界 count 跨挂起提升进帧。这是 AWAIT-in-loop 在真实 socket 上的验收。
_MULTI_SERVER_TEMPLATE = (
    "10 USE SYS.NET AS N\n"
    "20 ASYNC SUB serve_n(listener AS HANDLE AS TCP_LISTENER, count AS NUM AS LONG) AS VOID\n"
    "30 DIM conn AS HANDLE AS NET_STREAM AS VAR\n"
    "40 DIM msg AS STRING AS VAR\n"
    "50 DIM sent AS NUM AS LONG AS VAR\n"
    "60 DIM ok AS BOOL AS VAR\n"
    "70 DIM i AS NUM AS LONG AS VAR\n"
    "80 FOR i = 1 TO count\n"
    "90 conn = AWAIT N.ACCEPT_ASYNC(listener)\n"
    "100 msg = AWAIT N.RECV_ASYNC(conn, 1024)\n"
    "110 sent = AWAIT N.SEND_ASYNC(conn, msg)\n"
    "120 ok = N.STREAM_CLOSE(conn)\n"
    "130 .ENDFOR\n"
    "140 RETURN\n"
    "150 .ENDSUB\n"
    "160 SUB main AS PUBLIC AS VOID\n"
    "170 DIM listener AS HANDLE AS TCP_LISTENER AS VAR\n"
    "180 DIM ok AS BOOL AS VAR\n"
    '190 listener = N.TCP_LISTEN("127.0.0.1", {port}, 8)\n'
    "200 SYNC serve_n(listener, 3)\n"
    "210 ok = N.TCP_LISTENER_CLOSE(listener)\n"
    '220 PRINT "DONE"\n'
    "230 .ENDSUB\n"
    "240 CALL main\n"
    "250 END\n"
)


@requires_c_compiler
def test_e2e_async_serves_multiple_connections() -> None:
    """一个 server 用 FOR 循环依次服务 3 个连接：循环体三个 net 挂起点、每轮帧字段重赋值。

    3 个客户端串行连入各发不同数据，各自收到原样回显，最后 server 处理满 3 个干净退出。
    这条守的是「循环里连续 AWAIT net I/O」不退化——多连接场景的最小可验收形态。
    """
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc")
    port = _free_port()
    with TemporaryDirectory(prefix="sonalgebraic-multi-") as temp:
        src = Path(temp) / "multi.sa"
        src.write_text(_MULTI_SERVER_TEMPLATE.format(port=port), encoding="utf-8")
        exe = _exe_path(Path(temp), "multi")
        build_exe(src, exe, keep_c=False, backend="c")
        proc = subprocess.Popen([str(exe)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            payloads = [b"first conn", b"second one", b"third and last"]
            for idx, payload in enumerate(payloads):
                deadline = time.monotonic() + 15
                conn = None
                last: OSError | None = None
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        raise AssertionError(f"server 提前退出 rc={proc.returncode}")
                    try:
                        conn = socket.create_connection(("127.0.0.1", port), timeout=2)
                        break
                    except OSError as exc:
                        last = exc
                        time.sleep(0.05)
                assert conn is not None, f"连接 {idx} 没连上: {last}"
                conn.sendall(payload)
                echo = conn.recv(1024)
                conn.close()
                assert echo == payload, f"连接 {idx} 回显不匹配: {echo!r} != {payload!r}"
            stdout, stderr = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        assert proc.returncode == 0, stderr
        assert "DONE" in stdout, f"stdout={stdout!r}"


# --- 阶段 3：并发 join（p = CALL async 启动得 promise，多个并发在途，AWAIT p 收割） ---

# 两个 work 协程各自 start 进就绪队列，run 里 AWAIT 两个 promise 变量 join。这验证
# 「p = CALL async()」拿到 future + 「AWAIT p」等一个 promise 变量（而非直接 AWAIT 调用）。
# work(10)*... => 20 + 40 = 60。
_JOIN_SOURCE = (
    "10 ASYNC SUB work(n AS NUM AS LONG) AS NUM AS LONG\n"
    "20 RETURN n * 2\n"
    "30 .ENDSUB\n"
    "40 ASYNC SUB run() AS NUM AS LONG\n"
    "50 DIM p1 AS PROMISE OF NUM AS LONG AS VAR\n"
    "60 DIM p2 AS PROMISE OF NUM AS LONG AS VAR\n"
    "70 DIM a AS NUM AS LONG AS VAR\n"
    "80 DIM b AS NUM AS LONG AS VAR\n"
    "90 p1 = CALL work(10)\n"
    "100 p2 = CALL work(20)\n"
    "110 a = AWAIT p1\n"
    "120 b = AWAIT p2\n"
    "130 RETURN a + b\n"
    "140 .ENDSUB\n"
    "150 SUB main AS PUBLIC AS VOID\n"
    "160 DIM r AS NUM AS LONG AS VAR\n"
    "170 r = SYNC run()\n"
    "180 PRINT r\n"
    "190 .ENDSUB\n"
    "200 CALL main\n"
    "210 END\n"
)


def test_promise_call_starts_coroutine_and_awaits_variable() -> None:
    """p = CALL async() 翻成 sa_<sub>_start（启动得 promise），AWAIT p 对 promise 变量挂起。

    这两条合起来就是 join 式并发的地基：先 start 多个协程铺开，再逐个 AWAIT 收割。
    """
    c = compile_c(_JOIN_SOURCE)
    assert "sa_work_start(10)" in c and "sa_work_start(20)" in c  # 两个协程各自启动
    assert "f->base.awaited = f->sa_p1" in c                      # AWAIT 的是 promise 变量本身
    assert "f->base.awaited = f->sa_p2" in c


@requires_c_compiler
def test_e2e_async_concurrent_join() -> None:
    """两个协程并发启动、AWAIT join 收割：run()=60。"""
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc")
    with TemporaryDirectory(prefix="sonalgebraic-coro-") as temp:
        proc = _build_and_run(_JOIN_SOURCE, temp, "join")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "60"


@requires_c_compiler
def test_e2e_async_concurrent_join_is_leak_free() -> None:
    """join 模型无泄漏：p 被 AWAIT 消费后 release，帧终结再 release 同句柄不 double-free。"""
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc 做 malloc 计数插桩")
    with TemporaryDirectory(prefix="sonalgebraic-coro-") as temp:
        proc = _build_and_run(_JOIN_SOURCE, temp, "join_leak", opt="-O0", leak=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "60"
        assert "SA_LIVE=0" in proc.stderr, f"检测到内存泄漏: {proc.stderr}"


# server 先 accept 两条连接、各 spawn 一个 serve 协程（p = CALL serve），再 AWAIT 两个
# promise join。两个 serve 并发在途——一个挂起等 recv 时事件循环去推进另一个。
_CONCURRENT_SERVER_TEMPLATE = (
    "10 USE SYS.NET AS N\n"
    "20 ASYNC SUB serve(conn AS HANDLE AS NET_STREAM) AS NUM AS LONG\n"
    "30 DIM msg AS STRING AS VAR\n"
    "40 DIM sent AS NUM AS LONG AS VAR\n"
    "50 DIM ok AS BOOL AS VAR\n"
    "60 msg = AWAIT N.RECV_ASYNC(conn, 1024)\n"
    "70 sent = AWAIT N.SEND_ASYNC(conn, msg)\n"
    "80 ok = N.STREAM_CLOSE(conn)\n"
    "90 RETURN sent\n"
    "100 .ENDSUB\n"
    "110 ASYNC SUB run(listener AS HANDLE AS TCP_LISTENER) AS VOID\n"
    "120 DIM conn1 AS HANDLE AS NET_STREAM AS VAR\n"
    "130 DIM conn2 AS HANDLE AS NET_STREAM AS VAR\n"
    "140 DIM p1 AS PROMISE OF NUM AS LONG AS VAR\n"
    "150 DIM p2 AS PROMISE OF NUM AS LONG AS VAR\n"
    "160 DIM s1 AS NUM AS LONG AS VAR\n"
    "170 DIM s2 AS NUM AS LONG AS VAR\n"
    "180 conn1 = AWAIT N.ACCEPT_ASYNC(listener)\n"
    "190 p1 = CALL serve(conn1)\n"
    "200 conn2 = AWAIT N.ACCEPT_ASYNC(listener)\n"
    "210 p2 = CALL serve(conn2)\n"
    "220 s1 = AWAIT p1\n"
    "230 s2 = AWAIT p2\n"
    "240 RETURN\n"
    "250 .ENDSUB\n"
    "260 SUB main AS PUBLIC AS VOID\n"
    "270 DIM listener AS HANDLE AS TCP_LISTENER AS VAR\n"
    "280 DIM ok AS BOOL AS VAR\n"
    '290 listener = N.TCP_LISTEN("127.0.0.1", {port}, 8)\n'
    "300 SYNC run(listener)\n"
    "310 ok = N.TCP_LISTENER_CLOSE(listener)\n"
    '320 PRINT "DONE"\n'
    "330 .ENDSUB\n"
    "340 CALL main\n"
    "350 END\n"
)


@requires_c_compiler
def test_e2e_async_concurrent_connections() -> None:
    """两条连接并发在途、交错处理——区别于串行的判定性验收。

    客户端 1 先连但延迟发送，客户端 2 后连立即发收：并发下 serve#1 挂起等 recv 时事件循环
    转去推进 serve#2，客户端 2 在客户端 1 之前拿到回显。串行实现里 server 会卡在 recv#1、
    根本不 accept#2，客户端 2 必然超时——所以这条通过就等于证明了真并发。
    """
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc")
    port = _free_port()
    with TemporaryDirectory(prefix="sonalgebraic-conc-") as temp:
        src = Path(temp) / "conc.sa"
        src.write_text(_CONCURRENT_SERVER_TEMPLATE.format(port=port), encoding="utf-8")
        exe = _exe_path(Path(temp), "conc")
        build_exe(src, exe, keep_c=False, backend="c")
        proc = subprocess.Popen([str(exe)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def wait_connect(deadline: float) -> socket.socket:
            last: OSError | None = None
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise AssertionError(f"server 提前退出 rc={proc.returncode}")
                try:
                    return socket.create_connection(("127.0.0.1", port), timeout=2)
                except OSError as exc:
                    last = exc
                    time.sleep(0.05)
            raise AssertionError(f"没连上: {last}")

        try:
            c1 = wait_connect(time.monotonic() + 15)   # 先连
            time.sleep(0.3)                            # server accept#1 + spawn serve#1（挂起等 recv）
            c2 = wait_connect(time.monotonic() + 15)   # 后连
            time.sleep(0.3)                            # server accept#2 + spawn serve#2
            # 客户端 2 先发收（c1 沉默）：并发下立即拿到，串行下 server 卡在 recv#1 会超时
            c2.sendall(b"fast one")
            c2.settimeout(5)
            echo2 = c2.recv(1024)
            assert echo2 == b"fast one", f"c2 回显不符: {echo2!r}"
            # 再放客户端 1
            c1.sendall(b"slow one")
            c1.settimeout(5)
            echo1 = c1.recv(1024)
            assert echo1 == b"slow one", f"c1 回显不符: {echo1!r}"
            c1.close()
            c2.close()
            stdout, stderr = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        assert proc.returncode == 0, stderr
        assert "DONE" in stdout, f"stdout={stdout!r}"


# --- 阶段 3：drop / 取消 + 挂起协程安全回收 ---

@requires_c_compiler
@pytest.mark.parametrize("kind", ["SYMBOL", "ERROR"])
@pytest.mark.parametrize("lifecycle", ["complete", "caller_free", "drop_unstarted", "cancel_suspended"])
@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_async_managed_param_owns_snapshot(kind: str, lifecycle: str, opt: str) -> None:
    """用真实帧校验深拷贝，并精确停在取消点；净分配检查覆盖帧和整棵符号树。

    C 驱动能原地修改调用方树的叶节点，避免普通赋值更换根指针掩盖浅层 clone，
    也能在子协程尚未运行时断言父帧确实挂起，防止取消用例误走正常完成路径。
    """
    gcc = shutil.which("gcc")
    if gcc is None:
        pytest.skip("需要 gcc 做 malloc 计数插桩")
    source = (
        "10 ASYNC SUB tick() AS VOID\n20 RETURN\n30 .ENDSUB\n"
        f"40 ASYNC SUB worker(value AS {kind}) AS NUM AS LONG\n"
        "50 AWAIT tick()\n60 PRINT value\n70 RETURN 7\n80 .ENDSUB\n"
        "90 SUB main AS PUBLIC AS VOID\n100 .ENDSUB\n110 CALL main\n120 END\n"
    )
    if kind == "SYMBOL":
        setup = """
            SaSymbol original = sa_symbol_new(SA_SYM_OP, NULL, '+',
                sa_symbol_new(SA_SYM_VAR, "x", 0, NULL, NULL),
                sa_symbol_new(SA_SYM_OP, NULL, '*',
                    sa_symbol_new(SA_SYM_CONST, "2", 0, NULL, NULL),
                    sa_symbol_new(SA_SYM_CONST, "3", 0, NULL, NULL)));
        """
        snapshot = "check_tree(frame->sa_value, original);"
        mutate = "original->left->text[0] = 'y'; assert(strcmp(frame->sa_value->left->text, \"x\") == 0);"
        clear = "sa_symbol_free(original); original = NULL;"
        intact = "assert(strcmp(original->left->text, \"x\") == 0);"
        expected = "(x + (2 * 3))"
    else:
        setup = 'SaError original = {42, "ERR_SAMPLE", sa_strdup("original message"), 123, "caller"};'
        snapshot = """
            assert(frame->sa_value.message != original.message);
            assert(strcmp(frame->sa_value.message, original.message) == 0);
            assert(frame->sa_value.err_code == 42);
            assert(strcmp(frame->sa_value.type, "ERR_SAMPLE") == 0);
            assert(frame->sa_value.line_number == 123);
            assert(strcmp(frame->sa_value.sub_name, "caller") == 0);
        """
        mutate = 'original.message[0] = \'X\'; assert(strcmp(frame->sa_value.message, "original message") == 0);'
        clear = "sa_error_clear(&original);"
        intact = 'assert(strcmp(original.message, "original message") == 0);'
        expected = "original message"
    if lifecycle == "caller_free":
        before = mutate + clear
        after = ""
    else:
        before = ""
        after = intact + clear
    if lifecycle in {"complete", "caller_free"}:
        action = """
            sa_event_loop_run_until(promise);
            assert(sa_async_slot(promise)->coro == NULL);
            assert(sa_promise_take_long(promise) == 7);
            sa_promise_release(promise);
        """
    elif lifecycle == "drop_unstarted":
        action = "assert(frame->base.state == 0); sa_promise_release(promise);"
    else:
        action = """
            assert(sa_ready_queue[sa_ready_head] == promise);
            sa_ready_head = (sa_ready_head + 1) % SA_ASYNC_SLOT_COUNT;
            frame->base.resume(&frame->base);
            assert(frame->base.state != 0);
            SaHandle child = frame->base.awaited;
            assert(child != 0);
            assert(sa_async_slot(child)->status == SA_PROMISE_PENDING);
            assert(sa_async_slot(child)->waiter == promise);
            sa_promise_release(promise);
            assert(sa_async_slot(child) == NULL);
        """
    # 只替换入口，保留真实生成的 start / resume / cleanup 和完整运行时。
    c_text = compile_c(source).replace("int main(void) {", "int sa_test_unused_main(void) {", 1)
    tree_check = """
        static void check_tree(SaSymbol copy, SaSymbol original) {
            if (!original) { assert(copy == NULL); return; }
            assert(copy && copy != original);
            assert(copy->kind == original->kind && copy->op == original->op);
            if (original->text) {
                assert(copy->text != original->text);
                assert(strcmp(copy->text, original->text) == 0);
            }
            check_tree(copy->left, original->left);
            check_tree(copy->right, original->right);
        }
    """ if kind == "SYMBOL" else ""
    c_text = _LEAK_SHIM + "\n#include <assert.h>\n" + c_text + tree_check + f"""
        int main(void) {{
            {setup}
            SaHandle promise = sa_worker_start(original);
            SaCoro_sa_worker* frame = (SaCoro_sa_worker*)sa_async_slot(promise)->coro;
            {snapshot}
            {before}
            {action}
            assert(sa_async_slot(promise) == NULL);
            {after}
            /* 在全局退出兜底之前检查，防止兜底回收掩盖当前路径泄漏。 */
            assert(sa__live == 0);
            sa__rep();
            return 0;
        }}
    """
    with build_temp("coro-param-", subdir="coroutine-tests") as temp:
        c_path = Path(temp) / "param.c"
        c_path.write_text(c_text, encoding="utf-8")
        exe = _exe_path(Path(temp), "param")
        compiled = subprocess.run([gcc, str(c_path), opt, "-std=c11", "-o", str(exe), "-lm"], text=True, capture_output=True, timeout=60)
        assert compiled.returncode == 0, compiled.stderr
        proc = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "SA_LIVE=0" in proc.stderr
    if lifecycle in {"complete", "caller_free"}:
        assert expected in proc.stdout
    else:
        assert proc.stdout == ""


# CALL 得到 promise 却不 AWAIT：pending 协程被 release 时，帧内 strdup 的参数/局部得清理，
# 否则泄漏。echo 的 STRING 参数在 start 时 strdup 进帧——run 丢弃 p 就触发这条回收路径。
_DROP_UNSTARTED_SOURCE = (
    "10 ASYNC SUB echo(s AS STRING) AS STRING\n"
    "20 RETURN s\n"
    "30 .ENDSUB\n"
    "40 ASYNC SUB run() AS VOID\n"
    "50 DIM p AS PROMISE OF STRING AS VAR\n"
    '60 p = CALL echo("hello world")\n'
    "70 RETURN\n"
    "80 .ENDSUB\n"
    "90 SUB main AS PUBLIC AS VOID\n"
    "100 SYNC run()\n"
    "110 .ENDSUB\n"
    "120 CALL main\n"
    "130 END\n"
)

# 挂起中的协程被 drop：middle 执行到 AWAIT leaf 挂起（帧内 m 已初始化、正等子 promise），
# run 不 AWAIT 就 RETURN 丢弃它。要求 cleanup 清 m、级联清掉已完成却没人取的 leaf 结果、
# 摘除等待链不 UAF、事件循环不因取消而 hang。settle_soon 让 run 先让出一次好让 middle 挂起。
_DROP_SUSPENDED_SOURCE = (
    "10 ASYNC SUB leaf() AS STRING\n"
    "20 DIM s AS STRING AS VAR\n"
    '30 s = "leaf result here"\n'
    "40 RETURN s\n"
    "50 .ENDSUB\n"
    "60 ASYNC SUB settle_soon() AS NUM AS LONG\n"
    "70 RETURN 1\n"
    "80 .ENDSUB\n"
    "90 ASYNC SUB middle() AS STRING\n"
    "100 DIM m AS STRING AS VAR\n"
    "110 DIM x AS STRING AS VAR\n"
    '120 m = "middle local string"\n'
    "130 x = AWAIT leaf()\n"
    "140 RETURN x\n"
    "150 .ENDSUB\n"
    "160 ASYNC SUB run() AS VOID\n"
    "170 DIM p AS PROMISE OF STRING AS VAR\n"
    "180 DIM tick AS NUM AS LONG AS VAR\n"
    "190 p = CALL middle()\n"
    "200 tick = AWAIT settle_soon()\n"
    "210 RETURN\n"
    "220 .ENDSUB\n"
    "230 SUB main AS PUBLIC AS VOID\n"
    "240 SYNC run()\n"
    "250 .ENDSUB\n"
    "260 CALL main\n"
    "270 END\n"
)


def test_async_sub_emits_cleanup_and_zeroed_frame() -> None:
    """每个 ASYNC SUB 生成一个 cleanup 函数、帧走 calloc 零初始化、start 挂上 cleanup 指针。

    三样合起来才让「挂起中被 drop」的协程安全回收：cleanup 清帧内资源、calloc 保证没执行
    到的局部是 NULL（free(NULL) 安全）、指针让 runtime 在 dispose 时找得到 cleanup。
    """
    c = compile_c(_DROP_UNSTARTED_SOURCE)
    assert "sa_echo_cleanup(SaCoroBase* base)" in c   # 生成了 cleanup 函数
    assert "f->base.cleanup = sa_echo_cleanup;" in c  # start 挂上指针
    assert "calloc(1, sizeof(" in c                   # 帧零初始化


@requires_c_compiler
def test_e2e_async_drop_unstarted_is_leak_free() -> None:
    """CALL 得到 promise 不 AWAIT，未启动协程被 release：strdup 的参数不泄漏。"""
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc 做 malloc 计数插桩")
    with TemporaryDirectory(prefix="sonalgebraic-coro-") as temp:
        proc = _build_and_run(_DROP_UNSTARTED_SOURCE, temp, "drop1", opt="-O0", leak=True)
        assert proc.returncode == 0, proc.stderr
        assert "SA_LIVE=0" in proc.stderr, f"检测到内存泄漏: {proc.stderr}"


@requires_c_compiler
def test_e2e_async_drop_suspended_is_leak_free() -> None:
    """挂起中的协程被 drop：cleanup 清帧内局部 + 级联清掉已完成子 promise 的结果，无泄漏、不 hang。

    subprocess 超时兜底——若取消把等待链留成了死锁，事件循环会 hang，这里会超时失败而非静默。
    """
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc 做 malloc 计数插桩")
    with TemporaryDirectory(prefix="sonalgebraic-coro-") as temp:
        proc = _build_and_run(_DROP_SUSPENDED_SOURCE, temp, "drop2", opt="-O0", leak=True)
        assert proc.returncode == 0, proc.stderr
        assert "SA_LIVE=0" in proc.stderr, f"检测到内存泄漏: {proc.stderr}"


# --- 阶段 3：connect_promise 异步客户端（唯一「先真的 connect() 再 poll writable」的原语）---

# 反过来的方向：前面都是 SA 当 server，这里 SA 当客户端。fetch 协程 AWAIT CONNECT_ASYNC 连上、
# SEND_ASYNC 发、RECV_ASYNC 收回显、关闭、返回。connect 立即成功就 fulfill，EINPROGRESS/
# WSAEWOULDBLOCK 就登记 writable poll，醒后 getsockopt(SO_ERROR) 验成败——这条链只有 connect 有。
_CONNECT_CLIENT_TEMPLATE = (
    "10 USE SYS.NET AS N\n"
    "20 ASYNC SUB fetch(host AS STRING, port AS NUM AS LONG) AS STRING\n"
    "30 DIM conn AS HANDLE AS NET_STREAM AS VAR\n"
    "40 DIM sent AS NUM AS LONG AS VAR\n"
    "50 DIM reply AS STRING AS VAR\n"
    "60 DIM ok AS BOOL AS VAR\n"
    "70 conn = AWAIT N.CONNECT_ASYNC(host, port)\n"
    '80 sent = AWAIT N.SEND_ASYNC(conn, "ping from sa")\n'
    "90 reply = AWAIT N.RECV_ASYNC(conn, 1024)\n"
    "100 ok = N.STREAM_CLOSE(conn)\n"
    "110 RETURN reply\n"
    "120 .ENDSUB\n"
    "130 SUB main AS PUBLIC AS VOID\n"
    "140 DIM reply AS STRING AS VAR\n"
    '150 reply = SYNC fetch("127.0.0.1", {port})\n'
    "160 PRINT reply\n"
    "170 .ENDSUB\n"
    "180 CALL main\n"
    "190 END\n"
)


def _echo_server_thread() -> tuple[socket.socket, threading.Thread, int]:
    """起一个只服务一条连接的 echo server：bind 空闲端口、listen，后台线程 accept 后原样回写。

    保持 listener 打开（不像 _free_port 那样探测完就关），端口不会被别人抢，SA 客户端连得上。
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve() -> None:
        listener.settimeout(30)
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            data = conn.recv(1024)
            if data:
                conn.sendall(data)   # echo

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return listener, thread, listener.getsockname()[1]


def test_connect_async_emits_connect_promise() -> None:
    """CONNECT_ASYNC 翻成 sa_net_connect_promise——客户端侧独有、先 connect 再 poll 的异步原语。"""
    c = compile_c(_CONNECT_CLIENT_TEMPLATE.format(port=9099))
    assert "sa_net_connect_promise" in c


@requires_c_compiler
def test_e2e_async_connect_client() -> None:
    """SA 当客户端：AWAIT N.CONNECT_ASYNC 连到 Python echo server，发一句、收回显、干净返回。

    非阻塞 connect 在 localhost 上可能立即成功、也可能 WSAEWOULDBLOCK 走 poll writable——
    两条路都得把 fd 交给 stream 句柄，跑通即证明 connect 的 fulfill 时机与 fd 归属都对。
    """
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc")
    listener, thread, port = _echo_server_thread()
    try:
        with TemporaryDirectory(prefix="sonalgebraic-connect-") as temp:
            src = Path(temp) / "connect.sa"
            src.write_text(_CONNECT_CLIENT_TEMPLATE.format(port=port), encoding="utf-8")
            exe = _exe_path(Path(temp), "connect")
            build_exe(src, exe, keep_c=False, backend="c")   # driver 编译，Win 下自动链 ws2_32
            proc = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "ping from sa"
    finally:
        thread.join(timeout=5)
        listener.close()


@requires_c_compiler
def test_e2e_async_connect_client_is_leak_free() -> None:
    """connect 客户端全链无泄漏：游离 connect fd 交给句柄后由 STREAM_CLOSE 收、reply 走 string move。"""
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc 做 malloc 计数插桩")
    listener, thread, port = _echo_server_thread()
    try:
        with TemporaryDirectory(prefix="sonalgebraic-connect-") as temp:
            proc = _build_and_run(_CONNECT_CLIENT_TEMPLATE.format(port=port), temp, "connect_leak", opt="-O0", leak=True, net=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "ping from sa"
        assert "SA_LIVE=0" in proc.stderr, f"检测到内存泄漏: {proc.stderr}"
    finally:
        thread.join(timeout=5)
        listener.close()


# --- 阶段 3：模块 + async 跨文件（让 ASYNC SUB 正式进入 SA 的模块 ABI）---

# 模块里的 async sub 编成帧 + resume（模块 TU 内 static）+ start（导出：非 static、带 sa_mod_
# 前缀、进头文件）。主文件靠 sa_runtime.h 顶部的 SA_ENABLE_ASYNC 宏拿到事件循环/promise 声明，
# SaCoroBase 完整定义随共享头进模块 TU 让协程帧编得过。调用语义不变：CALL 得 PROMISE、
# SYNC/AWAIT 取现。
_MODULE_WORKER = (
    "10 ASYNC SUB compute(n AS NUM AS LONG) AS PUBLIC AS NUM AS LONG\n"
    "20 DIM r AS NUM AS LONG AS VAR\n"
    "30 r = n * n\n"
    "40 RETURN r\n"
    "50 .ENDSUB\n"
)

# 主文件零 async sub，纯 SYNC 调模块 async——features 得从模块并入工程，main.c 才拿得到宏。
_APP_SYNC = (
    "10 USE mathworker AS W\n"
    "20 SUB main AS PUBLIC AS VOID\n"
    "30 DIM x AS NUM AS LONG AS VAR\n"
    "40 x = SYNC W.compute(7)\n"
    "50 PRINT x\n"
    "60 .ENDSUB\n"
    "70 CALL main\n"
    "80 END\n"
)

# 主文件 async sub 跨 TU AWAIT 模块 async 两次（3->9->81）：协程链跨编译单元调度。
_APP_AWAIT = (
    "10 USE mathworker AS W\n"
    "20 ASYNC SUB run(n AS NUM AS LONG) AS NUM AS LONG\n"
    "30 DIM a AS NUM AS LONG AS VAR\n"
    "40 DIM b AS NUM AS LONG AS VAR\n"
    "50 a = AWAIT W.compute(n)\n"
    "60 b = AWAIT W.compute(a)\n"
    "70 RETURN b\n"
    "80 .ENDSUB\n"
    "90 SUB main AS PUBLIC AS VOID\n"
    "100 DIM x AS NUM AS LONG AS VAR\n"
    "110 x = SYNC run(3)\n"
    "120 PRINT x\n"
    "130 .ENDSUB\n"
    "140 CALL main\n"
    "150 END\n"
)


def _write_module_project(temp: Path, app_src: str) -> Path:
    """在 temp 放 mathworker.sa（含 async 导出）与引用它的主文件，返回主文件路径。"""
    (temp / "mathworker.sa").write_text(_MODULE_WORKER, encoding="utf-8")
    app = temp / "app.sa"
    app.write_text(app_src, encoding="utf-8")
    return app


def test_module_async_exports_start_symbol() -> None:
    """模块 async sub 作为 start 进入模块 ABI：头文件出现 SaHandle ..._sub_compute_start extern
    声明，且工程 runtime 前缀开了 SA_ENABLE_ASYNC（否则 main.c 看不见事件循环/promise 声明）。"""
    with TemporaryDirectory(prefix="sonalgebraic-modasync-") as temp:
        app = _write_module_project(Path(temp), _APP_SYNC)
        plan = compile_project(app, Path(temp) / "out")
        generated = "\n".join(p.read_text(encoding="utf-8") for p in plan.c_files + plan.headers if p.exists())
    assert "SaHandle sa_mod_mathworker_sub_compute_start(" in generated   # 头文件 extern 声明
    assert "#define SA_ENABLE_ASYNC" in generated                         # 工程开了 async 运行时


@requires_c_compiler
def test_e2e_module_async_sync_call() -> None:
    """主文件零 async sub、SYNC 调模块 async：features 从模块并入、main.c 拿到 SA_ENABLE_ASYNC。"""
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc")
    with TemporaryDirectory(prefix="sonalgebraic-modasync-") as temp:
        app = _write_module_project(Path(temp), _APP_SYNC)
        exe = _exe_path(Path(temp), "app")
        build_exe(app, exe, keep_c=False, backend="c")
        proc = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "49"


@requires_c_compiler
def test_e2e_module_async_cross_await() -> None:
    """主文件 async sub 跨 TU AWAIT 模块 async 两次（3->9->81）：协程链跨编译单元调度、
    模块 TU 的 resume 推进子协程、fulfill 回来唤醒主文件协程。"""
    if shutil.which("gcc") is None:
        pytest.skip("需要 gcc")
    with TemporaryDirectory(prefix="sonalgebraic-modasync-") as temp:
        app = _write_module_project(Path(temp), _APP_AWAIT)
        exe = _exe_path(Path(temp), "app")
        build_exe(app, exe, keep_c=False, backend="c")
        proc = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "81"
