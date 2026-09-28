from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union


@dataclass(frozen=True)
class TypeSpec:
    name: str
    subtype: str | None = None
    inner: "TypeSpec | None" = None
    # 定长数组的元素个数；None 表示非数组。数组的元素类型是去掉 array_size 的同一 TypeSpec。
    array_size: int | None = None
    # 只对 name == "SUB" 有意义：callable 的参数表（返回类型放 inner，与 PTR TO / PROMISE OF 同构）。
    # 复用 Param 而不是另造一个「无名参数」类型，是为了让签名能直接喂给 check_call_args /
    # call_args_with_prelude 这些吃 `.params` 的现成路径；名字和行号不参与 same_type_spec 比较。
    params: "tuple[Param, ...] | None" = None


@dataclass(frozen=True)
class Declaration:
    name: str
    type_spec: TypeSpec
    mutable: bool
    expr: "Expr | None"
    line_no: int


@dataclass(frozen=True)
class Param:
    name: str
    type_spec: TypeSpec
    by_ref: bool
    line_no: int


@dataclass(frozen=True)
class UseModule:
    module: str
    alias: str
    line_no: int


@dataclass(frozen=True)
class UseCHeader:
    header: str
    alias: str
    is_system: bool
    line_no: int


@dataclass(frozen=True)
class UseLibrary:
    library: str
    alias: str
    line_no: int


@dataclass(frozen=True)
class CFunctionDecl:
    alias: str
    name: str
    params: list[Param]
    return_type: TypeSpec
    line_no: int


@dataclass(frozen=True)
class Program:
    uses: list[UseModule] = field(default_factory=list)
    usec_headers: list[UseCHeader] = field(default_factory=list)
    uselibs: list[UseLibrary] = field(default_factory=list)
    c_decls: list[CFunctionDecl] = field(default_factory=list)
    entities: list["EntityDef"] = field(default_factory=list)
    enums: list["EnumDef"] = field(default_factory=list)
    declarations: list[Declaration] = field(default_factory=list)
    subs: list["Subroutine"] = field(default_factory=list)
    top_level: list["Stmt"] = field(default_factory=list)
    source_lines: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EntityDef:
    name: str
    fields: list[Declaration]
    line_no: int


@dataclass(frozen=True)
class EnumDef:
    """ENUM Name ... .ENDENUM。成员按出现顺序从 0 自增，作为 LONG 常量。"""
    name: str
    members: list[str]
    line_no: int


@dataclass(frozen=True)
class Subroutine:
    name: str
    params: list[Param]
    visibility: str
    return_type: TypeSpec
    body: list["Stmt"]
    line_no: int
    # ASYNC SUB：编译成无栈状态机协程，返回 PROMISE 而非直接返回值。
    is_async: bool = False


@dataclass(frozen=True)
class Stmt:
    line_no: int


@dataclass(frozen=True)
class NoOp(Stmt):
    pass


@dataclass(frozen=True)
class Print(Stmt):
    expr: "Expr | None"


@dataclass(frozen=True)
class LocalDeclaration(Stmt):
    name: str
    type_spec: TypeSpec
    mutable: bool
    expr: "Expr | None"


@dataclass(frozen=True)
class Assign(Stmt):
    target: "Expr"
    expr: "Expr"
    # "copy"（=，深拷贝）| "borrow"（f=，只读借用，不取所有权）| "move"（m=，所有权转移，源作废）。
    # 带默认值是为了让唯一的构造点和所有 isinstance 分支都不用动：普通赋值看不见这个字段。
    mode: str = "copy"


@dataclass(frozen=True)
class Call(Stmt):
    name: str
    args: list["Expr"]


@dataclass(frozen=True)
class CallRet(Stmt):
    """CALLRET name(args)：把 callable 当作当前 SUB 的返回出口。调用它、然后本 SUB 就此终结，
    其后语句不可达。name 是 callable 实体或 PTR TO SUB 变量（可带 ENTITY 字段路径）。"""
    name: str
    args: list["Expr"]


@dataclass(frozen=True)
class NewSub(Stmt):
    """NEW SUB name FROM ptr：从函数引用生成一个有本块生命周期的 callable 实体并绑定到 name。
    没有 .ENDSUB——它不定义函数体。类型由 source 的签名推出，所以这里不带 type_spec。"""
    name: str
    source: "Expr"


