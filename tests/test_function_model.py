"""函数模型：函数引用 PTR TO SUB、callable 实体 NEW SUB、CALLRET、托管 GUI 回调。

两种值的分工：
- `PTR TO SUB(...)` 是函数引用，C 里就是个擦了签名的函数指针（SaSubFn）。它不持有任何东西，
  所以能 `=` / `f=`、不能 `m=`；FFI 边界上它就是 C 函数指针，双向都能过。
- `SUB(...)` 是 callable 实体，`NEW SUB h FROM ref` 生成，带引用计数、按作用域释放，能 `m=`；
  它是 SA 运行时托管的对象，不能过 FFI。
"""
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from conftest import build_temp, compile_user_c, expect_error, requires_gcc, requires_windows
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.backend.c_runtime import RUNTIME
from sonalgebraic.backend.native import generate_native_llvm_ir
from sonalgebraic.core import ast
from sonalgebraic.core.errors import SonCompileError
from sonalgebraic.frontend.parser import parse_program
from test_coroutines import _build_and_run

_FN = "SUB(x AS NUM AS LONG) AS NUM AS LONG"

_HELPERS = (
    "10 SUB add1(x AS NUM AS LONG) AS NUM AS LONG\n20 RETURN x + 1\n30 .ENDSUB\n"
    "40 SUB dbl(x AS NUM AS LONG) AS NUM AS LONG\n50 RETURN x * 2\n60 .ENDSUB\n"
    "70 SUB hello(n AS NUM AS LONG) AS VOID\n80 PRINT n\n90 .ENDSUB\n"
)


def _main(body: str, prefix: str = _HELPERS) -> str:
    return f"{prefix}500 SUB main AS PUBLIC AS VOID\n{body}\n900 .ENDSUB\n910 CALL main\n920 END\n"


def _check(source: str) -> None:
    check_program(parse_program(source))


# --- 解析 ---

def test_ptr_to_sub_type_carries_signature() -> None:
    prog = parse_program(_main(f"510 DIM fp AS PTR TO {_FN} AS VAR"))
    decl = prog.subs[-1].body[0]
    assert isinstance(decl, ast.LocalDeclaration)
    assert decl.type_spec.name == "PTR"
    inner = decl.type_spec.inner
    assert inner is not None and inner.name == "SUB"
    assert inner.inner is not None and inner.inner.subtype == "LONG"
    assert [p.name for p in inner.params or ()] == ["x"]


def test_bare_ptr_to_sub_means_no_args_void() -> None:
    prog = parse_program(_main("510 DIM fp AS PTR TO SUB AS VAR"))
    decl = prog.subs[-1].body[0]
    assert isinstance(decl, ast.LocalDeclaration)
    inner = decl.type_spec.inner
    assert inner is not None and inner.inner is not None
    assert inner.name == "SUB" and inner.inner.name == "VOID" and tuple(inner.params or ()) == ()


def test_sub_param_with_nested_signature_splits_on_top_level_commas() -> None:
    prog = parse_program(f"10 SUB apply(f AS {_FN}, v AS NUM AS LONG) AS NUM AS LONG\n20 CALLRET f(v)\n30 .ENDSUB\n")
    sub = prog.subs[0]
    assert [p.name for p in sub.params] == ["f", "v"]
    assert sub.params[0].type_spec.name == "SUB"
    assert isinstance(sub.body[0], ast.CallRet)


def test_sub_ref_and_new_sub_parse() -> None:
    prog = parse_program(_main("510 NEW SUB h FROM @add1()"))
    stmt = prog.subs[-1].body[0]
    assert isinstance(stmt, ast.NewSub) and stmt.name == "h"
    assert isinstance(stmt.source, ast.SubRef) and stmt.source.name == "add1"


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ("510 NEW SUB h @add1()", "NEW SUB 必须写成"),
        ("510 DIM fp AS PTR TO SUB AS VAR\n520 fp = @add1(1)", "括号里不能写参数"),
        ("510 DIM r AS NUM AS LONG AS VAR\n520 r = CALLRET add1(1)", "CALLRET"),
    ],
)
def test_parse_errors(body: str, needle: str) -> None:
    with pytest.raises(SonCompileError) as excinfo:
        parse_program(_main(body))
    assert needle in str(excinfo.value)


