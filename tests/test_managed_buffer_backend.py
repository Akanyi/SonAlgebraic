"""`BUFFER` 和 `FILE` 是同一套托管句柄协议，只换了个 kind。

buffer 没有 FILE 那种「游标属于所有者」的额外麻烦，所以这组测试盯的是**协议本身**是否
真的按 kind 走一遍：工厂认领、独占（不能 `=` 复制）、裸/托管不能互转、借用只读、
所有者借出期间冻结、关闭后失效。少了任何一条，BUFFER 就会退化成「只有 FILE 享受保护」
——而它们在 runtime 里是同一个 `SaHandle`，一个整数 token，类型层是唯一的防线。

另一半在真的编译运行：作用域退出要按 `sa_binary_drop` 把 buffer 关掉，而且移动出去的
和重开的都各归各的，一个都不能漏。文字断言只证明「发射了 drop」，计数桩才证明
「每个 buffer 恰好关一次」。

恢复说明：原文件在 10 月初丢失，测试侧没有字节码可比对。这份是按幸存的 pytest 节点名
（含每个参数化用例的源码与期望诊断）配合恢复后的 `analysis/builtin_effects.py`、
`analysis/typesys.py`（`SYS.BINARY` 成员表）重建的，不是逐字找回。详见 `.recovery/RECOVERY.md`。
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from conftest import build_temp, compile_c, requires_gcc
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.backend.c_runtime import RUNTIME_HEADER, RUNTIME_SOURCE
from sonalgebraic.backend.native import generate_native_llvm_ir
from sonalgebraic.core.errors import SonCompileError
from sonalgebraic.frontend.parser import parse_program
from test_coroutines import _LEAK_SHIM
from test_native_coroutines import _WRAP, requires_wrap

_BACKENDS = [pytest.param("c", marks=requires_gcc), pytest.param("native", marks=requires_wrap)]

_HEADER = "10 USE SYS.BINARY AS B\n"

_LIVE_REPORT = r'''
#include <stdio.h>
extern long sa_test_live;
static void sa_probe_report(void) { fprintf(stderr, "SA_LIVE=%ld\n", sa_test_live); }
__attribute__((constructor)) static void sa_probe_register(void) { atexit(sa_probe_report); }
'''


def _program(fragment: str, local: str = "DIM owner AS MANAGED HANDLE AS BUFFER") -> str:
    """`local` 一行声明 + 片段，一起放进 main 体内，行号从 40 起。

    变量声明必须留在 SUB 里而不是写成全局：`f=` / `m=` 一律拒绝全局源（其他 SUB 可能
    在背后改它，冻结管不到），全局声明会让每条负例都报成同一条无关的错。
    """
    lines = [f"{40 + index * 10} {line.strip()}"
             for index, line in enumerate(f"{local}\n{fragment}".strip().splitlines())]
    return (_HEADER + "30 SUB main AS PUBLIC AS VOID\n" + "\n".join(lines)
            + "\n500 .ENDSUB\n510 CALL main\n520 END\n")


_ERROR_CASES = [
    # `=` 在托管句柄上不是复制而是多一个持有者，析构会关两遍
    ("DIM alias AS MANAGED HANDLE AS BUFFER\nalias = owner", "独占资源不能用 = 复制"),
    # 裸句柄和托管句柄在 C 里是同一个整数，只能靠类型层拦住互转
    ("DIM raw AS HANDLE AS BUFFER\nraw = owner", "不能隐式互转"),
    # 所有者借出期间冻结：PACK_* 会改缓冲区内容，借用者读到的就变了
    ("DIM view AS MANAGED HANDLE AS BUFFER\nview f= owner\nPRINT B.PACK_U16_LE(owner, 0, 1)", "借出"),
    # 借用者只读：它不持有所有权，写进去等于替所有者改数据
    ("DIM view AS MANAGED HANDLE AS BUFFER\nview f= owner\nPRINT B.PACK_U16_LE(view, 0, 1)", "只读"),
    # 关闭状态跨语句保留：CLOSE 之后再查询是拿失效 token 取值
    ("PRINT B.CLOSE(owner)\nPRINT B.LENGTH(owner)", "已关闭"),
]


@pytest.mark.parametrize(("body", "needle"), _ERROR_CASES)
def test_buffer_ownership_rules_match_the_file_kind(body: str, needle: str) -> None:
    """五条规则一条不能少：只要 kind 不是 FILE 就整体放行，等于没有保护。

    负例一律走语义检查（不编译），因为要守的正是「编译期拦住」这件事——漏到这里
    之后 runtime 拿到的是一个早已关掉的整数，报错只会是一句无因的 -1。
    """
    with pytest.raises(SonCompileError, match=needle):
        check_program(parse_program(_program(body)))


_SOURCE = (
    _HEADER
    + "20 SUB main AS PUBLIC AS VOID\n"
      "30 DIM first AS MANAGED HANDLE AS BUFFER AS VAR\n"
      "40 DIM second AS MANAGED HANDLE AS BUFFER AS VAR\n"
      "50 first = B.NEW(8)\n"
      "60 second = B.NEW(4)\n"
      "70 SCOPE\n"
      "80 DIM moved AS MANAGED HANDLE AS BUFFER\n"
      "90 moved m= first\n"
      "100 PRINT B.LENGTH(moved)\n"
      "110 .ENDSCOPE\n"
      "120 first = B.NEW(16)\n"
      "130 PRINT B.LENGTH(first)\n"
      "140 PRINT B.LENGTH(second)\n"
      "150 PRINT B.CLOSE(first)\n"
      "160 first = B.NEW(2)\n"
      "170 PRINT B.LENGTH(first)\n"
      "180 .ENDSUB\n"
      "190 CALL main\n"
      "200 END\n"
)

_EXPECTED = ["8", "16", "4", "1", "2"]


@pytest.mark.parametrize("backend", _BACKENDS)
@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_scope_exit_move_and_reopen_release_every_buffer(backend: str, opt: str) -> None:
    """运行一遍：移动进 SCOPE 的那个随作用域退出释放，重开的按新长度算，退出后净分配为 0。

    每一段都对应一个具体的漏法：SCOPE 里 `m=` 之后原变量置空、块尾释放 moved；
    重开的 buffer 不是泄漏就是双重释放；显式 CLOSE 之后不能再析构第二次。
    """
    generated = compile_c(_SOURCE)
    # 作用域退出与程序退出都必须走 kind 对应的 drop，而不是同一个通用 free
    assert "sa_binary_drop(" in generated

    with build_temp(f"managed-buffer-{backend}-", "buffer-tests") as temp:
        root = Path(temp)
        exe = root / ("buffer.exe" if sys.platform == "win32" else "buffer")
        if backend == "c":
            c_path = root / "buffer.c"
            text = _LEAK_SHIM + "\n" + generated
            text = text.replace("sa_program_end:", "sa_program_end: atexit(sa__rep);", 1)
            c_path.write_text(text, encoding="utf-8")
            gcc = shutil.which("gcc")
            assert gcc is not None
            command = [gcc, str(c_path), opt, "-std=c11", "-o", str(exe), "-lm"]
        else:
            checked = check_program(parse_program(_SOURCE))
            (root / "buffer.ll").write_text(generate_native_llvm_ir(checked), encoding="utf-8")
            (root / "rt.c").write_text(
                "#define SA_ENABLE_BINARY\n" + RUNTIME_HEADER + "\n" + RUNTIME_SOURCE, encoding="utf-8")
            (root / "wrap.c").write_text(_WRAP + _LIVE_REPORT, encoding="utf-8")
            flags = [f"-Wl,-wrap:{name}" if sys.platform == "win32" else f"-Wl,--wrap={name}"
                     for name in ("malloc", "calloc", "realloc", "free")]
            command = [shutil.which("clang"), "-fuse-ld=lld", str(root / "buffer.ll"),
                       str(root / "rt.c"), str(root / "wrap.c"), opt, "-fno-builtin",
                       "-D_CRT_SECURE_NO_WARNINGS", *flags, "-o", str(exe)]
            if sys.platform != "win32":
                command.append("-lm")
        compiled = subprocess.run(command, text=True, capture_output=True, timeout=180)
        assert compiled.returncode == 0, compiled.stderr
        result = subprocess.run([str(exe)], cwd=root, text=True, encoding="utf-8",
                               capture_output=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == _EXPECTED
    assert "SA_LIVE=0" in result.stderr, result.stderr
