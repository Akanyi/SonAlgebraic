from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from zipfile import ZipFile, ZIP_DEFLATED

from ..core.errors import SonCompileError
from ..core.names import module_c_name


SPKG_FORMAT = "sonalgebraic-spkg"
SPKG_VERSION = 1


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return f"sha256:{h.hexdigest()}"


def _safe_member_path(out_dir: Path, member: str) -> Path:
    normalized = member.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if (
        not normalized
        or "\x00" in normalized
        or PurePosixPath(normalized).is_absolute()
        or any(part in {"", ".", ".."} or ":" in part for part in parts)
    ):
        raise SonCompileError(f".spkg 包含不安全路径: {member}")

    target = out_dir.joinpath(*parts)
    root = out_dir.resolve()
    try:
        target.resolve().relative_to(root)
    except ValueError as exc:
        raise SonCompileError(f".spkg 包含不安全路径: {member}") from exc
    return target


def _module_name_from_path(path: Path, package_name: str, src_dir: Path) -> str:
    relative = path.with_suffix("").relative_to(src_dir)
    if relative.name == "__init__":
        relative = relative.parent
    parts = [package_name.upper()] + [p.upper() for p in relative.parts]
    return ".".join(parts)


def pack_spkg(
    source: Path,
    output_path: Path,
    package_name: str | None = None,
    version: str = "0.1.0",
    description: str = "",
    author: str = "",
    license: str = "",
) -> Path:
    if source.is_file():
        if package_name is None:
            package_name = source.stem.lower()
    else:
        if package_name is None:
            package_name = source.name.lower()

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(prefix="sonalgebraic-spkg-") as temp:
        temp_dir = Path(temp)
        pkg_dir = temp_dir / "packages" / package_name
        src_dir = pkg_dir / "src"
        src_dir.mkdir(parents=True, exist_ok=True)

        if source.is_file():
            target = src_dir / "__init__.sa"
            target.write_bytes(source.read_bytes())
            sa_files = [target]
        else:
            source_dir = source
            sa_files = sorted(source_dir.rglob("*.sa"))
            if not sa_files:
                raise SonCompileError(f"在 {source_dir} 中找不到任何 .sa 源文件")
            for path in sa_files:
                rel = path.relative_to(source_dir)
                target = src_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())

        modules: list[dict] = []
        for path in sa_files:
            rel_in_pkg = path.relative_to(pkg_dir)
            module_name = _module_name_from_path(path, package_name, src_dir)
            modules.append(
                {
                    "name": module_name,
                    "package": package_name,
                    "source": str(rel_in_pkg).replace("\\", "/"),
                    "binaries": {},
                }
            )

        module_to_package = {module["name"]: package_name for module in modules}

        manifest = {
            "format": SPKG_FORMAT,
            "version": SPKG_VERSION,
            "package": {
                "name": package_name,
                "version": version,
                "description": description,
                "author": author,
                "license": license,
            },
            "bundled_packages": [
                {
                    "name": package_name,
                    "version": version,
                    "is_root": True,
                    "path": f"packages/{package_name}",
                    "artifacts": {
                        "source": True,
                        "binary": False,
                        "headers": False,
                        "targets": [],
                    },
                    "native_libs": [],
                }
            ],
            "modules": modules,
            "module_to_package": module_to_package,
            "dependency_graph": {},
            "hashes": {},
        }

        for path in sa_files:
            rel_in_pkg = path.relative_to(pkg_dir)
            manifest["hashes"][str(rel_in_pkg).replace("\\", "/")] = _sha256(path)

        with ZipFile(output_path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            for path in sa_files:
                rel_in_pkg = path.relative_to(pkg_dir)
                archive.write(path, f"packages/{package_name}/{rel_in_pkg.as_posix()}")

    return output_path


def load_spkg_manifest(spkg_path: Path) -> dict:
    with ZipFile(spkg_path, "r") as archive:
        return json.loads(archive.read("manifest.json").decode("utf-8"))


def extract_spkg(spkg_path: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    with ZipFile(spkg_path, "r") as archive:
        for info in archive.infolist():
            target = _safe_member_path(out_dir, info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))

    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        raise SonCompileError(f".spkg 缺少 manifest.json: {spkg_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _verify_hashes(manifest, out_dir)
    return manifest


def _verify_hashes(manifest: dict, out_dir: Path) -> None:
    hashes = manifest.get("hashes") or {}
    if not isinstance(hashes, dict):
        raise SonCompileError(".spkg manifest.hashes 必须是对象")
    for entry, expected in hashes.items():
        if not isinstance(entry, str) or not isinstance(expected, str):
            raise SonCompileError(".spkg manifest.hashes 包含非法项")
        if not expected.lower().startswith("sha256:"):
            raise SonCompileError(f".spkg 不支持的 hash 格式: {entry}")
        path = _hash_entry_path(manifest, out_dir, entry)
        if not path.exists() or not path.is_file():
            raise SonCompileError(f".spkg hash 文件不存在: {entry}")
        actual = _sha256(path)
        if actual.lower() != expected.lower():
            raise SonCompileError(f".spkg hash 校验失败: {entry}")


def _hash_entry_path(manifest: dict, out_dir: Path, entry: str) -> Path:
    if entry.replace("\\", "/").startswith("packages/"):
        return _safe_member_path(out_dir, entry)

    direct = _safe_member_path(out_dir, entry)
    package_name = (manifest.get("package") or {}).get("name")
    if isinstance(package_name, str) and package_name:
        packaged = _safe_member_path(out_dir, f"packages/{package_name}/{entry}")
        if packaged.exists():
            return packaged
    if direct.exists():
        return direct
    return direct


def spkg_module_source(manifest: dict, module: str, extract_dir: Path) -> Path | None:
    module_to_package = manifest.get("module_to_package", {})
    package_name = module_to_package.get(module)
    if package_name is None:
        return None

    for item in manifest.get("modules", []):
        if item["name"] == module and item["package"] == package_name:
            source_entry = item.get("source")
            if source_entry:
                if source_entry.replace("\\", "/").startswith("packages/"):
                    return _safe_member_path(extract_dir, source_entry)
                return _safe_member_path(extract_dir, f"packages/{package_name}/{source_entry}")
    return None
