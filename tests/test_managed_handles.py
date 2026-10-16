"""显式托管句柄：`FILE` 这类类型本身、它的析构协议，以及和裸 `HANDLE` 的边界。

裸 `HANDLE AS FILE` 和托管 `FILE` 在 C 里是同一个 `SaHandle`（一个整数 token），所以两者
之间没有任何**表示**上的差别可依赖——区别完全由类型层维持。这组测试守的就是这条线：
托管那一侧必须有完整的析构协议（否则作用域退出时没东西可调），而两侧不能隐式互转
（否则 close 责任会凭空出现或凭空消失，而且两个方向都错得很安静）。

另一半是「谁能改游标」。`F.SIZE` 看着像查询，实际要 seek 到文件尾再恢复，借用者调它
就会动到所有者的位置。所以 effect 表里它和 `READ` / `WRITE` / `SEEK` 一样是 MUTATE，
只有 `TELL` 是真只读——这个区分不做，借用检查就形同虚设。

恢复说明：原文件在 10 月初丢失，测试侧没有字节码可比对。这份是按幸存的 pytest 节点名
配合恢复后的 `analysis/resource_traits.py`、`analysis/builtin_effects.py` 重建的，
不是逐字找回。`test_managed_handle_does_not_implicitly_cross_raw_boundary` 的三组参数
在节点名里只留下 `targetN-sourceN`（非字符串参数），具体类型对是按「两个方向 + 第二个
kind」补的。详见 `.recovery/RECOVERY.md`。
"""
from __future__ import annotations

import re

import pytest

from conftest import compile_user_c
from sonalgebraic.analysis.builtin_effects import ArgumentEffect, builtin_effect
from sonalgebraic.analysis.resource_traits import (
    handle_close_owned_function,
    handle_drop_function,
    handle_drop_value_function,
)
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.analysis.typesys import runtime_features_for_program
from sonalgebraic.backend.c_runtime import RUNTIME_HEADER, RUNTIME_SOURCE
from sonalgebraic.backend.native import generate_native_llvm_ir
from sonalgebraic.core import ast
from sonalgebraic.core.errors import SonCompileError
from sonalgebraic.frontend.parser import parse_program

_HEADER = "10 USE SYS.FILE AS F\n"
_USES = {"f": "SYS.FILE"}


def _in_main(body: str) -> str:
    lines = "\n".join(f"{30 + i * 10} {line.strip()}" for i, line in enumerate(body.strip().splitlines()))
    return (_HEADER + "20 SUB main AS PUBLIC AS VOID\n" + lines
            + "\n900 .ENDSUB\n910 CALL main\n920 END\n")


def _declared(type_text: str) -> ast.TypeSpec:
    return parse_program(f"10 DIM value AS {type_text}\n").declarations[0].type_spec


def _text(type_spec: ast.TypeSpec) -> str:
    """把 TypeSpec 写回声明文本。`FILE` 是唯一的简写，别的 kind 都得写全。"""
    return (f"MANAGED HANDLE AS {type_spec.subtype}" if type_spec.managed
            else f"HANDLE AS {type_spec.subtype}")


# --- 类型本身 ---

def test_file_type_is_explicit_managed_handle_shorthand() -> None:
    """`FILE` 不是新类型，只是 `MANAGED HANDLE AS FILE` 的写法糖。

    必须是**同一个** TypeSpec，而不是「行为相似的两个类型」：下游所有判定（析构前缀、
    ops 表、互转检查）都按 TypeSpec 字段走，两个表示就要维护两条等价路径。
    """
    assert _declared("FILE") == ast.TypeSpec("HANDLE", "FILE", managed=True)
    assert _declared("FILE") == _declared("MANAGED HANDLE AS FILE")
    # 裸句柄只差 managed 一个字段，别的都一样——区别全靠它
    assert _declared("HANDLE AS FILE") == ast.TypeSpec("HANDLE", "FILE", managed=False)


@pytest.mark.parametrize("type_text", ["MANAGED FILE", "MANAGED MANAGED HANDLE AS FILE", "MANAGED STRING"])
def test_managed_modifier_cannot_forge_an_unrelated_type(type_text: str) -> None:
    # MANAGED 是「这个句柄由作用域负责关」的声明，不是能贴在任意类型上的通用修饰符。
    # 放行 MANAGED STRING 之类只会让人以为有另一套语义，实际什么都没变。
    with pytest.raises(SonCompileError, match="MANAGED 只能修饰 HANDLE AS Kind"):
        parse_program(f"10 DIM value AS {type_text}\n")


