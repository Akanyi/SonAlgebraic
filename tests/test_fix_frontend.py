"""前端（词法/表达式/语句解析）修复的回归测试。

这里只覆盖 parser.py / expr_parser.py / expr_lexer.py 三个文件的行为，
尽量直接断言 AST 或诊断文案，不依赖 C 工具链。
"""
from __future__ import annotations

import pytest

from conftest import compile_c
from sonalgebraic.core import ast
from sonalgebraic.core.errors import SonCompileError
from sonalgebraic.frontend.expr_parser import parse_expr
from sonalgebraic.frontend.parser import parse_program


def _wrap(body: str) -> str:
    return f"10 SUB main AS PUBLIC AS VOID\n{body}\n900 .ENDSUB\n910 CALL main\n920 END\n"


def _parse_error(source: str) -> SonCompileError:
    with pytest.raises(SonCompileError) as excinfo:
        parse_program(source)
    return excinfo.value


# --- F-string 插值扫描 ---------------------------------------------------


def test_fstring_interpolation_allows_same_quote_string() -> None:
    expr = parse_expr('F"got {pick("x", "y")} done"', 10)
    assert isinstance(expr, ast.FString)
    assert expr.parts[0] == "got "
    call = expr.parts[1]
    assert isinstance(call, ast.CallExpr) and call.name == "pick"
    assert [arg.value for arg in call.args] == ["x", "y"]
    assert expr.parts[2] == " done"


def test_fstring_interpolation_allows_brace_inside_string() -> None:
    expr = parse_expr('F"a {f("}")} b"', 10)
    call = expr.parts[1]
    assert isinstance(call, ast.CallExpr)
    assert call.args[0].value == "}"
    assert expr.parts[2] == " b"


def test_fstring_interpolation_keeps_legacy_escaped_quotes() -> None:
    # 旧写法 \"..\"（examples/net_tls.sa 在用）必须继续可解析
    expr = parse_expr('F"tls={f(resp, \\"HTTP/\\") = 0} end"', 10)
    binary = expr.parts[1]
    assert isinstance(binary, ast.Binary) and binary.op == "="
    assert binary.left.args[1].value == "HTTP/"


def test_fstring_interpolation_escape_is_not_double_expanded() -> None:
    # 插值段不能被展开两遍：`"a\\nb"` 里的反斜杠要原样留给 parse_expr
    expr = parse_expr('F"{g("a\\\\nb")}"', 10)
    assert expr.parts[0].args[0].value == "a\\nb"


def test_fstring_supports_backslash_brace_escape() -> None:
    expr = parse_expr('F"esc \\{ok\\} and {n}"', 10)
    assert expr.parts[0] == "esc {ok} and "
    assert isinstance(expr.parts[1], ast.VarRef)


def test_fstring_unterminated_interpolation_still_reports_missing_brace() -> None:
    with pytest.raises(SonCompileError, match="F-string 缺少右花括号"):
        parse_expr('F"broken {x"', 10)


# --- CAST 到 ENTITY / HANDLE ---------------------------------------------


def test_cast_to_entity_pointer_parses() -> None:
    expr = parse_expr("CAST PTR TO ENTITY AS Hero (raw)", 10)
    assert isinstance(expr, ast.Cast)
    assert expr.type_spec.name == "PTR"
    assert expr.type_spec.inner.name == "ENTITY"
    assert expr.type_spec.inner.subtype == "Hero"


def test_cast_to_handle_parses() -> None:
    expr = parse_expr("CAST HANDLE AS LIST (xs)", 10)
    assert expr.type_spec.name == "HANDLE"
    assert expr.type_spec.subtype == "LIST"


