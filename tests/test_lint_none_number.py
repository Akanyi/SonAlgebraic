from __future__ import annotations

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from conftest import compile_c, expect_error, requires_c_compiler
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.core.lines import apply_lint_source, detect_lint_options, read_numbered_lines
from sonalgebraic.driver.compiler import build_exe
from sonalgebraic.frontend.parser import parse_program


def test_detect_lint_none_number_without_line_numbers() -> None:
    source = "USE SYS.LINT AS NONE_NUMBER\nSUB main AS PUBLIC AS VOID\nPRINT \"hi\"\n.ENDSUB\nCALL main\nEND\n"
    assert detect_lint_options(source) == {"NONE_NUMBER"}


def test_apply_lint_source_auto_numbers_nonempty_lines() -> None:
    source = "USE SYS.LINT AS NONE_NUMBER\n\nSUB main AS PUBLIC AS VOID\nPRINT \"hi\"\n.ENDSUB\nCALL main\nEND\n"
    assert apply_lint_source(source) == (
        "10 USE SYS.LINT AS NONE_NUMBER\n"
        "\n"
        "20 SUB main AS PUBLIC AS VOID\n"
        "30 PRINT \"hi\"\n"
        "40 .ENDSUB\n"
        "50 CALL main\n"
        "60 END\n"
    )


def test_parse_program_accepts_none_number_mode() -> None:
    source = """USE SYS.LINT AS NONE_NUMBER
SUB main AS PUBLIC AS VOID
PRINT "no line numbers"
.ENDSUB
CALL main
END
"""
    program = parse_program(source)
    checked = check_program(program)
    assert checked.uses["none_number"] == "SYS.LINT"
    assert program.source_lines[10] == "USE SYS.LINT AS NONE_NUMBER"
    assert program.source_lines[30] == 'PRINT "no line numbers"'
    c = compile_c(source)
    assert "no line numbers" in c


def test_missing_line_numbers_still_rejected_without_lint() -> None:
    expect_error(
        'SUB main AS PUBLIC AS VOID\nPRINT "x"\n.ENDSUB\nCALL main\nEND\n',
        "USE SYS.LINT AS NONE_NUMBER",
    )


def test_unknown_lint_option_is_rejected() -> None:
    expect_error(
        "10 USE SYS.LINT AS MAGIC\n20 SUB main AS PUBLIC AS VOID\n30 .ENDSUB\n40 CALL main\n50 END\n",
        "未知 SYS.LINT 选项",
    )


def test_mixed_existing_numbers_are_rewritten_under_none_number() -> None:
    source = """USE SYS.LINT AS NONE_NUMBER
100 PRINT "a"
200 PRINT "b"
SUB main AS PUBLIC AS VOID
RETURN
.ENDSUB
CALL main
END
"""
    lines = read_numbered_lines(source)
    assert [line.no for line in lines] == [10, 20, 30, 40, 50, 60, 70, 80]
    assert lines[1].text == 'PRINT "a"'
    assert lines[2].text == 'PRINT "b"'


@requires_c_compiler
def test_c_backend_runs_none_number_program() -> None:
    source = """USE SYS.LINT AS NONE_NUMBER
SUB main AS PUBLIC AS VOID
PRINT "lint ok"
.ENDSUB
CALL main
END
"""
    with TemporaryDirectory(dir=Path("build"), prefix="sonalgebraic-lint-") as temp:
        root = Path(temp)
        path = root / "lint.sa"
        path.write_text(source, encoding="utf-8")
        exe = root / "lint.exe"
        build_exe(path, exe, keep_c=False, backend="c")
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == ["lint ok"]
