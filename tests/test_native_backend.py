"""实验性 native/LLVM 后端测试。"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess

from conftest import requires_native_compiler
from sonalgebraic.backend.native import generate_native_llvm_ir
from sonalgebraic.driver.compiler import build_exe, compile_to_native_ir, compile_main_to_native_ir_with_modules
from sonalgebraic.frontend.parser import parse_program
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.packaging.module_compiler import compile_project

import shutil

import pytest

# leak 验证需要 clang + lld：native 的 IR 直接 call @free，宏插桩对它无效，
# 必须用符号级 -Wl,-wrap 同时拦截 IR 与运行时的 malloc/free。
_HAS_CLANG_LLD = shutil.which("clang") is not None and shutil.which("lld-link") is not None
requires_clang_lld = pytest.mark.skipif(not _HAS_CLANG_LLD, reason="leak 验证需要 clang + lld")


def native_temp(prefix: str) -> TemporaryDirectory[str]:
    # Windows 上安全软件容易拦截系统 TEMP 里刚生成的 exe；native e2e 统一放到
    # 项目 build/native-tests 下，和日常构建目录保持一致，减少误杀噪声。
    root = Path("build") / "native-tests"
    root.mkdir(parents=True, exist_ok=True)
    return TemporaryDirectory(prefix=prefix, dir=root)


def compile_native_ir(source: str) -> str:
    return generate_native_llvm_ir(check_program(parse_program(source)))


def _native_live_allocs(temp: Path, source: str) -> int:
    """编译 native 程序并用符号级 -Wl,-wrap 计 malloc/free 净分配。返回退出时存活块数。"""
    from sonalgebraic.backend.c_runtime import RUNTIME_HEADER, RUNTIME_SOURCE

    src = temp / "leak.sa"
    src.write_text(source, encoding="utf-8")
    ir = temp / "leak.ll"
    compile_to_native_ir(src, ir)
    (temp / "rt.c").write_text(RUNTIME_HEADER + "\n" + RUNTIME_SOURCE, encoding="utf-8")
    wrap = (
        "#include <stdio.h>\n#include <stdlib.h>\n"
        "static long sa__live=0;\n"
        "void* __real_malloc(size_t); void __real_free(void*); void* __real_realloc(void*,size_t);\n"
        "void* __wrap_malloc(size_t n){void* p=__real_malloc(n); if(p)sa__live++; return p;}\n"
        "void* __wrap_realloc(void* q,size_t n){ if(!q){void* p=__real_malloc(n); if(p)sa__live++; return p;} return __real_realloc(q,n);}\n"
        "void __wrap_free(void* p){ if(p){sa__live--; __real_free(p);}}\n"
        "static void sa__rep(void){ fprintf(stderr,\"SA_LIVE=%ld\\n\", sa__live);}\n"
        "__attribute__((constructor)) static void sa__init(void){ atexit(sa__rep); }\n"
    )
    (temp / "wrap.c").write_text(wrap, encoding="utf-8")
    exe = temp / "leak.exe"
    cmd = [
        "clang", "-fuse-ld=lld", str(ir), str(temp / "rt.c"), str(temp / "wrap.c"),
        "-O0", "-D_CRT_SECURE_NO_WARNINGS",
        "-Wl,-wrap:malloc", "-Wl,-wrap:free", "-Wl,-wrap:realloc", "-o", str(exe),
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run([str(exe)], text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    line = next(ln for ln in run.stderr.splitlines() if ln.startswith("SA_LIVE="))
    return int(line.split("=", 1)[1])




def test_native_ir_generates_minimal_program() -> None:
    source = """10 DIM x AS NUM AS LONG AS VAR
