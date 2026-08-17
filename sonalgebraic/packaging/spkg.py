from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED

from ..core.errors import SonCompileError


SPKG_FORMAT = "sonalgebraic-spkg"
SPKG_VERSION = 1
SUPPORTED_SPKG_VERSIONS = {SPKG_VERSION}

_WINDOWS_DEVICE_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return f"sha256:{h.hexdigest()}"


def _is_windows_device_name(part: str) -> bool:
    """CON / NUL / COM1 这类保留名在 Windows 上不是文件：写进去会打到设备而不是磁盘。

    判定要去掉扩展名，因为 `NUL.sa` 同样命中设备。
    """
    stem = part.split(".")[0].upper().rstrip(" ")
    return stem in _WINDOWS_DEVICE_NAMES


def _safe_member_path(out_dir: Path, member: str) -> Path:
    normalized = member.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if (
        not normalized
        or "\x00" in normalized
        or PurePosixPath(normalized).is_absolute()
        or any(part in {"", ".", ".."} or ":" in part for part in parts)
        or any(_is_windows_device_name(part) for part in parts)
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
            found = sorted(source_dir.rglob("*.sa"))
            if not found:
                raise SonCompileError(f"在 {source_dir} 中找不到任何 .sa 源文件")
            # 后续的 relative_to(pkg_dir) 和模块命名都以包内路径为准，
            # 所以这里必须记录拷贝后的目标路径，而不是用户源目录里的原始路径。
            sa_files = []
            for path in found:
                rel = path.relative_to(source_dir)
                target = src_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
                sa_files.append(target)

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


def spkg_extract_dir(out_dir: Path, spkg_path: Path) -> Path:
    """一个 .spkg 的专属解包目录。

    只用 stem 命名会让 a/lib.spkg 和 b/lib.spkg 解到同一个坑里互相覆盖，
    所以拼上完整路径的摘要。
    """
    digest = hashlib.sha256(str(spkg_path.resolve()).lower().encode("utf-8")).hexdigest()[:12]
    return out_dir / f"{spkg_path.stem}-{digest}"


def extract_spkg(spkg_path: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with ZipFile(spkg_path, "r") as archive:
            # 先读 manifest 校验格式再落盘：格式对不上的东西没必要往用户磁盘上铺。
            manifest = _read_manifest(archive, spkg_path)
            for info in archive.infolist():
                target = _safe_member_path(out_dir, info.filename)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
    except BadZipFile as exc:
        raise SonCompileError(f".spkg 不是合法的 zip 包: {spkg_path}（{exc}）") from exc

    _verify_hashes(manifest, out_dir)
    return manifest


def _read_manifest(archive: ZipFile, spkg_path: Path) -> dict:
    try:
        raw = archive.read("manifest.json")
    except KeyError as exc:
        raise SonCompileError(f".spkg 缺少 manifest.json: {spkg_path}") from exc
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SonCompileError(f".spkg manifest.json 不是合法 JSON: {spkg_path}") from exc
    if not isinstance(manifest, dict):
        raise SonCompileError(f".spkg manifest.json 必须是对象: {spkg_path}")
    # .slib 侧一直在查 format/version，.spkg 这边漏了，随便一个 zip 都能被当成包解。
    if manifest.get("format") != SPKG_FORMAT:
        raise SonCompileError(f"不支持的 .spkg 格式: {spkg_path}（format={manifest.get('format')!r}）")
    if manifest.get("version") not in SUPPORTED_SPKG_VERSIONS:
        raise SonCompileError(f"不支持的 .spkg 版本: {spkg_path}（version={manifest.get('version')!r}）")
    return manifest


def _verify_hashes(manifest: dict, out_dir: Path) -> None:
    hashes = manifest.get("hashes") or {}
    if not isinstance(hashes, dict):
        raise SonCompileError(".spkg manifest.hashes 必须是对象")
    verified: set[Path] = set()
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
        verified.add(path.resolve())

    # 只验 manifest 自己声明的条目是不够的：省掉某个条目（甚至把 hashes 留空）
    # 就能让对应模块源码零校验地参与编译。这里反查每个会被真正编译的源文件。
    for item in manifest.get("modules", []):
        source_entry = item.get("source")
        if not source_entry:
            continue
        source_path = _hash_entry_path(manifest, out_dir, source_entry)
        if source_path.resolve() not in verified:
            raise SonCompileError(f".spkg 模块源码缺少 hash 声明: {item.get('name', source_entry)}")


def _hash_entry_path(manifest: dict, out_dir: Path, entry: str) -> Path:
    """条目名 -> 解包目录里的路径，纯字符串规则，不看磁盘。

    以前会先试 packages/<pkg>/<entry> 再试 <entry>，哪个文件存在就算哪个：
    同一个条目名在「有没有那个文件」两种情况下指向不同对象，反查覆盖率时会漏。
    """
    normalized = entry.replace("\\", "/")
    if normalized.startswith("packages/"):
        return _safe_member_path(out_dir, normalized)
    package_name = (manifest.get("package") or {}).get("name")
    if isinstance(package_name, str) and package_name:
        return _safe_member_path(out_dir, f"packages/{package_name}/{normalized}")
    return _safe_member_path(out_dir, normalized)


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
