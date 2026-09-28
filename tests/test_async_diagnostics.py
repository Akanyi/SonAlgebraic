"""异步结果限制必须在检查阶段生效，不能等到生成 C 才发现。"""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.core import ast
from sonalgebraic.core.errors import SonCompileError
from sonalgebraic.driver.compiler import check_source, check_source_diagnostics
from sonalgebraic.frontend.parser import parse_program


def numbered(*lines: str) -> str:
    return "\n".join(f"{i * 10} {line}" for i, line in enumerate(lines, 1)) + "\n"


@pytest.fixture
def source_dir():
    # 不使用会保留最近几轮产物的 tmp_path，检查结束即删除模块和主文件。
    with TemporaryDirectory(prefix="sonalgebraic-async-diagnostics-") as temp:
        yield Path(temp)


@pytest.mark.parametrize("result", [
    "SYMBOL", "ERROR", "ENTITY AS Box", "CPTR", "PTR TO NUM AS LONG",
    "SUB() AS VOID", "PTR TO SUB() AS VOID", "PROMISE OF STRING",
])
def test_check_rejects_async_result_at_header(source_dir: Path, result: str) -> None:
    source = numbered(f"ASYNC SUB work() AS {result}", ".ENDSUB",
                      "SUB main AS PUBLIC AS VOID", ".ENDSUB", "CALL main", "END")
    path = source_dir / "main.sa"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(SonCompileError, match="ASYNC SUB 返回类型不支持") as error:
        check_source(path)
    assert error.value.line_no == 10
    with pytest.raises(SonCompileError, match="ASYNC SUB 返回类型不支持"):
        check_program(parse_program(source))


@pytest.mark.parametrize("result", ["SYMBOL", "ERROR", "CPTR", "PTR TO NUM AS LONG", "PROMISE OF BOOL", "ENTITY AS Box"])
@pytest.mark.parametrize("position", ["local", "global", "parameter", "return", "field"])
def test_check_rejects_unsupported_promise_results(source_dir: Path, result: str, position: str) -> None:
    declaration = f"DIM pending AS PROMISE OF {result} AS VAR"
    parts = {
        "local": ["SUB main AS PUBLIC AS VOID", declaration, ".ENDSUB"],
        "global": [declaration, "SUB main AS PUBLIC AS VOID", ".ENDSUB"],
        "parameter": [f"SUB consume(pending AS PROMISE OF {result}) AS VOID", ".ENDSUB", "SUB main AS PUBLIC AS VOID", ".ENDSUB"],
        "return": [f"SUB forward() AS PROMISE OF {result}", ".ENDSUB", "SUB main AS PUBLIC AS VOID", ".ENDSUB"],
        "field": ["FOR ENTITY AS Box", declaration, ".ENDENTITY", "SUB main AS PUBLIC AS VOID", ".ENDSUB"],
    }
    path = source_dir / "main.sa"
    path.write_text(numbered(*parts[position], "CALL main", "END"), encoding="utf-8")
    with pytest.raises(SonCompileError, match="PROMISE 结果类型不支持") as error:
        check_source(path)
    assert error.value.line_no == (20 if position in {"local", "field"} else 10)


@pytest.mark.parametrize(("result", "value"), [
    ("NUM AS LONG", "42"), ("NUM AS DOUBLE", "1.5"), ("NUM AS FLOAT", "1.5"),
    ("BOOL", "TRUE"), ("STRING", '"完成"'), ("HANDLE AS NET_STREAM", "NULL"), ("VOID", ""),
])
def test_check_accepts_supported_results(source_dir: Path, result: str, value: str) -> None:
    path = source_dir / "main.sa"
    path.write_text(numbered(
        f"ASYNC SUB work() AS {result}", f"RETURN {value}", ".ENDSUB",
        "SUB main AS PUBLIC AS VOID", f"DIM pending AS PROMISE OF {result} AS VAR",
        "pending = CALL work()", "SYNC pending", ".ENDSUB", "CALL main", "END",
    ), encoding="utf-8")
    check_source(path)


