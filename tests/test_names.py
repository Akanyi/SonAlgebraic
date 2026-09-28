"""生成名不能被系统头宏重写，也不能因避让宏而合并不同的用户标识符。"""
from pathlib import Path
import subprocess

import pytest

from conftest import build_temp, requires_c_compiler
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.backend.codegen import CGen
from sonalgebraic.core.names import c_ident
from sonalgebraic.driver.compiler import find_c_compiler, run_c_compiler
from sonalgebraic.frontend.parser import parse_program


@pytest.mark.parametrize(("name", "expected"), [
    ("counter", "sa_counter"),
    ("handler", "sa_HANDLER"),
    ("HANDLER", "sa_HANDLER"),
    ("sigaction", "sa_SIGACTION"),
    ("SIGACTION", "sa_SIGACTION"),
    ("user_handler", "sa_user_handler"),
    ("handler_", "sa_handler_"),
])
def test_c_ident_avoids_signal_macros(name: str, expected: str) -> None:
    assert c_ident(name) == expected


@pytest.mark.e2e
@requires_c_compiler
def test_signal_macro_names_compile_and_run() -> None:
    source = """10 DIM user_handler AS NUM AS LONG AS VAR
20 SUB handler(sigaction AS NUM AS LONG) AS NUM AS LONG
30 RETURN sigaction + 1
40 .ENDSUB
50 SUB main AS PUBLIC AS VOID
60 NEW SUB sigaction FROM @handler()
70 user_handler = sigaction(41)
80 PRINT user_handler
90 .ENDSUB
100 CALL main
110 END
"""
    with build_temp("signal-macros-") as temp:
        root = Path(temp)
        # 在 Windows 上也模拟 POSIX 字段宏，避免此类回归只能等 Linux CI 才发现。
        (root / "signal_aliases.h").write_text(
            "#ifndef sa_handler\n#define sa_handler __sigaction_handler.sa_handler\n#endif\n"
            "#ifndef sa_sigaction\n#define sa_sigaction __sigaction_handler.sa_sigaction\n#endif\n",
            encoding="utf-8",
        )
        checked = check_program(parse_program(source))
        code = CGen(checked, include_headers=["signal_aliases.h"]).generate()
        c_file = root / "probe.c"
        c_file.write_text(code, encoding="utf-8")
        exe = root / "probe.exe"
        compiler = find_c_compiler()
        assert compiler is not None
        run_c_compiler(compiler, [c_file], exe)
        result = subprocess.run([str(exe)], cwd=root, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "42"
