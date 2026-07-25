"""SYS.MAP 关联容器测试：codegen 映射、语义约束、端到端运行和泄漏验证。"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import shutil
import subprocess
import sys

import pytest

from conftest import compile_c, expect_error, requires_c_compiler


def _wrap(decls: str, body: str, uses: str = "5 USE SYS.MAP AS M") -> str:
    return f"{uses}\n{decls}\n100 SUB main AS PUBLIC AS VOID\n{body}\n800 .ENDSUB\n810 CALL main\n820 END\n"


# --- codegen 映射 ---

def test_map_new_set_get_map_to_runtime() -> None:
    c = compile_c(_wrap(
        "10 DIM m AS HANDLE AS MAP AS VAR\n20 DIM ok AS BOOL AS VAR\n30 DIM v AS NUM AS DOUBLE AS VAR",
        "110 m = M.NEW()\n120 ok = M.SET(m, \"k\", 1.5)\n130 v = M.GET(m, \"k\")\n140 ok = M.CLOSE(m)",
    ))
    assert "sa_map_new()" in c
    assert 'sa_map_set(sa_m, "k", 1.5)' in c
    assert 'sa_map_get(sa_m, "k")' in c
    assert "sa_map_close(sa_m)" in c


def test_map_feature_enables_list_runtime() -> None:
    # KEYS 产出 STR_LIST 句柄，map runtime 调 list runtime，两个宏必须同时开
    c = compile_c(_wrap(
        "10 DIM m AS HANDLE AS MAP AS VAR\n20 DIM ok AS BOOL AS VAR",
        "110 m = M.NEW()\n120 ok = M.CLOSE(m)",
    ))
    assert "#define SA_ENABLE_MAP" in c
    assert "#define SA_ENABLE_LIST" in c


def test_strmap_get_is_freed() -> None:
    c = compile_c(_wrap(
        "10 DIM m AS HANDLE AS STR_MAP AS VAR\n20 DIM ok AS BOOL AS VAR\n30 DIM s AS STRING AS VAR",
        "110 m = M.NEW_STR()\n120 ok = M.SET_STR(m, \"k\", \"v\")\n130 s = M.GET_STR(m, \"k\")\n140 ok = M.CLOSE_STR(m)",
    ))
    assert 'sa_strmap_get(sa_m, "k")' in c
    assert "free(" in c


# --- 语义约束 ---

def test_map_kind_mismatch_rejected() -> None:
    expect_error(
        _wrap(
            "10 DIM m AS HANDLE AS STR_MAP AS VAR\n20 DIM v AS NUM AS DOUBLE AS VAR",
            "110 m = M.NEW_STR()\n120 v = M.GET(m, \"k\")",
        ),
        "类型不兼容",
    )


def test_map_key_must_be_string() -> None:
    expect_error(
        _wrap(
            "10 DIM m AS HANDLE AS MAP AS VAR\n20 DIM v AS NUM AS DOUBLE AS VAR",
            "110 m = M.NEW()\n120 v = M.GET(m, 42)",
        ),
        "类型不兼容",
    )


def test_unowned_map_handle_rejected() -> None:
    expect_error(
        _wrap(
            "10 DIM n AS NUM AS LONG AS VAR",
            "110 n = M.LENGTH(M.NEW())",
        ),
        "必须先赋给",
    )


def test_unowned_keys_result_rejected() -> None:
    # KEYS 返回的 STR_LIST 也是显式资源，不能匿名嵌套
    expect_error(
        _wrap(
            "10 DIM m AS HANDLE AS MAP AS VAR\n20 DIM n AS NUM AS LONG AS VAR",
            "110 m = M.NEW()\n120 n = L.LENGTH_STR(M.KEYS(m))",
            uses="5 USE SYS.MAP AS M\n6 USE SYS.LIST AS L",
        ),
        "必须先赋给",
    )


# --- 端到端 ---

_E2E_SOURCE = """10 USE SYS.MAP AS M
20 USE SYS.LIST AS L
30 DIM scores AS HANDLE AS MAP AS VAR
40 DIM labels AS HANDLE AS STR_MAP AS VAR
50 DIM keys AS HANDLE AS STR_LIST AS VAR
60 DIM ok AS BOOL AS VAR
70 SUB main AS PUBLIC AS VOID
80 scores = M.NEW()
90 ok = M.SET(scores, "alice", 95)
100 ok = M.SET(scores, "bob", 87)
110 ok = M.SET(scores, "alice", 99)
120 PRINT M.LENGTH(scores)
130 PRINT M.GET(scores, "alice")
140 PRINT M.HAS(scores, "bob")
150 ok = M.REMOVE(scores, "bob")
160 PRINT M.HAS(scores, "bob")
170 keys = M.KEYS(scores)
180 PRINT L.JOIN_STR(keys, ",")
190 ok = L.CLOSE_STR(keys)
200 ok = M.CLEAR(scores)
210 PRINT M.LENGTH(scores)
220 ok = M.CLOSE(scores)
230 labels = M.NEW_STR()
240 ok = M.SET_STR(labels, "en", "hello")
250 ok = M.SET_STR(labels, "en", "hi")
260 PRINT M.GET_STR(labels, "en")
270 ok = M.CLOSE_STR(labels)
280 PRINT M.GET(scores, "alice")
290 PRINT M.LAST_ERROR()
300 .ENDSUB
310 CALL main
320 END
"""

_E2E_EXPECTED = [
    "2",
    "99",
    "1",
    "0",
    "alice",
    "0",
    "hi",
    "0",
    "invalid or closed MAP handle",
]


@pytest.mark.e2e
@requires_c_compiler
def test_e2e_map_operations_run() -> None:
    from sonalgebraic.driver.compiler import build_exe

    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "maps.sa"
        src.write_text(_E2E_SOURCE, encoding="utf-8")
        exe = Path(temp) / ("maps.exe" if sys.platform == "win32" else "maps")
        build_exe(src, exe, keep_c=False)
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == _E2E_EXPECTED


@pytest.mark.e2e
@requires_c_compiler
def test_e2e_map_rehash_survives_many_keys() -> None:
    # 40 个 key 触发 16 桶两次 rehash，验证 rehash 后取值仍正确
    source = """10 USE SYS.MAP AS M
