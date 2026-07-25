"""SYS.LIST 动态列表测试：codegen 映射、语义约束、端到端运行和泄漏验证。"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import shutil
import subprocess
import sys

import pytest

from conftest import compile_c, expect_error, requires_c_compiler


def _wrap(decls: str, body: str, uses: str = "5 USE SYS.LIST AS L") -> str:
    return f"{uses}\n{decls}\n100 SUB main AS PUBLIC AS VOID\n{body}\n800 .ENDSUB\n810 CALL main\n820 END\n"


# --- codegen 映射 ---

def test_list_new_push_get_map_to_runtime() -> None:
    c = compile_c(_wrap(
        "10 DIM xs AS HANDLE AS LIST AS VAR\n20 DIM ok AS BOOL AS VAR\n30 DIM v AS NUM AS DOUBLE AS VAR",
        "110 xs = L.NEW()\n120 ok = L.PUSH(xs, 1.5)\n130 v = L.GET(xs, 0)\n140 ok = L.CLOSE(xs)",
    ))
    assert "sa_list_new()" in c
    assert "sa_list_push(sa_xs, 1.5)" in c
    assert "sa_list_get(sa_xs, 0)" in c
    assert "sa_list_close(sa_xs)" in c


def test_list_feature_define_emitted() -> None:
    c = compile_c(_wrap(
        "10 DIM xs AS HANDLE AS LIST AS VAR\n20 DIM ok AS BOOL AS VAR",
        "110 xs = L.NEW()\n120 ok = L.CLOSE(xs)",
    ))
    assert "#define SA_ENABLE_LIST" in c


def test_strlist_heap_returns_are_freed() -> None:
    # GET_STR/JOIN_STR 返回 malloc 字符串，必须挂进语句级 cleanup
    c = compile_c(_wrap(
        "10 DIM names AS HANDLE AS STR_LIST AS VAR\n20 DIM ok AS BOOL AS VAR\n30 DIM s AS STRING AS VAR",
        "110 names = L.NEW_STR()\n120 ok = L.PUSH_STR(names, \"a\")\n130 s = L.GET_STR(names, 0)\n140 s = L.JOIN_STR(names, \",\")\n150 ok = L.CLOSE_STR(names)",
    ))
    assert "sa_strlist_get(sa_names, 0)" in c
    assert "sa_strlist_join(sa_names," in c
    assert "free(" in c


# --- 语义约束 ---

def test_list_functions_require_use_module() -> None:
    expect_error(
        "10 DIM xs AS HANDLE AS LIST AS VAR\n100 SUB main AS PUBLIC AS VOID\n110 xs = L.NEW()\n800 .ENDSUB\n810 CALL main\n820 END\n",
        "未知内置函数或 SUB",
    )


def test_list_push_arity_checked() -> None:
    expect_error(
        _wrap(
            "10 DIM xs AS HANDLE AS LIST AS VAR\n20 DIM ok AS BOOL AS VAR",
            "110 xs = L.NEW()\n120 ok = L.PUSH(xs)",
        ),
        "需要 2 个参数",
    )


def test_list_kind_mismatch_rejected() -> None:
    # 字符串列表句柄传给数值 GET：HANDLE kind 不同，编译期报类型不兼容
    expect_error(
        _wrap(
            "10 DIM names AS HANDLE AS STR_LIST AS VAR\n20 DIM v AS NUM AS DOUBLE AS VAR",
            "110 names = L.NEW_STR()\n120 v = L.GET(names, 0)",
        ),
        "类型不兼容",
    )


def test_unowned_list_handle_rejected() -> None:
    # NEW() 返回值不落到 HANDLE 变量就没人能 CLOSE，编译期直接拒绝
    expect_error(
        _wrap(
            "10 DIM n AS NUM AS LONG AS VAR",
            "110 n = L.LENGTH(L.NEW())",
        ),
        "必须先赋给",
    )


def test_list_handle_assign_to_wrong_kind_rejected() -> None:
    expect_error(
        _wrap(
            "10 DIM f AS HANDLE AS FILE AS VAR",
            "110 f = L.NEW()",
        ),
        "必须先赋给",
    )


# --- 端到端 ---

_E2E_SOURCE = """10 USE SYS.LIST AS L
20 DIM xs AS HANDLE AS LIST AS VAR
30 DIM names AS HANDLE AS STR_LIST AS VAR
40 DIM ok AS BOOL AS VAR
50 DIM i AS NUM AS LONG AS VAR
60 SUB main AS PUBLIC AS VOID
70 xs = L.NEW()
80 FOR i = 1 TO 5
90 ok = L.PUSH(xs, i * 10)
100 .ENDFOR
110 PRINT L.LENGTH(xs)
120 ok = L.SET(xs, 0, 7)
130 ok = L.INSERT(xs, 1, 8)
140 ok = L.REMOVE(xs, 2)
150 PRINT L.GET(xs, 0)
160 PRINT L.GET(xs, 1)
170 PRINT L.POP(xs)
180 PRINT L.LENGTH(xs)
190 ok = L.CLOSE(xs)
200 names = L.NEW_STR()
210 ok = L.PUSH_STR(names, "alpha")
220 ok = L.PUSH_STR(names, "beta")
230 ok = L.INSERT_STR(names, 1, "mid")
240 ok = L.SET_STR(names, 2, "gamma")
250 PRINT L.JOIN_STR(names, "|")
260 PRINT L.POP_STR(names)
270 ok = L.REMOVE_STR(names, 0)
280 PRINT L.LENGTH_STR(names)
290 ok = L.CLOSE_STR(names)
300 PRINT L.LENGTH(xs)
310 PRINT L.LAST_ERROR()
320 .ENDSUB
330 CALL main
340 END
"""

_E2E_EXPECTED = [
    "5",
    "7",
    "8",
    "50",
    "4",
    "alpha|mid|gamma",
    "gamma",
    "1",
    "-1",
    "invalid or closed LIST handle",
]


@pytest.mark.e2e
@requires_c_compiler
def test_e2e_list_operations_run() -> None:
    from sonalgebraic.driver.compiler import build_exe

    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "lists.sa"
        src.write_text(_E2E_SOURCE, encoding="utf-8")
        exe = Path(temp) / ("lists.exe" if sys.platform == "win32" else "lists")
        build_exe(src, exe, keep_c=False)
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == _E2E_EXPECTED


@pytest.mark.e2e
@requires_c_compiler
def test_e2e_list_is_leak_free() -> None:
    # 显式 CLOSE 后净分配必须为 0；REMOVE/SET_STR/POP_STR 的元素释放路径都在此覆盖
    gcc = shutil.which("gcc")
    if gcc is None:
        pytest.skip("需要 gcc 做 malloc 计数插桩")
    from sonalgebraic.driver.compiler import compile_to_c

    shim = (
        "#include <stdio.h>\n#include <stdlib.h>\n"
        "static long sa__live=0;\n"
        "static void* sa__m(size_t n){void* p=malloc(n); if(p)sa__live++; return p;}\n"
        "static void* sa__c(size_t n,size_t s){void* p=calloc(n,s); if(p)sa__live++; return p;}\n"
        "static void* sa__r(void* q,size_t n){ if(!q) return sa__m(n); return realloc(q,n);}\n"
        "static void sa__f(void* p){ if(p){sa__live--; free(p);}}\n"
        "static void sa__rep(void){ fprintf(stderr,\"SA_LIVE=%ld\\n\", sa__live);}\n"
        "#define malloc sa__m\n#define calloc sa__c\n#define realloc sa__r\n#define free sa__f\n"
    )
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "lists.sa"
        src.write_text(_E2E_SOURCE, encoding="utf-8")
        c_path = Path(temp) / "lists.c"
        compile_to_c(src, c_path)
        patched = shim + "\n" + c_path.read_text(encoding="utf-8")
        patched = patched.replace("sa_program_end:", "sa_program_end: atexit(sa__rep);", 1)
        c_path.write_text(patched, encoding="utf-8")
        exe = Path(temp) / ("lists_counted.exe" if sys.platform == "win32" else "lists_counted")
        compile_proc = subprocess.run([gcc, str(c_path), "-O2", "-std=c11", "-o", str(exe), "-lm"], text=True, capture_output=True)
        assert compile_proc.returncode == 0, compile_proc.stderr
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0
        assert "SA_LIVE=0" in proc.stderr, f"检测到内存泄漏: {proc.stderr}"
