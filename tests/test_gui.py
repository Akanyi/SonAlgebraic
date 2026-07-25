"""SYS.GUI 窗口模块测试：codegen 映射、语义约束、Windows 上的事件循环自测。"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys

import pytest

from conftest import compile_c, expect_error, requires_c_compiler


def _wrap(decls: str, body: str, uses: str = "5 USE SYS.GUI AS G") -> str:
    return f"{uses}\n{decls}\n100 SUB main AS PUBLIC AS VOID\n{body}\n800 .ENDSUB\n810 CALL main\n820 END\n"


# --- codegen 映射 ---

def test_gui_window_button_map_to_runtime() -> None:
    c = compile_c(_wrap(
        "10 DIM win AS HANDLE AS WINDOW AS VAR\n20 DIM btn AS HANDLE AS WIDGET AS VAR\n30 DIM ev AS NUM AS LONG AS VAR",
        "110 win = G.WINDOW(\"t\", 300, 200)\n120 btn = G.BUTTON(win, 1, \"OK\", 0, 0, 60, 24)\n130 ev = G.WAIT_EVENT()",
    ))
    assert 'sa_gui_window("t", 300, 200)' in c
    assert 'sa_gui_button(sa_win, 1, "OK", 0, 0, 60, 24)' in c
    assert "sa_gui_wait_event()" in c
    assert "#define SA_ENABLE_GUI" in c


def test_gui_get_text_is_freed() -> None:
    c = compile_c(_wrap(
        "10 DIM win AS HANDLE AS WINDOW AS VAR\n20 DIM box AS HANDLE AS WIDGET AS VAR\n30 DIM s AS STRING AS VAR",
        "110 win = G.WINDOW(\"t\", 300, 200)\n120 box = G.TEXTBOX(win, 0, 0, 100, 24)\n130 s = G.GET_TEXT(box)",
    ))
    assert "sa_gui_get_text(sa_box)" in c
    assert "free(" in c


# --- 语义约束 ---

def test_gui_widget_not_assignable_to_window() -> None:
    expect_error(
        _wrap(
            "10 DIM win AS HANDLE AS WINDOW AS VAR\n20 DIM w2 AS HANDLE AS WINDOW AS VAR",
            "110 win = G.WINDOW(\"t\", 300, 200)\n120 w2 = G.LABEL(win, \"x\", 0, 0, 10, 10)",
        ),
        "类型不兼容",
    )


def test_gui_set_text_requires_widget_handle() -> None:
    expect_error(
        _wrap(
            "10 DIM win AS HANDLE AS WINDOW AS VAR\n20 DIM ok AS BOOL AS VAR",
            "110 win = G.WINDOW(\"t\", 300, 200)\n120 ok = G.SET_TEXT(win, \"x\")",
        ),
        "类型不兼容",
    )


def test_gui_button_arity_checked() -> None:
    expect_error(
        _wrap(
            "10 DIM win AS HANDLE AS WINDOW AS VAR\n20 DIM btn AS HANDLE AS WIDGET AS VAR",
            "110 win = G.WINDOW(\"t\", 300, 200)\n120 btn = G.BUTTON(win, \"OK\")",
        ),
        "需要 7 个参数",
    )


# --- 端到端（仅 Windows；用 FFI PostMessage 自己点自己的按钮，验证事件循环闭环） ---

_SELFTEST_SOURCE = """10 USE SYS.GUI AS G
20 USEC <windows.h> AS WIN
30 USELIB "user32" AS U32
40 DECLARE C SUB WIN.FindWindowA(cls AS STRING, title AS CPTR) AS CPTR
50 DECLARE C SUB WIN.PostMessageA(h AS CPTR, m AS NUM AS LONG, w AS NUM AS LONG, l AS NUM AS LONG) AS NUM AS LONG
60 DIM win AS HANDLE AS WINDOW AS VAR
70 DIM btn AS HANDLE AS WIDGET AS VAR
80 DIM box AS HANDLE AS WIDGET AS VAR
90 DIM hwnd AS CPTR AS VAR
100 DIM r AS NUM AS LONG AS VAR
110 DIM ev AS NUM AS LONG AS VAR
120 DIM ok AS BOOL AS VAR
130 SUB main AS PUBLIC AS VOID
140 win = G.WINDOW("selftest", 300, 120)
150 btn = G.BUTTON(win, 7, "Go", 10, 10, 60, 24)
160 box = G.TEXTBOX(win, 10, 44, 200, 24)
170 ok = G.SET_TEXT(box, "hello from SA")
180 PRINT G.GET_TEXT(box)
190 hwnd = WIN.FindWindowA("SonAlgebraicWindow", NULL)
200 r = WIN.PostMessageA(hwnd, 273, 7, 0)
210 ev = G.WAIT_EVENT()
220 PRINT F"event={ev}"
230 ok = G.CLOSE(win)
240 ev = G.WAIT_EVENT()
250 PRINT F"after close={ev}"
260 .ENDSUB
270 CALL main
280 END
"""


@pytest.mark.e2e
@requires_c_compiler
@pytest.mark.skipif(sys.platform != "win32", reason="SYS.GUI 仅在 Windows 上有真实实现")
def test_e2e_gui_event_loop_roundtrip() -> None:
    from sonalgebraic.driver.compiler import build_exe

    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        src = Path(temp) / "guiself.sa"
        src.write_text(_SELFTEST_SOURCE, encoding="utf-8")
        exe = Path(temp) / "guiself.exe"
        build_exe(src, exe, keep_c=False)
        proc = subprocess.run([str(exe)], text=True, capture_output=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == ["hello from SA", "event=7", "after close=0"]
