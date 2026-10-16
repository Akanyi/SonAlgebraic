"""实例身份与词法生命周期分开建模，导入名字不等于取得外部函数定义。"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum

from . import ast
from .errors import SonCompileError
from .module_model import ModuleExports
from .names import split_module_member
from .walk import child_bodies, walk_statements


class FunctionIdentity(str, Enum):
    REAL = "real"
    NAMED_VIRTUAL = "named_virtual"
    ANONYMOUS_VIRTUAL = "anonymous_virtual"


@dataclass(frozen=True)
class FunctionBinding:
    name: str
    defining_instance: str | None
    observing_instance: str
    identity: FunctionIdentity
    signature: ast.TypeSpec
    declaration: ast.Subroutine | ast.CFunctionDecl | None = None
    definition: ast.Subroutine | None = None
    origin: str = "sa"

    def imported(self, name: str, instance: str) -> "FunctionBinding":
        declaration = self.declaration
        if isinstance(declaration, ast.Subroutine):
            declaration = replace(declaration, body=[])
        return replace(self, name=name, observing_instance=instance,
                       identity=FunctionIdentity.NAMED_VIRTUAL, declaration=declaration, definition=None)

    @classmethod
    def callable(cls, name: str, signature: ast.TypeSpec, instance: str) -> "FunctionBinding":
        return cls(name, None, instance, FunctionIdentity.ANONYMOUS_VIRTUAL, signature, origin="callable")


@dataclass
class InstanceScope:
    name: str
    internal: dict[str, FunctionBinding] = field(default_factory=dict)
    public: dict[str, FunctionBinding] = field(default_factory=dict)

    def resolve(self, name: str) -> FunctionBinding | None:
        return self.internal.get(name.lower()) or self.public.get(name.lower())


class FunctionTable(dict[str, ast.Subroutine]):
    """旧调用路径仍按字典读取本地定义，跨实例查询统一走绑定身份。"""

    def __init__(self, definitions: dict[str, ast.Subroutine], instance: InstanceScope):
        super().__init__(definitions)
        self.instance = instance


def resolve_function_binding(
    name: str,
    subs: dict[str, ast.Subroutine],
    external_modules: dict[str, ModuleExports],
    c_funcs: dict[str, ast.CFunctionDecl],
    instance_name: str = "<main>",
) -> FunctionBinding | None:
    if isinstance(subs, FunctionTable):
        return subs.instance.resolve(name)
    sub = subs.get(name.lower())
    if sub is not None:
        signature = ast.TypeSpec("SUB", inner=sub.return_type, params=tuple(sub.params))
        return FunctionBinding(name, instance_name, instance_name, FunctionIdentity.REAL, signature, sub, sub)
    split = split_module_member(name)
    if split is not None:
        module = external_modules.get(split[0])
        sub = module.subs.get(split[1].lower()) if module is not None else None
        if sub is not None and module is not None and sub.visibility == "PUBLIC":
            signature = ast.TypeSpec("SUB", inner=sub.return_type, params=tuple(sub.params))
            return FunctionBinding(name, module.module.lower(), instance_name, FunctionIdentity.NAMED_VIRTUAL,
                                   signature, replace(sub, body=[]))
    declaration = c_funcs.get(name.lower())
    if declaration is not None:
        signature = ast.TypeSpec("SUB", inner=declaration.return_type, params=tuple(declaration.params))
        return FunctionBinding(name, f"c:{declaration.alias.lower()}", instance_name,
                               FunctionIdentity.NAMED_VIRTUAL, signature, declaration, origin="c")
    return None


@dataclass
class RuntimeScope:
    instances: dict[str, InstanceScope] = field(default_factory=dict)

    def instance(self, name: str) -> InstanceScope:
        key = name.lower()
        if key not in self.instances:
            self.instances[key] = InstanceScope(key)
        return self.instances[key]


def build_instance_scope(program: ast.Program, external_modules: dict[str, ModuleExports],
                         c_funcs: dict[str, ast.CFunctionDecl], name: str = "<main>") -> tuple[RuntimeScope, InstanceScope]:
    runtime = RuntimeScope()
    scope = runtime.instance(name)
    for sub in program.subs:
        signature = ast.TypeSpec("SUB", inner=sub.return_type, params=tuple(sub.params))
        scope.internal[sub.name.lower()] = FunctionBinding(
            sub.name, scope.name, scope.name, FunctionIdentity.REAL, signature, sub, sub
        )
    for alias, module in external_modules.items():
        origin = runtime.instance(module.module)
        for sub in module.subs.values():
            if sub.visibility != "PUBLIC":
                continue
            signature = ast.TypeSpec("SUB", inner=sub.return_type, params=tuple(sub.params))
            qualified = f"{alias}.{sub.name}"
            declaration = replace(sub, body=[])
            binding = FunctionBinding(sub.name, origin.name, origin.name, FunctionIdentity.REAL,
                                      signature, declaration)
            scope.public[qualified.lower()] = binding.imported(qualified, scope.name)
    for qualified, declaration in c_funcs.items():
        signature = ast.TypeSpec("SUB", inner=declaration.return_type, params=tuple(declaration.params))
        scope.public[qualified.lower()] = FunctionBinding(
            qualified, f"c:{declaration.alias.lower()}", scope.name,
            FunctionIdentity.NAMED_VIRTUAL, signature, declaration, origin="c"
        )
    return runtime, scope


@dataclass
class ScopeGraph:
    paths: dict[int, tuple[int, ...]] = field(default_factory=dict)
    initialized: dict[int, frozenset[int]] = field(default_factory=dict)
    declarations: dict[int, tuple[int, ...]] = field(default_factory=dict)
    lifetime_events: dict[int, tuple[int, ...]] = field(default_factory=dict)
    labels: dict[str, ast.Label] = field(default_factory=dict)
    jumps: list[ast.Goto | ast.Gosub] = field(default_factory=list)
    exit_depths: dict[int, int] = field(default_factory=dict)
    protected: bool = False
    _next_scope: int = 0

    @classmethod
    def for_body(cls, body: list[ast.Stmt], *, protected: bool = False) -> "ScopeGraph":
        graph = cls(protected=protected or any(isinstance(stmt, ast.Scope) for stmt in walk_statements(body)))
        graph._visit(body, (), frozenset())
        if graph.protected:
            graph.validate_jumps()
        return graph

    def _visit(self, body: list[ast.Stmt], path: tuple[int, ...], initialized: frozenset[int]) -> None:
        for stmt in body:
            self.paths[stmt.line_no] = path
            self.initialized[stmt.line_no] = initialized
            if isinstance(stmt, ast.Label):
                key = stmt.name.lower()
                if key in self.labels and self.protected:
                    raise SonCompileError(f"重复标签: {stmt.name}", stmt.line_no)
                self.labels[key] = stmt
            elif isinstance(stmt, ast.Goto | ast.Gosub):
                self.jumps.append(stmt)
            elif isinstance(stmt, ast.LocalDeclaration | ast.NewSub):
                self.declarations[stmt.line_no] = path
                self.lifetime_events[stmt.line_no] = path
                initialized = initialized | {stmt.line_no}
            elif isinstance(stmt, ast.Assign) and stmt.mode == "borrow":
                # 借用绑定同样建立生命周期边界。
                self.lifetime_events[stmt.line_no] = path
                initialized = initialized | {stmt.line_no}
            for nested in child_bodies(stmt):
                self._next_scope += 1
                self._visit(nested, (*path, self._next_scope), initialized)

    def validate_jumps(self) -> None:
        for jump in self.jumps:
            target = self.labels.get(jump.label.lower())
            if target is None:
                raise SonCompileError(f"未知标签: {jump.label}", jump.line_no)
            source_path = self.paths[jump.line_no]
            target_path = self.paths[target.line_no]
            if source_path[:len(target_path)] != target_path:
                raise SonCompileError("不能跳入另一个作用域或绕过它的初始化", jump.line_no)
            depth = len(source_path) - len(target_path)
            if depth and isinstance(jump, ast.Gosub):
                raise SonCompileError("GOSUB 不能跨越托管作用域边界，请使用同层标签或 SUB", jump.line_no)

            def retained(line: int) -> frozenset[int]:
                return frozenset(declaration for declaration in self.initialized[line]
                                 if target_path[:len(self.lifetime_events[declaration])] == self.lifetime_events[declaration])
            if retained(jump.line_no) != retained(target.line_no):
                raise SonCompileError("跳转不能越过仍存活作用域中的局部声明或借用绑定", jump.line_no)
            self.exit_depths[jump.line_no] = depth
