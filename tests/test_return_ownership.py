"""RETURN 的所有权：交给调用方的托管值必须是一份独立所有权，调用方拿到后负责释放。

以前 `RETURN s`（局部 STRING）生成 `tmp = sa_s; free(sa_s); return tmp;`——调用方拿到的是悬空指针；
而 `x = CALL f()` 又从不释放返回值。现在的规则：本帧拥有的局部整体搬出（源清零），搬不动的深拷贝，
本语句刚算出的临时量直接接管；调用方把返回值当临时量登记，赋值/RETURN/建树接管，否则语句尾释放。
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest

from conftest import build_temp, compile_user_c, requires_gcc
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.backend.native import generate_native_llvm_ir
from sonalgebraic.frontend.parser import parse_program
from test_coroutines import _build_and_run
from test_native_backend import _native_live_allocs, requires_clang_lld


def _main(body: str) -> str:
    return f"500 SUB main AS PUBLIC AS VOID\n{body}\n900 .ENDSUB\n910 CALL main\n920 END\n"


_BOX = "1 FOR ENTITY AS Box\n2 DIM text AS STRING AS VAR\n3 DIM n AS NUM AS LONG AS VAR\n4 .ENDENTITY\n"

_WRAP = '10 SUB wrap(x AS STRING) AS STRING\n20 DIM s AS STRING AS VAR\n30 s = F"[{x}]"\n40 RETURN s\n50 .ENDSUB\n'


# --- 语义 ---

def test_return_of_block_local_variable_is_accepted() -> None:
    # check_return 以前拿外层作用域去看嵌套块里的 RETURN，IF 里 DIM 的变量被报「未声明」
    check_program(parse_program(
        "10 SUB f(flag AS NUM AS LONG) AS STRING\n20 IF flag > 0 THEN\n30 DIM inner AS STRING AS VAR\n"
        '40 inner = "inner"\n50 RETURN inner\n60 .ENDIF\n70 DIM i AS NUM AS LONG AS VAR\n80 FOR i = 1 TO 2\n'
        '90 DIM t AS STRING AS VAR\n100 RETURN t\n110 .ENDFOR\n120 RETURN "outer"\n130 .ENDSUB\n' + _main("")
    ))


def test_symbol_sub_returning_expression_builds_tree() -> None:
    # `RETURN s * 2` 以前按数值表达式生成 `(sa_s * 2)`——对 SaSymbol 指针做乘法，C 编译直接报错
    c_text = compile_user_c(
        "10 SUB twice(s AS SYMBOL) AS SYMBOL\n20 RETURN s * 2\n30 .ENDSUB\n"
        + _main("510 DIM x AS NUM AS LONG AS VAR\n520 DIM v AS SYMBOL AS VAR\n530 v = x\n540 PRINT twice(v)")
    )
    body = c_text[c_text.find("static SaSymbol sa_twice(SaSymbol sa_s) {"):]
    assert "(sa_s * 2)" not in body
    assert "sa_symbol_op('*', sa_symbol_clone(sa_s), sa_symbol_const(\"2\"))" in body


# --- 生成 C ---

def test_c_return_of_owned_local_moves_and_nulls_source() -> None:
    c_text = compile_user_c(_WRAP + _main('510 PRINT wrap("a")'))
    body = c_text[c_text.find("static char* sa_wrap(char* sa_x) {"):]
    assert "char* sa_tmp_2 = sa_s;\n    sa_s = NULL;\n    free(sa_s);\n    free(sa_x);\n    return sa_tmp_2;" in body
    assert "sa_strdup(sa_s)" not in body


def test_c_return_of_unowned_string_copies() -> None:
    c_text = compile_user_c(
        "1 DIM g AS STRING AS VAR\n"
        "10 SUB ret_global() AS STRING\n20 RETURN g\n30 .ENDSUB\n"
        "40 SUB ret_ref(r AS STRING AS REF) AS STRING\n50 RETURN r\n60 .ENDSUB\n"
        '70 SUB ret_borrowed() AS STRING\n80 DIM s AS STRING AS VAR\n90 s = "b"\n100 DIM v AS STRING AS VAR\n'
        "110 v f= s\n120 RETURN v\n130 .ENDSUB\n"
        '140 SUB ret_elem() AS STRING\n150 DIM arr[2] AS STRING AS VAR\n160 arr[0] = "e"\n170 RETURN arr[0]\n180 .ENDSUB\n'
        + _main("510 PRINT ret_global()\n520 PRINT ret_ref(g)\n530 PRINT ret_borrowed()\n540 PRINT ret_elem()")
    )
    # 全局、REF 形参、f= 借来的、数组元素都不归本帧：只能拷贝，且源保持原样
    assert "sa_strdup(sa_g);" in c_text
    assert "sa_strdup((*sa_r));" in c_text
    assert "sa_strdup(sa_v);\n    free(sa_s);" in c_text
    assert "sa_strdup(sa_arr[0]);" in c_text
    assert "    sa_g = NULL;" not in c_text and "    sa_v = NULL;" not in c_text


def test_c_return_of_temp_adopts_instead_of_copying() -> None:
    c_text = compile_user_c(
        "1 USE SYS.STRING AS STR\n" + _WRAP
        + '60 SUB cc(a AS STRING) AS STRING\n70 RETURN STR.CONCAT(a, "!")\n80 .ENDSUB\n'
        + '90 SUB nested() AS STRING\n100 RETURN wrap(wrap("n"))\n110 .ENDSUB\n'
        + '120 SUB lit() AS STRING\n130 RETURN "lit"\n140 .ENDSUB\n'
        + _main('510 PRINT cc("x")\n520 PRINT nested()\n530 PRINT lit()')
    )
    cc = c_text[c_text.find("static char* sa_cc(char* sa_a) {"):c_text.find("static char* sa_nested(void) {")]
    assert 'char* sa_tmp_4 = sa_str_concat(sa_a, "!");\n    char* sa_tmp_3 = sa_tmp_4;\n    free(sa_a);\n    return sa_tmp_3;' in cc
    nested = c_text[c_text.find("static char* sa_nested(void) {"):c_text.find("static char* sa_lit(void) {")]
    # 内层 wrap 的结果是外层的实参，用完释放；外层结果被 RETURN 接管
    assert 'char* sa_tmp_6 = sa_wrap("n");\n    char* sa_tmp_7 = sa_wrap(sa_tmp_6);\n    char* sa_tmp_5 = sa_tmp_7;\n    free(sa_tmp_6);\n    return sa_tmp_5;' in nested
    assert 'char* sa_tmp_8 = sa_strdup("lit");' in c_text


def test_c_caller_adopts_returned_string_and_frees_discarded_ones() -> None:
    c_text = compile_user_c(_WRAP + _main(
        '510 DIM tag AS STRING AS VAR\n520 tag = CALL wrap("abc")\n530 PRINT wrap(tag)\n'
        '540 DIM d AS STRING AS VAR = wrap("dim")\n'
        '550 DIM trap AS ERROR AS VAR\n560 TRY CALL wrap("discard") TRACEBACK ERROR AS trap\n'
        "570 CATCH ERR_ANY AS e\n580 PRINT e\n590 .ENDTRY"
    ))
    body = c_text[c_text.find("static void sa_main(void) {"):]
    assert 'char* sa_tmp_3 = sa_wrap("abc");\n        free(sa_tag);\n        sa_tag = sa_tmp_3;' in body
    assert "sa_set_string(&sa_tag" not in body
    assert "char* sa_tmp_4 = sa_wrap(sa_tag);\n        sa_print_string(sa_tmp_4);\n        free(sa_tmp_4);" in body
    assert 'char* sa_tmp_5 = sa_wrap("dim");\n    free(sa_d);\n    sa_d = sa_tmp_5;' in body
    assert 'char* sa_tmp_6 = sa_wrap("discard");\n        free(sa_tmp_6);' in body


def test_c_entity_return_moves_struct_and_caller_adopts() -> None:
    c_text = compile_user_c(_BOX + (
        "10 SUB mkbox(t AS STRING) AS ENTITY AS Box\n20 DIM b AS ENTITY AS Box AS VAR\n30 b.text = t\n40 RETURN b\n50 .ENDSUB\n"
        "60 SUB field() AS STRING\n70 DIM b AS ENTITY AS Box AS VAR\n80 RETURN b.text\n90 .ENDSUB\n"
    ) + _main('510 DIM b AS ENTITY AS Box AS VAR\n520 b = CALL mkbox("hi")\n530 PRINT b.text\n540 PRINT field()'))
    assert "SaEntity_box sa_tmp_1 = sa_b;\n    memset(&sa_b, 0, sizeof(sa_b));" in c_text
    assert "char* sa_tmp_2 = sa_b.text;\n    sa_b.text = NULL;" in c_text
    body = c_text[c_text.find("static void sa_main(void) {"):]
    assert 'SaEntity_box sa_tmp_3 = sa_mkbox("hi");\n        free(sa_b.text);\n        sa_b = sa_tmp_3;' in body


def test_c_symbol_return_clones_unowned_and_adopts_temp() -> None:
    c_text = compile_user_c(
        "1 DIM gsym AS SYMBOL AS VAR\n"
        '10 SUB dsym(s AS SYMBOL) AS SYMBOL\n20 RETURN DERIV(s, "x")\n30 .ENDSUB\n'
        "40 SUB pass_sym(s AS SYMBOL) AS SYMBOL\n50 RETURN s\n60 .ENDSUB\n"
        "70 SUB ret_gsym() AS SYMBOL\n80 RETURN gsym\n90 .ENDSUB\n"
        "100 SUB build() AS SYMBOL\n110 DIM x AS NUM AS LONG AS VAR\n120 DIM t AS SYMBOL AS VAR\n130 t = x * 2\n140 RETURN t\n150 .ENDSUB\n"
        + _main("510 DIM v AS SYMBOL AS VAR\n520 v = CALL build()\n530 v = CALL dsym(v)\n540 PRINT pass_sym(v)\n550 PRINT ret_gsym()")
    )
    assert 'SaSymbol sa_tmp_2 = sa_symbol_deriv(sa_s, "x");\n    SaSymbol sa_tmp_1 = sa_tmp_2;\n    return sa_tmp_1;' in c_text
    assert "SaSymbol sa_tmp_3 = sa_symbol_clone(sa_s);" in c_text
    assert "SaSymbol sa_tmp_4 = sa_symbol_clone(sa_gsym);" in c_text
    assert "SaSymbol sa_tmp_6 = sa_t;\n    sa_t = NULL;" in c_text
    body = c_text[c_text.find("static void sa_main(void) {"):]
    # 调用方：返回的树直接接管，不再 clone 一份再把原件 free 掉
    assert "sa_symbol_clone(" not in body
    assert "SaSymbol sa_tmp_7 = sa_build();\n        SaSymbol sa_tmp_8 = sa_tmp_7;\n        sa_symbol_free(sa_v);\n        sa_v = sa_tmp_8;" in body
    assert "SaSymbol sa_tmp_11 = sa_pass_sym(sa_v);\n        char* sa_tmp_12 = sa_symbol_to_string(sa_tmp_11);\n        sa_print_string(sa_tmp_12);\n        free(sa_tmp_12);\n        sa_symbol_free(sa_tmp_11);" in body


# --- 端到端：输出 + 零泄漏 ---

_E2E_STRING = "1 USE SYS.STRING AS STR\n2 DIM g AS STRING AS VAR\n" + _WRAP + (
    "60 SUB ret_global() AS STRING\n70 RETURN g\n80 .ENDSUB\n"
    "90 SUB ret_ref(r AS STRING AS REF) AS STRING\n100 RETURN r\n110 .ENDSUB\n"
    '120 SUB ret_borrowed() AS STRING\n130 DIM s AS STRING AS VAR\n140 s = "borrowed"\n150 DIM v AS STRING AS VAR\n'
    "160 v f= s\n170 RETURN v\n180 .ENDSUB\n"
    "190 SUB ret_nested(flag AS NUM AS LONG) AS STRING\n200 IF flag > 0 THEN\n210 DIM inner AS STRING AS VAR\n"
    '220 inner = "inner"\n230 RETURN inner\n240 .ENDIF\n250 RETURN "outer"\n260 .ENDSUB\n'
    '270 SUB ret_concat(a AS STRING) AS STRING\n280 RETURN STR.CONCAT(a, "!")\n290 .ENDSUB\n'
    '300 SUB ret_elem() AS STRING\n310 DIM arr[2] AS STRING AS VAR\n320 arr[0] = "elem"\n330 RETURN arr[0]\n340 .ENDSUB\n'
    "350 SUB loop_ret(n AS NUM AS LONG) AS STRING\n360 DIM i AS NUM AS LONG AS VAR\n370 FOR i = 1 TO n\n"
    '380 DIM t AS STRING AS VAR\n390 t = F"loop{i}"\n400 IF i = 2 THEN\n410 RETURN t\n420 .ENDIF\n430 .ENDFOR\n'
    '440 RETURN "none"\n450 .ENDSUB\n'
    "460 SUB show(t AS STRING) AS VOID\n470 PRINT t\n480 .ENDSUB\n"
) + _main(
    '510 g = "global"\n520 DIM s AS STRING AS VAR\n'
    "530 s = CALL ret_global()\n540 PRINT s\n550 s = CALL ret_ref(g)\n560 PRINT s\n"
    "570 s = CALL ret_borrowed()\n580 PRINT s\n590 s = CALL ret_nested(1)\n600 PRINT s\n610 s = CALL ret_nested(0)\n620 PRINT s\n"
    '630 s = CALL ret_concat("cc")\n640 PRINT s\n650 s = CALL wrap(wrap(s))\n660 PRINT s\n'
    "670 s = CALL ret_elem()\n680 PRINT s\n690 s = CALL loop_ret(3)\n700 PRINT s\n"
    '710 CALL show(wrap("arg"))\n720 PRINT STR.LENGTH(wrap("abc"))\n730 IF wrap("a") = "[a]" THEN\n740 PRINT "cond"\n750 .ENDIF\n'
    '760 DIM trap AS ERROR AS VAR\n770 TRY CALL wrap("discard") TRACEBACK ERROR AS trap\n780 CATCH ERR_ANY AS e\n790 PRINT e\n800 .ENDTRY\n'
    '810 DIM d AS STRING AS VAR = wrap("dim")\n820 PRINT d'
)
_E2E_STRING_OUT = ["global", "global", "borrowed", "inner", "outer", "cc!", "[[cc!]]", "elem", "loop2", "[arg]", "5", "cond", "[dim]"]

_E2E_SYMBOL = "1 DIM x AS NUM AS LONG AS VAR\n2 DIM gsym AS SYMBOL AS VAR\n" + (
    '10 SUB dsym(s AS SYMBOL) AS SYMBOL\n20 RETURN DERIV(s, "x")\n30 .ENDSUB\n'
    "40 SUB pass_sym(s AS SYMBOL) AS SYMBOL\n50 RETURN s\n60 .ENDSUB\n"
    "70 SUB ret_gsym() AS SYMBOL\n80 RETURN gsym\n90 .ENDSUB\n"
    "100 SUB build(s AS SYMBOL) AS SYMBOL\n110 DIM t AS SYMBOL AS VAR\n120 t = s * 2 + 1\n130 RETURN t\n140 .ENDSUB\n"
    "150 SUB twice(s AS SYMBOL) AS SYMBOL\n160 RETURN s * 2\n170 .ENDSUB\n"
) + _main(
    "510 gsym = x * x\n520 DIM sym AS SYMBOL AS VAR\n530 sym = x * 3\n"
    "540 sym = CALL dsym(sym)\n550 PRINT sym\n560 sym = CALL pass_sym(sym)\n570 PRINT sym\n"
    "580 sym = CALL ret_gsym()\n590 PRINT sym\n600 PRINT dsym(sym)\n"
    "610 sym = CALL build(sym)\n620 PRINT sym\n630 PRINT twice(sym)"
)
_E2E_SYMBOL_OUT = [
    "((1 * 3) + (x * 0))", "((1 * 3) + (x * 0))", "(x * x)", "((1 * x) + (x * 1))",
    "(((x * x) * 2) + 1)", "((((x * x) * 2) + 1) * 2)",
]

_E2E_ENTITY = _BOX + "5 USE SYS.STRING AS STR\n6 DIM gbox AS ENTITY AS Box AS VAR\n" + (
    "10 SUB mkbox(t AS STRING) AS ENTITY AS Box\n20 DIM b AS ENTITY AS Box AS VAR\n30 b.text = t\n40 b.n = STR.LENGTH(t)\n50 RETURN b\n60 .ENDSUB\n"
    "70 SUB pass_box(b AS ENTITY AS Box) AS ENTITY AS Box\n80 b.n = b.n + 100\n90 RETURN b\n100 .ENDSUB\n"
    '110 SUB ret_box_call() AS ENTITY AS Box\n120 RETURN mkbox("viacall")\n130 .ENDSUB\n'
    "140 SUB ret_gbox() AS ENTITY AS Box\n150 RETURN gbox\n160 .ENDSUB\n"
    '170 SUB fill_box(out AS ENTITY AS Box AS REF) AS VOID\n180 out = CALL mkbox("filled")\n190 .ENDSUB\n'
    '200 SUB ret_field() AS STRING\n210 DIM b AS ENTITY AS Box AS VAR\n220 b.text = "field"\n230 RETURN b.text\n240 .ENDSUB\n'
) + _main(
    '510 gbox.text = "gbox"\n520 DIM b AS ENTITY AS Box AS VAR\n'
    '530 b = CALL mkbox("hi")\n540 PRINT F"{b.text}/{b.n}"\n550 b = CALL pass_box(b)\n560 PRINT F"{b.text}/{b.n}"\n'
    '570 b = CALL ret_box_call()\n580 PRINT F"{b.text}/{b.n}"\n590 b = CALL ret_gbox()\n600 PRINT F"{b.text}/{b.n}"\n'
    '610 CALL fill_box(b)\n620 PRINT F"{b.text}/{b.n}"\n'
    '630 DIM c AS ENTITY AS Box AS VAR = mkbox("init")\n640 PRINT F"{c.text}/{c.n}"\n650 PRINT ret_field()'
)
_E2E_ENTITY_OUT = ["hi/2", "hi/102", "viacall/7", "gbox/0", "filled/6", "init/4", "field"]

_E2E_ASYNC = (
    '10 ASYNC SUB work() AS STRING\n20 DIM s AS STRING AS VAR\n30 s = "frame"\n40 RETURN s\n50 .ENDSUB\n'
    '60 ASYNC SUB deco(x AS STRING) AS STRING\n70 RETURN F"<{x}>"\n80 .ENDSUB\n'
) + _main("510 DIM r AS STRING AS VAR\n520 r = SYNC work()\n530 PRINT r\n540 r = SYNC deco(r)\n550 PRINT r")
_E2E_ASYNC_OUT = ["frame", "<frame>"]


@pytest.mark.e2e
@requires_gcc
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (_E2E_STRING, _E2E_STRING_OUT),
        (_E2E_SYMBOL, _E2E_SYMBOL_OUT),
        (_E2E_ENTITY, _E2E_ENTITY_OUT),
        (_E2E_ASYNC, _E2E_ASYNC_OUT),
    ],
    ids=["string", "symbol", "entity", "async"],
)
def test_c_backend_returns_without_leaks(source: str, expected: list[str]) -> None:
    with build_temp("ret-", "return-ownership-tests") as temp:
        result = _build_and_run(source, temp, "ret", opt="-O0", leak=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == expected
    assert "SA_LIVE=0" in result.stderr


# --- native ---

def _native_ir(source: str) -> str:
    return generate_native_llvm_ir(check_program(parse_program(source)))


def test_native_ir_return_moves_owned_local_and_copies_unowned() -> None:
    ir = _native_ir(
        "1 DIM g AS STRING AS VAR\n" + _WRAP
        + "60 SUB ret_global() AS STRING\n70 RETURN g\n80 .ENDSUB\n"
        + _main('510 PRINT wrap("a")\n520 PRINT ret_global()')
    )
    wrap = ir[ir.find("define ptr @sa_wrap("):ir.find("define ptr @sa_ret_global(")]
    moved = wrap[wrap.find("; SA 40: RETURN s"):]
    assert "load ptr, ptr %sa_s.addr\n  store ptr null, ptr %sa_s.addr" in moved
    assert "@sa_strdup" not in moved
    ret_global = ir[ir.find("define ptr @sa_ret_global("):ir.find("define void @sa_main(")]
    assert "load ptr, ptr @sa_g\n" in ret_global and "call ptr @sa_strdup(" in ret_global


def test_native_ir_caller_adopts_and_frees_discarded_results() -> None:
    ir = _native_ir(_WRAP + _main(
        '510 DIM tag AS STRING AS VAR\n520 tag = CALL wrap("abc")\n530 PRINT wrap(tag)\n'
        '540 DIM trap AS ERROR AS VAR\n550 TRY CALL wrap("discard") TRACEBACK ERROR AS trap\n'
        "560 CATCH ERR_ANY AS e\n570 PRINT e\n580 .ENDTRY"
    ))
    body = ir[ir.find("define void @sa_main("):]
    # 赋值：返回值（经 setjmp 包装后从结果槽 load 出来）直接存进变量，之前先 free 旧串，不 sa_set_string 多拷一份
    assert re.search(
        r"(%sa_tmp_\d+) = load ptr, ptr %sa_tmp_\d+\n"
        r"  (%sa_tmp_\d+) = load ptr, ptr %sa_tag\.addr\n  call void @free\(ptr \2\)\n"
        r"  store ptr \1, ptr %sa_tag\.addr",
        body,
    ), body
    assert "@sa_set_string(ptr %sa_tag.addr" not in body
    # PRINT：打印完释放
    assert re.search(
        r"(%sa_tmp_\d+) = load ptr, ptr %sa_tmp_\d+\n"
        r"  call i32 \(ptr, \.\.\.\) @printf\(ptr @\.sa_fmt_str, ptr \1\)\n  call void @free\(ptr \1\)",
        body,
    ), body
    # TRY CALL：丢弃的返回值紧跟着 call 释放（try_end 块还有 CATCH 汇入，try 块里的 SSA 值拖到语句尾就不支配了）
    assert re.search(
        r"sa_try_body_\d+:\n  (%sa_tmp_\d+) = call ptr @sa_wrap\(ptr @\.sa_str_\d+\)\n  call void @free\(ptr \1\)",
        body,
    ), body


def test_native_ir_entity_return_zeroes_source_and_symbol_return_clones_param() -> None:
    ir = _native_ir(_BOX + (
        "10 SUB mkbox(t AS STRING) AS ENTITY AS Box\n20 DIM b AS ENTITY AS Box AS VAR\n30 b.text = t\n40 RETURN b\n50 .ENDSUB\n"
        "60 SUB pass_sym(s AS SYMBOL) AS SYMBOL\n70 RETURN s\n80 .ENDSUB\n"
    ) + _main('510 DIM b AS ENTITY AS Box AS VAR\n520 b = CALL mkbox("hi")\n530 DIM v AS SYMBOL AS VAR\n540 v = CALL pass_sym(v)'))
    mkbox = ir[ir.find("define %SaEntity_box @sa_mkbox("):ir.find("define ptr @sa_pass_sym(")]
    assert "load %SaEntity_box, ptr %sa_b.addr\n  store %SaEntity_box zeroinitializer, ptr %sa_b.addr" in mkbox
    pass_sym = ir[ir.find("define ptr @sa_pass_sym("):ir.find("define void @sa_main(")]
    assert "call ptr @sa_symbol_clone(" in pass_sym
    body = ir[ir.find("define void @sa_main("):]
    # 调用方接管返回的 ENTITY：释放旧的串字段后整体 store，不逐字段 sa_set_string
    adopted = re.search(r"(%sa_tmp_\d+) = load %SaEntity_box, ptr %sa_tmp_\d+\n((?:.*\n)*?)  store %SaEntity_box \1, ptr %sa_b\.addr", body)
    assert adopted is not None, body
    assert adopted.group(2).count("call void @free(") == 1
    assert "@sa_set_string(" not in body
    # 接管返回的树：不再 clone
    assert "@sa_symbol_clone(" not in body


@pytest.mark.e2e
@requires_clang_lld
@pytest.mark.parametrize("source", [_E2E_STRING, _E2E_SYMBOL, _E2E_ENTITY], ids=["string", "symbol", "entity"])
def test_native_backend_returns_without_leaks(source: str) -> None:
    with build_temp("ret-native-", "return-ownership-tests") as temp:
        assert _native_live_allocs(Path(temp), source) == 0
