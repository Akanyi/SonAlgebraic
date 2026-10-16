"""函数实例身份：本地定义、跨实例导入、callable 值，三种身份不能混成一种。

`FunctionBinding` 把「这个名字在当前实例里指向什么」拆成两半：定义它的实例
（`defining_instance`）和观察它的实例（`observing_instance`）。本地 SUB 是 REAL，带着
函数体；USE 进来的模块 SUB 是 NAMED_VIRTUAL，只有签名——函数体在另一个编译单元里，
链接时才对上；callable 值是 ANONYMOUS_VIRTUAL，运行期才知道指向谁，连定义实例都没有。

这组测试守的是「导入名字 ≠ 取得定义」这条线。定义一旦跟着导入流进来，codegen 就可能
在当前单元里再发射一份函数体（链接期重复定义），分析阶段也能顺手「看穿」别的实例的
实现——那正是模块边界要挡住的东西。

恢复说明：原文件在 10 月初丢失，测试侧没有字节码可比对。这份是按幸存的 pytest 节点名
配合恢复后的 `core/scopes.py` 重建的，不是逐字找回。详见 `.recovery/RECOVERY.md`。
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from sonalgebraic.analysis.exports import collect_exports
from sonalgebraic.analysis.semantics import Symbol, check_program
from sonalgebraic.analysis.typesys import callable_binding
from sonalgebraic.core import ast
from sonalgebraic.core.errors import SonCompileError
from sonalgebraic.core.module_model import ModuleExports
from sonalgebraic.core.names import module_header_name
from sonalgebraic.core.scopes import FunctionBinding, FunctionIdentity, build_instance_scope, resolve_function_binding
from sonalgebraic.frontend.parser import parse_program

_LONG = ast.TypeSpec("NUM", "LONG")

_LIBRARY = (
    "10 SUB twice(x AS NUM AS LONG) AS PUBLIC AS NUM AS LONG\n"
    "20 RETURN x * 2\n"
    "30 .ENDSUB\n"
    "40 SUB secret(x AS NUM AS LONG) AS PRIVATE AS NUM AS LONG\n"
    "50 RETURN x + 1\n"
    "60 .ENDSUB\n"
)


def _library_subs() -> dict[str, ast.Subroutine]:
    """模块源码解析出的完整 SUB（带函数体），不经过 collect_exports 的裁剪。"""
    return {sub.name.lower(): sub for sub in parse_program(_LIBRARY).subs}


def _injected_exports(*names: str) -> ModuleExports:
    """绕过 collect_exports 直接塞进 subs 表的导出——模拟手写或被篡改的 .slib 元数据。

    正常路径上 collect_exports 已经去掉了函数体、滤掉了 PRIVATE，所以只靠它的话下面
    这些断言永远是真的，证明不了实例边界自己守得住。
    """
    subs = _library_subs()
    return ModuleExports(module="LIB", header_name=module_header_name("LIB"),
                         subs={name: subs[name] for name in names})


def _main(body: str) -> str:
    return ("10 USE LIB AS L\n20 DIM r AS NUM AS LONG AS VAR\n30 SUB main AS PUBLIC AS VOID\n"
            + body + "\n900 .ENDSUB\n910 CALL main\n920 END\n")


def test_callable_binding_is_anonymous_virtual() -> None:
    """callable 值没有名字可查，指向哪个函数是运行期的事。

    所以它既不是 REAL（没有可以发射的函数体），也不能冒充 NAMED_VIRTUAL（没有一个
    能在链接期对上的符号）。defining_instance 必须留空：填成观察者的实例，就等于声称
    这个函数定义在本单元里。
    """
    signature = ast.TypeSpec("SUB", inner=_LONG, params=(ast.Param("x", _LONG, False, 0),))
    symbols = {"h": Symbol("h", signature, True)}
    binding = callable_binding("h", symbols, {}, 10, "consumer")
    assert binding is not None
    assert binding.identity is FunctionIdentity.ANONYMOUS_VIRTUAL
    assert binding.defining_instance is None
    assert binding.observing_instance == "consumer"
    assert binding.declaration is None and binding.definition is None
    assert binding.origin == "callable"
    # 调用检查只认签名，签名必须原样带过来
    assert binding.signature == signature
    # 函数引用 PTR TO SUB(...) 同样是匿名的：它是一个值，不是一个名字
    pointer = ast.TypeSpec("PTR", inner=signature)
    via_pointer = callable_binding("p", {"p": Symbol("p", pointer, True)}, {}, 10, "consumer")
    assert via_pointer is not None
    assert via_pointer.identity is FunctionIdentity.ANONYMOUS_VIRTUAL
    assert via_pointer.signature == signature
    # 不是 callable 的变量不产生绑定，调用点要落回自己的「未知 SUB」诊断
    assert callable_binding("n", {"n": Symbol("n", _LONG, True)}, {}, 10, "consumer") is None


def test_checked_instance_uses_declaration_only_imports() -> None:
    """检查后的实例里，本地 SUB 是带函数体的定义，导入的 SUB 只有声明。

    导出这里故意带着函数体注入：哪怕元数据里有实现，实例也只能拿到签名。
    """
    library = _injected_exports("twice")
    assert library.subs["twice"].body, "前提：注入的导出确实带着函数体"
    checked = check_program(parse_program(_main("40 r = CALL L.twice(21)")), {"l": library})
    scope = checked.instance_scope
    assert scope is not None and scope.name == "<main>"
    # 旧调用路径按字典读本地定义，它和实例必须是同一份，不能各存一套
    assert checked.subs.instance is scope

    local = scope.resolve("main")
    assert local is not None
    assert local.identity is FunctionIdentity.REAL
    assert local.defining_instance == local.observing_instance == "<main>"
    assert local.definition is checked.program.subs[0]

    imported = scope.resolve("L.twice")
    assert imported is not None
    assert imported.identity is FunctionIdentity.NAMED_VIRTUAL
    assert imported.defining_instance == "lib"
    assert imported.observing_instance == "<main>"
    assert imported.definition is None
    assert isinstance(imported.declaration, ast.Subroutine)
    assert imported.declaration.body == []
    # 只剩声明，签名照旧完整，调用检查不受影响
    assert imported.signature.inner == _LONG
    assert [param.name for param in imported.signature.params] == ["x"]
    # 导入的名字不进本地定义表：它在这个实例里没有函数体可发射
    assert "l.twice" not in checked.subs
    assert set(checked.runtime_scope.instances) == {"<main>", "lib"}


def test_importing_real_binding_never_carries_definition() -> None:
    """`imported()` 是 REAL 跨到另一个实例的唯一通道，过了这道门就只剩签名。"""
    sub = _library_subs()["twice"]
    signature = ast.TypeSpec("SUB", inner=sub.return_type, params=tuple(sub.params))
    real = FunctionBinding("twice", "lib", "lib", FunctionIdentity.REAL, signature, sub, sub)
    assert real.definition is sub and real.declaration.body

    seen = real.imported("L.twice", "consumer")
    assert seen.identity is FunctionIdentity.NAMED_VIRTUAL
    assert seen.definition is None
    assert seen.declaration.body == []
    # 去掉的只有函数体：名字、行号、参数、返回类型都是声明的一部分
    assert seen.declaration == replace(sub, body=[])
    assert seen.signature == signature
    # 定义在哪没变，变的是谁在看、用什么名字看
    assert seen.defining_instance == "lib"
    assert seen.observing_instance == "consumer"
    assert seen.name == "L.twice"
    # 原绑定不受影响：同一个 REAL 还要留给它自己的实例用
    assert real.identity is FunctionIdentity.REAL and real.definition is sub

    # 再导入一层也一样，不会因为中转而把定义找回来
    again = seen.imported("M.twice", "third")
    assert again.definition is None and again.declaration.body == []
    assert again.defining_instance == "lib"

    # C 函数声明本来就没有函数体，导入后照样只是声明
    c_decl = ast.CFunctionDecl("C", "abs", [ast.Param("x", _LONG, False, 0)], _LONG, 0)
    c_binding = FunctionBinding("C.abs", "c:c", "<main>", FunctionIdentity.NAMED_VIRTUAL, signature, c_decl, origin="c")
    assert c_binding.imported("X.abs", "other").declaration is c_decl


def test_private_declaration_cannot_be_imported_by_injected_export() -> None:
    """PRIVATE 由实例边界自己挡，不能只指望 collect_exports 没把它写进导出表。

    导出元数据是磁盘上的文件（.slib），被手写、被旧版本生成、被篡改都有可能。只要
    塞进 subs 表就能调用的话，PRIVATE 就成了一个「请勿调用」的建议。
    """
    # 正常导出路径确实会滤掉它——这是前提，不是被测对象
    assert set(collect_exports("LIB", parse_program(_LIBRARY)).subs) == {"twice"}

    library = _injected_exports("twice", "secret")
    modules = {"l": library}
    _, scope = build_instance_scope(parse_program(_main("40 PRINT 1")), modules, {})
    assert scope.resolve("L.twice") is not None
    assert scope.resolve("L.secret") is None
    assert resolve_function_binding("L.secret", {}, modules, {}) is None
    assert resolve_function_binding("L.twice", {}, modules, {}) is not None

    # 语句调用和表达式调用都只能报「找不到」，而不是放行或给出 PRIVATE 函数的签名
    with pytest.raises(SonCompileError, match="未知 SUB"):
        check_program(parse_program(_main("40 CALL L.secret(1)")), modules)
    with pytest.raises(SonCompileError):
        check_program(parse_program(_main("40 r = CALL L.secret(1)")), modules)
    # 同一份注入导出里的 PUBLIC 成员照常可用：挡的是可见性，不是整个模块
    check_program(parse_program(_main("40 r = CALL L.twice(1)")), modules)