@pytest.mark.parametrize("kind", ["LIST", "MAP", "STR_LIST", "STR_MAP", "WIDGET"])
def test_kinds_without_a_drop_protocol_are_rejected_at_declaration(kind: str) -> None:
    """没有析构协议的 kind 挡在声明处，不能等到作用域退出才发现没东西可调。

    两类原因要说不同的话：旧 LIST/MAP 句柄的槽位里只有 double 和 char*，逐元素析构
    这回事压根不存在，该写 `LIST OF T`；WIDGET 则是随父窗口销毁，本就不单独拥有。
    """
    expected = ("托管容器请写 LIST OF" if kind in {"LIST", "MAP", "STR_LIST", "STR_MAP"}
                else "托管 HANDLE 尚未接入析构器")
    with pytest.raises(SonCompileError, match=expected):
        check_program(parse_program(
            f"10 DIM value AS MANAGED HANDLE AS {kind}\n20 SUB main AS PUBLIC AS VOID\n30 .ENDSUB\n"
            "40 CALL main\n50 END\n"
        ))


@pytest.mark.parametrize(("target", "source"), [
    # 两个方向都要挡：裸转托管会凭空多出一份 close 责任，
    (_declared("FILE"), _declared("HANDLE AS FILE")),
    # 托管转裸会让责任凭空消失（作用域退出时 owner 已经不是 owner 了）
    (_declared("HANDLE AS FILE"), _declared("FILE")),
    # 换个 kind 同样成立——这条线不是 FILE 特例
    (_declared("MANAGED HANDLE AS BUFFER"), _declared("HANDLE AS BUFFER")),
])
def test_managed_handle_does_not_implicitly_cross_raw_boundary(target: ast.TypeSpec,
                                                               source: ast.TypeSpec) -> None:
    # 两者在 C 里是同一个整数，表示层没有任何能拦住的东西，只能靠类型层
    assert target.name == source.name and target.subtype == source.subtype
    assert target.managed != source.managed
    body = (f"DIM src AS {_text(source)}\nDIM dst AS {_text(target)} = src")
    with pytest.raises(SonCompileError, match="不能隐式互转"):
        check_program(parse_program(_in_main(body)))


# --- 析构协议 ---

@pytest.mark.parametrize(("kind", "prefix"), [
    ("BUFFER", "sa_binary"),
    ("FILE", "sa_file"),
    ("NET_STREAM", "sa_net_stream"),
    ("TCP_LISTENER", "sa_net_tcp_listener"),
    ("UDP_SOCKET", "sa_net_udp"),
    ("WINDOW", "sa_gui_window"),
])
def test_every_wired_kind_has_the_full_drop_triple(kind: str, prefix: str) -> None:
    """三件套必须齐：缺一个就是链接期报错，而不是编译期能看懂的话。

    `_drop` 给作用域退出用，`_drop_value` 是指针 ABI（`SaValueOps.drop` 要的签名，
    容器元素走它），`_close_owned` 给显式 CLOSE 用——它要先清槽再关，否则后面的
    作用域析构会对同一个 token 再关一遍。
    """
    type_spec = ast.TypeSpec("HANDLE", kind, managed=True)
    names = (handle_drop_function(type_spec),
             handle_drop_value_function(type_spec),
             handle_close_owned_function(type_spec))
    assert names == (f"{prefix}_drop", f"{prefix}_drop_value", f"{prefix}_close_owned")
    # 名字对不代表 runtime 里有。三个都要真能找到定义，否则是个只在链接期炸的名字
    runtime = RUNTIME_HEADER + RUNTIME_SOURCE
    for name in names:
        assert name in runtime, f"runtime 里没有 {name}"


def test_both_backends_drop_file_tokens_with_integer_abi() -> None:
    """两个后端对同一个 token 的 ABI 必须一致：按值传的整数，不是指针。

    混用会很安静地错——native 那边 `i64` 和 `ptr` 都能过 LLVM 的类型检查，错的是
    传给 runtime 的东西，要到运行时关错句柄才看得出来。
    """
    source = _in_main('DIM owner AS FILE = F.OPEN("probe", "READ")\nPRINT F.TELL(owner)')
    generated = compile_user_c(source)
    # C 侧：token 存在 SaHandle 里，drop 直接收这个值
    assert re.search(r"SaHandle sa_owner\b", generated)
    assert "sa_file_drop(sa_owner);" in generated
    # native 侧：声明和调用点都必须是 i64
    ir = generate_native_llvm_ir(check_program(parse_program(source)))
    assert "declare void @sa_file_drop(i64)" in ir
    assert re.search(r"call void @sa_file_drop\(i64 ", ir)


