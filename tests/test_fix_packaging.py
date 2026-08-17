"""包格式修复的回归测试：.slib 嵌套二进制依赖、.slib 完整性清单、.spkg 解包与命名空间。"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile
import hashlib
import json
import shutil
import subprocess

import pytest

from conftest import requires_gcc
from sonalgebraic.core.errors import SonCompileError
from sonalgebraic.driver.compiler import build_exe
from sonalgebraic.packaging.module_compiler import compile_project
from sonalgebraic.packaging.slib import build_slib
from sonalgebraic.packaging.spkg import extract_spkg, pack_spkg

LEAF_SRC = """10 CONST LEAFK AS NUM AS DOUBLE = 3.0
20 SUB triple(value AS NUM AS DOUBLE) AS PUBLIC AS NUM AS DOUBLE
30 RETURN value * LEAFK
40 .ENDSUB
"""

ROOT_SRC = """10 USE LEAF AS L
20 SUB sixfold(value AS NUM AS DOUBLE) AS PUBLIC AS NUM AS DOUBLE
30 RETURN L.triple(value) * 2.0
40 .ENDSUB
"""

MAIN_SRC = """10 USE ROOT AS R
20 DIM answer AS NUM AS DOUBLE AS VAR
30 SUB main AS PUBLIC AS VOID
40 answer = R.sixfold(2.0)
50 PRINT answer
60 .ENDSUB
70 CALL main
80 END
"""


def _seed_nested_binary_dep(temp_dir: Path) -> Path:
    """铺一个「ROOT 源码 + LEAF 二进制 .slib」的场景，返回 main.sa。"""
    (temp_dir / "leaf.sa").write_text(LEAF_SRC, encoding="utf-8")
    (temp_dir / "root.sa").write_text(ROOT_SRC, encoding="utf-8")
    (temp_dir / "main.sa").write_text(MAIN_SRC, encoding="utf-8")
    build_slib(temp_dir / "leaf.sa", temp_dir / "leaf.slib", binary=True)
    (temp_dir / "leaf.sa").unlink()
    return temp_dir / "main.sa"


def _rewrite_slib(src: Path, dst: Path, mutate) -> None:
    with ZipFile(src) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(members["manifest.json"])
    mutate(manifest, members)
    members["manifest.json"] = json.dumps(manifest, indent=2).encode("utf-8")
    with ZipFile(dst, "w", ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)


@requires_gcc
def test_source_slib_packs_binary_dependency_and_links() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        main = _seed_nested_binary_dep(temp_dir)
        build_slib(temp_dir / "root.sa", temp_dir / "root.slib")
        (temp_dir / "root.sa").unlink()

        with ZipFile(temp_dir / "root.slib") as archive:
            manifest = json.loads(archive.read("manifest.json"))
            names = set(archive.namelist())
        leaf = next(item for item in manifest["units"] if item["module"] == "LEAF")
        assert "c_entry" not in leaf
        entry = next(iter(leaf["archives"].values()))["entry"]
        assert entry in names

        plan = compile_project(main, temp_dir / "out")
        assert any(path.suffix == ".a" for path in plan.libs)
        assert not any(path.name == "sa_user_leaf.c" for path in plan.c_files)

        result = build_exe(main, temp_dir / "main.exe", keep_c=False)
        proc = subprocess.run([str(result.exe_path)], capture_output=True, text=True)
        assert proc.stdout.strip() == "12"


@requires_gcc
def test_static_slib_packs_binary_dependency_and_links() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        main = _seed_nested_binary_dep(temp_dir)
        build_slib(temp_dir / "root.sa", temp_dir / "root.slib", binary=True)
        (temp_dir / "root.sa").unlink()

        plan = compile_project(main, temp_dir / "out")
        assert len([path for path in plan.libs if path.suffix == ".a"]) == 2

        result = build_exe(main, temp_dir / "main.exe", keep_c=False)
        proc = subprocess.run([str(result.exe_path)], capture_output=True, text=True)
        assert proc.stdout.strip() == "12"


@requires_gcc
def test_dynamic_slib_rejects_binary_dependency() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        _seed_nested_binary_dep(temp_dir)
        with pytest.raises(SonCompileError) as excinfo:
            build_slib(temp_dir / "root.sa", temp_dir / "root.slib", dynamic=True)
        assert "暂不支持依赖二进制 .slib" in str(excinfo.value)
        assert "LEAF" in str(excinfo.value)


@requires_gcc
def test_slib_binary_dependency_reports_target_mismatch() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        main = _seed_nested_binary_dep(temp_dir)
        build_slib(temp_dir / "root.sa", temp_dir / "root.slib")
        (temp_dir / "root.sa").unlink()

        with pytest.raises(SonCompileError) as excinfo:
            compile_project(main, temp_dir / "out", target="aarch64-linux-gnu")
        assert "只有预编译产物" in str(excinfo.value)


def _seed_source_slib(temp_dir: Path) -> Path:
    (temp_dir / "mathlib.sa").write_text(Path("examples/mathlib.sa").read_text(encoding="utf-8"), encoding="utf-8")
    (temp_dir / "main.sa").write_text(
        Path("examples/use_user_module.sa").read_text(encoding="utf-8"), encoding="utf-8"
    )
    build_slib(temp_dir / "mathlib.sa", temp_dir / "mathlib.slib")
    (temp_dir / "mathlib.sa").unlink()
    return temp_dir / "main.sa"


def test_slib_manifest_declares_hashes_for_every_member() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        _seed_source_slib(temp_dir)
        with ZipFile(temp_dir / "mathlib.slib") as archive:
            manifest = json.loads(archive.read("manifest.json"))
            members = set(archive.namelist()) - {"manifest.json"}
        assert manifest["version"] == 3
        assert set(manifest["hashes"]) == members


def test_slib_rejects_tampered_member() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        main = _seed_source_slib(temp_dir)
        original = temp_dir / "mathlib.orig"
        shutil.move(temp_dir / "mathlib.slib", original)

        def tamper(manifest, members):
            members["c/sa_user_mathlib.c"] = members["c/sa_user_mathlib.c"].replace(b"2.5", b"99.0")

        _rewrite_slib(original, temp_dir / "mathlib.slib", tamper)
        with pytest.raises(SonCompileError) as excinfo:
            compile_project(main, temp_dir / "out")
        assert "hash 校验失败" in str(excinfo.value)


def test_slib_rejects_dropped_hash_entry() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        main = _seed_source_slib(temp_dir)
        original = temp_dir / "mathlib.orig"
        shutil.move(temp_dir / "mathlib.slib", original)

        def drop(manifest, members):
            manifest["hashes"].pop("c/sa_user_mathlib.c")
            members["c/sa_user_mathlib.c"] = members["c/sa_user_mathlib.c"].replace(b"2.5", b"99.0")

        _rewrite_slib(original, temp_dir / "mathlib.slib", drop)
        with pytest.raises(SonCompileError) as excinfo:
            compile_project(main, temp_dir / "out")
        assert "缺少 hash 声明" in str(excinfo.value)


def test_slib_v3_without_hashes_is_rejected() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        main = _seed_source_slib(temp_dir)
        original = temp_dir / "mathlib.orig"
        shutil.move(temp_dir / "mathlib.slib", original)
        _rewrite_slib(original, temp_dir / "mathlib.slib", lambda manifest, members: manifest.pop("hashes"))
        with pytest.raises(SonCompileError) as excinfo:
            compile_project(main, temp_dir / "out")
        assert "必须带 hashes 清单" in str(excinfo.value)


def test_legacy_slib_without_hashes_still_loads_with_warning(capsys: pytest.CaptureFixture[str]) -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        main = _seed_source_slib(temp_dir)
        original = temp_dir / "mathlib.orig"
        shutil.move(temp_dir / "mathlib.slib", original)

        def downgrade(manifest, members):
            manifest["version"] = 2
            manifest.pop("hashes")

        _rewrite_slib(original, temp_dir / "mathlib.slib", downgrade)
        plan = compile_project(main, temp_dir / "out")
        assert "mathlib" in plan.modules
        assert "没有 hash 清单" in capsys.readouterr().err


def test_corrupt_slib_reports_compile_error() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        main = _seed_source_slib(temp_dir)
        original = temp_dir / "mathlib.orig"
        shutil.move(temp_dir / "mathlib.slib", original)
        _rewrite_slib(original, temp_dir / "mathlib.slib", lambda manifest, members: manifest.pop("units"))
        with pytest.raises(SonCompileError):
            compile_project(main, temp_dir / "out")


def test_slib_root_module_must_match_requested_module() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        _seed_source_slib(temp_dir)
        shutil.copy(temp_dir / "mathlib.slib", temp_dir / "impostor.slib")
        (temp_dir / "main.sa").write_text(
            "10 USE IMPOSTOR AS LIB\n"
            "20 DIM answer AS NUM AS DOUBLE AS VAR\n"
            "30 SUB main AS PUBLIC AS VOID\n"
            "40 answer = LIB.twice(4.0)\n"
            "50 .ENDSUB\n"
            "60 CALL main\n"
            "70 END\n",
            encoding="utf-8",
        )
        with pytest.raises(SonCompileError) as excinfo:
            compile_project(temp_dir / "main.sa", temp_dir / "out")
        assert "根模块是 MATHLIB" in str(excinfo.value)


def test_slib_with_gui_module_injects_windows_link_libs() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        (temp_dir / "guilib.sa").write_text(
            "10 USE SYS.GUI AS G\n"
            "20 SUB open_win AS PUBLIC AS VOID\n"
            "30 DIM w AS HANDLE AS WINDOW AS VAR\n"
            '40 w = G.WINDOW("hi", 100, 100)\n'
            "50 .ENDSUB\n",
            encoding="utf-8",
        )
        (temp_dir / "main.sa").write_text(
            "10 USE GUILIB AS L\n20 SUB main AS PUBLIC AS VOID\n30 CALL L.open_win\n40 .ENDSUB\n50 CALL main\n60 END\n",
            encoding="utf-8",
        )
        build_slib(temp_dir / "guilib.sa", temp_dir / "guilib.slib", target="x86_64-windows-gnu")
        (temp_dir / "guilib.sa").unlink()

        plan = compile_project(temp_dir / "main.sa", temp_dir / "out", target="x86_64-windows-gnu")
        assert {"user32", "gdi32"} <= set(plan.link_libs)


def test_same_stem_spkgs_do_not_overwrite_each_other() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        (temp_dir / "a").mkdir()
        (temp_dir / "b").mkdir()
        (temp_dir / "a" / "x.sa").write_text(
            "10 SUB ping AS PUBLIC AS NUM AS DOUBLE\n20 RETURN 1.0\n30 .ENDSUB\n", encoding="utf-8"
        )
        (temp_dir / "b" / "y.sa").write_text(
            "10 SUB pong AS PUBLIC AS NUM AS DOUBLE\n20 RETURN 2.0\n30 .ENDSUB\n", encoding="utf-8"
        )
        first = pack_spkg(temp_dir / "a" / "x.sa", temp_dir / "a" / "lib.spkg", package_name="alib")
        second = pack_spkg(temp_dir / "b" / "y.sa", temp_dir / "b" / "lib.spkg", package_name="blib")
        (temp_dir / "main.sa").write_text(
            "10 USE ALIB AS A\n"
            "15 USE BLIB AS B\n"
            "20 DIM answer AS NUM AS DOUBLE AS VAR\n"
            "30 SUB main AS PUBLIC AS VOID\n"
            "40 answer = A.ping() + B.pong()\n"
            "50 PRINT answer\n"
            "60 .ENDSUB\n"
            "70 CALL main\n"
            "80 END\n",
            encoding="utf-8",
        )

        plan = compile_project(temp_dir / "main.sa", temp_dir / "out", spkgs=[first, second])
        assert set(plan.modules) == {"alib", "blib"}


def test_spkg_extraction_drops_stale_files() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        (temp_dir / "src").mkdir()
        (temp_dir / "src" / "keep.sa").write_text(
            "10 SUB ping AS PUBLIC AS NUM AS DOUBLE\n20 RETURN 1.0\n30 .ENDSUB\n", encoding="utf-8"
        )
        (temp_dir / "src" / "gone.sa").write_text(
            "10 SUB pong AS PUBLIC AS NUM AS DOUBLE\n20 RETURN 2.0\n30 .ENDSUB\n", encoding="utf-8"
        )
        spkg = pack_spkg(temp_dir / "src", temp_dir / "demo.spkg", package_name="demo")
        (temp_dir / "main.sa").write_text(
            "10 USE DEMO.KEEP AS K\n"
            "20 DIM answer AS NUM AS DOUBLE AS VAR\n"
            "30 SUB main AS PUBLIC AS VOID\n"
            "40 answer = K.ping()\n"
            "50 .ENDSUB\n"
            "60 CALL main\n"
            "70 END\n",
            encoding="utf-8",
        )
        out_dir = temp_dir / "out"
        compile_project(temp_dir / "main.sa", out_dir, spkgs=[spkg])

        # 新版本包去掉了 gone.sa，重新解包不该留下上一版的残留
        (temp_dir / "src" / "gone.sa").unlink()
        pack_spkg(temp_dir / "src", spkg, package_name="demo")
        compile_project(temp_dir / "main.sa", out_dir, spkgs=[spkg])
        leftovers = list((out_dir / "_spkgs").rglob("gone.sa"))
        assert leftovers == []


def test_intra_package_use_resolves_to_package_namespace() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        (temp_dir / "src").mkdir()
        (temp_dir / "src" / "util.sa").write_text(
            "10 CONST UK AS NUM AS DOUBLE = 7.0\n"
            "20 SUB bump(v AS NUM AS DOUBLE) AS PUBLIC AS NUM AS DOUBLE\n"
            "30 RETURN v + UK\n"
            "40 .ENDSUB\n",
            encoding="utf-8",
        )
        (temp_dir / "src" / "core.sa").write_text(
            "10 USE UTIL AS U\n"
            "20 SUB run(v AS NUM AS DOUBLE) AS PUBLIC AS NUM AS DOUBLE\n"
            "30 RETURN U.bump(v) * 2.0\n"
            "40 .ENDSUB\n",
            encoding="utf-8",
        )
        spkg = pack_spkg(temp_dir / "src", temp_dir / "demo.spkg", package_name="demo")
        (temp_dir / "main.sa").write_text(
            "10 USE DEMO.CORE AS C\n"
            "15 USE DEMO.UTIL AS U\n"
            "20 DIM answer AS NUM AS DOUBLE AS VAR\n"
            "30 SUB main AS PUBLIC AS VOID\n"
            "40 answer = C.run(U.bump(1.0))\n"
            "50 PRINT answer\n"
            "60 .ENDSUB\n"
            "70 CALL main\n"
            "80 END\n",
            encoding="utf-8",
        )

        plan = compile_project(temp_dir / "main.sa", temp_dir / "out", spkgs=[spkg])
        # 修好前会同时出现 util 和 demo.util 两份同源模块
        assert set(plan.modules) == {"demo.core", "demo.util"}


@requires_gcc
def test_intra_package_module_runs_end_to_end() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        (temp_dir / "src").mkdir()
        (temp_dir / "src" / "util.sa").write_text(
            "10 CONST UK AS NUM AS DOUBLE = 7.0\n"
            "20 SUB bump(v AS NUM AS DOUBLE) AS PUBLIC AS NUM AS DOUBLE\n"
            "30 RETURN v + UK\n"
            "40 .ENDSUB\n",
            encoding="utf-8",
        )
        (temp_dir / "src" / "core.sa").write_text(
            "10 USE UTIL AS U\n"
            "20 SUB run(v AS NUM AS DOUBLE) AS PUBLIC AS NUM AS DOUBLE\n"
            "30 RETURN U.bump(v) * 2.0\n"
            "40 .ENDSUB\n",
            encoding="utf-8",
        )
        spkg = pack_spkg(temp_dir / "src", temp_dir / "demo.spkg", package_name="demo")
        (temp_dir / "main.sa").write_text(
            "10 USE DEMO.CORE AS C\n"
            "20 DIM answer AS NUM AS DOUBLE AS VAR\n"
            "30 SUB main AS PUBLIC AS VOID\n"
            "40 answer = C.run(1.0)\n"
            "50 PRINT answer\n"
            "60 .ENDSUB\n"
            "70 CALL main\n"
            "80 END\n",
            encoding="utf-8",
        )
        result = build_exe(temp_dir / "main.sa", temp_dir / "main.exe", keep_c=False, spkgs=[spkg])
        proc = subprocess.run([str(result.exe_path)], capture_output=True, text=True)
        assert proc.stdout.strip() == "16"


def test_module_name_normalization_collision_is_rejected() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        (temp_dir / "a_b.sa").write_text(
            "10 SUB ping AS PUBLIC AS NUM AS DOUBLE\n20 RETURN 1.0\n30 .ENDSUB\n", encoding="utf-8"
        )
        (temp_dir / "main.sa").write_text(
            "10 USE A.B AS P\n"
            "15 USE A_B AS Q\n"
            "20 DIM answer AS NUM AS DOUBLE AS VAR\n"
            "30 SUB main AS PUBLIC AS VOID\n"
            "40 answer = P.ping() + Q.ping()\n"
            "50 .ENDSUB\n"
            "60 CALL main\n"
            "70 END\n",
            encoding="utf-8",
        )
        with pytest.raises(SonCompileError) as excinfo:
            compile_project(temp_dir / "main.sa", temp_dir / "out")
        assert "归一化后都是" in str(excinfo.value)


def test_extract_spkg_rejects_foreign_format() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        spkg = temp_dir / "fake.spkg"
        with ZipFile(spkg, "w", ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps({"format": "not-spkg", "version": 1}))
            archive.writestr("packages/x/src/__init__.sa", "10 END\n")
        with pytest.raises(SonCompileError) as excinfo:
            extract_spkg(spkg, temp_dir / "out")
        assert "不支持的 .spkg 格式" in str(excinfo.value)
        assert not (temp_dir / "out" / "packages").exists()


def test_extract_spkg_rejects_unknown_version() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        spkg = temp_dir / "future.spkg"
        with ZipFile(spkg, "w", ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps({"format": "sonalgebraic-spkg", "version": 99}))
        with pytest.raises(SonCompileError) as excinfo:
            extract_spkg(spkg, temp_dir / "out")
        assert "不支持的 .spkg 版本" in str(excinfo.value)


def test_extract_spkg_rejects_non_zip() -> None:
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        spkg = temp_dir / "junk.spkg"
        spkg.write_bytes(b"definitely not a zip")
        with pytest.raises(SonCompileError) as excinfo:
            extract_spkg(spkg, temp_dir / "out")
        assert "不是合法的 zip 包" in str(excinfo.value)


def test_hash_entry_resolution_ignores_decoy_outside_package_dir() -> None:
    """hash 条目的落位是纯字符串规则，不会因为根目录有同名诱饵文件而改判。"""
    with TemporaryDirectory(prefix="sonalgebraic-test-") as temp:
        temp_dir = Path(temp)
        spkg = temp_dir / "decoy.spkg"
        payload = b"10 SUB noop AS PUBLIC AS VOID\n20 .ENDSUB\n"
        manifest = {
            "format": "sonalgebraic-spkg",
            "version": 1,
            "package": {"name": "mathlib"},
            "modules": [{"name": "MATHLIB", "package": "mathlib", "source": "src/__init__.sa", "binaries": {}}],
            "module_to_package": {"MATHLIB": "mathlib"},
            "hashes": {"src/__init__.sa": f"sha256:{hashlib.sha256(payload).hexdigest()}"},
        }
        with ZipFile(spkg, "w", ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            # 只有根目录的诱饵，真正会被编译的 packages/mathlib/... 根本不存在
            archive.writestr("src/__init__.sa", payload)

        with pytest.raises(SonCompileError) as excinfo:
            extract_spkg(spkg, temp_dir / "out")
        assert "hash 文件不存在" in str(excinfo.value)