# --- 语义 ---

def test_valid_program_checks() -> None:
    _check(_main(
        f"510 DIM fp AS PTR TO {_FN} AS VAR\n"
        "520 fp = @add1()\n"
        "530 fp f= @dbl()\n"
        "540 NEW SUB h FROM fp\n"
        f"550 DIM k AS {_FN} AS VAR\n"
        "560 k m= h\n"
        "570 PRINT k(1) + fp(2)\n"
        "580 PRINT fp = @add1()\n"
    ))


def test_callret_satisfies_required_return_path() -> None:
    _check(_main("510 PRINT 1", _HELPERS + f"100 SUB apply(f AS {_FN}) AS NUM AS LONG\n110 CALLRET f(1)\n120 .ENDSUB\n"))


@pytest.mark.parametrize(
    ("source", "needle"),
    [
        (_main(f"510 DIM fp AS PTR TO {_FN} AS VAR\n520 fp m= @add1()"), "不能 m= 移动"),
        (_main("510 DIM fp AS PTR TO SUB AS VAR\n520 fp = @add1()"), "函数签名不一致"),
        (_main(f"510 DIM h AS {_FN} AS VAR\n520 h = @add1()"), "NEW SUB 名称 FROM"),
        (_main(f"510 DIM fp AS PTR TO {_FN} AS VAR\n520 PRINT ^fp"), "^ 不能用于函数引用"),
        (_main("510 NEW SUB h FROM @add1()\n520 DIM p AS CPTR AS VAR\n530 p = CAST CPTR (@h)"), "不能对 callable 实体取址"),
        (_main("510 NEW SUB h FROM @add1()\n520 PRINT h"), "不能打印"),
        (_main("510 NEW SUB h FROM @add1()\n520 PRINT h = h"), "函数引用只能做等值比较"),
        (_main(f"510 DIM fp AS PTR TO {_FN} AS VAR\n520 PRINT fp + 1"), "函数引用只能做等值比较"),
        (_main("510 NEW SUB add1 FROM @dbl()"), "不能与 SUB 同名"),
        (_main("510 NEW SUB h FROM @hello()\n520 PRINT h(1)"), "不返回值的 callable 不能作为表达式使用"),
        (_main("510 NEW SUB h FROM @add1()\n520 CALL h(1)"), "带返回值的 callable 必须通过"),
        (_main("510 NEW SUB h FROM @hello()\n520 DIM e AS ERROR AS VAR\n530 TRY CALL h(1) TRACEBACK ERROR AS e\n540 CATCH ERR_ANY AS x\n550 PRINT 1\n560 .ENDTRY"), "TRY CALL 暂不支持"),
        (_main(f"510 DIM a[3] AS PTR TO {_FN} AS VAR"), "数组暂不支持"),
        (_main("510 PRINT 1", _HELPERS + "100 DIM g AS PTR TO SUB(x AS NUM AS LONG) AS NUM AS LONG AS VAR = @add1()\n"), "全局变量的初值不能是函数引用"),
        (_main("510 NEW SUB h FROM @nothing()"), "找不到可取引用的 SUB 或 C 函数"),
        (_main("510 NEW SUB h FROM @add1()\n520 NEW SUB k FROM h"), "NEW SUB 的来源必须是函数引用"),
    ],
)
def test_semantic_errors(source: str, needle: str) -> None:
    expect_error(source, needle)


def test_callret_rules() -> None:
    expect_error(_main("510 PRINT 1", _HELPERS + "100 SUB f AS NUM AS LONG\n110 CALLRET add1(1)\n120 .ENDSUB\n"), "请直接 CALL 后 RETURN")
    expect_error(
        _main("510 PRINT 1", _HELPERS + "100 SUB f(g AS SUB(n AS NUM AS LONG) AS VOID) AS NUM AS LONG\n110 CALLRET g(1)\n120 .ENDSUB\n"),
        "g 却不返回值",
    )
    expect_error(
        _main("510 PRINT 1", _HELPERS + f"100 SUB f(g AS {_FN}) AS NUM AS LONG\n110 CALLRET g(1)\n120 PRINT 2\n130 .ENDSUB\n"),
        "CALLRET 之后的代码不可达",
    )


