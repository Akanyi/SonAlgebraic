from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import platform
import re
import shutil
import subprocess

from ..core.errors import SonCompileError
from ..core.names import module_c_name


@dataclass(frozen=True)
class StaticLibraryResult:
    target: str
    lib_path: Path


@dataclass(frozen=True)
class DynamicLibraryResult:
    target: str
    dll_path: Path
    import_lib: Path | None = None


def host_target() -> str:
    machine = platform.machine().lower()
    arch = "x86_64" if machine in {"amd64", "x86_64"} else "aarch64" if machine in {"arm64", "aarch64"} else machine
    system = platform.system().lower()
    if system == "windows":
        return f"{arch}-windows-gnu"
    if system == "linux":
        return f"{arch}-linux-gnu"
    if system == "darwin":
        return f"{arch}-macos"
    return f"{arch}-{system}"


def normalize_target(target: str | None) -> str:
    return (target or host_target()).lower()


def static_lib_name(module: str, target: str | None = None) -> str:
    return f"libsa_user_{module_c_name(module)}_{normalize_target(target).replace('-', '_')}.a"


def dynamic_lib_name(module: str, target: str | None = None) -> str:
    normalized = normalize_target(target)
    base = f"sa_user_{module_c_name(module)}"
    if "windows" in normalized:
        return f"{base}.dll"
    if "darwin" in normalized or "macos" in normalized:
        return f"lib{base}.dylib"
    return f"lib{base}.so"


def dynamic_import_lib_name(module: str, target: str | None = None) -> str:
    return f"libsa_user_{module_c_name(module)}_{normalize_target(target).replace('-', '_')}.dll.a"


def compile_static_library(c_paths: list[Path], include_dir: Path, output_path: Path, target: str | None = None) -> StaticLibraryResult:
    normalized = normalize_target(target)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    compiler_cmd, archiver_cmd = static_toolchain(normalized)
    object_dir = output_path.parent / (output_path.stem + "_obj")
    object_dir.mkdir(parents=True, exist_ok=True)

    objects: list[Path] = []
    for c_path in c_paths:
        obj_path = object_dir / (c_path.stem + ".o")
        command = [*compiler_cmd, "-O2", "-std=c11", f"-I{include_dir}", "-c", str(c_path), "-o", str(obj_path)]
        run_tool(command, f"C 静态库目标文件编译失败: {c_path}")
        objects.append(obj_path)

    run_tool([*archiver_cmd, "rcs", str(output_path), *(str(obj) for obj in objects)], "静态库归档失败")
    return StaticLibraryResult(normalized, output_path)


def compile_dynamic_library(c_paths: list[Path], include_dir: Path, output_dir: Path, module: str, target: str | None = None, link_libs: list[str] | None = None) -> DynamicLibraryResult:
    normalized = normalize_target(target)
    output_dir.mkdir(parents=True, exist_ok=True)
    compiler_cmd = dynamic_compiler_cmd(normalized)
    dll_name = dynamic_lib_name(module, normalized)
    dll_path = output_dir / dll_name

    object_dir = output_dir / (module_c_name(module) + "_dyn_obj")
    object_dir.mkdir(parents=True, exist_ok=True)

    objects: list[Path] = []
    for c_path in c_paths:
        obj_path = object_dir / (c_path.stem + ".o")
        command = [*compiler_cmd, "-O2", "-std=c11", "-fPIC", f"-I{include_dir}", "-c", str(c_path), "-o", str(obj_path)]
        run_tool(command, f"动态库目标文件编译失败: {c_path}")
        objects.append(obj_path)

    import_lib: Path | None = None
    link_lib_args = link_library_args(link_libs or [])
    if "windows" in normalized:
        import_lib = output_dir / dynamic_import_lib_name(module, normalized)
        command = [*compiler_cmd, "-shared", "-o", str(dll_path), *(str(obj) for obj in objects), f"-Wl,--out-implib,{import_lib}", *link_lib_args]
    elif "darwin" in normalized or "macos" in normalized:
        command = [*compiler_cmd, "-dynamiclib", "-o", str(dll_path), *(str(obj) for obj in objects), *link_lib_args]
    else:
        command = [*compiler_cmd, "-shared", "-fPIC", "-o", str(dll_path), *(str(obj) for obj in objects), *link_lib_args]

    run_tool(command, "动态库链接失败")
    return DynamicLibraryResult(normalized, dll_path, import_lib)


