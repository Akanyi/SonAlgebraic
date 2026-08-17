from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

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
    normalize_target,
    static_lib_name,
)

SLIB_FORMAT = "sonalgebraic-slib"
SLIB_VERSION = 3

# v3 起 manifest 带 hashes 清单；1/2 是存量包，读得进但没有完整性保证。
SUPPORTED_SLIB_VERSIONS = {1, 2, SLIB_VERSION}
HASHED_SLIB_VERSION = 3


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

        # 依赖解析到二进制 .slib 时该单元没有 C 源码，只有一份预编译产物。
        prebuilt = [unit for unit in units.values() if unit.lib_path or unit.dll_path]
        if dynamic and prebuilt:
            joined = ", ".join(sorted(unit.module for unit in prebuilt))
            raise SonCompileError(
                f"动态 .slib 暂不支持依赖二进制 .slib: {joined}；"
                "把依赖的实现吸收进 DLL 会让它的模块级状态在 DLL 内外各存一份，"
                "请改用源码 .slib 依赖，或把本模块也打成源码/静态 .slib"
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

        entries = _collect_archive_entries(units, binary_result, dynamic, target_name)
        manifest["hashes"] = {name: _sha256_bytes(path.read_bytes()) for name, path in entries.items()}

        with ZipFile(output_path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            for name, path in entries.items():
                archive.write(path, name)

    return output_path


def _collect_archive_entries(
    units: dict[str, ModuleUnit],
    binary_result,
    dynamic: bool,
    target: str,
) -> dict[str, Path]:
    """列出要写进 zip 的 (包内条目 -> 磁盘文件)。

    先收齐再写，是因为 manifest 里的 hashes 需要所有成员的摘要，而 manifest
    本身也要写进同一个 zip。
    """
    entries: dict[str, Path] = {}

    def add(name: str, path: Path) -> None:
        existing = entries.get(name)
        if existing is not None and existing != path:
            raise SonCompileError(f".slib 内部条目冲突: {name}（{existing} 与 {path}）")
        entries[name] = path

    for unit in units.values():
        add(f"sources/{source_archive_name(unit.module)}", Path(unit.source_path))
        if unit.c_path:
            add(f"c/{Path(unit.c_path).name}", Path(unit.c_path))
        add(f"include/{Path(unit.h_path).name}", Path(unit.h_path))
        for name, path in _prebuilt_entries(unit).items():
            add(name, path)

    if binary_result is not None:
        if dynamic:
            add(archive_entry(binary_result.dll_path, target), binary_result.dll_path)
            if getattr(binary_result, "import_lib", None):
                add(archive_entry(binary_result.import_lib, target), binary_result.import_lib)
        else:
            add(archive_entry(binary_result.lib_path, target), binary_result.lib_path)
    return entries


def _prebuilt_entries(unit: ModuleUnit) -> dict[str, Path]:
    """依赖二进制 .slib 带来的产物在外层包里的落位。"""
    if not unit.target:
        return {}
    result: dict[str, Path] = {}
    if unit.dll_path:
        dll = Path(unit.dll_path)
        result[archive_entry(dll, unit.target)] = dll
    if unit.lib_path and unit.lib_path != unit.dll_path:
        lib = Path(unit.lib_path)
        result[archive_entry(lib, unit.target)] = lib
    return result


def load_slib(
    slib_path: Path,
    out_dir: Path,
    module_units: dict[str, ModuleUnit],
    target: str | None = None,
    expected_module: str | None = None,
) -> ModuleUnit:
    try:
        return _load_slib(slib_path, out_dir, module_units, target, expected_module)
    except (BadZipFile, KeyError, ValueError, TypeError) as exc:
        # 损坏包会从 zipfile/json/manifest 取值处抛各种原生异常，CLI 只认
        # SonCompileError，不转换的话用户看到的是 Python traceback。
        raise SonCompileError(f".slib 内容损坏或格式不正确: {slib_path}（{exc}）") from exc


def _load_slib(
    slib_path: Path,
    out_dir: Path,
    module_units: dict[str, ModuleUnit],
    target: str | None,
    expected_module: str | None,
) -> ModuleUnit:
    with ZipFile(slib_path) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        if manifest.get("format") != SLIB_FORMAT or manifest.get("version") not in SUPPORTED_SLIB_VERSIONS:
            raise SonCompileError(f"不支持的 .slib 格式: {slib_path}")

        _verify_slib_hashes(archive, manifest, slib_path)

        root_module = manifest["root_module"]
        root_key = root_module.lower()
        # 按文件名命中 .slib 不等于包里就是那个模块：不查一下，`use foo` 可能
        # 悄悄链上 bar 的实现，而报错要等到链接期才出现。
        if expected_module is not None and expected_module.lower() != root_key:
            raise SonCompileError(
                f"{slib_path.name} 的根模块是 {root_module}，与请求的模块 {expected_module} 不一致；"
                "打包时用 --module 指定模块名，或把文件改成匹配的名字"
            )

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
            h_path = out_dir / Path(item["h_entry"]).name
            c_path = None
            lib_path = None
            dll_path = None
            source_path.write_text(source_text, encoding="utf-8")

            selected = archive_info if (archive_info and key == root_key) else (item.get("archives") or {}).get(requested_target)
            if selected:
                if selected["kind"] == "dynamic":
                    dll_path = out_dir / Path(selected["dll"]).name
                    dll_path.write_bytes(archive.read(selected["dll"]))
                    if selected.get("import_lib"):
                        import_lib_path = out_dir / Path(selected["import_lib"]).name
                        import_lib_path.write_bytes(archive.read(selected["import_lib"]))
                        lib_path = import_lib_path
                    else:
                        lib_path = dll_path
                else:
                    lib_path = out_dir / Path(selected["entry"]).name
                    lib_path.write_bytes(archive.read(selected["entry"]))
            elif item.get("c_entry"):
                c_path = out_dir / Path(item["c_entry"]).name
                c_path.write_text(archive.read(item["c_entry"]).decode("utf-8"), encoding="utf-8")
            else:
                # 只带二进制的单元，包里没有任何 target 能对上：没 C 源码也没库，无法继续。
                available = ", ".join(sorted(item.get("archives") or {})) or "无"
                raise SonCompileError(
                    f"{slib_path.name} 里的模块 {module} 只有预编译产物，"
                    f"缺少 target `{requested_target}` 的版本（包内可用: {available}）"
                )
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


def _sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _archive_entry_names(archives: object) -> set[str]:
    names: set[str] = set()
    if not isinstance(archives, dict):
        return names
    for info in archives.values():
        if not isinstance(info, dict):
            continue
        for field in ("entry", "dll", "import_lib"):
            value = info.get(field)
            if isinstance(value, str):
                names.add(value)
    return names


def _verify_slib_hashes(archive: ZipFile, manifest: dict, slib_path: Path) -> None:
    """校验包内成员摘要。

    语义检查读的是包里的 .sa，实际链接的却是包里的 .a/.dll，两边对不上时以前
    没人发现。清单挡不住「整份 manifest 被重写」（.slib 没有签名），但能挡住
    换掉某个成员这种局部篡改和传输损坏。
    """
    version = manifest.get("version")
    hashes = manifest.get("hashes")
    if not hashes:
        if isinstance(version, int) and version >= HASHED_SLIB_VERSION:
            raise SonCompileError(f".slib v{version} 必须带 hashes 清单: {slib_path}")
        # v1/v2 存量包没有这个字段，直接废掉会让老包全部编译不了，只警告。
        print(
            f"sonc: warning: {slib_path.name} 是 v{version} 旧格式 .slib，没有 hash 清单，跳过完整性校验",
            file=sys.stderr,
        )
        return
    if not isinstance(hashes, dict):
        raise SonCompileError(f".slib manifest.hashes 必须是对象: {slib_path}")

    for entry, expected in hashes.items():
        if not isinstance(entry, str) or not isinstance(expected, str):
            raise SonCompileError(f".slib manifest.hashes 包含非法项: {slib_path}")
        if not expected.lower().startswith("sha256:"):
            raise SonCompileError(f".slib 不支持的 hash 格式: {entry}")
        try:
            data = archive.read(entry)
        except KeyError as exc:
            raise SonCompileError(f".slib hash 条目对应的文件不存在: {entry}") from exc
        if _sha256_bytes(data) != expected.lower():
            raise SonCompileError(f".slib hash 校验失败: {entry}")

    # 反查：只验清单里写了的条目等于没验——删掉某条就能让对应文件零校验参与编译。
    required: set[str] = set()
    for item in manifest.get("units", []):
        if not isinstance(item, dict):
            continue
        for field in ("source_entry", "c_entry", "h_entry"):
            value = item.get(field)
            if isinstance(value, str):
                required.add(value)
        required |= _archive_entry_names(item.get("archives"))
    required |= _archive_entry_names(manifest.get("archives"))
    missing = sorted(required - set(hashes))
    if missing:
        raise SonCompileError(f".slib 缺少 hash 声明: {', '.join(missing)}")


def unit_manifest(unit: ModuleUnit) -> dict[str, object]:
    entry: dict[str, object] = {
        "module": unit.module,
        "source_entry": f"sources/{source_archive_name(unit.module)}",
        "h_entry": f"include/{Path(unit.h_path).name}",
        "runtime_features": unit.runtime_features,
    }
    # 来自二进制 .slib 的依赖没有 C 源码，只能把它的预编译产物挂在单元上带走。
    if unit.c_path:
        entry["c_entry"] = f"c/{Path(unit.c_path).name}"
    prebuilt = unit_archive_manifest(unit)
    if prebuilt:
        entry["archives"] = prebuilt
    return entry


def unit_archive_manifest(unit: ModuleUnit) -> dict[str, dict[str, str]]:
    if not unit.target or not (unit.lib_path or unit.dll_path):
        return {}
    if unit.dll_path:
        entry: dict[str, str] = {"kind": "dynamic", "dll": archive_entry(Path(unit.dll_path), unit.target)}
        if unit.lib_path and unit.lib_path != unit.dll_path:
            entry["import_lib"] = archive_entry(Path(unit.lib_path), unit.target)
        return {unit.target: entry}
    return {unit.target: {"kind": "static", "entry": archive_entry(Path(unit.lib_path), unit.target)}}


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
    if "windows" in target and "gui" in features:
        result.extend(["user32", "gdi32"])
    return result