20 SUB main AS PUBLIC AS VOID
30 x = 40 + 2
40 PRINT x
50 .ENDSUB
60 CALL main
70 END
"""
    ir = compile_native_ir(source)
    assert "define void @sa_main()" in ir
    assert "@sa_x = global i64 0" in ir
    assert "add i64 40, 2" in ir
    assert "call i32 (ptr, ...) @printf(ptr @.sa_fmt_i64" in ir


# GOTO 标签循环。以前这条测试直接拿 examples/hello.sa 当素材，结果那个入门示例
# 被简化成七行之后，测的东西（br / label 的生成）就跟着一起没了。控制流是这里要
# 守的能力，素材得自己带，不能指望入门示例恰好含有循环。
_GOTO_LOOP_SOURCE = """10 DIM counter AS NUM AS LONG AS VAR
20 SUB main AS PUBLIC AS VOID
30 counter = 1
40 ::loop_start
50 IF counter > 5 THEN
60 GOTO ::loop_end
70 END IF
80 PRINT F"Counter is now: {counter}"
90 counter = counter + 1
100 GOTO ::loop_start
110 ::loop_end
120 PRINT "Loop finished."
130 .ENDSUB
140 CALL main
150 END
"""


def test_native_ir_handles_goto_control_flow() -> None:
    with native_temp("sonalgebraic-native-goto-") as temp:
        src = Path(temp) / "goto_loop.sa"
        src.write_text(_GOTO_LOOP_SOURCE, encoding="utf-8")
        ir = compile_to_native_ir(src, Path(temp) / "goto_loop.ll")
        text = ir.read_text(encoding="utf-8")
        assert "br label %sa_label_loop_start" in text
        assert "sa_label_loop_end:" in text
        assert "Counter is now:" in text


@requires_native_compiler
def test_native_backend_hello_runs() -> None:
    """native 后端也能真编真跑仓库入门示例，断言与 C 后端的 e2e 保持对称。"""
    with native_temp("sonalgebraic-native-test-") as temp:
        exe = Path(temp) / "hello_native.exe"
        build_exe(Path("examples/hello.sa"), exe, keep_c=False, backend="native")
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0
        assert proc.stdout.strip() == "Hello World!"


_LOOPS_SOURCE = """10 DIM i AS NUM AS LONG AS VAR
20 DIM total AS NUM AS LONG AS VAR
30 DIM w AS NUM AS LONG AS VAR
40 DIM x AS NUM AS DOUBLE AS VAR
50 SUB main AS PUBLIC AS VOID
60 total = 0
70 FOR i = 1 TO 5
80 total = total + i
90 .ENDFOR
100 PRINT F"sum={total}"
110 FOR i = 6 TO 2 STEP -2
120 PRINT F"down={i}"
130 .ENDFOR
140 w = 3
150 WHILE w > 0
160 PRINT F"w={w}"
170 w = w - 1
180 .ENDWHILE
190 FOR x = 0.0 TO 1.0 STEP 0.5
200 PRINT F"x={x}"
210 .ENDFOR
220 .ENDSUB
230 CALL main
240 END
"""


def test_native_ir_emits_loop_control_flow() -> None:
    ir = compile_native_ir(_LOOPS_SOURCE)
    # FOR：边界/步长在前置块求值，cond 块用 select 兼容正负步长
    assert "select i1" in ir
    assert "sa_for_cond_" in ir
    assert "sa_for_body_" in ir
    assert "sa_for_end_" in ir
    # WHILE：条件块每轮重求值
    assert "sa_while_cond_" in ir
    assert "sa_while_body_" in ir
    # double FOR 走浮点路径
    assert "fadd double" in ir
    # double 常量用 IEEE754 位模式，不会退化成被当作整型的 "0"
    assert "store double 0," not in ir


def test_native_ir_double_literal_uses_bit_pattern() -> None:
    source = """10 DIM x AS NUM AS DOUBLE AS VAR
