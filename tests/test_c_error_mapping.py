"""C 编译错误映射回 SA 源码行号的测试。"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from conftest import REPO_ROOT  # noqa: F401  确保 sys.path 注入
from sonalgebraic.driver.compiler import map_c_errors_to_sa_lines

_C_TEXT = """#include <stdio.h>
static long long sa_x = 0;
void sa_sub_main(void) {
    /* SA 40: x = 1 */
    sa_x = 1;
    /* SA 50: PRINT x */
    printf("%lld\\n", sa_x;
}
"""


def _write_c(temp: Path) -> Path:
    c_path = temp / "app.c"
    c_path.write_text(_C_TEXT, encoding="utf-8")
    return c_path


def test_gcc_style_error_maps_to_nearest_sa_comment() -> None:
    with TemporaryDirectory() as temp:
        c_path = _write_c(Path(temp))
        output = f"{c_path}:7:27: error: expected ')' before ';' token"
        hints = map_c_errors_to_sa_lines(output, [c_path])
        assert len(hints) == 1
        assert "SA 50: PRINT x" in hints[0]


def test_msvc_style_error_maps_to_nearest_sa_comment() -> None:
    with TemporaryDirectory() as temp:
        c_path = _write_c(Path(temp))
        output = f"{c_path.name}(5): error C2065: something"
        hints = map_c_errors_to_sa_lines(output, [c_path])
        assert len(hints) == 1
        assert "SA 40: x = 1" in hints[0]


def test_error_before_any_sa_comment_yields_no_hint() -> None:
    with TemporaryDirectory() as temp:
        c_path = _write_c(Path(temp))
        output = f"{c_path}:1:1: fatal error: stdio.h: No such file or directory"
        assert map_c_errors_to_sa_lines(output, [c_path]) == []


def test_duplicate_sa_lines_are_deduped() -> None:
    with TemporaryDirectory() as temp:
        c_path = _write_c(Path(temp))
        output = "\n".join([
            f"{c_path}:7:27: error: expected ')'",
            f"{c_path}:7:28: error: expected ';'",
        ])
        hints = map_c_errors_to_sa_lines(output, [c_path])
        assert len(hints) == 1


def test_unknown_file_is_ignored() -> None:
    with TemporaryDirectory() as temp:
        c_path = _write_c(Path(temp))
        output = "other.c:7:1: error: boom"
        assert map_c_errors_to_sa_lines(output, [c_path]) == []
