"""按需注入运行时的回归。

以前是把整份 RUNTIME 文本塞进生成的 .c，靠 `#ifdef SA_ENABLE_*` 让预处理器裁。
现在改成 Python 侧就只输出够得着的部分：feature 区整块取舍，无条件区按符号依赖
闭包切。这组测试守两件事——**别多塞**（体积断言）和**别少塞**（真编译）。

少塞会直接编译失败而不是静默出错，所以"每个示例都真编一遍"是这里最强的防线，
比任何文本断言都靠谱。
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from conftest import REPO_ROOT, compile_c, requires_c_compiler
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.analysis.typesys import runtime_features_for_program
from sonalgebraic.backend import runtime_slicer as slicer
from sonalgebraic.backend.c_runtime import RUNTIME_IMPL
from sonalgebraic.backend.codegen import generate_c
from sonalgebraic.driver.compiler import find_c_compiler, gc_section_flags
from sonalgebraic.frontend.parser import parse_program

EXAMPLES = REPO_ROOT / "examples"


def _wrap(body: str) -> str:
    return f"10 SUB main AS PUBLIC AS VOID\n{body}\n900 .ENDSUB\n910 CALL main\n920 END\n"


def _example_c(name: str) -> str:
    source = (EXAMPLES / f"{name}.sa").read_text(encoding="utf-8-sig")
    return generate_c(check_program(parse_program(source)))


# --------------------------------------------------------------------------
# 切分器自身的完整性
# --------------------------------------------------------------------------


def test_fragments_cover_the_whole_runtime() -> None:
    """片段拼回去必须是原文。

    切分器一旦漏掉一段，被漏的那些函数就再也不会被注入——而且只有在某个程序
    恰好用到它时才炸。这条把"丢代码"挡在最前面。
    """
    joined = "\n".join(fragment.text for fragment in slicer.fragments())
    content = [line for line in RUNTIME_IMPL.split("\n") if line.strip()]
    assert [line for line in joined.split("\n") if line.strip()] == content


def test_no_fragment_has_a_dangling_dependency() -> None:
    """每个片段引用的运行时符号都得有出处。

    悬空引用意味着闭包会去找一个不存在的提供者，然后静默少注入一个函数。
    这条不用编译就能抓住切分器的推导 bug。
    """
    known = set(slicer.prelude_symbols())
    for fragment in slicer.fragments():
        known |= fragment.provides

    dangling = {
        fragment.name: sorted(fragment.depends - known)
        for fragment in slicer.fragments()
        if fragment.depends - known
    }
    assert not dangling, f"这些片段引用了无处定义的符号: {dangling}"


def test_feature_blocks_are_kept_whole() -> None:
    """7 个 feature 区必须整块存在，不能被切碎。

    它们内部藏着切分器啃不动的东西——BINARY 的 pack/unpack 是宏展开的、
    FILE 的 sa_stricmp_ascii 只通过 #define _stricmp 被引用、NET/TLS 有
    Win 和 POSIX 两份完整实现。整块取舍才安全。
    """
    features = {name for fragment in slicer.fragments() for name in fragment.features}
    assert features == {"net", "file", "desktop", "binary", "list", "map", "gui"}


def test_feature_code_is_not_pulled_in_by_symbol_reference() -> None:
    """没启用的 feature 不能被符号引用拽进来。

    撞个名就把 1600 行 NET 代码拖进去，按需注入就白做了。
    """
    impl = slicer.runtime_impl_for({"sa_net_http_fetch", "sa_file_mode"}, set())
    assert "sa_net_http_fetch" not in impl
    assert "SA_FILE_SLOT_COUNT" not in impl


def test_comments_do_not_inflate_the_closure() -> None:
    """依赖提取前必须剥注释。

    runtime 里注释密度很高且大量提及符号名——sa_str_upper 上面就写着
    「sa_binary_range 同样写法」。不剥的话扫到这个名字会把整个 BINARY 区
    拖进闭包，按需注入直接失效。
    """
    assert slicer.runtime_symbols_in("/* 见 sa_binary_range */ x = 1;") == set()
    assert slicer.runtime_symbols_in("// sa_symbol_deriv 在这里\ny = 2;") == set()
    assert "sa_strdup" in slicer.runtime_symbols_in("char* p = sa_strdup(s);")


# --------------------------------------------------------------------------
# 注入面：该有的有，不该有的没有
# --------------------------------------------------------------------------


def test_hello_world_does_not_carry_symbol_algebra() -> None:
    """SYMBOL 代数是无条件区里最大的一块（约 300 行），却只有用 SYMBOL 的程序要它。

    这正是做按需注入的直接动机。
    """
    c = _example_c("hello")
    assert "sa_symbol_deriv" not in c
    assert "sa_symbol_simplify" not in c
    assert "sa_net_" not in c
    assert "sa_file_open" not in c
    assert "sa_list_new" not in c


def test_symbol_program_still_gets_the_algebra() -> None:
    """反过来：真用 SYMBOL 求导时那套必须在，且连带它依赖的东西一起。"""
    c = _example_c("fluid_symbolic")
    assert "sa_symbol_deriv" in c
    # deriv 引用了定义在它后面的这两个，靠前置声明兜底——闭包必须把它们带上
    assert "sa_symbol_free" in c
    assert "sa_symbol_is_const_value" in c


def test_list_program_gets_list_but_not_net() -> None:
    c = _example_c("lists")
    assert "SA_LIST_SLOT_COUNT" in c
    assert "sa_net_http_fetch" not in c
    assert "sa_symbol_deriv" not in c


def test_prelude_is_always_kept_whole() -> None:
    """PRELUDE 整块保留：类型定义和 setjmp 宏没法按需，拆了只会自找麻烦。"""
    c = _example_c("hello")
    for needle in ("typedef struct", "SA_SETJMP", "SaError", "SaTryFrame"):
        assert needle in c


def test_runtime_shrinks_a_lot() -> None:
    """效果断言。改造前 hello 生成 4193 行，其中 98.6% 是运行时。"""
    assert len(_example_c("hello").split("\n")) < 600


# --------------------------------------------------------------------------
# 最强的防线：每个示例都真编一遍
# --------------------------------------------------------------------------


def _single_file_examples() -> list[str]:
    """能走单文件模式的示例。用户模块类的走另一条路径，由模块模式那条测试覆盖。"""
    names = []
    for path in sorted(EXAMPLES.glob("*.sa")):
        try:
            generate_c(check_program(parse_program(path.read_text(encoding="utf-8-sig"))))
        except Exception:
            continue
        names.append(path.stem)
    return names


@requires_c_compiler
@pytest.mark.parametrize("name", _single_file_examples())
def test_every_example_still_compiles(name: str, tmp_path: Path) -> None:
    """按需注入漏一个函数就是编译失败，不是静默错误——所以真编译是最硬的检查。

    这批示例横跨 STRING / SYMBOL / ERROR / FILE / LIST / MAP / BINARY / NET / GUI，
    等于把 feature 组合和依赖闭包一起覆盖了。
    """
    compiler = find_c_compiler()
    assert compiler is not None
    if compiler == "cl":
        pytest.skip("MSVC 的命令行形状不同，这条只覆盖 gcc/clang 系")

    c_file = tmp_path / f"{name}.c"
    c_file.write_text(_example_c(name), encoding="utf-8")
    proc = subprocess.run(
        [compiler, "-c", "-O2", "-std=c11", "-o", os.devnull, str(c_file)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"{name} 注入不全，编不过:\n{proc.stderr[:1500]}"


@requires_c_compiler
def test_module_mode_runtime_covers_every_translation_unit(tmp_path: Path) -> None:
    """模块模式的 sa_runtime.c 服务所有 TU，根符号必须取全项目并集。

    只按主程序裁的话，模块里用到而主程序没用的函数会缺席，链接期才冒出
    undefined reference。这里刻意让字符串处理只出现在模块侧。
    """
    (tmp_path / "helper.sa").write_text(
        "10 USE SYS.STRING AS STR\n"
        "20 SUB shout AS PUBLIC AS STRING\n"
        '30 RETURN STR.UPPER(STR.CONCAT("hi", "!"))\n'
        "40 .ENDSUB\n",
        encoding="utf-8",
    )
    main_sa = tmp_path / "app.sa"
    main_sa.write_text(
        "10 USE HELPER AS H\n20 SUB main AS PUBLIC AS VOID\n30 PRINT H.shout()\n40 .ENDSUB\n50 CALL main\n60 END\n",
        encoding="utf-8",
    )

    from sonalgebraic.driver.compiler import build_exe

    exe = tmp_path / "app.exe"
    build_exe(main_sa, exe, keep_c=False)
    proc = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert proc.stdout.strip() == "HI!"


# --------------------------------------------------------------------------
# 链接期裁剪的 flag 选择
# --------------------------------------------------------------------------


def test_gc_flags_skip_toolchains_that_reject_them() -> None:
    """tcc 两个 flag 都不认；Windows 上实测是负收益（PE 节区对齐吃掉裁剪量）。"""
    assert gc_section_flags("tcc", "x86_64-linux-gnu") == ([], [])
    assert gc_section_flags("gcc", "x86_64-windows-gnu") == ([], [])


def test_gc_flags_match_the_target_linker() -> None:
    """按目标平台给 flag，不能按宿主——交叉编译时两者不是一回事。"""
    _, mac_link = gc_section_flags("clang", "x86_64-macos")
    assert mac_link == ["-Wl,-dead_strip"]  # Apple ld64 不认 --gc-sections

    _, linux_link = gc_section_flags("gcc", "x86_64-linux-gnu")
    assert linux_link == ["-Wl,--gc-sections"]

    msvc_compile, msvc_link = gc_section_flags("cl", "x86_64-windows-msvc")
    assert "/Gy" in msvc_compile
    assert msvc_link[0] == "/link"  # /OPT:REF 必须排在 /link 之后


# --------------------------------------------------------------------------
# feature 推导与切片的一致性
# --------------------------------------------------------------------------


def test_selected_features_match_the_program() -> None:
    checked = check_program(parse_program(_wrap("20 PRINT 1")))
    assert runtime_features_for_program(checked.program, checked.uses) == set()

    checked = check_program(parse_program("5 USE SYS.MAP AS M\n" + _wrap("20 PRINT 1")))
    # MAP.KEYS() 返回 STR_LIST 句柄，map runtime 直接调 list runtime，必须连带
    assert runtime_features_for_program(checked.program, checked.uses) == {"map", "list"}
