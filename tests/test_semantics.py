"""语义与解析约束测试：断言非法源码被正确拒绝，合法源码被接受。"""
from __future__ import annotations

from conftest import compile_c, expect_error
from sonalgebraic.frontend.parser import parse_program
from sonalgebraic.analysis.semantics import check_program


def test_line_numbers_must_increase() -> None:
    expect_error(
        "10 SUB main AS PUBLIC AS VOID\n10 .ENDSUB\n20 CALL main\n30 END",
        "行号必须严格递增",
    )


def test_top_level_executable_code_is_rejected() -> None:
    expect_error(
        "10 DIM x AS NUM AS LONG AS VAR\n20 SUB main AS PUBLIC AS VOID\n30 RETURN\n40 .ENDSUB\n50 PRINT x\n60 CALL main\n70 END",
        "顶层只能放",
    )


def test_undeclared_variable_is_rejected() -> None:
    expect_error(
        "10 SUB main AS PUBLIC AS VOID\n20 PRINT missing\n30 .ENDSUB\n40 CALL main\n50 END",
        "变量未声明",
    )


def test_return_inside_if_reports_compile_error() -> None:
    source = """10 SUB main AS PUBLIC AS VOID
20 IF 1 THEN
30 RETURN 1
40 END IF
50 .ENDSUB
60 CALL main
70 END
"""
    expect_error(source, "VOID SUB 不能 RETURN 值")


def test_non_void_sub_requires_definite_return() -> None:
    source = """10 DIM result AS NUM AS LONG AS VAR
20 SUB maybe(flag AS NUM AS LONG) AS NUM AS LONG
30 IF flag THEN
40 RETURN 1
50 END IF
60 .ENDSUB
70 SUB main AS PUBLIC AS VOID
80 result = CALL maybe(1)
90 PRINT result
100 .ENDSUB
110 CALL main
120 END
"""
    expect_error(source, "非 VOID SUB 必须保证")


def test_use_sys_io_requires_registered_alias() -> None:
    source = """10 USE SYS.IO AS CONSOLE
20 DIM name AS STRING AS VAR
30 SUB main AS PUBLIC AS VOID
40 CONSOLE.INPUT "Name: ", name
50 PRINT name
60 .ENDSUB
70 CALL main
80 END
"""
    # 注册了 alias，应当通过
    check_program(parse_program(source))


def test_unregistered_io_alias_is_rejected() -> None:
    expect_error(
        "10 DIM name AS STRING AS VAR\n20 SUB main AS PUBLIC AS VOID\n30 IO.INPUT \"Name: \", name\n40 .ENDSUB\n50 CALL main\n60 END",
        "USE SYS.IO",
    )


def test_unregistered_math_alias_is_rejected() -> None:
    expect_error(
        "10 DIM area AS NUM AS DOUBLE AS VAR\n20 SUB main AS PUBLIC AS VOID\n30 area = M.PI * M.POW(2.0, 2.0)\n40 .ENDSUB\n50 CALL main\n60 END",
        "变量未声明",
    )


def test_uselib_adds_link_lib() -> None:
    source = '''10 USELIB "curl" AS CURL_LIB
20 SUB main AS PUBLIC AS VOID
30 PRINT "ok"
40 .ENDSUB
50 CALL main
60 END
'''
    checked = check_program(parse_program(source))
    assert checked.c_libs
    assert checked.c_libs["curl_lib"].library == "curl"


def test_ref_parameter_rejects_const_argument() -> None:
    expect_error(
        '''10 CONST value AS NUM AS LONG = 1
20 SUB change(target AS NUM AS LONG AS REF) AS VOID
30 target = 2
40 .ENDSUB
50 SUB main AS PUBLIC AS VOID
60 CALL change(value)
70 .ENDSUB
80 CALL main
90 END
''',
        "不能传入 CONST",
    )