20 SUB main AS PUBLIC AS VOID
30 x = 0.0
40 .ENDSUB
50 CALL main
60 END
"""
    ir = compile_native_ir(source)
    # 0.0 必须发射成 0x0000000000000000 而非裸 "0"
    assert "store double 0x0000000000000000" in ir


@requires_native_compiler
def test_native_backend_loops_run() -> None:
    with native_temp("sonalgebraic-native-loop-") as temp:
        src = Path(temp) / "loops.sa"
        src.write_text(_LOOPS_SOURCE, encoding="utf-8")
        exe = Path(temp) / "loops.exe"
        build_exe(src, exe, keep_c=False, backend="native")
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout.splitlines()
        assert "sum=15" in out
        assert out.count("down=6") == 1 and "down=4" in out and "down=2" in out
        assert "w=3" in out and "w=1" in out
        assert "x=0" in out and "x=0.5" in out and "x=1" in out


# --- 字符串运行时（owned-heap 模型 + SYS.STRING / NUMBER / STRING 内置） ---

_STRING_SOURCE = """10 USE SYS.STRING AS S
20 DIM greeting AS STRING AS VAR
30 DIM name AS STRING AS VAR
40 SUB shout(msg AS STRING) AS STRING
50 RETURN S.UPPER(msg)
60 .ENDSUB
70 SUB main AS PUBLIC AS VOID
80 name = "world"
90 greeting = S.CONCAT("hello, ", name)
100 PRINT greeting
110 PRINT F"len={S.LENGTH(greeting)} upper={shout(greeting)}"
120 IF name = "world" THEN
130 PRINT "matched"
140 END IF
150 DIM i AS NUM AS LONG AS VAR
160 FOR i = 1 TO 3
170 DIM tmp AS STRING AS VAR
180 tmp = S.CONCAT("item ", S.SLICE("abcde", 0, i))
190 PRINT tmp
200 .ENDFOR
210 .ENDSUB
220 CALL main
230 END
"""


def test_native_ir_string_uses_runtime_calls() -> None:
    ir = compile_native_ir(_STRING_SOURCE)
    # 字符串走 owned-heap：声明初始化为 sa_strdup 空串，赋值走 sa_set_string
    assert "call ptr @sa_strdup(ptr @.sa_empty)" in ir
    assert "call void @sa_set_string(" in ir
    # SYS.STRING 函数 declare + 调用
    assert "declare ptr @sa_str_concat(ptr, ptr)" in ir
    assert "call ptr @sa_str_upper(" in ir
    assert "call i64 @sa_str_length(" in ir
    # 字符串相等用 strcmp
    assert "call i32 @strcmp(" in ir
    # 堆临时串登记 free 清理
    assert "call void @free(" in ir


def test_native_ir_fstring_as_value_uses_builder() -> None:
    # F-string 作为值（赋值给变量）才用 string builder；直接 PRINT F"..." 是逐段 printf
    source = """10 DIM s AS STRING AS VAR
20 DIM n AS NUM AS LONG AS VAR
30 SUB main AS PUBLIC AS VOID
40 n = 7
50 s = F"value={n}"
60 PRINT s
70 .ENDSUB
80 CALL main
90 END
"""
    ir = compile_native_ir(source)
    assert "call void @sa_sb_init(" in ir
    assert "call void @sa_sb_append(" in ir
    assert "call ptr @sa_sb_take(" in ir


def test_native_ir_number_builtin() -> None:
    source = """10 DIM n AS NUM AS DOUBLE AS VAR
20 SUB main AS PUBLIC AS VOID
30 n = NUMBER("3.14")
40 PRINT n
50 .ENDSUB
60 CALL main
70 END
"""
    ir = compile_native_ir(source)
    assert "call double @sa_number(" in ir


def test_native_ir_power_operator_uses_pow() -> None:
    source = """10 DIM n AS NUM AS DOUBLE AS VAR
