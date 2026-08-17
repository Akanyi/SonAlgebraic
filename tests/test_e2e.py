"""端到端测试：真正编译成可执行文件并运行，断言运行时输出。"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import shutil
import subprocess
import sys

import pytest

from conftest import requires_c_compiler
from sonalgebraic.driver.compiler import build_exe
pytestmark = [pytest.mark.e2e, requires_c_compiler]


def _exe_path(temp_dir: Path, stem: str) -> Path:
    return temp_dir / (f"{stem}.exe" if sys.platform == "win32" else stem)


def test_e2e_hello_runs() -> None:
    """仓库里的入门示例能真编真跑。

    断言绑的是 examples/hello.sa 的实际输出，所以改那个示例时这里要一起改——
    这是有意的：入门示例的输出变了，就该有人确认一遍是不是想要的。
    """
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        exe = _exe_path(Path(temp), "hello")
        build_exe(Path("examples/hello.sa"), exe, keep_c=False)
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0
        assert proc.stdout.strip() == "Hello World!"


def test_e2e_entity_strings_run_with_deep_copy() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        exe = _exe_path(Path(temp), "entity_strings")
        build_exe(Path("examples/entity_strings.sa"), exe, keep_c=False)
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0
        assert proc.stdout.splitlines() == ["LANS: 99", "SA: 100"]


def test_e2e_string_array_and_replace() -> None:
    source = """10 USE SYS.STRING AS STR
20 DIM names[3] AS STRING AS VAR
30 DIM i AS NUM AS LONG AS VAR
40 SUB main AS PUBLIC AS VOID
50 names[0] = "alpha"
60 names[1] = STR.UPPER("beta")
70 names[2] = STR.REPLACE("a-b-c", "-", "+")
80 FOR i = 0 TO 2
90 PRINT names[i]
100 .ENDFOR
110 .ENDSUB
120 CALL main
130 END
"""
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "strarr.sa"
        src.write_text(source, encoding="utf-8")
        exe = _exe_path(Path(temp), "strarr")
        build_exe(src, exe, keep_c=False)
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0
        assert proc.stdout.splitlines() == ["alpha", "BETA", "a+b+c"]


def test_e2e_try_catch_and_gosub_with_local_entity_resource() -> None:
    source = """10 DIM trap AS ERROR AS VAR
20 FOR ENTITY AS Box
30 DIM text AS STRING AS VAR
40 .ENDENTITY
50 SUB risky AS VOID
60 THROW NEW ERR_DEMO, "boom"
70 .ENDSUB
80 SUB main AS PUBLIC AS VOID
90 DIM item AS ENTITY AS Box AS VAR
100 item.text = "alive"
110 TRY CALL risky TRACEBACK ERROR AS trap
120 CATCH ERR_DEMO AS e
130 PRINT F"caught {e}"
140 .ENDTRY
150 GOSUB ::tail
160 RETURN
170 ::tail
180 PRINT item.text
190 RETURN
200 .ENDSUB
210 CALL main
220 END
"""
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "try_gosub_entity.sa"
        src.write_text(source, encoding="utf-8")
        exe = _exe_path(Path(temp), "try_gosub_entity")
        build_exe(src, exe, keep_c=False)
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0
        assert proc.stdout.splitlines() == ["caught boom", "alive"]


def test_e2e_gosub_from_catch_inside_loop_survives_o2() -> None:
    # 回归：SUB 含 GOSUB 时，CATCH 的 SaError 必须提升到函数作用域。
    # 否则 GOSUB 的 RETURN goto 跳回 CATCH 块内已失效的自动变量，-O2 下堆损坏崩溃。
    # 同时验证 __builtin_setjmp 路径在循环里反复 longjmp 不炸。
    source = """10 DIM trap AS ERROR AS VAR
