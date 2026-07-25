"""CLI 命令测试：check / run / fmt 的退出码、诊断输出、行号重排。"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import io

from sonalgebraic.__main__ import main as cli_main
from sonalgebraic.driver.formatter import renumber_source


def test_cli_check_command_returns_zero() -> None:
    assert cli_main(["check", "examples/hello.sa"]) == 0


def test_cli_native_ir_command_writes_llvm_ir() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        output = Path(temp) / "hello.ll"
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli_main(["native-ir", "examples/hello.sa", "-o", str(output)])

        assert exit_code == 0
        assert stderr.getvalue() == ""
        assert stdout.getvalue().strip() == str(output)
        text = output.read_text(encoding="utf-8")
        assert "define void @sa_main()" in text
        assert "@printf" in text


def test_cli_check_reports_multiple_errors_with_caret() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        source = Path(temp) / "broken.sa"
        source.write_text(
            "10 SUB main AS PUBLIC AS VOID\n"
            "20 PRINT missing\n"
            "30 PRINT also_missing\n"
            "40 .ENDSUB\n"
            "50 CALL main\n"
            "60 END\n",
            encoding="utf-8",
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli_main(["check", str(source)])

        output = stderr.getvalue()
        assert exit_code == 1
        assert output.count("error:") >= 2
        assert "变量未声明: missing" in output
        assert "变量未声明: also_missing" in output
        assert "^" in output


def test_cli_run_reports_diagnostics_before_building() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        source = Path(temp) / "broken.sa"
        source.write_text(
            "10 SUB main AS PUBLIC AS VOID\n"
            "20 PRINT F\"broken {x\"\n"
            "30 PRINT missing\n"
            "40 .ENDSUB\n"
            "50 CALL main\n"
            "60 END\n",
            encoding="utf-8",
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli_main(["run", str(source)])

        output = stderr.getvalue()
        assert exit_code == 1
        assert "F-string 缺少右花括号" in output
        assert "变量未声明: missing" in output
        assert "^" in output


def test_renumber_source_preserves_blank_lines() -> None:
    source = '100 PRINT "a"\n\n250 PRINT "b"\n'
    assert renumber_source(source, step=5) == '5 PRINT "a"\n\n10 PRINT "b"\n'


def test_cli_fmt_can_write_to_output() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        source = temp_dir / "messy.sa"
        output = temp_dir / "formatted.sa"
        source.write_text('100 PRINT "a"\n250 PRINT "b"\n', encoding="utf-8")

        assert cli_main(["fmt", str(source), "-o", str(output), "--renumber", "20"]) == 0
        assert output.read_text(encoding="utf-8") == '20 PRINT "a"\n40 PRINT "b"\n'