20 SUB main AS PUBLIC AS VOID
30 n = 2.0 ** 8.0
40 PRINT n
50 .ENDSUB
60 CALL main
70 END
"""
    ir = compile_native_ir(source)
    assert "declare double @pow(double, double)" in ir
    assert "call double @pow(" in ir


@requires_native_compiler
def test_native_backend_strings_run() -> None:
    with native_temp("sonalgebraic-native-str-") as temp:
        src = Path(temp) / "strings.sa"
        src.write_text(_STRING_SOURCE, encoding="utf-8")
        exe = Path(temp) / "strings.exe"
        build_exe(src, exe, keep_c=False, backend="native")
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout.splitlines()
        assert "hello, world" in out
        assert "len=12 upper=HELLO, WORLD" in out
        assert "matched" in out
        assert "item a" in out and "item ab" in out and "item abc" in out


# 字符串确定性内存模型：全局/局部 owned 串、循环内局部串、CONCAT/SLICE 临时串
# 都必须在作用域退出时释放，净分配为 0。这是 native 与 C 后端的内存行为 parity 基线。
_STRING_LEAK_SOURCE = """10 USE SYS.STRING AS S
20 DIM g AS STRING AS VAR
30 SUB main AS PUBLIC AS VOID
40 DIM local AS STRING AS VAR
50 DIM i AS NUM AS LONG AS VAR
60 g = S.CONCAT("a", "b")
70 local = S.UPPER("hello")
80 FOR i = 1 TO 5
90 DIM t AS STRING AS VAR
100 t = S.CONCAT("x", S.SLICE("12345", 0, i))
110 PRINT t
120 .ENDFOR
130 .ENDSUB
140 CALL main
150 END
"""


@requires_clang_lld
def test_native_backend_strings_are_leak_free() -> None:
    with native_temp("sonalgebraic-native-leak-") as temp:
        live = _native_live_allocs(Path(temp), _STRING_LEAK_SOURCE)
        assert live == 0, f"native 字符串内存模型泄漏: SA_LIVE={live}"


# --- 数组 + 指针 ---

_ARRAY_PTR_SOURCE = """10 USE SYS.STRING AS S
20 DIM nums[4] AS NUM AS LONG AS VAR
30 DIM words[3] AS STRING AS VAR
40 DIM scalar AS NUM AS LONG AS VAR
50 SUB bump(p AS PTR TO NUM AS LONG) AS VOID
60 ^p = ^p + 10
70 .ENDSUB
80 SUB main AS PUBLIC AS VOID
90 DIM p AS PTR TO NUM AS LONG AS VAR
100 DIM raw AS CPTR AS VAR
110 nums[0] = 5
120 nums[1] = 7
130 scalar = 100
140 p = @scalar
150 CALL bump(p)
160 PRINT F"n0={nums[0]} n1={nums[1]} scalar={scalar}"
170 words[0] = "alpha"
180 words[1] = S.CONCAT(words[0], " beta")
190 PRINT words[1]
200 raw = CAST CPTR p
210 IF raw <> NULL THEN
220 PRINT "raw ok"
230 END IF
240 p = CAST PTR TO NUM AS LONG raw
250 ^p = ^p + 1
260 PRINT scalar
270 .ENDSUB
280 CALL main
290 END
"""


def test_native_ir_arrays_and_pointers() -> None:
    ir = compile_native_ir(_ARRAY_PTR_SOURCE)
    assert "@sa_nums = global [4 x i64] zeroinitializer" in ir
    assert "@sa_words = global [3 x ptr] zeroinitializer" in ir
    assert "getelementptr inbounds [4 x i64]" in ir
    assert "getelementptr inbounds [3 x ptr]" in ir
    assert "load i64, ptr" in ir
    assert "store i64" in ir
    assert "icmp ne ptr" in ir
    assert "call i32 @strcmp" not in ir  # 指针比较不能误走字符串 strcmp


@requires_native_compiler
def test_native_backend_arrays_and_pointers_run() -> None:
    with native_temp("sonalgebraic-native-arrptr-") as temp:
        src = Path(temp) / "arrptr.sa"
        src.write_text(_ARRAY_PTR_SOURCE, encoding="utf-8")
        exe = Path(temp) / "arrptr.exe"
        build_exe(src, exe, keep_c=False, backend="native")
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == ["n0=5 n1=7 scalar=110", "alpha beta", "raw ok", "111"]


@requires_clang_lld
def test_native_backend_string_arrays_are_leak_free() -> None:
    source = """10 USE SYS.STRING AS S
20 DIM words[4] AS STRING AS VAR
30 SUB main AS PUBLIC AS VOID
40 words[0] = S.CONCAT("a", "b")
50 words[1] = S.UPPER(words[0])
60 words[2] = S.REPLACE("x-y", "-", "+")
70 words[3] = S.LOWER("ZZ")
80 PRINT F"{words[0]} {words[1]} {words[2]} {words[3]}"
90 .ENDSUB
100 CALL main
110 END
"""
    with native_temp("sonalgebraic-native-strarr-leak-") as temp:
        live = _native_live_allocs(Path(temp), source)
        assert live == 0, f"native STRING 数组泄漏: SA_LIVE={live}"


# --- SYMBOL 代数 ---

_SYMBOL_SOURCE = """10 DIM f AS SYMBOL AS VAR
20 DIM df AS SYMBOL AS VAR
30 DIM x AS NUM AS LONG AS VAR
40 DIM at3 AS NUM AS DOUBLE AS VAR
50 SUB main AS PUBLIC AS VOID
60 x = 0
70 f = x * x + 2 * x
80 PRINT F"f(x) = {f}"
90 df = SIMPLIFY(DERIV(f, "x"))
100 PRINT F"f'(x) = {df}"
110 at3 = EVAL(SUBST(f, "x", 3))
120 PRINT F"f(3) = {at3}"
130 f = f * x + 1
140 PRINT F"self = {f}"
150 .ENDSUB
160 CALL main
170 END
"""


def test_native_ir_symbol_uses_runtime_calls() -> None:
    ir = compile_native_ir(_SYMBOL_SOURCE)
    assert "declare ptr @sa_symbol_op" in ir
    assert "call ptr @sa_symbol_var" in ir
    assert "call ptr @sa_symbol_clone" in ir
    assert "call ptr @sa_symbol_deriv" in ir
    assert "call ptr @sa_symbol_simplify" in ir
    assert "call ptr @sa_symbol_subst" in ir
    assert "call double @sa_symbol_eval" in ir
    assert "call void @sa_symbol_free" in ir


@requires_native_compiler
def test_native_backend_symbol_runs() -> None:
    with native_temp("sonalgebraic-native-symbol-") as temp:
        src = Path(temp) / "symbol.sa"
        src.write_text(_SYMBOL_SOURCE, encoding="utf-8")
        exe = Path(temp) / "symbol.exe"
        build_exe(src, exe, keep_c=False, backend="native")
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == [
            "f(x) = ((x * x) + (2 * x))",
            "f'(x) = ((x + x) + 2)",
            "f(3) = 15",
            "self = ((((x * x) + (2 * x)) * x) + 1)",
        ]


@requires_clang_lld
def test_native_backend_symbol_is_leak_free() -> None:
    with native_temp("sonalgebraic-native-symbol-leak-") as temp:
        live = _native_live_allocs(Path(temp), _SYMBOL_SOURCE)
        assert live == 0, f"native SYMBOL 泄漏: SA_LIVE={live}"


# --- GOSUB / INPUT / CLS ---

_GOSUB_SOURCE = """10 DIM total AS NUM AS LONG AS VAR
20 SUB main AS PUBLIC AS VOID
30 total = 1
40 GOSUB ::first
50 PRINT F"after first {total}"
60 GOSUB ::second
70 PRINT F"after second {total}"
80 GOTO ::done
90 ::first
100 total = total + 10
110 GOSUB ::nested
120 total = total + 100
130 RETURN
140 ::nested
150 total = total + 1000
160 RETURN
170 ::second
180 total = total * 2
190 RETURN
200 ::done
210 .ENDSUB
220 CALL main
230 END
"""


def test_native_ir_gosub_input_cls_use_runtime_and_switch() -> None:
    source = """10 USE SYS.IO AS IO
