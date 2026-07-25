"""第四批语言特性测试：完整集内置常量 + 枚举 + SYMBOL 代数。"""
from __future__ import annotations

from conftest import compile_c, expect_error
from sonalgebraic.frontend.parser import parse_program
from sonalgebraic.analysis.semantics import check_program


def _wrap(decls: str, body: str, uses: str = "") -> str:
    head = f"{uses}\n" if uses else ""
    return f"{head}{decls}\n200 SUB main AS PUBLIC AS VOID\n{body}\n800 .ENDSUB\n810 CALL main\n820 END\n"


# --- 内置常量 ---

def test_math_constants_e_tau() -> None:
    c = compile_c(_wrap(
        "10 DIM d AS NUM AS DOUBLE AS VAR",
        "210 d = M.E\n220 d = M.TAU",
        uses="5 USE SYS.MATH AS M",
    ))
    assert "2.71828182845904523536" in c
    assert "6.28318530717958647692" in c


def test_math_max_long_constant() -> None:
    c = compile_c(_wrap(
        "10 DIM n AS NUM AS LONG AS VAR",
        "210 n = M.MAX_LONG",
        uses="5 USE SYS.MATH AS M",
    ))
    assert "9223372036854775807LL" in c


def test_string_newline_tab_constants() -> None:
    c = compile_c(_wrap(
        "10 DIM s AS STRING AS VAR",
        "210 s = S.NEWLINE\n220 s = S.TAB",
        uses="5 USE SYS.STRING AS S",
    ))
    assert r'"\n"' in c
    assert r'"\t"' in c


def test_pi_still_works() -> None:
    c = compile_c(_wrap(
        "10 DIM d AS NUM AS DOUBLE AS VAR",
        "210 d = M.PI",
        uses="5 USE SYS.MATH AS M",
    ))
    assert "3.14159265358979323846" in c


# --- 枚举 ---

def test_enum_members_get_incrementing_values() -> None:
    source = """10 ENUM Color
20 RED
30 GREEN
40 BLUE
50 .ENDENUM
60 DIM c AS NUM AS LONG AS VAR
200 SUB main AS PUBLIC AS VOID
210 c = Color.GREEN
220 .ENDSUB
230 CALL main
240 END
"""
    c = compile_c(source)
    assert "sa_c = 1;" in c  # GREEN = 1


def test_enum_last_member_value() -> None:
    source = """10 ENUM Dir
20 UP
30 DOWN
40 LEFT
50 RIGHT
60 .ENDENUM
70 DIM d AS NUM AS LONG AS VAR
200 SUB main AS PUBLIC AS VOID
210 d = Dir.RIGHT
220 .ENDSUB
230 CALL main
240 END
"""
    c = compile_c(source)
    assert "sa_d = 3;" in c  # RIGHT = 3


def test_duplicate_enum_member_rejected() -> None:
    source = """10 ENUM Bad
20 A
30 A
40 .ENDENUM
200 SUB main AS PUBLIC AS VOID
210 PRINT "x"
220 .ENDSUB
230 CALL main
240 END
"""
    expect_error(source, "ENUM 成员重复")


def test_empty_enum_rejected() -> None:
    source = """10 ENUM Empty
20 .ENDENUM
200 SUB main AS PUBLIC AS VOID
210 PRINT "x"
220 .ENDSUB
230 CALL main
240 END
"""
    expect_error(source, "ENUM 至少要有一个成员")


# --- SYMBOL 代数 ---

def _sym(body: str) -> str:
    return _wrap(
        "10 DIM x AS NUM AS LONG AS VAR\n20 DIM f AS SYMBOL AS VAR\n30 DIM g AS SYMBOL AS VAR\n40 DIM v AS NUM AS DOUBLE AS VAR",
        body,
    )


def test_deriv_generates_runtime_call() -> None:
    c = compile_c(_sym("210 x = 2\n220 f = x * x\n230 g = DERIV(f, \"x\")"))
    assert "sa_symbol_deriv(" in c


def test_symbol_power_operator_builds_power_node() -> None:
    c = compile_c(_sym("210 x = 2\n220 f = x ** 3\n230 g = SIMPLIFY(DERIV(f, \"x\"))"))
    assert "sa_symbol_op('^'" in c
    assert "sa_symbol_deriv(" in c


def test_sys_math_pow_can_build_symbol_expression() -> None:
    c = compile_c(_wrap(
        "10 DIM x AS NUM AS LONG AS VAR\n20 DIM f AS SYMBOL AS VAR",
        "210 f = M.POW(x, 2)",
        uses="5 USE SYS.MATH AS M",
    ))
    assert "sa_symbol_op('^'" in c


def test_simplify_generates_runtime_call() -> None:
    c = compile_c(_sym("210 x = 2\n220 f = x * x\n230 g = SIMPLIFY(f)"))
    assert "sa_symbol_simplify(" in c


def test_subst_and_eval_generate_runtime_calls() -> None:
    c = compile_c(_sym("210 x = 2\n220 f = x * x\n230 v = EVAL(SUBST(f, \"x\", 3))"))
    assert "sa_symbol_subst(" in c
    assert "sa_symbol_eval(" in c


def test_deriv_var_must_be_string_literal() -> None:
    expect_error(
        _sym("210 x = 2\n220 f = x * x\n230 g = DERIV(f, x)"),
        "变量名必须是字符串字面量",
    )


def test_eval_returns_double() -> None:
    # EVAL 结果是 DOUBLE，可赋给 DOUBLE 变量
    check_program(parse_program(_sym("210 x = 2\n220 f = x * x\n230 v = EVAL(f)")))


def test_deriv_first_arg_must_be_symbol() -> None:
    expect_error(
        _wrap(
            "10 DIM n AS NUM AS LONG AS VAR\n20 DIM g AS SYMBOL AS VAR",
            "210 n = 5\n220 g = DERIV(n, \"x\")",
        ),
        "第一个参数必须是 SYMBOL",
    )
