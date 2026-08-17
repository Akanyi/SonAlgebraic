"""CLI 与诊断链路的缺陷回归。

这一批全部对应「命令跑完了、输出却在骗人」的路径：打印不存在的路径、旗标静默失效、
报错把用户指向已经装好的东西、行号指到一行完全正确的代码。所以断言尽量落在
真实命令的 stdout/stderr 和磁盘产物上，而不是内部函数的返回值。
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from conftest import REPO_ROOT, requires_c_compiler
from sonalgebraic.__main__ import exe_suffix, main as cli_main
from sonalgebraic.analysis.diagnostics import Diagnostic, diagnostics_to_json, render_diagnostics
from sonalgebraic.core.errors import SonCompileError
from sonalgebraic.driver.compiler import check_source_diagnostics, missing_compiler_error


_MATHLIB = "10 SUB add_two(a AS NUM AS LONG, b AS NUM AS LONG) AS PUBLIC AS NUM AS LONG\n20 RETURN a + b\n30 .ENDSUB\n"
_APP = (
    "10 USE mathlib AS M\n20 DIM n AS NUM AS LONG AS VAR\n30 SUB main AS PUBLIC AS VOID\n"
    "40 n = M.add_two(2, 3)\n50 PRINT F\"n={n}\"\n60 .ENDSUB\n70 CALL main\n80 END\n"
)


def _module_project(tmp_path: Path) -> Path:
    (tmp_path / "mathlib.sa").write_text(_MATHLIB, encoding="utf-8")
    app = tmp_path / "app.sa"
    app.write_text(_APP, encoding="utf-8")
    return app


def _run_cli(*args: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli_main(list(args))
    return code, stdout.getvalue(), stderr.getvalue()


# --------------------------------------------------------------------------
# 行号语义：header 用物理行号，SA 行号保留在消息里
# --------------------------------------------------------------------------


def test_header_line_is_physical_not_sa(tmp_path: Path) -> None:
    """带头部注释的文件里 SA 行号和物理行号从第一行就错开，编辑器只认物理行。"""
    source = tmp_path / "hdr.sa"
    source.write_text(
        "10 REM 头部注释\n20 REM 又一行注释\n30 SUB main AS PUBLIC AS VOID\n"
        "40 PRINT missing\n50 .ENDSUB\n60 CALL main\n70 END\n",
        encoding="utf-8",
    )

    code, _, err = _run_cli("check", str(source))
    assert code == 1
    # SA 40 位于物理第 4 行；两者恰好不同，能区分开
    assert "hdr.sa:4:" in err
    assert "[SA 40]" in err
    assert "hdr.sa:40" not in err
    assert "40 PRINT missing" in err


def test_missing_line_number_points_at_the_offending_physical_line(tmp_path: Path) -> None:
    """缺行号的错误带的是物理行号，以前渲染器却拿它去匹配 `nnn ...` 开头的行，
    在按 10 递增编号的文件里必然命中开头某一行完全正确的代码。"""
    lines = ["10 SUB main AS PUBLIC AS VOID"]
    lines += [f'{i * 10} PRINT "line {i}"' for i in range(2, 10)]
    lines.append('PRINT "forgot the number"')  # 第 10 个物理行，没写行号
    lines += ["110 .ENDSUB", "120 CALL main", "130 END"]
    source = tmp_path / "missnum.sa"
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")

    code, _, err = _run_cli("check", str(source))
    assert code == 1
    assert 'PRINT "forgot the number"' in err
    assert "10 SUB main AS PUBLIC AS VOID" not in err
    # 恢复时按物理行留白，否则同一个错会被重复报出来
    assert err.count("每一行都必须以递增的正整数行号开头") == 1


def test_none_number_diagnostic_shows_the_right_source_line(tmp_path: Path) -> None:
    """NONE_NUMBER 的 SA 行号是编译期补的，文件里不存在；以前直接当物理行号用，
    第 3 行的错误会去展示物理第 30 行。"""
    body = ["USE SYS.LINT AS NONE_NUMBER", "SUB main AS PUBLIC AS VOID", "PRINT missing", ".ENDSUB", "CALL main", "END"]
    body += [f"REM 占位物理行 {i}" for i in range(7, 35)]
    source = tmp_path / "nonum.sa"
    source.write_text("\n".join(body) + "\n", encoding="utf-8")

    code, _, err = _run_cli("check", str(source))
    assert code == 1
    assert "nonum.sa:3:" in err
    assert "[SA 30]" in err
    assert "PRINT missing" in err
    assert "占位物理行" not in err


def test_dependency_module_diagnostic_uses_module_physical_line(tmp_path: Path) -> None:
    (tmp_path / "dep.sa").write_text(
        "\n\n10 SUB helper AS PUBLIC AS VOID\n20 PRINT oops\n30 .ENDSUB\n",
        encoding="utf-8",
    )
    main_sa = tmp_path / "mainapp.sa"
    main_sa.write_text(
        "10 USE dep AS D\n20 SUB main AS PUBLIC AS VOID\n30 RETURN\n40 .ENDSUB\n50 CALL main\n60 END\n",
        encoding="utf-8",
    )

    code, _, err = _run_cli("check", str(main_sa))
    assert code == 1
    # 模块前两行是空行，SA 20 落在物理第 4 行
    assert "dep.sa:4:" in err
    assert "[SA 20]" in err
    assert "20 PRINT oops" in err
    assert "mainapp.sa:" not in err


def test_renderer_never_falls_back_to_sa_line_as_physical_index() -> None:
    """定位不到就只出 header，不能拿 SA 行号当物理下标去取一行不相干的代码。"""
    source = "\n".join(f"REM 第 {i} 行" for i in range(1, 40)) + "\n"
    diagnostic = Diagnostic(message="boom", line=30)
    rendered = render_diagnostics("x.sa", source, [diagnostic])

    assert rendered == "x.sa:30:1 error: boom"
    assert "第 30 行" not in rendered


# --------------------------------------------------------------------------
# --json 出口
# --------------------------------------------------------------------------


def test_check_json_carries_both_line_numbers(tmp_path: Path) -> None:
    source = tmp_path / "bad.sa"
    source.write_text(
        "10 REM 注释\n20 SUB main AS PUBLIC AS VOID\n30 PRINT missing\n40 .ENDSUB\n50 CALL main\n60 END\n",
        encoding="utf-8",
    )

    code, out, err = _run_cli("check", str(source), "--json")
    assert code == 1
    assert err == ""  # --json 独占输出，不能混进人类可读的那一份
    payload = json.loads(out)
    assert len(payload) == 1
    entry = payload[0]
    assert entry["line"] == 3  # 物理行，编辑器按这个跳
    assert entry["sa_line"] == 30  # SA 行号不丢
    assert entry["column"] == 10 and entry["length"] == 7
    assert entry["severity"] == "error"
    assert entry["message"] == "变量未声明: missing"
    assert Path(entry["file"]) == source


def test_check_json_is_empty_list_when_clean() -> None:
    code, out, err = _run_cli("check", str(REPO_ROOT / "examples" / "hello.sa"), "--json")
    assert code == 0
    assert err == ""
    assert json.loads(out) == []
    assert "OK" not in out


def test_json_points_at_the_dependency_file(tmp_path: Path) -> None:
    (tmp_path / "dep.sa").write_text(
        "10 SUB helper AS PUBLIC AS VOID\n20 PRINT oops\n30 .ENDSUB\n", encoding="utf-8"
    )
    main_sa = tmp_path / "mainapp.sa"
    main_sa.write_text(
        "10 USE dep AS D\n20 SUB main AS PUBLIC AS VOID\n30 RETURN\n40 .ENDSUB\n50 CALL main\n60 END\n",
        encoding="utf-8",
    )

    entry = json.loads(diagnostics_to_json(main_sa, check_source_diagnostics(main_sa)))[0]
    assert Path(entry["file"]).name == "dep.sa"
    assert entry["line"] == 2 and entry["sa_line"] == 20


# --------------------------------------------------------------------------
# sonc c / build 的产物路径与旗标
# --------------------------------------------------------------------------


def test_sonc_c_prints_the_path_that_actually_exists(tmp_path: Path) -> None:
    """含用户模块时产物是一个 C 项目目录，以前打印的仍是那个不存在的单文件路径。"""
    app = _module_project(tmp_path)
    out_c = tmp_path / "out" / "app.c"

    code, out, _ = _run_cli("c", str(app), "-o", str(out_c))
    assert code == 0
    printed = Path(out.strip())
    assert printed.is_file()
    assert printed != out_c


def test_sonc_c_single_file_still_prints_the_requested_path(tmp_path: Path) -> None:
    source = tmp_path / "hello.sa"
    source.write_text("10 SUB main AS PUBLIC AS VOID\n20 PRINT 1\n30 .ENDSUB\n40 CALL main\n50 END\n", encoding="utf-8")
    out_c = tmp_path / "hello.c"

    code, out, _ = _run_cli("c", str(source), "-o", str(out_c))
    assert code == 0
    assert Path(out.strip()) == out_c
    assert out_c.is_file()


@requires_c_compiler
def test_discard_c_removes_the_generated_project_directory(tmp_path: Path) -> None:
    """--discard-c 以前只对单文件生效，加了模块就静默失效，构建目录里留一坨中间产物。"""
    app = _module_project(tmp_path)
    exe = tmp_path / "out" / f"app{exe_suffix()}"

    assert cli_main(["build", str(app), "-o", str(exe), "--discard-c"]) == 0
    assert exe.is_file()
    assert not (exe.parent / "app").exists()
    assert list(exe.parent.rglob("*.c")) == []
    assert subprocess.run([str(exe)], capture_output=True, text=True).stdout.strip() == "n=5"


@requires_c_compiler
def test_build_keeps_the_project_directory_by_default(tmp_path: Path) -> None:
    app = _module_project(tmp_path)
    exe = tmp_path / "out" / f"app{exe_suffix()}"

    assert cli_main(["build", str(app), "-o", str(exe)]) == 0
    assert (exe.parent / "app" / "app.c").is_file()


# --------------------------------------------------------------------------
# 门面：后缀、--version、找不到编译器的报错
# --------------------------------------------------------------------------


def test_exe_suffix_follows_the_target_not_the_host() -> None:
    assert exe_suffix("x86_64-windows-gnu") == ".exe"
    assert exe_suffix("x86_64-linux-gnu") == ""
    assert exe_suffix("aarch64-macos-none") == ""
    # 不带 --target 时才看宿主
    assert exe_suffix() == (".exe" if sys.platform == "win32" else "")


def test_build_default_output_uses_the_target_suffix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """非 Windows 目标不该产出 app.exe；这里拦住链接步骤只看默认输出名。"""
    source = tmp_path / "app.sa"
    source.write_text("10 SUB main AS PUBLIC AS VOID\n20 PRINT 1\n30 .ENDSUB\n40 CALL main\n50 END\n", encoding="utf-8")
    seen: list[Path] = []

    def fake_build_exe(src: Path, output: Path, **kwargs):
        seen.append(output)
        raise SonCompileError("stop here")

    monkeypatch.setattr("sonalgebraic.__main__.build_exe", fake_build_exe)
    _run_cli("build", str(source), "--target", "x86_64-linux-gnu")
    assert seen == [tmp_path / "app"]

    seen.clear()
    _run_cli("build", str(source), "--target", "x86_64-windows-gnu")
    assert seen == [tmp_path / "app.exe"]


def test_version_flag_reports_the_package_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "sonalgebraic", "--version"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0
    assert result.stdout.strip().startswith("sonc ")


def test_cross_compile_error_names_zig_instead_of_already_installed_gcc() -> None:
    """find_c_compiler 在跨目标时只认 zig，本机 gcc/clang 被静默忽略；
    报「请安装 gcc、clang……」等于把用户指回他已经装好的东西。"""
    cross = missing_compiler_error("x86_64-linux-gnu" if sys.platform == "win32" else "x86_64-windows-gnu")
    assert "zig" in str(cross)
    assert "交叉编译" in str(cross)
    assert "请安装 gcc、clang、tcc" not in str(cross)

    native = missing_compiler_error(None)
    assert "请安装 gcc、clang、tcc" in str(native)


@pytest.mark.skipif(shutil.which("zig") is not None, reason="装了 zig 就不会走到这条报错")
def test_cross_compile_without_zig_reports_the_real_reason(tmp_path: Path) -> None:
    source = tmp_path / "hello.sa"
    source.write_text("10 SUB main AS PUBLIC AS VOID\n20 PRINT 1\n30 .ENDSUB\n40 CALL main\n50 END\n", encoding="utf-8")
    code, _, err = _run_cli("build", str(source), "-o", str(tmp_path / "hello"), "--target", "x86_64-linux-gnu")
    assert code == 1
    assert "zig" in err


def test_diagnostic_error_is_gone() -> None:
    """从未被抛出过的假通路，删掉以免后来者以为诊断体系走异常传播。"""
    import sonalgebraic.analysis.diagnostics as diagnostics

    assert not hasattr(diagnostics, "DiagnosticError")