20 DIM name AS STRING AS VAR
30 DIM age AS NUM AS LONG AS VAR
40 SUB main AS PUBLIC AS VOID
50 CLS
60 IO.INPUT "Name: ", name
70 IO.INPUT "Age: ", age
80 GOSUB ::tail
90 GOTO ::done
100 ::tail
110 PRINT F"{name}:{age}"
120 RETURN
130 ::done
140 .ENDSUB
150 CALL main
160 END
"""
    ir = compile_native_ir(source)
    assert "call void @sa_cls()" in ir
    assert "call void @sa_read_line(" in ir
    assert "alloca [4096 x i8]" in ir
    assert "alloca [64 x i64]" in ir
    assert "switch i64" in ir
    assert "sa_gosub_return_80" in ir


@requires_native_compiler
def test_native_backend_gosub_runs() -> None:
    with native_temp("sonalgebraic-native-gosub-") as temp:
        src = Path(temp) / "gosub.sa"
        src.write_text(_GOSUB_SOURCE, encoding="utf-8")
        exe = Path(temp) / "gosub.exe"
        build_exe(src, exe, keep_c=False, backend="native")
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == ["after first 1111", "after second 2222"]


@requires_native_compiler
def test_native_backend_input_runs() -> None:
    source = """10 USE SYS.IO AS IO