20 DIM n AS NUM AS LONG AS VAR
30 SUB risky AS PRIVATE AS VOID
40 THROW NEW ERR_X, "boom"
50 .ENDSUB
60 SUB main AS PUBLIC AS VOID
70 n = 0
80 FOR n = 1 TO 3
90 TRY CALL risky TRACEBACK ERROR AS trap
100 CATCH ERR_ANY AS e
110 PRINT F"caught {e}"
120 GOSUB ::rescue
130 .ENDTRY
140 .ENDFOR
150 PRINT "done"
160 RETURN
170 ::rescue
180 PRINT "rescue"
190 RETURN
200 .ENDSUB
210 CALL main
220 END
"""
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "gosub_catch.sa"
        src.write_text(source, encoding="utf-8")
        exe = _exe_path(Path(temp), "gosub_catch")
        build_exe(src, exe, keep_c=False)
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0
        assert proc.stdout.splitlines() == ["caught boom", "rescue", "caught boom", "rescue", "caught boom", "rescue", "done"]


_THROW_LEAK_SOURCE = """10 DIM trap AS ERROR AS VAR
20 SUB leaky AS PRIVATE AS VOID
30 DIM msg AS STRING AS VAR
40 DIM expr AS SYMBOL AS VAR
50 msg = "local string that must be freed"
60 expr = 3 * 3 + 2 * 3
70 THROW NEW ERR_BOOM, "boom from leaky"
80 .ENDSUB
90 SUB main AS PUBLIC AS VOID
100 DIM i AS NUM AS LONG AS VAR
110 FOR i = 1 TO 3
120 TRY CALL leaky TRACEBACK ERROR AS trap
130 CATCH ERR_ANY AS e
140 PRINT F"caught {e}"
150 .ENDTRY
160 .ENDFOR
170 PRINT "done"
180 .ENDSUB
190 CALL main
200 END
"""


def test_e2e_throw_escape_runs_correctly() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "throwleak.sa"
        src.write_text(_THROW_LEAK_SOURCE, encoding="utf-8")
        exe = _exe_path(Path(temp), "throwleak")
        build_exe(src, exe, keep_c=False)
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0
        assert proc.stdout.splitlines() == ["caught boom from leaky"] * 3 + ["done"]


def test_e2e_throw_escape_is_leak_free() -> None:
    # 用 malloc/free 计数桩验证 THROW 逃逸不泄漏：注入计数宏，程序退出时净分配必须为 0。
    # 需要 gcc 做 malloc 宏插桩（MinGW 上稳定）。
    gcc = shutil.which("gcc")
    if gcc is None:
        pytest.skip("需要 gcc 做 malloc 计数插桩")
    from sonalgebraic.driver.compiler import compile_to_c

    shim = (
        "#include <stdio.h>\n#include <stdlib.h>\n"
        "static long sa__live=0;\n"
        "static void* sa__m(size_t n){void* p=malloc(n); if(p)sa__live++; return p;}\n"
        "static void* sa__r(void* q,size_t n){ if(!q) return sa__m(n); return realloc(q,n);}\n"
        "static void sa__f(void* p){ if(p){sa__live--; free(p);}}\n"
        "static void sa__rep(void){ fprintf(stderr,\"SA_LIVE=%ld\\n\", sa__live);}\n"
        "#define malloc sa__m\n#define realloc sa__r\n#define free sa__f\n"
    )
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "throwleak.sa"
        src.write_text(_THROW_LEAK_SOURCE, encoding="utf-8")
        c_path = Path(temp) / "throwleak.c"
        compile_to_c(src, c_path)
        patched = shim + "\n" + c_path.read_text(encoding="utf-8")
        patched = patched.replace("sa_program_end:", "sa_program_end: atexit(sa__rep);", 1)
        c_path.write_text(patched, encoding="utf-8")
        exe = _exe_path(Path(temp), "throwleak_counted")
        compile_proc = subprocess.run([gcc, str(c_path), "-O0", "-std=c11", "-o", str(exe), "-lm"], text=True, capture_output=True)
        assert compile_proc.returncode == 0, compile_proc.stderr
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0
        assert "SA_LIVE=0" in proc.stderr, f"检测到内存泄漏: {proc.stderr}"


# 异常穿过一个没设 TRY、但持有托管局部资源的中间 SUB：
# deep 抛 -> middle(有 STRING/SYMBOL 局部，无 TRY) -> main(有 TRY 捕获)。
# 中间帧的 per-call landing pad 必须在 longjmp 越过它之前清理本帧资源。
_THROW_PASSTHROUGH_SOURCE = """10 DIM trap AS ERROR AS VAR
20 SUB deep AS PRIVATE AS VOID
30 THROW NEW ERR_DEEP, "deep boom"
40 .ENDSUB
50 SUB middle AS PRIVATE AS VOID
60 DIM s AS STRING AS VAR
70 DIM expr AS SYMBOL AS VAR
80 s = "middle local must not leak when exception passes through"
90 expr = 5 * 5 + 7 * 5
100 CALL deep
110 PRINT "unreachable"
120 .ENDSUB
130 SUB main AS PUBLIC AS VOID
140 DIM k AS NUM AS LONG AS VAR
150 FOR k = 1 TO 3
160 TRY CALL middle TRACEBACK ERROR AS trap
170 CATCH ERR_ANY AS e
180 PRINT F"caught {e}"
190 .ENDTRY
200 .ENDFOR
210 PRINT "done"
220 .ENDSUB
230 CALL main
240 END
"""


def test_e2e_throw_propagates_through_frame_runs_correctly() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "throwpass.sa"
        src.write_text(_THROW_PASSTHROUGH_SOURCE, encoding="utf-8")
        exe = _exe_path(Path(temp), "throwpass")
        build_exe(src, exe, keep_c=False)
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0
        assert proc.stdout.splitlines() == ["caught deep boom"] * 3 + ["done"]


def test_e2e_throw_propagates_through_frame_is_leak_free() -> None:
    # 中间帧 landing pad 的核心验证：异常穿过 middle 时其 STRING/SYMBOL 局部必须被释放，净分配为 0。
    gcc = shutil.which("gcc")
    if gcc is None:
        pytest.skip("需要 gcc 做 malloc 计数插桩")
    from sonalgebraic.driver.compiler import compile_to_c

    shim = (
        "#include <stdio.h>\n#include <stdlib.h>\n"
        "static long sa__live=0;\n"
        "static void* sa__m(size_t n){void* p=malloc(n); if(p)sa__live++; return p;}\n"
        "static void* sa__r(void* q,size_t n){ if(!q) return sa__m(n); return realloc(q,n);}\n"
        "static void sa__f(void* p){ if(p){sa__live--; free(p);}}\n"
        "static void sa__rep(void){ fprintf(stderr,\"SA_LIVE=%ld\\n\", sa__live);}\n"
        "#define malloc sa__m\n#define realloc sa__r\n#define free sa__f\n"
    )
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "throwpass.sa"
        src.write_text(_THROW_PASSTHROUGH_SOURCE, encoding="utf-8")
        c_path = Path(temp) / "throwpass.c"
        compile_to_c(src, c_path)
        patched = shim + "\n" + c_path.read_text(encoding="utf-8")
        patched = patched.replace("sa_program_end:", "sa_program_end: atexit(sa__rep);", 1)
        c_path.write_text(patched, encoding="utf-8")
        exe = _exe_path(Path(temp), "throwpass_counted")
        # 关键：-O2 才能复现优化档下的控制流问题，与生产构建一致。
        compile_proc = subprocess.run([gcc, str(c_path), "-O2", "-std=c11", "-o", str(exe), "-lm"], text=True, capture_output=True)
        assert compile_proc.returncode == 0, compile_proc.stderr
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0
        assert "SA_LIVE=0" in proc.stderr, f"检测到内存泄漏: {proc.stderr}"


# 无人捕获的异常穿过多层持资源的中间帧后 exit(1)：
# uncaught 走 sa_throw_dispatch 的 exit(1) 分支，不经过 sa_program_end，
# 所以既要靠 per-call landing pad 清理每层递归帧的局部，
# 又要靠 dispatch 退出前的 sa_error_clear 释放错误自身的 message，否则净分配不为 0。
_THROW_UNCAUGHT_SOURCE = """10 SUB boom(d AS NUM AS LONG) AS VOID
20 DIM why AS STRING AS VAR
30 why = "deep uncaught"
40 THROW NEW ERR_X, "no catcher anywhere"
50 .ENDSUB
60 SUB chain(d AS NUM AS LONG) AS VOID
70 DIM tag AS STRING AS VAR
80 DIM acc AS SYMBOL AS VAR
90 tag = "frame holds resources"
100 acc = d * d + d
110 IF d <= 0 THEN
120 CALL boom(d)
130 ELSE
140 CALL chain(d - 1)
150 END IF
160 .ENDSUB
170 SUB main AS PUBLIC AS VOID
180 CALL chain(8)
190 PRINT "unreachable"
200 .ENDSUB
210 CALL main
220 END
"""


def test_e2e_throw_uncaught_through_frames_is_leak_free() -> None:
    # uncaught 退出路径的泄漏验证：8 层持资源帧穿透 + exit(1)，净分配必须为 0。
    gcc = shutil.which("gcc")
    if gcc is None:
        pytest.skip("需要 gcc 做 malloc 计数插桩")
    from sonalgebraic.driver.compiler import compile_to_c

    # uncaught 不经 sa_program_end，atexit 注册点放到 main 入口；exit(1) 仍会触发 atexit 回调。
    shim = (
        "#include <stdio.h>\n#include <stdlib.h>\n"
        "static long sa__live=0;\n"
        "static void* sa__m(size_t n){void* p=malloc(n); if(p)sa__live++; return p;}\n"
        "static void* sa__r(void* q,size_t n){ if(!q) return sa__m(n); return realloc(q,n);}\n"
        "static void sa__f(void* p){ if(p){sa__live--; free(p);}}\n"
        "static void sa__rep(void){ fprintf(stderr,\"SA_LIVE=%ld\\n\", sa__live);}\n"
        "#define malloc sa__m\n#define realloc sa__r\n#define free sa__f\n"
    )
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "throwuncaught.sa"
        src.write_text(_THROW_UNCAUGHT_SOURCE, encoding="utf-8")
        c_path = Path(temp) / "throwuncaught.c"
        compile_to_c(src, c_path)
        patched = shim + "\n" + c_path.read_text(encoding="utf-8")
        patched = patched.replace("int main(void) {", "int main(void) { atexit(sa__rep);", 1)
        c_path.write_text(patched, encoding="utf-8")
        exe = _exe_path(Path(temp), "throwuncaught_counted")
        compile_proc = subprocess.run([gcc, str(c_path), "-O2", "-std=c11", "-o", str(exe), "-lm"], text=True, capture_output=True)
        assert compile_proc.returncode == 0, compile_proc.stderr
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 1
        assert "Uncaught SonAlgebraic error ERR_X" in proc.stderr
        assert "SA_LIVE=0" in proc.stderr, f"检测到内存泄漏: {proc.stderr}"


# 从 CATCH 块内部用 GOTO 跳出 .ENDTRY：goto 会跳过块尾的 sa_error_clear(&e)，
# 导致 CATCH 别名持有的 message 泄漏。修复靠：含 GOTO 的 SUB 把 CATCH 变量提升到
# 函数作用域 + SUB 末尾兜底清理（clear 幂等）。循环多轮放大泄漏，最后一轮残留必须被兜底清掉。
_GOTO_OUT_OF_CATCH_SOURCE = """10 DIM trap AS ERROR AS VAR
20 SUB boom AS PRIVATE AS VOID
30 THROW NEW ERR_LOOP, "round failure"
40 .ENDSUB
50 SUB main AS PUBLIC AS VOID
60 DIM n AS NUM AS LONG AS VAR
70 n = 0
80 ::again
90 TRY CALL boom TRACEBACK ERROR AS trap
100 CATCH ERR_ANY AS e
110 PRINT F"caught round {n}: {e}"
120 GOTO ::after
130 .ENDTRY
140 ::after
150 n = n + 1
160 IF n < 5 THEN
170 GOTO ::again
180 END IF
190 PRINT "done"
200 .ENDSUB
210 CALL main
220 END
"""


def test_e2e_goto_out_of_catch_runs_correctly() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "gotocatch.sa"
        src.write_text(_GOTO_OUT_OF_CATCH_SOURCE, encoding="utf-8")
        exe = _exe_path(Path(temp), "gotocatch")
        build_exe(src, exe, keep_c=False)
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0
        assert proc.stdout.splitlines() == [f"caught round {i}: round failure" for i in range(5)] + ["done"]


def test_e2e_goto_out_of_catch_is_leak_free() -> None:
    # GOTO 跳出 CATCH 的泄漏验证：5 轮循环每轮跳过块尾 clear，净分配必须为 0。
    gcc = shutil.which("gcc")
    if gcc is None:
        pytest.skip("需要 gcc 做 malloc 计数插桩")
    from sonalgebraic.driver.compiler import compile_to_c

    shim = (
        "#include <stdio.h>\n#include <stdlib.h>\n"
        "static long sa__live=0;\n"
        "static void* sa__m(size_t n){void* p=malloc(n); if(p)sa__live++; return p;}\n"
        "static void* sa__r(void* q,size_t n){ if(!q) return sa__m(n); return realloc(q,n);}\n"
        "static void sa__f(void* p){ if(p){sa__live--; free(p);}}\n"
        "static void sa__rep(void){ fprintf(stderr,\"SA_LIVE=%ld\\n\", sa__live);}\n"
        "#define malloc sa__m\n#define realloc sa__r\n#define free sa__f\n"
    )
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "gotocatch.sa"
        src.write_text(_GOTO_OUT_OF_CATCH_SOURCE, encoding="utf-8")
        c_path = Path(temp) / "gotocatch.c"
        compile_to_c(src, c_path)
        patched = shim + "\n" + c_path.read_text(encoding="utf-8")
        patched = patched.replace("sa_program_end:", "sa_program_end: atexit(sa__rep);", 1)
        c_path.write_text(patched, encoding="utf-8")
        exe = _exe_path(Path(temp), "gotocatch_counted")
        compile_proc = subprocess.run([gcc, str(c_path), "-O2", "-std=c11", "-o", str(exe), "-lm"], text=True, capture_output=True)
        assert compile_proc.returncode == 0, compile_proc.stderr
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0
        assert "SA_LIVE=0" in proc.stderr, f"检测到内存泄漏: {proc.stderr}"


# 符号变量自引用赋值：wave = wave * t + SIMPLIFY(DERIV(wave, "t"))，通过 SYMBOL REF 参数
# 反复重赋值。修复前 codegen 先 free 旧树再 clone 它（UAF），简单情况靠 UB 侥幸跑通、
# 复杂情况直接段错误并打印 <null-symbol>。
_SYMBOL_SELF_REF_SOURCE = """10 USE SYS.MATH AS M
20 SUB mutate(w AS SYMBOL AS REF) AS VOID
30 DIM t AS NUM AS DOUBLE AS VAR
40 w = w * t + SIMPLIFY(DERIV(w, "t"))
50 .ENDSUB
60 SUB main AS PUBLIC AS VOID
70 DIM t AS NUM AS DOUBLE AS VAR
80 DIM wave AS SYMBOL AS VAR
90 wave = t * t + M.E
100 CALL mutate(wave)
110 CALL mutate(wave)
120 CALL mutate(wave)
130 PRINT F"wave: {wave}"
140 .ENDSUB
150 CALL main
160 END
"""


def test_e2e_symbol_self_reference_does_not_crash() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "symself.sa"
        src.write_text(_SYMBOL_SELF_REF_SOURCE, encoding="utf-8")
        exe = _exe_path(Path(temp), "symself")
        build_exe(src, exe, keep_c=False)
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, f"段错误/异常退出: {proc.returncode}"
        # 修复前会出现已释放节点导致的 <null-symbol>；修复后符号树结构完整
        assert "<null-symbol>" not in proc.stdout
        assert proc.stdout.startswith("wave: ")


def test_e2e_symbol_power_derivative_runs() -> None:
    source = """10 DIM x AS NUM AS DOUBLE AS VAR
