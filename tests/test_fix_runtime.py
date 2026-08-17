"""C 运行时模板（backend/c_runtime.py）的回归测试。

这里的用例分三层：
1. 结构性断言——头文件/实现共用同一段前导、宏定义顺序、导出面一致，这些跨平台恒真。
2. 本机行为验证——用 gcc 把 RUNTIME 拼上一个 main() 编出来真跑，证明修的确实修好了。
3. 交叉编译验证——POSIX 相关的修复在 Windows 上跑不了，用 zig cc 交叉编译到
   linux/macos 来证明「至少编得过」，这是分离编译头文件漂移那一类 bug 的唯一有效防线。
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import re
import shutil
import socket
import subprocess

import pytest

from sonalgebraic.backend import c_runtime
from sonalgebraic.backend.c_runtime import (
    RUNTIME,
    RUNTIME_HEADER,
    RUNTIME_IMPL,
    RUNTIME_PRELUDE,
    RUNTIME_SOURCE,
)

HAS_GCC = shutil.which("gcc") is not None
HAS_ZIG = shutil.which("zig") is not None

requires_gcc = pytest.mark.skipif(not HAS_GCC, reason="需要 MinGW gcc 才能实际编译运行 runtime")
requires_zig = pytest.mark.skipif(not HAS_ZIG, reason="需要 zig 才能交叉编译到 POSIX 目标")

# 打开所有可选模块，保证每条 #ifdef 分支都被编译器看到
ALL_FEATURES = (
    "#define SA_ENABLE_NET\n"
    "#define SA_ENABLE_TLS\n"
    "#define SA_ENABLE_FILE\n"
    "#define SA_ENABLE_BINARY\n"
    "#define SA_ENABLE_LIST\n"
    "#define SA_ENABLE_MAP\n"
    "#define SA_ENABLE_DESKTOP\n"
    "#define SA_ENABLE_GUI\n"
)
# OpenSSL/GTK 的头在交叉编译的 sysroot 里没有，交叉目标只开不依赖外部库的部分
PORTABLE_FEATURES = ALL_FEATURES.replace("#define SA_ENABLE_TLS\n", "")


def work_dir(prefix: str) -> TemporaryDirectory[str]:
    # 和 native 测试一致：Windows 上安全软件容易拦 TEMP 里新生成的 exe，放项目 build 下
    root = Path("build") / "runtime-tests"
    root.mkdir(parents=True, exist_ok=True)
    return TemporaryDirectory(prefix=prefix, dir=root)


# --------------------------------------------------------------------------- 结构


def test_header_and_impl_share_one_prelude() -> None:
    """头文件的前导必须就是 RUNTIME 的前导本体，而不是手抄的第二份。

    历史问题：两份 include 列表手工同步，结果 signal.h / NI_MAXHOST 兜底 / gtk 头
    只留在了 RUNTIME 里，分离编译的 sa_runtime.c 在 POSIX 上直接编不过。
    """
    assert RUNTIME == RUNTIME_PRELUDE + RUNTIME_IMPL
    assert RUNTIME_PRELUDE in RUNTIME_HEADER
    assert RUNTIME_SOURCE == RUNTIME_IMPL.replace("static ", "")


def test_header_carries_every_include_the_impl_needs() -> None:
    needed = set(re.findall(r"#include\s*<([^>]+)>", RUNTIME_PRELUDE))
    have = set(re.findall(r"#include\s*<([^>]+)>", RUNTIME_HEADER))
    assert needed <= have
    # 具体点名这几个，它们正是当初漂移丢掉的
    for header in ("signal.h", "gtk/gtk.h", "direct.h", "poll.h"):
        assert f"<{header}>" in RUNTIME_HEADER, f"RUNTIME_HEADER 缺少 <{header}>"
    assert "#define NI_MAXHOST" in RUNTIME_HEADER
    assert "#define NI_MAXSERV" in RUNTIME_HEADER


def test_file_offset_bits_is_defined_before_any_include() -> None:
    """_FILE_OFFSET_BITS 必须抢在系统头之前，否则 32 位上 off_t 还是 32 位。"""
    macro = RUNTIME_PRELUDE.index("#define _FILE_OFFSET_BITS 64")
    first_include = RUNTIME_PRELUDE.index("#include")
    assert macro < first_include


def test_stricmp_shim_precedes_first_use() -> None:
    shim = RUNTIME.index("#define _stricmp sa_stricmp_ascii")
    first_use = RUNTIME.index("static const char* sa_file_mode(")
    assert shim < first_use


def test_runtime_source_exports_are_all_declared_in_header() -> None:
    """分离编译时实现里的每个 sa_ 函数定义都得在头文件里有声明，否则隐式声明截断返回值。"""
    declared = set(re.findall(r"\b(sa_[a-z0-9_]+)\s*\(", RUNTIME_HEADER))
    # 一部分 pack/unpack 是宏展开出来的，没有字面定义，所以只按名字出现与否判定
    missing = {name for name in declared if name not in RUNTIME_SOURCE}
    assert not missing, f"RUNTIME_HEADER 声明了但实现里找不到: {sorted(missing)}"


def test_static_replacement_only_hits_real_declarations() -> None:
    """RUNTIME_SOURCE 靠去掉 `static ` 生成，这个文本替换不能误伤字符串或注释。"""
    for lineno, line in enumerate(RUNTIME_IMPL.splitlines(), 1):
        for match in re.finditer(r"static ", line):
            head = line[: match.start()].strip()
            assert head == "" or head.startswith("#define"), f"第 {lineno} 行的 `static ` 位置可疑: {line!r}"


def test_str_slice_uses_overflow_safe_comparison() -> None:
    assert "if (count > len - start) count = len - start;" in RUNTIME
    assert "if (start + count > len)" not in RUNTIME


def test_posix_socket_wait_uses_poll_not_select() -> None:
    """POSIX 侧换成 poll：fd >= FD_SETSIZE 时 FD_SET 会栈越界写。"""
    body = RUNTIME[RUNTIME.index("static int sa_net_wait_socket(") :]
    body = body[: body.index("\nstatic ")]
    assert "poll(&pfd, 1, timeout)" in body
    posix_branch = body[body.index("#else") :]
    assert "FD_SET(" not in posix_branch


def test_windows_connect_wait_watches_exceptfds() -> None:
    """Winsock 的非阻塞 connect 失败只进 exceptfds，不看它就得干等满超时。"""
    body = RUNTIME[RUNTIME.index("static int sa_net_wait_socket(") :]
    body = body[: body.index("\nstatic ")]
    win_branch = body[: body.index("#else")]
    assert "except_set" in win_branch
    assert "FD_ISSET(socket_value, &except_set)" in win_branch


def test_http_has_total_timeout_and_size_cap() -> None:
    assert "#define SA_HTTP_MAX_RESPONSE" in RUNTIME
    assert "#define SA_HTTP_TOTAL_TIMEOUT_FACTOR" in RUNTIME
    assert "HTTP total timeout exceeded" in RUNTIME
    # 两个后端都要有上限，不然只堵了一边
    assert RUNTIME.count("HTTP response exceeds the 64 MB limit") == 2


def test_posix_http_rejects_chunked_instead_of_returning_garbage() -> None:
    assert "sa_net_headers_have_chunked" in RUNTIME
    assert "chunked transfer encoding is not supported" in RUNTIME


def test_posix_file_offsets_use_64bit_api() -> None:
    file_section = RUNTIME[RUNTIME.index("#ifdef SA_ENABLE_FILE") :]
    assert "fseeko(slot->stream" in file_section
    assert "ftello(slot->stream" in file_section
    assert "fseek(slot->stream" not in file_section
    assert "ftell(slot->stream" not in file_section


def test_file_write_does_not_flush_every_time() -> None:
    write_fn = RUNTIME[RUNTIME.index("static long long sa_file_write(") :]
    write_fn = write_fn[: write_fn.index("\nstatic ")]
    assert "fflush(" not in write_fn
    assert "pending_write = 1" in write_fn


def test_gui_widget_slots_are_cleared_by_ownership() -> None:
    """父窗口 WM_DESTROY 时子窗还活着，只能用 IsChild 判归属，IsWindow 永远为真。"""
    assert "IsChild(hwnd, sa_gui_widgets[i].hwnd)" in RUNTIME


def test_gui_event_queue_overflow_is_visible() -> None:
    push = RUNTIME[RUNTIME.index("static void sa_gui_push_event(") :]
    push = push[: push.index("\nstatic ")]
    assert "sa_gui_set_error" in push


def test_schannel_enables_revocation_check() -> None:
    assert "SCH_CRED_REVOCATION_CHECK_CHAIN_EXCLUDE_ROOT" in RUNTIME
    # 离线环境不能因为拿不到 CRL 就把所有 HTTPS 全枪毙
    assert "SCH_CRED_IGNORE_NO_REVOCATION_CHECK" in RUNTIME
    assert "SCH_CRED_IGNORE_REVOCATION_OFFLINE" in RUNTIME


def test_symbol_deriv_covers_tan_and_sqrt() -> None:
    deriv = RUNTIME[RUNTIME.index("static SaSymbol sa_symbol_deriv(") :]
    deriv = deriv[: deriv.index("\nstatic ")]
    assert '"TAN"' in deriv
    assert '"SQRT"' in deriv


# --------------------------------------------------------------------------- 本机行为

_BEHAVIOUR_MAIN = r'''
static int sa_probe_fails = 0;
#define CHECK(cond, msg) do { if (!(cond)) { printf("FAIL %s\n", msg); sa_probe_fails++; } } while (0)

int main(int argc, char** argv) {
    /* count 取到 LLONG_MAX：老写法 start + count 会有符号溢出 UB */
    char* s = sa_str_slice("hello", 1, 9223372036854775807LL);
    CHECK(strcmp(s, "ello") == 0, "str_slice LLONG_MAX count");
    free(s);
    s = sa_str_slice("hello", 5, 9223372036854775807LL);
    CHECK(strcmp(s, "") == 0, "str_slice at end");
    free(s);

    /* 去掉每次 fflush 之后，读写混用仍要正确（读转写以前是直接失败的） */
    SaHandle f = sa_file_open("sa_probe.bin", "CREATE");
    CHECK(f != 0, "file open");
    CHECK(sa_file_write(f, "hello") == 5, "write 1");
    CHECK(sa_file_tell(f) == 5, "tell after write");
    CHECK(sa_file_seek(f, 0, "START") == 1, "seek start");
    char* rd = sa_file_read(f, 5);
    CHECK(strcmp(rd, "hello") == 0, "read after write+seek");
    free(rd);
    CHECK(sa_file_write(f, "WORLD") == 5, "write right after read");
    CHECK(sa_file_size(f) == 10, "size");
    CHECK(sa_file_close(f) == 1, "close");
    char* whole = sa_file_read_text("sa_probe.bin");
    CHECK(strcmp(whole, "helloWORLD") == 0, "content on disk");
    free(whole);
    sa_file_delete("sa_probe.bin");

    /* 事件队列满了要丢最旧、留最新，并且能从 LAST_ERROR 看出来 */
    sa_gui_event_head = 0; sa_gui_event_count = 0; sa_gui_clear_error();
    for (long long i = 1; i <= SA_GUI_EVENT_QUEUE + 6; i++) sa_gui_push_event(i);
    CHECK(sa_gui_event_count == SA_GUI_EVENT_QUEUE, "queue capped");
    CHECK(sa_gui_last_error[0] != '\0', "overflow reported");
    CHECK(sa_gui_pop_event() == 7, "oldest dropped");
    long long last = 0;
    while (sa_gui_event_count) last = sa_gui_pop_event();
    CHECK(last == SA_GUI_EVENT_QUEUE + 6, "newest kept");

#ifdef _WIN32
    /* 连接被拒必须靠 exceptfds 立刻返回，而不是耗满 timeout */
    if (argc > 1) {
        long long port = atoll(argv[1]);
        DWORD started = GetTickCount();
        SaHandle h = sa_net_tcp_connect("127.0.0.1", port, 6000);
        DWORD elapsed = GetTickCount() - started;
        printf("CONNECT %lu %llu\n", (unsigned long)elapsed, (unsigned long long)h);
    }
#else
    (void)argc; (void)argv;
#endif

    printf("FAILS %d\n", sa_probe_fails);
    return sa_probe_fails ? 1 : 0;
}
'''


@requires_gcc
def test_runtime_behaviour_on_host() -> None:
    with work_dir("sa-rt-") as temp:
        root = Path(temp)
        source = root / "probe.c"
        source.write_text(ALL_FEATURES + RUNTIME + _BEHAVIOUR_MAIN, encoding="utf-8")
        exe = root / "probe.exe"
        build = subprocess.run(
            ["gcc", "-O1", "-o", str(exe), str(source), "-lws2_32", "-lwinhttp", "-lsecur32", "-luser32", "-lgdi32"],
            capture_output=True,
            text=True,
        )
        assert build.returncode == 0, build.stderr[-4000:]

        # 先占一个端口再关掉，保证 connect 一定被拒（而不是撞上真在监听的服务）
        probe_socket = socket.socket()
        probe_socket.bind(("127.0.0.1", 0))
        closed_port = probe_socket.getsockname()[1]
        probe_socket.close()

        run = subprocess.run([str(exe), str(closed_port)], capture_output=True, text=True, cwd=root, timeout=120)
        assert "FAILS 0" in run.stdout, run.stdout

        match = re.search(r"CONNECT (\d+) (\d+)", run.stdout)
        assert match, run.stdout
        elapsed_ms, handle = int(match.group(1)), int(match.group(2))
        assert handle == 0, "连到已关闭端口不该成功"
        # 修之前这里稳定是 6000ms（select 只等 writefds，被拒的连接一直等到超时）
        assert elapsed_ms < 5000, f"连接被拒耗时 {elapsed_ms}ms，exceptfds 没起作用"


_GUI_MAIN = r'''
static void sa_probe_pump(void) {
    MSG msg;
    for (int i = 0; i < 200; i++) {
        if (!PeekMessageW(&msg, NULL, 0, 0, PM_REMOVE)) break;
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
}

int main(void) {
    SaHandle win = sa_gui_window("probe", 240, 120);
    if (!win) { printf("NOWINDOW\n"); return 0; }
    SaHandle btn = sa_gui_button(win, 1, "click", 10, 10, 80, 24);
    SaHandle lbl = sa_gui_label(win, "hi", 10, 44, 80, 20);
    sa_probe_pump();
    int fails = 0;
    if (sa_gui_widget_hwnd(btn) == NULL) fails++;
    if (!sa_gui_close(win)) fails++;
    sa_probe_pump();
    if (sa_gui_window_hwnd(win) != NULL) { printf("FAIL window slot\n"); fails++; }
    if (sa_gui_widget_hwnd(btn) != NULL) { printf("FAIL button slot\n"); fails++; }
    if (sa_gui_widget_hwnd(lbl) != NULL) { printf("FAIL label slot\n"); fails++; }
    printf("FAILS %d\n", fails);
    return fails ? 1 : 0;
}
'''


@requires_gcc
def test_closing_window_invalidates_its_widget_handles() -> None:
    """关窗后控件句柄必须失效，否则 HWND 被系统复用时 SET_TEXT 会打到别人窗口上。"""
    with work_dir("sa-gui-") as temp:
        root = Path(temp)
        source = root / "gui_probe.c"
        source.write_text("#define SA_ENABLE_GUI\n" + RUNTIME + _GUI_MAIN, encoding="utf-8")
        exe = root / "gui_probe.exe"
        build = subprocess.run(
            ["gcc", "-O1", "-o", str(exe), str(source), "-luser32", "-lgdi32"],
            capture_output=True,
            text=True,
        )
        assert build.returncode == 0, build.stderr[-4000:]
        run = subprocess.run([str(exe)], capture_output=True, text=True, cwd=root, timeout=120)
        if "NOWINDOW" in run.stdout:
            pytest.skip("当前会话没有可用桌面，建不了窗口")
        assert "FAILS 0" in run.stdout, run.stdout


# --------------------------------------------------------------------------- 交叉编译


@requires_zig
@pytest.mark.parametrize("target", ["x86_64-linux-gnu", "x86-linux-gnu", "aarch64-macos"])
def test_split_runtime_compiles_for_posix_targets(target: str) -> None:
    """分离编译产物（sa_runtime.h + sa_runtime.c）必须能在 POSIX 上编过。

    这是头文件漂移那类 bug 唯一能在 Windows 开发机上抓到的手段——单文件模式一直是
    好的，坏的只有模块化构建走的这条路。
    """
    with work_dir("sa-cross-") as temp:
        root = Path(temp)
        (root / "sa_runtime.h").write_text(PORTABLE_FEATURES + RUNTIME_HEADER.strip() + "\n", encoding="utf-8")
        (root / "sa_runtime.c").write_text(
            PORTABLE_FEATURES + '#include "sa_runtime.h"\n\n' + RUNTIME_SOURCE.strip() + "\n", encoding="utf-8"
        )
        (root / "single.c").write_text(
            PORTABLE_FEATURES + RUNTIME + "\nint main(void) { return 0; }\n", encoding="utf-8"
        )
        for name in ("sa_runtime.c", "single.c"):
            result = subprocess.run(
                ["zig", "cc", "-target", target, "-c", str(root / name), "-o", str(root / (name + ".o"))],
                capture_output=True,
                text=True,
                timeout=600,
            )
            assert result.returncode == 0, f"{target} / {name}:\n{result.stderr[-4000:]}"


@requires_zig
def test_module_project_links_on_linux() -> None:
    """真实的用户模块工程（含 SYS.LIST / SYS.FILE / SYS.NET）在 Linux 上能编能链。"""
    from sonalgebraic.packaging.module_compiler import compile_project

    with work_dir("sa-modlink-") as temp:
        root = Path(temp)
        (root / "mathlib.sa").write_text(Path("examples/mathlib.sa").read_text(encoding="utf-8"), encoding="utf-8")
        main = root / "app.sa"
        main.write_text(
            "10 USE MATHLIB AS LIB\n"
            "20 USE SYS.LIST AS L\n"
            "30 USE SYS.FILE AS F\n"
            "40 USE SYS.NET AS N\n"
            "50 DIM xs AS HANDLE AS LIST AS VAR\n"
            "60 DIM ok AS BOOL AS VAR\n"
            "70 SUB main AS PUBLIC AS VOID\n"
            "80 xs = L.NEW()\n"
            "90 ok = L.PUSH(xs, LIB.SCALE + LIB.twice(4.0))\n"
            "100 PRINT L.GET(xs, 0)\n"
            "110 ok = L.CLOSE(xs)\n"
            "120 ok = F.EXISTS(\"nope.txt\")\n"
            "130 PRINT N.URLENCODE(\"a b\")\n"
            "140 .ENDSUB\n"
            "150 CALL main\n"
            "160 END\n",
            encoding="utf-8",
        )
        out = root / "out"
        plan = compile_project(main, out)
        objects = []
        for c_file in plan.c_files:
            obj = out / (c_file.stem + ".o")
            result = subprocess.run(
                ["zig", "cc", "-target", "x86_64-linux-gnu", "-c", str(c_file), "-I", str(out), "-o", str(obj)],
                capture_output=True,
                text=True,
                timeout=600,
            )
            assert result.returncode == 0, f"{c_file.name}:\n{result.stderr[-4000:]}"
            objects.append(str(obj))
        linked = subprocess.run(
            ["zig", "cc", "-target", "x86_64-linux-gnu", *objects, "-lm", "-o", str(out / "app_linux")],
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert linked.returncode == 0, linked.stderr[-4000:]
        assert (out / "app_linux").exists()


def test_module_ships_expected_public_names() -> None:
    """防手滑：拆前导之后这几个名字还得都在。"""
    for name in ("RUNTIME", "RUNTIME_PRELUDE", "RUNTIME_IMPL", "RUNTIME_HEADER", "RUNTIME_SOURCE"):
        assert isinstance(getattr(c_runtime, name), str)