20 DIM name AS STRING AS VAR
30 DIM age AS NUM AS LONG AS VAR
40 SUB main AS PUBLIC AS VOID
50 IO.INPUT "Name: ", name
60 IO.INPUT "Age: ", age
70 PRINT F"hello {name} age={age}"
80 .ENDSUB
90 CALL main
100 END
"""
    with native_temp("sonalgebraic-native-input-") as temp:
        src = Path(temp) / "input.sa"
        src.write_text(source, encoding="utf-8")
        exe = Path(temp) / "input.exe"
        build_exe(src, exe, keep_c=False, backend="native")
        proc = subprocess.run([str(exe)], input="Lans\n33\n", text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "Name: Age: hello Lans age=33\n"


# --- TRY / CATCH / THROW ---

_TRY_SOURCE = """10 DIM trap AS ERROR AS VAR
20 SUB risky AS VOID
30 THROW NEW ERR_DEMO, "boom"
40 .ENDSUB
50 SUB main AS PUBLIC AS VOID
60 TRY CALL risky TRACEBACK ERROR AS trap
70 CATCH ERR_DEMO AS e
80 PRINT F"caught {e} / {trap}"
90 .ENDTRY
100 PRINT "after"
110 .ENDSUB
120 CALL main
130 END
"""


_TRY_PASSTHROUGH_SOURCE = """10 DIM trap AS ERROR AS VAR
20 SUB deep AS VOID
30 THROW NEW ERR_DEEP, "deep boom"
40 .ENDSUB
50 SUB middle AS VOID
60 DIM s AS STRING AS VAR
70 DIM expr AS SYMBOL AS VAR
80 s = "middle local"
90 expr = 5 * 5 + 7 * 5
100 CALL deep
110 .ENDSUB
120 SUB main AS PUBLIC AS VOID
130 TRY CALL middle TRACEBACK ERROR AS trap
140 CATCH ERR_DEEP AS e
150 PRINT F"caught {e}"
160 .ENDTRY
170 .ENDSUB
180 CALL main
190 END
"""


def test_native_ir_try_catch_uses_setjmp_runtime() -> None:
    ir = compile_native_ir(_TRY_SOURCE)
    assert "declare ptr @sa_try_push_env()" in ir
    assert "declare i32 @_setjmp(ptr, ptr)" in ir
    assert "call ptr @llvm.frameaddress.p0(i32 0)" in ir
    assert "call void @sa_raise_new" in ir
    assert "call void @sa_throw_dispatch" in ir
    assert "call void @sa_set_error" in ir
    assert "call void @sa_error_clear" in ir


@requires_native_compiler
def test_native_backend_try_catch_runs() -> None:
    with native_temp("sonalgebraic-native-try-") as temp:
        src = Path(temp) / "try.sa"
        src.write_text(_TRY_SOURCE, encoding="utf-8")
        exe = Path(temp) / "try.exe"
        build_exe(src, exe, keep_c=False, backend="native")
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == ["caught boom / boom", "after"]


@requires_native_compiler
def test_native_backend_uncaught_throw_exits_nonzero() -> None:
    source = """10 SUB main AS PUBLIC AS VOID
