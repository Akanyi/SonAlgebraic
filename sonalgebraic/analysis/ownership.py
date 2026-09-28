"""`f=`（借用）与 `m=`（移动）的所有权预检。

这是独立于 check_stmt 的第二遍：check_stmt 负责「变量存不存在、类型对不对」，这里只管
「谁拥有资源、这个时刻还能不能碰」。两遍分开是因为所有权是**顺序敏感**的状态机（移走之后
再读就是错），而 check_stmt 是无状态的逐句校验，往它那 10 个参数的签名里再塞一套可变状态
只会让两边都难读。

代价是这里要自己递归走一遍 SUB 体。不含 f=/m= 的 SUB 直接短路返回，老程序零开销、零风险。

codegen 的实现方式决定了这里的几条硬规则，理由写在对应的检查旁边：
- 借用靠「把目标从块尾清理登记里摘掉」实现，登记表是静态、按块的，所以借用目标必须与
  该语句同块 DIM；
- 移动靠「源置空」实现，源保持登记（free(NULL) 安全），所以移动对控制流路径不敏感，
  唯独循环例外——下一轮迭代会重新读到已置空的源。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core import ast
from ..core.errors import SonCompileError
from ..core.module_model import ModuleExports
from ..core.names import split_module_member
from .typesys import callable_symbol_type, describe_type, is_error, is_string, is_sub_ptr, is_sub_type, is_symbol, resolve_c_func, resolve_path_type, same_type_spec, sub_signature, type_of

if TYPE_CHECKING:
    from .semantics import Symbol


_OP = {"borrow": "f=", "move": "m="}


def is_managed_type(
    type_spec: ast.TypeSpec,
    entities: dict[str, ast.EntityDef],
    external_modules: dict[str, ModuleExports],
) -> bool:
    """f=/m= 只对「作用域末尾会被自动释放」的类型有意义：STRING / SYMBOL / ERROR、callable
    实体（SUB），以及含这类字段的 ENTITY。数组、PROMISE（已是单消费者语义）、数值 / HANDLE / PTR 之类的值
    类型没有所有权可言，一律不算。判定口径与 codegen 的 type_has_managed_resources 一致。"""
    if type_spec.array_size is not None:
        return False
    if is_string(type_spec) or is_symbol(type_spec) or is_error(type_spec) or is_sub_type(type_spec):
        return True
    return type_spec.name == "ENTITY" and _entity_has_managed_fields(type_spec, entities, external_modules)


def _entity_has_managed_fields(
    type_spec: ast.TypeSpec,
    entities: dict[str, ast.EntityDef],
    external_modules: dict[str, ModuleExports],
) -> bool:
    entity = _resolve_entity(type_spec, entities, external_modules)
    if entity is None:
        return False
    for field in entity.fields:
        field_type = field.type_spec
        if field_type.array_size is not None:
            continue
        if is_string(field_type) or is_symbol(field_type) or is_error(field_type) or is_sub_type(field_type):
            return True
        if field_type.name == "ENTITY" and _entity_has_managed_fields(field_type, entities, external_modules):
            return True
    return False


def _resolve_entity(
    type_spec: ast.TypeSpec,
    entities: dict[str, ast.EntityDef],
    external_modules: dict[str, ModuleExports],
) -> ast.EntityDef | None:
    subtype = type_spec.subtype or ""
    split = split_module_member(subtype)
    if split:
        alias, member = split
        module = external_modules.get(alias)
        return module.entities.get(member.lower()) if module is not None else None
    return entities.get(subtype.lower())


def _frame_owns(kind: str, type_spec: ast.TypeSpec) -> bool:
    """本帧是否真正持有这个变量的资源。局部 DIM 总是持有；按值参数只有 STRING、含托管
    字段的 ENTITY 会在入口被深拷贝进本帧，callable 在入口多持有一份引用计数，SYMBOL / ERROR 按值参数只是调用方指针的浅拷贝，
    释放它就是释放调用方的东西。"""
    if kind == "local":
        return True
    if kind == "param":
        return is_string(type_spec) or type_spec.name == "ENTITY" or is_sub_type(type_spec)
    return False


@dataclass
class _VarInfo:
    kind: str  # "local" | "param" | "ref_param" | "global"
    block: int  # 声明所在块的编号；参数与全局记 0
    mutable: bool


def uses_ownership_assign(body: list[ast.Stmt]) -> bool:
    return any(_stmt_uses_ownership(stmt) for stmt in body)


def _stmt_uses_ownership(stmt: ast.Stmt) -> bool:
    if isinstance(stmt, ast.Assign):
        return stmt.mode != "copy"
    return any(_stmt_uses_ownership(inner) for body in _child_bodies(stmt) for inner in body)


def _has_jumps(body: list[ast.Stmt]) -> bool:
    for stmt in body:
        if isinstance(stmt, ast.Goto | ast.Gosub):
            return True
        if any(_has_jumps(inner) for inner in _child_bodies(stmt)):
            return True
    return False


def _child_bodies(stmt: ast.Stmt) -> list[list[ast.Stmt]]:
    if isinstance(stmt, ast.If):
        return [stmt.body, *(branch.body for branch in stmt.elifs), stmt.else_body]
    if isinstance(stmt, ast.ForLoop | ast.WhileLoop):
        return [stmt.body]
    if isinstance(stmt, ast.TryCatch):
        return [branch.body for branch in stmt.catches]
    return []


def check_ownership(
    sub: ast.Subroutine,
    symbols: dict[str, Symbol],
    subs: dict[str, ast.Subroutine],
    entities: dict[str, ast.EntityDef],
    external_modules: dict[str, ModuleExports],
    c_funcs: dict[str, ast.CFunctionDecl],
) -> None:
    """在 check_stmt 全部通过之后调用：这里假定变量都已声明、类型都能解析。"""
    if not uses_ownership_assign(sub.body):
        return
    _Walker(sub, symbols, subs, entities, external_modules, c_funcs).run()


class _Walker:
    def __init__(
        self,
        sub: ast.Subroutine,
        symbols: dict[str, Symbol],
        subs: dict[str, ast.Subroutine],
        entities: dict[str, ast.EntityDef],
        external_modules: dict[str, ModuleExports],
        c_funcs: dict[str, ast.CFunctionDecl],
    ) -> None:
        from .semantics import Symbol

        self.sub = sub
        self.subs = subs
        self.entities = entities
        self.external_modules = external_modules
        self.c_funcs = c_funcs
        self.has_jumps = _has_jumps(sub.body)

        self.scope: dict[str, Symbol] = symbols.copy()
        self.info: dict[str, _VarInfo] = {key: _VarInfo("global", 0, symbol.mutable) for key, symbol in symbols.items()}
        for param in sub.params:
            key = param.name.lower()
            self.scope[key] = Symbol(param.name, param.type_spec, True, param.by_ref)
            self.info[key] = _VarInfo("ref_param" if param.by_ref else "param", 0, True)

        self.moved: set[str] = set()
        # f= 的目标 -> 源。目标不持有所有权、只读；源在任一借用者存活期间冻结（不能改）。
        # 借用者死在块尾，源随之解冻——这就是为什么用映射而不是两个 set。
        self.borrowed: dict[str, str] = {}
        self.block_counter = 0
        self.block_stack: list[int] = []
        self.loop_blocks: list[int] = []
        self.declared_stack: list[list[str]] = []

    def run(self) -> None:
        self.block(self.sub.body)

    # --- 作用域 ---

    def block(self, body: list[ast.Stmt], loop: bool = False) -> None:
        self.block_counter += 1
        block_id = self.block_counter
        self.block_stack.append(block_id)
        if loop:
            self.loop_blocks.append(block_id)
        declared: list[str] = []
        self.declared_stack.append(declared)
        for stmt in body:
            self.stmt(stmt)
        self.declared_stack.pop()
        self.leave_block(declared)
        if loop:
            self.loop_blocks.pop()
        self.block_stack.pop()

    def leave_block(self, declared: list[str]) -> None:
        # 块内声明的变量随块死亡，连同它们的所有权状态一起清掉：兄弟块可以再声明同名变量，
        # 它们借走的源也随之解冻。块外变量在块内被移走的状态则保留——任一分支移走就算死。
        for key in declared:
            self.scope.pop(key, None)
            self.info.pop(key, None)
            self.moved.discard(key)
            self.borrowed.pop(key, None)

    def frozen(self, key: str) -> bool:
        return any(source == key for source in self.borrowed.values())

    def declare(self, name: str, type_spec: ast.TypeSpec, mutable: bool) -> None:
        from .semantics import Symbol

        key = name.lower()
        self.scope[key] = Symbol(name, type_spec, mutable)
        self.info[key] = _VarInfo("local", self.block_stack[-1], mutable)
        self.declared_stack[-1].append(key)

    # --- 语句 ---

    def stmt(self, stmt: ast.Stmt) -> None:
        if isinstance(stmt, ast.LocalDeclaration):
            self.declare(stmt.name, stmt.type_spec, stmt.mutable)
            if stmt.expr is not None:
                self.read_expr(stmt.expr)
        elif isinstance(stmt, ast.Assign):
            if stmt.mode != "copy":
                self.ownership_assign(stmt)
                return
            if isinstance(stmt.target, ast.VarRef):
                self.use(stmt.target.name, stmt.line_no, write=True)
            elif isinstance(stmt.target, ast.Deref):
                self.read_expr(stmt.target.expr)
            elif isinstance(stmt.target, ast.Index):
                root = _index_root(stmt.target)
                if root is not None:
                    self.use(root.name, stmt.line_no, write=True)
                self.read_expr(stmt.target.index)
            self.read_expr(stmt.expr)
        elif isinstance(stmt, ast.Print):
            self.read_expr(stmt.expr)
        elif isinstance(stmt, ast.Input):
            self.read_expr(stmt.prompt)
            self.use(stmt.target, stmt.line_no, write=True)
        elif isinstance(stmt, ast.Call):
            self.call_args(stmt.name, stmt.args, stmt.line_no)
        elif isinstance(stmt, ast.CallRet):
            self.call_args(stmt.name, stmt.args, stmt.line_no)
        elif isinstance(stmt, ast.NewSub):
            self.read_expr(stmt.source)
            source_type = type_of(stmt.source, self.scope, self.subs, self.entities, None, self.external_modules, self.c_funcs)
            self.declare(stmt.name, sub_signature(source_type), True)
        elif isinstance(stmt, ast.TryCatch):
            self.call_args(stmt.call_name, stmt.args, stmt.line_no)
            self.use(stmt.traceback_var, stmt.line_no, write=True)
            for branch in stmt.catches:
                self.catch_block(branch)
        elif isinstance(stmt, ast.ThrowNew):
            self.read_expr(stmt.message)
        elif isinstance(stmt, ast.ThrowVar):
            self.use(stmt.name, stmt.line_no)
        elif isinstance(stmt, ast.If):
            self.read_expr(stmt.condition)
            self.block(stmt.body)
            for branch in stmt.elifs:
                self.read_expr(branch.condition)
                self.block(branch.body)
            self.block(stmt.else_body)
        elif isinstance(stmt, ast.ForLoop):
            self.use(stmt.var, stmt.line_no, write=True)
            self.read_expr(stmt.start)
            self.read_expr(stmt.end)
            self.read_expr(stmt.step)
            self.block(stmt.body, loop=True)
        elif isinstance(stmt, ast.WhileLoop):
            self.read_expr(stmt.condition)
            self.block(stmt.body, loop=True)
        elif isinstance(stmt, ast.Return):
            self.read_expr(stmt.expr)
        elif isinstance(stmt, ast.AwaitStmt):
            self.read_expr(stmt.expr)

    def catch_block(self, branch: ast.CatchBranch) -> None:
        # CATCH 别名是块内的只读 ERROR 局部：declare 要在块里做，所以不能直接复用 block()。
        self.block_counter += 1
        block_id = self.block_counter
        self.block_stack.append(block_id)
        declared: list[str] = []
        self.declared_stack.append(declared)
        self.declare(branch.alias, ast.TypeSpec("ERROR"), False)
        for stmt in branch.body:
            self.stmt(stmt)
        self.declared_stack.pop()
        self.leave_block(declared)
        self.block_stack.pop()

    # --- f= / m= 本体 ---

    def ownership_assign(self, stmt: ast.Assign) -> None:
        op = _OP[stmt.mode]
        line = stmt.line_no
        if isinstance(stmt.expr, ast.SubRef) or (
            isinstance(stmt.target, ast.VarRef) and is_sub_ptr(resolve_path_type(stmt.target.name, self.scope, self.entities, line))
        ):
            # 函数引用是只读值，f= 就是普通赋值（m= 已在 check_stmt 拦下），不牵涉任何所有权状态
            if isinstance(stmt.target, ast.VarRef):
                self.use(stmt.target.name, line, write=True)
            self.read_expr(stmt.expr)
            return
        if self.has_jumps:
            raise SonCompileError(f"含 GOTO / GOSUB 的 SUB 里不能使用 {op}：标签跳转让所有权的顺序分析不可靠", line)
        if not isinstance(stmt.target, ast.VarRef):
            raise SonCompileError(f"{op} 的目标必须是变量或 ENTITY 字段", line)
        if not isinstance(stmt.expr, ast.VarRef) or "." in stmt.expr.name:
            raise SonCompileError(f"{op} 的右侧必须是一个变量（不能是表达式或字段路径）", line)

        source = stmt.expr.name
        source_key = source.lower()
        target = stmt.target.name
        target_root = target.split(".")[0]
        target_key = target_root.lower()
        if source_key not in self.info or target_key not in self.info:
            raise SonCompileError(f"{op} 两侧必须都是当前 SUB 可见的变量", line)
        if source_key == target_key:
            raise SonCompileError(f"{op} 的目标和源不能是同一个变量: {source}", line)

        # 通用状态检查：源不能已移走；目标不能已移走 / 是借用来的 / 已被借出
        self.use(source, line)
        self.use(target, line, write=True)

        target_type = resolve_path_type(target, self.scope, self.entities, line)
        source_type = self.scope[source_key].type_spec
        for label, name, type_spec in (("目标", target, target_type), ("源", source, source_type)):
            if not is_managed_type(type_spec, self.entities, self.external_modules):
                raise SonCompileError(
                    f"{op} 只适用于 STRING / SYMBOL / ERROR、callable 实体和含托管字段的 ENTITY，{label} {name} 是 {describe_type(type_spec)}",
                    line,
                )
        if not same_type_spec(target_type, source_type):
            raise SonCompileError(
                f"{op} 两侧类型必须完全一致（别名共享同一块存储，不做隐式转换）：{target} 是 {describe_type(target_type)}，{source} 是 {describe_type(source_type)}",
                line,
            )

        if stmt.mode == "move":
            self.check_move(source, target_root, line)
            self.moved.add(source_key)
        else:
            self.check_borrow(source, target, target_root, line)
            self.borrowed[target_key] = source_key

    def check_move(self, source: str, target_root: str, line: int) -> None:
        source_key = source.lower()
        info = self.info[source_key]
        source_type = self.scope[source_key].type_spec
        if info.kind == "global":
            raise SonCompileError(f"m= 的源不能是全局变量: {source}（其他 SUB 仍可能访问它）", line)
        if info.kind == "ref_param":
            raise SonCompileError(f"m= 的源不能是 AS REF 参数: {source}（它属于调用方）", line)
        if not info.mutable:
            raise SonCompileError(f"m= 的源不能是 CONST: {source}", line)
        if not _frame_owns(info.kind, source_type):
            raise SonCompileError(f"m= 的源不能是按值传入的 SYMBOL / ERROR 参数: {source}（本帧不持有它的所有权）", line)
        if source_key in self.borrowed:
            raise SonCompileError(f"借用来的变量不持有所有权，不能被 m= 移走: {source}", line)
        if self.frozen(source_key):
            raise SonCompileError(f"变量已被 f= 借出，不能被 m= 移走: {source}", line)
        if self.loop_blocks and info.block < self.loop_blocks[-1]:
            raise SonCompileError(f"循环体内不能移走循环外声明的变量: {source}（下一轮迭代会读到已移走的值）", line)

        target_info = self.info[target_root.lower()]
        target_root_type = self.scope[target_root.lower()].type_spec
        if target_info.kind == "param" and not _frame_owns("param", target_root_type):
            raise SonCompileError(f"m= 的目标不能是按值传入的 SYMBOL / ERROR 参数: {target_root}（本帧不持有它，释放旧值会动到调用方）", line)

    def check_borrow(self, source: str, target: str, target_root: str, line: int) -> None:
        info = self.info[source.lower()]
        if info.kind == "global":
            raise SonCompileError(f"f= 的源不能是全局变量: {source}（其他 SUB 可能修改或释放它，冻结管不到）", line)
        if "." in target:
            raise SonCompileError(f"f= 的目标必须是变量本身，不能是字段路径: {target}", line)
        target_info = self.info[target_root.lower()]
        if target_info.kind != "local" or target_info.block != self.block_stack[-1]:
            raise SonCompileError(
                f"f= 的目标必须是与该语句同一个块里 DIM 的局部变量: {target}（借用靠把目标从块尾清理里摘掉实现，跨块会漏掉没走借用分支时目标自己的值）",
                line,
            )

    # --- 访问检查 ---

    def use(self, name: str, line: int, write: bool = False) -> None:
        """每一次对变量的触碰都经过这里。read 只拦「已移走」；write（含取址、REF 传参、
        INPUT、写字段）还要拦「借用来的」和「已借出的」。"""
        key = name.split(".")[0].lower()
        if key not in self.info:
            return
        if key in self.moved:
            raise SonCompileError(f"变量已被 m= 移走: {name}", line)
        if not write:
            return
        if key in self.borrowed:
            raise SonCompileError(f"借用来的变量是只读的，不能赋值、取址、传 REF 或写字段: {name}", line)
        if self.frozen(key):
            raise SonCompileError(f"变量已被 f= 借出，借用存续期间不能赋值、取址、传 REF 或写字段: {name}", line)

    def read_expr(self, expr: ast.Expr | None) -> None:
        if expr is None:
            return
        if isinstance(expr, ast.VarRef):
            self.use(expr.name, expr.line_no)
        elif isinstance(expr, ast.AddressOf):
            if isinstance(expr.expr, ast.VarRef):
                self.use(expr.expr.name, expr.line_no, write=True)
            else:
                self.read_expr(expr.expr)
        elif isinstance(expr, ast.Unary | ast.Deref | ast.Cast):
            self.read_expr(expr.expr)
        elif isinstance(expr, ast.Binary):
            self.read_expr(expr.left)
            self.read_expr(expr.right)
        elif isinstance(expr, ast.Index):
            self.read_expr(expr.base)
            self.read_expr(expr.index)
        elif isinstance(expr, ast.FString):
            for part in expr.parts:
                if isinstance(part, ast.Expr):
                    self.read_expr(part)
        elif isinstance(expr, ast.CallExpr):
            self.call_args(expr.name, expr.args, expr.line_no)
        elif isinstance(expr, ast.AwaitExpr | ast.SyncExpr):
            self.read_expr(expr.operand)

    def call_args(self, name: str, args: list[ast.Expr], line: int) -> None:
        # 经 callable / 函数引用变量调用，本身就是对这个变量的一次读：被 m= 移走之后再调用要报错
        if self.callable_var(name, line) is not None:
            self.use(name, line)
        params = self.callee_params(name, line)
        for index, arg in enumerate(args):
            by_ref = index < len(params) and params[index].by_ref
            if by_ref and isinstance(arg, ast.VarRef):
                self.use(arg.name, arg.line_no, write=True)
            else:
                self.read_expr(arg)

    def callable_var(self, name: str, line: int) -> ast.TypeSpec | None:
        if self.subs.get(name.lower()) is not None:
            return None
        return callable_symbol_type(name, self.scope, self.entities, line)

    def callee_params(self, name: str, line: int) -> list[ast.Param]:
        sub = self.subs.get(name.lower())
        if sub is not None:
            return sub.params
        callee = self.callable_var(name, line)
        if callee is not None:
            return list(sub_signature(callee).params or ())
        split = split_module_member(name)
        if split is not None:
            alias, member = split
            module = self.external_modules.get(alias)
            if module is not None and member.lower() in module.subs:
                return module.subs[member.lower()].params
        c_func = resolve_c_func(name, self.c_funcs)
        return c_func.params if c_func is not None else []


def _index_root(expr: ast.Index) -> ast.VarRef | None:
    base = expr.base
    while isinstance(base, ast.Index):
        base = base.base
    return base if isinstance(base, ast.VarRef) else None
