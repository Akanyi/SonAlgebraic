"""C 后端生成器的共享状态与底层设施。

CGen 的全部 dataclass 字段都只在这里声明：各 Mixin 是纯方法类、不带字段，字段散落到
多个 dataclass 基类里会让 __init__ 的参数顺序取决于 MRO，读代码的人猜不出来。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ...analysis.semantics import CheckedProgram, Symbol
from ...analysis.typesys import c_type, is_error, is_handle, is_numeric, is_promise, is_string, is_symbol, runtime_features_for_program, type_of
from ...core import ast
from ...core.errors import SonCompileError
from ...core.names import entity_c_name, c_ident as make_c_ident, module_symbol_prefix, split_module_member


@dataclass
class AsyncFrameCtx:
    """一个 ASYNC SUB 的协程帧生成上下文。生成 resume 体前压栈、生成完出栈。

    frame_vars 是提升进帧的变量名集合（参数 + 所有局部，首期无脑全提升）。codegen 的
    标识符解析（c_value）命中它就把名字重写成 `f-><c_ident>`——这是把同步 codegen 复用
    到协程体的唯一闸口。resume_points 按源码顺序给每个 AWAIT 分配恢复点编号，最后拼成
    resume 顶部的 switch 派发表（照抄 GOSUB 的返回地址 switch）。 """
    sub: ast.Subroutine
    frame_type: str
    frame_vars: set[str]
    resume_points: list[int] = field(default_factory=list)
    # FOR 循环的上界/步长也要提升进帧（按 line_no 记）：循环体含 AWAIT 时，resume 用 goto
    # 跳进体中间，会跳过栈上的上界/步长初始化，留下垃圾值让循环失控。提升进帧才能在挂起—
    # 恢复之间保持有效，和循环变量本身一个道理。
    for_frame_lines: set[int] = field(default_factory=set)

    def next_resume_point(self) -> int:
        point = len(self.resume_points) + 1
        self.resume_points.append(point)
        return point


@dataclass
class CGenBase:
    """生成器状态 + 所有 Mixin 都要用的小工具：临时量、prelude/cleanup 栈、作用域、命名、类型名。"""

    checked: CheckedProgram
    module_name: str | None = None
    include_runtime: bool = True
    include_main: bool = True
    include_headers: list[str] = field(default_factory=list)
    main_init_calls: list[str] = field(default_factory=list)
    main_free_calls: list[str] = field(default_factory=list)
    dynamic: bool = False
    temp_index: int = 0
    prelude_stack: list[list[str]] = field(default_factory=list)
    cleanup_stack: list[list[str]] = field(default_factory=list)
    local_resource_stack: list[list[tuple[str, ast.TypeSpec]]] = field(default_factory=list)
    scope_stack: list[dict[str, Symbol]] = field(default_factory=list)
    sub_name_stack: list[str] = field(default_factory=list)
    sub_return_type_stack: list[ast.TypeSpec] = field(default_factory=list)
    sub_gosub_stack: list[bool] = field(default_factory=list)
    sub_gosub_lines_stack: list[list[int]] = field(default_factory=list)
    sub_has_goto_stack: list[bool] = field(default_factory=list)
    # 当 SUB 含 GOSUB 或 GOTO 时，CATCH 的 SaError 变量必须提升到函数作用域：
    # GOSUB 的 RETURN 会 goto 回到 CATCH 块内的返回标签；GOTO 则可能从 CATCH 块内部直接
    # 跳出，跳过块尾的 sa_error_clear。两种情况下若 SaError 是块内自动变量，跳转后对其
    # message 调 free 会读到野指针或干脆漏掉清理。提升后配合 SUB 末尾兜底清理（clear 幂等，
    # 不会双重 free）。键为 C 变量名，去重（不同 CATCH 不会同时存活）。
    hoisted_catch_vars: dict[str, str] = field(default_factory=dict)
    # 当前正在生成的 ASYNC SUB 协程帧上下文（嵌套 async 不会发生——async sub 体里调另一个
    # async sub 是 start，不是内联展开），命中帧变量集合的标识符会被重写成 f-> 帧字段访问。
    async_frame_stack: list[AsyncFrameCtx] = field(default_factory=list)


    @property
    def symbols(self) -> dict[str, Symbol]:
        if self.scope_stack:
            return self.scope_stack[-1]
        return self.checked.symbols

    @property
    def source_lines(self) -> dict[int, str]:
        return self.checked.program.source_lines

    @property
    def c_headers(self) -> dict[str, ast.UseCHeader]:
        return self.checked.c_headers

    @property
    def c_funcs(self) -> dict[str, ast.CFunctionDecl]:
        return self.checked.c_funcs

    def add_prelude(self, line: str) -> None:
        if not self.prelude_stack:
            raise SonCompileError("内部错误: 表达式临时代码没有归属语句")
        self.prelude_stack[-1].append(line)

    def add_cleanup(self, line: str) -> None:
        if not self.cleanup_stack:
            raise SonCompileError("内部错误: 表达式清理代码没有归属语句")
        self.cleanup_stack[-1].append(line)

    def register_local_resource(self, name: str, type_spec: ast.TypeSpec) -> None:
        if not self.local_resource_stack:
            return
        # STRING 数组按托管资源登记（清理时逐元素 free）
        if type_spec.array_size is not None:
            if is_string(type_spec):
                self.local_resource_stack[-1].append((name, type_spec))
            return
        if is_string(type_spec) or is_symbol(type_spec) or is_error(type_spec) or is_promise(type_spec) or (type_spec.name == "ENTITY" and self.type_has_managed_resources(type_spec)):
            self.local_resource_stack[-1].append((name, type_spec))

    def active_local_resource_cleanup_lines(self, indent: int) -> list[str]:
        lines: list[str] = []
        for resources in reversed(self.local_resource_stack):
            lines.extend(self.local_resource_cleanup_lines(resources, indent))
        return lines

    def local_resource_cleanup_lines(self, resources: list[tuple[str, ast.TypeSpec]], indent: int) -> list[str]:
        pad = "    " * indent
        lines: list[str] = []
        for name, type_spec in reversed(resources):
            if type_spec.array_size is not None:
                # STRING 数组：逐元素释放
                if is_string(type_spec):
                    idx = self.next_temp()
                    lines.append(f"{pad}for (long long {idx} = 0; {idx} < {type_spec.array_size}; {idx}++) {{")
                    lines.append(f"{pad}    free({name}[{idx}]);")
                    lines.append(f"{pad}}}")
                continue
            if is_string(type_spec):
                lines.append(f"{pad}free({name});")
            elif is_symbol(type_spec):
                lines.append(f"{pad}sa_symbol_free({name});")
            elif is_promise(type_spec):
                lines.append(f"{pad}sa_promise_release({name});")
            elif is_error(type_spec):
                lines.append(f"{pad}sa_error_clear(&{name});")
            elif type_spec.name == "ENTITY":
                lines.extend(self.entity_free_lines(name, type_spec, indent))
        return lines

    def prepare_value_param_resources(self, sub: ast.Subroutine, indent: int) -> list[str]:
        lines: list[str] = []
        for param in sub.params:
            if param.by_ref:
                continue
            name = self.c_ident(param.name)
            if is_string(param.type_spec):
                lines.append(f"{'    ' * indent}{name} = sa_strdup({name});")
                self.register_local_resource(name, param.type_spec)
            elif param.type_spec.name == "ENTITY" and self.type_has_managed_resources(param.type_spec):
                temp = self.next_temp()
                lines.append(f"{'    ' * indent}{self.c_type(param.type_spec)} {temp} = {name};")
                lines.extend(self.entity_init_lines(name, param.type_spec, indent))
                lines.extend(self.entity_copy_lines(name, temp, param.type_spec, indent))
                self.register_local_resource(name, param.type_spec)
        return lines

    def next_temp(self) -> str:
        self.temp_index += 1
        return f"sa_tmp_{self.temp_index}"

    def sub_signature(self, sub: ast.Subroutine) -> str:
        params = ", ".join(self.param_decl(param) for param in sub.params) or "void"
        storage = "" if self.is_exported_sub(sub) else "static "
        return f"{storage}{self.c_type(sub.return_type)} {self.sub_c_name(sub.name)}({params})"

    def param_decl(self, param: ast.Param) -> str:
        ctype = self.c_type(param.type_spec)
        if param.by_ref:
            return f"{ctype}* {self.c_ident(param.name)}"
        return f"{ctype} {self.c_ident(param.name)}"

    def call_args_with_prelude(self, name: str, args: list[ast.Expr]) -> tuple[list[str], list[str], list[str]]:
        sub = self.resolve_called_sub(name)
        prelude_all: list[str] = []
        cleanup_all: list[str] = []
        values: list[str] = []
        for arg, param in zip(args, sub.params):
            if param.by_ref:
                if not isinstance(arg, ast.VarRef):
                    raise SonCompileError(f"REF 参数 {param.name} 必须传入变量", arg.line_no)
                values.append(f"&({self.c_value(arg.name)})")
                continue
            prelude, value, cleanup = self.expr_with_prelude(arg)
            prelude_all.extend(prelude)
            cleanup_all.extend(cleanup)
            if is_handle(param.type_spec) and isinstance(arg, ast.NullLiteral):
                value = "0"
            values.append(value)
        return prelude_all, values, cleanup_all

    def resolve_called_sub(self, name: str) -> ast.Subroutine:
        local = self.checked.subs.get(name.lower())
        if local is not None:
            return local
        split = split_module_member(name)
        if split is not None:
            alias, member = split
            module = self.checked.external_modules.get(alias)
            if module and member.lower() in module.subs:
                return module.subs[member.lower()]
        raise SonCompileError(f"未知 SUB: {name}")

    def resolve_external_sub(self, name: str) -> ast.Subroutine | None:
        split = split_module_member(name)
        if split is None:
            return None
        alias, member = split
        module = self.checked.external_modules.get(alias)
        if module and member.lower() in module.subs:
            return module.subs[member.lower()]
        return None

    def resolve_c_func(self, name: str) -> ast.CFunctionDecl | None:
        split = split_module_member(name)
        if split is None:
            return None
        alias, member = split
        return self.c_funcs.get(f"{alias.lower()}.{member.lower()}")

    def c_call_args_with_prelude(self, c_func: ast.CFunctionDecl, args: list[ast.Expr]) -> tuple[list[str], list[str], list[str]]:
        prelude_all: list[str] = []
        cleanup_all: list[str] = []
        values: list[str] = []
        for arg, param in zip(args, c_func.params):
            if param.by_ref:
                if not isinstance(arg, ast.VarRef):
                    raise SonCompileError(f"REF 参数 {param.name} 必须传入变量", arg.line_no)
                values.append(f"&({self.c_value(arg.name)})")
                continue
            prelude, value, cleanup = self.expr_with_prelude(arg)
            prelude_all.extend(prelude)
            cleanup_all.extend(cleanup)
            values.append(self.c_cast_arg(value, param.type_spec, self.type_of(arg)))
        return prelude_all, values, cleanup_all

    def c_cast_arg(self, value: str, param_type: ast.TypeSpec, arg_type: ast.TypeSpec) -> str:
        if is_handle(param_type) and arg_type.name == "NULLT":
            return "0"
        if param_type.name == "CPTR" and is_numeric(arg_type):
            return f"(void*)({value})"
        if is_numeric(param_type) and arg_type.name == "CPTR":
            return f"(long long)({value})"
        return value

    def call_c_name(self, name: str) -> str:
        split = split_module_member(name)
        if split is not None:
            alias, member = split
            module = self.checked.external_modules.get(alias)
            if module and member.lower() in module.subs:
                return f"{module_symbol_prefix(module.module)}_sub_{member.lower()}"
        return self.sub_c_name(name)

    def external_const_c_name(self, name: str) -> str | None:
        split = split_module_member(name)
        if split is None:
            return None
        alias, member = split
        module = self.checked.external_modules.get(alias)
        if module and member.lower() in module.consts:
            return f"{module_symbol_prefix(module.module)}_const_{member.lower()}"
        return None

    def push_sub_scope(self, sub: ast.Subroutine) -> None:
        scope = self.checked.symbols.copy()
        for param in sub.params:
            scope[param.name.lower()] = Symbol(param.name, param.type_spec, True, param.by_ref)
        self.scope_stack.append(scope)

    def c_value(self, name: str) -> str:
        parts = name.split(".")
        root = parts[0]
        symbol = self.symbols[root.lower()]
        if symbol.by_ref:
            base = f"(*{self.c_ident(root)})"
            return base + ("." + ".".join(parts[1:]) if len(parts) > 1 else "")
        return self.c_ident_path(name)

    def c_ident_path(self, name: str) -> str:
        parts = name.split(".")
        field = self.async_frame_field(parts[0])
        base = field if field is not None else self.local_global_name(parts[0])
        return base + ("." + ".".join(parts[1:]) if len(parts) > 1 else "")

    def local_global_name(self, name: str) -> str:
        symbol = self.checked.symbols.get(name.lower())
        if symbol is not None and self.module_name and not symbol.mutable:
            return f"{module_symbol_prefix(self.module_name)}_const_{name.lower()}"
        return self.c_ident(name)

    def type_of(self, expr: ast.Expr) -> ast.TypeSpec:
        return type_of(expr, self.symbols, self.checked.subs, self.checked.entities, self.checked.uses, self.checked.external_modules, self.checked.c_funcs)

    def default_value(self, type_spec: ast.TypeSpec) -> str:
        if is_string(type_spec):
            return '""'
        return "0"

    def c_unary_op(self, op: str) -> str:
        return {"NOT": "!", "BNOT": "~"}.get(op, op)

    def current_sub_name(self) -> str:
        return self.sub_name_stack[-1] if self.sub_name_stack else "<top>"

    def current_sub_has_gosub(self) -> bool:
        return self.sub_gosub_stack[-1] if self.sub_gosub_stack else False

    def current_sub_has_goto(self) -> bool:
        return self.sub_has_goto_stack[-1] if self.sub_has_goto_stack else False

    def current_sub_gosub_lines(self) -> list[int]:
        return self.sub_gosub_lines_stack[-1] if self.sub_gosub_lines_stack else []

    def current_sub_return_type(self) -> ast.TypeSpec:
        return self.sub_return_type_stack[-1] if self.sub_return_type_stack else ast.TypeSpec("VOID")

    def require_arg_count(self, expr: ast.CallExpr, count: int) -> None:
        if len(expr.args) != count:
            raise SonCompileError(f"{expr.name}() 需要 {count} 个参数", expr.line_no)

    def c_ident(self, name: str) -> str:
        return make_c_ident(name)

    def global_c_name(self, decl: ast.Declaration) -> str:
        if self.is_exported_const(decl):
            return f"{module_symbol_prefix(self.module_name or '')}_const_{decl.name.lower()}"
        return self.c_ident(decl.name)

    def is_exported_const(self, decl: ast.Declaration) -> bool:
        return bool(self.module_name and not decl.mutable)

    def sub_c_name(self, name: str) -> str:
        if self.module_name and self.is_public_sub_name(name):
            return f"{module_symbol_prefix(self.module_name)}_sub_{name.lower()}"
        return self.c_ident(name)

    def is_public_sub_name(self, name: str) -> bool:
        sub = self.checked.subs.get(name.lower())
        return bool(sub and sub.visibility == "PUBLIC")

    def is_exported_sub(self, sub: ast.Subroutine) -> bool:
        return bool(self.module_name and sub.visibility == "PUBLIC")

    def c_type(self, type_spec: ast.TypeSpec) -> str:
        if type_spec.name == "ENTITY":
            return self.entity_type_from_spec(type_spec)
        return c_type(type_spec)

    def entity_type_name(self, name: str) -> str:
        if self.module_name:
            return f"{module_symbol_prefix(self.module_name)}_entity_{entity_c_name(name)}"
        return f"SaEntity_{entity_c_name(name)}"

    def entity_type_from_spec(self, type_spec: ast.TypeSpec) -> str:
        subtype = type_spec.subtype or ""
        split = split_module_member(subtype)
        if split:
            alias, member = split
            module = self.checked.external_modules.get(alias)
            if module:
                return f"{module_symbol_prefix(module.module)}_entity_{entity_c_name(member)}"
        return self.entity_type_name(subtype)

    def label_ident(self, name: str) -> str:
        return "sa_label_" + name.lower()

    def gosub_return_label(self, line_no: int) -> str:
        return f"sa_gosub_return_{line_no}"

    def sub_has_gosub(self, sub: ast.Subroutine) -> bool:
        return any(stmt_has_gosub(stmt) for stmt in sub.body)

    def sub_gosub_lines(self, sub: ast.Subroutine) -> list[int]:
        lines: list[int] = []
        for stmt in sub.body:
            lines.extend(stmt_gosub_lines(stmt))
        return list(dict.fromkeys(lines))

    def gosub_return_dispatch_lines(self, indent: int) -> list[str]:
        pad = "    " * indent
        lines = [
            f"{pad}if (sa_gosub_top > 0) {{",
            f"{pad}    switch (sa_gosub_stack[--sa_gosub_top]) {{",
        ]
        for line_no in self.current_sub_gosub_lines():
            lines.append(f"{pad}        case {line_no}: goto {self.gosub_return_label(line_no)};")
        lines.extend([
            f"{pad}        default: fputs(\"SonAlgebraic runtime: invalid GOSUB return address\\n\", stderr); exit(1);",
            f"{pad}    }}",
            f"{pad}}}",
        ])
        return lines

    def uses_net(self) -> bool:
        return "SYS.NET" in self.checked.uses.values()

    def runtime_features(self) -> set[str]:
        return runtime_features_for_program(self.checked.program, self.checked.uses)

    def runtime_feature_defines(self) -> list[str]:
        macros = {
            "net": "#define SA_ENABLE_NET",
            "tls": "#define SA_ENABLE_TLS",
            "file": "#define SA_ENABLE_FILE",
            "desktop": "#define SA_ENABLE_DESKTOP",
            "binary": "#define SA_ENABLE_BINARY",
            "list": "#define SA_ENABLE_LIST",
            "map": "#define SA_ENABLE_MAP",
            "gui": "#define SA_ENABLE_GUI",
            "async": "#define SA_ENABLE_ASYNC",
        }
        return [macros[feature] for feature in sorted(self.runtime_features()) if feature in macros]

    def runtime_feature_prefix(self) -> str:
        defines = self.runtime_feature_defines()
        return "" if not defines else "\n".join(defines) + "\n"

    def source_comment(self, line_no: int, indent: int) -> str:
        source = self.source_lines.get(line_no)
        if source is None:
            return ""
        pad = "    " * indent
        return f"{pad}/* SA {line_no}: {c_comment_text(source)} */"

    def is_math_const(self, name: str) -> bool:
        split = split_module_member(name)
        return bool(split and self.checked.uses.get(split[0]) == "SYS.MATH" and split[1].upper() == "PI")

    def is_math_function(self, name: str, function_name: str) -> bool:
        split = split_module_member(name)
        return bool(split and self.checked.uses.get(split[0]) == "SYS.MATH" and split[1].upper() == function_name.upper())


def c_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


def c_number(value: str) -> str:
    """SA 数字字面量转 C：去掉下划线分隔符。C11 原生支持十六进制和科学计数法。"""
    return value.replace("_", "")


def c_comment_text(value: str) -> str:
    return value.replace("*/", "* /")


def stmt_has_gosub(stmt: ast.Stmt) -> bool:
    if isinstance(stmt, ast.Gosub):
        return True
    if isinstance(stmt, ast.If):
        in_body = any(stmt_has_gosub(inner) for inner in stmt.body)
        in_elifs = any(stmt_has_gosub(inner) for branch in stmt.elifs for inner in branch.body)
        in_else = any(stmt_has_gosub(inner) for inner in stmt.else_body)
        return in_body or in_elifs or in_else
    if isinstance(stmt, ast.TryCatch):
        return any(stmt_has_gosub(inner) for branch in stmt.catches for inner in branch.body)
    if isinstance(stmt, ast.ForLoop | ast.WhileLoop):
        return any(stmt_has_gosub(inner) for inner in stmt.body)
    return False


def stmt_has_goto(stmt: ast.Stmt) -> bool:
    # 保守判定：SUB 内任意位置（含嵌套块、CATCH 块内）出现 GOTO，就认为存在跨块跳转风险，
    # 据此把 CATCH 变量提升到函数作用域并在 SUB 末尾兜底清理。
    if isinstance(stmt, ast.Goto):
        return True
    if isinstance(stmt, ast.If):
        in_body = any(stmt_has_goto(inner) for inner in stmt.body)
        in_elifs = any(stmt_has_goto(inner) for branch in stmt.elifs for inner in branch.body)
        in_else = any(stmt_has_goto(inner) for inner in stmt.else_body)
        return in_body or in_elifs or in_else
    if isinstance(stmt, ast.TryCatch):
        return any(stmt_has_goto(inner) for branch in stmt.catches for inner in branch.body)
    if isinstance(stmt, ast.ForLoop | ast.WhileLoop):
        return any(stmt_has_goto(inner) for inner in stmt.body)
    return False


def stmt_gosub_lines(stmt: ast.Stmt) -> list[int]:
    if isinstance(stmt, ast.Gosub):
        return [stmt.line_no]
    if isinstance(stmt, ast.If):
        lines: list[int] = []
        for inner in stmt.body:
            lines.extend(stmt_gosub_lines(inner))
        for branch in stmt.elifs:
            for inner in branch.body:
                lines.extend(stmt_gosub_lines(inner))
        for inner in stmt.else_body:
            lines.extend(stmt_gosub_lines(inner))
        return lines
    if isinstance(stmt, ast.TryCatch):
        lines: list[int] = []
        for branch in stmt.catches:
            for inner in branch.body:
                lines.extend(stmt_gosub_lines(inner))
        return lines
    if isinstance(stmt, ast.ForLoop | ast.WhileLoop):
        lines: list[int] = []
        for inner in stmt.body:
            lines.extend(stmt_gosub_lines(inner))
        return lines
    return []