def test_cast_to_entity_pointer_compiles() -> None:
    source = (
        "10 FOR ENTITY AS Hero\n20 DIM hp AS NUM AS LONG AS VAR\n30 .ENDENTITY\n"
        "40 SUB main AS PUBLIC AS VOID\n"
        "50 DIM h AS ENTITY AS Hero AS VAR\n"
        "60 DIM raw AS CPTR AS VAR\n"
        "70 DIM back AS PTR TO ENTITY AS Hero AS VAR\n"
        "80 raw = CAST CPTR (@h)\n"
        "90 back = CAST PTR TO ENTITY AS Hero (raw)\n"
        "100 .ENDSUB\n110 CALL main\n120 END\n"
    )
    assert "SaEntity_hero* sa_back" in compile_c(source)


# --- INPUT 字段赋值 -------------------------------------------------------


def test_assign_to_entity_field_named_input_is_not_io_input() -> None:
    source = (
        "10 FOR ENTITY AS Cfg\n20 DIM INPUT AS STRING AS VAR\n30 .ENDENTITY\n"
        "40 SUB main AS PUBLIC AS VOID\n"
        "50 DIM cfg AS ENTITY AS Cfg AS VAR\n"
        "60 cfg.INPUT = \"x\"\n"
        "70 .ENDSUB\n80 CALL main\n90 END\n"
    )
    stmt = parse_program(source).subs[0].body[1]
    assert isinstance(stmt, ast.Assign)
    assert isinstance(stmt.target, ast.VarRef) and stmt.target.name == "cfg.INPUT"


def test_io_input_statement_still_parses() -> None:
    source = (
        "10 USE SYS.IO AS IO\n20 SUB main AS PUBLIC AS VOID\n"
        "30 DIM name AS STRING AS VAR\n40 IO.INPUT \"name? \", name\n"
        "50 .ENDSUB\n60 CALL main\n70 END\n"
    )
    stmt = parse_program(source).subs[0].body[1]
    assert isinstance(stmt, ast.Input) and stmt.target == "name"


def test_io_input_missing_target_still_reports_its_own_error() -> None:
    source = (
        "10 USE SYS.IO AS IO\n20 SUB main AS PUBLIC AS VOID\n"
        "30 IO.INPUT \"name? \"\n40 .ENDSUB\n50 CALL main\n60 END\n"
    )
    assert "IO.INPUT 必须提供提示文本和目标变量" in _parse_error(source).message


# --- NOT 优先级（行为变更） ----------------------------------------------


def test_not_binds_looser_than_comparison() -> None:
    expr = parse_expr("NOT a = b", 10)
    assert isinstance(expr, ast.Unary) and expr.op == "NOT"
    assert isinstance(expr.expr, ast.Binary) and expr.expr.op == "="


def test_not_binds_tighter_than_and() -> None:
    expr = parse_expr("NOT a = b AND c", 10)
    assert isinstance(expr, ast.Binary) and expr.op == "AND"
    assert isinstance(expr.left, ast.Unary) and expr.left.op == "NOT"


def test_bnot_and_unary_minus_keep_high_precedence() -> None:
    assert isinstance(parse_expr("BNOT a + b", 10), ast.Binary)
    minus = parse_expr("-2 ** 2", 10)
    assert isinstance(minus, ast.Unary) and isinstance(minus.expr, ast.Binary)


def test_not_comparison_generates_negated_condition() -> None:
    c = compile_c(_wrap("20 DIM a AS NUM AS LONG AS VAR\n30 a = 1\n40 IF NOT a = 2 THEN\n50 PRINT \"ok\"\n60 END IF"))
    assert "if ((!(sa_a == 2)))" in c


# --- 嵌套块缺终结符的错误恢复 --------------------------------------------


def test_if_missing_end_if_reports_the_if_line() -> None:
    source = (
        "10 SUB main AS PUBLIC AS VOID\n20 DIM a AS NUM AS LONG AS VAR\n"
        "30 FOR a = 1 TO 3\n40 IF a > 1 THEN\n50 PRINT \"big\"\n"
        "60 .ENDFOR\n70 .ENDSUB\n80 CALL main\n90 END\n"
    )
    error = _parse_error(source)
    assert error.message == "IF 缺少 END IF 或 .ENDIF"
    assert error.line_no == 40


