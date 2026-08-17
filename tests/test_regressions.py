"""审计发现的缺陷回归。

每个用例对应一个曾经「check 说没问题、结果却是错的」的路径，
所以断言尽量落在可观察行为上（真编译、真运行），而不是生成的 C 字符串。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from conftest import REPO_ROOT, build_temp, expect_error, requires_c_compiler
from sonalgebraic.analysis.diagnostics import Diagnostic, render_diagnostics
from sonalgebraic.backend import c_runtime
from sonalgebraic.core.errors import SonCompileError
from sonalgebraic.driver.compiler import build_exe, compile_to_c
from sonalgebraic.driver.formatter import renumber_source
from sonalgebraic.packaging.spkg import _safe_member_path, pack_spkg
from sonalgebraic.packaging.toolchain import link_library_args


def _sonc(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "sonalgebraic", *args],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


# --------------------------------------------------------------------------
# runtime 头文件与实现的一致性
# --------------------------------------------------------------------------


def test_runtime_header_declares_every_function_codegen_emits() -> None:
    """模块模式下生成的 .c 只 include sa_runtime.h。

    头文件漏声明不会让生成 C 失败，而是变成隐式声明——现代 GCC 直接报错，
    老编译器则把 SaHandle 返回值按 int 截断。这条防的就是头文件再次漂移。
    """
    emitted: set[str] = set()
    # 递归扫整个 backend 包而不是列举文件名：native 后端已经拆成多个模块，
    # 写死列表的话下次再拆就会静默漏扫，这条防线自己先失效。
    # c_runtime.py 除外——那是 runtime 的 C 实现本身，里面大量 static helper
    # 本来就不该出现在头文件里，扫进来只会得到一堆假阳性。
    for path in sorted((REPO_ROOT / "sonalgebraic" / "backend").rglob("*.py")):
        if path.name == "c_runtime.py":
            continue
        text = path.read_text(encoding="utf-8")
        emitted |= set(re.findall(r"\bsa_[a-z0-9_]+(?=\()", text))

    defined = set(re.findall(r"\bsa_[a-z0-9_]+(?=\s*\()", c_runtime.RUNTIME))
    declared = set(re.findall(r"\bsa_[a-z0-9_]+(?=\s*\()", c_runtime.RUNTIME_HEADER))

    missing = sorted(name for name in emitted & defined if name not in declared)
    assert not missing, f"RUNTIME_HEADER 缺少这些函数的声明: {missing}"


def test_stricmp_shim_precedes_first_use() -> None:
    """预处理按文本顺序展开：垫片排在使用点之后，POSIX 上 SYS.FILE 直接编不过。"""
    lines = c_runtime.RUNTIME.split("\n")
    macro_line = next(i for i, line in enumerate(lines) if line.strip().startswith("#define _stricmp"))
    call_lines = [
        i
        for i, line in enumerate(lines)
        if re.search(r"_stricmp\s*\(", line)
        and not line.strip().startswith(("/*", "*", "//"))
        and "sa_stricmp_ascii(const" not in line
    ]
    assert call_lines, "没找到 _stricmp 调用点，测试本身需要更新"
    assert min(call_lines) > macro_line, "存在早于 #define _stricmp 的调用，POSIX 构建会失败"


def test_symbol_deriv_covers_every_function_eval_supports() -> None:
    """eval/simplify 支持而 deriv 不支持的函数会静默返回导数 0。"""
    source = c_runtime.RUNTIME
    deriv_body = source[source.index("static SaSymbol sa_symbol_deriv") : source.index("static SaSymbol sa_symbol_simplify")]
    eval_body = source[source.index("static double sa_symbol_eval") :]
    eval_body = eval_body[: eval_body.index("\n}")]

    eval_funcs = set(re.findall(r'strcmp\(s->text,\s*"([A-Z]+)"\)', eval_body))
    deriv_funcs = set(re.findall(r'strcmp\(s->text,\s*"([A-Z]+)"\)', deriv_body))

    missing = sorted(eval_funcs - deriv_funcs)
    assert not missing, f"这些函数能求值却不能求导，会静默返回 0: {missing}"


# --------------------------------------------------------------------------
# 返回路径与作用域
# --------------------------------------------------------------------------


def test_goto_skipping_return_is_rejected() -> None:
    """标签是控制流汇合点，倒序扫描不能把它当透明跳过。"""
    expect_error(
        "10 SUB f AS PUBLIC AS NUM AS LONG\n20 GOTO ::done\n30 RETURN 1\n40 ::done\n50 .ENDSUB\n"
        "60 SUB main AS PUBLIC AS VOID\n70 PRINT f()\n80 .ENDSUB\n90 CALL main\n100 END",
        "必须保证所有明显路径 RETURN",
    )


def test_label_before_return_still_counts_as_returning() -> None:
    """标签排在 RETURN 之前时，跳过来照样会撞上 RETURN，不该误报。"""
    from sonalgebraic.analysis.semantics import check_program
    from sonalgebraic.frontend.parser import parse_program

    check_program(parse_program(
        "10 DIM x AS NUM AS LONG AS VAR\n"
        "20 SUB f AS PUBLIC AS NUM AS LONG\n30 IF x > 0 THEN\n40 GOTO ::done\n50 END IF\n"
        "60 ::done\n70 RETURN 1\n80 .ENDSUB\n"
        "90 SUB main AS PUBLIC AS VOID\n100 x = 1\n110 PRINT f()\n120 .ENDSUB\n130 CALL main\n140 END"
    ))


def test_unreferenced_trailing_label_does_not_false_positive() -> None:
    from sonalgebraic.analysis.semantics import check_program
    from sonalgebraic.frontend.parser import parse_program

    check_program(parse_program(
        "10 SUB f AS PUBLIC AS NUM AS LONG\n20 RETURN 1\n30 ::unused\n40 .ENDSUB\n"
        "50 SUB main AS PUBLIC AS VOID\n60 PRINT f()\n70 .ENDSUB\n80 CALL main\n90 END"
    ))


def test_block_local_is_not_visible_after_block() -> None:
    """块内 DIM 的 C 声明发射在 `{ }` 里，语义侧必须同样隔离。"""
    expect_error(
        "10 DIM flag AS BOOL AS VAR\n20 SUB main AS PUBLIC AS VOID\n30 flag = TRUE\n"
        "40 IF flag THEN\n50 DIM inner AS NUM AS LONG AS VAR\n60 inner = 5\n70 END IF\n"
        "80 PRINT inner\n90 .ENDSUB\n100 CALL main\n110 END",
        "变量未声明: inner",
    )


def test_sibling_branches_may_declare_the_same_name() -> None:
    from sonalgebraic.analysis.semantics import check_program
    from sonalgebraic.frontend.parser import parse_program

    check_program(parse_program(
        "10 DIM flag AS BOOL AS VAR\n20 SUB main AS PUBLIC AS VOID\n30 flag = TRUE\n"
        "40 IF flag THEN\n50 DIM tmp AS NUM AS LONG AS VAR\n60 tmp = 1\n"
        "70 ELSE\n80 DIM tmp AS NUM AS LONG AS VAR\n90 tmp = 2\n100 END IF\n"
        "110 .ENDSUB\n120 CALL main\n130 END"
    ))


def test_shadowing_an_outer_local_is_still_rejected() -> None:
    expect_error(
        "10 DIM flag AS BOOL AS VAR\n20 SUB main AS PUBLIC AS VOID\n30 DIM v AS NUM AS LONG AS VAR\n"
        "40 flag = TRUE\n50 IF flag THEN\n60 DIM v AS NUM AS LONG AS VAR\n70 v = 1\n80 END IF\n"
        "90 .ENDSUB\n100 CALL main\n110 END",
        "重复声明变量: v",
    )


def test_return_value_inside_catch_of_void_sub_is_rejected() -> None:
    """check_return 以前不递归 TryCatch，生成的 C 字面上是 `void tmp = 42;`。"""
    expect_error(
        "10 DIM trap AS ERROR AS VAR\n20 SUB risky AS VOID\n30 THROW NEW ERR_TEST, \"boom\"\n40 .ENDSUB\n"
        "50 SUB main AS PUBLIC AS VOID\n60 TRY CALL risky TRACEBACK ERROR AS trap\n"
        "70 CATCH ERR_TEST AS e\n80 RETURN 42\n90 .ENDTRY\n100 .ENDSUB\n110 CALL main\n120 END",
        "VOID SUB 不能 RETURN 值",
    )


def test_wrong_return_type_inside_catch_is_rejected() -> None:
    expect_error(
        "10 DIM trap AS ERROR AS VAR\n20 SUB risky AS VOID\n30 THROW NEW ERR_TEST, \"boom\"\n40 .ENDSUB\n"
        "50 SUB f AS PUBLIC AS NUM AS LONG\n60 TRY CALL risky TRACEBACK ERROR AS trap\n"
        "70 CATCH ERR_TEST AS e\n80 RETURN \"nope\"\n90 .ENDTRY\n100 RETURN 0\n110 .ENDSUB\n"
        "120 SUB main AS PUBLIC AS VOID\n130 PRINT f()\n140 .ENDSUB\n150 CALL main\n160 END",
        "类型不兼容",
    )


# --------------------------------------------------------------------------
# 内置转换与数组下标
# --------------------------------------------------------------------------


def test_number_builtin_rejects_non_string_argument() -> None:
    """runtime 签名是 sa_number(const char*)，传数值等于把整数当地址解引用。"""
    expect_error(
        "10 DIM n AS NUM AS LONG AS VAR\n20 DIM d AS NUM AS DOUBLE AS VAR\n"
        "30 SUB main AS PUBLIC AS VOID\n40 n = 42\n50 d = NUMBER(n)\n60 .ENDSUB\n70 CALL main\n80 END",
        "NUMBER() 的参数必须是 STRING",
    )


def test_number_builtin_rejects_wrong_arity_at_check_time() -> None:
    expect_error(
        "10 DIM s AS STRING AS VAR\n20 SUB main AS PUBLIC AS VOID\n30 s = \"1\"\n"
        "40 PRINT NUMBER(s, s)\n50 .ENDSUB\n60 CALL main\n70 END",
        "需要 1 个参数",
    )


@pytest.mark.parametrize("index", ["5", "-1"])
def test_constant_array_index_out_of_range_is_rejected(index: str) -> None:
    """编译期完全可判定，放过去就是裸 sa_xs[5] 静默踩内存。"""
    expect_error(
        f"10 SUB main AS PUBLIC AS VOID\n20 DIM xs[3] AS NUM AS LONG AS VAR\n30 xs[{index}] = 1\n"
        "40 .ENDSUB\n50 CALL main\n60 END",
        "数组下标越界",
    )


def test_in_range_constant_index_is_accepted() -> None:
    from sonalgebraic.analysis.semantics import check_program
    from sonalgebraic.frontend.parser import parse_program

    check_program(parse_program(
        "10 SUB main AS PUBLIC AS VOID\n20 DIM xs[3] AS NUM AS LONG AS VAR\n"
        "30 xs[0] = 7\n40 xs[2] = 9\n50 PRINT xs[2]\n60 .ENDSUB\n70 CALL main\n80 END"
    ))


# --------------------------------------------------------------------------
# 包系统安全
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["-fplugin=./evil.so", "-Wl,-rpath,/tmp/x.so", "-O3", "--param=x.a"],
)
def test_uselib_rejects_compiler_flags(value: str) -> None:
    """USELIB 的值来自模块源码，原样进命令行等于构建期任意代码执行。"""
    with pytest.raises(SonCompileError, match="USELIB"):
        link_library_args([value])


@pytest.mark.parametrize("value", ["m", "curl", "stdc++"])
def test_uselib_accepts_plain_library_names(value: str) -> None:
    assert link_library_args([value]) == [f"-l{value}"]


@pytest.mark.parametrize("member", ["packages/x/src/NUL", "packages/x/src/CON.sa", "packages/COM1/a.sa"])
def test_spkg_rejects_windows_device_names(member: str, tmp_path: Path) -> None:
    with pytest.raises(SonCompileError, match="不安全路径"):
        _safe_member_path(tmp_path, member)


def test_spkg_allows_names_merely_prefixed_by_device_name(tmp_path: Path) -> None:
    assert _safe_member_path(tmp_path, "packages/x/src/console.sa").name == "console.sa"


def test_spkg_packs_a_directory(tmp_path: Path) -> None:
    """目录分支以前把原始路径塞进 sa_files，relative_to 必炸 ValueError。"""
    src = tmp_path / "mypkg"
    (src / "sub").mkdir(parents=True)
    (src / "util.sa").write_text("10 CONST K AS NUM AS LONG = 7\n", encoding="utf-8")
    (src / "sub" / "extra.sa").write_text("10 CONST J AS NUM AS LONG = 3\n", encoding="utf-8")

    out = pack_spkg(src, tmp_path / "mypkg.spkg")

    with zipfile.ZipFile(out) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        names = set(archive.namelist())

    assert "packages/mypkg/src/util.sa" in names
    assert "packages/mypkg/src/sub/extra.sa" in names
    assert {item["name"] for item in manifest["modules"]} == {"MYPKG.UTIL", "MYPKG.SUB.EXTRA"}
    # 每个模块源码都必须被 hash 覆盖，否则校验形同虚设
    assert set(manifest["hashes"]) == {"src/util.sa", "src/sub/extra.sa"}


def test_spkg_rejects_module_source_without_declared_hash(tmp_path: Path) -> None:
    """只验 manifest 声明的条目，清空 hashes 就能让源码零校验参与编译。"""
    from sonalgebraic.packaging.spkg import extract_spkg

    src = tmp_path / "pkg"
    src.mkdir()
    (src / "util.sa").write_text("10 CONST K AS NUM AS LONG = 7\n", encoding="utf-8")
    packed = pack_spkg(src, tmp_path / "pkg.spkg")

    tampered = tmp_path / "tampered.spkg"
    with zipfile.ZipFile(packed) as zin, zipfile.ZipFile(tampered, "w") as zout:
        manifest = json.loads(zin.read("manifest.json"))
        manifest["hashes"] = {}
        for item in zin.infolist():
            if item.filename == "manifest.json":
                zout.writestr("manifest.json", json.dumps(manifest))
            else:
                zout.writestr(item.filename, zin.read(item))

    with pytest.raises(SonCompileError, match="缺少 hash 声明"):
        extract_spkg(tampered, tmp_path / "out")


# --------------------------------------------------------------------------
# CLI 与诊断体验
# --------------------------------------------------------------------------


def test_missing_source_file_reports_error_not_traceback(tmp_path: Path) -> None:
    result = _sonc("check", str(tmp_path / "nope.sa"))
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "找不到文件" in result.stderr


def test_diagnostics_survive_a_pipe_without_mojibake(tmp_path: Path) -> None:
    """Windows 上流被重定向时会退回本地代码页，中文诊断全变乱码。"""
    source = tmp_path / "bad.sa"
    source.write_text(
        "10 SUB main AS PUBLIC AS VOID\n20 PRINT missing\n30 .ENDSUB\n40 CALL main\n50 END\n",
        encoding="utf-8",
    )
    result = _sonc("check", str(source))
    assert "变量未声明" in result.stderr


def test_dependency_module_error_points_at_the_module_file(tmp_path: Path) -> None:
    """以前这个错误被安在主文件的同号行上，文件/行/下划线三者全错。"""
    (tmp_path / "badmod.sa").write_text(
        "10 CONST A AS NUM AS LONG = 1\n20 SUB helper AS PUBLIC AS VOID\n"
        "30 PRINT undeclared_thing\n40 .ENDSUB\n",
        encoding="utf-8",
    )
    main_sa = tmp_path / "mainapp.sa"
    main_sa.write_text(
        "10 USE BADMOD AS B\n20 DIM x AS NUM AS LONG AS VAR\n30 SUB main AS PUBLIC AS VOID\n"
        "40 x = 1\n50 .ENDSUB\n60 CALL main\n70 END\n",
        encoding="utf-8",
    )

    result = _sonc("check", str(main_sa))
    assert result.returncode == 1
    # 冒号位置是物理行（编辑器跳转靠它），SA 逻辑行号在消息里的 [SA n]
    assert "badmod.sa:3:10" in result.stderr
    assert "[SA 30]" in result.stderr
    assert "PRINT undeclared_thing" in result.stderr
    assert "mainapp.sa" not in result.stderr


def test_diagnostic_uses_origin_file_when_present() -> None:
    module_text = "10 CONST A AS NUM AS LONG = 1\n20 SUB helper AS PUBLIC AS VOID\n30 PRINT oops\n40 .ENDSUB\n"
    error = SonCompileError("变量未声明: oops", 30, origin_path="dep.sa", origin_text=module_text)
    rendered = render_diagnostics("main.sa", "10 REM main\n", [Diagnostic.from_compile_error(error)])

    assert "dep.sa:3:10" in rendered
    assert "[SA 30]" in rendered
    assert "30 PRINT oops" in rendered
    assert "main.sa" not in rendered


def test_fmt_numbers_a_none_number_source() -> None:
    """fmt 以前拒收无行号源码，而它正是最该负责补号的场景。"""
    source = "USE SYS.LINT AS NONE_NUMBER\nSUB main AS PUBLIC AS VOID\nPRINT 1\n.ENDSUB\nCALL main\nEND\n"
    result = renumber_source(source, step=10)

    assert result.splitlines() == [
        "10 USE SYS.LINT AS NONE_NUMBER",
        "20 SUB main AS PUBLIC AS VOID",
        "30 PRINT 1",
        "40 .ENDSUB",
        "50 CALL main",
        "60 END",
    ]
    assert renumber_source(result, step=10) == result  # 幂等


@requires_c_compiler
@pytest.mark.e2e
def test_run_passes_arguments_after_double_dash() -> None:
    """`--` 之后的东西转发给程序，但 --backend 这类编译器选项不能被一起吞掉。"""
    result = _sonc("run", str(REPO_ROOT / "examples" / "hello.sa"), "--backend", "c", "--", "extra")
    assert result.returncode == 0
    assert "Hello World!" in result.stdout


_TWO_INPUTS_SOURCE = """10 USE SYS.IO AS CONSOLE
20 SUB main AS PUBLIC AS VOID
30 DIM name AS STRING AS VAR
40 DIM city AS STRING AS VAR
50 DIM age AS NUM AS LONG AS VAR
60 CONSOLE.INPUT "name: ", name
70 CONSOLE.INPUT "city: ", city
80 CONSOLE.INPUT "age: ", age
90 PRINT F"{name} {city} {age}"
100 .ENDSUB
110 CALL main
120 END
"""


def test_repeated_input_gets_distinct_buffers(tmp_path: Path) -> None:
    """同一个块里读两次输入，以前生成两个同名的 char[4096]，C 编译器直接拒绝。

    缓冲区名曾经硬编码成 sa_input_buf，所以任何「先问名字再问年龄」的程序都编不过。
    native 后端用 alloca 天然唯一，只有 C 后端踩这个坑。
    """
    source = tmp_path / "two_inputs.sa"
    source.write_text(_TWO_INPUTS_SOURCE, encoding="utf-8")
    generated = tmp_path / "two_inputs.c"
    compile_to_c(source, generated)

    buffers = re.findall(r"char (\w+)\[4096\];", generated.read_text(encoding="utf-8"))
    assert len(buffers) == 3, f"三条 INPUT 应各自声明缓冲区，实际 {buffers}"
    assert len(set(buffers)) == 3, f"缓冲区重名: {buffers}"


@requires_c_compiler
@pytest.mark.e2e
def test_repeated_input_compiles_and_runs(tmp_path: Path) -> None:
    """光比对生成的 C 不够硬——真正的判据是 C 编译器收不收。"""
    source = tmp_path / "two_inputs.sa"
    source.write_text(_TWO_INPUTS_SOURCE, encoding="utf-8")
    exe = tmp_path / ("two_inputs.exe" if sys.platform == "win32" else "two_inputs")
    build_exe(source, exe, keep_c=False)

    proc = subprocess.run([str(exe)], input="LANS\nHangzhou\n30\n", text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    assert "LANS Hangzhou 30" in proc.stdout


# --------------------------------------------------------------------------
# native 后端与 C 后端的一致性
# --------------------------------------------------------------------------


def test_native_backend_rejects_float_subtype() -> None:
    """以前 FLOAT 落进 i64 分支，1.5 被静默存成 1。"""
    from sonalgebraic.backend.native import generate_native_llvm_ir
    from sonalgebraic.analysis.semantics import check_program
    from sonalgebraic.frontend.parser import parse_program

    checked = check_program(parse_program(
        "10 DIM f AS NUM AS FLOAT AS VAR\n20 SUB main AS PUBLIC AS VOID\n30 f = 1.5\n"
        "40 PRINT f\n50 .ENDSUB\n60 CALL main\n70 END"
    ))
    with pytest.raises(SonCompileError, match="NUM AS FLOAT"):
        generate_native_llvm_ir(checked)


def test_native_and_or_short_circuit_in_ir() -> None:
    """C 后端用 && / ||；native 若两侧都先求值，守卫条件会照样执行右侧。"""
    from sonalgebraic.backend.native import generate_native_llvm_ir
    from sonalgebraic.analysis.semantics import check_program
    from sonalgebraic.frontend.parser import parse_program

    checked = check_program(parse_program(
        "10 DIM d AS NUM AS LONG AS VAR\n20 DIM total AS NUM AS LONG AS VAR\n"
        "30 SUB main AS PUBLIC AS VOID\n40 d = 0\n50 total = 100\n"
        "60 IF d <> 0 AND total / d > 1 THEN\n70 PRINT 999\n80 END IF\n"
        "90 .ENDSUB\n100 CALL main\n110 END"
    ))
    ir = generate_native_llvm_ir(checked)

    assert "phi i1" in ir, "AND/OR 应当通过分支 + phi 实现短路"
    # 除法必须落在条件分支里，而不是和左侧并排无条件执行
    sdiv_line = next(i for i, line in enumerate(ir.splitlines()) if "sdiv" in line)
    branch_line = next(i for i, line in enumerate(ir.splitlines()) if "br i1" in line)
    assert branch_line < sdiv_line, "sdiv 出现在条件分支之前，说明右侧被无条件求值了"


_DIFFERENTIAL_PROGRAMS = {
    "short_circuit_and": (
        "10 DIM d AS NUM AS LONG AS VAR\n20 DIM total AS NUM AS LONG AS VAR\n"
        "30 SUB main AS PUBLIC AS VOID\n40 d = 0\n50 total = 100\n"
        "60 IF d <> 0 AND total / d > 1 THEN\n70 PRINT 999\n80 ELSE\n90 PRINT 1\n100 END IF\n"
        "110 .ENDSUB\n120 CALL main\n130 END"
    ),
    "short_circuit_or": (
        "10 DIM d AS NUM AS LONG AS VAR\n20 DIM total AS NUM AS LONG AS VAR\n"
        "30 SUB main AS PUBLIC AS VOID\n40 d = 0\n50 total = 100\n"
        "60 IF d = 0 OR total / d > 1 THEN\n70 PRINT 2\n80 END IF\n"
        "90 .ENDSUB\n100 CALL main\n110 END"
    ),
    "nested_logic": (
        "10 DIM a AS NUM AS LONG AS VAR\n20 DIM b AS NUM AS LONG AS VAR\n"
        "30 SUB main AS PUBLIC AS VOID\n40 a = 0\n50 b = 3\n"
        "60 IF a <> 0 AND b <> 0 AND b / a > 1 THEN\n70 PRINT 8\n80 ELSE\n90 PRINT 9\n100 END IF\n"
        "110 .ENDSUB\n120 CALL main\n130 END"
    ),
    "arithmetic": (
        "10 DIM x AS NUM AS DOUBLE AS VAR\n20 SUB main AS PUBLIC AS VOID\n"
        "30 x = 7.5\n40 PRINT x * 2.0\n50 PRINT 10 / 4\n60 .ENDSUB\n70 CALL main\n80 END"
    ),
}


@requires_c_compiler
@pytest.mark.e2e
@pytest.mark.parametrize("name", sorted(_DIFFERENTIAL_PROGRAMS))
def test_c_and_native_backends_agree(name: str) -> None:
    """两个后端跑同一份源码必须给出同样的输出。

    AND/OR 短路、FLOAT 截断这类偏差属于「两边各自都能跑、结果不同」，
    只对单个后端断言期望值的测试结构性地发现不了。

    产物落在 build/native-tests 而不是 tmp_path：这条也编 native 产物，和
    test_native_backend.py 里那批走同一条工具链，构建位置的约定不该两样。
    """
    from sonalgebraic.driver.compiler import find_native_compiler

    if find_native_compiler() is None:
        pytest.skip("未找到 clang 或 zig，无法编译 native 后端产物")

    with build_temp(f"sonalgebraic-diff-{name}-") as temp:
        work = Path(temp)
        source = work / f"{name}.sa"
        source.write_text(_DIFFERENTIAL_PROGRAMS[name], encoding="utf-8")

        outputs = {}
        for backend in ("c", "native"):
            exe = work / f"{name}_{backend}.exe"
            build = _sonc("build", str(source), "-o", str(exe), "--backend", backend)
            assert build.returncode == 0, f"{backend} 后端构建失败: {build.stderr}"
            run = subprocess.run([str(exe)], capture_output=True, text=True)
            assert run.returncode == 0, f"{backend} 后端运行失败: {run.stderr}"
            outputs[backend] = run.stdout

    assert outputs["c"] == outputs["native"], (
        f"两个后端输出不一致\nC:\n{outputs['c']}\nnative:\n{outputs['native']}"
    )