@dataclass(frozen=True)
class AwaitStmt(Stmt):
    """独立成句的 AWAIT / SYNC，执行异步操作并丢弃结果。expr 是 AwaitExpr 或 SyncExpr。"""
    expr: "Expr"


@dataclass(frozen=True)
class CatchBranch:
    error_type: str
    alias: str
    body: list["Stmt"]
    line_no: int


@dataclass(frozen=True)
class TryCatch(Stmt):
    call_name: str
    args: list["Expr"]
    traceback_var: str
    catches: list[CatchBranch]


@dataclass(frozen=True)
class ThrowNew(Stmt):
    error_type: str
    message: "Expr"


@dataclass(frozen=True)
class ThrowVar(Stmt):
    name: str


@dataclass(frozen=True)
class ElifBranch:
    condition: "Expr"
    body: list[Stmt]
    line_no: int


@dataclass(frozen=True)
class If(Stmt):
    condition: "Expr"
    body: list[Stmt]
    # ELSE IF 分支按出现顺序排列；else_body 为最终 ELSE 块（无则为空列表）
    elifs: list[ElifBranch] = field(default_factory=list)
    else_body: list[Stmt] = field(default_factory=list)


@dataclass(frozen=True)
class Goto(Stmt):
    label: str


@dataclass(frozen=True)
class ForLoop(Stmt):
    """FOR var = start TO end [STEP step] ... .ENDFOR。var 必须是已声明的数值变量。"""
    var: str
    start: "Expr"
    end: "Expr"
    step: "Expr | None"
    body: list[Stmt]


@dataclass(frozen=True)
class WhileLoop(Stmt):
    """WHILE cond ... .ENDWHILE"""
    condition: "Expr"
    body: list[Stmt]

@dataclass(frozen=True)
class Gosub(Stmt):
    label: str


@dataclass(frozen=True)
class Label(Stmt):
    name: str


@dataclass(frozen=True)
class Return(Stmt):
    expr: "Expr | None"


@dataclass(frozen=True)
class End(Stmt):
    pass


@dataclass(frozen=True)
class Input(Stmt):
    alias: str
    prompt: "Expr"
    target: str


@dataclass(frozen=True)
class Cls(Stmt):
    pass


@dataclass(frozen=True)
class Expr:
    line_no: int


@dataclass(frozen=True)
class NumberLiteral(Expr):
    value: str


@dataclass(frozen=True)
class NullLiteral(Expr):
    pass


@dataclass(frozen=True)
class BoolLiteral(Expr):
    value: bool


@dataclass(frozen=True)
class StringLiteral(Expr):
    value: str


@dataclass(frozen=True)
class FString(Expr):
    parts: list[Union[str, Expr]]


@dataclass(frozen=True)
class VarRef(Expr):
    name: str


@dataclass(frozen=True)
class Unary(Expr):
    op: str
    expr: Expr


@dataclass(frozen=True)
class Deref(Expr):
    expr: Expr


@dataclass(frozen=True)
class AddressOf(Expr):
    expr: Expr


@dataclass(frozen=True)
class SubRef(Expr):
    """@name()：取函数引用，类型是 PTR TO SUB(签名)。尾随的空括号是语法层区分「取函数」与
    「取变量地址」的记号，不是调用。name 可以是本地 SUB、USE 模块的 SUB 或 DECLARE C 的函数。"""
    name: str


@dataclass(frozen=True)
class Cast(Expr):
    type_spec: TypeSpec
    expr: Expr


@dataclass(frozen=True)
class Binary(Expr):
    left: Expr
    op: str
    right: Expr


@dataclass(frozen=True)
class Index(Expr):
    """数组下标访问：base[index]。base 通常是 VarRef（含 ENTITY 字段路径）。"""
    base: Expr
    index: Expr


@dataclass(frozen=True)
class CallExpr(Expr):
    name: str
    args: list[Expr]


@dataclass(frozen=True)
class AwaitExpr(Expr):
    """AWAIT p / AWAIT asyncfoo()。operand 要么是 PROMISE 值，要么是对 ASYNC SUB 的
    CallExpr（此时先隐式启动再等待）。语句级关键字，只能独立成句或占赋值右侧。"""
    operand: Expr


@dataclass(frozen=True)
class SyncExpr(Expr):
    """SYNC asyncfoo()。把 ASYNC SUB 按同步方式调用，当前执行流阻塞到结果出来。
    operand 必须是对 ASYNC SUB 的 CallExpr。"""
    operand: Expr
