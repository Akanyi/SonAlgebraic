"""含托管资源的 ENTITY：字段级所有权，以及作用域退出时的逐字段析构。

单个 `FILE` 变量好办——变量名就是槽位名。实体麻烦在**同一个变量里的多个槽位各自
独立**：`bundle.first` 关掉了不代表 `bundle.second` 也关了，而 `bundle` 整体离开作用域时
两个都得关，且各关一次。所以这里的两组测试一头一尾：

- 语义侧：诊断必须按字段路径算，而不是退化成「实体里有没有托管资源」这种整块判断。
  复用 `bundle` 本身是复制（析构会关两遍）、借用整个 `bundle` 后写它另一个字段
  （冻结的是所有者，不只是被读的那个字段）、CLOSE 借用来的字段（字段得跟着只读）。
- 后端侧：真的编译运行两遍，让作用域退出把两个 FILE 各析构一次，再用 malloc 计数桩
  证明没有泄漏、也没有第二次 fclose。

恢复说明：原文件在 10 月初丢失，测试侧没有字节码可比对。这份是按幸存的 pytest 节点名
（含每个参数化用例的源码与期望诊断）配合恢复后的 `analysis/ownership.py`、
`backend/codegen/entities.py`、`backend/native/entities.py` 重建的，不是逐字找回。
详见 `.recovery/RECOVERY.md`。
"""
from __future__ import annotations

from pathlib import Path
import re
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

# runtime 是共用的，但字段析构在两个后端是两份独立实现（C 展开成 sa_file_drop 调用，
# native 走 emit_free_slot 的 IR），所以后端要各跑一遍。opt 也各跑一遍：C 侧
# sa_file_drop 的槽位清理是可用性推理就能删掉的代码，优化开关看到的是不同路径。
_BACKENDS = [pytest.param("c", marks=requires_gcc), pytest.param("native", marks=requires_wrap)]

# _WRAP 只提供计数变量，不负责汇报。退出时把净分配打到 stderr，两边的断言才能用同一行。
_LIVE_REPORT = r'''
#include <stdio.h>
extern long sa_test_live;
static void sa_probe_report(void) { fprintf(stderr, "SA_LIVE=%ld\n", sa_test_live); }
__attribute__((constructor)) static void sa_probe_register(void) { atexit(sa_probe_report); }
'''

_HEADER = (
    "10 USE SYS.FILE AS F\n"
    "20 FOR ENTITY AS Bundle\n"
    "30 DIM first AS FILE AS VAR\n"
    "40 DIM second AS FILE AS VAR\n"
    "50 DIM note AS STRING AS VAR\n"
    "60 .ENDENTITY\n"
)


# 两个实体变量在 main 里以局部身份声明，片段紧接其后——必须是局部：`f=` 的源不能是
# 全局变量（其他 SUB 可能在背后改它，冻结管不到），所以借用那两条只有局部才谈得上。
_LOCALS = ["DIM bundle AS ENTITY AS Bundle", "DIM other AS ENTITY AS Bundle"]


def _program(body: str) -> str:
    """实体声明 + SUB 头 + 两个局部实体变量 + 片段，行号按 10 递增。"""
    lines = [*(f"{100 + index * 10} {text}" for index, text in enumerate(_LOCALS))]
    lines += [f"{100 + (len(_LOCALS) + index) * 10} {line.strip()}"
              for index, line in enumerate(body.strip().splitlines())]
    return (_HEADER + "90 SUB main AS PUBLIC AS VOID\n" + "\n".join(lines)
            + "\n500 .ENDSUB\n510 CALL main\n520 END\n")


# 每条负例的原文就是参数化 id 里存的那段（两个变量声明在参数之外），必须逐字对上。
_ERROR_CASES = [
    # 整块复制：字段里各有一个 FILE，复制出两份持有者，作用域退出会各关两遍
    ("other = bundle", "独占资源不能用 = 复制"),
    # 借用整个实体：借用者只读，借来的字段上的 CLOSE 会把所有者的流关掉
    ("other f= bundle\nPRINT F.CLOSE(other.first)", "只读"),
    # 冻结的是所有者本身：借用存续期间写它**另一个**字段同样越权，
    # 否则借用者手里的 first 会在背后被换掉
    ('other f= bundle\nPRINT F.WRITE(bundle.second, "bad")', "借出"),
    # 关闭状态按字段路径记：关过 first 之后再用 first 要报错，second 不受影响
    ("PRINT F.CLOSE(bundle.first)\nPRINT F.SIZE(bundle.first)", "已关闭"),
]


