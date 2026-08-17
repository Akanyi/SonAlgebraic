from __future__ import annotations

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from conftest import compile_c, expect_error, requires_c_compiler, requires_native_compiler
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.backend.native import generate_native_llvm_ir
from sonalgebraic.driver.compiler import build_exe
from sonalgebraic.frontend.parser import parse_program


_BINARY_SOURCE = '''10 USE SYS.BINARY AS B
20 DIM packet AS HANDLE AS BUFFER AS VAR
30 DIM copy AS HANDLE AS BUFFER AS VAR
40 DIM ok AS BOOL AS VAR
50 DIM checksum AS NUM AS LONG AS VAR
60 DIM value16 AS NUM AS LONG AS VAR
70 DIM value32 AS NUM AS LONG AS VAR
80 SUB main AS PUBLIC AS VOID
90 packet = B.NEW(8)
100 ok = B.PACK_U16_BE(packet, 0, 4660)
110 ok = B.PACK_U32_LE(packet, 2, 2309737967)
120 checksum = B.CHECKSUM8(packet, 0, 6)
130 value16 = B.UNPACK_U16_BE(packet, 0)
140 value32 = B.UNPACK_U32_LE(packet, 2)
150 copy = B.SLICE(packet, 1, 4)
160 PRINT F"hex={B.HEX_ENCODE(packet)}"
170 PRINT F"values={value16},{value32} checksum={checksum}"
180 PRINT F"slice={B.HEX_ENCODE(copy)} length={B.LENGTH(copy)}"
190 ok = B.CLOSE(copy)
200 PRINT F"closed={B.LENGTH(copy)} error={B.LAST_ERROR()}"
210 ok = B.CLOSE(packet)
220 .ENDSUB
230 CALL main
240 END
'''


_EXPECTED = [
    "hex=1234EFCDAB890000",
    "values=4660,2309737967 checksum=54",
    "slice=34EFCDAB length=4",
    "closed=-1 error=invalid or closed BUFFER handle",
]


def test_binary_module_generates_c_and_native_ir() -> None:
    checked = check_program(parse_program(_BINARY_SOURCE))
    c = compile_c(_BINARY_SOURCE)
    ir = generate_native_llvm_ir(checked)
    assert "#define SA_ENABLE_BINARY" in c
    assert "sa_binary_pack_u32_le" in c
    assert "declare ptr @sa_binary_hex_encode(i64)" in ir
    assert "declare i64 @sa_binary_checksum8(i64, i64, i64)" in ir


def test_binary_handle_kind_is_nominal() -> None:
    expect_error(
        '''10 DIM buffer AS HANDLE AS BUFFER AS VAR
20 DIM file AS HANDLE AS FILE AS VAR
30 SUB main AS PUBLIC AS VOID
40 buffer = file
50 .ENDSUB
60 CALL main
70 END
''',
        "类型不兼容",
    )


def test_buffer_handle_cannot_be_anonymous_nested_resource() -> None:
    expect_error(
        '''10 USE SYS.BINARY AS B
20 DIM packet AS HANDLE AS BUFFER AS VAR
30 DIM length AS NUM AS LONG AS VAR
40 SUB main AS PUBLIC AS VOID
50 packet = B.NEW(4)
60 length = B.LENGTH(B.SLICE(packet, 0, 1))
70 .ENDSUB
80 CALL main
90 END
''',
        "HANDLE AS BUFFER",
    )


def test_binary_null_handle_lowers_to_integer_zero() -> None:
    source = '''10 USE SYS.BINARY AS B
20 DIM ok AS BOOL AS VAR
30 SUB main AS PUBLIC AS VOID
40 ok = B.CLOSE(NULL)
50 .ENDSUB
60 CALL main
70 END
'''
    checked = check_program(parse_program(source))
    c = compile_c(source)
    ir = generate_native_llvm_ir(checked)
    assert "sa_binary_close(0)" in c
    assert "call i32 @sa_binary_close(i64 0)" in ir


def _run_binary_program(temp: str, backend: str) -> list[str]:
    root = Path(temp)
    source = root / "binary.sa"
    source.write_text(_BINARY_SOURCE, encoding="utf-8")
    exe = root / f"binary_{backend}.exe"
    build_exe(source, exe, keep_c=False, backend=backend)
    proc = subprocess.run([str(exe)], text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


@requires_c_compiler
def test_c_backend_binary_packet_roundtrip() -> None:
    with TemporaryDirectory(dir=Path("build"), prefix="sonalgebraic-binary-c-") as temp:
        assert _run_binary_program(temp, "c") == _EXPECTED


@requires_native_compiler
def test_native_backend_binary_packet_roundtrip() -> None:
    with TemporaryDirectory(dir=Path("build"), prefix="sonalgebraic-binary-native-") as temp:
        assert _run_binary_program(temp, "native") == _EXPECTED
