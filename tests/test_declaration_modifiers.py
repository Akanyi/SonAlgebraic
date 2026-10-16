"""DIM 默认可变，AS VAR 是显式风格而不是必填；CONST 仍需明确初始化。"""
import pytest

from conftest import compile_c, expect_error
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.frontend.parser import parse_program


@pytest.mark.parametrize("type_text", ["STRING", "BOOL", "NUM AS LONG", "HANDLE AS FILE", "PTR TO NUM AS LONG"])
def test_dim_var_modifier_is_optional(type_text: str) -> None:
    implicit = parse_program(f"10 DIM value AS {type_text}\n")
    explicit = parse_program(f"10 DIM value AS {type_text} AS VAR\n")
    assert implicit.declarations == explicit.declarations
    check_program(implicit, require_main=False)


def test_local_and_entity_fields_allow_implicit_var() -> None:
    source = """10 FOR ENTITY AS Note
20 DIM text AS STRING
30 .ENDENTITY
40 SUB main AS PUBLIC AS VOID
50 DIM note AS ENTITY AS Note
60 note.text = "hello"
70 PRINT note.text
80 .ENDSUB
90 CALL main
100 END
"""
    code = compile_c(source)
    assert 'sa_set_string(&sa_note.text, "hello")' in code
    assert "free(sa_note.text)" in code


@pytest.mark.parametrize(("declaration", "message"), [
    ("CONST value AS STRING", "CONST 必须提供初始值"),
    ('CONST value AS STRING AS VAR = "x"', "CONST 不能带"),
    ("DIM value AS VAR", "声明缺少类型"),
    ("DIM value AS STRING AS VAR AS VAR", "无法识别的类型声明"),
])
def test_invalid_declaration_modifiers_still_fail(declaration: str, message: str) -> None:
    expect_error("10 " + declaration + "\n", message)
