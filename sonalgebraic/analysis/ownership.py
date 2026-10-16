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
from ..core.scopes import ScopeGraph
from ..core.walk import child_bodies as _child_bodies, walk_statements
from .builtin_effects import ArgumentEffect, builtin_effect
from .resource_traits import has_explicit_resources, resource_traits
from .typesys import callable_symbol_type, describe_type, is_managed_handle, is_managed_resource, is_string, is_sub_ptr, is_sub_type, resolve_c_func, resolve_managed_container_function, resolve_path_type, same_type_spec, sub_signature, type_of

if TYPE_CHECKING:
    from .semantics import Symbol


_OP = {"borrow": "f=", "move": "m="}
_FlowState = tuple[set[str], dict[str, str], set[str]]


def is_managed_type(
    type_spec: ast.TypeSpec,
    entities: dict[str, ast.EntityDef],
    external_modules: dict[str, ModuleExports],
) -> bool:
    """只判断 f=/m= 的资源类型；PROMISE 继续由单消费者协议负责。"""
    if type_spec.array_size is not None or type_spec.name == "PROMISE":
        return False
    return resource_traits(type_spec, entities, external_modules).has_resources


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


def sub_has_explicit_resources(
    sub: ast.Subroutine,
    symbols: dict[str, Symbol],
    entities: dict[str, ast.EntityDef],
    external_modules: dict[str, ModuleExports],
) -> bool:
    kinds = [sub.return_type, *(param.type_spec for param in sub.params),
             *(symbol.type_spec for symbol in symbols.values()),
             *(stmt.type_spec for stmt in walk_statements(sub.body) if isinstance(stmt, ast.LocalDeclaration))]
    for kind in kinds:
        try:
            if has_explicit_resources(kind, entities, external_modules):
                return True
        except SonCompileError:
            # 类型错误由语义检查给出；此处只探测是否需要资源检查。
            pass
    return False


def check_ownership(
    sub: ast.Subroutine,
    symbols: dict[str, Symbol],
    subs: dict[str, ast.Subroutine],
    entities: dict[str, ast.EntityDef],
    external_modules: dict[str, ModuleExports],
    c_funcs: dict[str, ast.CFunctionDecl],
    uses: dict[str, str] | None = None,
) -> None:
    """在 check_stmt 全部通过之后调用：这里假定变量都已声明、类型都能解析。"""
    if not uses_ownership_assign(sub.body) and not sub_has_explicit_resources(sub, symbols, entities, external_modules):
        return
    _Walker(sub, symbols, subs, entities, external_modules, c_funcs, uses or {}).run()


