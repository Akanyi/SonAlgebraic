"""一键构建 SADK 安装包：PyInstaller 冻结 sonc，再交给 Inno Setup 打成 exe。

    python installer/build_installer.py                # 在线版，zig 安装时下载
    python installer/build_installer.py --bundle-zig    # 离线版，zig 打进安装包

产物在 build/installer/SADK-Setup-<version>[-with-zig].exe。
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request

INSTALLER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = INSTALLER_DIR.parent
BUILD_DIR = PROJECT_ROOT / "build"
DIST_DIR = BUILD_DIR / "sadk-dist"
WORK_DIR = BUILD_DIR / "pyi-work"
OUTPUT_DIR = BUILD_DIR / "installer"
# 下载的 zig 归档缓存在这里。97MB 一次就够，重复构建不该反复拉。
# 解压结果单独放，别和归档混在一个目录：CI 只缓存这份 zip，解压出来的
# 三百多 MB 目录树每次重新展开就好，缓存里没必要背着它。
ZIG_CACHE_DIR = BUILD_DIR / "zig-cache"
ZIG_EXTRACT_DIR = BUILD_DIR / "zig-extract"

# winget 装的 Inno Setup 落在用户目录，官网安装包默认落在 Program Files，两边都试
ISCC_CANDIDATES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
)


def project_version() -> str:
    """从 sonalgebraic/__init__.py 读版本，避免安装包版本和编译器版本各说各话。"""
    text = (PROJECT_ROOT / "sonalgebraic" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not match:
        raise SystemExit("无法从 sonalgebraic/__init__.py 解析 __version__")
    return match.group(1)


def find_iscc() -> Path:
    found = shutil.which("ISCC")
    if found:
        return Path(found)
    for candidate in ISCC_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "找不到 Inno Setup 编译器 ISCC.exe。\n"
        "安装方式：winget install --id JRSoftware.InnoSetup"
    )


def zig_release(arch: str = "X64") -> dict[str, str]:
    """从 sadk.iss 读 zig 的下载地址、摘要和大小。

    这三个值只在 .iss 里定义一次。让构建脚本自己再抄一份是坑：升级 zig 时改了一处
    忘了另一处，在线版和离线版就会拿到不同的编译器，而且不到安装那一刻发现不了。
    """
    text = (INSTALLER_DIR / "sadk.iss").read_text(encoding="utf-8")
    fields = {}
    for key, pattern in (
        ("url", rf'#define\s+ZigUrl{arch}\s+"([^"]+)"'),
        ("hash", rf'#define\s+ZigHash{arch}\s+"([^"]+)"'),
        ("size", rf"#define\s+ZigSize{arch}\s+(\d+)"),
    ):
        match = re.search(pattern, text)
        if not match:
            raise SystemExit(f"无法从 sadk.iss 解析 Zig{key.capitalize()}{arch}")
        fields[key] = match.group(1)
    version = re.search(r'#define\s+ZigVersion\s+"([^"]+)"', text)
    fields["version"] = version.group(1) if version else "unknown"
    return fields


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_zig(release: dict[str, str]) -> Path:
    """下载并校验 zig 归档，返回本地路径。命中缓存则直接复用。"""
    ZIG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    archive = ZIG_CACHE_DIR / Path(release["url"]).name
    expected = release["hash"].lower()

    if archive.is_file():
        actual = _sha256(archive)
        if actual == expected:
            print(f"    命中缓存: {archive.name}  ({archive.stat().st_size / 1048576:.1f} MB)")
            return archive
        # 缓存坏了就重下，别拿一个摘要不对的编译器往安装包里塞
        print(f"    缓存摘要不符，重新下载\n      期望 {expected}\n      实际 {actual}")
        archive.unlink()

    print(f"\n>>> 下载 Zig {release['version']}\n    {release['url']}", flush=True)
    partial = archive.with_suffix(archive.suffix + ".part")
    try:
        with urllib.request.urlopen(release["url"]) as response, partial.open("wb") as out:
            total = int(release["size"])
            done = 0
            while chunk := response.read(1024 * 256):
                out.write(chunk)
                done += len(chunk)
                print(f"\r    {done / 1048576:6.1f} / {total / 1048576:.1f} MB", end="", flush=True)
        print()
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise SystemExit(f"下载 Zig 失败: {exc}") from exc

    actual = _sha256(partial)
    if actual != expected:
        partial.unlink(missing_ok=True)
        raise SystemExit(
            f"Zig 归档摘要校验失败，已丢弃。\n  期望 {expected}\n  实际 {actual}\n"
            "sadk.iss 里的 ZigHash 可能没跟着 ZigUrl 一起更新。"
        )
    partial.replace(archive)
    print(f"    摘要校验通过: {archive.name}")
    return archive


def extract_zig(archive: Path) -> Path:
    """解压 zig 归档，返回解压出来的顶层目录。

    Inno 的 extractarchive 标志只能配 external 用，也就是它只会解压安装时下载的东西，
    没法把包内的 zip 展开。所以离线版得在构建时就解压好，按目录树交给 [Files]——
    顺带让 Inno 的 lzma2 接手压缩，比原样塞进一个 deflate 的 zip 更省。
    """
    # zip 内已含 zig-x86_64-windows-<ver>/ 这层，解压到目标根目录即可
    target = ZIG_EXTRACT_DIR / archive.stem
    probe = target / "zig.exe"
    if probe.is_file():
        print(f"    复用已解压的 {target.name}")
        return target

    print(f"\n>>> 解压 {archive.name}", flush=True)
    shutil.rmtree(target, ignore_errors=True)
    ZIG_EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.unpack_archive(archive, ZIG_EXTRACT_DIR)
    if not probe.is_file():
        raise SystemExit(
            f"解压后找不到 {probe}。\n"
            "zig 官方 zip 的顶层目录名可能变了，构建脚本对目录名的假设需要跟着改。"
        )
    total = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    print(f"    {target.name}  ({total / 1048576:.1f} MB)")
    return target


def run(command: list[str], step: str) -> None:
    print(f"\n>>> {step}\n    {' '.join(command)}", flush=True)
    result = subprocess.run(command, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        raise SystemExit(f"{step} 失败（退出码 {result.returncode}）")


def build_icon() -> None:
    sys.path.insert(0, str(INSTALLER_DIR))
    from make_icon import build_ico

    build_ico(INSTALLER_DIR / "assets" / "sadk.ico")


def freeze_compiler(clean: bool) -> None:
    if clean:
        shutil.rmtree(DIST_DIR, ignore_errors=True)
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    run(
        [
            sys.executable, "-m", "PyInstaller",
            str(INSTALLER_DIR / "sonc.spec"),
            "--distpath", str(DIST_DIR),
            "--workpath", str(WORK_DIR),
            "--noconfirm",
        ],
        "PyInstaller 冻结 sonc",
    )

    exe = DIST_DIR / "sonc" / "sonc.exe"
    if not exe.is_file():
        raise SystemExit(f"PyInstaller 没有产出 {exe}")
    # 冻结后立刻自检一次：spec 里的 excludes 一旦排过头，是启动即崩而不是编译期报错，
    # 等打完 100MB 安装包再发现就太晚了
    probe = subprocess.run([str(exe), "--version"], capture_output=True, text=True)
    if probe.returncode != 0:
        raise SystemExit(f"冻结产物无法启动:\n{probe.stdout}{probe.stderr}")
    print(f"    冻结产物自检通过: {probe.stdout.strip()}")


def build_installer(version: str, zig_dir: Path | None) -> Path:
    iscc = find_iscc()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    command = [str(iscc), f"/DSadkVersion={version}"]
    if zig_dir is not None:
        # 目录名要和在线版解压出来的一致，两种包装完的 toolchain/ 布局才一样
        command += [f"/DBundleZigDir={zig_dir}", f"/DBundleZigName={zig_dir.name}"]
    command.append(str(INSTALLER_DIR / "sadk.iss"))
    run(command, "Inno Setup 打包")

    suffix = "-with-zig" if zig_dir is not None else ""
    setup = OUTPUT_DIR / f"SADK-Setup-{version}{suffix}.exe"
    if not setup.is_file():
        raise SystemExit(f"Inno Setup 没有产出 {setup}")
    return setup


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 SADK 安装包")
    parser.add_argument("--skip-freeze", action="store_true", help="复用上次的 PyInstaller 产物，只重打安装包")
    parser.add_argument("--clean", action="store_true", help="冻结前清空 PyInstaller 的 dist / work 目录")
    parser.add_argument(
        "--bundle-zig",
        action="store_true",
        help="把 x64 的 Zig 工具链打进安装包，产出可离线安装的 -with-zig 版本（约 72 MB）",
    )
    args = parser.parse_args()

    version = project_version()
    print(f"SADK {version}")

    zig_dir = None
    if args.bundle_zig:
        # 先把 97MB 拿到手并解压好再去冻结：下载失败的话，没必要白跑一遍 PyInstaller
        zig_dir = extract_zig(fetch_zig(zig_release("X64")))

    build_icon()
    if args.skip_freeze:
        if not (DIST_DIR / "sonc" / "sonc.exe").is_file():
            raise SystemExit("--skip-freeze 需要上次的冻结产物，但没找到；去掉这个参数重跑")
        print("\n>>> 跳过冻结，复用已有产物")
    else:
        freeze_compiler(args.clean)

    setup = build_installer(version, zig_dir)
    print(f"\n完成: {setup}  ({setup.stat().st_size / 1048576:.1f} MB)")
    if zig_dir is not None:
        print("    这个包自带 x64 Zig，装的时候不需要联网。ARM64 机器上仍会回退到在线下载。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