def static_toolchain(target: str) -> tuple[list[str], list[str]]:
    if target != host_target():
        if shutil.which("zig"):
            return ["zig", "cc", "-target", target], ["zig", "ar"]
        raise SonCompileError(f"交叉编译 target `{target}` 需要安装 zig")

    if shutil.which("gcc") and find_archiver():
        return ["gcc"], [find_archiver() or "ar"]
    if shutil.which("clang") and find_archiver():
        return ["clang"], [find_archiver() or "ar"]
    if shutil.which("zig"):
        return ["zig", "cc", "-target", target], ["zig", "ar"]
    raise SonCompileError("未找到可用于生成静态库的 gcc/clang/zig 和 ar")


def dynamic_compiler_cmd(target: str) -> list[str]:
    if target != host_target():
        if shutil.which("zig"):
            return ["zig", "cc", "-target", target]
        raise SonCompileError(f"交叉编译动态库 target `{target}` 需要安装 zig")

    if shutil.which("gcc"):
        return ["gcc"]
    if shutil.which("clang"):
        return ["clang"]
    if shutil.which("zig"):
        return ["zig", "cc", "-target", target]
    raise SonCompileError("未找到可用于生成动态库的 gcc/clang/zig")


def find_archiver() -> str | None:
    for name in ("ar", "gcc-ar", "llvm-ar"):
        if shutil.which(name):
            return name
    return None


LIBRARY_FILE_SUFFIXES = {".a", ".so", ".dylib", ".lib", ".dll"}

# 库名允许字母数字和 _ . + -，但不能以 - 打头（stdc++ / c++abi 这类要放行 +）
_SAFE_LIB_NAME = re.compile(r"^[A-Za-z0-9_.+][A-Za-z0-9_.+-]*$")


def validate_link_library(lib: str) -> str:
    """校验一个 USELIB 值，返回原值；不合法则报编译错误。

    USELIB 的值会原样进入 C 编译器命令行，而它来自模块源码——第三方 .slib/.spkg
    里也能写。以 '-' 开头的值会被当成编译器选项解析，`USELIB "-fplugin=evil.so"`
    足以让 GCC 在构建期加载任意插件，也就是 sonc build 一跑就中招。
    """
    if not lib or not lib.strip():
        raise SonCompileError("USELIB 的值不能为空")
    if lib.startswith("-"):
        raise SonCompileError(f"USELIB 的值不能以 '-' 开头（会被 C 编译器当成选项）: {lib}")
    if Path(lib).suffix in LIBRARY_FILE_SUFFIXES or Path(lib).exists():
        return lib
    if not _SAFE_LIB_NAME.match(lib):
        raise SonCompileError(f"USELIB 的库名只允许字母、数字和 _ . + - 组合: {lib}")
    return lib


def link_library_args(link_libs: list[str]) -> list[str]:
    args: list[str] = []
    for lib in link_libs:
        validate_link_library(lib)
        path = Path(lib)
        if path.exists() or path.suffix in LIBRARY_FILE_SUFFIXES:
            args.append(str(path))
        else:
            args.append(f"-l{lib}")
    return args


def run_tool(command: list[str], message: str) -> None:
    proc = subprocess.run(command, text=True, capture_output=True)
    if proc.returncode != 0:
        output = (proc.stdout + proc.stderr).strip()
        raise SonCompileError(f"{message}:\n{output}")
