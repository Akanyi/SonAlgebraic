# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 sonc 编译器冻结成不依赖 Python 环境的可执行文件。

用 onedir 而不是 onefile：onefile 每次启动都要把整包解压到临时目录，对一个会被
编辑器、构建脚本高频调用的编译器来说，那点启动开销累计起来很难受。反正外面还有
一层 Inno Setup 安装包，目录形态对用户是不可见的。
"""

from pathlib import Path

PROJECT_ROOT = Path(SPECPATH).parent

analysis = Analysis(
    [str(PROJECT_ROOT / "sonc.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 编译器本身只用到 jinja2 / pydantic，把 GUI、科学计算、测试框架这些
    # 顺着依赖爬进来的重量级包挡在外面，省下几十 MB 和一堆无用 DLL。
    # 标准库不碰：pydantic 的 plugin loader 走 importlib.metadata，
    # 而它一路依赖到 email，排掉就是启动即崩。
    excludes=[
        "tkinter",
        "pytest",
        "numpy",
        "matplotlib",
        "PIL",
        "IPython",
        "setuptools",
        "pip",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="sonc",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / "installer" / "assets" / "sadk.ico"),
)

collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="sonc",
)
