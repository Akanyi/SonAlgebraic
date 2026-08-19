"""反向 FFI 测试：C 程序作为消费方，调用 SonAlgebraic 编译出的动态库。

验证链路：mathffi.sa -> C11 -> DLL(+import lib) -> 外部 C 程序 #include 头 + 链接调用。
需要 gcc（MinGW），缺失时整个模块被 skip。
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile
import json
import shutil
import subprocess

import pytest

from conftest import requires_gcc, requires_windows
from sonalgebraic.backend.c_runtime import RUNTIME_HEADER
from sonalgebraic.packaging.slib import build_slib

# 整个文件断言的是 Windows 的反向 FFI 产物：manifest 里的 import_lib、随 exe
# 落地的 .dll。POSIX 的动态库没有 import lib 这个概念，测不了同一件事。
pytestmark = [pytest.mark.ffi, requires_gcc, requires_windows]

FFI_DIR = Path(__file__).resolve().parent / "ffi_c"


def test_c_consumer_calls_sa_dynamic_library() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-ffi-") as temp:
        work = Path(temp)

        # 1. SA -> dynamic .slib
        slib_path = work / "mathffi.slib"
        build_slib(FFI_DIR / "mathffi.sa", slib_path, dynamic=True)

        # 2. 解包
        with ZipFile(slib_path) as archive:
            archive.extractall(work)
        manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))

        # 3. 写出 sa_runtime.h（头文件 #include 了它，但不打进包内）
        (work / "include" / "sa_runtime.h").write_text(RUNTIME_HEADER.strip() + "\n", encoding="utf-8")

        # 4. 从 manifest 取当前 target 的 DLL / import lib 真实路径，不硬编码文件名
        target = manifest["target"]
        archive_info = manifest["archives"][target]
        dll = work / archive_info["dll"]
        import_lib = work / archive_info["import_lib"]
        assert dll.exists() and import_lib.exists()

        # 5. gcc 编译 C 测试，链接 import lib
        exe = work / "test_ffi.exe"
        result = subprocess.run(
            [
                "gcc", "-std=c11", "-O2",
                f"-I{work / 'include'}",
                str(FFI_DIR / "test_ffi.c"),
                str(import_lib),
                "-o", str(exe),
            ],
            text=True, capture_output=True,
        )
        assert result.returncode == 0, f"gcc 编译失败:\n{result.stdout}\n{result.stderr}"

        # 6. DLL 必须与 exe 同目录才能在运行时加载
        shutil.copy(dll, exe.parent / dll.name)

        # 7. 运行，C 端所有断言通过则退出码为 0
        run = subprocess.run([str(exe)], text=True, capture_output=True)
        assert run.returncode == 0, f"C 测试失败:\n{run.stdout}\n{run.stderr}"
        assert "ALL FFI CHECKS PASSED" in run.stdout
