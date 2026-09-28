"""native 手写 IR 的函数引用、callable 与终结调用回归。"""
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from conftest import build_temp, requires_native_compiler, requires_windows
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.backend.native import generate_native_llvm_ir
from sonalgebraic.driver.compiler import build_exe
from sonalgebraic.frontend.parser import parse_program
from test_native_backend import _native_live_allocs, requires_clang_lld
from test_function_model import _E2E, _ENTITY_AND_NULL, _FN, _GUI_MAIN, _main


SOURCE = (
    "10 SUB plus(n AS NUM AS LONG) AS NUM AS LONG\n"
    "20 RETURN n + 1\n30 .ENDSUB\n"
    "40 SUB apply(f AS SUB(n AS NUM AS LONG) AS NUM AS LONG) AS NUM AS LONG\n"
    "50 CALLRET f(41)\n60 .ENDSUB\n"
    "70 SUB main AS PUBLIC AS VOID\n"
    "80 DIM fn AS PTR TO SUB(n AS NUM AS LONG) AS NUM AS LONG AS VAR\n"
    "90 fn = @plus()\n"
    "100 NEW SUB f FROM fn\n"
    "110 PRINT apply(f)\n"
    "120 PRINT fn(3)\n"
    "130 .ENDSUB\n140 CALL main\n150 END\n"
)


CALLBACK = (
    "10 USE SYS.GUI AS G\n"
    "20 SUB clicked(id AS NUM AS LONG) AS VOID\n"
    "30 PRINT id\n40 .ENDSUB\n"
    "50 SUB main AS PUBLIC AS VOID\n"
    "60 DIM widget AS HANDLE AS WIDGET AS VAR\n"
    "70 DIM ok AS BOOL AS VAR\n"
    "80 NEW SUB handler FROM @clicked()\n"
    "90 ok = G.ON_CLICK(widget, handler)\n"
    "100 ok = G.RUN()\n"
    "110 .ENDSUB\n120 CALL main\n130 END\n"
)


NULL_CALLS = (
    "10 SUB failPtr AS VOID\n"
    "20 DIM fp AS PTR TO SUB AS VAR\n"
    "30 DIM text AS STRING AS VAR\n"
    "40 text = \"owned\"\n"
    "50 CALL fp()\n"
    "60 .ENDSUB\n"
    "70 SUB failCallable AS VOID\n"
    "80 DIM cb AS SUB AS VAR\n"
    "90 DIM text AS STRING AS VAR\n"
    "100 text = \"owned\"\n"
    "110 CALL cb()\n"
    "120 .ENDSUB\n"
    "130 SUB main AS PUBLIC AS VOID\n"
    "140 DIM err AS ERROR AS VAR\n"
    "150 TRY CALL failPtr TRACEBACK ERROR AS err\n"
    "160 CATCH ERR_NULL_CALL AS e\n"
    "170 PRINT e\n"
    "180 .ENDTRY\n"
    "190 TRY CALL failCallable TRACEBACK ERROR AS err\n"
    "200 CATCH ERR_NULL_CALL AS e\n"
    "210 PRINT e\n"
    "220 .ENDTRY\n"
    "230 .ENDSUB\n"
    "240 CALL main\n"
    "250 END\n"
)


def test_native_function_model_emits_independent_ir() -> None:
    ir = generate_native_llvm_ir(check_program(parse_program(SOURCE)))
    assert "define void @sa_main()" in ir
    assert "call ptr @sa_callable_new" in ir
    assert "call ptr @sa_callable_fn" in ir
    assert "call ptr @sa_sub_check" in ir
    assert "call void @sa_callable_release" in ir


def test_native_gui_dispatch_uses_shared_runtime() -> None:
    ir = generate_native_llvm_ir(check_program(parse_program(CALLBACK)))
    assert "call i32 @sa_gui_on_click" in ir
    assert "call i32 @sa_gui_run()" in ir


@requires_native_compiler
def test_native_callable_and_callret_run() -> None:
    with build_temp("native-callable-", "native-tests") as temp:
        root = Path(temp)
        source = root / "app.sa"
        source.write_text(SOURCE, encoding="utf-8")
        exe = root / "app.exe"
        build_exe(source, exe, keep_c=False, backend="native")
        result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["42", "4"]


@requires_native_compiler
def test_native_null_indirect_calls_preserve_error_and_cleanup() -> None:
    with build_temp("native-null-call-", "native-tests") as temp:
        root = Path(temp)
        source = root / "app.sa"
        source.write_text(NULL_CALLS, encoding="utf-8")
        exe = root / "app.exe"
        build_exe(source, exe, keep_c=False, backend="native")
        result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [
            "call through a NULL function reference",
            "call through a NULL callable",
        ]


@requires_clang_lld
def test_native_null_indirect_calls_release_owned_locals() -> None:
    with build_temp("native-null-leak-", "native-tests") as temp:
        assert _native_live_allocs(Path(temp), NULL_CALLS) == 0


@requires_native_compiler
@requires_windows
def test_native_gui_register_and_run_after_window_close() -> None:
    with build_temp("native-gui-", "native-tests") as temp:
        root = Path(temp)
        source = root / "app.sa"
        source.write_text(_GUI_MAIN, encoding="utf-8")
        exe = root / "app.exe"
        build_exe(source, exe, keep_c=False, backend="native")
        result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["1", "0", "ON_CLICK only applies to BUTTON widgets", "1"]