def test_async_rejects_new_sub_callret_and_refs() -> None:
    expect_error(
        _main("510 PRINT 1", _HELPERS + "100 ASYNC SUB job AS VOID\n110 NEW SUB h FROM @hello()\n120 .ENDSUB\n"),
        "ASYNC SUB 里暂不支持 NEW SUB",
    )
    expect_error(
        _main("510 NEW SUB h FROM @job()", _HELPERS + "100 ASYNC SUB job AS VOID\n110 .ENDSUB\n"),
        "不能对 ASYNC SUB 取函数引用",
    )


def test_moved_callable_cannot_be_called() -> None:
    expect_error(
        _main(f"510 NEW SUB h FROM @add1()\n520 DIM k AS {_FN} AS VAR\n530 k m= h\n540 PRINT h(1)"),
        "变量已被 m= 移走: h",
    )


# --- FFI：PTR TO SUB 就是 C 函数指针，callable 不能过边界 ---

_CB_H = r"""#ifndef CB_H
#define CB_H
static long long cb_apply(long long (*f)(long long), long long v) { return f(v); }
static long long cb_triple(long long x) { return x * 3; }
static long long (*cb_get_triple(void))(long long) { return cb_triple; }
static void cb_set(long long (**out)(long long)) { *out = cb_triple; }
static int cb_is_triple(long long (*f)(long long)) { return f == cb_triple; }
static const char* cb_name(void) { return "cb"; }
#endif
"""

_FFI_DECLS = (
    '1 USEC "cb.h" AS CB\n'
    f"2 DECLARE C SUB CB.cb_apply(f AS PTR TO {_FN}, v AS NUM AS LONG) AS NUM AS LONG\n"
    "3 DECLARE C SUB CB.cb_triple(x AS NUM AS LONG) AS NUM AS LONG\n"
    f"4 DECLARE C SUB CB.cb_get_triple() AS PTR TO {_FN}\n"
    f"5 DECLARE C SUB CB.cb_set(out AS PTR TO {_FN} AS REF) AS VOID\n"
    f"6 DECLARE C SUB CB.cb_is_triple(f AS PTR TO {_FN}) AS BOOL\n"
    "7 DECLARE C SUB CB.cb_name() AS STRING\n"
)

_FFI_SOURCE = _main(
    f"510 DIM fp AS PTR TO {_FN} AS VAR\n"
    "520 PRINT CB.cb_apply(@add1(), 10)\n"
    "530 fp = @CB.cb_triple()\n"
    "540 PRINT fp(5)\n"
    "550 PRINT CB.cb_apply(fp, 7)\n"
    "560 fp = CB.cb_get_triple()\n"
    "570 PRINT fp(2)\n"
    "580 fp = NULL\n"
    "590 CALL CB.cb_set(fp)\n"
    "600 PRINT fp(4)\n"
    "610 PRINT CB.cb_is_triple(fp)\n"
    "620 PRINT CB.cb_is_triple(@add1())\n"
    "630 NEW SUB h FROM fp\n"
    "640 PRINT h(100)",
    _FFI_DECLS + _HELPERS,
)


def test_ffi_passes_function_refs_through_void_star() -> None:
    c_text = compile_user_c(_FFI_SOURCE)
    # 转换点只有一处：实参 (void*)，REF 实参 (void*)&，C 返回值擦回 SaSubFn
    assert "cb_apply((void*)(((SaSubFn)sa_add1)), 10)" in c_text
    assert "cb_set((void*)&(sa_fp))" in c_text
    assert "((SaSubFn)cb_get_triple())" in c_text
    assert "sa_fp = ((SaSubFn)cb_triple);" in c_text


