"""一键构建 SADK 安装包：PyInstaller 冻结 sonc，再交给 Inno Setup 打成 exe。

    python installer/build_installer.py

产物在 build/installer/SADK-Setup-<version>.exe。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

INSTALLER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = INSTALLER_DIR.parent
BUILD_DIR = PROJECT_ROOT / "build"
DIST_DIR = BUILD_DIR / "sadk-dist"
WORK_DIR = BUILD_DIR / "pyi-work"
OUTPUT_DIR = BUILD_DIR / "installer"

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


def build_installer(version: str) -> Path:
    iscc = find_iscc()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    run(
        [str(iscc), f"/DSadkVersion={version}", str(INSTALLER_DIR / "sadk.iss")],
        "Inno Setup 打包",
    )
    setup = OUTPUT_DIR / f"SADK-Setup-{version}.exe"
    if not setup.is_file():
        raise SystemExit(f"Inno Setup 没有产出 {setup}")
    return setup


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 SADK 安装包")
    parser.add_argument("--skip-freeze", action="store_true", help="复用上次的 PyInstaller 产物，只重打安装包")
    parser.add_argument("--clean", action="store_true", help="冻结前清空 PyInstaller 的 dist / work 目录")
    args = parser.parse_args()

    version = project_version()
    print(f"SADK {version}")

    build_icon()
    if args.skip_freeze:
        if not (DIST_DIR / "sonc" / "sonc.exe").is_file():
            raise SystemExit("--skip-freeze 需要上次的冻结产物，但没找到；去掉这个参数重跑")
        print("\n>>> 跳过冻结，复用已有产物")
    else:
        freeze_compiler(args.clean)

    setup = build_installer(version)
    print(f"\n完成: {setup}  ({setup.stat().st_size / 1048576:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
