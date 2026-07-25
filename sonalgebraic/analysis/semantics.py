from __future__ import annotations

from dataclasses import dataclass, field

from ..core import ast
from ..core.errors import SonCompileError
from ..core.module_model import ModuleExports
from ..core.names import split_module_member
from ..core.lines import LINT_OPTIONS
from .typesys import BUILTIN_MODULES, is_bool, is_cptr, is_error, is_handle, is_null, is_numeric, is_ptr, is_string, is_symbol, resolve_binary_function, resolve_builtin_const, resolve_desktop_function, resolve_file_function, resolve_list_function, resolve_net_function, resolve_string_function, same_handle_kind, type_of


@dataclass(frozen=True)
class Symbol:
    name: str
    type_spec: ast.TypeSpec
    mutable: bool
    by_ref: bool = False


@dataclass(frozen=True)
class CheckedProgram:
    program: ast.Program
    symbols: dict[str, Symbol]
    subs: dict[str, ast.Subroutine]
    entities: dict[str, ast.EntityDef]
    uses: dict[str, str]
    external_modules: dict[str, ModuleExports]
    c_headers: dict[str, ast.UseCHeader]
    c_libs: dict[str, ast.UseLibrary]
    c_funcs: dict[str, ast.CFunctionDecl]
    # 枚举成员值：键为 "enumname.member"（小写），值为自增整数
    enum_members: dict[str, int] = field(default_factory=dict)


