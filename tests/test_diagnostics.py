"""诊断系统测试：渲染下划线、一次收集多个错误、语法恢复。"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from sonalgebraic.analysis.diagnostics import Diagnostic, render_diagnostics
from sonalgebraic.core.errors import SonCompileError
from sonalgebraic.driver.compiler import collect_source_diagnostics


def test_diagnostic_renderer_marks_source_line() -> None:
    source = "10 SUB main AS PUBLIC AS VOID\n20 PRINT missing\n30 .ENDSUB\n40 CALL main\n50 END\n"
    diagnostic = Diagnostic.from_compile_error(SonCompileError("变量未声明: missing", 20))
    rendered = render_diagnostics("broken.sa", source, [diagnostic])

    assert "broken.sa:20:1 error: 变量未声明: missing" in rendered
    assert "20 PRINT missing" in rendered
    assert "^" in rendered


def test_diagnostic_renderer_underlines_undeclared_name() -> None:
    source = "10 SUB main AS PUBLIC AS VOID\n20 PRINT missing\n30 .ENDSUB\n40 CALL main\n50 END\n"
    diagnostic = Diagnostic.from_compile_error(SonCompileError("变量未声明: missing", 20), source)
    rendered = render_diagnostics("broken.sa", source, [diagnostic])

    assert "20 PRINT missing" in rendered
    assert "         ^^^^^^^" in rendered


def test_collect_source_diagnostics_reports_multiple_undeclared_variables() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        source = Path(temp) / "main.sa"
        source.write_text(
            "10 SUB main AS PUBLIC AS VOID\n20 PRINT missing_one\n30 PRINT missing_two\n40 .ENDSUB\n50 CALL main\n60 END\n",
            encoding="utf-8",
        )

        diagnostics = collect_source_diagnostics(source)
        messages = [str(item) for item in diagnostics]
        assert len(diagnostics) == 2
        assert any("变量未声明: missing_one" in message for message in messages)
        assert any("变量未声明: missing_two" in message for message in messages)


def test_collect_source_diagnostics_recovers_from_multiple_parse_errors() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        source = Path(temp) / "main.sa"
        source.write_text(
            "10 SUB main AS PUBLIC AS VOID\n"
            "20 PRINT F\"broken {x\"\n"
            "30 ELSE\n"
            "40 PRINT missing\n"
            "50 .ENDSUB\n"
            "60 CALL main\n"
            "70 END\n",
            encoding="utf-8",
        )

        diagnostics = collect_source_diagnostics(source)
        messages = [str(item) for item in diagnostics]
        assert len(diagnostics) >= 3
        assert any("F-string 缺少右花括号" in message for message in messages)
        assert any("无法解析的语句: ELSE" in message for message in messages)
        assert any("变量未声明: missing" in message for message in messages)


def test_collect_source_diagnostics_reports_multiple_type_mismatches() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        source = Path(temp) / "main.sa"
        source.write_text(
            "10 DIM n AS NUM AS LONG AS VAR\n"
            "20 DIM text AS STRING AS VAR\n"
            "30 SUB main AS PUBLIC AS VOID\n"
            "40 n = \"nope\"\n"
            "50 text = 1\n"
            "60 .ENDSUB\n"
            "70 CALL main\n"
            "80 END\n",
            encoding="utf-8",
        )

        diagnostics = collect_source_diagnostics(source)
        messages = [str(item) for item in diagnostics]
        assert len(diagnostics) == 2
        assert sum("赋值两侧类型不兼容" in message for message in messages) == 2


def test_strip_trailing_comment_handles_rem_and_strings() -> None:
    # 行尾 REM 注释剥离：REM 标记「该行剩余部分」为注释，可跟在语句之后。
    # 必须跳过字符串内的 REM，且只在 REM 作为独立单词时才截断（不误伤 PREMIUM 等）。
    from sonalgebraic.core.lines import strip_trailing_comment as strip

    assert strip("x = 1 REM 注释") == "x = 1"
    assert strip("RETURN  REM 返回") == "RETURN"
    assert strip("REM 整行注释") == ""
    assert strip('PRINT "PREMIUM"') == 'PRINT "PREMIUM"'
    assert strip('s = "say REM here"') == 's = "say REM here"'
    assert strip("x = premium") == "x = premium"
    assert strip('PRINT F"a={a} REM b"') == 'PRINT F"a={a} REM b"'


def test_trailing_rem_comment_compiles() -> None:
    # 端到端：带行尾 REM 的源码应正常编译，不再报「表达式后存在无法解析的内容」。
    from conftest import compile_c

    source = """10 DIM n AS NUM AS LONG AS VAR
20 SUB main AS PUBLIC AS VOID
30 n = 2 * 3 REM 计算结果
40 PRINT F"n={n}" REM 输出
50 RETURN REM 收尾
60 .ENDSUB
70 CALL main
80 END
"""
    c = compile_c(source)
    assert "sa_n = (2 * 3);" in c