class _Walker:
    def __init__(
        self,
        sub: ast.Subroutine,
        symbols: dict[str, Symbol],
        subs: dict[str, ast.Subroutine],
        entities: dict[str, ast.EntityDef],
        external_modules: dict[str, ModuleExports],
        c_funcs: dict[str, ast.CFunctionDecl],
        uses: dict[str, str],
    ) -> None:
        from .semantics import Symbol

        self.sub = sub
        self.subs = subs
        self.entities = entities
        self.external_modules = external_modules
        self.c_funcs = c_funcs
        self.uses = uses
        self.explicit_resources = sub_has_explicit_resources(sub, symbols, entities, external_modules)
        self.has_jumps = _has_jumps(sub.body)
        self.graph = ScopeGraph.for_body(sub.body, protected=self.explicit_resources)
        self.checked_jumps = self.graph.protected and not any(isinstance(stmt, ast.Gosub) for stmt in walk_statements(sub.body))
        self.jump_states: dict[str, list[_FlowState]] = {}
        self.label_states: dict[str, _FlowState] = {}
        self.label_names: dict[str, set[str]] = {}

        self.scope: dict[str, Symbol] = symbols.copy()
        self.info: dict[str, _VarInfo] = {key: _VarInfo("global", 0, symbol.mutable) for key, symbol in symbols.items()}
        for param in sub.params:
            key = param.name.lower()
            self.scope[key] = Symbol(param.name, param.type_spec, True, param.by_ref)
            self.info[key] = _VarInfo("ref_param" if param.by_ref else "param", 0, True)

        self.moved: set[str] = set()
        self.closed: set[str] = set()
        # f= 的目标 -> 源。目标不持有所有权、只读；源在任一借用者存活期间冻结（不能改）。
        # 借用者死在块尾，源随之解冻——这就是为什么用映射而不是两个 set。
        self.borrowed: dict[str, str] = {}
        self.block_counter = 0
        self.block_stack: list[int] = []
        self.loop_blocks: list[int] = []
        self.declared_stack: list[list[str]] = []

    def run(self) -> None:
        if self.explicit_resources and any(isinstance(stmt, ast.Gosub) for stmt in walk_statements(self.sub.body)):
            raise SonCompileError("含显式托管资源的 SUB 暂不支持 GOSUB，请使用 SUB 调用", self.sub.line_no)
        for param in self.sub.params:
            if self.move_only(param.type_spec) and not param.by_ref:
                raise SonCompileError(f"独占资源参数必须使用 AS REF，不能按值复制: {param.name}", param.line_no)
        self.block(self.sub.body)

    def move_only(self, kind: ast.TypeSpec) -> bool:
        traits = resource_traits(kind, self.entities, self.external_modules)
        return traits.explicit and traits.move_only

    def expr_type(self, expr: ast.Expr) -> ast.TypeSpec:
        return type_of(expr, self.scope, self.subs, self.entities, self.uses, self.external_modules, self.c_funcs)

    def check_store(self, kind: ast.TypeSpec, expr: ast.Expr) -> None:
        if not self.move_only(kind) or isinstance(expr, ast.NullLiteral):
            return
        source = self.expr_type(expr)
        if isinstance(expr, ast.NewContainer):
            if same_type_spec(kind, source):
                return
            raise SonCompileError(
                f"容器类型不符：声明是 {describe_type(kind)}，构造的是 {describe_type(source)}", expr.line_no)
        if isinstance(expr, ast.CallExpr):
            effect = builtin_effect(expr.name, self.uses)
            if effect is not None and is_managed_handle(kind) and effect.fresh_handle == kind.subtype:
                return
            if same_type_spec(kind, source):
                return
        if isinstance(expr, ast.AwaitExpr | ast.SyncExpr) and same_type_spec(kind, source):
            return
        raise SonCompileError("独占资源不能用 = 复制；请用 m= 移动、f= 借用或接管新建资源", expr.line_no)

    def reinitialize(self, name: str) -> None:
        path = name.lower()
        self.closed = {item for item in self.closed if item != path and not item.startswith(path + ".")}
        self.moved = {item for item in self.moved if item != path and not item.startswith(path + ".")}

    # --- 作用域 ---

    def block(self, body: list[ast.Stmt], loop: bool = False) -> bool:
        self.block_counter += 1
        block_id = self.block_counter
        self.block_stack.append(block_id)
        if loop:
            self.loop_blocks.append(block_id)
        declared: list[str] = []
        self.declared_stack.append(declared)
        live = self.body(body)
        self.declared_stack.pop()
        self.leave_block(declared)
        if loop:
            self.loop_blocks.pop()
        self.block_stack.pop()
        return live

    def body(self, body: list[ast.Stmt]) -> bool:
        live = True
        for stmt in body:
            if self.checked_jumps and isinstance(stmt, ast.Label):
                key = stmt.name.lower()
                states = [self.visible_state(state, set(self.scope)) for state in self.jump_states.pop(key, [])]
                if live:
                    states.append(self.snapshot())
                if not states:
                    continue
                self.merge(states)
                self.label_states[key] = self.snapshot()
                self.label_names[key] = set(self.scope)
                live = True
            elif live or self.has_jumps and not self.checked_jumps:
                live = self.stmt(stmt)
        return live

    def visible_state(self, state: _FlowState, names: set[str]) -> _FlowState:
        return ({key for key in state[0] if key.split(".")[0] in names},
                {key: source for key, source in state[1].items() if key in names},
                {key for key in state[2] if key.split(".")[0] in names})

    def loop_body(self, body: list[ast.Stmt]) -> None:
        incoming = self.snapshot()
        states = [incoming]
        if self.block(body, loop=True):
            states.append(self.snapshot())
        self.merge(states)

    def snapshot(self) -> _FlowState:
        return self.moved.copy(), self.borrowed.copy(), self.closed.copy()

    def restore(self, state: _FlowState) -> None:
        self.moved, self.borrowed, self.closed = state[0].copy(), state[1].copy(), state[2].copy()

    def merge(self, states: list[_FlowState]) -> None:
        self.moved = set().union(*(state[0] for state in states))
        self.borrowed = {key: source for _, borrowed, _ in states for key, source in borrowed.items()}
        self.closed = set().union(*(state[2] for state in states))

    def leave_block(self, declared: list[str]) -> None:
        # 块内声明的变量随块死亡，连同它们的所有权状态一起清掉：兄弟块可以再声明同名变量，
        # 它们借走的源也随之解冻。块外变量在块内被移走的状态则保留——任一分支移走就算死。
        for key in declared:
            self.scope.pop(key, None)
            self.info.pop(key, None)
            self.moved.discard(key)
            self.borrowed.pop(key, None)
        self.moved = {path for path in self.moved if path.split(".")[0] not in declared}
        self.closed = {path for path in self.closed if path.split(".")[0] not in declared}

    def frozen(self, key: str) -> bool:
        return any(source == key for source in self.borrowed.values())

    def declare(self, name: str, type_spec: ast.TypeSpec, mutable: bool) -> None:
        from .semantics import Symbol

        key = name.lower()
        self.scope[key] = Symbol(name, type_spec, mutable)
        self.info[key] = _VarInfo("local", self.block_stack[-1], mutable)
        self.declared_stack[-1].append(key)

    # --- 语句 ---

    def stmt(self, stmt: ast.Stmt) -> bool:
        if isinstance(stmt, ast.LocalDeclaration):
            self.declare(stmt.name, stmt.type_spec, stmt.mutable)
            if stmt.expr is not None:
                self.check_store(stmt.type_spec, stmt.expr)
                self.read_expr(stmt.expr)
        elif isinstance(stmt, ast.Assign):
            if stmt.mode != "copy":
                self.ownership_assign(stmt)
                return True
            if isinstance(stmt.target, ast.VarRef):
                target_type = self.expr_type(stmt.target)
                self.check_store(target_type, stmt.expr)
                self.use(stmt.target.name, stmt.line_no, write=True, replace=self.move_only(target_type))
            elif isinstance(stmt.target, ast.Deref):
                self.read_expr(stmt.target.expr)
            elif isinstance(stmt.target, ast.Index):
                root = _index_root(stmt.target)
                if root is not None:
                    self.use(root.name, stmt.line_no, write=True)
                self.read_expr(stmt.target.index)
            self.read_expr(stmt.expr)
            if isinstance(stmt.target, ast.VarRef):
                self.reinitialize(stmt.target.name)
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
            source_type = type_of(stmt.source, self.scope, self.subs, self.entities, self.uses, self.external_modules, self.c_funcs)
            self.declare(stmt.name, sub_signature(source_type), True)
        elif isinstance(stmt, ast.TryCatch):
            self.call_args(stmt.call_name, stmt.args, stmt.line_no)
            self.use(stmt.traceback_var, stmt.line_no, write=True)
            incoming = self.snapshot()
            states = [incoming]
            for branch in stmt.catches:
                self.restore(incoming)
                if self.catch_block(branch):
                    states.append(self.snapshot())
            self.merge(states)
        elif isinstance(stmt, ast.ThrowNew):
            self.read_expr(stmt.message)
        elif isinstance(stmt, ast.ThrowVar):
            self.use(stmt.name, stmt.line_no)
        elif isinstance(stmt, ast.Scope):
            return self.block(stmt.body)
        elif isinstance(stmt, ast.If):
            self.read_expr(stmt.condition)
            incoming = self.snapshot()
            states: list[_FlowState] = []
            if self.block(stmt.body):
                states.append(self.snapshot())
            for branch in stmt.elifs:
                self.restore(incoming)
                self.read_expr(branch.condition)
                incoming = self.snapshot()
                if self.block(branch.body):
                    states.append(self.snapshot())
            self.restore(incoming)
            if self.block(stmt.else_body):
                states.append(self.snapshot())
            self.merge(states)
            return bool(states)
        elif isinstance(stmt, ast.ForLoop):
            self.use(stmt.var, stmt.line_no, write=True)
            self.read_expr(stmt.start)
            self.read_expr(stmt.end)
            self.read_expr(stmt.step)
            self.loop_body(stmt.body)
        elif isinstance(stmt, ast.WhileLoop):
            incoming = self.snapshot()
            self.read_expr(stmt.condition)
            if self.moved != incoming[0] or self.closed != incoming[2]:
                raise SonCompileError("循环条件不能消费资源；下一次判断会再次访问已关闭或移动的值", stmt.line_no)
            self.loop_body(stmt.body)
        elif isinstance(stmt, ast.Goto) and self.checked_jumps:
            key = stmt.label.lower()
            if key in self.label_states:
                state = self.visible_state(self.snapshot(), self.label_names[key])
                if state != self.label_states[key]:
                    raise SonCompileError("跳转回边不能改变仍存活变量的移动或借用状态", stmt.line_no)
            elif self.graph.labels[key].line_no < stmt.line_no:
                raise SonCompileError("所有权检查不支持回跳到尚未分析的不可达标签", stmt.line_no)
            else:
                self.jump_states.setdefault(key, []).append(self.snapshot())
            return False
        elif isinstance(stmt, ast.Return):
            self.read_expr(stmt.expr)
            if stmt.expr is not None and self.move_only(self.sub.return_type):
                if isinstance(stmt.expr, ast.VarRef):
                    root = stmt.expr.name.split(".")[0].lower()
                    if root in self.borrowed or self.info[root].kind != "local":
                        raise SonCompileError("RETURN 不能转出借用、REF 参数或全局资源的所有权", stmt.line_no)
                    if self.frozen(root):
                        raise SonCompileError("RETURN 不能转出仍被 f= 借出的独占资源", stmt.line_no)
                    if "." in stmt.expr.name:
                        raise SonCompileError("独占字段请先 m= 移入局部变量再 RETURN", stmt.line_no)
                else:
                    self.check_store(self.sub.return_type, stmt.expr)
        elif isinstance(stmt, ast.AwaitStmt):
            self.read_expr(stmt.expr)
        return not isinstance(stmt, ast.Return | ast.CallRet | ast.ThrowNew | ast.ThrowVar)

    def catch_block(self, branch: ast.CatchBranch) -> bool:
        # CATCH 别名是块内的只读 ERROR 局部：declare 要在块里做，所以不能直接复用 block()。
        self.block_counter += 1
        block_id = self.block_counter
        self.block_stack.append(block_id)
        declared: list[str] = []
        self.declared_stack.append(declared)
        self.declare(branch.alias, ast.TypeSpec("ERROR"), False)
        live = self.body(branch.body)
        self.declared_stack.pop()
        self.leave_block(declared)
        self.block_stack.pop()
        return live

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
        if self.has_jumps and not self.checked_jumps:
            raise SonCompileError(f"含 GOTO / GOSUB 的 SUB 里不能使用 {op}：标签跳转让所有权的顺序分析不可靠", line)
        if not isinstance(stmt.target, ast.VarRef):
            raise SonCompileError(f"{op} 的目标必须是变量或 ENTITY 字段", line)
        if not isinstance(stmt.expr, ast.VarRef):
            if stmt.mode == "borrow" and self.container_borrow(stmt, line):
                return
            raise SonCompileError(f"{op} 的右侧必须是一个变量或字段路径（不能是表达式）", line)

        source = stmt.expr.name
        source_path = source.lower()
        source_key = source_path.split(".")[0]
        target = stmt.target.name
        target_path = target.lower()
        target_root = target.split(".")[0]
        target_key = target_root.lower()
        if source_key not in self.info or target_key not in self.info:
            raise SonCompileError(f"{op} 两侧必须都是当前 SUB 可见的变量", line)
        if source_path == target_path or source_path.startswith(target_path + ".") or target_path.startswith(source_path + "."):
            raise SonCompileError(f"{op} 的目标和源不能是同一个变量或重叠的父子字段: {source}", line)

        target_type = resolve_path_type(target, self.scope, self.entities, line)
        source_type = resolve_path_type(source, self.scope, self.entities, line)
        self.use(source, line)
        self.use(target, line, write=True, replace=self.move_only(target_type))
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
            self.moved.add(source_path)
        else:
            self.check_borrow(source, target, target_root, line)
            self.borrowed[target_key] = source_key
        self.reinitialize(target)

    def check_move(self, source: str, target_root: str, line: int) -> None:
        source_key = source.split(".")[0].lower()
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
        info = self.info[source.split(".")[0].lower()]
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

    def container_borrow(self, stmt: ast.Assign, line: int) -> bool:
        """`目标 f= 容器.BORROW(下标)`：借到的是容器里那一格，所以冻结的是容器本身。

容器一改就可能整块 realloc（扩容）或者把那一格挪走（删除、覆盖），借用视图随即
悬空。effect 表已经把除查询和 BORROW 以外的容器操作都标成修改，所以冻结容器这个
根变量就足够挡住它们；容器被 m= 移走、CLOSE 掉或者整体覆盖，拦在同一个 frozen 上。

不是容器 BORROW 就交回去（返回 False），让调用方照旧报「f= 的右侧必须是变量」。
"""
        expr = stmt.expr
        if not isinstance(expr, ast.CallExpr) or (split_module_member(expr.name) or ("", ""))[1].upper() != "BORROW":
            return False
        if resolve_managed_container_function(expr, self.uses, self.scope, self.entities) is None:
            return False
        assert isinstance(stmt.target, ast.VarRef)
        container = expr.args[0]
        assert isinstance(container, ast.VarRef)
        target = stmt.target.name
        target_root = target.split(".")[0]
        for arg in expr.args[1:]:
            self.read_expr(arg)
        self.use(container.name, line)
        target_type = resolve_path_type(target, self.scope, self.entities, line)
        self.use(target, line, write=True, replace=self.move_only(target_type))
        self.check_borrow(container.name, target, target_root, line)
        self.borrowed[target_root.lower()] = container.name.split(".")[0].lower()
        self.reinitialize(target)
        return True

    # --- 访问检查 ---

    @staticmethod
    def _unavailable(paths: set[str], path: str, replace: bool) -> bool:
        """失效沿字段路径向下传播：父级失效挡住全部字段，而覆盖一个槽位只恢复它自己和它的子字段。"""
        if any(path.startswith(item + ".") for item in paths):
            return True
        return not replace and any(item == path or item.startswith(path + ".") for item in paths)

    def use(self, name: str, line: int, write: bool = False, replace: bool = False) -> None:
        """覆盖独占槽位可以恢复已移动／关闭状态，但不能绕过借用冻结。"""
        path = name.lower()
        key = path.split(".")[0]
        if key not in self.info:
            return
        if self._unavailable(self.moved, path, replace):
            raise SonCompileError(f"变量已被 m= 移走: {name}", line)
        if self._unavailable(self.closed, path, replace):
            raise SonCompileError(f"托管资源已关闭，重新初始化后才能使用: {name}", line)
        if not write:
            return
        if not self.info[key].mutable:
            raise SonCompileError(f"不能修改或关闭 CONST 资源: {name}", line)
        if replace and self.info[key].kind == "ref_param":
            raise SonCompileError(f"不能替换 AS REF 参数的独占资源；它属于调用方: {name}", line)
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
            incoming = self.snapshot()
            self.read_expr(expr.right)
            if expr.op in {"AND", "OR"}:
                self.merge([incoming, self.snapshot()])
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
        effect = builtin_effect(name, self.uses)
        for arg in args:
            self.read_expr(arg)
        for index, arg in enumerate(args):
            kind = self.expr_type(arg)
            by_ref = index < len(params) and params[index].by_ref
            if effect is not None and is_managed_resource(kind):
                action = effect.argument(index)
                if not isinstance(arg, ast.VarRef):
                    raise SonCompileError("托管资源操作需要变量或字段；请先接管返回值", arg.line_no)
                self.use(arg.name, arg.line_no, write=action is not ArgumentEffect.READ)
                if action is ArgumentEffect.CONSUME:
                    root = arg.name.split(".")[0].lower()
                    info = self.info[root]
                    if info.kind == "ref_param":
                        raise SonCompileError("不能 CLOSE 调用方的 AS REF 资源", arg.line_no)
                    if self.loop_blocks and info.block < self.loop_blocks[-1]:
                        raise SonCompileError("循环体内不能 CLOSE 循环外声明的资源", arg.line_no)
                    self.closed.add(arg.name.lower())
                elif action is ArgumentEffect.CAPTURE:
                    raise SonCompileError("托管资源不能逃逸到保存参数的调用", arg.line_no)
            elif by_ref and isinstance(arg, ast.VarRef):
                self.use(arg.name, arg.line_no, write=True)
            else:
                if self.move_only(kind) and params:
                    raise SonCompileError("独占资源不能按值传参；请使用 AS REF", arg.line_no)
                if isinstance(arg, ast.VarRef):
                    self.use(arg.name, arg.line_no)

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
