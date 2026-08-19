from __future__ import annotations

from pathlib import Path
import subprocess

from conftest import build_temp, compile_c, expect_error, requires_c_compiler, requires_native_compiler
from sonalgebraic.backend.native import generate_native_llvm_ir
from sonalgebraic.driver.compiler import build_exe
from sonalgebraic.frontend.parser import parse_program
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.packaging.module_compiler import compile_project
from sonalgebraic.packaging.slib import build_slib


_FILE_SOURCE = '''10 USE SYS.FILE AS F
20 DIM handle AS HANDLE AS FILE AS VAR
30 DIM text AS STRING AS VAR
40 DIM cwd AS STRING AS VAR
50 DIM absolute AS STRING AS VAR
60 DIM written AS NUM AS LONG AS VAR
70 DIM size AS NUM AS LONG AS VAR
80 DIM ok AS BOOL AS VAR
90 SUB main AS PUBLIC AS VOID
100 handle = F.OPEN("roundtrip.txt", "WRITE")
110 written = F.WRITE(handle, "hello file")
120 ok = F.CLOSE(handle)
130 handle = NULL
140 ok = F.APPEND_TEXT("roundtrip.txt", "!")
150 text = F.READ_TEXT("roundtrip.txt")
160 handle = F.OPEN("roundtrip.txt", "READ")
170 size = F.SIZE(handle)
180 ok = F.CLOSE(handle)
185 handle = NULL
190 cwd = F.CWD()
200 absolute = F.ABSOLUTE("roundtrip.txt")
210 PRINT F"text={text}"
220 PRINT F"written={written} size={size}"
230 PRINT F"exists={F.EXISTS(absolute)} file={F.IS_FILE(absolute)} dir={F.IS_DIR(cwd)}"
240 PRINT F"closed_null={handle = NULL}"
250 .ENDSUB
260 CALL main
270 END
'''


def test_handle_kind_is_nominal() -> None:
    expect_error(
        '''10 DIM file AS HANDLE AS FILE AS VAR
20 DIM socket AS HANDLE AS SOCKET AS VAR
30 SUB main AS PUBLIC AS VOID
40 file = socket
50 .ENDSUB
60 CALL main
70 END
''',
        "类型不兼容",
    )


def test_handle_parameter_accepts_null_without_pointer_c_literal() -> None:
    source = '''10 SUB isNull(file AS HANDLE AS FILE) AS BOOL
20 RETURN file = NULL
30 .ENDSUB
40 DIM result AS BOOL AS VAR
50 SUB main AS PUBLIC AS VOID
60 result = CALL isNull(NULL)
70 .ENDSUB
80 CALL main
90 END
'''
    c = compile_c(source)
    assert "sa_isnull(0)" in c


def test_handle_array_requires_indexed_assignment() -> None:
    expect_error(
        '''10 DIM files[2] AS HANDLE AS FILE AS VAR
20 DIM file AS HANDLE AS FILE AS VAR
30 SUB main AS PUBLIC AS VOID
40 file = files
50 .ENDSUB
60 CALL main
70 END
''',
        "数组不能整体赋值",
    )


def test_file_module_generates_c_and_native_ir() -> None:
    checked = check_program(parse_program(_FILE_SOURCE))
    c = compile_c(_FILE_SOURCE)
    ir = generate_native_llvm_ir(checked)
    assert "#define SA_ENABLE_FILE" in c
    assert "SaHandle sa_handle = 0;" in c
    assert "sa_file_open" in c
    assert "declare i64 @sa_file_open(ptr, ptr)" in ir
    assert "call i32 @sa_file_close" in ir


