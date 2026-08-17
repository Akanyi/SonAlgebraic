from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from . import __version__
from .driver.compiler import build_exe, build_slib, check_source_diagnostics, compile_to_c, compile_to_native_ir
from .analysis.diagnostics import diagnostics_to_json, render_diagnostics
from .core.errors import SonCompileError
from .driver.formatter import renumber_file
from .packaging.spkg import pack_spkg
from .packaging.toolchain import normalize_target


def _force_utf8_streams() -> None:
    """诊断文案是中文，而 Windows 在管道/重定向下会退回本地代码页。

    cp936 环境里输出乱码，西文 locale（cp1252）更是直接 UnicodeEncodeError，
    CI 日志和 `2> err.txt` 全中招，所以这里统一钉成 UTF-8。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def exe_suffix(target: str | None = None) -> str:
    """可执行文件后缀。看的是目标平台而不是宿主：在 Linux 上 `--target
    x86_64-windows-gnu` 产出的就该叫 .exe，反过来在 Windows 上交叉编译 Linux 目标不该带。"""
    if target is not None:
        return ".exe" if "windows" in normalize_target(target) else ""
    return ".exe" if sys.platform == "win32" else ""


def ensure_no_diagnostics(source: Path, spkgs: list[Path]) -> bool:
    diagnostics = check_source_diagnostics(source, spkgs=spkgs)
    if not diagnostics:
        return True
    source_text = source.read_text(encoding="utf-8-sig")
    print(render_diagnostics(source, source_text, diagnostics), file=sys.stderr)
    return False


def main(argv: list[str] | None = None) -> int:
    _force_utf8_streams()

    # `sonc run app.sa -- a b` 里 `--` 之后的东西转发给被编译的程序。
    # 这里手工切分而不是用 argparse.REMAINDER：后者会从第一个位置参数起吞掉一切，
    # 连 --backend 都会被当成程序参数。
    raw_args = list(sys.argv[1:] if argv is None else argv)
    program_args: list[str] = []
    if "--" in raw_args:
        cut = raw_args.index("--")
        raw_args, program_args = raw_args[:cut], raw_args[cut + 1 :]

    target_help = "目标三元组（交叉编译需要 zig；也用于选择二进制 .slib）"
    pkg_help = "解析模块时额外使用的 .spkg 包路径，可重复"
    backend_help = "代码生成后端；native 仍是实验特性"

    parser = argparse.ArgumentParser(
        prog="sonc",
        description="SonAlgebraic 编译器",
        epilog="常用：sonc run app.sa —— 编译并直接运行；`--` 之后的参数原样转发给程序。",
    )
    parser.add_argument("--version", action="version", version=f"sonc {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    c_cmd = sub.add_parser("c", help="编译成 C 源码")
    c_cmd.add_argument("source", type=Path)
    c_cmd.add_argument("-o", "--output", type=Path)
    c_cmd.add_argument("--target", help=target_help)
    c_cmd.add_argument("--pkg", action="append", help=pkg_help)

    native_ir_cmd = sub.add_parser("native-ir", help="编译成实验性的 LLVM IR")
    native_ir_cmd.add_argument("source", type=Path)
    native_ir_cmd.add_argument("-o", "--output", type=Path)
    native_ir_cmd.add_argument("--pkg", action="append", help=pkg_help)

    check_cmd = sub.add_parser("check", help="解析并做类型检查，不产出文件")
    check_cmd.add_argument("source", type=Path)
    check_cmd.add_argument("--pkg", action="append", help=pkg_help)
    check_cmd.add_argument("--json", action="store_true", help="以 JSON 输出诊断供编辑器/CI 消费，取代人类可读格式")

    build_cmd = sub.add_parser("build", help="编译成本机可执行文件")
    build_cmd.add_argument("source", type=Path)
    build_cmd.add_argument("-o", "--output", type=Path)
    build_cmd.add_argument("--discard-c", action="store_true", help="构建成功后删除生成的 C 文件（模块项目则删除整个生成目录）")
    build_cmd.add_argument("--backend", choices=("c", "native"), default="c", help=backend_help)
    build_cmd.add_argument("--target", help=target_help)
    build_cmd.add_argument("--pkg", action="append", help=pkg_help)

    run_cmd = sub.add_parser("run", help="编译并立即运行")
    run_cmd.add_argument("source", type=Path)
    run_cmd.add_argument("--backend", choices=("c", "native"), default="c", help=backend_help)
    run_cmd.add_argument("--target", help=target_help)
    run_cmd.add_argument("--pkg", action="append", help=pkg_help)

    fmt_cmd = sub.add_parser("fmt", help="重排行号（默认原地写回）")
    fmt_cmd.add_argument("source", type=Path)
    fmt_cmd.add_argument("-o", "--output", type=Path)
    fmt_cmd.add_argument("--renumber", type=int, default=10, metavar="STEP", help="行号步长")
    fmt_cmd.add_argument("--start", type=int, help="起始行号，默认等于 STEP")

    pack_cmd = sub.add_parser("pack", help="把模块打包成 .spkg 源码包")
    pack_cmd.add_argument("source", type=Path)
    pack_cmd.add_argument("-o", "--output", type=Path)
    pack_cmd.add_argument("--module", help="覆盖包名/模块名")
    pack_cmd.add_argument("--version", default="0.1.0", help="包版本号")
    pack_cmd.add_argument("--description", default="", help="包描述")
    pack_cmd.add_argument("--author", default="", help="包作者")
    pack_cmd.add_argument("--license", default="", help="包许可协议")

    slib_cmd = sub.add_parser("slib", help="把模块编译成 .slib 库")
    slib_cmd.add_argument("source", type=Path)
    slib_cmd.add_argument("-o", "--output", type=Path)
    slib_cmd.add_argument("--module", help="覆盖 .slib 里记录的模块名")
    slib_cmd.add_argument("--binary", action="store_true", help="附带目标平台的静态库")
    slib_cmd.add_argument("--dynamic", action="store_true", help="附带目标平台的动态库（DLL/SO/dylib）")
    slib_cmd.add_argument("--target", help=target_help)

    args = parser.parse_args(raw_args)

    try:
        if args.command == "c":
            output = args.output or args.source.with_suffix(".c")
            spkgs = [Path(p) for p in args.pkg] if args.pkg else []
            if not ensure_no_diagnostics(args.source, spkgs):
                return 1
            # 含用户模块时 compile_to_c 会把 -o 当成项目目录，真正的主 C 文件是返回值；
            # 直接 print(output) 打的是一个不存在的路径，下游脚本拿去喂 gcc 必挂。
            print(compile_to_c(args.source, output, args.target, spkgs=spkgs))
            return 0
        if args.command == "check":
            spkgs = [Path(p) for p in args.pkg] if args.pkg else []
            diagnostics = check_source_diagnostics(args.source, spkgs=spkgs)
            # --json 独占输出：走这条路的是编辑器/CI，混进 "OK" 或带下划线的文本会破坏解析
            if args.json:
                print(diagnostics_to_json(args.source, diagnostics))
                return 1 if diagnostics else 0
            if diagnostics:
                source_text = args.source.read_text(encoding="utf-8-sig")
                print(render_diagnostics(args.source, source_text, diagnostics), file=sys.stderr)
                return 1
            print("OK")
            return 0
        if args.command == "native-ir":
            output = args.output or args.source.with_suffix(".ll")
            spkgs = [Path(p) for p in args.pkg] if args.pkg else []
            if not ensure_no_diagnostics(args.source, spkgs):
                return 1
            compile_to_native_ir(args.source, output, spkgs=spkgs)
            print(output)
            return 0
        if args.command == "build":
            output = args.output or args.source.with_suffix(exe_suffix(args.target))
            spkgs = [Path(p) for p in args.pkg] if args.pkg else []
            if not ensure_no_diagnostics(args.source, spkgs):
                return 1
            result = build_exe(args.source, output, keep_c=not args.discard_c, target=args.target, spkgs=spkgs, backend=args.backend)
            print(result.exe_path)
            return 0
        if args.command == "run":
            spkgs = [Path(p) for p in args.pkg] if args.pkg else []
            if not ensure_no_diagnostics(args.source, spkgs):
                return 1
            with TemporaryDirectory(prefix="sonalgebraic-run-") as temp:
                output = Path(temp) / f"{args.source.stem}{exe_suffix(args.target)}"
                result = build_exe(args.source, output, keep_c=False, target=args.target, spkgs=spkgs, backend=args.backend)
                proc = subprocess.run([str(result.exe_path), *program_args])
                return proc.returncode
        if args.command == "fmt":
            output = renumber_file(args.source, args.output, step=args.renumber, start=args.start)
            print(output)
            return 0
        if args.command == "pack":
            output = args.output or Path(f"{args.source.stem}.spkg")
            pack_spkg(
                args.source,
                output,
                package_name=args.module,
                version=args.version,
                description=args.description,
                author=args.author,
                license=args.license,
            )
            print(output)
            return 0
        if args.command == "slib":
            output = args.output or args.source.with_suffix(".slib")
            build_slib(args.source, output, args.module, args.binary, args.dynamic, args.target)
            print(output)
            return 0
    except SonCompileError as exc:
        print(f"sonc: error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"sonc: error: 找不到文件: {exc.filename}", file=sys.stderr)
        return 1
    except IsADirectoryError as exc:
        print(f"sonc: error: 需要文件但给的是目录: {exc.filename}", file=sys.stderr)
        return 1
    except UnicodeDecodeError:
        print("sonc: error: 源文件不是合法的 UTF-8 文本", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"sonc: error: 读写失败: {exc}", file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
