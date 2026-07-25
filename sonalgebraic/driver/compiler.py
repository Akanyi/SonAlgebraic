from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from ..backend.codegen import generate_c
from ..backend.native.llvmir import generate_native_llvm_ir
from ..analysis.diagnostics import Diagnostic, DiagnosticError
from ..core.errors import SonCompileError, module_cycle_error
from ..analysis.exports import collect_exports
from ..core.module_model import ModuleExports
from ..packaging.module_compiler import compile_project
from ..core.names import module_path_to_slib, module_path_to_source, module_symbol_prefix
from ..frontend.parser import parse_program
from ..analysis.semantics import check_program, collect_program_diagnostics
from ..analysis.typesys import BUILTIN_MODULES, runtime_features_for_program
from ..packaging.slib import build_slib
from ..packaging.spkg import extract_spkg, spkg_module_source
from ..packaging.toolchain import host_target, normalize_target


@dataclass(frozen=True)
class BuildResult:
    c_path: Path
    exe_path: Path | None
    compiler: str | None


def compile_to_native_ir(source_path: Path, ir_path: Path, spkgs: list[Path] | None = None) -> Path:
    if spkgs:
        raise SonCompileError("native 后端暂不支持 .spkg")
    if source_has_user_modules(source_path):
        raise SonCompileError("native 后端暂不支持用户模块")
    source = source_path.read_text(encoding="utf-8-sig")
    checked = check_program(parse_program(source))
    ir_path.parent.mkdir(parents=True, exist_ok=True)
    ir_path.write_text(generate_native_llvm_ir(checked), encoding="utf-8")
    return ir_path


def compile_to_c(source_path: Path, c_path: Path, target: str | None = None, spkgs: list[Path] | None = None) -> Path:
    if source_has_user_modules(source_path):
        out_dir = c_path if c_path.suffix == "" else c_path.with_suffix("")
        plan = compile_project(source_path, out_dir, target, spkgs=spkgs)
        return plan.main_c

    source = source_path.read_text(encoding="utf-8-sig")
    checked = check_program(parse_program(source))
    c_path.parent.mkdir(parents=True, exist_ok=True)
    c_path.write_text(generate_c(checked), encoding="utf-8")
    return c_path


def check_source(source_path: Path, spkgs: list[Path] | None = None) -> None:
    source = source_path.read_text(encoding="utf-8-sig")
    diagnostics = _collect_source_text_diagnostics(source_path, source, spkgs, max_errors=50)
    if not diagnostics:
        return
    if len(diagnostics) == 1:
        raise diagnostics[0]
    raise SonCompileError(f"{len(diagnostics)} 个编译错误:\n" + "\n".join(str(item) for item in diagnostics))


def collect_source_diagnostics(source_path: Path, spkgs: list[Path] | None = None, max_errors: int = 50) -> list[SonCompileError]:
    source = source_path.read_text(encoding="utf-8-sig")
    return _collect_source_text_diagnostics(source_path, source, spkgs, max_errors)


def check_source_diagnostics(source_path: Path, spkgs: list[Path] | None = None, max_errors: int = 50) -> list[Diagnostic]:
    source = source_path.read_text(encoding="utf-8-sig")
    return [Diagnostic.from_compile_error(error, source) for error in _collect_source_text_diagnostics(source_path, source, spkgs, max_errors=max_errors)]


def _check_source_text(source_path: Path, source: str, spkgs: list[Path] | None = None) -> None:
    diagnostics = _collect_source_text_diagnostics(source_path, source, spkgs, max_errors=1)
    if diagnostics:
        raise diagnostics[0]


