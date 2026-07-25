from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from .driver.compiler import build_exe, build_slib, check_source_diagnostics, compile_to_c, compile_to_native_ir
from .analysis.diagnostics import DiagnosticError, render_diagnostics
from .core.errors import SonCompileError
from .driver.formatter import renumber_file
from .packaging.spkg import pack_spkg


def ensure_no_diagnostics(source: Path, spkgs: list[Path]) -> bool:
    diagnostics = check_source_diagnostics(source, spkgs=spkgs)
    if not diagnostics:
        return True
    source_text = source.read_text(encoding="utf-8-sig")
    print(render_diagnostics(source, source_text, diagnostics), file=sys.stderr)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sonc", description="SonAlgebraic compiler")
    sub = parser.add_subparsers(dest="command", required=True)

    c_cmd = sub.add_parser("c", help="compile SonAlgebraic source to C")
    c_cmd.add_argument("source", type=Path)
    c_cmd.add_argument("-o", "--output", type=Path)
    c_cmd.add_argument("--target", help="target triple used when loading binary .slib packages")
    c_cmd.add_argument("--pkg", action="append", help="path to a .spkg package to use during resolution")

    native_ir_cmd = sub.add_parser("native-ir", help="compile SonAlgebraic source to experimental LLVM IR")
    native_ir_cmd.add_argument("source", type=Path)
    native_ir_cmd.add_argument("-o", "--output", type=Path)
    native_ir_cmd.add_argument("--pkg", action="append", help="path to a .spkg package to use during resolution")

    check_cmd = sub.add_parser("check", help="parse and type-check SonAlgebraic source")
    check_cmd.add_argument("source", type=Path)
    check_cmd.add_argument("--pkg", action="append", help="path to a .spkg package to use during resolution")

    build_cmd = sub.add_parser("build", help="compile SonAlgebraic source to native executable")
    build_cmd.add_argument("source", type=Path)
    build_cmd.add_argument("-o", "--output", type=Path)
    build_cmd.add_argument("--discard-c", action="store_true", help="delete generated C file after successful build")
    build_cmd.add_argument("--backend", choices=("c", "native"), default="c", help="backend to use; native is experimental")
    build_cmd.add_argument("--target", help="target triple used when loading binary .slib packages")
    build_cmd.add_argument("--pkg", action="append", help="path to a .spkg package to use during resolution")

    run_cmd = sub.add_parser("run", help="compile and run SonAlgebraic source")
    run_cmd.add_argument("source", type=Path)
    run_cmd.add_argument("--backend", choices=("c", "native"), default="c", help="backend to use; native is experimental")
    run_cmd.add_argument("--target", help="target triple used when loading binary .slib packages")
    run_cmd.add_argument("--pkg", action="append", help="path to a .spkg package to use during resolution")

    fmt_cmd = sub.add_parser("fmt", help="format SonAlgebraic source")
    fmt_cmd.add_argument("source", type=Path)
    fmt_cmd.add_argument("-o", "--output", type=Path)
    fmt_cmd.add_argument("--renumber", type=int, default=10, metavar="STEP", help="renumber non-empty source lines with the given step")
    fmt_cmd.add_argument("--start", type=int, help="first generated line number; defaults to STEP")

    pack_cmd = sub.add_parser("pack", help="package SonAlgebraic modules into a .spkg file")
    pack_cmd.add_argument("source", type=Path)
    pack_cmd.add_argument("-o", "--output", type=Path)
    pack_cmd.add_argument("--module", help="override package/module name")
    pack_cmd.add_argument("--version", default="0.1.0", help="package version")
    pack_cmd.add_argument("--description", default="", help="package description")
    pack_cmd.add_argument("--author", default="", help="package author")
    pack_cmd.add_argument("--license", default="", help="package license")

    slib_cmd = sub.add_parser("slib", help="compile a SonAlgebraic module to .slib")
    slib_cmd.add_argument("source", type=Path)
    slib_cmd.add_argument("-o", "--output", type=Path)
    slib_cmd.add_argument("--module", help="override module name stored in the .slib")
    slib_cmd.add_argument("--binary", action="store_true", help="include a static library for the requested target")
    slib_cmd.add_argument("--dynamic", action="store_true", help="include a dynamic library (DLL/SO/dylib) for the requested target")
    slib_cmd.add_argument("--target", help="target triple for binary .slib output")

    args = parser.parse_args(argv)

    try:
        if args.command == "c":
            output = args.output or args.source.with_suffix(".c")
            spkgs = [Path(p) for p in args.pkg] if args.pkg else []
            if not ensure_no_diagnostics(args.source, spkgs):
                return 1
            compile_to_c(args.source, output, args.target, spkgs=spkgs)
            print(output)
            return 0
        if args.command == "check":
            spkgs = [Path(p) for p in args.pkg] if args.pkg else []
            diagnostics = check_source_diagnostics(args.source, spkgs=spkgs)
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
            output = args.output or args.source.with_suffix(".exe")
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
            suffix = ".exe" if sys.platform == "win32" else ""
            with TemporaryDirectory(prefix="sonalgebraic-run-") as temp:
                output = Path(temp) / f"{args.source.stem}{suffix}"
                result = build_exe(args.source, output, keep_c=False, target=args.target, spkgs=spkgs, backend=args.backend)
                proc = subprocess.run([str(result.exe_path)])
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
    except DiagnosticError as exc:
        print(render_diagnostics("<unknown>", "", exc.diagnostics), file=sys.stderr)
        return 1
    except SonCompileError as exc:
        print(f"sonc: error: {exc}", file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