@requires_native_compiler
def test_native_entity_callable_fields_and_globals() -> None:
    source_text = _main(
        "510 DIM b AS ENTITY AS Button AS VAR\n"
        "520 DIM c AS ENTITY AS Button AS VAR\n"
        "530 NEW SUB h FROM @hello()\n"
        "540 b.label = \"ok\"\n"
        "550 b.onPress = h\n"
        "560 c = b\n"
        "570 CALL press(c)\n"
        "580 CALL c.onPress(8)\n"
        "590 gh = h\n"
        "600 CALL gh(9)",
        _ENTITY_AND_NULL,
    )
    with build_temp("native-entity-cb-", "native-tests") as temp:
        root = Path(temp)
        source = root / "app.sa"
        source.write_text(source_text, encoding="utf-8")
        exe = root / "app.exe"
        build_exe(source, exe, keep_c=False, backend="native")
        result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["hello 7", "hello 8", "hello 9"]


FULL_FUNCTION_MODEL = _main(
    f"510 DIM fp AS PTR TO {_FN} AS VAR\n"
    "520 DIM r AS NUM AS LONG AS VAR\n"
    "530 fp = @add1()\n"
    "540 r = fp(41)\n"
    "550 PRINT r\n"
    "560 fp f= @dbl()\n"
    "570 PRINT fp(21)\n"
    "580 NEW SUB h FROM @add1()\n"
    "590 r = CALL apply(h, 9)\n"
    "600 PRINT r\n"
    f"610 DIM g AS {_FN} AS VAR\n"
    "620 g = CALL make(@dbl())\n"
    "630 PRINT g(5)\n"
    f"640 DIM k AS {_FN} AS VAR\n"
    "650 k m= g\n"
    "660 PRINT k(6)\n"
    "670 DIM gs AS PTR TO SUB(name AS STRING) AS STRING AS VAR\n"
    "680 gs = @greet()\n"
    "690 PRINT gs(\"bob\")\n"
    "700 NEW SUB gg FROM gs\n"
    "710 PRINT gg(\"amy\")\n"
    "720 PRINT shout(\"yo\")\n"
    "730 PRINT fp = @dbl()",
    _E2E,
)


@requires_native_compiler
def test_native_function_model_full_ownership_run() -> None:
    with build_temp("native-full-cb-", "native-tests") as temp:
        root = Path(temp)
        source = root / "app.sa"
        source.write_text(FULL_FUNCTION_MODEL, encoding="utf-8")
        exe = root / "app.exe"
        build_exe(source, exe, keep_c=False, backend="native")
        result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["42", "42", "10", "10", "12", "hi bob", "hi amy", "hi yo!", "1"]


@requires_clang_lld
def test_native_function_model_full_ownership_leaks() -> None:
    with build_temp("native-full-leak-", "native-tests") as temp:
        assert _native_live_allocs(Path(temp), FULL_FUNCTION_MODEL) == 0


ARGUMENT_THROW = (
    "10 USE SYS.STRING AS S\n"
    "20 SUB receiver(text AS STRING) AS VOID\n"
    "30 PRINT text\n"
    "40 .ENDSUB\n"
    "50 SUB fail AS VOID\n"
    "60 DIM fp AS PTR TO SUB(text AS STRING) AS VOID AS VAR\n"
    "70 CALL fp(S.CONCAT(\"temporary\", \" value\"))\n"
    "80 .ENDSUB\n"
    "90 SUB main AS PUBLIC AS VOID\n"
    "100 DIM err AS ERROR AS VAR\n"
    "110 TRY CALL fail TRACEBACK ERROR AS err\n"
    "120 CATCH ERR_NULL_CALL AS e\n"
    "130 PRINT e\n"
    "140 .ENDTRY\n"
    "150 .ENDSUB\n"
    "160 CALL main\n"
    "170 END\n"
)


@requires_clang_lld
def test_native_indirect_throw_releases_argument_temporaries() -> None:
    with build_temp("native-arg-throw-", "native-tests") as temp:
        assert _native_live_allocs(Path(temp), ARGUMENT_THROW) == 0


DIRECT_ARGUMENT_THROW = (
    "10 USE SYS.STRING AS S\n"
    "20 SUB raiseError(text AS STRING) AS VOID\n"
    "30 THROW NEW ERR_SAMPLE, text\n"
    "40 .ENDSUB\n"
    "50 SUB fail AS VOID\n"
    "60 CALL raiseError(S.CONCAT(\"temporary\", \" value\"))\n"
    "70 .ENDSUB\n"
    "80 SUB main AS PUBLIC AS VOID\n"
    "90 DIM err AS ERROR AS VAR\n"
    "100 TRY CALL fail TRACEBACK ERROR AS err\n"
    "110 CATCH ERR_SAMPLE AS e\n"
    "120 PRINT e\n"
    "130 .ENDTRY\n"
    "140 .ENDSUB\n"
    "150 CALL main\n"
    "160 END\n"
)


@requires_clang_lld
def test_native_direct_throw_releases_argument_temporaries() -> None:
    with build_temp("native-direct-arg-", "native-tests") as temp:
        assert _native_live_allocs(Path(temp), DIRECT_ARGUMENT_THROW) == 0
