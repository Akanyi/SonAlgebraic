"""第三批语言特性测试：位运算 + SYS.STRING 字符串操作。"""
from __future__ import annotations

from conftest import compile_c, expect_error
from sonalgebraic.frontend.parser import parse_program
from sonalgebraic.analysis.semantics import check_program


def _wrap(decls: str, body: str, uses: str = "") -> str:
    head = f"{uses}\n" if uses else ""
    return f"{head}{decls}\n100 SUB main AS PUBLIC AS VOID\n{body}\n800 .ENDSUB\n810 CALL main\n820 END\n"


# --- 位运算 ---

def test_bitwise_and_maps_to_c() -> None:
    c = compile_c(_wrap("10 DIM a AS NUM AS LONG AS VAR", "110 a = 12 BAND 10"))
    assert "(12 & 10)" in c


def test_bitwise_or_xor_map_to_c() -> None:
    c = compile_c(_wrap("10 DIM a AS NUM AS LONG AS VAR", "110 a = 12 BOR 10\n120 a = 12 BXOR 10"))
    assert "(12 | 10)" in c
    assert "(12 ^ 10)" in c


def test_shift_operators_map_to_c() -> None:
    c = compile_c(_wrap("10 DIM a AS NUM AS LONG AS VAR", "110 a = 1 SHL 4\n120 a = 256 SHR 2"))
    assert "(1 << 4)" in c
    assert "(256 >> 2)" in c


def test_bnot_maps_to_c_tilde() -> None:
    c = compile_c(_wrap("10 DIM a AS NUM AS LONG AS VAR", "110 a = BNOT 0"))
    assert "(~0)" in c


def test_bitwise_precedence_shift_over_band() -> None:
    # SHL 优先级高于 BAND：a BAND b SHL c 应解析为 a BAND (b SHL c)
    c = compile_c(_wrap("10 DIM a AS NUM AS LONG AS VAR", "110 a = 1 BAND 2 SHL 3"))
    assert "(1 & (2 << 3))" in c


def test_bitwise_on_string_rejected() -> None:
    expect_error(
        _wrap("10 DIM s AS STRING AS VAR\n20 DIM a AS NUM AS LONG AS VAR", "110 s = \"x\"\n120 a = s BAND 1"),
        "位运算只能用于整数",
    )


# --- SYS.STRING ---

def test_string_length_returns_long() -> None:
    c = compile_c(_wrap(
        "10 DIM s AS STRING AS VAR\n20 DIM n AS NUM AS LONG AS VAR",
        "110 s = \"hi\"\n120 n = STR.LENGTH(s)",
        uses="5 USE SYS.STRING AS STR",
    ))
    assert "sa_str_length(sa_s)" in c


def test_string_concat_allocates_and_frees() -> None:
    c = compile_c(_wrap(
        "10 DIM a AS STRING AS VAR\n20 DIM b AS STRING AS VAR\n30 DIM r AS STRING AS VAR",
        "110 a = \"x\"\n120 b = \"y\"\n130 r = STR.CONCAT(a, b)",
        uses="5 USE SYS.STRING AS STR",
    ))
    assert "sa_str_concat(sa_a, sa_b)" in c
    assert "free(" in c


def test_string_slice_and_find() -> None:
    c = compile_c(_wrap(
        "10 DIM s AS STRING AS VAR\n20 DIM part AS STRING AS VAR\n30 DIM p AS NUM AS LONG AS VAR",
        "110 s = \"hello\"\n120 part = STR.SLICE(s, 0, 3)\n130 p = STR.FIND(s, \"ll\")",
        uses="5 USE SYS.STRING AS STR",
    ))
    assert "sa_str_slice(sa_s, 0, 3)" in c
    assert "sa_str_find(sa_s," in c


def test_string_upper_lower() -> None:
    c = compile_c(_wrap(
        "10 DIM s AS STRING AS VAR\n20 DIM u AS STRING AS VAR",
        "110 s = \"hi\"\n120 u = STR.UPPER(s)\n130 u = STR.LOWER(s)",
        uses="5 USE SYS.STRING AS STR",
    ))
    assert "sa_str_upper(sa_s)" in c
    assert "sa_str_lower(sa_s)" in c


def test_string_replace() -> None:
    c = compile_c(_wrap(
        "10 DIM s AS STRING AS VAR\n20 DIM r AS STRING AS VAR",
        "110 s = \"a-b-c\"\n120 r = STR.REPLACE(s, \"-\", \"+\")",
        uses="5 USE SYS.STRING AS STR",
    ))
    assert "sa_str_replace(sa_s," in c


def test_string_function_arg_count_checked() -> None:
    expect_error(
        _wrap(
            "10 DIM s AS STRING AS VAR\n20 DIM n AS NUM AS LONG AS VAR",
            "110 s = \"x\"\n120 n = STR.LENGTH(s, s)",
            uses="5 USE SYS.STRING AS STR",
        ),
        "需要 1 个参数",
    )


def test_string_function_arg_type_checked() -> None:
    expect_error(
        _wrap(
            "10 DIM n AS NUM AS LONG AS VAR",
            "110 n = STR.LENGTH(42)",
            uses="5 USE SYS.STRING AS STR",
        ),
        "类型不兼容",
    )


def test_string_module_without_use_rejected() -> None:
    # 没有 USE SYS.STRING，STR.LENGTH 解析不到
    expect_error(
        _wrap("10 DIM n AS NUM AS LONG AS VAR", "110 n = STR.LENGTH(\"x\")"),
        "未知内置函数或 SUB",
    )
