from __future__ import annotations

from dataclasses import dataclass, field

from ..core import ast
from .c_runtime import RUNTIME
from ..core.errors import SonCompileError
from ..core.names import c_ident as make_c_ident, entity_c_name, module_header_name, module_symbol_prefix, split_module_member
from ..analysis.semantics import CheckedProgram, Symbol
from ..analysis.typesys import c_type, is_cptr, is_error, is_handle, is_numeric, is_ptr, is_string, is_symbol, resolve_builtin_const, runtime_features_for_program, type_of


@dataclass
class CGen:
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

    def generate(self) -> str:
        chunks = self.generate_preamble()
        chunks.extend(["", self.generate_entities(), "", self.generate_globals(), "", self.generate_prototypes(), ""])
        for sub in self.checked.program.subs:
            chunks.append(self.generate_sub(sub))
            chunks.append("")
        if self.module_name and not self.include_main:
            chunks.append(self.generate_module_init())
            chunks.append("")
            chunks.append(self.generate_module_free())
        if self.include_main:
            chunks.append(self.generate_c_main())
        return "\n".join(chunks).rstrip() + "\n"

    def generate_preamble(self) -> list[str]:
        if self.include_runtime:
            prefix = self.runtime_feature_prefix()
            lines = [(prefix + RUNTIME.strip()).rstrip()]
        else:
            lines = []
            lines.extend(self.runtime_feature_defines())
            lines.append('#include "sa_runtime.h"')
            if self.dynamic and self.module_name:
                build_macro = f"SA_BUILD_{module_symbol_prefix(self.module_name).upper().replace('-', '_')}"
                lines.append(f"#define {build_macro}")
                lines.append(f'#include "{module_header_name(self.module_name)}"')
        for header in self.usec_includes():
            lines.append(header)
        lines.extend(f'#include "{header}"' for header in self.include_headers)
        return lines

    def usec_includes(self) -> list[str]:
        lines: list[str] = []
        for header in self.c_headers.values():
            if header.is_system:
                lines.append(f"#include <{header.header}>")
            else:
                lines.append(f'#include "{header.header}"')
        return lines

    def generate_entities(self) -> str:
        chunks: list[str] = []
        for entity in self.checked.program.entities:
            chunks.append(self.source_comment(entity.line_no, 0))
            chunks.append("typedef struct {")
            for field in entity.fields:
                chunks.append(self.source_comment(field.line_no, 1))
                # 实体字段同样支持定长数组（如 DIM tensor[3]）；漏掉 array_size 会把数组
                # 字段生成成标量，导致 .field[i] 下标访问编译失败。
                suffix = f"[{field.type_spec.array_size}]" if field.type_spec.array_size is not None else ""
                chunks.append(f"    {self.c_type(field.type_spec)} {field.name}{suffix};")
            chunks.append(f"}} {self.entity_type_name(entity.name)};")
            chunks.append("")
        return "\n".join(chunks).rstrip()

    def generate_globals(self) -> str:
        lines: list[str] = []
        for decl in self.checked.program.declarations:
            c_name = self.global_c_name(decl)
            ctype = self.c_type(decl.type_spec)
            if decl.type_spec.array_size is not None:
                lines.append(self.source_comment(decl.line_no, 0))
                storage = "" if self.is_exported_const(decl) else "static "
                lines.append(f"{storage}{ctype} {c_name}[{decl.type_spec.array_size}] = {{0}};")
                continue
            init = "NULL" if is_string(decl.type_spec) or is_cptr(decl.type_spec) or is_ptr(decl.type_spec) else "0"
            if decl.type_spec.name == "ENTITY":
                init = "{0}"
            if is_error(decl.type_spec):
                init = '{0, "ERR_NONE", NULL, 0, NULL}'
            if is_symbol(decl.type_spec):
                init = "NULL"
            lines.append(self.source_comment(decl.line_no, 0))
            storage = "" if self.is_exported_const(decl) else "static "
            lines.append(f"{storage}{ctype} {c_name} = {init};")
        return "\n".join(lines)

    def generate_prototypes(self) -> str:
        return "\n".join(self.sub_signature(sub) + ";" for sub in self.checked.program.subs)

    def generate_sub(self, sub: ast.Subroutine) -> str:
        self.push_sub_scope(sub)
        self.sub_name_stack.append(sub.name)
        self.sub_return_type_stack.append(sub.return_type)
        gosub_lines = self.sub_gosub_lines(sub)
        self.sub_gosub_stack.append(bool(gosub_lines))
        self.sub_gosub_lines_stack.append(gosub_lines)
        self.sub_has_goto_stack.append(any(stmt_has_goto(stmt) for stmt in sub.body))
        self.hoisted_catch_vars = {}
        self.local_resource_stack.append([])
        body = self.prepare_value_param_resources(sub, 1)
        for stmt in sub.body:
            body.extend(self.stmt(stmt, 1))
        body.extend(self.local_resource_cleanup_lines(self.local_resource_stack[-1], 1))
        self.local_resource_stack.pop()
        if self.hoisted_catch_vars:
            hoist = [f"    SaError {name} = {{0, \"ERR_NONE\", NULL, 0, NULL}};" for name in self.hoisted_catch_vars.values()]
            body = [*hoist, *body]
            # SUB 末尾兜底清理提升的 CATCH 变量：若控制流被 GOSUB RETURN 或 GOTO 跳过了块尾
            # 的 sa_error_clear，最后一次捕获的 message 仍残留在函数作用域变量里。clear 幂等，
            # 与正常路径的块尾清理叠加不会双重 free。
            body.extend(f"    sa_error_clear(&{name});" for name in self.hoisted_catch_vars.values())
        if self.sub_gosub_stack[-1]:
            body = ["    int sa_gosub_stack[64];", "    int sa_gosub_top = 0;", *body]

        self.sub_gosub_lines_stack.pop()
        self.sub_gosub_stack.pop()
        self.sub_has_goto_stack.pop()
        self.sub_return_type_stack.pop()
        self.sub_name_stack.pop()
        self.scope_stack.pop()
        if not body:
            body = ["    return;" if sub.return_type.name == "VOID" else f"    return {self.default_value(sub.return_type)};"]
        return "\n".join([self.source_comment(sub.line_no, 0), self.sub_signature(sub) + " {", *body, "}"])

    def generate_c_main(self) -> str:
        lines = ["int main(void) {", "    sa_setup_console();"]
        lines.extend(f"    {call}();" for call in self.main_init_calls)
        lines.extend(self.init_string_globals())
        lines.extend(self.init_entity_globals())
        lines.extend(self.init_global_values())
        lines.extend(self.emit_top_level())
        lines.append("sa_program_end:")
        lines.extend(self.free_string_globals())
        lines.extend(f"    {call}();" for call in reversed(self.main_free_calls))
        # 释放运行时全局错误对象残留的 message：最后一次未捕获/已捕获错误的 strdup 副本
        lines.append("    sa_error_clear(&sa_current_error);")
        lines.append("    return 0;")
        lines.append("}")
        return "\n".join(lines)

    def generate_module_init(self) -> str:
        lines = [f"void {module_symbol_prefix(self.module_name or '')}_init(void) {{"]
        lines.extend(self.init_string_globals())
        lines.extend(self.init_entity_globals())
        lines.extend(self.init_global_values())
        lines.append("}")
        return "\n".join(lines)

    def generate_module_free(self) -> str:
        lines = [f"void {module_symbol_prefix(self.module_name or '')}_free(void) {{"]
        lines.extend(self.free_string_globals())
        lines.append("}")
        return "\n".join(lines)

    def init_string_globals(self) -> list[str]:
        lines: list[str] = []
        for decl in self.checked.program.declarations:
            if not is_string(decl.type_spec):
                continue
            lines.append(self.source_comment(decl.line_no, 1))
            if decl.type_spec.array_size is not None:
                idx = self.next_temp()
                name = self.global_c_name(decl)
                lines.append(f"    for (long long {idx} = 0; {idx} < {decl.type_spec.array_size}; {idx}++) {{")
                lines.append(f"        {name}[{idx}] = sa_strdup(\"\");")
                lines.append(f"    }}")
            else:
                lines.append(f"    {self.global_c_name(decl)} = sa_strdup(\"\");")
        return lines

    def init_entity_globals(self) -> list[str]:
        lines: list[str] = []
        for decl in self.checked.program.declarations:
            if decl.type_spec.name != "ENTITY" or not self.type_has_managed_resources(decl.type_spec):
                continue
            lines.append(self.source_comment(decl.line_no, 1))
            lines.extend(self.entity_init_lines(self.global_c_name(decl), decl.type_spec, 1))
        return lines

    def init_global_values(self) -> list[str]:
        lines: list[str] = []
        for decl in self.checked.program.declarations:
            if decl.expr is None:
                continue
            prelude, value, cleanup = self.expr_with_prelude(decl.expr)
            lines.append(self.source_comment(decl.line_no, 1))
            lines.extend(f"    {line}" for line in prelude)
            if is_string(decl.type_spec):
                lines.append(f"    sa_set_string(&{self.global_c_name(decl)}, {value});")
            elif decl.type_spec.name == "ENTITY" and self.type_has_managed_resources(decl.type_spec):
                lines.extend(self.entity_copy_lines(self.global_c_name(decl), value, decl.type_spec, 1))
            else:
                if is_handle(decl.type_spec) and isinstance(decl.expr, ast.NullLiteral):
                    value = "0"
                lines.append(f"    {self.global_c_name(decl)} = {value};")
            lines.extend(f"    {line}" for line in cleanup)
        return lines

    def emit_top_level(self) -> list[str]:
        lines: list[str] = []
        ended = False
        for stmt in self.checked.program.top_level:
            if ended:
                break
            lines.extend(self.stmt(stmt, 1))
            if isinstance(stmt, ast.End):
                ended = True
        return lines

    def free_string_globals(self) -> list[str]:
        lines: list[str] = []
        for decl in self.checked.program.declarations:
            if is_string(decl.type_spec):
                if decl.type_spec.array_size is not None:
                    idx = self.next_temp()
                    name = self.global_c_name(decl)
                    lines.append(f"    for (long long {idx} = 0; {idx} < {decl.type_spec.array_size}; {idx}++) {{")
                    lines.append(f"        free({name}[{idx}]);")
                    lines.append(f"    }}")
                else:
                    lines.append(f"    free({self.global_c_name(decl)});")
            elif is_error(decl.type_spec):
                lines.append(f"    sa_error_clear(&{self.global_c_name(decl)});")
            elif is_symbol(decl.type_spec):
                lines.append(f"    sa_symbol_free({self.global_c_name(decl)});")
            elif decl.type_spec.name == "ENTITY" and self.type_has_managed_resources(decl.type_spec):
                lines.extend(self.entity_free_lines(self.global_c_name(decl), decl.type_spec, 1))
        return lines

    def block(self, body: list[ast.Stmt], indent: int) -> list[str]:
        self.local_resource_stack.append([])
        lines: list[str] = []
        for stmt in body:
            lines.extend(self.stmt(stmt, indent))
        lines.extend(self.local_resource_cleanup_lines(self.local_resource_stack[-1], indent))
        self.local_resource_stack.pop()
        return lines

    def stmt(self, stmt: ast.Stmt, indent: int) -> list[str]:
        # 异常穿透清理：若当前帧有存活局部托管资源，且本语句会 CALL 可能抛异常的用户 SUB，
        # 用一个只做清理的 landing pad（setjmp 帧）包住它——被调用方抛出时 longjmp 回这里，
        # 先释放本帧资源再向外层重抛，避免异常穿过本 SUB 时局部泄漏。
        if self._stmt_may_throw_user_call(stmt):
            cleanup = self.active_local_resource_cleanup_lines(indent + 1)
            if cleanup:
                return self._wrap_throw_cleanup(stmt, indent, cleanup)
        return self._emit_stmt(stmt, indent)

    def _stmt_may_throw_user_call(self, stmt: ast.Stmt) -> bool:
        if isinstance(stmt, ast.Call):
            # FFI C 函数不抛 SA 异常；只有用户/外部模块 SUB 才需要 landing pad
            return self.resolve_c_func(stmt.name) is None
        if isinstance(stmt, ast.Assign):
            return self.expr_has_user_call(stmt.expr)
        if isinstance(stmt, ast.Print):
            return stmt.expr is not None and self.expr_has_user_call(stmt.expr)
        return False

    def expr_has_user_call(self, expr: ast.Expr | None) -> bool:
        if expr is None:
            return False
        if isinstance(expr, ast.CallExpr):
            if self.checked.subs.get(expr.name.lower()) is not None or self.resolve_external_sub(expr.name) is not None:
                return True
            return any(self.expr_has_user_call(arg) for arg in expr.args)
        if isinstance(expr, ast.Binary):
            return self.expr_has_user_call(expr.left) or self.expr_has_user_call(expr.right)
        if isinstance(expr, ast.Unary | ast.Deref | ast.AddressOf | ast.Cast):
            return self.expr_has_user_call(expr.expr)
        if isinstance(expr, ast.Index):
            return self.expr_has_user_call(expr.base) or self.expr_has_user_call(expr.index)
        if isinstance(expr, ast.FString):
            return any(self.expr_has_user_call(part) for part in expr.parts if not isinstance(part, str))
        return False

    def _wrap_throw_cleanup(self, stmt: ast.Stmt, indent: int, cleanup: list[str]) -> list[str]:
        pad = "    " * indent
        body = self._emit_stmt(stmt, indent)
        return [
            f"{pad}sa_try_top++;",
            f"{pad}if (SA_SETJMP(sa_try_stack[sa_try_top - 1].env) == 0) {{",
            *(f"    {line}" if line else line for line in body),
            f"{pad}    sa_try_top--;",
            f"{pad}}} else {{",
            f"{pad}    sa_try_top--;",
            *cleanup,
            f"{pad}    sa_throw_dispatch();",
            f"{pad}}}",
        ]

    def _emit_stmt(self, stmt: ast.Stmt, indent: int) -> list[str]:
        pad = "    " * indent
        if isinstance(stmt, ast.NoOp):
            return [self.source_comment(stmt.line_no, indent)] if self.source_lines.get(stmt.line_no) else []
        if isinstance(stmt, ast.LocalDeclaration):
            return self.local_declaration_stmt(stmt, indent)
        if isinstance(stmt, ast.Print):
            return self.print_stmt(stmt, indent)
        if isinstance(stmt, ast.Assign):
            return self.assign_stmt(stmt, indent)
        if isinstance(stmt, ast.Call):
            c_func = self.resolve_c_func(stmt.name)
            if c_func is not None:
                prelude, args, cleanup = self.c_call_args_with_prelude(c_func, stmt.args)
                return [self.source_comment(stmt.line_no, indent), *(f"{pad}{line}" for line in prelude), f"{pad}{c_func.name}({', '.join(args)});", *(f"{pad}{line}" for line in cleanup)]
            prelude, args, cleanup = self.call_args_with_prelude(stmt.name, stmt.args)
            return [self.source_comment(stmt.line_no, indent), *(f"{pad}{line}" for line in prelude), f"{pad}{self.call_c_name(stmt.name)}({', '.join(args)});", *(f"{pad}{line}" for line in cleanup)]
        if isinstance(stmt, ast.TryCatch):
            return self.try_catch_stmt(stmt, indent)
        if isinstance(stmt, ast.ThrowNew):
            prelude, message, cleanup = self.expr_with_prelude(stmt.message)
            return [
                self.source_comment(stmt.line_no, indent),
                *(f"{pad}{line}" for line in prelude),
                f"{pad}sa_raise_new(\"{stmt.error_type}\", {message}, {stmt.line_no}, \"{self.current_sub_name()}\");",
                *(f"{pad}{line}" for line in cleanup),
                *self.active_local_resource_cleanup_lines(indent),
                f"{pad}sa_throw_dispatch();",
            ]
        if isinstance(stmt, ast.ThrowVar):
            return [
                self.source_comment(stmt.line_no, indent),
                f"{pad}sa_raise_error(&{self.c_value(stmt.name)});",
                *self.active_local_resource_cleanup_lines(indent),
                f"{pad}sa_throw_dispatch();",
            ]
        if isinstance(stmt, ast.If):
            return self.if_stmt(stmt, indent)
        if isinstance(stmt, ast.ForLoop):
            return self.for_stmt(stmt, indent)
        if isinstance(stmt, ast.WhileLoop):
            return self.while_stmt(stmt, indent)
        if isinstance(stmt, ast.Goto):
            return [self.source_comment(stmt.line_no, indent), f"{pad}goto {self.label_ident(stmt.label)};"]
        if isinstance(stmt, ast.Gosub):
            return [
                self.source_comment(stmt.line_no, indent),
                f'{pad}if (sa_gosub_top >= 64) {{ fputs("SonAlgebraic runtime: GOSUB stack overflow\\n", stderr); exit(1); }}',
                f"{pad}sa_gosub_stack[sa_gosub_top++] = {stmt.line_no};",
                f"{pad}goto {self.label_ident(stmt.label)};",
                f"{self.gosub_return_label(stmt.line_no)}:;",
            ]
        if isinstance(stmt, ast.Label):
            return [self.source_comment(stmt.line_no, indent), f"{self.label_ident(stmt.name)}:;"]
        if isinstance(stmt, ast.Return):
            if stmt.expr is None:
                if self.current_sub_has_gosub():
                    return [
                        self.source_comment(stmt.line_no, indent),
                        *self.gosub_return_dispatch_lines(indent),
                        *self.active_local_resource_cleanup_lines(indent),
                        f"{pad}return;",
                    ]
                return [self.source_comment(stmt.line_no, indent), *self.active_local_resource_cleanup_lines(indent), f"{pad}return;"]
            prelude, value, _cleanup = self.expr_with_prelude(stmt.expr)
            temp = self.next_temp()
            return_type = self.current_sub_return_type()
            if is_handle(return_type) and isinstance(stmt.expr, ast.NullLiteral):
                value = "0"
            return_value = f"{pad}{self.c_type(return_type)} {temp} = {value};"
            return [self.source_comment(stmt.line_no, indent), *(f"{pad}{line}" for line in prelude), return_value, *self.active_local_resource_cleanup_lines(indent), f"{pad}return {temp};"]
        if isinstance(stmt, ast.End):
            return [self.source_comment(stmt.line_no, indent), f"{pad}goto sa_program_end;"]
        if isinstance(stmt, ast.Input):
            return self.input_stmt(stmt, indent)
        if isinstance(stmt, ast.Cls):
            return [self.source_comment(stmt.line_no, indent), f"{pad}sa_cls();"]
        raise SonCompileError("未知语句类型", stmt.line_no)

    def print_stmt(self, stmt: ast.Print, indent: int) -> list[str]:
        pad = "    " * indent
        if stmt.expr is None:
            return [self.source_comment(stmt.line_no, indent), f"{pad}puts(\"\");"]

        value_type = self.type_of(stmt.expr)
        prelude, value, cleanup = self.expr_with_prelude(stmt.expr)
        lines = [self.source_comment(stmt.line_no, indent), *(f"{pad}{line}" for line in prelude)]
        if is_string(value_type):
            lines.append(f"{pad}sa_print_string({value});")
        elif is_cptr(value_type) or is_ptr(value_type):
            lines.append(f'{pad}printf("%p", {value});')
            lines.append(f'{pad}puts("");')
        elif is_handle(value_type):
            lines.append(f"{pad}sa_print_long((long long){value});")
        elif is_error(value_type):
            lines.append(f"{pad}sa_print_string({value}.message);")
        elif is_symbol(value_type):
            temp = self.next_temp()
            lines.append(f"{pad}char* {temp} = sa_symbol_to_string({value});")
            lines.append(f"{pad}sa_print_string({temp});")
            lines.append(f"{pad}free({temp});")
        elif value_type.subtype == "LONG":
            lines.append(f"{pad}sa_print_long({value});")
        else:
            lines.append(f"{pad}sa_print_double({value});")
        lines.extend(f"{pad}{line}" for line in cleanup)
        return lines

    def local_declaration_stmt(self, stmt: ast.LocalDeclaration, indent: int) -> list[str]:
        pad = "    " * indent
        self.symbols[stmt.name.lower()] = Symbol(stmt.name, stmt.type_spec, stmt.mutable)
        name = self.c_ident(stmt.name)
        lines = [self.source_comment(stmt.line_no, indent)]
        if stmt.type_spec.array_size is not None:
            lines.append(f"{pad}{self.c_type(stmt.type_spec)} {name}[{stmt.type_spec.array_size}] = {{0}};")
            if is_string(stmt.type_spec):
                # STRING 数组：每个元素初始化为空串，并登记整段数组待逐元素释放
                idx = self.next_temp()
                lines.append(f"{pad}for (long long {idx} = 0; {idx} < {stmt.type_spec.array_size}; {idx}++) {{")
                lines.append(f"{pad}    {name}[{idx}] = sa_strdup(\"\");")
                lines.append(f"{pad}}}")
                self.register_local_resource(name, stmt.type_spec)
            return lines
        init = "NULL" if is_string(stmt.type_spec) or is_cptr(stmt.type_spec) or is_ptr(stmt.type_spec) else "0"
        if stmt.type_spec.name == "ENTITY":
            init = "{0}"
        if is_error(stmt.type_spec):
            init = '{0, "ERR_NONE", NULL, 0, NULL}'
        if is_symbol(stmt.type_spec):
            init = "NULL"
        lines.append(f"{pad}{self.c_type(stmt.type_spec)} {name} = {init};")
        self.register_local_resource(name, stmt.type_spec)
        if is_string(stmt.type_spec):
            lines.append(f"{pad}{name} = sa_strdup(\"\");")
        elif stmt.type_spec.name == "ENTITY" and self.type_has_managed_resources(stmt.type_spec):
            lines.extend(self.entity_init_lines(name, stmt.type_spec, indent))
        if stmt.expr is not None:
            if is_symbol(stmt.type_spec):
                # SYMBOL 走独立的符号树构建路径，自带 prelude（DERIV/SUBST 等会产生临时量）
                sym_prelude, sym_value, sym_cleanup = self.symbol_expr_with_prelude(stmt.expr)
                lines.extend(f"{pad}{line}" for line in sym_prelude)
                # 先把新树求值到临时量，再释放旧树，最后接管。否则当 RHS 引用 LHS 自身
                # （如 wave = wave * t + ...）时，sa_symbol_clone 会克隆已被 free 的指针（UAF）。
                new_tmp = self.next_temp()
                lines.append(f"{pad}SaSymbol {new_tmp} = {sym_value};")
                lines.append(f"{pad}sa_symbol_free({name});")
                lines.append(f"{pad}{name} = {new_tmp};")
                lines.extend(f"{pad}{line}" for line in sym_cleanup)
                return lines
            prelude, value, cleanup = self.expr_with_prelude(stmt.expr)
            lines.extend(f"{pad}{line}" for line in prelude)
            if is_string(stmt.type_spec):
                lines.append(f"{pad}sa_set_string(&{name}, {value});")
            elif stmt.type_spec.name == "ENTITY" and self.type_has_managed_resources(stmt.type_spec):
                lines.extend(self.entity_copy_lines(name, value, stmt.type_spec, indent))
            else:
                if is_handle(stmt.type_spec) and isinstance(stmt.expr, ast.NullLiteral):
                    value = "0"
                lines.append(f"{pad}{name} = {value};")
            lines.extend(f"{pad}{line}" for line in cleanup)
        return lines

    def assign_stmt(self, stmt: ast.Assign, indent: int) -> list[str]:
        pad = "    " * indent
        # SYMBOL 变量赋值走独立路径：symbol_expr 自带 prelude（DERIV/SUBST 产生临时量）
        if isinstance(stmt.target, ast.VarRef) and is_symbol(self.type_of(stmt.target)):
            name = stmt.target.name
            sym_prelude, sym_value, sym_cleanup = self.symbol_expr_with_prelude(stmt.expr)
            lines = [self.source_comment(stmt.line_no, indent), *(f"{pad}{line}" for line in sym_prelude)]
            # 同 LocalDeclaration：新树先落到临时量再释放旧树，规避 wave = f(wave) 的 use-after-free
            new_tmp = self.next_temp()
            lines.append(f"{pad}SaSymbol {new_tmp} = {sym_value};")
            lines.append(f"{pad}sa_symbol_free({self.c_value(name)});")
            lines.append(f"{pad}{self.c_value(name)} = {new_tmp};")
            lines.extend(f"{pad}{line}" for line in sym_cleanup)
            return lines
        prelude, value, cleanup = self.expr_with_prelude(stmt.expr)
        lines = [self.source_comment(stmt.line_no, indent), *(f"{pad}{line}" for line in prelude)]

        if isinstance(stmt.target, ast.Deref):
            target_expr = self.expr(stmt.target.expr)
            target_type = self.type_of(stmt.target)
            if is_string(target_type):
                lines.append(f"{pad}sa_set_string(({target_expr}), {value});")
            else:
                if is_handle(target_type) and isinstance(stmt.expr, ast.NullLiteral):
                    value = "0"
                lines.append(f"{pad}(*({target_expr})) = {value};")
            lines.extend(f"{pad}{line}" for line in cleanup)
            return lines

        if isinstance(stmt.target, ast.Index):
            target_type = self.type_of(stmt.target)
            if is_string(target_type):
                lines.append(f"{pad}sa_set_string(&{self.expr(stmt.target)}, {value});")
            else:
                if is_handle(target_type) and isinstance(stmt.expr, ast.NullLiteral):
                    value = "0"
                lines.append(f"{pad}{self.expr(stmt.target)} = {value};")
            lines.extend(f"{pad}{line}" for line in cleanup)
            return lines

        name = stmt.target.name
        target_type = self.type_of(ast.VarRef(stmt.line_no, name))
        target_root = self.symbols[name.split(".", 1)[0].lower()]
        if is_string(target_type):
            target_name = self.c_value(name) if target_root.by_ref else self.c_ident_path(name)
            lines.append(f"{pad}sa_set_string(&{target_name}, {value});")
        elif target_type.name == "ENTITY" and self.type_has_managed_resources(target_type):
            lines.extend(self.entity_copy_lines(self.c_value(name), value, target_type, indent))
        else:
            if is_handle(target_type) and isinstance(stmt.expr, ast.NullLiteral):
                value = "0"
            lines.append(f"{pad}{self.c_value(name)} = {value};")
        lines.extend(f"{pad}{line}" for line in cleanup)
        return lines

    def input_stmt(self, stmt: ast.Input, indent: int) -> list[str]:
        pad = "    " * indent
        target = self.symbols[stmt.target.lower()]
        name = self.c_value(stmt.target)
        prelude, prompt, cleanup = self.expr_with_prelude(stmt.prompt)
        lines = [self.source_comment(stmt.line_no, indent), *(f"{pad}{line}" for line in prelude)]
        lines.extend([f"{pad}printf(\"%s\", {prompt});", f"{pad}char sa_input_buf[4096];", f"{pad}sa_read_line(sa_input_buf, sizeof(sa_input_buf));"])
        if is_string(target.type_spec):
            lines.append(f"{pad}sa_set_string(&{name}, sa_input_buf);")
        elif is_numeric(target.type_spec):
            cast = "(long long)" if target.type_spec.subtype == "LONG" else ""
            lines.append(f"{pad}{name} = {cast}sa_number(sa_input_buf);")
        else:
            raise SonCompileError("IO.INPUT 当前只支持 STRING 和 NUM", stmt.line_no)
        lines.extend(f"{pad}{line}" for line in cleanup)
        return lines

    def try_catch_stmt(self, stmt: ast.TryCatch, indent: int) -> list[str]:
        pad = "    " * indent
        prelude, args, cleanup = self.call_args_with_prelude(stmt.call_name, stmt.args)
        lines = [self.source_comment(stmt.line_no, indent), *(f"{pad}{line}" for line in prelude)]
        lines.append(f"{pad}sa_try_top++;")
        lines.append(f"{pad}if (SA_SETJMP(sa_try_stack[sa_try_top - 1].env) == 0) {{")
        lines.append(f"{pad}    {self.call_c_name(stmt.call_name)}({', '.join(args)});")
        lines.append(f"{pad}    sa_try_top--;")
        lines.append(f"{pad}}} else {{")
        lines.append(f"{pad}    sa_try_top--;")
        lines.append(f"{pad}    sa_set_error(&{self.c_ident(stmt.traceback_var)}, &sa_current_error);")

        for index, branch in enumerate(stmt.catches):
            prefix = "if" if index == 0 else "else if"
            condition = "1" if branch.error_type == "ERR_ANY" else f"strcmp(sa_current_error.type, \"{branch.error_type}\") == 0"
            lines.append(f"{pad}    {prefix} ({condition}) {{")
            lines.append(self.source_comment(branch.line_no, indent + 2))
            alias_c = self.c_ident(branch.alias)
            if self.current_sub_has_gosub() or self.current_sub_has_goto():
                # 提升到函数作用域：GOSUB RETURN 的 goto 会跳回 CATCH 块内返回标签，
                # GOTO 可能从 CATCH 块内直接跳出——两者都会跨过/跳过块尾的清理。
                self.hoisted_catch_vars[alias_c] = alias_c
            else:
                lines.append(f"{pad}        SaError {alias_c} = {{0, \"ERR_NONE\", NULL, 0, NULL}};")
            lines.append(f"{pad}        sa_set_error(&{alias_c}, &sa_current_error);")
            branch_scope = self.symbols.copy()
            branch_scope[branch.alias.lower()] = Symbol(branch.alias, ast.TypeSpec("ERROR"), False)
            self.scope_stack.append(branch_scope)
            lines.extend(self.block(branch.body, indent + 2))
            self.scope_stack.pop()
            lines.append(f"{pad}        sa_error_clear(&{self.c_ident(branch.alias)});")
            lines.append(f"{pad}    }}")

        lines.append(f"{pad}    else {{")
        # 无匹配 CATCH：向外层帧重抛前，先清理当前 SUB 的局部资源，避免逃逸泄漏
        lines.extend(f"{pad}        {line.strip()}" for line in self.active_local_resource_cleanup_lines(0))
        lines.append(f"{pad}        sa_throw_dispatch();")
        lines.append(f"{pad}    }}")
        lines.append(f"{pad}}}")
        lines.extend(f"{pad}{line}" for line in cleanup)
        return lines

    def expr_with_prelude(self, expr: ast.Expr) -> tuple[list[str], str, list[str]]:
        self.prelude_stack.append([])
        self.cleanup_stack.append([])
        value = self.expr(expr)
        prelude = self.prelude_stack.pop()
        cleanup = self.cleanup_stack.pop()
        return prelude, value, cleanup

    def expr(self, expr: ast.Expr | None) -> str:
        if expr is None:
            return ""
        if isinstance(expr, ast.NumberLiteral):
            return c_number(expr.value)
        if isinstance(expr, ast.NullLiteral):
            return "NULL"
        if isinstance(expr, ast.BoolLiteral):
            return "1" if expr.value else "0"
        if isinstance(expr, ast.StringLiteral):
            return c_string(expr.value)
        if isinstance(expr, ast.FString):
            return self.fstring(expr)
        if isinstance(expr, ast.VarRef):
            builtin = resolve_builtin_const(expr.name, self.checked.uses)
            if builtin is not None:
                return builtin[1]
            enum_value = self.checked.enum_members.get(expr.name.lower())
            if enum_value is not None:
                return str(enum_value)
            external_const = self.external_const_c_name(expr.name)
            if external_const is not None:
                return external_const
            return self.c_value(expr.name)
        if isinstance(expr, ast.Unary):
            return f"({self.c_unary_op(expr.op)}{self.expr(expr.expr)})"
        if isinstance(expr, ast.Deref):
            return f"(*{self.expr(expr.expr)})"
        if isinstance(expr, ast.AddressOf):
            return f"(&{self.c_value(expr.expr.name)})"
        if isinstance(expr, ast.Cast):
            return f"({self.c_type(expr.type_spec)})({self.expr(expr.expr)})"
        if isinstance(expr, ast.Binary):
            return self.binary(expr)
        if isinstance(expr, ast.Index):
            return f"{self.expr(expr.base)}[{self.expr(expr.index)}]"
        if isinstance(expr, ast.CallExpr):
            return self.call_expr(expr)
        raise SonCompileError("未知表达式类型", expr.line_no)

    def binary(self, expr: ast.Binary) -> str:
        if expr.op == "**":
            return f"pow({self.expr(expr.left)}, {self.expr(expr.right)})"
        op = {
            "=": "==", "<>": "!=", "AND": "&&", "OR": "||",
            "BAND": "&", "BOR": "|", "BXOR": "^", "SHL": "<<", "SHR": ">>",
        }.get(expr.op, expr.op)
        left_type = self.type_of(expr.left)
        right_type = self.type_of(expr.right)
        if expr.op in {"=", "==", "!=", "<>"} and is_string(left_type) and is_string(right_type):
            cmp = f"(strcmp({self.expr(expr.left)}, {self.expr(expr.right)}) == 0)"
            return cmp if op == "==" else f"(!{cmp})"
        if expr.op in {"=", "==", "!=", "<>"} and (is_handle(left_type) or is_handle(right_type)):
            left = "0" if isinstance(expr.left, ast.NullLiteral) else self.expr(expr.left)
            right = "0" if isinstance(expr.right, ast.NullLiteral) else self.expr(expr.right)
            return f"({left} {op} {right})"
        return f"({self.expr(expr.left)} {op} {self.expr(expr.right)})"

    def call_expr(self, expr: ast.CallExpr) -> str:
        name = expr.name.upper()
        if name == "NUMBER":
            self.require_arg_count(expr, 1)
            return f"sa_number({self.expr(expr.args[0])})"
        if name == "STRING":
            self.require_arg_count(expr, 1)
            arg_type = self.type_of(expr.args[0])
            if is_string(arg_type):
                return self.expr(expr.args[0])
            if is_error(arg_type):
                return f"{self.expr(expr.args[0])}.message"
            if is_symbol(arg_type):
                temp = self.next_temp()
                self.add_prelude(f"char* {temp} = sa_symbol_to_string({self.expr(expr.args[0])});")
                self.add_cleanup(f"free({temp});")
                return temp
            if arg_type.subtype == "LONG" or is_handle(arg_type):
                temp = self.next_temp()
                self.add_prelude(f"char* {temp} = sa_to_string_long((long long){self.expr(expr.args[0])});")
                self.add_cleanup(f"free({temp});")
                return temp
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = sa_to_string_double({self.expr(expr.args[0])});")
            self.add_cleanup(f"free({temp});")
            return temp
        if self.is_math_function(expr.name, "POW"):
            self.require_arg_count(expr, 2)
            if is_symbol(self.type_of(expr)):
                temp = self.next_temp()
                self.add_prelude(f"SaSymbol {temp} = {self.symbol_expr(expr)};")
                self.add_cleanup(f"sa_symbol_free({temp});")
                return temp
            return f"pow({self.expr(expr.args[0])}, {self.expr(expr.args[1])})"
        algebra = self.symbol_algebra_call(expr)
        if algebra is not None:
            return algebra
        string_call = self.string_function_call(expr)
        if string_call is not None:
            return string_call
        net_call = self.net_function_call(expr)
        if net_call is not None:
            return net_call
        file_call = self.file_function_call(expr)
        if file_call is not None:
            return file_call
        desktop_call = self.desktop_function_call(expr)
        if desktop_call is not None:
            return desktop_call
        binary_call = self.binary_function_call(expr)
        if binary_call is not None:
            return binary_call
        list_call = self.list_function_call(expr)
        if list_call is not None:
            return list_call
        map_call = self.map_function_call(expr)
        if map_call is not None:
            return map_call
        gui_call = self.gui_function_call(expr)
        if gui_call is not None:
            return gui_call
        c_func = self.resolve_c_func(expr.name)
        if c_func is not None:
            prelude, args, cleanup = self.c_call_args_with_prelude(c_func, expr.args)
            for line in prelude:
                self.add_prelude(line)
            for line in cleanup:
                self.add_cleanup(line)
            return f"{c_func.name}({', '.join(args)})"
        sub = self.checked.subs.get(expr.name.lower()) or self.resolve_external_sub(expr.name)
        if sub is not None:
            prelude, args, cleanup = self.call_args_with_prelude(expr.name, expr.args)
            for line in prelude:
                self.add_prelude(line)
            for line in cleanup:
                self.add_cleanup(line)
            return f"{self.call_c_name(expr.name)}({', '.join(args)})"
        raise SonCompileError(f"未知内置函数: {expr.name}", expr.line_no)

    def symbol_algebra_call(self, expr: ast.CallExpr) -> str | None:
        """SYMBOL 代数内置函数 -> runtime 调用。"""
        name = expr.name.upper()
        if name not in {"DERIV", "SIMPLIFY", "SUBST", "EVAL"}:
            return None
        sym = self.expr(expr.args[0])
        if name == "EVAL":
            return f"sa_symbol_eval({sym})"
        # 返回新建的 SaSymbol，登记 free
        temp = self.next_temp()
        if name == "SIMPLIFY":
            call = f"sa_symbol_simplify({sym})"
        elif name == "DERIV":
            var = c_string(expr.args[1].value)
            call = f"sa_symbol_deriv({sym}, {var})"
        else:  # SUBST
            var = c_string(expr.args[1].value)
            value = self.expr(expr.args[2])
            call = f"sa_symbol_subst({sym}, {var}, {value})"
        self.add_prelude(f"SaSymbol {temp} = {call};")
        self.add_cleanup(f"sa_symbol_free({temp});")
        return temp

    def string_function_call(self, expr: ast.CallExpr) -> str | None:
        """SYS.STRING 内置函数 -> runtime 调用。返回 None 表示不是字符串函数。"""
        split = split_module_member(expr.name)
        if split is None:
            return None
        alias, member = split
        if self.checked.uses.get(alias) != "SYS.STRING":
            return None
        member = member.upper()
        args = [self.expr(arg) for arg in expr.args]
        # 返回 char* 的函数：包临时变量并登记 free
        heap_returning = {
            "CONCAT": "sa_str_concat",
            "SLICE": "sa_str_slice",
            "UPPER": "sa_str_upper",
            "LOWER": "sa_str_lower",
            "REPLACE": "sa_str_replace",
        }
        if member in heap_returning:
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = {heap_returning[member]}({', '.join(args)});")
            self.add_cleanup(f"free({temp});")
            return temp
        # 返回 long long 的函数直接内联
        if member == "LENGTH":
            return f"sa_str_length({args[0]})"
        if member == "FIND":
            return f"sa_str_find({args[0]}, {args[1]})"
        return None

    def net_function_call(self, expr: ast.CallExpr) -> str | None:
        """SYS.NET 内置函数 -> runtime 调用。当前支持阻塞 HTTP GET/STATUS。"""
        split = split_module_member(expr.name)
        if split is None:
            return None
        alias, member = split
        if self.checked.uses.get(alias) != "SYS.NET":
            return None
        member = member.upper()
        args = ["0" if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        if member == "GET":
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = sa_net_http_get({args[0]});")
            self.add_cleanup(f"free({temp});")
            return temp
        if member == "STATUS":
            return f"sa_net_http_status({args[0]})"
        if member == "POST":
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = sa_net_http_post({args[0]}, {args[1]}, {args[2]});")
            self.add_cleanup(f"free({temp});")
            return temp
        if member == "REQUEST":
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = sa_net_http_request({args[0]}, {args[1]}, {args[2]}, {args[3]});")
            self.add_cleanup(f"free({temp});")
            return temp
        if member == "REQUEST_STATUS":
            return f"sa_net_http_request_status({args[0]}, {args[1]}, {args[2]}, {args[3]})"
        if member == "REQUEST_TIMEOUT":
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = sa_net_http_request_timeout({args[0]}, {args[1]}, {args[2]}, {args[3]}, {args[4]});")
            self.add_cleanup(f"free({temp});")
            return temp
        if member == "REQUEST_STATUS_TIMEOUT":
            return f"sa_net_http_request_status_timeout({args[0]}, {args[1]}, {args[2]}, {args[3]}, {args[4]})"
        if member == "LAST_HEADERS":
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = sa_net_last_headers_copy();")
            self.add_cleanup(f"free({temp});")
            return temp
        if member == "LAST_ERROR":
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = sa_net_last_error_copy();")
            self.add_cleanup(f"free({temp});")
            return temp
        if member == "LAST_CODE":
            return "sa_net_last_code_value()"
        if member == "LAST_PEER_PORT":
            return "sa_net_last_peer_port_value()"
        if member in {"LAST_PEER_HOST", "DNS"}:
            temp = self.next_temp()
            call = "sa_net_last_peer_host_copy()" if member == "LAST_PEER_HOST" else f"sa_net_dns({args[0]})"
            self.add_prelude(f"char* {temp} = {call};")
            self.add_cleanup(f"free({temp});")
            return temp
        if member == "URLENCODE":
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = sa_net_urlencode({args[0]});")
            self.add_cleanup(f"free({temp});")
            return temp
        direct = {
            "TCP_CONNECT": "sa_net_tcp_connect",
            "TLS_CONNECT": "sa_net_tls_connect",
            "TCP_LISTEN": "sa_net_tcp_listen",
            "TCP_ACCEPT": "sa_net_tcp_accept",
            "TCP_LISTENER_CLOSE": "sa_net_tcp_listener_close",
            "STREAM_SEND": "sa_net_stream_send",
            "STREAM_SEND_BUFFER": "sa_net_stream_send_buffer",
            "STREAM_RECV_BUFFER": "sa_net_stream_recv_buffer",
            "STREAM_CLOSE": "sa_net_stream_close",
            "UDP_OPEN": "sa_net_udp_open",
            "UDP_BIND": "sa_net_udp_bind",
            "UDP_CONNECT": "sa_net_udp_connect",
            "UDP_SEND": "sa_net_udp_send",
            "UDP_SEND_TO": "sa_net_udp_send_to",
            "UDP_SEND_BUFFER": "sa_net_udp_send_buffer",
            "UDP_SEND_BUFFER_TO": "sa_net_udp_send_buffer_to",
            "UDP_RECV_BUFFER": "sa_net_udp_recv_buffer",
            "UDP_CLOSE": "sa_net_udp_close",
            "LOCAL_PORT": "sa_net_tcp_listener_local_port",
            "UDP_LOCAL_PORT": "sa_net_udp_local_port",
        }
        if member in direct:
            return f"{direct[member]}({', '.join(args)})"
        if member in {"STREAM_RECV", "UDP_RECV"}:
            temp = self.next_temp()
            fn = "sa_net_stream_recv" if member == "STREAM_RECV" else "sa_net_udp_recv"
            self.add_prelude(f"char* {temp} = {fn}({', '.join(args)});")
            self.add_cleanup(f"free({temp});")
            return temp
        return None

    def file_function_call(self, expr: ast.CallExpr) -> str | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.FILE":
            return None
        member = split[1].upper()
        args = ["0" if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        if member in {"READ", "WRITE", "SEEK", "TELL", "SIZE", "CLOSE"} and isinstance(expr.args[0], ast.NullLiteral):
            args[0] = "0"
        direct = {
            "OPEN": "sa_file_open",
            "WRITE": "sa_file_write",
            "SEEK": "sa_file_seek",
            "TELL": "sa_file_tell",
            "SIZE": "sa_file_size",
            "CLOSE": "sa_file_close",
            "WRITE_TEXT": "sa_file_write_text",
            "APPEND_TEXT": "sa_file_append_text",
            "EXISTS": "sa_file_exists",
            "IS_FILE": "sa_file_is_file",
            "IS_DIR": "sa_file_is_dir",
            "DELETE": "sa_file_delete",
            "MKDIR": "sa_file_mkdir",
        }
        if member in direct:
            return f"{direct[member]}({', '.join(args)})"
        heap = {
            "READ": "sa_file_read",
            "READ_TEXT": "sa_file_read_text",
            "CWD": "sa_file_cwd",
            "ABSOLUTE": "sa_file_absolute",
            "LAST_ERROR": "sa_file_last_error_copy",
        }
        if member in heap:
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = {heap[member]}({', '.join(args)});")
            self.add_cleanup(f"free({temp});")
            return temp
        return None

    def desktop_function_call(self, expr: ast.CallExpr) -> str | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.DESKTOP":
            return None
        member = split[1].upper()
        args = ["0" if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        direct = {
            "MESSAGE": "sa_desktop_message",
            "OPEN": "sa_desktop_open",
            "CLIPBOARD_SET": "sa_desktop_clipboard_set",
        }
        if member in direct:
            return f"{direct[member]}({', '.join(args)})"
        heap = {
            "CLIPBOARD_GET": "sa_desktop_clipboard_get",
            "LAST_ERROR": "sa_desktop_last_error_copy",
        }
        if member in heap:
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = {heap[member]}({', '.join(args)});")
            self.add_cleanup(f"free({temp});")
            return temp
        return None

    def binary_function_call(self, expr: ast.CallExpr) -> str | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.BINARY":
            return None
        member = split[1].upper()
        args = ["0" if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        direct = {
            "NEW": "sa_binary_new",
            "CLOSE": "sa_binary_close",
            "LENGTH": "sa_binary_length",
            "SLICE": "sa_binary_slice",
            "COPY": "sa_binary_copy",
            "HEX_DECODE": "sa_binary_hex_decode",
            "PACK_U16_LE": "sa_binary_pack_u16_le",
            "PACK_U16_BE": "sa_binary_pack_u16_be",
            "PACK_U32_LE": "sa_binary_pack_u32_le",
            "PACK_U32_BE": "sa_binary_pack_u32_be",
            "PACK_U64_LE": "sa_binary_pack_u64_le",
            "PACK_U64_BE": "sa_binary_pack_u64_be",
            "UNPACK_U16_LE": "sa_binary_unpack_u16_le",
            "UNPACK_U16_BE": "sa_binary_unpack_u16_be",
            "UNPACK_U32_LE": "sa_binary_unpack_u32_le",
            "UNPACK_U32_BE": "sa_binary_unpack_u32_be",
            "UNPACK_U64_LE": "sa_binary_unpack_u64_le",
            "UNPACK_U64_BE": "sa_binary_unpack_u64_be",
            "CHECKSUM8": "sa_binary_checksum8",
        }
        if member in direct:
            return f"{direct[member]}({', '.join(args)})"
        heap = {
            "HEX_ENCODE": "sa_binary_hex_encode",
            "LAST_ERROR": "sa_binary_last_error_copy",
        }
        if member in heap:
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = {heap[member]}({', '.join(args)});")
            self.add_cleanup(f"free({temp});")
            return temp
        return None

    def list_function_call(self, expr: ast.CallExpr) -> str | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.LIST":
            return None
        member = split[1].upper()
        args = ["0" if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        direct = {
            "NEW": "sa_list_new",
            "PUSH": "sa_list_push",
            "POP": "sa_list_pop",
            "GET": "sa_list_get",
            "SET": "sa_list_set",
            "INSERT": "sa_list_insert",
            "REMOVE": "sa_list_remove",
            "LENGTH": "sa_list_length",
            "CLEAR": "sa_list_clear",
            "CLOSE": "sa_list_close",
            "NEW_STR": "sa_strlist_new",
            "PUSH_STR": "sa_strlist_push",
            "SET_STR": "sa_strlist_set",
            "INSERT_STR": "sa_strlist_insert",
            "REMOVE_STR": "sa_strlist_remove",
            "LENGTH_STR": "sa_strlist_length",
            "CLEAR_STR": "sa_strlist_clear",
            "CLOSE_STR": "sa_strlist_close",
        }
        if member in direct:
            return f"{direct[member]}({', '.join(args)})"
        heap = {
            "POP_STR": "sa_strlist_pop",
            "GET_STR": "sa_strlist_get",
            "JOIN_STR": "sa_strlist_join",
            "LAST_ERROR": "sa_list_last_error_copy",
        }
        if member in heap:
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = {heap[member]}({', '.join(args)});")
            self.add_cleanup(f"free({temp});")
            return temp
        return None

    def map_function_call(self, expr: ast.CallExpr) -> str | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.MAP":
            return None
        member = split[1].upper()
        args = ["0" if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        direct = {
            "NEW": "sa_map_new",
            "SET": "sa_map_set",
            "GET": "sa_map_get",
            "HAS": "sa_map_has",
            "REMOVE": "sa_map_remove",
            "LENGTH": "sa_map_length",
            "KEYS": "sa_map_keys",
            "CLEAR": "sa_map_clear",
            "CLOSE": "sa_map_close",
            "NEW_STR": "sa_strmap_new",
            "SET_STR": "sa_strmap_set",
            "HAS_STR": "sa_strmap_has",
            "REMOVE_STR": "sa_strmap_remove",
            "LENGTH_STR": "sa_strmap_length",
            "KEYS_STR": "sa_strmap_keys",
            "CLEAR_STR": "sa_strmap_clear",
            "CLOSE_STR": "sa_strmap_close",
        }
        if member in direct:
            return f"{direct[member]}({', '.join(args)})"
        heap = {
            "GET_STR": "sa_strmap_get",
            "LAST_ERROR": "sa_map_last_error_copy",
        }
        if member in heap:
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = {heap[member]}({', '.join(args)});")
            self.add_cleanup(f"free({temp});")
            return temp
        return None

    def gui_function_call(self, expr: ast.CallExpr) -> str | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.GUI":
            return None
        member = split[1].upper()
        args = ["0" if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        direct = {
            "WINDOW": "sa_gui_window",
            "BUTTON": "sa_gui_button",
            "LABEL": "sa_gui_label",
            "TEXTBOX": "sa_gui_textbox",
            "SET_TEXT": "sa_gui_set_text",
            "WAIT_EVENT": "sa_gui_wait_event",
            "CLOSE": "sa_gui_close",
        }
        if member in direct:
            return f"{direct[member]}({', '.join(args)})"
        heap = {
            "GET_TEXT": "sa_gui_get_text",
            "LAST_ERROR": "sa_gui_last_error_copy",
        }
        if member in heap:
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = {heap[member]}({', '.join(args)});")
            self.add_cleanup(f"free({temp});")
            return temp
        return None

    def fstring(self, expr: ast.FString) -> str:
        temp = self.next_temp()
        self.add_prelude(f"SaStringBuilder {temp};")
        self.add_prelude(f"sa_sb_init(&{temp});")
        for part in expr.parts:
            if isinstance(part, str):
                self.add_prelude(f"sa_sb_append(&{temp}, {c_string(part)});")
            else:
                for line in self.append_expr_to_builder(temp, part):
                    self.add_prelude(line)
        self.add_prelude(f"char* {temp}_result = sa_sb_take(&{temp});")
        self.add_cleanup(f"free({temp}_result);")
        return f"{temp}_result"

    def append_expr_to_builder(self, builder: str, expr: ast.Expr) -> list[str]:
        value = self.expr(expr)
        value_type = self.type_of(expr)
        if is_string(value_type):
            return [f"sa_sb_append(&{builder}, {value});"]
        if is_cptr(value_type) or is_ptr(value_type):
            temp = self.next_temp()
            return [f"char* {temp} = sa_to_string_pointer({value});", f"sa_sb_append(&{builder}, {temp});", f"free({temp});"]
        if is_error(value_type):
            return [f"sa_sb_append(&{builder}, {value}.message);"]
        if is_symbol(value_type):
            temp = self.next_temp()
            return [f"char* {temp} = sa_symbol_to_string({value});", f"sa_sb_append(&{builder}, {temp});", f"free({temp});"]
        if value_type.subtype == "LONG" or is_handle(value_type):
            temp = self.next_temp()
            return [f"char* {temp} = sa_to_string_long((long long){value});", f"sa_sb_append(&{builder}, {temp});", f"free({temp});"]
        temp = self.next_temp()
        return [f"char* {temp} = sa_to_string_double({value});", f"sa_sb_append(&{builder}, {temp});", f"free({temp});"]

    def for_stmt(self, stmt: ast.ForLoop, indent: int) -> list[str]:
        pad = "    " * indent
        var = self.c_value(stmt.var)
        # 边界和步长只在进入循环前求值一次（BASIC 语义），存入临时变量
        start_pre, start_val, start_cl = self.expr_with_prelude(stmt.start)
        end_pre, end_val, end_cl = self.expr_with_prelude(stmt.end)
        end_tmp = self.next_temp()
        step_tmp = self.next_temp()
        lines = [self.source_comment(stmt.line_no, indent)]
        lines.extend(f"{pad}{line}" for line in start_pre)
        lines.extend(f"{pad}{line}" for line in end_pre)
        lines.append(f"{pad}{var} = {start_val};")
        lines.append(f"{pad}long long {end_tmp} = {end_val};")
        if stmt.step is not None:
            step_pre, step_val, step_cl = self.expr_with_prelude(stmt.step)
            lines.extend(f"{pad}{line}" for line in step_pre)
            lines.append(f"{pad}long long {step_tmp} = {step_val};")
            lines.extend(f"{pad}{line}" for line in step_cl)
        else:
            lines.append(f"{pad}long long {step_tmp} = 1;")
        lines.extend(f"{pad}{line}" for line in start_cl)
        lines.extend(f"{pad}{line}" for line in end_cl)
        # 步长正负都支持：正步长用 <=，负步长用 >=
        cond = f"({step_tmp} >= 0 ? {var} <= {end_tmp} : {var} >= {end_tmp})"
        inner = self.block(stmt.body, indent + 1)
        lines.append(f"{pad}for (; {cond}; {var} += {step_tmp}) {{")
        lines.extend(inner)
        lines.append(f"{pad}}}")
        return lines

    def while_stmt(self, stmt: ast.WhileLoop, indent: int) -> list[str]:
        pad = "    " * indent
        # 条件在每次迭代都要重新求值，所以 prelude 放进循环体内、条件前
        prelude, condition, cleanup = self.truthy_with_prelude(stmt.condition)
        inner = self.block(stmt.body, indent + 1)
        lines = [self.source_comment(stmt.line_no, indent), f"{pad}while (1) {{"]
        lines.extend(f"{pad}    {line}" for line in prelude)
        lines.append(f"{pad}    if (!({condition})) {{")
        lines.extend(f"{pad}        {line}" for line in cleanup)
        lines.append(f"{pad}        break;")
        lines.append(f"{pad}    }}")
        lines.extend(f"{pad}    {line}" for line in cleanup)
        lines.extend(inner)
        lines.append(f"{pad}}}")
        return lines

    def if_stmt(self, stmt: ast.If, indent: int) -> list[str]:
        # 把 IF / ELSE IF / ELSE 展开成嵌套 if-else。
        # 这样每个 ELSE IF 条件的 prelude（临时变量）可以安全地放在它自己的 if 之前，
        # 而 C 的 `else if (...)` 语法没法在条件括号前插语句。
        branches: list[tuple[int, ast.Expr, list[ast.Stmt]]] = [(stmt.line_no, stmt.condition, stmt.body)]
        for branch in stmt.elifs:
            branches.append((branch.line_no, branch.condition, branch.body))
        return self._if_chain(branches, stmt.else_body, indent)

    def _if_chain(self, branches: list[tuple[int, ast.Expr, list[ast.Stmt]]], else_body: list[ast.Stmt], indent: int) -> list[str]:
        pad = "    " * indent
        line_no, condition_expr, body = branches[0]
        prelude, condition, cleanup = self.truthy_with_prelude(condition_expr)
        inner = self.block(body, indent + 1)
        lines = [
            self.source_comment(line_no, indent),
            *(f"{pad}{line}" for line in prelude),
            f"{pad}if ({condition}) {{",
            *inner,
            f"{pad}}}",
        ]
        rest = branches[1:]
        if rest or else_body:
            lines.append(f"{pad}else {{")
            if rest:
                lines.extend(self._if_chain(rest, else_body, indent + 1))
            else:
                lines.extend(self.block(else_body, indent + 1))
            lines.append(f"{pad}}}")
        # 条件 prelude 的清理放在整个 if-else 之后，无论走哪个分支都会执行
        lines.extend(f"{pad}{line}" for line in cleanup)
        return lines

    def truthy_with_prelude(self, expr: ast.Expr) -> tuple[list[str], str, list[str]]:
        prelude, value, cleanup = self.expr_with_prelude(expr)
        expr_type = self.type_of(expr)
        if is_string(expr_type):
            return prelude, f"({value} && {value}[0] != '\\0')", cleanup
        return prelude, value, cleanup

    def symbol_expr_with_prelude(self, expr: ast.Expr) -> tuple[list[str], str, list[str]]:
        self.prelude_stack.append([])
        self.cleanup_stack.append([])
        value = self.symbol_expr(expr)
        prelude = self.prelude_stack.pop()
        cleanup = self.cleanup_stack.pop()
        return prelude, value, cleanup

    def symbol_expr(self, expr: ast.Expr) -> str:
        if isinstance(expr, ast.NumberLiteral):
            return f"sa_symbol_const({c_string(expr.value)})"
        if isinstance(expr, ast.StringLiteral):
            return f"sa_symbol_const({c_string(expr.value)})"
        if isinstance(expr, ast.VarRef):
            if is_symbol(self.type_of(expr)):
                return f"sa_symbol_clone({self.c_value(expr.name)})"
            return f"sa_symbol_var({c_string(expr.name)})"
        if isinstance(expr, ast.Binary) and expr.op in {"+", "-", "*", "/", "**"}:
            op = "^" if expr.op == "**" else expr.op
            return f"sa_symbol_op('{op}', {self.symbol_expr(expr.left)}, {self.symbol_expr(expr.right)})"
        if isinstance(expr, ast.CallExpr) and self.is_math_function(expr.name, "POW"):
            return f"sa_symbol_op('^', {self.symbol_expr(expr.args[0])}, {self.symbol_expr(expr.args[1])})"
        if isinstance(expr, ast.CallExpr) and is_symbol(self.type_of(expr)):
            return f"sa_symbol_clone({self.expr(expr)})"
        raise SonCompileError("SYMBOL 只支持变量/数字/+ - * / ** 表达式和 DERIV/SIMPLIFY/SUBST", expr.line_no)

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
        if is_string(type_spec) or is_symbol(type_spec) or is_error(type_spec) or (type_spec.name == "ENTITY" and self.type_has_managed_resources(type_spec)):
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

    def type_has_managed_resources(self, type_spec: ast.TypeSpec, inside_entity: bool = False) -> bool:
        if is_string(type_spec) or is_symbol(type_spec) or is_error(type_spec):
            return not (inside_entity and is_symbol(type_spec))
        if type_spec.name != "ENTITY":
            return False
        entity = self.resolve_entity_def(type_spec)
        if entity is None:
            return False
        return any(self.type_has_managed_resources(field.type_spec, inside_entity=True) for field in entity.fields)

    def resolve_entity_def(self, type_spec: ast.TypeSpec) -> ast.EntityDef | None:
        if type_spec.name != "ENTITY":
            return None
        subtype = type_spec.subtype or ""
        split = split_module_member(subtype)
        if split:
            alias, member = split
            module = self.checked.external_modules.get(alias)
            return module.entities.get(member.lower()) if module is not None else None
        return self.checked.entities.get(subtype.lower())

    def entity_init_lines(self, target: str, type_spec: ast.TypeSpec, indent: int) -> list[str]:
        pad = "    " * indent
        entity = self.resolve_entity_def(type_spec)
        if entity is None:
            return []
        lines: list[str] = []
        for field in entity.fields:
            field_target = f"{target}.{field.name}"
            if is_string(field.type_spec):
                lines.append(f"{pad}{field_target} = sa_strdup(\"\");")
            elif is_error(field.type_spec):
                lines.append(f"{pad}{field_target} = (SaError){{0, \"ERR_NONE\", NULL, 0, NULL}};")
            elif field.type_spec.name == "ENTITY":
                lines.extend(self.entity_init_lines(field_target, field.type_spec, indent))
        return lines

    def entity_free_lines(self, target: str, type_spec: ast.TypeSpec, indent: int) -> list[str]:
        pad = "    " * indent
        entity = self.resolve_entity_def(type_spec)
        if entity is None:
            return []
        lines: list[str] = []
        for field in reversed(entity.fields):
            field_target = f"{target}.{field.name}"
            if is_string(field.type_spec):
                lines.append(f"{pad}free({field_target});")
            elif is_error(field.type_spec):
                lines.append(f"{pad}sa_error_clear(&{field_target});")
            elif field.type_spec.name == "ENTITY":
                lines.extend(self.entity_free_lines(field_target, field.type_spec, indent))
        return lines

    def entity_copy_lines(self, target: str, source: str, type_spec: ast.TypeSpec, indent: int) -> list[str]:
        pad = "    " * indent
        entity = self.resolve_entity_def(type_spec)
        if entity is None:
            return [f"{pad}{target} = {source};"]
        lines: list[str] = []
        for field in entity.fields:
            field_target = f"{target}.{field.name}"
            field_source = f"{source}.{field.name}"
            if is_string(field.type_spec):
                lines.append(f"{pad}sa_set_string(&{field_target}, {field_source});")
            elif is_error(field.type_spec):
                lines.append(f"{pad}sa_set_error(&{field_target}, &{field_source});")
            elif field.type_spec.name == "ENTITY":
                lines.extend(self.entity_copy_lines(field_target, field_source, field.type_spec, indent))
            else:
                lines.append(f"{pad}{field_target} = {field_source};")
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
        base = self.local_global_name(parts[0])
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
        }
        return [macros[feature] for feature in sorted(runtime_features_for_program(self.checked.program, self.checked.uses)) if feature in macros]

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


def generate_c(checked: CheckedProgram) -> str:
    generator = CGen(checked)
    return generator.generate()


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