def _collect_source_text_diagnostics(source_path: Path, source: str, spkgs: list[Path] | None = None, max_errors: int = 50) -> list[SonCompileError]:
    max_errors = max(1, max_errors)
    diagnostics: list[SonCompileError] = []
    parse_source = source
    program = None
    for _ in range(max_errors):
        try:
            program = parse_program(parse_source)
            break
        except SonCompileError as exc:
            diagnostics.append(exc)
            if exc.line_no is None:
                return diagnostics
            recovered = comment_out_source_line(parse_source, exc.line_no)
            if recovered == parse_source:
                return diagnostics
            parse_source = recovered
    if program is None:
        return diagnostics[:max_errors]
    if len(diagnostics) >= max_errors:
        return diagnostics[:max_errors]

    with TemporaryDirectory(prefix="sonalgebraic-check-") as temp:
        try:
            spkg_dirs = extract_spkgs_for_check(spkgs or [], Path(temp))
        except SonCompileError as exc:
            diagnostics.append(exc)
            return diagnostics[:max_errors]
        external_modules: dict[str, ModuleExports] = {}
        checked_modules: dict[str, ModuleExports] = {}
        for use in program.uses:
            if use.module in BUILTIN_MODULES:
                continue
            try:
                exports = check_module_for_source(use.module, source_path.parent, checked_modules, [], spkg_dirs)
            except SonCompileError as exc:
                diagnostics.append(exc)
                return diagnostics[:max_errors]
            external_modules[use.alias.lower()] = exports
        diagnostics.extend(collect_program_diagnostics(program, external_modules=external_modules, require_main=True, max_errors=max_errors - len(diagnostics)))
        return diagnostics[:max_errors]


def comment_out_source_line(source: str, line_no: int) -> str:
    lines = source.splitlines(keepends=True)
    changed = False
    for index, raw in enumerate(lines):
        stripped = raw.lstrip()
        if not stripped.startswith(str(line_no)):
            continue
        after_number = stripped[len(str(line_no)) :]
        if after_number and not after_number[0].isspace():
            continue
        indent = raw[: len(raw) - len(stripped)]
        newline = "\n" if raw.endswith("\n") else ""
        lines[index] = f"{indent}{line_no} REM diagnostic recovery{newline}"
        changed = True
        break
    return "".join(lines) if changed else source


def extract_spkgs_for_check(spkgs: list[Path], temp_dir: Path) -> list[Path]:
    dirs: list[Path] = []
    for path in spkgs:
        extract_dir = temp_dir / path.stem
        extract_spkg(path, extract_dir)
        dirs.append(extract_dir)
    return dirs


def check_module_for_source(
    module: str,
    source_root: Path,
    checked_modules: dict[str, ModuleExports],
    visiting: list[str],
    spkg_dirs: list[Path],
) -> ModuleExports:
    key = module.lower()
    if key in checked_modules:
        return checked_modules[key]
    if any(item.lower() == key for item in visiting):
        raise module_cycle_error(visiting, module)
    visiting.append(module)

    try:
        source_text, dep_root = read_module_source_for_check(module, source_root, spkg_dirs)
        program = parse_program(source_text)
        external_modules: dict[str, ModuleExports] = {}
        for use in program.uses:
            if use.module in BUILTIN_MODULES:
                continue
            exports = check_module_for_source(use.module, dep_root, checked_modules, visiting, spkg_dirs)
            external_modules[use.alias.lower()] = exports
        check_program(program, external_modules=external_modules, require_main=False)
        exports = collect_exports(module, program)
        checked_modules[key] = exports
        return exports
    finally:
        visiting.pop()


def read_module_source_for_check(module: str, source_root: Path, spkg_dirs: list[Path]) -> tuple[str, Path]:
    source_path = module_path_to_source(source_root, module)
    if source_path.exists():
        return source_path.read_text(encoding="utf-8-sig"), source_path.parent

    slib_path = module_path_to_slib(source_root, module)
    if slib_path.exists():
        source_text = read_slib_root_source_for_check(slib_path)
        return source_text, source_root

    for spkg_dir in spkg_dirs:
        manifest_path = spkg_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        spkg_source = spkg_module_source(manifest, module, spkg_dir)
        if spkg_source is not None and spkg_source.exists():
            return spkg_source.read_text(encoding="utf-8-sig"), spkg_source.parent

    raise SonCompileError(f"找不到模块源文件、.slib 或 .spkg: {module}")


def read_slib_root_source_for_check(slib_path: Path) -> str:
    with ZipFile(slib_path) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        root_module = manifest.get("root_module")
        for item in manifest.get("units", []):
            if item.get("module") == root_module:
                return archive.read(item["source_entry"]).decode("utf-8-sig")
    raise SonCompileError(f".slib 缺少根模块源码: {slib_path}")