20 DIM f AS SYMBOL AS VAR
30 DIM df AS SYMBOL AS VAR
40 DIM v AS NUM AS DOUBLE AS VAR
50 SUB main AS PUBLIC AS VOID
60 x = 0
70 f = x ** 3
80 df = SIMPLIFY(DERIV(f, "x"))
90 v = EVAL(SUBST(df, "x", 2.0))
100 PRINT F"df={df}"
110 PRINT F"v={v}"
120 .ENDSUB
130 CALL main
140 END
"""
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "sympow.sa"
        src.write_text(source, encoding="utf-8")
        exe = _exe_path(Path(temp), "sympow")
        build_exe(src, exe, keep_c=False)
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert "df=" in proc.stdout
        assert "v=12" in proc.stdout


def test_e2e_symbol_variable_power_derivative_runs() -> None:
    source = """10 DIM x AS NUM AS DOUBLE AS VAR
20 DIM f AS SYMBOL AS VAR
30 DIM df AS SYMBOL AS VAR
40 DIM v AS NUM AS DOUBLE AS VAR
50 SUB main AS PUBLIC AS VOID
60 x = 0
70 f = x ** x
80 df = SIMPLIFY(DERIV(f, "x"))
90 v = EVAL(SUBST(df, "x", 2.0))
100 PRINT F"df={df}"
110 PRINT F"v={v}"
120 .ENDSUB
130 CALL main
140 END
"""
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "sympowvar.sa"
        src.write_text(source, encoding="utf-8")
        exe = _exe_path(Path(temp), "sympowvar")
        build_exe(src, exe, keep_c=False)
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert "LOG(x)" in proc.stdout
        assert "v=6.77258872223978" in proc.stdout


def test_e2e_symbol_self_reference_is_leak_free() -> None:
    gcc = shutil.which("gcc")
    if gcc is None:
        pytest.skip("需要 gcc 做 malloc 计数插桩")
    from sonalgebraic.driver.compiler import compile_to_c

    shim = (
        "#include <stdio.h>\n#include <stdlib.h>\n"
        "static long sa__live=0;\n"
        "static void* sa__m(size_t n){void* p=malloc(n); if(p)sa__live++; return p;}\n"
        "static void* sa__r(void* q,size_t n){ if(!q) return sa__m(n); return realloc(q,n);}\n"
        "static void sa__f(void* p){ if(p){sa__live--; free(p);}}\n"
        "static void sa__rep(void){ fprintf(stderr,\"SA_LIVE=%ld\\n\", sa__live);}\n"
        "#define malloc sa__m\n#define realloc sa__r\n#define free sa__f\n"
    )
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "symself.sa"
        src.write_text(_SYMBOL_SELF_REF_SOURCE, encoding="utf-8")
        c_path = Path(temp) / "symself.c"
        compile_to_c(src, c_path)
        patched = shim + "\n" + c_path.read_text(encoding="utf-8")
        patched = patched.replace("sa_program_end:", "sa_program_end: atexit(sa__rep);", 1)
        c_path.write_text(patched, encoding="utf-8")
        exe = _exe_path(Path(temp), "symself_counted")
        compile_proc = subprocess.run([gcc, str(c_path), "-O2", "-std=c11", "-o", str(exe), "-lm"], text=True, capture_output=True)
        assert compile_proc.returncode == 0, compile_proc.stderr
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0
        assert "SA_LIVE=0" in proc.stderr, f"检测到内存泄漏: {proc.stderr}"


# 三重组合：CATCH 块内 GOSUB -> GOSUB 体的 FOR 循环里 GOTO 跳出 -> 落点榨字符串后 RETURN，
# 凭票据跳回 CATCH 块内的返回标签（位于 sa_error_clear 之前，清理不被跳过）。多轮放大验证
# GOSUB 票据栈在 CATCH/GOTO/FOR 交织下平衡、无 use-after-free、无泄漏。
_GOSUB_FROM_CATCH_GOTO_OUT_SOURCE = """5 USE SYS.STRING AS S
10 DIM trap AS ERROR AS VAR
20 SUB boom AS PRIVATE AS VOID
30 THROW NEW ERR_BIG, "forced failure with a long message to allocate heap"
40 .ENDSUB
50 SUB main AS PUBLIC AS VOID
60 DIM obs AS STRING AS VAR
70 DIM idx AS NUM AS LONG AS VAR
80 DIM round AS NUM AS LONG AS VAR
90 obs = "START"
100 FOR round = 1 TO 8
110 TRY CALL boom TRACEBACK ERROR AS trap
120 CATCH ERR_ANY AS e
130 GOSUB ::tunnel
140 .ENDTRY
150 .ENDFOR
160 GOTO ::done
170 ::tunnel
180 FOR idx = 0 TO 3
190 IF idx = 0 THEN
200 GOTO ::horizon
210 END IF
220 .ENDFOR
230 RETURN
240 ::horizon
250 obs = S.REPLACE(S.CONCAT(obs, " (OBSERVED)"), "O", "0")
260 RETURN
270 ::done
280 PRINT F"obs={obs}"
290 .ENDSUB
300 CALL main
310 END
"""


def test_e2e_gosub_from_catch_goto_out_of_loop_runs_correctly() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "gosubcatch.sa"
        src.write_text(_GOSUB_FROM_CATCH_GOTO_OUT_SOURCE, encoding="utf-8")
        exe = _exe_path(Path(temp), "gosubcatch")
        build_exe(src, exe, keep_c=False)
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0
        # 8 轮各追加一次 " (0BSERVED)"（O 被替换成 0）
        assert proc.stdout.strip() == "obs=START" + " (0BSERVED)" * 8


def test_e2e_gosub_from_catch_goto_out_of_loop_is_leak_free() -> None:
    gcc = shutil.which("gcc")
    if gcc is None:
        pytest.skip("需要 gcc 做 malloc 计数插桩")
    from sonalgebraic.driver.compiler import compile_to_c

    shim = (
        "#include <stdio.h>\n#include <stdlib.h>\n"
        "static long sa__live=0;\n"
        "static void* sa__m(size_t n){void* p=malloc(n); if(p)sa__live++; return p;}\n"
        "static void* sa__r(void* q,size_t n){ if(!q) return sa__m(n); return realloc(q,n);}\n"
        "static void sa__f(void* p){ if(p){sa__live--; free(p);}}\n"
        "static void sa__rep(void){ fprintf(stderr,\"SA_LIVE=%ld\\n\", sa__live);}\n"
        "#define malloc sa__m\n#define realloc sa__r\n#define free sa__f\n"
    )
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "gosubcatch.sa"
        src.write_text(_GOSUB_FROM_CATCH_GOTO_OUT_SOURCE, encoding="utf-8")
        c_path = Path(temp) / "gosubcatch.c"
        compile_to_c(src, c_path)
        patched = shim + "\n" + c_path.read_text(encoding="utf-8")
        patched = patched.replace("sa_program_end:", "sa_program_end: atexit(sa__rep);", 1)
        c_path.write_text(patched, encoding="utf-8")
        exe = _exe_path(Path(temp), "gosubcatch_counted")
        compile_proc = subprocess.run([gcc, str(c_path), "-O2", "-std=c11", "-o", str(exe), "-lm"], text=True, capture_output=True)
        assert compile_proc.returncode == 0, compile_proc.stderr
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0
        assert "SA_LIVE=0" in proc.stderr, f"检测到内存泄漏: {proc.stderr}"
