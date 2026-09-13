"""语句发射。"""
from __future__ import annotations

from ...analysis.semantics import Symbol
from ...analysis.typesys import is_cptr, is_error, is_handle, is_numeric, is_ptr, is_string, is_symbol
from ...core import ast
from ...core.errors import SonCompileError
from .base import CGenBase


class StmtsMixin(CGenBase):
    """语句发射：分发、异常穿透清理、赋值 / 声明 / IO、TRY-CATCH、控制流。"""

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
            if self.async_frame_stack:
                return self.async_return_stmt(stmt, indent)
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
        if isinstance(stmt, ast.AwaitStmt):
            return self.await_stmt(stmt, indent)
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
        in_frame = self.async_frame_field(stmt.name) is not None
        name = self.c_ident_path(stmt.name) if in_frame else self.c_ident(stmt.name)
        lines = [self.source_comment(stmt.line_no, indent)]
        if stmt.type_spec.array_size is not None:
            if in_frame:
                lines.append(f"{pad}memset(&{name}, 0, sizeof({name}));")
            else:
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
        if in_frame:
            lines.append(f"{pad}{name} = {init};")
        else:
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
        # 缓冲区名走 next_temp：固定叫 sa_input_buf 的话，同一个块里读两次输入就是
        # 两个同名的 char[4096]，C 编译器直接报 redeclaration。native 后端用 alloca
        # 天然唯一，只有这条路径会踩。
        buffer = f"{self.next_temp()}_input"
        lines = [self.source_comment(stmt.line_no, indent), *(f"{pad}{line}" for line in prelude)]
        lines.extend([f"{pad}printf(\"%s\", {prompt});", f"{pad}char {buffer}[4096];", f"{pad}sa_read_line({buffer}, sizeof({buffer}));"])
        if is_string(target.type_spec):
            lines.append(f"{pad}sa_set_string(&{name}, {buffer});")
        elif is_numeric(target.type_spec):
            cast = "(long long)" if target.type_spec.subtype == "LONG" else ""
            lines.append(f"{pad}{name} = {cast}sa_number({buffer});")
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

    def for_stmt(self, stmt: ast.ForLoop, indent: int) -> list[str]:
        pad = "    " * indent
        var = self.c_value(stmt.var)
        # 边界和步长只在进入循环前求值一次（BASIC 语义），存入临时变量
        start_pre, start_val, start_cl = self.expr_with_prelude(stmt.start)
        end_pre, end_val, end_cl = self.expr_with_prelude(stmt.end)
        # 在 async 协程帧里，循环体可能含 AWAIT——resume 靠 goto 跳进体中间的恢复点，会跳过
        # 栈上的上界/步长初始化，留下垃圾值使循环失控。所以这两个量得像循环变量一样提升进帧
        # （typedef 里已声明，这里只赋值）；帧外仍是普通栈临时量。
        in_frame = bool(self.async_frame_stack and stmt.line_no in self.async_frame_stack[-1].for_frame_lines)
        if in_frame:
            end_name, step_name = self._for_frame_names(stmt.line_no)
            end_tmp, step_tmp = f"f->{end_name}", f"f->{step_name}"
            decl = ""
        else:
            end_tmp = self.next_temp()
            step_tmp = self.next_temp()
            decl = "long long "
        lines = [self.source_comment(stmt.line_no, indent)]
        lines.extend(f"{pad}{line}" for line in start_pre)
        lines.extend(f"{pad}{line}" for line in end_pre)
        lines.append(f"{pad}{var} = {start_val};")
        lines.append(f"{pad}{decl}{end_tmp} = {end_val};")
        if stmt.step is not None:
            step_pre, step_val, step_cl = self.expr_with_prelude(stmt.step)
            lines.extend(f"{pad}{line}" for line in step_pre)
            lines.append(f"{pad}{decl}{step_tmp} = {step_val};")
            lines.extend(f"{pad}{line}" for line in step_cl)
        else:
            lines.append(f"{pad}{decl}{step_tmp} = 1;")
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