def build_exe(
    source_path: Path,
    output_path: Path,
    c_path: Path | None = None,
    keep_c: bool = True,
    target: str | None = None,
    spkgs: list[Path] | None = None,
    backend: str = "c",
) -> BuildResult:
    if backend == "native":
        return build_native_exe(source_path, output_path, ir_path=c_path, keep_ir=keep_c, target=target, spkgs=spkgs)
    if backend != "c":
        raise SonCompileError(f"未知后端: {backend}")
    if source_has_user_modules(source_path):
        out_dir = (c_path.parent if c_path is not None else output_path.parent) / output_path.stem
        plan = compile_project(source_path, out_dir, target, spkgs=spkgs)
        compiler = find_c_compiler(target)
        if compiler is None:
            raise SonCompileError("未找到 C 编译器，请安装 gcc、clang、tcc、zig 或 Visual Studio cl")
        run_c_compiler(compiler, plan.c_files, output_path, include_dir=out_dir, libs=plan.libs, link_libs=plan.link_libs, target=target, extra_args=gui_backend_args(plan.runtime_features, target))
        for dll in plan.dlls:
            shutil.copy2(dll, output_path.parent / dll.name)
        return BuildResult(plan.main_c, output_path, compiler)

    c_file = c_path or output_path.with_suffix(".c")
    source = source_path.read_text(encoding="utf-8-sig")
    checked = check_program(parse_program(source))
    c_file.parent.mkdir(parents=True, exist_ok=True)
    c_file.write_text(generate_c(checked), encoding="utf-8")
    compiler = find_c_compiler(target)
    if compiler is None:
        raise SonCompileError("未找到 C 编译器，请安装 gcc、clang、tcc、zig 或 Visual Studio cl")

    features = runtime_features_for_program(checked.program, checked.uses)
    link_libs = [lib.library for lib in checked.c_libs.values()] + builtin_link_libs(checked.uses, target, features)
    run_c_compiler(compiler, [c_file], output_path, link_libs=link_libs, target=target, extra_args=gui_backend_args(features, target))
    if not keep_c and c_path is None:
        c_file.unlink(missing_ok=True)
    return BuildResult(c_file, output_path, compiler)


def build_native_exe(
    source_path: Path,
    output_path: Path,
    ir_path: Path | None = None,
    keep_ir: bool = True,
    target: str | None = None,
    spkgs: list[Path] | None = None,
) -> BuildResult:
    if source_has_user_modules(source_path):
        out_dir = (ir_path.parent if ir_path is not None else output_path.parent) / output_path.stem
        plan = compile_project(source_path, out_dir, target, spkgs=spkgs)
        ir_file = ir_path or out_dir / source_path.with_suffix(".ll").name
        compile_main_to_native_ir_with_modules(source_path, ir_file, plan)
        compiler = find_native_compiler(target)
        if compiler is None:
            raise SonCompileError("native 后端需要 clang 或 zig cc 来编译 LLVM IR")
        module_sources = [Path(unit.c_path) for unit in plan.modules.values() if unit.c_path]
        extra_sources = [*( [plan.runtime_c] if plan.runtime_c is not None else [] ), *module_sources]
        run_native_compiler(compiler, ir_file, output_path, target=target, extra_sources=extra_sources, libs=plan.libs, link_libs=plan.link_libs, extra_args=gui_backend_args(plan.runtime_features, target))
        for dll in plan.dlls:
            shutil.copy2(dll, output_path.parent / dll.name)
        return BuildResult(ir_file, output_path, compiler)

    ir_file = ir_path or output_path.with_suffix(".ll")
    compile_to_native_ir(source_path, ir_file, spkgs=spkgs)
    compiler = find_native_compiler(target)
    if compiler is None:
        raise SonCompileError("native 后端需要 clang 或 zig cc 来编译 LLVM IR")
    # native 不在 IR 里重写算法，而是链接 C 运行时。把 runtime 源码写到 IR 旁，
    # 与 .ll 一起交给 clang/zig 编译链接（clang 按扩展名分别处理 .ll 与 .c）。
    checked = check_program(parse_program(source_path.read_text(encoding="utf-8-sig")))
    runtime_c = ir_file.with_name("sa_runtime.c")
    features = runtime_features_for_program(checked.program, checked.uses)
    runtime_c.write_text(_native_runtime_source(features), encoding="utf-8")
    link_libs = [lib.library for lib in checked.c_libs.values()] + builtin_link_libs(checked.uses, target, features)
    run_native_compiler(compiler, ir_file, output_path, target=target, extra_sources=[runtime_c], link_libs=link_libs, extra_args=gui_backend_args(features, target))
    if not keep_ir and ir_path is None:
        ir_file.unlink(missing_ok=True)
        runtime_c.unlink(missing_ok=True)
    return BuildResult(ir_file, output_path, compiler)