def test_managed_file_decl_alone_requests_file_runtime() -> None:
    # 没有 USE SYS.FILE 也要带上 file 切片：作用域退出会调 sa_file_drop，
    # 而那是 runtime 的 file 部分。按 USE 决定切片会漏掉这种只声明不调用的程序。
    program = parse_program(
        "10 DIM owner AS FILE\n20 SUB main AS PUBLIC AS VOID\n30 .ENDSUB\n40 CALL main\n50 END\n"
    )
    assert "file" in runtime_features_for_program(program, {})


# --- 游标与借用 ---

_CURSOR_CALLS = {
    "READ": 'F.READ(view, 1)',
    "WRITE": 'F.WRITE(view, "x")',
    "SEEK": 'F.SEEK(view, 0, "SET")',
    # SIZE 临时 seek 到文件尾再恢复，借用者调它就动到了所有者的位置
    "SIZE": "F.SIZE(view)",
}


@pytest.mark.parametrize("member", sorted(_CURSOR_CALLS))
def test_file_cursor_operations_require_mutable_owner(member: str) -> None:
    assert builtin_effect(f"F.{member}", _USES).argument(0) is ArgumentEffect.MUTATE
    with pytest.raises(SonCompileError, match="借用来的变量是只读的"):
        check_program(parse_program(_in_main(
            f"DIM owner AS FILE\nDIM view AS FILE\nview f= owner\nPRINT {_CURSOR_CALLS[member]}"
        )))


def test_file_factory_query_and_close_have_distinct_effects() -> None:
    """三种影响必须分开记：产出新资源、只读观察、消费掉。

    混成一类会直接坏掉：把 OPEN 当普通调用就没有接管点，托管变量永远初始化不了；
    把 TELL 当 MUTATE 则借用者连读位置都不行，借用这个特性就没用了。
    """
    assert builtin_effect("F.OPEN", _USES).fresh_handle == "FILE"
    assert builtin_effect("F.TELL", _USES).argument(0) is ArgumentEffect.READ
    assert builtin_effect("F.CLOSE", _USES).argument(0) is ArgumentEffect.CONSUME
    # 只读观察在借用者上必须放行，这是上面那条 MUTATE 规则的反面
    check_program(parse_program(_in_main(
        "DIM owner AS FILE\nDIM view AS FILE\nview f= owner\nPRINT F.TELL(view)"
    )))


def test_file_operations_accept_managed_handle_without_general_type_conversion() -> None:
    """内置 API 收托管句柄，但这不等于开了一条通用转换。

    SYS.FILE 的签名写的是裸 `HANDLE AS FILE`，托管值能传进去是因为 effect 表登记过
    这些成员。要是改成「托管句柄可隐式转裸句柄」，用户代码里的赋值也会跟着放行。
    """
    check_program(parse_program(_in_main(
        'DIM owner AS FILE = F.OPEN("probe", "READ")\nPRINT F.READ(owner, 1)\nPRINT F.CLOSE(owner)'
    )))
    with pytest.raises(SonCompileError, match="不能隐式互转"):
        check_program(parse_program(_in_main(
            'DIM owner AS FILE = F.OPEN("probe", "READ")\nDIM raw AS HANDLE AS FILE = owner'
        )))


def test_only_known_factory_expression_is_an_adoption_point() -> None:
    """只有已登记的工厂调用（和返回托管类型的 SUB）能初始化托管变量。

    底层是个整数，所以不挡的话任何 LONG 都能赋进来——包括 `F.TELL` 的返回值和字面量 0。
    那样作用域退出会去关一个从来没打开过的 token。
    """
    check_program(parse_program(_in_main('DIM owner AS FILE = F.OPEN("probe", "READ")')))
    # 返回托管类型的 SUB 也是接管点：所有权随 RETURN 转给调用方
    check_program(parse_program(
        _HEADER
        + '20 SUB maker() AS FILE\n30 RETURN F.OPEN("probe", "READ")\n40 .ENDSUB\n'
          "50 SUB main AS PUBLIC AS VOID\n60 DIM owner AS FILE = maker()\n70 .ENDSUB\n"
          "80 CALL main\n90 END\n"
    ))
    # 查询返回的是位置，不是资源
    with pytest.raises(SonCompileError, match="不能隐式互转"):
        check_program(parse_program(_in_main(
            'DIM other AS FILE = F.OPEN("probe", "READ")\nDIM owner AS FILE = F.TELL(other)'
        )))
    # 字面量更直接：0 不是任何人打开的东西
    with pytest.raises(SonCompileError, match="不能隐式互转"):
        check_program(parse_program(_in_main("DIM owner AS FILE = 0")))
