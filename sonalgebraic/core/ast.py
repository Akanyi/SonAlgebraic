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


@dataclass(frozen=True)
class Call(Stmt):
    name: str
    args: list["Expr"]


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