def compile_main_to_native_ir_with_modules(source_path: Path, ir_path: Path, plan) -> Path:
    source = source_path.read_text(encoding="utf-8-sig")
    program = parse_program(source)
    external_modules = {}
    for use in program.uses:
        if use.module in BUILTIN_MODULES:
            continue
        unit = plan.modules.get(use.module.lower())
        if unit is None:
            raise SonCompileError(f"找不到已编译模块: {use.module}", use.line_no)
        external_modules[use.alias.lower()] = unit.exports
    checked = check_program(program, external_modules=external_modules, require_main=True)
    init_calls = [module_symbol_prefix(unit.exports.module) + "_init" for unit in plan.modules.values()]
    free_calls = [module_symbol_prefix(unit.exports.module) + "_free" for unit in plan.modules.values()]
    ir_path.parent.mkdir(parents=True, exist_ok=True)
    ir_path.write_text(generate_native_llvm_ir(checked, main_init_calls=init_calls, main_free_calls=free_calls), encoding="utf-8")
    return ir_path


def gui_backend_args(features: set[str] | None, target: str | None = None) -> list[str]:
    """SYS.GUI 的 POSIX 后端探测：宿主机装有 GTK3 开发文件（pkg-config 可查到
    gtk+-3.0）时注入 -DSA_ENABLE_GUI_GTK 和编译/链接 flags，让 runtime 编出真窗口；
    否则静默跳过，落回"返回失败 + LAST_ERROR"分支。交叉编译时宿主 pkg-config
    的 flags 对目标平台无意义，同样跳过。"""
    if not features or "gui" not in features or sys.platform == "win32":
        return []
    if normalize_target(target) != host_target():
        return []
    try:
        cflags = subprocess.run(["pkg-config", "--cflags", "gtk+-3.0"], text=True, capture_output=True)
        libs = subprocess.run(["pkg-config", "--libs", "gtk+-3.0"], text=True, capture_output=True)
    except OSError:
        return []
    if cflags.returncode != 0 or libs.returncode != 0:
        return []
    return ["-DSA_ENABLE_GUI_GTK", *cflags.stdout.split(), *libs.stdout.split()]


def builtin_link_libs(uses: dict[str, str], target: str | None = None, features: set[str] | None = None) -> list[str]:
    target_name = normalize_target(target)
    modules = set(uses.values())
    runtime_features = features or set()
    libs: list[str] = []
    if "windows" in target_name and "SYS.NET" in modules:
        libs.extend(["winhttp", "ws2_32"])
    if "windows" in target_name and "tls" in runtime_features:
        libs.append("secur32")
    if "windows" not in target_name and "tls" in runtime_features:
        libs.extend(["ssl", "crypto"])
    if "windows" in target_name and "SYS.DESKTOP" in modules:
        libs.extend(["user32", "shell32"])
    if "windows" in target_name and "SYS.GUI" in modules:
        libs.extend(["user32", "gdi32"])
    return libs


def _native_runtime_source(runtime_features: set[str] | None = None) -> str:
    # RUNTIME_HEADER 含类型定义与声明，RUNTIME_SOURCE 是去掉 static 的实现，
    # 拼成一个自包含的 .c。native IR 只 declare 用到的符号，链接时解析到这里。
    from ..backend.c_runtime import RUNTIME_HEADER, RUNTIME_SOURCE

    macros = {"net": "SA_ENABLE_NET", "tls": "SA_ENABLE_TLS", "file": "SA_ENABLE_FILE", "desktop": "SA_ENABLE_DESKTOP", "binary": "SA_ENABLE_BINARY", "list": "SA_ENABLE_LIST", "map": "SA_ENABLE_MAP", "gui": "SA_ENABLE_GUI"}
    features = runtime_features or set()
    lines = [f"#define {macros[feature]}" for feature in sorted(features) if feature in macros]
    prefix = "" if not lines else "\n".join(lines) + "\n"
    return prefix + RUNTIME_HEADER + "\n" + RUNTIME_SOURCE