@pytest.mark.parametrize(("body", "needle"), _ERROR_CASES)
def test_entity_file_field_diagnostics_match_check_mode(body: str, needle: str) -> None:
    """字段路径必须参与所有权判定，而不是「实体里有没有托管资源」一刀切。

    四条负例覆盖两种错法：把实体当整体（前两条会漏报或报错话），以及把字段状态
    挂在实体名上（后两条会把 second 也一起禁掉，或让 first 的关闭失效）。
    """
    source = _program(body)
    with pytest.raises(SonCompileError, match=needle) as excinfo:
        check_program(parse_program(source))
    # 同一份源码在逐条收集诊断的模式下必须给出同样的结论：并行/批量报错路径绕开了
    # 这条检查的话，IDE 里会显示「无错误」而命令行拒绝编译。
    from sonalgebraic.analysis.semantics import collect_program_diagnostics

    collected = collect_program_diagnostics(parse_program(source))
    assert any(needle in str(error) for error in collected), [str(error) for error in collected]


_OWNERSHIP_SOURCE = (
    _HEADER
    + "90 DIM bundle AS ENTITY AS Bundle AS VAR\n"
      "100 SUB main AS PUBLIC AS VOID\n"
      '110 bundle.first = F.OPEN("first.bin", "WRITE")\n'
      '120 bundle.second = F.OPEN("second.bin", "WRITE")\n'
      '130 PRINT F.TELL(bundle.first)\n'
      '140 PRINT F.CLOSE(bundle.first)\n'
      '150 bundle.first = F.OPEN("first-again.bin", "WRITE")\n'
      '160 PRINT F.WRITE(bundle.second, "payload")\n'
      '170 PRINT F.TELL(bundle.first)\n'
      "190 .ENDSUB\n"
      "200 CALL main\n"
      "210 END\n"
)

_OWNERSHIP_EXPECTED = ["0", "1", "7", "0"]


def _write_c(root: Path, source: str) -> Path:
    c_path = root / "bundle.c"
    c_path.write_text(_LEAK_SHIM + "\n" + compile_c(source), encoding="utf-8")
    return c_path


def _write_ir(root: Path, source: str) -> Path:
    """IR 原样落盘，不把 @main 改名。

    clang 靠「某个输入里有 main」来定子系统与入口点；改名之后链接器只会报
    `subsystem must be defined`——那是脚手架自己造出来的问题，不是后端的问题。
    """
    checked = check_program(parse_program(source))
    ir_path = root / "bundle.ll"
    ir_path.write_text(generate_native_llvm_ir(checked), encoding="utf-8")
    return ir_path


@pytest.mark.parametrize("backend", _BACKENDS)
@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_entity_owns_independent_file_fields(backend: str, opt: str) -> None:
    """真跑一遍：一个字段关掉、重开，另一个照用；退出时两个各析构一次。

    断言同时看 stdout 和 net allocation：只对 stdout 的话，「析构漏了」这种错会表现为
    程序退出后流还在（看不到），而「析构多了一次」才可能崩。
    """
    with build_temp(f"managed-entity-{backend}-", "entity-tests") as temp:
        root = Path(temp)
        exe = root / ("bundle.exe" if sys.platform == "win32" else "bundle")
        if backend == "c":
            c_path = _write_c(root, _OWNERSHIP_SOURCE)
            c_text = c_path.read_text(encoding="utf-8")
            c_text = c_text.replace("sa_program_end:", "sa_program_end: atexit(sa__rep);", 1)
            c_path.write_text(c_text, encoding="utf-8")
            gcc = shutil.which("gcc")
            assert gcc is not None
            in_main = compile_c(_OWNERSHIP_SOURCE)
            for field in ("first", "second"):
                assert f"sa_file_drop(sa_bundle.{field})" in in_main
            command = [gcc, str(c_path), opt, "-std=c11", "-o", str(exe), "-lm"]
        else:
            ir_path = _write_ir(root, _OWNERSHIP_SOURCE)
            (root / "rt.c").write_text(
                "#define SA_ENABLE_FILE\n" + RUNTIME_HEADER + "\n" + RUNTIME_SOURCE, encoding="utf-8")
            (root / "wrap.c").write_text(_WRAP + _LIVE_REPORT, encoding="utf-8")
            flags = [f"-Wl,-wrap:{name}" if sys.platform == "win32" else f"-Wl,--wrap={name}"
                     for name in ("malloc", "calloc", "realloc", "free")]
            command = [shutil.which("clang"), "-fuse-ld=lld", str(ir_path), str(root / "rt.c"),
                       str(root / "wrap.c"), opt, "-fno-builtin", "-D_CRT_SECURE_NO_WARNINGS",
                       *flags, "-o", str(exe)]
            if sys.platform != "win32":
                command.append("-lm")
        compiled = subprocess.run(command, text=True, capture_output=True, timeout=180)
        assert compiled.returncode == 0, compiled.stderr
        result = subprocess.run([str(exe)], cwd=root, text=True, encoding="utf-8",
                                capture_output=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == _OWNERSHIP_EXPECTED
    live = re.search(r"SA_LIVE=(-?\d+)", result.stderr)
    assert live is not None, result.stderr
    assert int(live.group(1)) == 0, f"实体字段析构漏了: {result.stderr}"