def _run_file_program(temp: str, backend: str) -> list[str]:
    root = Path(temp)
    source = root / "file.sa"
    source.write_text(_FILE_SOURCE, encoding="utf-8")
    exe = root / f"file_{backend}.exe"
    build_exe(source, exe, keep_c=False, backend=backend)
    proc = subprocess.run([str(exe)], cwd=root, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


_FILE_EXPECTED = [
    "text=hello file!",
    "written=10 size=11",
    "exists=1 file=1 dir=1",
    "closed_null=1",
]


@requires_c_compiler
def test_c_backend_file_module_roundtrip() -> None:
    with build_temp("sonalgebraic-file-c-") as temp:
        assert _run_file_program(temp, "c") == _FILE_EXPECTED


@requires_native_compiler
def test_native_backend_file_module_roundtrip() -> None:
    with build_temp("sonalgebraic-file-native-") as temp:
        assert _run_file_program(temp, "native") == _FILE_EXPECTED


def test_desktop_module_generates_native_calls() -> None:
    source = '''10 USE SYS.DESKTOP AS D
20 DIM ok AS BOOL AS VAR
30 DIM text AS STRING AS VAR
40 SUB main AS PUBLIC AS VOID
50 ok = D.CLIPBOARD_SET("hello")
60 text = D.CLIPBOARD_GET()
70 ok = D.OPEN("https://example.com")
80 ok = D.MESSAGE("SonAlgebraic", text)
90 PRINT D.LAST_ERROR()
100 .ENDSUB
110 CALL main
120 END
'''
    checked = check_program(parse_program(source))
    c = compile_c(source)
    ir = generate_native_llvm_ir(checked)
    assert "#define SA_ENABLE_DESKTOP" in c
    assert "sa_desktop_clipboard_set" in c
    assert "declare i32 @sa_desktop_open(ptr)" in ir
    assert "declare ptr @sa_desktop_clipboard_get()" in ir


@requires_c_compiler
def test_desktop_module_links_without_running() -> None:
    source = '''10 USE SYS.DESKTOP AS D
20 DIM ok AS BOOL AS VAR
30 SUB main AS PUBLIC AS VOID
40 ok = D.OPEN("https://example.com")
50 .ENDSUB
60 CALL main
70 END
'''
    with build_temp("sonalgebraic-desktop-link-") as temp:
        root = Path(temp)
        source_path = root / "desktop.sa"
        source_path.write_text(source, encoding="utf-8")
        build_exe(source_path, root / "desktop.exe", keep_c=False, backend="c")


@requires_native_compiler
def test_native_desktop_module_links_without_running() -> None:
    source = '''10 USE SYS.DESKTOP AS D
20 DIM ok AS BOOL AS VAR
30 SUB main AS PUBLIC AS VOID
40 ok = D.OPEN("https://example.com")
50 .ENDSUB
60 CALL main
70 END
'''
    with build_temp("sonalgebraic-desktop-native-link-") as temp:
        root = Path(temp)
        source_path = root / "desktop.sa"
        source_path.write_text(source, encoding="utf-8")
        build_exe(source_path, root / "desktop.exe", keep_c=False, backend="native")


def test_user_module_propagates_file_runtime_feature() -> None:
    module_source = '''10 USE SYS.FILE AS F
20 SUB exists(path AS STRING) AS PUBLIC AS BOOL
30 RETURN F.EXISTS(path)
40 .ENDSUB
'''
    main_source = '''10 USE FILELIB AS L
20 DIM result AS BOOL AS VAR
30 SUB main AS PUBLIC AS VOID
40 result = CALL L.exists("missing.txt")
50 .ENDSUB
60 CALL main
70 END
'''
    with build_temp("sonalgebraic-file-module-") as temp:
        root = Path(temp)
        (root / "filelib.sa").write_text(module_source, encoding="utf-8")
        main = root / "main.sa"
        main.write_text(main_source, encoding="utf-8")
        plan = compile_project(main, root / "out")
        assert "#define SA_ENABLE_FILE" in plan.runtime_c.read_text(encoding="utf-8")
        assert plan.modules["filelib"].runtime_features == ["file"]


def test_source_slib_preserves_file_runtime_feature() -> None:
    module_source = '''10 USE SYS.FILE AS F
20 SUB exists(path AS STRING) AS PUBLIC AS BOOL
30 RETURN F.EXISTS(path)
40 .ENDSUB
'''
    main_source = '''10 USE FILELIB AS L
20 DIM result AS BOOL AS VAR
30 SUB main AS PUBLIC AS VOID
40 result = CALL L.exists("missing.txt")
50 .ENDSUB
60 CALL main
70 END
'''
    with build_temp("sonalgebraic-file-slib-") as temp:
        root = Path(temp)
        module = root / "filelib.sa"
        module.write_text(module_source, encoding="utf-8")
        build_slib(module, root / "filelib.slib")
        module.unlink()
        main = root / "main.sa"
        main.write_text(main_source, encoding="utf-8")
        plan = compile_project(main, root / "out")
        assert "#define SA_ENABLE_FILE" in plan.runtime_c.read_text(encoding="utf-8")
        assert plan.modules["filelib"].runtime_features == ["file"]
