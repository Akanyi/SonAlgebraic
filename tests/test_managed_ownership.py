"""显式托管资源（`FILE` 等）的所有权状态机：不写 `m=` / `f=` 也要被检查。

`STRING` / `SYMBOL` / `ERROR` 的所有权只在出现 `m=` / `f=` 时才有分歧，所以 test_ownership
那批测试盯的是赋值语句本身。托管句柄不一样：`F.CLOSE` 这种**内置调用**就能消费掉资源，
`= 复制` 会凭空多出一个持有者。也就是说哪怕整段源码里一个所有权标记都没有，状态机依然
得跑——这组测试就是守住这一点。

每条负例对应 codegen 里一个具体的踩雷方式：双重 close、close 后再用（句柄已失效）、
复制出两个持有者导致关两遍。正例同样重要，它们划出状态机必须**放行**的边界：关闭后重新
初始化、借用者随 SCOPE 退出而解冻、互不相干的兄弟 SCOPE 各自独立。

恢复说明：原文件在 10 月初丢失，测试侧没有字节码可比对。这份是按幸存的 pytest 节点名
（含每个参数化用例的源码与期望诊断）配合恢复后的 `analysis/ownership.py` 重建的，
不是逐字找回。详见 `.recovery/RECOVERY.md`。
"""
from __future__ import annotations

import pytest

from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.core.errors import SonCompileError
from sonalgebraic.frontend.parser import parse_program

_HEADER = "10 USE SYS.FILE AS F\n"


def _lines(body: str, start: int = 30) -> str:
    return "\n".join(f"{start + i * 10} {line.strip()}" for i, line in enumerate(body.strip().splitlines()))


def _in_main(body: str) -> str:
    """未编号的片段放进 VOID 的 main。参数化 id 里存的就是未编号原文，便于直接读。"""
    return (_HEADER + "20 SUB main AS PUBLIC AS VOID\n" + _lines(body)
            + "\n900 .ENDSUB\n910 CALL main\n920 END\n")


def _in_maker(body: str) -> str:
    """片段放进一个返回 FILE 的 SUB——RETURN 的所有权转出规则只在非 VOID 里成立。"""
    return (_HEADER + "20 SUB maker() AS FILE\n" + _lines(body)
            + "\n800 .ENDSUB\n810 SUB main AS PUBLIC AS VOID\n820 .ENDSUB\n"
              "830 CALL main\n840 END\n")


# --- 没有 m= / f= 也要检查的那些操作 ---

@pytest.mark.parametrize(("source", "expected"), [
    # `=` 在托管句柄上不是复制而是多一个持有者，析构会关两遍
    ("DIM owner AS FILE\nDIM other AS FILE = owner", "独占资源不能用 = 复制"),
    ("DIM owner AS FILE\nDIM other AS FILE\nother = owner", "独占资源不能用 = 复制"),
    # 裸 HANDLE 和托管 HANDLE 底层都是整数句柄，只能靠类型层拦住互转，
    # 否则托管那一侧的 close 责任会凭空消失（或凭空多出来）
    ("DIM owner AS FILE\nDIM raw AS HANDLE AS FILE = owner", "不能隐式互转"),
    ("DIM raw AS HANDLE AS FILE\nDIM owner AS FILE = raw", "不能隐式互转"),
    # 移走之后原变量不再持有，任何内置操作都是对失效句柄取值
    ("DIM owner AS FILE\nDIM target AS FILE\ntarget m= owner\nPRINT F.TELL(owner)", "移走"),
    # 借用者只读：借来的句柄上 CLOSE 会把所有者的资源关掉，READ 会动到共享游标
    ("DIM owner AS FILE\nDIM view AS FILE\nview f= owner\nPRINT F.CLOSE(view)", "只读"),
    ("DIM owner AS FILE\nDIM view AS FILE\nview f= owner\nPRINT F.READ(view, 1)", "只读"),
    # 反过来，借用存续期间所有者也被冻结：SIZE 会临时 seek 到文件尾，借用者读到的位置就变了
    ("DIM owner AS FILE\nDIM view AS FILE\nview f= owner\nPRINT F.SIZE(owner)", "借出"),
    # 关闭状态必须跨分支汇合：任一分支关过就算关过
    ("DIM owner AS FILE\nIF TRUE THEN\nPRINT F.CLOSE(owner)\n.ENDIF\nPRINT F.TELL(owner)", "已关闭"),
    ("DIM owner AS FILE\nIF FALSE THEN\nELSE IF F.CLOSE(owner) THEN\nELSE\nPRINT F.TELL(owner)\n.ENDIF",
     "已关闭"),
    ("DIM owner AS FILE\nPRINT F.CLOSE(owner)\nPRINT F.CLOSE(owner)", "已关闭"),
    ("DIM owner AS FILE\nPRINT F.CLOSE(owner)\nPRINT F.TELL(owner)", "已关闭"),
    # 同一条语句内的求值顺序也算：实参先关掉了，外层调用拿到的已经是死句柄
    ("DIM owner AS FILE\nPRINT F.READ(owner, F.CLOSE(owner))", "已关闭"),
    # 循环的回边让「关一次」变成「每轮关一次」
    ("DIM owner AS FILE\nWHILE F.CLOSE(owner)\n.ENDWHILE", "循环条件不能消费"),
    ("DIM owner AS FILE\nWHILE TRUE\nPRINT F.CLOSE(owner)\n.ENDWHILE", "循环体内不能 CLOSE"),
    # 数组和 PTR 会把「谁持有」变成运行期才知道的事，状态机跟不住，所以挡在声明处
    ("DIM owner[2] AS FILE", "数组暂不支持托管"),
    ("DIM pointer AS PTR TO FILE", "PTR 不能绕过托管"),
])
def test_resource_operations_check_ownership_without_move_syntax(source: str, expected: str) -> None:
    with pytest.raises(SonCompileError, match=expected):
        check_program(parse_program(_in_main(source)))