def check_program(
    program: ast.Program,
    external_modules: dict[str, ModuleExports] | None = None,
    require_main: bool = True,
) -> CheckedProgram:
    external_modules = external_modules or {}
    uses = validate_uses(program, external_modules)
    c_headers = validate_c_headers(program)
    c_libs = validate_c_libs(program)
    c_funcs = collect_c_funcs(program, c_headers)
    entities = collect_entities(program)
    enum_members = collect_enums(program)
    symbols = collect_symbols(program, entities, uses, external_modules, c_funcs)
    inject_enum_symbols(symbols, enum_members)
    subs = collect_subs(program, uses, external_modules)

    if require_main and "main" not in {name.lower() for name in subs}:
        raise SonCompileError("程序必须定义 `SUB main AS PUBLIC AS VOID`")

    for sub in program.subs:
        check_sub(sub, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
    for stmt in program.top_level:
        if not isinstance(stmt, ast.Call | ast.End | ast.NoOp):
            raise SonCompileError("顶层只能放声明、SUB、CALL 和 END；可执行逻辑必须放进 SUB", stmt.line_no)
        check_stmt(stmt, symbols, subs, entities, uses, external_modules, set(), c_headers, c_libs, c_funcs)

    return CheckedProgram(program, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs, enum_members)


def collect_program_diagnostics(
    program: ast.Program,
    external_modules: dict[str, ModuleExports] | None = None,
    require_main: bool = True,
    max_errors: int = 50,
) -> list[SonCompileError]:
    diagnostics: list[SonCompileError] = []
    max_errors = max(1, max_errors)
    external_modules = external_modules or {}

    try:
        uses = validate_uses(program, external_modules)
        c_headers = validate_c_headers(program)
        c_libs = validate_c_libs(program)
        c_funcs = collect_c_funcs(program, c_headers)
        entities = collect_entities(program)
        enum_members = collect_enums(program)
        symbols = collect_symbols(program, entities, uses, external_modules, c_funcs)
        inject_enum_symbols(symbols, enum_members)
        subs = collect_subs(program, uses, external_modules)
    except SonCompileError as exc:
        diagnostics.append(exc)
        return diagnostics

    if require_main and "main" not in {name.lower() for name in subs}:
        if _add_diagnostic(diagnostics, SonCompileError("程序必须定义 `SUB main AS PUBLIC AS VOID`"), max_errors):
            return diagnostics

    for sub in program.subs:
        collect_sub_diagnostics(sub, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs, diagnostics, max_errors)
        if len(diagnostics) >= max_errors:
            return diagnostics

    for stmt in program.top_level:
        if not isinstance(stmt, ast.Call | ast.End | ast.NoOp):
            if _add_diagnostic(diagnostics, SonCompileError("顶层只能放声明、SUB、CALL 和 END；可执行逻辑必须放进 SUB", stmt.line_no), max_errors):
                return diagnostics
            continue
        try:
            check_stmt(stmt, symbols, subs, entities, uses, external_modules, set(), c_headers, c_libs, c_funcs)
        except SonCompileError as exc:
            if _add_diagnostic(diagnostics, exc, max_errors):
                return diagnostics

    return diagnostics


def collect_sub_diagnostics(
    sub: ast.Subroutine,
    symbols: dict[str, Symbol],
    subs: dict[str, ast.Subroutine],
    entities: dict[str, ast.EntityDef],
    uses: dict[str, str],
    external_modules: dict[str, ModuleExports],
    c_headers: dict[str, ast.UseCHeader],
    c_libs: dict[str, ast.UseLibrary],
    c_funcs: dict[str, ast.CFunctionDecl],
    diagnostics: list[SonCompileError],
    max_errors: int,
) -> None:
    scope = symbols.copy()
    for param in sub.params:
        scope[param.name.lower()] = Symbol(param.name, param.type_spec, True, param.by_ref)

    labels: set[str] = set()
    try:
        labels = collect_labels(sub.body)
    except SonCompileError as exc:
        if _add_diagnostic(diagnostics, exc, max_errors):
            return

    try:
        reject_end_in_sub(sub.body)
    except SonCompileError as exc:
        if _add_diagnostic(diagnostics, exc, max_errors):
            return

    for stmt in sub.body:
        stmt_ok = True
        try:
            check_stmt(stmt, scope, subs, entities, uses, external_modules, labels, c_headers, c_libs, c_funcs)
        except SonCompileError as exc:
            stmt_ok = False
            if _add_diagnostic(diagnostics, exc, max_errors):
                return
        if stmt_ok:
            try:
                check_return(stmt, sub.return_type, scope, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
            except SonCompileError as exc:
                if _add_diagnostic(diagnostics, exc, max_errors):
                    return

    if sub.return_type.name != "VOID" and not has_required_return_path(sub.body):
        if _add_diagnostic(diagnostics, SonCompileError("非 VOID SUB 必须保证所有明显路径 RETURN 一个值", sub.line_no), max_errors):
            return

    if sub.name.lower() == "main":
        if sub.visibility != "PUBLIC" or sub.return_type.name != "VOID":
            _add_diagnostic(diagnostics, SonCompileError("主入口必须写成 `SUB main AS PUBLIC AS VOID`", sub.line_no), max_errors)


def _add_diagnostic(diagnostics: list[SonCompileError], error: SonCompileError, max_errors: int) -> bool:
    if len(diagnostics) < max_errors:
        diagnostics.append(error)
    return len(diagnostics) >= max_errors


def validate_uses(program: ast.Program, external_modules: dict[str, ModuleExports]) -> dict[str, str]:
    uses: dict[str, str] = {}
    for use in program.uses:
        key = use.alias.lower()
        if key in uses:
            raise SonCompileError(f"重复 USE 别名: {use.alias}", use.line_no)
        if use.module == "SYS.LINT":
            option = use.alias.upper()
            if option not in LINT_OPTIONS:
                known = ", ".join(sorted(LINT_OPTIONS))
                raise SonCompileError(f"未知 SYS.LINT 选项: {use.alias}；当前支持: {known}", use.line_no)
            uses[key] = use.module
            continue
        if use.module not in BUILTIN_MODULES and key not in external_modules:
            raise SonCompileError(f"找不到用户模块或模块未加载: {use.module}", use.line_no)
        uses[key] = use.module
    return uses


def validate_c_headers(program: ast.Program) -> dict[str, ast.UseCHeader]:
    headers: dict[str, ast.UseCHeader] = {}
    for header in program.usec_headers:
        key = header.alias.lower()
        if key in headers:
            raise SonCompileError(f"重复 USEC 别名: {header.alias}", header.line_no)
        headers[key] = header
    return headers


def validate_c_libs(program: ast.Program) -> dict[str, ast.UseLibrary]:
    libs: dict[str, ast.UseLibrary] = {}
    for lib in program.uselibs:
        key = lib.alias.lower()
        if key in libs:
            raise SonCompileError(f"重复 USELIB 别名: {lib.alias}", lib.line_no)
        libs[key] = lib
    return libs


def collect_c_funcs(program: ast.Program, c_headers: dict[str, ast.UseCHeader]) -> dict[str, ast.CFunctionDecl]:
    funcs: dict[str, ast.CFunctionDecl] = {}
    for decl in program.c_decls:
        if decl.alias.lower() not in c_headers:
            raise SonCompileError(f"DECLARE C 引用了未定义的 USEC 别名: {decl.alias}", decl.line_no)
        key = f"{decl.alias.lower()}.{decl.name.lower()}"
        if key in funcs:
            raise SonCompileError(f"重复 DECLARE C: {decl.alias}.{decl.name}", decl.line_no)
        funcs[key] = decl
    return funcs


def collect_enums(program: ast.Program) -> dict[str, int]:
    """收集枚举成员值：键 `"enumname.member"`（小写），值从 0 自增。"""
    members: dict[str, int] = {}
    seen_enums: set[str] = set()
    for enum in program.enums:
        key = enum.name.lower()
        if key in seen_enums:
            raise SonCompileError(f"重复定义 ENUM: {enum.name}", enum.line_no)
        seen_enums.add(key)
        for index, member in enumerate(enum.members):
            members[f"{key}.{member.lower()}"] = index
    return members


def inject_enum_symbols(symbols: dict[str, Symbol], enum_members: dict[str, int]) -> None:
    """把枚举成员作为 LONG 常量注入符号表，键为完整点名（小写）。"""
    for dotted in enum_members:
        symbols[dotted] = Symbol(dotted, ast.TypeSpec("NUM", "LONG"), False)


def collect_entities(program: ast.Program) -> dict[str, ast.EntityDef]:
    entities: dict[str, ast.EntityDef] = {}
    for entity in program.entities:
        key = entity.name.lower()
        if key in entities:
            raise SonCompileError(f"重复定义 ENTITY: {entity.name}", entity.line_no)
        field_names: set[str] = set()
        for field in entity.fields:
            field_key = field.name.lower()
            if field_key in field_names:
                raise SonCompileError(f"ENTITY 字段重复: {field.name}", field.line_no)
            field_names.add(field_key)
            reject_unsupported_type(field.type_spec, field.line_no, allow_entity=True)
        entities[key] = entity
    return entities


def collect_symbols(
    program: ast.Program,
    entities: dict[str, ast.EntityDef],
    uses: dict[str, str],
    external_modules: dict[str, ModuleExports],
    c_funcs: dict[str, ast.CFunctionDecl] | None = None,
) -> dict[str, Symbol]:
    symbols: dict[str, Symbol] = {}
    for decl in program.declarations:
        key = decl.name.lower()
        if key in symbols:
            raise SonCompileError(f"重复声明变量: {decl.name}", decl.line_no)
        reject_unsupported_type(decl.type_spec, decl.line_no, entities=entities, uses=uses, external_modules=external_modules, allow_entity=True)
        symbols[key] = Symbol(decl.name, decl.type_spec, decl.mutable)
        if decl.expr is not None:
            check_expr(decl.expr, symbols, {}, entities, uses, external_modules, {}, {}, c_funcs or {})
            expr_type = type_of(decl.expr, symbols, {}, entities, uses, external_modules, c_funcs)
            reject_unowned_buffer_calls(decl.expr, uses, allow_root=owned_handle_root_ok(decl.type_spec, expr_type))
            require_assignable(decl.type_spec, expr_type, decl.line_no)
    return symbols


def collect_subs(
    program: ast.Program,
    uses: dict[str, str],
    external_modules: dict[str, ModuleExports],
) -> dict[str, ast.Subroutine]:
    subs: dict[str, ast.Subroutine] = {}
    for sub in program.subs:
        key = sub.name.lower()
        if key in subs:
            raise SonCompileError(f"重复定义 SUB: {sub.name}", sub.line_no)
        # ENTITY existence is validated during statement/type resolution where the entity table is available.
        reject_unsupported_type(sub.return_type, sub.line_no, allow_void=True, allow_entity=True, uses=uses, external_modules=external_modules)
        seen_params: set[str] = set()
        for param in sub.params:
            key_param = param.name.lower()
            if key_param in seen_params:
                raise SonCompileError(f"重复参数: {param.name}", param.line_no)
            seen_params.add(key_param)
            reject_unsupported_type(param.type_spec, param.line_no, allow_entity=True, uses=uses, external_modules=external_modules)
        subs[key] = sub
    return subs


def check_sub(
    sub: ast.Subroutine,
    symbols: dict[str, Symbol],
    subs: dict[str, ast.Subroutine],
    entities: dict[str, ast.EntityDef],
    uses: dict[str, str],
    external_modules: dict[str, ModuleExports],
    c_headers: dict[str, ast.UseCHeader],
    c_libs: dict[str, ast.UseLibrary],
    c_funcs: dict[str, ast.CFunctionDecl],
) -> None:
    scope = symbols.copy()
    for param in sub.params:
        scope[param.name.lower()] = Symbol(param.name, param.type_spec, True, param.by_ref)

    labels = collect_labels(sub.body)
    reject_end_in_sub(sub.body)
    for stmt in sub.body:
        check_stmt(stmt, scope, subs, entities, uses, external_modules, labels, c_headers, c_libs, c_funcs)
        check_return(stmt, sub.return_type, scope, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)

    if sub.return_type.name != "VOID" and not has_required_return_path(sub.body):
        raise SonCompileError("非 VOID SUB 必须保证所有明显路径 RETURN 一个值", sub.line_no)

    if sub.name.lower() == "main":
        if sub.visibility != "PUBLIC" or sub.return_type.name != "VOID":
            raise SonCompileError("主入口必须写成 `SUB main AS PUBLIC AS VOID`", sub.line_no)


def collect_labels(body: list[ast.Stmt]) -> set[str]:
    labels: set[str] = set()
    for stmt in body:
        if isinstance(stmt, ast.Label):
            key = stmt.name.lower()
            if key in labels:
                raise SonCompileError(f"重复标签: {stmt.name}", stmt.line_no)
            labels.add(key)
        elif isinstance(stmt, ast.If):
            labels.update(collect_labels(stmt.body))
            for branch in stmt.elifs:
                labels.update(collect_labels(branch.body))
            labels.update(collect_labels(stmt.else_body))
        elif isinstance(stmt, ast.ForLoop | ast.WhileLoop):
            labels.update(collect_labels(stmt.body))
    return labels


def reject_end_in_sub(body: list[ast.Stmt]) -> None:
    for stmt in body:
        if isinstance(stmt, ast.End):
            raise SonCompileError("END 只能放在顶层；SUB 内请使用 RETURN", stmt.line_no)
        if isinstance(stmt, ast.If):
            reject_end_in_sub(stmt.body)
            for branch in stmt.elifs:
                reject_end_in_sub(branch.body)
            reject_end_in_sub(stmt.else_body)
        if isinstance(stmt, ast.ForLoop | ast.WhileLoop):
            reject_end_in_sub(stmt.body)


def has_required_return_path(body: list[ast.Stmt]) -> bool:
    for stmt in reversed(body):
        if isinstance(stmt, ast.NoOp | ast.Label):
            continue
        if isinstance(stmt, ast.Return):
            return True
        # 带 ELSE 的 IF：当 then、所有 ELSE IF、ELSE 分支都保证返回时，整条 IF 必定返回
        if isinstance(stmt, ast.If) and stmt.else_body:
            if (
                has_required_return_path(stmt.body)
                and all(has_required_return_path(branch.body) for branch in stmt.elifs)
                and has_required_return_path(stmt.else_body)
            ):
                return True
            return False
        return False
    return False


def check_return(
    stmt: ast.Stmt,
    return_type: ast.TypeSpec,
    symbols: dict[str, Symbol],
    subs: dict[str, ast.Subroutine],
    entities: dict[str, ast.EntityDef],
    uses: dict[str, str],
    external_modules: dict[str, ModuleExports],
    c_headers: dict[str, ast.UseCHeader],
    c_libs: dict[str, ast.UseLibrary],
    c_funcs: dict[str, ast.CFunctionDecl],
) -> None:
    if isinstance(stmt, ast.If):
        for inner in stmt.body:
            check_return(inner, return_type, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        for branch in stmt.elifs:
            for inner in branch.body:
                check_return(inner, return_type, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        for inner in stmt.else_body:
            check_return(inner, return_type, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        return
    if isinstance(stmt, ast.ForLoop | ast.WhileLoop):
        for inner in stmt.body:
            check_return(inner, return_type, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        return
    if not isinstance(stmt, ast.Return):
        return
    if return_type.name == "VOID" and stmt.expr is not None:
        raise SonCompileError("VOID SUB 不能 RETURN 值", stmt.line_no)
    if return_type.name != "VOID" and stmt.expr is None:
        raise SonCompileError("非 VOID SUB 必须 RETURN 一个值", stmt.line_no)
    if stmt.expr is not None:
        expr_type = type_of(stmt.expr, symbols, subs, entities, uses, external_modules, c_funcs)
        reject_unowned_buffer_calls(stmt.expr, uses, allow_root=owned_handle_root_ok(return_type, expr_type))
        require_assignable(return_type, expr_type, stmt.line_no)


def check_stmt(
    stmt: ast.Stmt,
    symbols: dict[str, Symbol],
    subs: dict[str, ast.Subroutine],
    entities: dict[str, ast.EntityDef],
    uses: dict[str, str],
    external_modules: dict[str, ModuleExports],
    labels: set[str],
    c_headers: dict[str, ast.UseCHeader],
    c_libs: dict[str, ast.UseLibrary],
    c_funcs: dict[str, ast.CFunctionDecl],
) -> None:
    if isinstance(stmt, ast.LocalDeclaration):
        key = stmt.name.lower()
        if key in symbols:
            raise SonCompileError(f"重复声明变量: {stmt.name}", stmt.line_no)
        reject_unsupported_type(stmt.type_spec, stmt.line_no, entities=entities, uses=uses, external_modules=external_modules, allow_entity=True)
        symbols[key] = Symbol(stmt.name, stmt.type_spec, stmt.mutable)
        if stmt.expr is not None:
            check_expr(stmt.expr, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
            expr_type = type_of(stmt.expr, symbols, subs, entities, uses, external_modules, c_funcs)
            reject_unowned_buffer_calls(stmt.expr, uses, allow_root=owned_handle_root_ok(stmt.type_spec, expr_type))
            require_assignable(stmt.type_spec, expr_type, stmt.line_no)
    elif isinstance(stmt, ast.Assign):
        if isinstance(stmt.target, ast.VarRef):
            symbol = resolve_symbol_path(stmt.target.name, symbols, entities, stmt.line_no)
            if not symbol.mutable:
                raise SonCompileError(f"不能给 CONST 赋值: {stmt.target.name}", stmt.line_no)
            target_type = symbol.type_spec
        elif isinstance(stmt.target, ast.Deref):
            check_expr(stmt.target.expr, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
            ptr_type = type_of(stmt.target.expr, symbols, subs, entities, uses, external_modules, c_funcs)
            if not is_ptr(ptr_type):
                raise SonCompileError("^ 只能用于指针类型", stmt.line_no)
            target_type = ptr_type.inner if ptr_type.inner is not None else ast.TypeSpec("NUM", "LONG")
        elif isinstance(stmt.target, ast.Index):
            check_expr(stmt.target, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
            target_type = type_of(stmt.target, symbols, subs, entities, uses, external_modules, c_funcs)
        else:
            raise SonCompileError("赋值目标必须是变量、数组元素或 ^指针", stmt.line_no)
        check_expr(stmt.expr, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        expr_type = type_of(stmt.expr, symbols, subs, entities, uses, external_modules, c_funcs)
        reject_unowned_buffer_calls(stmt.expr, uses, allow_root=owned_handle_root_ok(target_type, expr_type))
        require_assignable(target_type, expr_type, stmt.line_no)
    elif isinstance(stmt, ast.Print):
        if stmt.expr is not None:
            check_expr(stmt.expr, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
            reject_unowned_buffer_calls(stmt.expr, uses)
    elif isinstance(stmt, ast.Input):
        symbol = require_symbol(stmt.target, symbols, stmt.line_no)
        if not symbol.mutable:
            raise SonCompileError(f"IO.INPUT 目标必须是 VAR: {stmt.target}", stmt.line_no)
        if not is_io_input_allowed(stmt, uses):
            raise SonCompileError("IO.INPUT 必须通过 `USE SYS.IO AS <别名>` 注册的别名调用", stmt.line_no)
        check_expr(stmt.prompt, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        if not is_string(type_of(stmt.prompt, symbols, subs, entities, uses, external_modules, c_funcs)):
            raise SonCompileError("IO.INPUT 的提示文本必须是 STRING", stmt.line_no)
    elif isinstance(stmt, ast.Call):
        sub = subs.get(stmt.name.lower()) or resolve_external_sub(stmt.name, uses, external_modules)
        c_func = resolve_c_func(stmt.name, c_funcs)
        if sub is None and c_func is None:
            raise SonCompileError(f"未知 SUB 或 C 函数: {stmt.name}", stmt.line_no)
        target = sub if sub is not None else c_func
        if sub is not None and target.return_type.name != "VOID":
            raise SonCompileError("带返回值的 SUB 必须通过 `x = CALL name(...)` 使用", stmt.line_no)
        check_call_args(stmt.name, stmt.args, target, symbols, subs, entities, uses, external_modules, stmt.line_no, c_headers, c_libs, c_funcs)
    elif isinstance(stmt, ast.TryCatch):
        trap = require_symbol(stmt.traceback_var, symbols, stmt.line_no)
        if not is_error(trap.type_spec):
            raise SonCompileError("TRACEBACK 目标必须是 ERROR 变量", stmt.line_no)
        sub = subs.get(stmt.call_name.lower()) or resolve_external_sub(stmt.call_name, uses, external_modules)
        if sub is None:
            raise SonCompileError(f"未知 SUB: {stmt.call_name}", stmt.line_no)
        check_call_args(stmt.call_name, stmt.args, sub, symbols, subs, entities, uses, external_modules, stmt.line_no, c_headers, c_libs, c_funcs)
        for branch in stmt.catches:
            branch_symbols = symbols.copy()
            branch_symbols[branch.alias.lower()] = Symbol(branch.alias, ast.TypeSpec("ERROR"), False)
            for inner in branch.body:
                check_stmt(inner, branch_symbols, subs, entities, uses, external_modules, labels, c_headers, c_libs, c_funcs)
    elif isinstance(stmt, ast.ThrowNew):
        check_expr(stmt.message, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        if not is_string(type_of(stmt.message, symbols, subs, entities, uses, external_modules, c_funcs)):
            raise SonCompileError("THROW NEW 的错误消息必须是 STRING", stmt.line_no)
    elif isinstance(stmt, ast.ThrowVar):
        symbol = require_symbol(stmt.name, symbols, stmt.line_no)
        if not is_error(symbol.type_spec):
            raise SonCompileError("THROW 只能抛出 ERROR 变量", stmt.line_no)
    elif isinstance(stmt, ast.If):
        check_expr(stmt.condition, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        for inner in stmt.body:
            check_stmt(inner, symbols, subs, entities, uses, external_modules, labels, c_headers, c_libs, c_funcs)
        for branch in stmt.elifs:
            check_expr(branch.condition, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
            for inner in branch.body:
                check_stmt(inner, symbols, subs, entities, uses, external_modules, labels, c_headers, c_libs, c_funcs)
        for inner in stmt.else_body:
            check_stmt(inner, symbols, subs, entities, uses, external_modules, labels, c_headers, c_libs, c_funcs)
    elif isinstance(stmt, ast.ForLoop):
        loop_var = require_symbol(stmt.var, symbols, stmt.line_no)
        if not loop_var.mutable:
            raise SonCompileError(f"FOR 循环变量必须是 VAR: {stmt.var}", stmt.line_no)
        if not is_numeric(loop_var.type_spec):
            raise SonCompileError(f"FOR 循环变量必须是数值类型: {stmt.var}", stmt.line_no)
        for bound, label in ((stmt.start, "起始"), (stmt.end, "结束")):
            check_expr(bound, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
            if not is_numeric(type_of(bound, symbols, subs, entities, uses, external_modules, c_funcs)):
                raise SonCompileError(f"FOR 的{label}值必须是数值", stmt.line_no)
        if stmt.step is not None:
            check_expr(stmt.step, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
            if not is_numeric(type_of(stmt.step, symbols, subs, entities, uses, external_modules, c_funcs)):
                raise SonCompileError("FOR 的步长必须是数值", stmt.line_no)
        for inner in stmt.body:
            check_stmt(inner, symbols, subs, entities, uses, external_modules, labels, c_headers, c_libs, c_funcs)
    elif isinstance(stmt, ast.WhileLoop):
        check_expr(stmt.condition, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        for inner in stmt.body:
            check_stmt(inner, symbols, subs, entities, uses, external_modules, labels, c_headers, c_libs, c_funcs)
    elif isinstance(stmt, ast.Goto):
        if labels and stmt.label.lower() not in labels:
            raise SonCompileError(f"未知标签: {stmt.label}", stmt.line_no)
    elif isinstance(stmt, ast.Gosub):
        if labels and stmt.label.lower() not in labels:
            raise SonCompileError(f"未知标签: {stmt.label}", stmt.line_no)
    elif isinstance(stmt, ast.Return) and stmt.expr is not None:
        check_expr(stmt.expr, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)


def check_expr(
    expr: ast.Expr,
    symbols: dict[str, Symbol],
    subs: dict[str, ast.Subroutine],
    entities: dict[str, ast.EntityDef],
    uses: dict[str, str],
    external_modules: dict[str, ModuleExports],
    c_headers: dict[str, ast.UseCHeader],
    c_libs: dict[str, ast.UseLibrary],
    c_funcs: dict[str, ast.CFunctionDecl],
) -> None:
    if isinstance(expr, ast.VarRef):
        if resolve_builtin_const(expr.name, uses) is not None:
            return
        if resolve_external_const(expr.name, uses, external_modules) is not None:
            return
        resolve_symbol_path(expr.name, symbols, entities, expr.line_no)
    elif isinstance(expr, ast.Unary):
        check_expr(expr.expr, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        if expr.op == "BNOT":
            operand = type_of(expr.expr, symbols, subs, entities, uses, external_modules, c_funcs)
            if not is_numeric(operand) and not is_bool(operand):
                raise SonCompileError("BNOT 只能用于整数", expr.line_no)
    elif isinstance(expr, ast.Deref):
        check_expr(expr.expr, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        ptr_type = type_of(expr.expr, symbols, subs, entities, uses, external_modules, c_funcs)
        if not is_ptr(ptr_type):
            raise SonCompileError("^ 只能用于指针类型", expr.line_no)
    elif isinstance(expr, ast.AddressOf):
        check_expr(expr.expr, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        if not isinstance(expr.expr, ast.VarRef):
            raise SonCompileError("@ 只能用于变量", expr.line_no)
    elif isinstance(expr, ast.Cast):
        check_expr(expr.expr, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        reject_unsupported_type(expr.type_spec, expr.line_no, allow_entity=True, entities=entities, uses=uses, external_modules=external_modules)
    elif isinstance(expr, ast.Index):
        check_expr(expr.base, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        base_type = type_of(expr.base, symbols, subs, entities, uses, external_modules, c_funcs)
        if base_type.array_size is None:
            raise SonCompileError("下标访问只能用于数组", expr.line_no)
        check_expr(expr.index, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        index_type = type_of(expr.index, symbols, subs, entities, uses, external_modules, c_funcs)
        if not is_numeric(index_type) and not is_bool(index_type):
            raise SonCompileError("数组下标必须是整数", expr.line_no)
    elif isinstance(expr, ast.Binary):
        check_expr(expr.left, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        check_expr(expr.right, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        validate_binary(expr, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
    elif isinstance(expr, ast.FString):
        for part in expr.parts:
            if isinstance(part, ast.Expr):
                check_expr(part, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
    elif isinstance(expr, ast.CallExpr):
        sub = subs.get(expr.name.lower()) or resolve_external_sub(expr.name, uses, external_modules)
        c_func = resolve_c_func(expr.name, c_funcs)
        string_fn = resolve_string_function(expr.name, uses)
        net_fn = resolve_net_function(expr.name, uses)
        file_fn = resolve_file_function(expr.name, uses)
        desktop_fn = resolve_desktop_function(expr.name, uses)
        binary_fn = resolve_binary_function(expr.name, uses)
        list_fn = resolve_list_function(expr.name, uses)
        if is_math_function(expr.name, "POW", uses):
            if len(expr.args) != 2:
                raise SonCompileError("POW() 需要 2 个参数", expr.line_no)
            for arg in expr.args:
                check_expr(arg, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
                arg_type = type_of(arg, symbols, subs, entities, uses, external_modules, c_funcs)
                if not is_numeric(arg_type) and not is_symbol(arg_type):
                    raise SonCompileError("POW() 参数必须是数值或 SYMBOL", arg.line_no)
            return
        elif expr.name.upper() in {"DERIV", "SIMPLIFY", "SUBST", "EVAL"}:
            check_symbol_algebra_call(expr, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
            return
        elif string_fn is not None:
            params, _ret = string_fn
            if len(expr.args) != len(params):
                raise SonCompileError(f"{expr.name} 需要 {len(params)} 个参数，实际给了 {len(expr.args)} 个", expr.line_no)
            for arg, param_type in zip(expr.args, params):
                check_expr(arg, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
                arg_type = type_of(arg, symbols, subs, entities, uses, external_modules, c_funcs)
                reject_unowned_buffer_calls(arg, uses)
                require_assignable(param_type, arg_type, arg.line_no)
            return
        elif net_fn is not None or file_fn is not None or desktop_fn is not None or binary_fn is not None or list_fn is not None:
            params, _ret = net_fn or file_fn or desktop_fn or binary_fn or list_fn
            if len(expr.args) != len(params):
                raise SonCompileError(f"{expr.name} 需要 {len(params)} 个参数，实际给了 {len(expr.args)} 个", expr.line_no)
            for arg, param_type in zip(expr.args, params):
                check_expr(arg, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
                arg_type = type_of(arg, symbols, subs, entities, uses, external_modules, c_funcs)
                reject_unowned_buffer_calls(arg, uses)
                require_assignable(param_type, arg_type, arg.line_no)
            return
        elif expr.name.upper() not in {"NUMBER", "STRING"} and sub is None and c_func is None:
            raise SonCompileError(f"未知内置函数或 SUB: {expr.name}", expr.line_no)
        target = sub if sub is not None else c_func
        if target is not None:
            if target.return_type.name == "VOID":
                raise SonCompileError("VOID SUB 或 C 函数不能作为表达式使用", expr.line_no)
            check_call_args(expr.name, expr.args, target, symbols, subs, entities, uses, external_modules, expr.line_no, c_headers, c_libs, c_funcs)
        for arg in expr.args:
            check_expr(arg, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)


def require_symbol(name: str, symbols: dict[str, Symbol], line_no: int) -> Symbol:
    key = name.lower()
    if key not in symbols:
        raise SonCompileError(f"变量未声明: {name}", line_no)
    return symbols[key]


def resolve_symbol_path(
    name: str,
    symbols: dict[str, Symbol],
    entities: dict[str, ast.EntityDef],
    line_no: int,
) -> Symbol:
    # 完整点名优先命中（枚举成员如 Color.RED 注入时就是完整点名）
    if name.lower() in symbols:
        return symbols[name.lower()]
    parts = name.split(".")
    symbol = require_symbol(parts[0], symbols, line_no)
    current_type = symbol.type_spec

    for field_name in parts[1:]:
        if current_type.name != "ENTITY":
            raise SonCompileError(f"{'.'.join(parts)} 的 `{field_name}` 不是 ENTITY 字段", line_no)
        entity = entities.get((current_type.subtype or "").lower())
        if entity is None:
            raise SonCompileError(f"未知 ENTITY: {current_type.subtype}", line_no)
        field = next((item for item in entity.fields if item.name.lower() == field_name.lower()), None)
        if field is None:
            raise SonCompileError(f"ENTITY {entity.name} 没有字段: {field_name}", line_no)
        current_type = field.type_spec

    return Symbol(name, current_type, symbol.mutable, symbol.by_ref)


def require_assignable(target: ast.TypeSpec, source: ast.TypeSpec, line_no: int) -> None:
    if target.array_size is not None or source.array_size is not None:
        raise SonCompileError("数组不能整体赋值或作为标量传递，请通过下标访问元素", line_no)
    if is_string(target) and is_string(source):
        return
    if is_numeric(target) and is_numeric(source):
        return
    # BOOL 与数值可互相转换（比较结果赋给 LONG、整数当条件都常见）
    if is_bool(target) and (is_bool(source) or is_numeric(source)):
        return
    if is_numeric(target) and is_bool(source):
        return
    if target.name == "ENTITY" and source.name == "ENTITY" and (target.subtype or "").lower() == (source.subtype or "").lower():
        return
    if same_handle_kind(target, source):
        return
    if is_symbol(target):
        return
    # NULL 可赋给任意指针/CPTR/HANDLE
    if (is_cptr(target) or is_ptr(target) or is_handle(target)) and is_null(source):
        return
    if is_cptr(target) and (is_cptr(source) or is_numeric(source)):
        return
    if is_ptr(target) and (is_ptr(source) or is_numeric(source)):
        return
    raise SonCompileError("赋值两侧类型不兼容，请用 NUMBER() 或 STRING() 显式转换", line_no)


def check_symbol_algebra_call(
    expr: ast.CallExpr,
    symbols: dict[str, Symbol],
    subs: dict[str, ast.Subroutine],
    entities: dict[str, ast.EntityDef],
    uses: dict[str, str],
    external_modules: dict[str, ModuleExports],
    c_headers: dict[str, ast.UseCHeader],
    c_libs: dict[str, ast.UseLibrary],
    c_funcs: dict[str, ast.CFunctionDecl],
) -> None:
    name = expr.name.upper()
    expected = {"DERIV": 2, "SIMPLIFY": 1, "SUBST": 3, "EVAL": 1}[name]
    if len(expr.args) != expected:
        raise SonCompileError(f"{name} 需要 {expected} 个参数", expr.line_no)
    check_expr(expr.args[0], symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
    if not is_symbol(type_of(expr.args[0], symbols, subs, entities, uses, external_modules, c_funcs)):
        raise SonCompileError(f"{name} 的第一个参数必须是 SYMBOL", expr.line_no)
    if name in {"DERIV", "SUBST"}:
        if not isinstance(expr.args[1], ast.StringLiteral):
            raise SonCompileError(f"{name} 的变量名必须是字符串字面量，例如 \"x\"", expr.line_no)
    if name == "SUBST":
        check_expr(expr.args[2], symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        if not is_numeric(type_of(expr.args[2], symbols, subs, entities, uses, external_modules, c_funcs)):
            raise SonCompileError("SUBST 的代入值必须是数值", expr.line_no)


def check_call_args(
    name: str,
    args: list[ast.Expr],
    sub: ast.Subroutine | ast.CFunctionDecl,
    symbols: dict[str, Symbol],
    subs: dict[str, ast.Subroutine],
    entities: dict[str, ast.EntityDef],
    uses: dict[str, str],
    external_modules: dict[str, ModuleExports],
    line_no: int,
    c_headers: dict[str, ast.UseCHeader],
    c_libs: dict[str, ast.UseLibrary],
    c_funcs: dict[str, ast.CFunctionDecl],
) -> None:
    if len(args) != len(sub.params):
        raise SonCompileError(f"SUB 或 C 函数 {name} 需要 {len(sub.params)} 个参数，实际给了 {len(args)} 个", line_no)
    for arg, param in zip(args, sub.params):
        check_expr(arg, symbols, subs, entities, uses, external_modules, c_headers, c_libs, c_funcs)
        arg_type = type_of(arg, symbols, subs, entities, uses, external_modules, c_funcs)
        reject_unowned_buffer_calls(arg, uses)
        if param.by_ref and not isinstance(arg, ast.VarRef):
            raise SonCompileError(f"REF 参数 {param.name} 必须传入变量", arg.line_no)
        if param.by_ref and isinstance(arg, ast.VarRef) and not resolve_symbol_path(arg.name, symbols, entities, arg.line_no).mutable:
            raise SonCompileError(f"REF 参数 {param.name} 不能传入 CONST", arg.line_no)
        require_assignable(param.type_spec, arg_type, arg.line_no)


def is_buffer_handle(type_spec: ast.TypeSpec) -> bool:
    return is_handle(type_spec) and (type_spec.subtype or "").upper() == "BUFFER"


def is_list_handle(type_spec: ast.TypeSpec) -> bool:
    return is_handle(type_spec) and (type_spec.subtype or "").upper() in {"LIST", "STR_LIST"}


def owned_handle_root_ok(target: ast.TypeSpec, source: ast.TypeSpec) -> bool:
    """BUFFER/LIST 这类必须显式 CLOSE 的句柄，其返回值只允许直接赋给同 kind 变量。"""
    if is_buffer_handle(target) and is_buffer_handle(source):
        return True
    return is_list_handle(target) and same_handle_kind(target, source)


def reject_unowned_buffer_calls(expr: ast.Expr, uses: dict[str, str], allow_root: bool = False) -> None:
    if isinstance(expr, ast.CallExpr):
        if not allow_root and buffer_producing_call(expr, uses):
            raise SonCompileError("BUFFER 返回值必须先赋给 HANDLE AS BUFFER 变量并在使用后显式 CLOSE", expr.line_no)
        if not allow_root and list_producing_call(expr, uses):
            raise SonCompileError("LIST 返回值必须先赋给对应 HANDLE AS LIST / STR_LIST 变量并在使用后显式 CLOSE", expr.line_no)
        for arg in expr.args:
            reject_unowned_buffer_calls(arg, uses)
        return
    if isinstance(expr, ast.FString):
        for part in expr.parts:
            if isinstance(part, ast.Expr):
                reject_unowned_buffer_calls(part, uses)
        return
    if isinstance(expr, ast.Binary):
        reject_unowned_buffer_calls(expr.left, uses)
        reject_unowned_buffer_calls(expr.right, uses)
        return
    if isinstance(expr, ast.Unary | ast.Deref | ast.AddressOf | ast.Cast):
        reject_unowned_buffer_calls(expr.expr, uses)
        return
    if isinstance(expr, ast.Index):
        reject_unowned_buffer_calls(expr.base, uses)
        reject_unowned_buffer_calls(expr.index, uses)


def buffer_producing_call(expr: ast.CallExpr, uses: dict[str, str]) -> bool:
    binary_fn = resolve_binary_function(expr.name, uses)
    net_fn = resolve_net_function(expr.name, uses)
    return bool((binary_fn and is_buffer_handle(binary_fn[1])) or (net_fn and is_buffer_handle(net_fn[1])))


def list_producing_call(expr: ast.CallExpr, uses: dict[str, str]) -> bool:
    list_fn = resolve_list_function(expr.name, uses)
    return bool(list_fn and is_list_handle(list_fn[1]))


def reject_unsupported_type(
    type_spec: ast.TypeSpec,
    line_no: int,
    allow_void: bool = False,
    allow_entity: bool = False,
    entities: dict[str, ast.EntityDef] | None = None,
    uses: dict[str, str] | None = None,
    external_modules: dict[str, ModuleExports] | None = None,
) -> None:
    # 数组：校验元素类型即可（元素类型 = 去掉 array_size）
    if type_spec.array_size is not None:
        element = ast.TypeSpec(type_spec.name, type_spec.subtype, type_spec.inner)
        # 支持值类型和 STRING 元素；SYMBOL/ERROR/ENTITY 数组需逐元素深拷贝，留待后续
        if element.name in {"SYMBOL", "ERROR", "ENTITY"}:
            raise SonCompileError(f"数组暂不支持 {element.name} 元素类型，当前支持 NUM/BOOL/HANDLE/CPTR/PTR/STRING", line_no)
        reject_unsupported_type(element, line_no, allow_void=False, allow_entity=allow_entity, entities=entities, uses=uses, external_modules=external_modules)
        return
    if allow_void and type_spec.name == "VOID":
        return
    if type_spec.name in {"NUM", "STRING", "ERROR", "SYMBOL", "CPTR", "BOOL"}:
        return
    if type_spec.name == "HANDLE":
        if not type_spec.subtype:
            raise SonCompileError("HANDLE 必须指定 kind，例如 HANDLE AS FILE", line_no)
        return
    if type_spec.name == "PTR":
        if type_spec.inner is not None:
            reject_unsupported_type(type_spec.inner, line_no, allow_entity=allow_entity, entities=entities, uses=uses, external_modules=external_modules)
        return
    if allow_entity and type_spec.name == "ENTITY":
        if entities is not None and (type_spec.subtype or "").lower() not in entities and not external_entity_exists(type_spec.subtype or "", uses or {}, external_modules or {}):
            raise SonCompileError(f"未知 ENTITY: {type_spec.subtype}", line_no)
        return
    raise SonCompileError(f"类型 {type_spec.name} 当前阶段还没接入 C 后端", line_no)


def validate_binary(
    expr: ast.Binary,
    symbols: dict[str, Symbol],
    subs: dict[str, ast.Subroutine],
    entities: dict[str, ast.EntityDef],
    uses: dict[str, str],
    external_modules: dict[str, ModuleExports],
    c_headers: dict[str, ast.UseCHeader],
    c_libs: dict[str, ast.UseLibrary],
    c_funcs: dict[str, ast.CFunctionDecl],
) -> None:
    left = type_of(expr.left, symbols, subs, entities, uses, external_modules, c_funcs)
    right = type_of(expr.right, symbols, subs, entities, uses, external_modules, c_funcs)
    if expr.op in {"BAND", "BOR", "BXOR", "SHL", "SHR"}:
        if (is_numeric(left) or is_bool(left)) and (is_numeric(right) or is_bool(right)):
            return
        raise SonCompileError("位运算只能用于整数", expr.line_no)
    if expr.op in {"+", "-", "*", "/", "**"}:
        if (is_numeric(left) and is_numeric(right)) or is_symbol(left) or is_symbol(right):
            return
        if expr.op in {"+", "-"} and ((is_ptr(left) and is_numeric(right)) or (is_numeric(left) and is_ptr(right))):
            return
        raise SonCompileError("当前版本只支持数值/指针之间做算术运算", expr.line_no)
    if expr.op in {"%", "<", "<=", ">", ">=", "AND", "OR"}:
        if is_numeric(left) and is_numeric(right):
            return
        # 逻辑运算允许 BOOL 操作数（含 BOOL 与数值混用）
        if expr.op in {"AND", "OR"} and (is_bool(left) or is_numeric(left)) and (is_bool(right) or is_numeric(right)):
            return
        # 比较运算允许 BOOL 参与
        if expr.op in {"<", "<=", ">", ">="} and (is_bool(left) or is_numeric(left)) and (is_bool(right) or is_numeric(right)):
            return
        raise SonCompileError("当前版本只支持数值/指针之间做算术、大小比较和逻辑运算", expr.line_no)
    if expr.op in {"=", "==", "!=", "<>"}:
        if (is_numeric(left) and is_numeric(right)) or (is_string(left) and is_string(right)) or (is_cptr(left) and is_cptr(right)) or (is_ptr(left) and is_ptr(right)) or same_handle_kind(left, right):
            return
        # BOOL 之间、BOOL 与数值之间可比较
        if (is_bool(left) or is_numeric(left)) and (is_bool(right) or is_numeric(right)):
            return
        # 指针/CPTR/HANDLE 与 NULL 比较
        if (is_ptr(left) or is_cptr(left) or is_handle(left)) and is_null(right):
            return
        if is_null(left) and (is_ptr(right) or is_cptr(right) or is_handle(right)):
            return
        raise SonCompileError("等值比较两侧类型不兼容", expr.line_no)


def is_math_const(name: str, uses: dict[str, str]) -> bool:
    split = split_module_member(name)
    return bool(split and uses.get(split[0]) == "SYS.MATH" and split[1].upper() == "PI")


def is_math_function(name: str, function_name: str, uses: dict[str, str]) -> bool:
    split = split_module_member(name)
    return bool(split and uses.get(split[0]) == "SYS.MATH" and split[1].upper() == function_name.upper())


def is_io_input_allowed(stmt: ast.Input, uses: dict[str, str]) -> bool:
    return uses.get(stmt.alias.lower()) == "SYS.IO"


def resolve_external_sub(
    name: str,
    uses: dict[str, str],
    external_modules: dict[str, ModuleExports],
) -> ast.Subroutine | None:
    split = split_module_member(name)
    if split is None:
        return None
    alias, member = split
    module = external_modules.get(alias)
    if module is None:
        return None
    return module.subs.get(member.lower())


def resolve_external_const(
    name: str,
    uses: dict[str, str],
    external_modules: dict[str, ModuleExports],
) -> ast.Declaration | None:
    split = split_module_member(name)
    if split is None:
        return None
    alias, member = split
    module = external_modules.get(alias)
    if module is None:
        return None
    const = module.consts.get(member.lower())
    return const if const is not None else None


def resolve_c_func(name: str, c_funcs: dict[str, ast.CFunctionDecl]) -> ast.CFunctionDecl | None:
    split = split_module_member(name)
    if split is None:
        return None
    alias, member = split
    return c_funcs.get(f"{alias.lower()}.{member.lower()}")


def external_entity_exists(
    name: str,
    uses: dict[str, str],
    external_modules: dict[str, ModuleExports],
) -> bool:
    split = split_module_member(name)
    if split is None:
        return False
    alias, member = split
    module = external_modules.get(alias)
    return bool(module and member.lower() in module.entities)