def test_for_missing_endfor_reports_the_for_line() -> None:
    source = (
        "10 DIM a AS NUM AS LONG AS VAR\n20 FOR a = 1 TO 3\n30 PRINT a\n"
        "40 SUB foo() AS PUBLIC AS VOID\n50 PRINT \"x\"\n60 .ENDSUB\n70 END\n"
    )
    error = _parse_error(source)
    assert error.message == "FOR 缺少 .ENDFOR"
    assert error.line_no == 20


def test_while_missing_endwhile_reports_the_while_line() -> None:
    source = (
        "10 SUB main AS PUBLIC AS VOID\n20 DIM i AS NUM AS LONG AS VAR\n"
        "30 WHILE i < 3\n40 i = i + 1\n50 .ENDSUB\n60 CALL main\n70 END\n"
    )
    error = _parse_error(source)
    assert error.message == "WHILE 缺少 .ENDWHILE"
    assert error.line_no == 30


def test_sub_missing_endsub_reports_the_header_line() -> None:
    source = (
        "10 SUB helper AS PUBLIC AS VOID\n20 PRINT \"hi\"\n"
        "30 SUB main AS PUBLIC AS VOID\n40 CALL helper\n50 .ENDSUB\n60 CALL main\n70 END\n"
    )
    error = _parse_error(source)
    assert error.message == "SUB 缺少 .ENDSUB"
    assert error.line_no == 10


def test_try_missing_endtry_reports_the_try_line() -> None:
    source = (
        "10 SUB boom AS PUBLIC AS VOID\n20 THROW NEW RANGE, \"bad\"\n30 .ENDSUB\n"
        "40 SUB main AS PUBLIC AS VOID\n50 TRY CALL boom TRACEBACK ERROR AS trap\n"
        "60 CATCH RANGE AS e\n70 PRINT \"caught\"\n80 .ENDSUB\n90 CALL main\n100 END\n"
    )
    error = _parse_error(source)
    assert error.message == "TRY 缺少 .ENDTRY"
    assert error.line_no == 50


# --- 词法层收口 -----------------------------------------------------------


def test_unknown_escape_is_rejected() -> None:
    with pytest.raises(SonCompileError, match="未知转义序列"):
        parse_expr('"C:\\data"', 10)


def test_known_escapes_still_work() -> None:
    assert parse_expr('"a\\nb\\tc\\\\d\\"e"', 10).value == 'a\nb\tc\\d"e'


def test_trailing_dot_identifier_is_rejected() -> None:
    with pytest.raises(SonCompileError, match="标识符中的成员路径不完整"):
        parse_expr("y.", 10)


def test_double_dot_identifier_is_rejected() -> None:
    with pytest.raises(SonCompileError, match="标识符中的成员路径不完整"):
        parse_expr("a..b", 10)


def test_sub_header_allows_space_before_params() -> None:
    source = (
        "10 SUB f (a AS NUM AS LONG) AS PUBLIC AS VOID\n20 PRINT a\n30 .ENDSUB\n"
        "40 SUB main AS PUBLIC AS VOID\n50 CALL f(7)\n60 .ENDSUB\n70 CALL main\n80 END\n"
    )
    sub = parse_program(source).subs[0]
    assert [param.name for param in sub.params] == ["a"]
    assert sub.visibility == "PUBLIC"


def test_string_token_pos_points_at_opening_quote() -> None:
    from sonalgebraic.frontend.expr_lexer import tokenize_expr

    tokens = tokenize_expr('x + "ab" + F"c{d}"', 10)
    assert [(token.kind, token.pos) for token in tokens if token.kind in {"STRING", "FSTRING"}] == [
        ("STRING", 4),
        ("FSTRING", 11),
    ]
