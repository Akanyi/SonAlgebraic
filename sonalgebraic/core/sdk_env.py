"""SADK 安装包布局相关的运行时环境探测。

源码/pip 安装的 sonalgebraic 完全不涉及这里的东西——它靠系统 PATH 找 C 工具链。
只有从 SADK 安装包跑出来的 sonc.exe 才有「自带工具链」这回事。
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

TOOLCHAIN_DIRNAME = "toolchain"


def sdk_home() -> Path | None:
    """定位 SADK 安装根目录；不是从安装包跑的返回 None。"""
    override = os.environ.get("SADK_HOME")
    if override:
        candidate = Path(override)
        return candidate if candidate.is_dir() else None

    # 源码或 pip 运行时没有捆绑工具链，用户的系统 PATH 就是唯一事实来源
    if not getattr(sys, "frozen", False):
        return None

    # 判定靠安装布局 {app}/bin/sonc.exe 本身，不能靠 toolchain/ 是否存在：
    # 没勾选下载工具链的用户正好就缺那个目录，而他恰恰最需要「重跑安装程序」
    # 这条提示——拿缺失项当判据，会把提示指反。
    exe_dir = Path(sys.executable).resolve().parent
    if exe_dir.name.lower() == "bin":
        return exe_dir.parent
    # 便携解压可能把 exe 直接丢在根上，这时只能靠 toolchain/ 认亲
    for candidate in (exe_dir, *list(exe_dir.parents)[:2]):
        if (candidate / TOOLCHAIN_DIRNAME).is_dir():
            return candidate
    return None


def bundled_toolchain_dirs() -> list[Path]:
    """SADK 自带工具链里该进 PATH 的目录。

    约定布局是 `toolchain/<工具名>/` 下直接放可执行文件——安装器下载 zig 官方 zip
    后会把解压出来的 `zig-x86_64-windows-<ver>/` 重命名成 `zig/`。额外认一层 `bin/`
    和带版本号的中间目录，这样手工解压出来的便携版不用调整目录结构也能直接用。
    """
    home = sdk_home()
    if home is None:
        return []
    root = home / TOOLCHAIN_DIRNAME
    if not root.is_dir():
        return []

    dirs: list[Path] = []
    for tool_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        dirs.append(tool_dir)
        if (tool_dir / "bin").is_dir():
            dirs.append(tool_dir / "bin")
            continue
        dirs.extend(sorted(p for p in tool_dir.iterdir() if p.is_dir() and p.name.startswith(tool_dir.name)))
    return dirs


def activate_bundled_toolchain() -> list[Path]:
    """把 SADK 自带工具链前置进本进程 PATH，返回实际生效的目录。

    编译器查找散在 driver/compiler.py 和 packaging/toolchain.py 的十几个
    `shutil.which` 上，而且拼命令行时用的是裸名字（`["zig", "cc", ...]`）。与其把
    每个调用点都改成绝对路径，不如在入口改一次 PATH——which 和 subprocess 都读它，
    一处覆盖整条链路，子进程也自动继承。
    """
    dirs = [d for d in bundled_toolchain_dirs() if d.is_dir()]
    if not dirs:
        return []
    prefix = os.pathsep.join(str(d) for d in dirs)
    current = os.environ.get("PATH", "")
    # run 子命令会 spawn 子进程，重复注入会让 PATH 一路滚大
    if current.startswith(prefix):
        return dirs
    os.environ["PATH"] = prefix + (os.pathsep + current if current else "")
    return dirs


def toolchain_install_hint() -> str:
    """C 编译器缺失时补一句「在这台机器上具体该怎么办」。

    装了 SADK 但当时没勾工具链的用户，和纯 pip 用户，要做的事情完全不同：
    前者重跑一次安装程序就有，后者得自己装 zig。给错指引比不给还费时间。
    """
    if sdk_home() is None:
        return "请安装 zig（https://ziglang.org/download/）并确保它在 PATH 中。"
    return "重新运行 SADK 安装程序并勾选「下载 Zig C 工具链」即可自动补齐，也可以自行安装 zig 并加入 PATH。"


def toolchain_report() -> str:
    """`sonc doctor` 的正文：装完之后到底能不能编，一眼看完。"""
    home = sdk_home()
    lines = [f"SADK 安装目录: {home}" if home else "SADK 安装目录: 未使用安装包（源码或 pip 运行）"]

    bundled = bundled_toolchain_dirs()
    lines.append("自带工具链目录: " + (", ".join(str(d) for d in bundled) if bundled else "无"))

    lines.append("")
    lines.append("C 工具链探测:")
    found_any = False
    for name in ("zig", "gcc", "clang", "tcc", "cl"):
        path = shutil.which(name)
        if path:
            found_any = True
        lines.append(f"  {name:<6} {path or '未找到'}")

    lines.append("")
    if found_any:
        lines.append("状态: 就绪，sonc build / run 可用。")
    else:
        lines.append("状态: 缺少 C 编译器，sonc check / c / fmt 可用，但 build / run 会失败。")
        lines.append(toolchain_install_hint())
    return "\n".join(lines)
