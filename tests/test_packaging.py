"""模块系统与包格式测试：分离编译、.slib 三态、.spkg、循环依赖。"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile
import hashlib
import json

import pytest

from conftest import requires_gcc, requires_windows
from sonalgebraic.core.errors import SonCompileError
from sonalgebraic.driver.compiler import check_source, compile_to_c
from sonalgebraic.frontend.parser import parse_program
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.packaging.module_compiler import compile_project
from sonalgebraic.packaging.slib import build_slib
from sonalgebraic.packaging.spkg import extract_spkg, pack_spkg


def _seed_mathlib(temp_dir: Path) -> tuple[Path, Path]:
    """在临时目录放置 mathlib 库和引用它的主程序，返回 (lib, main)。"""
    source_lib = temp_dir / "mathlib.sa"
    source_main = temp_dir / "use_mathlib.sa"
    source_lib.write_text(Path("examples/mathlib.sa").read_text(encoding="utf-8"), encoding="utf-8")
    source_main.write_text(Path("examples/use_user_module.sa").read_text(encoding="utf-8"), encoding="utf-8")
    return source_lib, source_main


def test_user_module_project_generates_headers_and_split_c() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        out_dir = Path(temp) / "out"
        plan = compile_project(Path("examples/use_user_module.sa"), out_dir)
        generated = "\n".join(path.read_text(encoding="utf-8") for path in plan.c_files + plan.headers if path.exists())
        assert (out_dir / "sa_runtime.h").exists()
        assert (out_dir / "sa_user_mathlib.h").exists()
        assert "extern double sa_mod_mathlib_const_scale;" in generated
        assert "double sa_mod_mathlib_sub_twice(double sa_value);" in generated
        assert "sa_mod_mathlib_init();" in generated
        assert "sa_mod_mathlib_sub_twice(4.0)" in generated


def test_slib_can_be_used_without_module_source() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        source_lib, source_main = _seed_mathlib(temp_dir)
        build_slib(source_lib, temp_dir / "mathlib.slib")
        source_lib.unlink()

        plan = compile_project(source_main, temp_dir / "out")
        generated = "\n".join(path.read_text(encoding="utf-8") for path in plan.c_files + plan.headers if path.exists())
        assert "sa_mod_mathlib_sub_twice(4.0)" in generated
        assert "extern double sa_mod_mathlib_const_scale;" in generated


@requires_gcc
def test_binary_slib_loads_static_library_for_target() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        source_lib, source_main = _seed_mathlib(temp_dir)
        build_slib(source_lib, temp_dir / "mathlib.slib", binary=True)
        source_lib.unlink()

        plan = compile_project(source_main, temp_dir / "out")
        assert plan.libs
        assert any(path.suffix == ".a" for path in plan.libs)
        assert not any(path.name == "sa_user_mathlib.c" for path in plan.c_files)


@requires_gcc
@requires_windows  # 断言产物里有 .dll 和 import lib，POSIX 上是 .so 且没有后者
def test_dynamic_slib_loads_dll_and_import_lib() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        source_lib, source_main = _seed_mathlib(temp_dir)
        build_slib(source_lib, temp_dir / "mathlib.slib", dynamic=True)
        source_lib.unlink()

        plan = compile_project(source_main, temp_dir / "out")
        assert plan.dlls
        assert any(path.suffix == ".dll" for path in plan.dlls)
        assert plan.libs
        assert any(path.name.endswith(".dll.a") for path in plan.libs)
        assert not any(path.name == "sa_user_mathlib.c" for path in plan.c_files)


def test_dynamic_slib_rejects_process_local_runtime_state() -> None:
    source = '''10 USE SYS.BINARY AS B
20 SUB make() AS PUBLIC AS HANDLE AS BUFFER
30 RETURN B.NEW(1)
40 .ENDSUB
'''
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        root = Path(temp)
        module = root / "bufferlib.sa"
        module.write_text(source, encoding="utf-8")
        with pytest.raises(SonCompileError, match="动态 \\.slib 暂不支持"):
            build_slib(module, root / "bufferlib.slib", dynamic=True)


def test_spkg_can_be_packed_and_used() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        source_lib, source_main = _seed_mathlib(temp_dir)
        spkg_path = temp_dir / "mathlib.spkg"
        pack_spkg(source_lib, spkg_path)
        source_lib.unlink()

        plan = compile_project(source_main, temp_dir / "out", spkgs=[spkg_path])
        generated = "\n".join(path.read_text(encoding="utf-8") for path in plan.c_files + plan.headers if path.exists())
        assert "sa_mod_mathlib_sub_twice(4.0)" in generated


def test_compile_to_c_accepts_spkg() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        source_lib, source_main = _seed_mathlib(temp_dir)
        spkg_path = temp_dir / "mathlib.spkg"
        pack_spkg(source_lib, spkg_path)
        source_lib.unlink()

        main_c = compile_to_c(source_main, temp_dir / "out", spkgs=[spkg_path])
        assert main_c.exists()
        assert "sa_mod_mathlib_sub_twice(4.0)" in main_c.read_text(encoding="utf-8")


def test_check_source_accepts_user_module() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        (temp_dir / "main.sa").write_text(
            '10 USE MATHLIB AS LIB\n20 DIM result AS NUM AS DOUBLE AS VAR\n30 SUB main AS PUBLIC AS VOID\n40 result = CALL LIB.twice(2.0)\n50 .ENDSUB\n60 CALL main\n70 END\n',
            encoding="utf-8",
        )
        (temp_dir / "mathlib.sa").write_text(Path("examples/mathlib.sa").read_text(encoding="utf-8"), encoding="utf-8")

        check_source(temp_dir / "main.sa")


def test_samath_module_exports_native_math_wrappers() -> None:
    check_program(parse_program(Path("examples/samath.sa").read_text(encoding="utf-8")), require_main=False)
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        plan = compile_project(Path("examples/use_samath.sa"), Path(temp) / "out")
        generated = "\n".join(path.read_text(encoding="utf-8") for path in plan.c_files + plan.headers if path.exists())
        assert "sa_mod_samath_sub_pow" in generated
        assert "sa_mod_samath_sub_gaussian_pdf" in generated
        assert "m" in plan.link_libs


def test_samath_source_slib_can_be_used_without_source() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        source_lib = temp_dir / "samath.sa"
        source_main = temp_dir / "use_samath.sa"
        source_lib.write_text(Path("examples/samath.sa").read_text(encoding="utf-8"), encoding="utf-8")
        source_main.write_text(Path("examples/use_samath.sa").read_text(encoding="utf-8"), encoding="utf-8")
        build_slib(source_lib, temp_dir / "samath.slib")
        source_lib.unlink()

        plan = compile_project(source_main, temp_dir / "out")
        generated = "\n".join(path.read_text(encoding="utf-8") for path in plan.c_files + plan.headers if path.exists())
        assert "sa_mod_samath_sub_pow" in generated
        assert "m" in plan.link_libs


def test_compile_project_reports_module_cycle_chain() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        (temp_dir / "main.sa").write_text(
            '10 USE A AS A\n20 SUB main AS PUBLIC AS VOID\n30 PRINT "ok"\n40 .ENDSUB\n50 CALL main\n60 END\n',
            encoding="utf-8",
        )
        (temp_dir / "a.sa").write_text("10 USE B AS B\n", encoding="utf-8")
        (temp_dir / "b.sa").write_text("10 USE A AS A\n", encoding="utf-8")

        with pytest.raises(SonCompileError) as excinfo:
            compile_project(temp_dir / "main.sa", temp_dir / "out")
        assert "模块循环依赖" in str(excinfo.value)
        assert "A -> B -> A" in str(excinfo.value)


def test_check_source_reports_module_cycle_chain() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        (temp_dir / "main.sa").write_text(
            '10 USE A AS A\n20 SUB main AS PUBLIC AS VOID\n30 PRINT "ok"\n40 .ENDSUB\n50 CALL main\n60 END\n',
            encoding="utf-8",
        )
        (temp_dir / "a.sa").write_text("10 USE B AS B\n", encoding="utf-8")
        (temp_dir / "b.sa").write_text("10 USE A AS A\n", encoding="utf-8")

        with pytest.raises(SonCompileError) as excinfo:
            check_source(temp_dir / "main.sa")
        assert "模块循环依赖" in str(excinfo.value)
        assert "A -> B -> A" in str(excinfo.value)


def test_recursive_module_uselib_adds_link_libs() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        (temp_dir / "main.sa").write_text(
            '10 USE ROOT AS R\n20 SUB main AS PUBLIC AS VOID\n30 PRINT "ok"\n40 .ENDSUB\n50 CALL main\n60 END\n',
            encoding="utf-8",
        )
        (temp_dir / "root.sa").write_text(
            '10 USE CHILD AS C\n20 USELIB "rootffi" AS ROOT_LIB\n30 SUB noop AS PUBLIC AS VOID\n40 .ENDSUB\n',
            encoding="utf-8",
        )
        (temp_dir / "child.sa").write_text(
            '10 USELIB "childffi" AS CHILD_LIB\n20 SUB ping AS PUBLIC AS VOID\n30 .ENDSUB\n',
            encoding="utf-8",
        )

        plan = compile_project(temp_dir / "main.sa", temp_dir / "out")
        assert set(plan.link_libs) == {"rootffi", "childffi"}


def test_spkg_rejects_zip_path_traversal() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        spkg_path = temp_dir / "evil.spkg"
        manifest = {
            "format": "sonalgebraic-spkg",
            "version": 1,
            "package": {"name": "evil"},
            "modules": [],
            "module_to_package": {},
            "hashes": {},
        }
        with ZipFile(spkg_path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("../owned.txt", "owned")

        with pytest.raises(SonCompileError) as excinfo:
            extract_spkg(spkg_path, temp_dir / "out")
        assert "不安全路径" in str(excinfo.value)
        assert not (temp_dir / "owned.txt").exists()


def test_spkg_verifies_declared_hashes() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        spkg_path = temp_dir / "bad_hash.spkg"
        good = b'10 SUB noop AS PUBLIC AS VOID\n20 .ENDSUB\n'
        bad = b'10 SUB noop AS PUBLIC AS VOID\n20 PRINT "tampered"\n30 .ENDSUB\n'
        entry = "packages/mathlib/src/__init__.sa"
        digest = hashlib.sha256(good).hexdigest()
        manifest = {
            "format": "sonalgebraic-spkg",
            "version": 1,
            "package": {"name": "mathlib"},
            "modules": [{"name": "MATHLIB", "package": "mathlib", "source": "src/__init__.sa", "binaries": {}}],
            "module_to_package": {"MATHLIB": "mathlib"},
            "hashes": {entry: f"sha256:{digest}"},
        }
        with ZipFile(spkg_path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr(entry, bad)

        with pytest.raises(SonCompileError) as excinfo:
            extract_spkg(spkg_path, temp_dir / "out")
        assert "hash 校验失败" in str(excinfo.value)