def test_check_keeps_parameters_and_sync_promise_forwarding_legal(source_dir: Path) -> None:
    path = source_dir / "main.sa"
    path.write_text(numbered(
        "ASYNC SUB work(tree AS SYMBOL, error AS ERROR, pending AS PROMISE OF NUM AS LONG) AS NUM AS LONG",
        "DIM result AS NUM AS LONG AS VAR", "result = AWAIT pending", "RETURN result", ".ENDSUB",
        "SUB forward(pending AS PROMISE OF NUM AS LONG) AS PROMISE OF NUM AS LONG",
        "RETURN pending", ".ENDSUB", "SUB main AS PUBLIC AS VOID", ".ENDSUB", "CALL main", "END",
    ), encoding="utf-8")
    check_source(path)


def test_module_diagnostic_preserves_origin(source_dir: Path) -> None:
    module = source_dir / "worker.sa"
    module.write_text(numbered("ASYNC SUB work() AS PUBLIC AS SYMBOL", ".ENDSUB"), encoding="utf-8")
    main = source_dir / "main.sa"
    main.write_text(numbered("USE worker AS W", "SUB main AS PUBLIC AS VOID", ".ENDSUB", "CALL main", "END"), encoding="utf-8")
    with pytest.raises(SonCompileError, match="ASYNC SUB 返回类型不支持") as error:
        check_source(main)
    assert Path(error.value.origin_path) == module
    assert error.value.line_no == 10
    diagnostics = check_source_diagnostics(main)
    assert len(diagnostics) == 1
    assert "ASYNC SUB 返回类型不支持" in diagnostics[0].message


def test_check_accepts_imported_async_and_net_promises(source_dir: Path) -> None:
    (source_dir / "worker.sa").write_text(numbered(
        "ASYNC SUB work() AS PUBLIC AS STRING", 'RETURN "完成"', ".ENDSUB",
    ), encoding="utf-8")
    main = source_dir / "main.sa"
    main.write_text(numbered(
        "USE worker AS W", "USE SYS.NET AS N", "SUB main AS PUBLIC AS VOID",
        "DIM text AS STRING AS VAR", "DIM pending AS PROMISE OF HANDLE AS NET_STREAM AS VAR",
        "text = SYNC W.work()", 'pending = N.CONNECT_ASYNC("127.0.0.1", 9000)',
        ".ENDSUB", "CALL main", "END",
    ), encoding="utf-8")
    check_source(main)


@pytest.mark.parametrize("promise", [False, True])
def test_array_result_is_not_mistaken_for_supported_scalar(promise: bool) -> None:
    # 返回类型的数组形状由 AST 表达；不能只看 name == NUM 就放行。
    program = parse_program(numbered("ASYNC SUB work() AS NUM AS LONG", "RETURN 1", ".ENDSUB",
                                     "SUB main AS PUBLIC AS VOID", ".ENDSUB"))
    array = ast.TypeSpec("NUM", "LONG", array_size=3)
    if promise:
        program.subs[0] = replace(program.subs[0], is_async=False, return_type=ast.TypeSpec("PROMISE", inner=array))
    else:
        program.subs[0] = replace(program.subs[0], return_type=array)
    with pytest.raises(SonCompileError, match="当前支持非数组"):
        check_program(program)


def test_local_promise_errors_are_collected_together(source_dir: Path) -> None:
    path = source_dir / "main.sa"
    path.write_text(numbered("SUB main AS PUBLIC AS VOID",
                            "DIM first AS PROMISE OF SYMBOL AS VAR",
                            "DIM second AS PROMISE OF ERROR AS VAR", ".ENDSUB"), encoding="utf-8")
    diagnostics = check_source_diagnostics(path)
    assert len(diagnostics) == 2
    assert all("PROMISE 结果类型不支持" in diagnostic.message for diagnostic in diagnostics)
