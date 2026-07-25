from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..backend.codegen import CGen
from ..backend.c_runtime import RUNTIME_HEADER, RUNTIME_SOURCE
from ..core.errors import SonCompileError, module_cycle_error
from ..analysis.exports import collect_exports
from ..analysis.typesys import BUILTIN_MODULES, runtime_features_for_program
from ..backend.headergen import generate_header
from ..core.module_model import ModuleExports, ModuleUnit
from ..core.names import module_path_to_slib, module_path_to_source, module_symbol_prefix
from ..frontend.parser import parse_program
from ..analysis.semantics import check_program
from .spkg import extract_spkg, spkg_module_source
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
    runtime_c.write_text(runtime_prefix + '#include "sa_runtime.h"\n\n' + RUNTIME_SOURCE.strip() + "\n", encoding="utf-8")

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
    )


def _extract_spkgs(spkgs: list[Path], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    dirs: list[Path] = []
    for path in spkgs:
        extract_dir = out_dir / path.stem
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
        if not source_path.exists():
            slib_path = module_path_to_slib(source_root, module)
            if slib_path.exists():
                from .slib import load_slib

                return load_slib(slib_path, out_dir, module_units, target)

            for spkg_dir in spkg_dirs:
                manifest_path = spkg_dir / "manifest.json"
                if not manifest_path.exists():
                    continue
                import json

                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                spkg_source = spkg_module_source(manifest, module, spkg_dir)
                if spkg_source is not None and spkg_source.exists():
                    source_path = spkg_source
                    dep_source_root = source_path.parent
                    break

            if not source_path.exists():
                raise SonCompileError(f"找不到模块源文件、.slib 或 .spkg: {module}")

        program = parse_program(source_path.read_text(encoding="utf-8-sig"))
        dependency_exports: dict[str, ModuleExports] = {}
        for use in program.uses:
            if use.module in BUILTIN_MODULES:
                continue
            dep = compile_module(use.module, use.alias, dep_source_root, out_dir, module_units, target, dynamic, spkg_dirs, module_stack)
            dependency_exports[use.alias.lower()] = dep.exports

        checked = check_program(program, external_modules=dependency_exports, require_main=False)
        exports = collect_exports(module, program)
        header_path = out_dir / exports.header_name
        c_path = out_dir / f"sa_user_{module.replace('.', '_').lower()}.c"

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
    }
    lines = [f"#define {macros[feature]}" for feature in sorted(features) if feature in macros]
    return "" if not lines else "\n".join(lines) + "\n"
