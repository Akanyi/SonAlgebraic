from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil

from ..backend.codegen import CGen
from ..backend.c_runtime import RUNTIME_HEADER, RUNTIME_SOURCE
from ..backend.runtime_slicer import runtime_impl_for, runtime_symbols_in
from ..core.errors import SonCompileError, module_cycle_error
from ..analysis.exports import collect_exports
from ..analysis.typesys import BUILTIN_MODULES, runtime_features_for_program
from ..backend.headergen import generate_header
from ..core.module_model import ModuleExports, ModuleUnit
from ..core.names import module_c_name, module_path_to_slib, module_path_to_source, module_symbol_prefix
from ..frontend.parser import parse_program
from ..analysis.semantics import check_program
from .spkg import extract_spkg, spkg_extract_dir, spkg_module_source
from .toolchain import normalize_target


@dataclass
class ModuleBuildPlan:
    main_c: Path
    runtime_c: Path | None
    c_files: list[Path] = field(default_factory=list)
    libs: list[Path] = field(default_factory=list)
    dlls: list[Path] = field(default_factory=list)
    link_libs: list[str] = field(default_factory=list)
    headers: list[Path] = field(default_factory=list)
    modules: dict[str, ModuleUnit] = field(default_factory=dict)
    runtime_features: set[str] = field(default_factory=set)


def compile_project(source_path: Path, out_dir: Path, target: str | None = None, spkgs: list[Path] | None = None) -> ModuleBuildPlan:
    out_dir.mkdir(parents=True, exist_ok=True)
    spkg_dirs = _extract_spkgs(spkgs or [], out_dir / "_spkgs")
    runtime_h = out_dir / "sa_runtime.h"
    runtime_c = out_dir / "sa_runtime.c"
    source_root = source_path.parent
    module_units: dict[str, ModuleUnit] = {}
    exports_by_alias: dict[str, ModuleExports] = {}

    main_source = source_path.read_text(encoding="utf-8-sig")
    main_program = parse_program(main_source)
    for use in main_program.uses:
        if use.module in BUILTIN_MODULES:
            continue
        unit = compile_module(use.module, use.alias, source_root, out_dir, module_units, target, spkg_dirs=spkg_dirs)
        exports_by_alias[use.alias.lower()] = unit.exports

    checked = check_program(main_program, external_modules=exports_by_alias, require_main=True)
    main_c = out_dir / source_path.with_suffix(".c").name
    includes = [unit.exports.header_name for unit in module_units.values()]
    main_body = CGen(
        checked,
        include_runtime=False,
        include_main=True,
        include_headers=includes,
        main_init_calls=[module_symbol_prefix(unit.exports.module) + "_init" for unit in module_units.values()],
        main_free_calls=[module_symbol_prefix(unit.exports.module) + "_free" for unit in module_units.values()],
    ).generate()
    main_c.write_text(main_body, encoding="utf-8")

    runtime_features = runtime_features_for_program(checked.program, checked.uses)
    runtime_features.update(feature for unit in module_units.values() for feature in unit.runtime_features)
    runtime_prefix = _runtime_feature_prefix(runtime_features)
    runtime_h.write_text(runtime_prefix + RUNTIME_HEADER.strip() + "\n", encoding="utf-8")
    runtime_c.write_text(
        runtime_prefix + '#include "sa_runtime.h"\n\n' + _runtime_impl([main_body], module_units, runtime_features) + "\n",
        encoding="utf-8",
    )

    return ModuleBuildPlan(
        main_c=main_c,
        runtime_c=runtime_c,
        c_files=[runtime_c, main_c, *(Path(unit.c_path) for unit in module_units.values() if unit.c_path)],
        libs=[Path(unit.lib_path) for unit in module_units.values() if unit.lib_path],
        dlls=[Path(unit.dll_path) for unit in module_units.values() if unit.dll_path],
        link_libs=_dedupe_link_libs(
            [lib.library for lib in checked.c_libs.values()]
            + _builtin_link_libs(checked.uses, target, runtime_features)
            + [lib for unit in module_units.values() for lib in unit.link_libs]
        ),
        headers=[runtime_h, *(Path(unit.h_path) for unit in module_units.values())],
        modules=module_units,
        runtime_features=runtime_features,
    )