def find_c_compiler(target: str | None = None) -> str | None:
    if normalize_target(target) != host_target():
        return "zig" if shutil.which("zig") else None
    for name in ("gcc", "clang", "tcc", "zig", "cl"):
        if shutil.which(name):
            return name
    return None


def find_native_compiler(target: str | None = None) -> str | None:
    if normalize_target(target) != host_target():
        return "zig" if shutil.which("zig") else None
    if shutil.which("clang"):
        return "clang"
    if shutil.which("zig"):
        return "zig"
    return None


def run_native_compiler(
    compiler: str,
    ir_path: Path,
    exe_path: Path,
    target: str | None = None,
    extra_sources: list[Path] | None = None,
    libs: list[Path] | None = None,
    link_libs: list[str] | None = None,
    extra_args: list[str] | None = None,
) -> None:
    exe_path.parent.mkdir(parents=True, exist_ok=True)
    sources = [str(ir_path), *(str(p) for p in extra_sources or [])]
    lib_args = [str(path) for path in libs or []]
    link_lib_args = _link_lib_args(link_libs or [], compiler)
    rpath_args = rpath_flags(libs or [], normalize_target(target))
    passthrough = extra_args or []
    if compiler == "zig":
        target_args = ["-target", normalize_target(target)] if target else []
        command = ["zig", "cc", *target_args, *sources, *lib_args, "-O2", "-D_CRT_SECURE_NO_WARNINGS", *rpath_args, *link_lib_args, *passthrough, "-o", str(exe_path)]
    else:
        target_args = ["--target", normalize_target(target)] if target else []
        command = [compiler, *target_args, *sources, *lib_args, "-O2", "-D_CRT_SECURE_NO_WARNINGS", *rpath_args, *link_lib_args, *passthrough, "-o", str(exe_path)]
    proc = subprocess.run(command, text=True, capture_output=True)
    if proc.returncode != 0:
        output = (proc.stdout + proc.stderr).strip()
        output = _with_tls_dependency_hint(output, link_libs or [], target)
        raise SonCompileError(f"native 后端编译失败:\n{output}")


def run_c_compiler(
    compiler: str,
    c_paths: list[Path],
    exe_path: Path,
    include_dir: Path | None = None,
    libs: list[Path] | None = None,
    link_libs: list[str] | None = None,
    target: str | None = None,
    extra_args: list[str] | None = None,
) -> None:
    exe_path.parent.mkdir(parents=True, exist_ok=True)
    c_args = [str(path) for path in c_paths]
    lib_args = [str(path) for path in libs or []]
    link_lib_args = _link_lib_args(link_libs or [], compiler)
    include_args = [f"-I{include_dir}"] if include_dir is not None else []
    rpath_args = rpath_flags(libs or [], normalize_target(target))
    passthrough = extra_args or []
    if compiler == "zig":
        target_args = ["-target", normalize_target(target)] if target else []
        command = [compiler, "cc", *target_args, *c_args, *lib_args, "-O2", "-std=c11", *include_args, *rpath_args, *link_lib_args, *passthrough, "-o", str(exe_path)]
    elif compiler == "cl":
        cl_include = [f"/I{include_dir}"] if include_dir is not None else []
        command = [compiler, "/nologo", *cl_include, *c_args, *lib_args, *link_lib_args, f"/Fe:{exe_path}"]
    else:
        command = [compiler, *c_args, *lib_args, "-O2", "-std=c11", *include_args, *rpath_args, *link_lib_args, *passthrough, "-o", str(exe_path), "-lm"]

    proc = subprocess.run(command, text=True, capture_output=True)
    if proc.returncode != 0:
        output = (proc.stdout + proc.stderr).strip()
        output = _with_tls_dependency_hint(output, link_libs or [], target)
        output = _with_sa_line_hints(output, c_paths)
        raise SonCompileError(f"C 编译失败:\n{output}")


