"""第一批语言特性测试：ELSE/ELSE IF、BOOL/TRUE/FALSE、NULL、数值字面量。"""
from __future__ import annotations

import pytest

from conftest import compile_c, expect_error
from sonalgebraic.frontend.parser import parse_program
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.analysis.typesys import classify_number_literal, is_bool, is_null


def _wrap(body: str) -> str:
    """把一段语句包进合法的 main 程序里。"""
    return f"10 SUB main AS PUBLIC AS VOID\n{body}\n900 .ENDSUB\n910 CALL main\n920 END\n"


# --- 数值字面量 ---

def test_hex_literal_is_long() -> None:
    assert classify_number_literal("0xFF").subtype == "LONG"


def test_scientific_literal_is_double() -> None:
    assert classify_number_literal("1.5e3").subtype == "DOUBLE"
    assert classify_number_literal("2E-5").subtype == "DOUBLE"


def test_underscore_literal_is_long() -> None:
    assert classify_number_literal("1_000_000").subtype == "LONG"


def test_hex_literal_generates_c() -> None:
    c = compile_c(_wrap("20 DIM x AS NUM AS LONG AS VAR\n30 x = 0xFF"))
    assert "0xFF" in c


def test_underscore_stripped_in_c() -> None:
    c = compile_c(_wrap("20 DIM x AS NUM AS LONG AS VAR\n30 x = 1_000_000"))
    # 生成的赋值语句里下划线被去掉（源码注释里仍保留原文，不检查注释）
    assert "sa_x = 1000000;" in c


def test_scientific_literal_generates_c() -> None:
    c = compile_c(_wrap("20 DIM x AS NUM AS DOUBLE AS VAR\n30 x = 1.5e3"))
    assert "1.5e3" in c


# --- BOOL / TRUE / FALSE ---

def test_bool_type_maps_to_int() -> None:
    c = compile_c(_wrap("20 DIM flag AS BOOL AS VAR\n30 flag = TRUE"))
    assert "int sa_flag" in c
    assert "sa_flag = 1;" in c


def test_false_generates_zero() -> None:
    c = compile_c(_wrap("20 DIM flag AS BOOL AS VAR\n30 flag = FALSE"))
    assert "sa_flag = 0;" in c


def test_bool_assignable_from_comparison() -> None:
    # 比较结果是 BOOL，可赋给 BOOL 变量
    check_program(parse_program(_wrap("20 DIM flag AS BOOL AS VAR\n30 flag = 5 > 3")))


def test_comparison_assignable_to_long() -> None:
    # BOOL 与数值互转：比较结果也能赋给 LONG
    check_program(parse_program(_wrap("20 DIM n AS NUM AS LONG AS VAR\n30 n = 5 > 3")))


# --- NULL ---

def test_null_is_null_type() -> None:
    from sonalgebraic.core import ast
    expr = ast.NullLiteral(0)
    from sonalgebraic.analysis.typesys import type_of
    assert is_null(type_of(expr, {}))


def test_null_assignable_to_pointer() -> None:
    source = _wrap("20 DIM p AS PTR TO NUM AS LONG AS VAR\n30 p = NULL")
    c = compile_c(source)
    assert "sa_p = NULL;" in c


def test_null_compared_with_pointer() -> None:
    check_program(parse_program(_wrap(
        "20 DIM p AS PTR TO NUM AS LONG AS VAR\n30 p = NULL\n40 IF p = NULL THEN\n50 PRINT \"null\"\n60 END IF"
    )))


# --- ELSE / ELSE IF ---

def test_else_generates_c() -> None:
    source = _wrap(
        "20 DIM x AS NUM AS LONG AS VAR\n30 x = 5\n40 IF x > 0 THEN\n50 PRINT \"pos\"\n60 ELSE\n70 PRINT \"neg\"\n80 END IF"
    )
    c = compile_c(source)
    assert "else {" in c


def test_else_if_chain_generates_nested_c() -> None:
    source = _wrap(
        "20 DIM x AS NUM AS LONG AS VAR\n30 x = 5\n"
        "40 IF x > 10 THEN\n50 PRINT \"big\"\n"
        "60 ELSE IF x > 0 THEN\n70 PRINT \"small\"\n"
        "80 ELSE\n90 PRINT \"neg\"\n100 END IF"
    )
    c = compile_c(source)
    # 展开成嵌套 if-else。runtime 全文嵌在生成的 C 里，自身也含 else 块，
    # 扣掉 runtime 的计数只数用户代码段，避免 runtime 演进把这个测试搞脆。
    from sonalgebraic.backend.c_runtime import RUNTIME

    assert c.count("else {") - RUNTIME.count("else {") == 2


def test_dot_endif_is_accepted() -> None:
    source = _wrap(
        '20 DIM value AS NUM AS LONG AS VAR\n30 IF TRUE THEN\n40 value = 1\n50 ELSE\n60 value = 2\n70 .ENDIF'
    )
    c = compile_c(source)
    assert "if (1)" in c
    assert "sa_value = 2;" in c


def test_non_void_sub_returns_via_complete_if_else() -> None:
    # 关键改进：没有末尾裸 RETURN，全靠 if/elif/else 各分支返回，应当通过
    source = """10 SUB grade(score AS NUM AS LONG) AS NUM AS LONG
20 IF score >= 90 THEN
30 RETURN 4
40 ELSE IF score >= 60 THEN
50 RETURN 2
60 ELSE
70 RETURN 0
80 END IF
90 .ENDSUB
100 DIM g AS NUM AS LONG AS VAR
110 SUB main AS PUBLIC AS VOID
120 g = CALL grade(85)
130 .ENDSUB
140 CALL main
150 END
"""
    check_program(parse_program(source))


def test_non_void_sub_without_else_still_requires_return() -> None:
    # IF 没有 ELSE 分支，无法保证返回，仍应报错
    source = """10 SUB grade(score AS NUM AS LONG) AS NUM AS LONG
20 IF score >= 90 THEN
30 RETURN 4
40 END IF
50 .ENDSUB
60 DIM g AS NUM AS LONG AS VAR
70 SUB main AS PUBLIC AS VOID
80 g = CALL grade(85)
90 .ENDSUB
100 CALL main
110 END
"""
    expect_error(source, "非 VOID SUB 必须保证")


def test_else_if_missing_then_is_rejected() -> None:
    source = _wrap(
        "20 DIM x AS NUM AS LONG AS VAR\n30 x = 5\n40 IF x > 0 THEN\n50 PRINT \"a\"\n60 ELSE IF x\n70 PRINT \"b\"\n80 END IF"
    )
    with pytest.raises(Exception):
        check_program(parse_program(source))