def _runtime_impl(root_texts: list[str], module_units: dict[str, ModuleUnit], features: set[str]) -> str:
    """模块模式下 sa_runtime.c 的实现部分。

    根符号取全项目并集——主程序加每个模块的 .c。少扫一个 TU，链接期就会
    冒出 undefined reference。

    模块来自预编译的 .a / .dll 时（.slib 二进制包）没有 c_path，它内部调了哪些
    sa_* 我们看不到，只能退回全量。少注入会链接失败，多注入只是白搭点体积，
    这里必须往保守一侧倒。
    """
    if any(not unit.c_path for unit in module_units.values()):
        return RUNTIME_SOURCE.strip()

    roots: set[str] = set()
    for text in root_texts:
        roots |= runtime_symbols_in(text)
    for unit in module_units.values():
        roots |= runtime_symbols_in(Path(unit.c_path).read_text(encoding="utf-8"))
    # RUNTIME_SOURCE 就是 RUNTIME_IMPL 去掉 static，这里对切片做同样处理：
    # 模块模式下 runtime 是独立 TU，符号必须能跨 TU 链接。
    return runtime_impl_for(roots, features).replace("static ", "").strip()


def rewrite_runtime_for_native(plan: ModuleBuildPlan, ir_text: str) -> None:
    """native + 用户模块时按 IR 重算 runtime 切片。

    这条路径下主程序是 .ll 而不是 main.c，两个后端对 runtime 的调用面并不完全
    重合。仍按 main.c 的符号裁剪就会漏掉只有 IR 用到的函数，链接期才炸。
    """
    if plan.runtime_c is None:
        return
    plan.runtime_c.write_text(
        _runtime_feature_prefix(plan.runtime_features)
        + '#include "sa_runtime.h"\n\n'
        + _runtime_impl([ir_text], plan.modules, plan.runtime_features)
        + "\n",
        encoding="utf-8",
    )


