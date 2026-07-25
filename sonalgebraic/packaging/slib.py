from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

from ..core.errors import SonCompileError
from ..analysis.exports import collect_exports
from ..analysis.typesys import runtime_features_for_program
from ..backend.c_runtime import RUNTIME_HEADER
from .module_compiler import compile_module
from ..core.module_model import ModuleUnit
from ..core.names import module_c_name
from ..frontend.parser import parse_program
from .toolchain import (
    compile_dynamic_library,
    compile_static_library,
    dynamic_import_lib_name,
    dynamic_lib_name,
    normalize_target,
    static_lib_name,
)

SLIB_FORMAT = "sonalgebraic-slib"
SLIB_VERSION = 2


def build_slib(
    source_path: Path,
    output_path: Path,
    module_name: str | None = None,
    binary: bool = False,
    dynamic: bool = False,
    target: str | None = None,
) -> Path:
    if binary and dynamic:
        raise SonCompileError("--binary 和 --dynamic 不能同时使用")
    module = (module_name or source_path.stem).upper()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(prefix="sonalgebraic-slib-") as temp:
        temp_dir = Path(temp)
        (temp_dir / "sa_runtime.h").write_text(RUNTIME_HEADER.strip() + "\n", encoding="utf-8")
        units: dict[str, ModuleUnit] = {}
        target_name = normalize_target(target)
        root = compile_module(module, module, source_path.parent, temp_dir, units, target_name, dynamic)
        runtime_features = sorted({feature for unit in units.values() for feature in unit.runtime_features})
        if dynamic and runtime_features:
            joined = ", ".join(runtime_features)
            raise SonCompileError(
                f"动态 .slib 暂不支持进程内 runtime 状态功能: {joined}；请改用源码或静态 .slib"
            )
        archives: dict[str, dict[str, str]] = {}
        binary_result = None
        if binary:
            c_paths = [Path(unit.c_path) for unit in units.values() if unit.c_path]
            lib_path = temp_dir / static_lib_name(root.module, target_name)
            binary_result = compile_static_library(c_paths, temp_dir, lib_path, target_name)
            archives = archive_manifest_static(binary_result.lib_path, target_name)
        elif dynamic:
            c_paths = [Path(unit.c_path) for unit in units.values() if unit.c_path]
            lib_dir = temp_dir / "lib" / target_name
            link_libs = dedupe_link_libs([lib for unit in units.values() for lib in unit.link_libs])
            binary_result = compile_dynamic_library(c_paths, temp_dir, lib_dir, root.module, target_name, link_libs)
            archives = archive_manifest_dynamic(binary_result, target_name)
        manifest = {
            "format": SLIB_FORMAT,
            "version": SLIB_VERSION,
            "root_module": root.module,
            "kind": "dynamic" if dynamic else ("static" if binary else "source"),
            "target": target_name if (binary or dynamic) else None,
            "units": [unit_manifest(unit) for unit in units.values()],
            "archives": archives,
        }

        with ZipFile(output_path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            for unit in units.values():
                archive.write(unit.source_path, f"sources/{source_archive_name(unit.module)}")
                archive.write(unit.c_path, f"c/{Path(unit.c_path).name}")
                archive.write(unit.h_path, f"include/{Path(unit.h_path).name}")
            if binary_result is not None:
                if dynamic:
                    archive.write(binary_result.dll_path, archive_entry(binary_result.dll_path, target_name))
                    if getattr(binary_result, "import_lib", None):
                        archive.write(binary_result.import_lib, archive_entry(binary_result.import_lib, target_name))
                else:
                    archive.write(binary_result.lib_path, archive_entry(binary_result.lib_path, target_name))

    return output_path


def load_slib(slib_path: Path, out_dir: Path, module_units: dict[str, ModuleUnit], target: str | None = None) -> ModuleUnit:
    with ZipFile(slib_path) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        if manifest.get("format") != SLIB_FORMAT or manifest.get("version") not in {1, SLIB_VERSION}:
            raise ValueError(f"不支持的 .slib 格式: {slib_path}")

        root_key = manifest["root_module"].lower()
        requested_target = normalize_target(target)
        archives = manifest.get("archives") or {}
        archive_info = archives.get(requested_target)
        for item in manifest["units"]:
            module = item["module"]
            key = module.lower()
            if key in module_units:
                continue

            source_text = archive.read(item["source_entry"]).decode("utf-8-sig")
            source_path = out_dir / Path(item["source_entry"]).name
            c_path = out_dir / Path(item["c_entry"]).name
            h_path = out_dir / Path(item["h_entry"]).name
            lib_path = None
            dll_path = None
            source_path.write_text(source_text, encoding="utf-8")
            if archive_info and key == root_key:
                c_path = None
                if archive_info["kind"] == "dynamic":
                    dll_path = out_dir / Path(archive_info["dll"]).name
                    dll_path.write_bytes(archive.read(archive_info["dll"]))
                    if archive_info.get("import_lib"):
                        import_lib_path = out_dir / Path(archive_info["import_lib"]).name
                        import_lib_path.write_bytes(archive.read(archive_info["import_lib"]))
                        lib_path = import_lib_path
                    else:
                        lib_path = dll_path
                else:
                    lib_path = out_dir / Path(archive_info["entry"]).name
                    lib_path.write_bytes(archive.read(archive_info["entry"]))
            else:
                c_path.write_text(archive.read(item["c_entry"]).decode("utf-8"), encoding="utf-8")
            h_path.write_text(archive.read(item["h_entry"]).decode("utf-8"), encoding="utf-8")

            program = parse_program(source_text)
            exports = collect_exports(module, program)
            uses = {use.alias.lower(): use.module for use in program.uses}
            runtime_features = item.get("runtime_features") or sorted(runtime_features_for_program(program, uses))
            module_units[key] = ModuleUnit(
                module=module,
                source_path=str(source_path),
                c_path=str(c_path) if c_path is not None else None,
                h_path=str(h_path),
                lib_path=str(lib_path) if lib_path is not None else None,
                dll_path=str(dll_path) if dll_path is not None else None,
                target=requested_target if lib_path is not None else None,
                link_libs=[lib.library for lib in program.uselibs] + builtin_link_libs_for_features(runtime_features, requested_target),
                runtime_features=runtime_features,
                exports=exports,
            )

        return module_units[root_key]


def unit_manifest(unit: ModuleUnit) -> dict[str, object]:
    return {
        "module": unit.module,
        "source_entry": f"sources/{source_archive_name(unit.module)}",
        "c_entry": f"c/{Path(unit.c_path).name}",
        "h_entry": f"include/{Path(unit.h_path).name}",
        "runtime_features": unit.runtime_features,
    }


def source_archive_name(module: str) -> str:
    return f"{module_c_name(module)}.sa"


def archive_entry(path: Path, target: str) -> str:
    return f"lib/{target}/{path.name}"


def archive_manifest_static(lib_path: Path, target: str) -> dict[str, dict[str, str]]:
    return {target: {"kind": "static", "entry": archive_entry(lib_path, target)}}


def archive_manifest_dynamic(result, target: str) -> dict[str, dict[str, str]]:
    entry: dict[str, str] = {"kind": "dynamic", "dll": archive_entry(result.dll_path, target)}
    if result.import_lib:
        entry["import_lib"] = archive_entry(result.import_lib, target)
    return {target: entry}


def dedupe_link_libs(libs: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for lib in libs:
        if lib in seen:
            continue
        seen.add(lib)
        result.append(lib)
    return result


def builtin_link_libs_for_features(features: list[str], target: str) -> list[str]:
    result: list[str] = []
    if "windows" in target and "net" in features:
        result.extend(["winhttp", "ws2_32"])
    if "tls" in features:
        if "windows" in target:
            result.append("secur32")
        else:
            result.extend(["ssl", "crypto"])
    if "windows" in target and "desktop" in features:
        result.extend(["user32", "shell32"])
    return result