# --- 状态机必须放行的边界 ---

@pytest.mark.parametrize("source", [
    # 接管新建资源是合法的初始化，不是复制
    'DIM owner AS FILE = F.OPEN("probe", "READ")\nPRINT F.TELL(owner)',
    # 关闭 / 移走之后重新赋一个新资源，变量就该恢复可用——否则「关了再开」这种常规写法没法写
    'DIM owner AS FILE\nPRINT F.CLOSE(owner)\nowner = F.OPEN("probe", "READ")\nPRINT F.TELL(owner)',
    'DIM owner AS FILE\nDIM other AS FILE\nother m= owner\nowner = F.OPEN("probe", "READ")\n'
    'PRINT F.TELL(owner)',
    # 分支里关掉后直接 RETURN，这条路径不会汇合回来，出口处的 owner 仍然有效
    "DIM owner AS FILE\nIF TRUE THEN\nPRINT F.CLOSE(owner)\nRETURN\n.ENDIF\nPRINT F.TELL(owner)",
    # 冻结跟着借用者的生命周期走：借用者随 .ENDSCOPE 死掉，所有者立刻解冻
    "DIM owner AS FILE\nSCOPE\nDIM view AS FILE\nview f= owner\nPRINT F.TELL(view)\n.ENDSCOPE\n"
    "PRINT F.CLOSE(owner)",
    # 兄弟 SCOPE 里的同名变量是两个互不相干的资源，前一个关过不该影响后一个
    "SCOPE\nDIM owner AS FILE\nPRINT F.CLOSE(owner)\n.ENDSCOPE\n"
    "SCOPE\nDIM owner AS FILE\nPRINT F.TELL(owner)\n.ENDSCOPE",
])
def test_owner_reinitialization_and_scope_exit_restore_valid_access(source: str) -> None:
    check_program(parse_program(_in_main(source)))


# --- 所有权转出 ---

@pytest.mark.parametrize("source", [
    # 转出所有者：调用方拿到句柄后 view 还指着它，借用就成了悬垂
    "DIM owner AS FILE\nDIM view AS FILE\nview f= owner\nRETURN owner",
    # 转出借用者：它本来就不持有所有权，转出等于凭空发明一个持有者
    "DIM owner AS FILE\nDIM view AS FILE\nview f= owner\nRETURN view",
])
def test_exclusive_return_cannot_escape_live_borrow(source: str) -> None:
    with pytest.raises(SonCompileError, match="RETURN 不能转出"):
        check_program(parse_program(_in_maker(source)))


def test_ref_parameter_can_operate_but_not_consume_owner() -> None:
    """`AS REF` 借到的是操作权，不是所有权；资源始终属于调用方。

    托管句柄按值传参会复制出第二个持有者，所以参数侧强制 `AS REF`。但 REF 不能反过来
    变成一条转出通道：被调方 CLOSE / RETURN / `m=` 走它，调用方手里的变量就在它不知道的
    时候失效了。
    """
    # 操作（读游标）是 REF 的全部意义所在，必须放行
    check_program(parse_program(
        _HEADER
        + "20 SUB touch(target AS FILE AS REF) AS VOID\n30 PRINT F.TELL(target)\n40 .ENDSUB\n"
          "50 SUB main AS PUBLIC AS VOID\n60 DIM owner AS FILE\n70 CALL touch(owner)\n80 .ENDSUB\n"
          "90 CALL main\n100 END\n"
    ))
    # 按值传参会多出一个持有者，挡在签名处而不是调用处——签名是契约
    with pytest.raises(SonCompileError, match="独占资源参数必须使用 AS REF"):
        check_program(parse_program(
            _HEADER
            + "20 SUB take(target AS FILE) AS VOID\n30 PRINT F.TELL(target)\n40 .ENDSUB\n"
              "50 SUB main AS PUBLIC AS VOID\n60 DIM owner AS FILE\n70 CALL take(owner)\n80 .ENDSUB\n"
              "90 CALL main\n100 END\n"
        ))
    # 消费掉调用方的资源：三条通道各自有话说，都不能放行
    with pytest.raises(SonCompileError, match="不能 CLOSE 调用方的 AS REF 资源"):
        check_program(parse_program(
            _HEADER
            + "20 SUB eat(target AS FILE AS REF) AS VOID\n30 PRINT F.CLOSE(target)\n40 .ENDSUB\n"
              "50 SUB main AS PUBLIC AS VOID\n60 DIM owner AS FILE\n70 CALL eat(owner)\n80 .ENDSUB\n"
              "90 CALL main\n100 END\n"
        ))
    with pytest.raises(SonCompileError, match="RETURN 不能转出"):
        check_program(parse_program(
            _HEADER
            + "20 SUB steal(target AS FILE AS REF) AS FILE\n30 RETURN target\n40 .ENDSUB\n"
              "50 SUB main AS PUBLIC AS VOID\n60 DIM owner AS FILE\n"
              "70 DIM got AS FILE = steal(owner)\n80 .ENDSUB\n"
              "90 CALL main\n100 END\n"
        ))
    with pytest.raises(SonCompileError, match="m= 的源不能是 AS REF 参数"):
        check_program(parse_program(
            _HEADER
            + "20 SUB drain(target AS FILE AS REF) AS VOID\n30 DIM mine AS FILE\n40 mine m= target\n"
              "50 .ENDSUB\n"
              "60 SUB main AS PUBLIC AS VOID\n70 DIM owner AS FILE\n80 CALL drain(owner)\n90 .ENDSUB\n"
              "100 CALL main\n110 END\n"
        ))
