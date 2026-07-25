"""第二批语言特性测试：数组（方括号定长）+ FOR/WHILE 循环。"""
from __future__ import annotations

from conftest import compile_c, expect_error
from sonalgebraic.frontend.parser import parse_program
from sonalgebraic.analysis.semantics import check_program


def _wrap(decls: str, body: str) -> str:
    return f"{decls}\n100 SUB main AS PUBLIC AS VOID\n{body}\n800 .ENDSUB\n810 CALL main\n820 END\n"


# --- 数组 ---

def test_array_declaration_generates_c_array() -> None:
    c = compile_c(_wrap("10 DIM xs[5] AS NUM AS LONG AS VAR", "110 xs[0] = 1"))
    assert "long long sa_xs[5] = {0};" in c


def test_array_element_assignment_and_read() -> None:
    c = compile_c(_wrap(
        "10 DIM xs[3] AS NUM AS LONG AS VAR\n20 DIM v AS NUM AS LONG AS VAR",
        "110 xs[0] = 42\n120 v = xs[0]",
    ))
    assert "sa_xs[0] = 42;" in c
    assert "sa_v = sa_xs[0];" in c


def test_array_double_element() -> None:
    c = compile_c(_wrap("10 DIM ds[4] AS NUM AS DOUBLE AS VAR", "110 ds[1] = 3.5"))
    assert "double sa_ds[4] = {0};" in c


def test_array_string_element_supported() -> None:
    # STRING 数组现已支持：每元素 sa_strdup 初始化，赋值用 sa_set_string，清理逐元素 free
    c = compile_c(_wrap("10 DIM names[3] AS STRING AS VAR", "110 names[0] = \"x\""))
    assert "char* sa_names[3] = {0};" in c
    assert "sa_strdup(\"\")" in c
    assert "sa_set_string(&sa_names[0]" in c


def test_array_symbol_element_rejected() -> None:
    expect_error(
        _wrap("10 DIM fs[3] AS SYMBOL AS VAR", "110 PRINT \"x\""),
        "数组暂不支持 SYMBOL 元素类型",
    )


def test_array_index_must_be_numeric() -> None:
    expect_error(
        _wrap("10 DIM xs[3] AS NUM AS LONG AS VAR\n20 DIM s AS STRING AS VAR", "110 s = \"k\"\n120 xs[s] = 1"),
        "数组下标必须是整数",
    )


def test_index_on_non_array_rejected() -> None:
    expect_error(
        _wrap("10 DIM n AS NUM AS LONG AS VAR", "110 n = 1\n120 n[0] = 2"),
        "下标访问只能用于数组",
    )


def test_zero_length_array_rejected() -> None:
    expect_error(
        _wrap("10 DIM xs[0] AS NUM AS LONG AS VAR", "110 PRINT \"x\""),
        "数组长度必须是正整数",
    )


# --- FOR 循环 ---

def test_for_loop_generates_c_for() -> None:
    c = compile_c(_wrap(
        "10 DIM i AS NUM AS LONG AS VAR",
        "110 FOR i = 0 TO 4\n120 PRINT i\n130 .ENDFOR",
    ))
    assert "for (;" in c
    assert "sa_i += " in c


def test_for_loop_with_step() -> None:
    c = compile_c(_wrap(
        "10 DIM i AS NUM AS LONG AS VAR",
        "110 FOR i = 10 TO 0 STEP -5\n120 PRINT i\n130 .ENDFOR",
    ))
    # 步长存进临时变量，条件用三元处理正负方向
    assert ">= 0 ?" in c


def test_for_loop_var_must_be_declared() -> None:
    expect_error(
        _wrap("10 DIM x AS NUM AS LONG AS VAR", "110 FOR undeclared = 0 TO 4\n120 PRINT x\n130 .ENDFOR"),
        "变量未声明",
    )


def test_for_loop_var_must_be_numeric() -> None:
    expect_error(
        _wrap("10 DIM s AS STRING AS VAR", "110 s = \"x\"\n120 FOR s = 0 TO 4\n130 PRINT s\n140 .ENDFOR"),
        "FOR 循环变量必须是数值类型",
    )


def test_for_missing_endfor_rejected() -> None:
    expect_error(
        _wrap("10 DIM i AS NUM AS LONG AS VAR", "110 FOR i = 0 TO 4\n120 PRINT i"),
        "FOR 缺少 .ENDFOR",
    )


# --- WHILE 循环 ---

def test_while_loop_generates_c_while() -> None:
    c = compile_c(_wrap(
        "10 DIM n AS NUM AS LONG AS VAR",
        "110 n = 3\n120 WHILE n > 0\n130 n = n - 1\n140 .ENDWHILE",
    ))
    assert "while (1) {" in c
    assert "break;" in c


def test_while_missing_endwhile_rejected() -> None:
    expect_error(
        _wrap("10 DIM n AS NUM AS LONG AS VAR", "110 n = 3\n120 WHILE n > 0\n130 n = n - 1"),
        "WHILE 缺少 .ENDWHILE",
    )


def test_return_inside_loop_type_checked() -> None:
    # 循环体内的 RETURN 类型应被检查
    source = """10 SUB find(n AS NUM AS LONG) AS NUM AS LONG
20 DIM i AS NUM AS LONG AS VAR
30 FOR i = 0 TO n
40 RETURN i
50 .ENDFOR
60 RETURN 0
70 .ENDSUB
80 DIM r AS NUM AS LONG AS VAR
90 SUB main AS PUBLIC AS VOID
100 r = CALL find(5)
110 .ENDSUB
120 CALL main
130 END
"""
    check_program(parse_program(source))