def _extract_spkgs(spkgs: list[Path], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    dirs: list[Path] = []
    seen: set[str] = set()
    for path in spkgs:
        resolved = str(path.resolve()).lower()
        if resolved in seen:
            continue
        seen.add(resolved)
        extract_dir = spkg_extract_dir(out_dir, path)
        # 增量构建里旧版本包的残留文件会被当成合法模块源继续参与编译，先清干净。
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_spkg(path, extract_dir)
        dirs.append(extract_dir)
    return dirs


def compile_module(
    module: str,
    alias: str,
    source_root: Path,
    out_dir: Path,
    module_units: dict[str, ModuleUnit],
    target: str | None = None,
    dynamic: bool = False,
    spkg_dirs: list[Path] | None = None,
    module_stack: list[str] | None = None,
) -> ModuleUnit:
    key = module.lower()
    if key in module_units:
        return module_units[key]
    module_stack = module_stack or []
    if any(item.lower() == key for item in module_stack):
        raise module_cycle_error(module_stack, module)
    module_stack.append(module)

    try:
        spkg_dirs = spkg_dirs or []
        source_path = module_path_to_source(source_root, module)
        dep_source_root = source_path.parent
        package_scope: str | None = None
        if not source_path.exists():
            slib_path = module_path_to_slib(source_root, module)
            if slib_path.exists():
                from .slib import load_slib

                return load_slib(slib_path, out_dir, module_units, target, expected_module=module)

            spkg_source, package_scope = _find_in_spkgs(module, spkg_dirs)
            if spkg_source is not None:
                source_path = spkg_source
                dep_source_root = source_path.parent

            if not source_path.exists():
                raise SonCompileError(f"找不到模块源文件、.slib 或 .spkg: {module}")

        _reject_module_name_collision(module, module_units)

        program = parse_program(source_path.read_text(encoding="utf-8-sig"))
        dependency_exports: dict[str, ModuleExports] = {}
        for use in program.uses:
            if use.module in BUILTIN_MODULES:
                continue
            dep_module = _qualify_in_package(use.module, package_scope, spkg_dirs)
            dep = compile_module(dep_module, use.alias, dep_source_root, out_dir, module_units, target, dynamic, spkg_dirs, module_stack)
            dependency_exports[use.alias.lower()] = dep.exports

        checked = check_program(program, external_modules=dependency_exports, require_main=False)
        exports = collect_exports(module, program)
        header_path = out_dir / exports.header_name
        c_path = out_dir / f"sa_user_{module_c_name(module)}.c"

        header_path.write_text(generate_header(exports, dynamic=dynamic), encoding="utf-8")
        body = CGen(
            checked,
            module_name=module,
            include_runtime=False,
            include_main=False,
            include_headers=[dep.exports.header_name for dep in module_units.values() if dep.exports.module.lower() != key],
            dynamic=dynamic,
        ).generate()
        c_path.write_text(body, encoding="utf-8")

        runtime_features = runtime_features_for_program(checked.program, checked.uses)
        unit = ModuleUnit(
            module=module,
            source_path=str(source_path),
            c_path=str(c_path),
            h_path=str(header_path),
            link_libs=_dedupe_link_libs([lib.library for lib in checked.c_libs.values()] + _builtin_link_libs(checked.uses, target, runtime_features)),
            runtime_features=sorted(runtime_features),
            exports=exports,
        )
        module_units[key] = unit
        return unit
    finally:
        module_stack.pop()


def _spkg_manifests(spkg_dirs: list[Path]) -> list[tuple[Path, dict]]:
    result: list[tuple[Path, dict]] = []
    for spkg_dir in spkg_dirs:
        manifest_path = spkg_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        result.append((spkg_dir, json.loads(manifest_path.read_text(encoding="utf-8"))))
    return result


def _find_in_spkgs(module: str, spkg_dirs: list[Path]) -> tuple[Path | None, str | None]:
    """在已解包的 .spkg 里找模块源码，同时返回它所属的包名。

    包名要带出来：包内模块之间互相 USE 时写的是短名，得靠它拼回 `PKG.MOD`。
    """
    for spkg_dir, manifest in _spkg_manifests(spkg_dirs):
        spkg_source = spkg_module_source(manifest, module, spkg_dir)
        if spkg_source is not None and spkg_source.exists():
            return spkg_source, (manifest.get("module_to_package") or {}).get(module)
    return None, None


def _qualify_in_package(module: str, package_scope: str | None, spkg_dirs: list[Path]) -> str:
    """包内模块的 `USE UTIL` 应该指向同包的 `PKG.UTIL`。

    不补包名的话，解析会先在解包后的 src 目录里按文件名撞到 util.sa，于是同一份
    源码被编译成 UTIL 和 PKG.UTIL 两个独立模块：状态两份、类型互不兼容。
    """
    if not package_scope:
        return module
    qualified = f"{package_scope.upper()}.{module}"
    for _, manifest in _spkg_manifests(spkg_dirs):
        if qualified in (manifest.get("module_to_package") or {}):
            return qualified
    return module


def _reject_module_name_collision(module: str, module_units: dict[str, ModuleUnit]) -> None:
    """A.B 和 A_B 归一化后都是 a_b：C 文件、头文件、符号前缀会互相覆盖。"""
    c_name = module_c_name(module)
    key = module.lower()
    for other in module_units.values():
        if other.module.lower() != key and module_c_name(other.module) == c_name:
            raise SonCompileError(
                f"模块名 {module} 与 {other.module} 归一化后都是 `{c_name}`，"
                "生成的 C 文件和符号前缀会互相覆盖；请改掉其中一个模块名"
            )


def _dedupe_link_libs(libs: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for lib in libs:
        if lib in seen:
            continue
        seen.add(lib)
        result.append(lib)
    return result


def _builtin_link_libs(uses: dict[str, str], target: str | None = None, features: set[str] | None = None) -> list[str]:
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


def _runtime_feature_prefix(features: set[str]) -> str:
    macros = {
        "net": "SA_ENABLE_NET",
        "tls": "SA_ENABLE_TLS",
        "file": "SA_ENABLE_FILE",
        "desktop": "SA_ENABLE_DESKTOP",
        "binary": "SA_ENABLE_BINARY",
        "list": "SA_ENABLE_LIST",
        "map": "SA_ENABLE_MAP",
        "gui": "SA_ENABLE_GUI",
    }
    lines = [f"#define {macros[feature]}" for feature in sorted(features) if feature in macros]
    return "" if not lines else "\n".join(lines) + "\n"