20 DIM m AS HANDLE AS MAP AS VAR
30 DIM ok AS BOOL AS VAR
40 DIM i AS NUM AS LONG AS VAR
50 DIM total AS NUM AS DOUBLE AS VAR
60 SUB main AS PUBLIC AS VOID
70 m = M.NEW()
80 FOR i = 1 TO 40
90 ok = M.SET(m, F"key{i}", i)
100 .ENDFOR
110 total = 0
120 FOR i = 1 TO 40
130 total = total + M.GET(m, F"key{i}")
140 .ENDFOR
150 PRINT M.LENGTH(m)
160 PRINT total
170 ok = M.CLOSE(m)
180 .ENDSUB
190 CALL main
200 END
"""
    from sonalgebraic.driver.compiler import build_exe

    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "maprehash.sa"
        src.write_text(source, encoding="utf-8")
        exe = Path(temp) / ("maprehash.exe" if sys.platform == "win32" else "maprehash")
        build_exe(src, exe, keep_c=False)
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == ["40", "820"]


@pytest.mark.e2e
@requires_c_compiler
def test_e2e_map_is_leak_free() -> None:
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
        src = Path(temp) / "maps.sa"
        src.write_text(_E2E_SOURCE, encoding="utf-8")
        c_path = Path(temp) / "maps.c"
        compile_to_c(src, c_path)
        patched = shim + "\n" + c_path.read_text(encoding="utf-8")
        patched = patched.replace("sa_program_end:", "sa_program_end: atexit(sa__rep);", 1)
        c_path.write_text(patched, encoding="utf-8")
        exe = Path(temp) / ("maps_counted.exe" if sys.platform == "win32" else "maps_counted")
        compile_proc = subprocess.run([gcc, str(c_path), "-O2", "-std=c11", "-o", str(exe), "-lm"], text=True, capture_output=True)
        assert compile_proc.returncode == 0, compile_proc.stderr
        proc = subprocess.run([str(exe)], text=True, capture_output=True)
        assert proc.returncode == 0
        assert "SA_LIVE=0" in proc.stderr, f"检测到内存泄漏: {proc.stderr}"