def test_ffi_rejects_callable_and_managed_c_returns() -> None:
    expect_error(
        '1 USEC "cb.h" AS CB\n2 DECLARE C SUB CB.run(f AS SUB) AS VOID\n' + _main("510 PRINT 1"),
        "不能是 callable 实体",
    )
    expect_error(
        '1 USEC "cb.h" AS CB\n2 DECLARE C SUB CB.run(f AS PTR TO SUB(g AS SUB) AS VOID) AS VOID\n' + _main("510 PRINT 1"),
        "不能是 callable 实体",
    )
    expect_error(
        _main("510 DIM fp AS PTR TO SUB AS STRING AS VAR\n520 fp = @CB.cb_name()", _FFI_DECLS + _HELPERS),
        "C 返回值的所有权不归 SA 管",
    )


@requires_gcc
def test_ffi_callbacks_run() -> None:
    with build_temp("fm-ffi-", "function-model-tests") as temp:
        (Path(temp) / "cb.h").write_text(_CB_H, encoding="utf-8")
        result = _build_and_run(_FFI_SOURCE, temp, "ffi", leak=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["11", "15", "21", "6", "12", "1", "0", "300"]
    assert "SA_LIVE=0" in result.stderr


# --- C 后端生成 ---

def test_codegen_shapes() -> None:
    c_text = compile_user_c(_main(
        f"510 DIM fp AS PTR TO {_FN} AS VAR\n"
        "520 fp = @add1()\n"
        "530 PRINT fp(1)\n"
        "540 NEW SUB h FROM fp\n"
        "550 PRINT h(2)"
    ))
    assert "SaSubFn sa_fp = NULL;" in c_text
    assert "((long long (*)(long long))sa_sub_check(sa_fp, 530, \"main\"))(1)" in c_text
    assert "SaCallable* sa_h = sa_callable_new(sa_fp);" in c_text
    assert "((long long (*)(long long))sa_callable_fn(sa_h, 550, \"main\"))(2)" in c_text
    assert "sa_callable_release(sa_h);" in c_text


def test_callable_param_is_retained_on_entry() -> None:
    c_text = compile_user_c(_main("510 PRINT 1", _HELPERS + f"100 SUB apply(f AS {_FN}) AS NUM AS LONG\n110 CALLRET f(1)\n120 .ENDSUB\n"))
    assert "sa_f = sa_callable_retain(sa_f);" in c_text
    # CALLRET：调用包在异常落地垫里，结果先落到外层变量，清理完本帧再返回
    body = c_text[c_text.index("static long long sa_apply(SaCallable* sa_f) {"):]
    body = body[: body.index("\n}\n")]
    assert body.index("SA_SETJMP") < body.index("sa_callable_fn(sa_f, 110")
    assert body.rindex("sa_callable_release(sa_f);") > body.index("sa_try_top--;")
    assert "return sa_tmp_" in body


# --- 端到端（带泄漏计数） ---

_E2E = (
    _HELPERS
    + "100 SUB greet(name AS STRING) AS STRING\n110 RETURN F\"hi {name}\"\n120 .ENDSUB\n"
    + f"130 SUB apply(f AS {_FN}, v AS NUM AS LONG) AS NUM AS LONG\n140 CALLRET f(v)\n150 .ENDSUB\n"
    + f"160 SUB make(p AS PTR TO {_FN}) AS {_FN}\n170 NEW SUB h FROM p\n180 RETURN h\n190 .ENDSUB\n"
    + "200 SUB shout(s AS STRING) AS STRING\n210 DIM tmp AS STRING AS VAR\n220 tmp = F\"{s}!\"\n"
    + "230 DIM gs AS PTR TO SUB(name AS STRING) AS STRING AS VAR\n240 gs = @greet()\n250 CALLRET gs(tmp)\n260 .ENDSUB\n"
)


@requires_gcc
def test_function_refs_and_callables_run_without_leaks() -> None:
    source = _main(
        f"510 DIM fp AS PTR TO {_FN} AS VAR\n"
        "520 DIM r AS NUM AS LONG AS VAR\n"
        "530 fp = @add1()\n"
        "540 r = fp(41)\n"
        "550 PRINT r\n"
        "560 fp f= @dbl()\n"
        "570 PRINT fp(21)\n"
        "580 NEW SUB h FROM @add1()\n"
        "590 r = CALL apply(h, 9)\n"
        "600 PRINT r\n"
        f"610 DIM g AS {_FN} AS VAR\n"
        "620 g = CALL make(@dbl())\n"
        "630 PRINT g(5)\n"
        f"640 DIM k AS {_FN} AS VAR\n"
        "650 k m= g\n"
        "660 PRINT k(6)\n"
        "670 DIM gs AS PTR TO SUB(name AS STRING) AS STRING AS VAR\n"
        "680 gs = @greet()\n"
        "690 PRINT gs(\"bob\")\n"
        "700 NEW SUB gg FROM gs\n"
        "710 PRINT gg(\"amy\")\n"
        "720 PRINT shout(\"yo\")\n"
        "730 PRINT fp = @dbl()",
        _E2E,
    )
    with build_temp("fm-e2e-", "function-model-tests") as temp:
        result = _build_and_run(source, temp, "fm", leak=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["42", "42", "10", "10", "12", "hi bob", "hi amy", "hi yo!", "1"]
    assert "SA_LIVE=0" in result.stderr


_ENTITY_AND_NULL = (
    "10 FOR ENTITY AS Button\n20 DIM label AS STRING AS VAR\n30 DIM onPress AS SUB(n AS NUM AS LONG) AS VOID AS VAR\n40 .ENDENTITY\n"
    "50 SUB hello(n AS NUM AS LONG) AS VOID\n60 PRINT F\"hello {n}\"\n70 .ENDSUB\n"
    "80 SUB callNull AS VOID\n90 DIM fp AS PTR TO SUB AS VAR\n100 DIM s AS STRING AS VAR\n110 s = \"leak?\"\n120 CALL fp()\n130 .ENDSUB\n"
    "140 SUB press(b AS ENTITY AS Button) AS VOID\n150 CALL b.onPress(7)\n160 .ENDSUB\n"
    "170 SUB nullCallable AS VOID\n180 DIM k AS SUB(n AS NUM AS LONG) AS VOID AS VAR\n190 DIM s AS STRING AS VAR\n200 s = \"x\"\n210 CALL k(1)\n220 .ENDSUB\n"
    "230 DIM gh AS SUB(n AS NUM AS LONG) AS VOID AS VAR\n240 DIM trap AS ERROR AS VAR\n"
)


@requires_gcc
def test_callable_fields_globals_and_null_calls() -> None:
    source = _main(
        "510 DIM b AS ENTITY AS Button AS VAR\n"
        "520 DIM c AS ENTITY AS Button AS VAR\n"
        "530 NEW SUB h FROM @hello()\n"
        "540 b.label = \"ok\"\n"
        "550 b.onPress = h\n"
        "560 c = b\n"
        "570 CALL press(c)\n"
        "580 CALL c.onPress(8)\n"
        "590 gh = h\n"
        "600 CALL gh(9)\n"
        "610 TRY CALL callNull TRACEBACK ERROR AS trap\n"
        "620 CATCH ERR_NULL_CALL AS e\n"
        "630 PRINT F\"caught: {e}\"\n"
        "640 .ENDTRY\n"
        "650 TRY CALL nullCallable TRACEBACK ERROR AS trap\n"
        "660 CATCH ERR_ANY AS e\n"
        "670 PRINT F\"caught2: {e}\"\n"
        "680 .ENDTRY",
        _ENTITY_AND_NULL,
    )
    with build_temp("fm-ent-", "function-model-tests") as temp:
        result = _build_and_run(source, temp, "fment", leak=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "hello 7",
        "hello 8",
        "hello 9",
        "caught: call through a NULL function reference",
        "caught2: call through a NULL callable",
    ]
    # 空调用抛出时，落地垫要把 callNull / nullCallable 里的局部 STRING 一起释放
    assert "SA_LIVE=0" in result.stderr


# --- GUI 回调 ---

_GUI = (
    "10 USE SYS.GUI AS G\n20 DIM win AS HANDLE AS WINDOW AS VAR\n30 DIM btn AS HANDLE AS WIDGET AS VAR\n"
    "40 DIM lbl AS HANDLE AS WIDGET AS VAR\n50 DIM ok AS BOOL AS VAR\n"
    "60 SUB onClick(id AS NUM AS LONG) AS VOID\n70 PRINT F\"clicked {id}\"\n80 .ENDSUB\n"
)
_GUI_MAIN = _main(
    "510 win = G.WINDOW(\"Demo\", 300, 120)\n"
    "520 btn = G.BUTTON(win, 1, \"OK\", 10, 44, 60, 26)\n"
    "530 lbl = G.LABEL(win, \"hi\", 10, 10, 60, 20)\n"
    "540 NEW SUB h FROM @onClick()\n"
    "550 ok = G.ON_CLICK(btn, h)\n"
    "560 PRINT ok\n"
    "570 ok = G.ON_CLICK(lbl, h)\n"
    "580 PRINT ok\n"
    "590 PRINT G.LAST_ERROR()\n"
    "600 ok = G.CLOSE(win)\n"
    "610 ok = G.RUN()\n"
    "620 PRINT ok",
    _GUI,
)


def test_gui_callbacks_map_to_runtime() -> None:
    c_text = compile_user_c(_GUI_MAIN)
    assert "sa_gui_on_click(sa_btn, sa_h)" in c_text
    assert "sa_gui_run()" in c_text


def test_gui_handler_signature_is_checked() -> None:
    expect_error(
        _GUI + "90 SUB bad AS VOID\n100 .ENDSUB\n" + _main("510 NEW SUB h FROM @bad()\n520 ok = G.ON_CLICK(btn, h)", ""),
        "函数签名不一致",
    )


@requires_gcc
@requires_windows
def test_gui_run_returns_after_windows_close_and_releases_handlers() -> None:
    # 窗口在 RUN 之前就关了：RUN 取到关窗事件立即返回，不会卡在事件循环里；
    # 控件随窗口销毁时槽位 release 掉 callable，泄漏计数归零
    with build_temp("fm-gui-", "function-model-tests") as temp:
        from test_coroutines import _LEAK_SHIM, compile_c

        c_text = _LEAK_SHIM + "\n" + compile_c(_GUI_MAIN)
        c_text = c_text.replace("sa_program_end:", "sa_program_end: atexit(sa__rep);", 1)
        c_path = Path(temp) / "gui.c"
        c_path.write_text(c_text, encoding="utf-8")
        exe = Path(temp) / "gui.exe"
        build = subprocess.run(["gcc", str(c_path), "-O2", "-std=c11", "-o", str(exe), "-lm", "-luser32", "-lgdi32"], text=True, capture_output=True)
        assert build.returncode == 0, build.stderr[-3000:]
        run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["1", "0", "ON_CLICK only applies to BUTTON widgets", "1"]
    assert "SA_LIVE=0" in run.stderr


_GUI_DISPATCH_MAIN = r"""
static long long sa_probe_hits = 0;
static long long sa_probe_last = 0;
static void sa_probe_handler(long long id) { sa_probe_hits++; sa_probe_last = id; }

int main(void) {
    SaCallable* h = sa_callable_new((SaSubFn)sa_probe_handler);
    sa_gui_widgets[3].control_id = 5;
    sa_callable_set(&sa_gui_widgets[3].on_click, h);
    sa_callable_release(h);
    sa_gui_push_event(4);
    sa_gui_push_event(5);
    sa_gui_push_event(0);
    int ok = sa_gui_run();
    printf("%d %lld %lld %ld\n", ok, sa_probe_hits, sa_probe_last, sa_gui_widgets[3].on_click->refs);
    return 0;
}
"""


@requires_gcc
@requires_windows  # 派发循环在 Win32 分支里；没有 GTK 的 POSIX 上 GUI 是桩
def test_gui_run_dispatches_queued_clicks_to_matching_button() -> None:
    with build_temp("fm-guid-", "function-model-tests") as temp:
        source = Path(temp) / "probe.c"
        source.write_text("#define SA_ENABLE_GUI\n" + RUNTIME + _GUI_DISPATCH_MAIN, encoding="utf-8")
        exe = Path(temp) / "probe.exe"
        build = subprocess.run(["gcc", "-O1", "-o", str(exe), str(source), "-luser32", "-lgdi32"], capture_output=True, text=True)
        assert build.returncode == 0, build.stderr[-3000:]
        run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    # 没挂回调的 id 4 被跳过，id 5 派发一次；派发期间多持的那份计数已还回去
    assert run.stdout.split() == ["1", "1", "5", "1"]


_GUI_DISPATCH_CLEANUP_MAIN = r"""
#include <assert.h>
static SaCallable* sa_probe_borrowed;
static char* sa_probe_message;
static int sa_probe_hits;

static void sa_probe_handler(long long id) {
    assert(id == 5);
    assert(sa_try_top == 3);
    assert(sa_probe_borrowed->refs == 2);
    sa_probe_hits++;
    if (SA_PROBE_DETACH) {
        /* 模拟关窗清槽位：此后唯一的所有者应当是 RUN 的临时引用。 */
        sa_callable_set(&sa_gui_widgets[3].on_click, NULL);
        sa_gui_widgets[3].control_id = 0;
        assert(sa_probe_borrowed->refs == 1);
        assert(sa__live == 1);
    }
    if (SA_PROBE_THROW) {
        sa_raise_new("ERR_GUI_PROBE", "original callback error", 731, "onClick");
        sa_probe_message = sa_current_error.message;
        sa_throw_dispatch();
    }
}

static void sa_probe_run_once(void) {
    sa_probe_borrowed = sa_callable_new(SA_PROBE_NULL ? NULL : (SaSubFn)sa_probe_handler);
    sa_gui_widgets[3].control_id = 5;
    sa_callable_set(&sa_gui_widgets[3].on_click, sa_probe_borrowed);
    sa_callable_release(sa_probe_borrowed);
    sa_gui_push_event(4);
    sa_gui_push_event(5);
    sa_gui_push_event(0);
    sa_try_push_env();
    if (SA_SETJMP(sa_try_stack[sa_try_top - 1].env) == 0) {
        assert(sa_gui_run() == 1);
        assert(!SA_PROBE_THROW && !SA_PROBE_NULL);
        assert(sa_try_top == 2);
        sa_try_pop();
    } else {
        /* 落到调用方时，RUN 的清理帧必须已经退栈。 */
        assert(SA_PROBE_THROW || SA_PROBE_NULL);
        assert(sa_try_top == 2);
        sa_try_pop();
        if (SA_PROBE_NULL) {
            assert(sa_current_error.err_code == sa_error_code("ERR_NULL_CALL"));
            assert(strcmp(sa_current_error.type, "ERR_NULL_CALL") == 0);
            assert(strcmp(sa_current_error.message, "call through a NULL function reference") == 0);
            assert(sa_current_error.line_number == 0);
            assert(strcmp(sa_current_error.sub_name, "SYS.GUI.RUN") == 0);
        } else {
            assert(sa_current_error.err_code == sa_error_code("ERR_GUI_PROBE"));
            assert(strcmp(sa_current_error.type, "ERR_GUI_PROBE") == 0);
            assert(strcmp(sa_current_error.message, "original callback error") == 0);
            assert(sa_current_error.message == sa_probe_message);
            assert(sa_current_error.line_number == 731);
            assert(strcmp(sa_current_error.sub_name, "onClick") == 0);
        }
        sa_error_clear(&sa_current_error);
        /* 异常不会吞掉后续事件；再次 RUN 消费结束标记且不遗留异常帧。 */
        assert(sa_gui_run() == 1);
    }
    assert(sa_try_top == 1);
    if (SA_PROBE_DETACH) {
        assert(sa_gui_widgets[3].on_click == NULL);
        assert(sa__live == 0);
    } else {
        assert(sa_gui_widgets[3].on_click->refs == 1);
        sa_callable_set(&sa_gui_widgets[3].on_click, NULL);
    }
    assert(sa__live == 0);
}

int main(void) {
    atexit(sa__rep);
    sa_try_push_env();
    if (SA_SETJMP(sa_try_stack[sa_try_top - 1].env) == 0) {
        /* 超过异常栈容量的重复派发可以暴露逐次遗漏 pop 的问题。 */
        for (int i = 0; i < 80; i++) sa_probe_run_once();
    } else {
        assert(0 && "不应跳过调用方的异常处理帧");
    }
    assert(sa_probe_hits == (SA_PROBE_NULL ? 0 : 80));
    assert(sa_try_top == 1);
    sa_try_pop();
    assert(sa_try_top == 0);
    puts("dispatch cleanup ok");
    return 0;
}
"""


@requires_gcc
@requires_windows
@pytest.mark.parametrize("opt", ["-O0", "-O2"])
@pytest.mark.parametrize(
    ("throws", "detach", "null_fn"),
    [(False, False, False), (True, False, False), (False, True, False),
     (True, True, False), (False, False, True)],
    ids=["return", "throw", "detach-return", "detach-throw", "null-function"],
)
def test_gui_dispatch_cleanup_preserves_error_and_try_stack(
    opt: str, throws: bool, detach: bool, null_fn: bool,
) -> None:
    from test_coroutines import _LEAK_SHIM

    defines = (
        "#define SA_ENABLE_GUI\n"
        f"#define SA_PROBE_THROW {int(throws)}\n"
        f"#define SA_PROBE_DETACH {int(detach)}\n"
        f"#define SA_PROBE_NULL {int(null_fn)}\n"
    )
    with build_temp("fm-gui-cleanup-", "function-model-tests") as temp:
        source = Path(temp) / "probe.c"
        source.write_text(_LEAK_SHIM + defines + RUNTIME + _GUI_DISPATCH_CLEANUP_MAIN, encoding="utf-8")
        exe = Path(temp) / "probe.exe"
        build = subprocess.run(
            ["gcc", opt, "-std=c11", "-o", str(exe), str(source), "-luser32", "-lgdi32"],
            capture_output=True, text=True,
        )
        assert build.returncode == 0, build.stderr[-3000:]
        run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["dispatch cleanup ok"]
    assert "SA_LIVE=0" in run.stderr


# --- 用户模块 ---

@requires_gcc
def test_module_subs_can_be_referenced_and_take_callables() -> None:
    from sonalgebraic.driver.compiler import build_exe

    lib = (
        f"10 SUB twice(f AS {_FN}, v AS NUM AS LONG) AS PUBLIC AS NUM AS LONG\n"
        "20 RETURN f(f(v))\n30 .ENDSUB\n"
        "40 SUB inc(x AS NUM AS LONG) AS PUBLIC AS NUM AS LONG\n50 RETURN x + 1\n60 .ENDSUB\n"
    )
    main = (
        "10 USE FMLIB AS LIB\n"
        "20 SUB main AS PUBLIC AS VOID\n"
        "30 NEW SUB h FROM @LIB.inc()\n"
        "40 PRINT LIB.twice(h, 5)\n"
        "50 .ENDSUB\n60 CALL main\n70 END\n"
    )
    with build_temp("fm-mod-", "function-model-tests") as temp:
        root = Path(temp)
        (root / "fmlib.sa").write_text(lib, encoding="utf-8")
        (root / "main.sa").write_text(main, encoding="utf-8")
        exe = root / "main.exe"
        build_exe(root / "main.sa", exe, keep_c=False)
        run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert run.stdout.split() == ["7"]


# --- native 后端 ---

@pytest.mark.parametrize(
    "source",
    [
        _main(f"510 DIM fp AS PTR TO {_FN} AS VAR"),
        _main("510 NEW SUB h FROM @add1()"),
        _main("510 PRINT @add1() = @dbl()"),
        _main("510 PRINT 1", _HELPERS + f"100 SUB apply(f AS {_FN}) AS NUM AS LONG\n110 CALLRET f(1)\n120 .ENDSUB\n"),
        _main("510 PRINT 1", "10 FOR ENTITY AS B\n20 DIM f AS SUB AS VAR\n30 .ENDENTITY\n"),
    ],
)
def test_native_backend_accepts_function_model(source: str) -> None:
    ir = generate_native_llvm_ir(check_program(parse_program(source)))
    assert "define void @sa_main()" in ir


def test_native_backend_accepts_gui_callbacks() -> None:
    source = "10 USE SYS.GUI AS G\n20 DIM ok AS BOOL AS VAR\n" + _main("510 ok = G.RUN()", "")
    ir = generate_native_llvm_ir(check_program(parse_program(source)))
    assert "call i32 @sa_gui_run()" in ir