# gcc/clang: `path.c:123:5: error ...`；MSVC cl: `path.c(123): error C2065 ...`
# 文件部分允许 Windows 盘符前缀（C:\...），否则盘符冒号会截断匹配
_C_ERROR_LOCATION_RE = re.compile(r"^\s*(?P<file>(?:[A-Za-z]:)?[^:(\n]+\.c)(?::(?P<line>\d+)|\((?P<cl_line>\d+)\))")
_SA_COMMENT_RE = re.compile(r"/\* SA (?P<sa_line>\d+): (?P<source>.*?) \*/")


def _sa_line_index(c_text: str) -> list[tuple[int, str] | None]:
    """每个 C 行号（1-based 下标）到「向上最近的 SA 源码注释」的映射。"""
    index: list[tuple[int, str] | None] = [None]
    current: tuple[int, str] | None = None
    for line in c_text.splitlines():
        match = _SA_COMMENT_RE.search(line)
        if match is not None:
            current = (int(match.group("sa_line")), match.group("source"))
        index.append(current)
    return index


def map_c_errors_to_sa_lines(output: str, c_paths: list[Path]) -> list[str]:
    """从 C 编译器输出提取错误位置，映射回 SA 源码行。生成的 C 在每条语句处
    内联了 `/* SA nnn: ... */` 注释，向上找最近一条就是错误对应的 SA 语句。"""
    indexes: dict[str, list[tuple[int, str] | None]] = {}
    for path in c_paths:
        try:
            indexes[path.name.lower()] = _sa_line_index(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    hints: list[str] = []
    seen: set[tuple[str, int]] = set()
    for line in output.splitlines():
        match = _C_ERROR_LOCATION_RE.match(line)
        if match is None:
            continue
        file_name = Path(match.group("file")).name.lower()
        index = indexes.get(file_name)
        if index is None:
            continue
        c_line = int(match.group("line") or match.group("cl_line"))
        if c_line >= len(index) or index[c_line] is None:
            continue
        sa_line, sa_source = index[c_line]
        key = (file_name, sa_line)
        if key in seen:
            continue
        seen.add(key)
        hints.append(f"  {match.group('file')}:{c_line} -> SA {sa_line}: {sa_source}")
    return hints


def _with_sa_line_hints(output: str, c_paths: list[Path]) -> str:
    hints = map_c_errors_to_sa_lines(output, c_paths)
    if not hints:
        return output
    return output + "\n\n可能对应的 SA 源码位置:\n" + "\n".join(hints)


def _with_tls_dependency_hint(output: str, link_libs: list[str], target: str | None) -> str:
    target_name = normalize_target(target)
    if "windows" in target_name or not {"ssl", "crypto"}.intersection(link_libs):
        return output
    hint = (
        "SYS.NET TLS 在 POSIX 目标需要 OpenSSL 开发文件（openssl/ssl.h、libssl、libcrypto）。\n"
        "Debian/Ubuntu: apt install libssl-dev；Fedora/RHEL: dnf install openssl-devel；"
        "macOS: brew install openssl pkg-config。"
    )
    if target_name != host_target():
        hint += " 交叉编译时必须提供目标平台的 OpenSSL SDK，不能复用宿主机库。"
    return f"{output}\n\n{hint}"


def _link_lib_args(link_libs: list[str], compiler: str | None = None) -> list[str]:
    args: list[str] = []
    for lib in link_libs:
        if Path(lib).exists() or Path(lib).suffix in {".a", ".so", ".dylib", ".lib", ".dll"}:
            args.append(str(Path(lib)))
        elif compiler == "cl":
            args.append(f"{lib}.lib")
        else:
            args.append(f"-l{lib}")
    return args


def rpath_flags(libs: list[Path], target: str) -> list[str]:
    has_dynamic = any(lib.suffix in {".so", ".dylib"} for lib in libs)
    if not has_dynamic:
        return []
    if "windows" in target:
        return []
    if "darwin" in target or "macos" in target:
        return ["-Wl,-rpath,@loader_path"]
    return ["-Wl,-rpath,$ORIGIN"]


def source_has_user_modules(source_path: Path) -> bool:
    program = parse_program(source_path.read_text(encoding="utf-8-sig"))
    return any(use.module not in BUILTIN_MODULES for use in program.uses)