20 THROW NEW ERR_FAIL, "fail"
30 .ENDSUB
40 CALL main
50 END
"""
    with native_temp("sonalgebraic-native-uncaught-") as temp:
        src = Path(temp) / "uncaught.sa"
        src.write_text(source, encoding="utf-8")
        exe = Path(temp) / "uncaught.exe"
        build_exe(src, exe, keep_c=False, backend="native")
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode != 0
        assert "Uncaught SonAlgebraic error ERR_FAIL" in proc.stderr


@requires_clang_lld
def test_native_backend_throw_passthrough_is_leak_free() -> None:
    with native_temp("sonalgebraic-native-throw-leak-") as temp:
        live = _native_live_allocs(Path(temp), _TRY_PASSTHROUGH_SOURCE)
        assert live == 0, f"native THROW 穿透泄漏: SA_LIVE={live}"


# --- 全局初值 ---

_GLOBAL_INIT_SOURCE = """10 USE SYS.STRING AS S
20 DIM x AS NUM AS LONG AS VAR = 7
30 DIM y AS NUM AS DOUBLE AS VAR = 2.5
40 DIM flag AS BOOL AS VAR = TRUE
50 DIM greeting AS STRING AS VAR = S.CONCAT("hi ", "there")
60 DIM f AS SYMBOL AS VAR = x * x + 1
70 SUB main AS PUBLIC AS VOID
80 PRINT F"x={x} y={y} flag={flag}"
90 PRINT greeting
100 PRINT f
110 .ENDSUB
120 CALL main
130 END
"""


def test_native_ir_global_initializers() -> None:
    ir = compile_native_ir(_GLOBAL_INIT_SOURCE)
    assert "@sa_x = global i64 0" in ir
    assert "@sa_greeting = global ptr @.sa_empty" in ir
    assert "call ptr @sa_str_concat" in ir
    assert "call void @sa_set_string(ptr @sa_greeting" in ir
    assert "call ptr @sa_symbol_op" in ir
    assert "store ptr" in ir and "ptr @sa_f" in ir


@requires_native_compiler
def test_native_backend_global_initializers_run() -> None:
    with native_temp("sonalgebraic-native-global-init-") as temp:
        src = Path(temp) / "global_init.sa"
        src.write_text(_GLOBAL_INIT_SOURCE, encoding="utf-8")
        exe = Path(temp) / "global_init.exe"
        build_exe(src, exe, keep_c=False, backend="native")
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == ["x=7 y=2.5 flag=1", "hi there", "((x * x) + 1)"]


@requires_clang_lld
def test_native_backend_global_initializers_are_leak_free() -> None:
    with native_temp("sonalgebraic-native-global-init-leak-") as temp:
        live = _native_live_allocs(Path(temp), _GLOBAL_INIT_SOURCE)
        assert live == 0, f"native 全局初值泄漏: SA_LIVE={live}"


# --- C FFI ---

_FFI_SOURCE = """10 USEC <stdio.h> AS STDIO
20 DECLARE C SUB STDIO.puts(s AS STRING) AS NUM AS LONG
30 DECLARE C SUB STDIO.strlen(s AS STRING) AS NUM AS LONG
40 DIM n AS NUM AS LONG AS VAR
50 SUB main AS PUBLIC AS VOID
60 CALL STDIO.puts("ffi hello")
70 n = STDIO.strlen("abcd")
80 PRINT F"len={n}"
90 .ENDSUB
100 CALL main
110 END
"""


def test_native_ir_c_ffi_declares_external_functions() -> None:
    ir = compile_native_ir(_FFI_SOURCE)
    assert "declare i64 @puts(ptr)" in ir
    assert "declare i64 @strlen(ptr)" in ir
    assert "call i64 @puts(ptr" in ir
    assert "call i64 @strlen(ptr" in ir


@requires_native_compiler
def test_native_backend_c_ffi_runs() -> None:
    with native_temp("sonalgebraic-native-ffi-") as temp:
        src = Path(temp) / "ffi.sa"
        src.write_text(_FFI_SOURCE, encoding="utf-8")
        exe = Path(temp) / "ffi.exe"
        build_exe(src, exe, keep_c=False, backend="native")
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == ["ffi hello", "len=4"]


# --- 用户模块 ---

_USER_MODULE_MAIN = """10 USE Calc AS C
20 DIM result AS NUM AS LONG AS VAR
30 SUB main AS PUBLIC AS VOID
40 result = C.add(2, 3)
50 PRINT F"sum={result} answer={C.answer}"
60 .ENDSUB
70 CALL main
80 END
"""

_USER_MODULE_CALC = """10 CONST answer AS NUM AS LONG = 42
20 SUB add(a AS NUM AS LONG, b AS NUM AS LONG) AS PUBLIC AS NUM AS LONG
30 RETURN a + b
40 .ENDSUB
"""

_USER_MODULE_BOOL_MAIN = """10 USE Flags AS F
20 DIM result AS BOOL AS VAR
30 SUB main AS PUBLIC AS VOID
40 result = F.from_number(TRUE)
50 PRINT F"result={result}"
60 .ENDSUB
70 CALL main
80 END
"""

_USER_MODULE_BOOL = """10 SUB from_number(enabled AS BOOL) AS PUBLIC AS BOOL
20 IF enabled THEN
30 RETURN 2
40 .ENDIF
50 RETURN 0
60 .ENDSUB
"""


def test_native_ir_declares_user_module_exports() -> None:
    with native_temp("sonalgebraic-native-module-ir-") as temp:
        root = Path(temp)
        main = root / "main.sa"
        main.write_text(_USER_MODULE_MAIN, encoding="utf-8")
        (root / "calc.sa").write_text(_USER_MODULE_CALC, encoding="utf-8")
        plan = compile_project(main, root / "build")
        ir_path = compile_main_to_native_ir_with_modules(main, root / "main.ll", plan)
        ir = ir_path.read_text(encoding="utf-8")
        assert "declare void @sa_mod_calc_init()" in ir
        assert "declare void @sa_mod_calc_free()" in ir
        assert "@sa_mod_calc_const_answer = external global i64" in ir
        assert "declare i64 @sa_mod_calc_sub_add(i64 %sa_a, i64 %sa_b)" in ir
        assert "call void @sa_mod_calc_init()" in ir
        assert "call i64 @sa_mod_calc_sub_add(i64 2, i64 3)" in ir
        assert "load i64, ptr @sa_mod_calc_const_answer" in ir
        assert "call void @sa_mod_calc_free()" in ir


@requires_native_compiler
def test_native_backend_user_module_runs() -> None:
    with native_temp("sonalgebraic-native-module-") as temp:
        root = Path(temp)
        src = root / "main.sa"
        src.write_text(_USER_MODULE_MAIN, encoding="utf-8")
        (root / "calc.sa").write_text(_USER_MODULE_CALC, encoding="utf-8")
        exe = root / "module.exe"
        build_exe(src, exe, keep_c=False, backend="native")
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == ["sum=5 answer=42"]


def test_native_ir_uses_c_bool_abi_for_user_module() -> None:
    with native_temp("sonalgebraic-native-module-bool-ir-") as temp:
        root = Path(temp)
        main = root / "main.sa"
        main.write_text(_USER_MODULE_BOOL_MAIN, encoding="utf-8")
        (root / "flags.sa").write_text(_USER_MODULE_BOOL, encoding="utf-8")
        plan = compile_project(main, root / "build")
        ir_path = compile_main_to_native_ir_with_modules(main, root / "main.ll", plan)
        ir = ir_path.read_text(encoding="utf-8")
        assert "declare i32 @sa_mod_flags_sub_from_number(i32 %sa_enabled)" in ir
        assert "call i32 @sa_mod_flags_sub_from_number(i32" in ir
        assert "icmp ne i32" in ir


@requires_native_compiler
def test_native_backend_user_module_bool_abi_runs() -> None:
    with native_temp("sonalgebraic-native-module-bool-") as temp:
        root = Path(temp)
        main = root / "main.sa"
        main.write_text(_USER_MODULE_BOOL_MAIN, encoding="utf-8")
        (root / "flags.sa").write_text(_USER_MODULE_BOOL, encoding="utf-8")
        exe = root / "module_bool.exe"
        build_exe(main, exe, keep_c=False, backend="native")
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == ["result=1"]


# --- ENTITY ---

_ENTITY_SOURCE = """10 FOR ENTITY AS Vector2D
20 DIM x AS NUM AS DOUBLE AS VAR
30 DIM y AS NUM AS DOUBLE AS VAR
40 .ENDENTITY
50 SUB move(point AS ENTITY AS Vector2D AS REF) AS VOID
60 point.x = point.x + 1.5
70 point.y = point.y + 2.0
80 .ENDSUB
90 SUB main AS PUBLIC AS VOID
100 DIM hero AS ENTITY AS Vector2D AS VAR
110 hero.x = 2.0
120 hero.y = 3.0
130 CALL move(hero)
140 PRINT F"hero=({hero.x}, {hero.y})"
150 .ENDSUB
160 CALL main
170 END
"""

_ENTITY_STRINGS_SOURCE = """10 FOR ENTITY AS NameBox
20 DIM text AS STRING AS VAR
30 .ENDENTITY
40 FOR ENTITY AS Profile
50 DIM name AS ENTITY AS NameBox AS VAR
60 DIM score AS NUM AS LONG AS VAR
70 .ENDENTITY
80 SUB show(item AS ENTITY AS Profile) AS VOID
90 PRINT F"{item.name.text}: {item.score}"
100 .ENDSUB
110 SUB main AS PUBLIC AS VOID
120 DIM first AS ENTITY AS Profile AS VAR
130 DIM second AS ENTITY AS Profile AS VAR
140 first.name.text = "LANS"
150 first.score = 99
160 second = first
170 second.name.text = "SA"
180 second.score = 100
190 CALL show(first)
200 CALL show(second)
210 .ENDSUB
220 CALL main
230 END
"""


def test_native_ir_entity_struct_and_fields() -> None:
    ir = compile_native_ir(_ENTITY_SOURCE)
    assert "%SaEntity_vector2d = type { double, double }" in ir
    assert "getelementptr inbounds %SaEntity_vector2d" in ir
    assert "define void @sa_move(ptr %sa_point)" in ir


@requires_native_compiler
def test_native_backend_entity_runs() -> None:
    with native_temp("sonalgebraic-native-entity-") as temp:
        src = Path(temp) / "entity.sa"
        src.write_text(_ENTITY_SOURCE, encoding="utf-8")
        exe = Path(temp) / "entity.exe"
        build_exe(src, exe, keep_c=False, backend="native")
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == ["hero=(3.5, 5)"]


@requires_native_compiler
def test_native_backend_entity_string_deep_copy_runs() -> None:
    with native_temp("sonalgebraic-native-entity-strings-") as temp:
        src = Path(temp) / "entity_strings.sa"
        src.write_text(_ENTITY_STRINGS_SOURCE, encoding="utf-8")
        exe = Path(temp) / "entity_strings.exe"
        build_exe(src, exe, keep_c=False, backend="native")
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == ["LANS: 99", "SA: 100"]


@requires_clang_lld
def test_native_backend_entity_string_fields_are_leak_free() -> None:
    with native_temp("sonalgebraic-native-entity-leak-") as temp:
        live = _native_live_allocs(Path(temp), _ENTITY_STRINGS_SOURCE)
        assert live == 0, f"native ENTITY 字符串字段泄漏: SA_LIVE={live}"
