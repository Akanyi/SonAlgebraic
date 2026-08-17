"""pytest 共享配置与工具。

集中三类东西，避免各主题测试模块重复：
1. sys.path 注入，保证不安装包也能 import sonalgebraic。
2. 编译/报错断言的轻量 helper。
3. 需要本机 C 工具链的测试统一 skip 条件。
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sonalgebraic.backend.codegen import generate_c
from sonalgebraic.core.errors import SonCompileError
from sonalgebraic.driver.compiler import find_c_compiler, find_native_compiler
from sonalgebraic.frontend.parser import parse_program
from sonalgebraic.analysis.semantics import check_program

# C 工具链是否可用，供 e2e/ffi 测试做 skip 判定
HAS_C_COMPILER = find_c_compiler() is not None
import shutil

HAS_GCC = shutil.which("gcc") is not None
HAS_NATIVE_COMPILER = find_native_compiler() is not None

requires_c_compiler = pytest.mark.skipif(
    not HAS_C_COMPILER, reason="未找到可用的 C 编译器"
)
requires_gcc = pytest.mark.skipif(
    not HAS_GCC, reason="未找到 gcc（反向 FFI 需要 MinGW gcc 生成/链接动态库）"
)
requires_native_compiler = pytest.mark.skipif(
    not HAS_NATIVE_COMPILER, reason="未找到 clang 或 zig（native 后端需要 LLVM IR 编译工具）"
)


def build_temp(prefix: str, subdir: str = "native-tests") -> TemporaryDirectory[str]:
    """要真构建 exe 的测试用这个，而不是 pytest 的 tmp_path。

    Windows 上安全软件容易拦截系统 TEMP 里刚落地的 exe，构建或运行会偶发失败，
    而且失败点跟被测行为无关。产物统一放到项目 build/ 下，和日常构建目录一致，
    减少这类误杀噪声。
    """
    root = REPO_ROOT / "build" / subdir
    root.mkdir(parents=True, exist_ok=True)
    return TemporaryDirectory(prefix=prefix, dir=root)


def compile_c(source: str) -> str:
    """解析 + 语义检查 + 生成 C，返回 C 源码文本。"""
    return generate_c(check_program(parse_program(source)))


def compile_user_c(source: str) -> str:
    """只生成用户代码段，不注入 runtime。

    数生成 C 里某个模式出现几次的测试要用这个。runtime 自己也含 `else {`、
    `while (` 之类，而它现在是按需注入的切片，"总数减去 RUNTIME 里的数量"
    这种老办法已经不成立了。
    """
    from sonalgebraic.backend.codegen import CGen

    return CGen(check_program(parse_program(source)), include_runtime=False).generate()


def expect_error(source: str, needle: str) -> None:
    """断言源码会触发包含 needle 的 SonCompileError。"""
    with pytest.raises(SonCompileError) as excinfo:
        check_program(parse_program(source))
    assert needle in str(excinfo.value), f"期望错误包含 `{needle}`，实际为 `{excinfo.value}`"


@pytest.fixture
def examples_dir() -> Path:
    return REPO_ROOT / "examples"


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT
