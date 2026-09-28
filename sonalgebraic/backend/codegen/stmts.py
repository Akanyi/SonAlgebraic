"""语句发射。"""
from __future__ import annotations

from ...analysis.semantics import Symbol
from ...analysis.typesys import is_cptr, is_error, is_handle, is_numeric, is_promise, is_ptr, is_string, is_sub_ptr, is_sub_type, is_symbol, same_type_spec, sub_signature
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
        if not self.async_frame_stack and self._stmt_may_throw_user_call(stmt):
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
            # 经 callable / 函数引用调用：目标可能是会抛异常的 SA SUB，空引用本身也会抛 ERR_NULL_CALL
            if self.callable_var_type(expr.name) is not None:
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
        return self._wrap_throw_lines(self._emit_stmt(stmt, indent), indent, cleanup)

    def _wrap_throw_lines(self, body: list[str], indent: int, cleanup: list[str]) -> list[str]:
        pad = "    " * indent
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
            indirect = self.callable_call(stmt.name, stmt.args, stmt.line_no)
            if indirect is not None:
                prelude, call, cleanup, _ = indirect
                return [self.source_comment(stmt.line_no, indent), *(f"{pad}{line}" for line in prelude), f"{pad}{call};", *(f"{pad}{line}" for line in cleanup)]
            prelude, args, cleanup = self.call_args_with_prelude(stmt.name, stmt.args)
            return [self.source_comment(stmt.line_no, indent), *(f"{pad}{line}" for line in prelude), f"{pad}{self.call_c_name(stmt.name)}({', '.join(args)});", *(f"{pad}{line}" for line in cleanup)]
        if isinstance(stmt, ast.TryCatch):
            return self.try_catch_stmt(stmt, indent)
        if isinstance(stmt, ast.NewSub):
            return self.new_sub_stmt(stmt, indent)
        if isinstance(stmt, ast.CallRet):
            return self.callret_stmt(stmt, indent)
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
            value_lines, temp = self.return_value_lines(stmt.expr, indent)
            return [self.source_comment(stmt.line_no, indent), *value_lines, *self.active_local_resource_cleanup_lines(indent), f"{pad}return {temp};"]
        if isinstance(stmt, ast.End):
            return [self.source_comment(stmt.line_no, indent), f"{pad}goto sa_program_end;"]
        if isinstance(stmt, ast.Input):
            return self.input_stmt(stmt, indent)
        if isinstance(stmt, ast.Cls):
            return [self.source_comment(stmt.line_no, indent), f"{pad}sa_cls();"]
        if isinstance(stmt, ast.AwaitStmt):
            return self.await_stmt(stmt, indent)
        raise SonCompileError("未知语句类型", stmt.line_no)

    def new_sub_stmt(self, stmt: ast.NewSub, indent: int) -> list[str]:
        pad = "    " * indent
        signature = sub_signature(self.type_of(stmt.source))
        prelude, value, cleanup = self.expr_with_prelude(stmt.source)
        self.symbols[stmt.name.lower()] = Symbol(stmt.name, signature, True)
        name = self.c_ident(stmt.name)
        lines = [
            self.source_comment(stmt.line_no, indent),
            *(f"{pad}{line}" for line in prelude),
            f"{pad}SaCallable* {name} = sa_callable_new({value});",
            *(f"{pad}{line}" for line in cleanup),
        ]
        self.register_local_resource(name, signature)
        return lines

    def callret_stmt(self, stmt: ast.CallRet, indent: int) -> list[str]:
        """CALLRET f(args)：先调用、再清理本帧、最后带着 f 的结果返回。结果先落到外层声明的变量里，
        这样调用可以像普通 CALL 一样包进异常落地垫——RETURN 自己不在落地垫里，return 会跳过 sa_try_top--。"""
        pad = "    " * indent
        call = ast.CallExpr(stmt.line_no, stmt.name, stmt.args)
        return_type = self.current_sub_return_type()
        lines = [self.source_comment(stmt.line_no, indent)]
        result: str | None = None
        if return_type.name == "VOID":
            # 被调方若有返回值就地丢弃；托管类型的返回值已由 owned_call_result 登记了释放
            prelude, value, cleanup = self.expr_with_prelude(call)
            body = [*(f"{pad}{line}" for line in prelude), f"{pad}(void){value};", *(f"{pad}{line}" for line in cleanup)]
        else:
            result = self.next_temp()
            value_lines, temp = self.return_value_lines(call, indent)
            lines.append(f"{pad}{self.c_type(return_type)} {result};")
            body = [*value_lines, f"{pad}{result} = {temp};"]
        landing_cleanup = self.active_local_resource_cleanup_lines(indent + 1)
        lines.extend(self._wrap_throw_lines(body, indent, landing_cleanup) if landing_cleanup else body)
        lines.extend(self.active_local_resource_cleanup_lines(indent))
        lines.append(f"{pad}return;" if result is None else f"{pad}return {result};")
        return lines

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

    def return_value_lines(self, expr: ast.Expr, indent: int) -> tuple[list[str], str]:
        """RETURN 的表达式求到临时量，返回 (语句行, 临时量名)。求值在帧清理之前；表达式自己的临时量
        清理照常执行——以前是整个丢掉，RETURN LENGTH(F"…") 这种嵌套临时量会漏。"""
        pad = "    " * indent
        return_type = self.current_sub_return_type()
        if self.type_has_managed_resources(return_type) or is_promise(return_type):
            return self.owned_value_lines(expr, return_type, indent)
        prelude, value, cleanup = self.expr_with_prelude(expr)
        if is_handle(return_type) and isinstance(expr, ast.NullLiteral):
            value = "0"
        temp = self.next_temp()
        lines = [*(f"{pad}{line}" for line in prelude), f"{pad}{self.c_type(return_type)} {temp} = {value};", *(f"{pad}{line}" for line in cleanup)]
        return lines, temp

    def owned_value_lines(self, expr: ast.Expr, type_spec: ast.TypeSpec, indent: int) -> tuple[list[str], str]:
        """把托管类型的表达式求成调用方独占的值。交出去的不能是本帧稍后会 free 的指针，所以分三路：
        - 本帧拥有的变量（登记在清理表里，字段路径看根变量）：移出——指针 / 结构体交出去、源置空，
          随后的帧清理全是空操作。RETURN s 以前生成 `tmp = s; free(s); return tmp;`，就是这里修的。
        - 本语句刚算出来的堆临时量（F-string、CONCAT、DERIV、别的 SUB 的返回值）：接管，摘掉释放行。
        - 其余（字面量、全局、借来的、AS REF 参数、按值传入的 SYMBOL、数组元素）：深拷贝一份。
        PROMISE 是单消费者句柄，没有拷贝这回事：变量移出，其余原样交出。"""
        pad = "    " * indent
        temp = self.next_temp()
        c_type = self.c_type(type_spec)
        # 类型必须严格一致才能整体移出：SYMBOL SUB 里 RETURN s（s 是 STRING）是合法的，但那是拿 s 当变量名建树
        if isinstance(expr, ast.VarRef) and self.is_owned_var(expr.name) and same_type_spec(self.type_of(expr), type_spec):
            source = self.c_value(expr.name)
            return [f"{pad}{c_type} {temp} = {source};", *self.moved_source_reset_lines(source, type_spec, indent)], temp
        if is_symbol(type_spec):
            # symbol_expr 永远给一棵新树（变量克隆、调用结果接管），不需要再拷
            prelude, value, cleanup = self.symbol_expr_with_prelude(expr)
            return [*(f"{pad}{line}" for line in prelude), f"{pad}SaSymbol {temp} = {value};", *(f"{pad}{line}" for line in cleanup)], temp
        prelude, value, cleanup = self.expr_with_prelude(expr)
        lines = [f"{pad}{line}" for line in prelude]
        if is_promise(type_spec) or self.adopt_temp_cleanup(value, type_spec, cleanup):
            lines.append(f"{pad}{c_type} {temp} = {value};")
        elif is_sub_type(type_spec):
            lines.append(f"{pad}SaCallable* {temp} = sa_callable_retain({value});")
        elif is_string(type_spec):
            lines.append(f"{pad}char* {temp} = sa_strdup({value});")
        elif is_error(type_spec):
            lines.append(f"{pad}SaError {temp} = {{0, \"ERR_NONE\", NULL, 0, NULL}};")
            lines.append(f"{pad}sa_set_error(&{temp}, &{value});")
        else:
            # 零初始化的结构体上直接逐字段 copy：sa_set_string / sa_set_error 对 NULL 旧值 free 是空操作
            lines.append(f"{pad}{c_type} {temp} = {{0}};")
            lines.extend(self.entity_copy_lines(temp, value, type_spec, indent))
        lines.extend(f"{pad}{line}" for line in cleanup)
        return lines, temp

    def is_owned_var(self, name: str) -> bool:
        """VarRef（可带字段路径）的根变量是否由本帧持有。"""
        root = name.split(".", 1)[0]
        return root.lower() in self.symbols and self.is_frame_owned(self.c_value(root))

    def string_store_lines(self, target: str, value: str, cleanup: list[str], indent: int) -> list[str]:
        """STRING 赋值。value 若是本语句刚生成的堆串（F-string、CONCAT、SUB 返回值），直接接管指针，
        省掉 sa_set_string 那次 strdup；否则深拷贝。接管前 prelude 已全部跑完，所以 x = F"{x}!" 这类
        自引用是先读旧值再 free，没有 UAF。"""
        pad = "    " * indent
        if self.adopt_temp_cleanup(value, ast.TypeSpec("STRING"), cleanup):
            return [f"{pad}free({target});", f"{pad}{target} = {value};"]
        return [f"{pad}sa_set_string(&{target}, {value});"]

    def entity_store_lines(self, target: str, value: str, type_spec: ast.TypeSpec, cleanup: list[str], indent: int) -> list[str]:
        """含托管字段的 ENTITY 赋值：源是刚返回的临时结构体就整体接管（先释放目标各字段），否则逐字段深拷贝。"""
        pad = "    " * indent
        if self.adopt_temp_cleanup(value, type_spec, cleanup):
            return [*self.entity_free_lines(target, type_spec, indent), f"{pad}{target} = {value};"]
        return self.entity_copy_lines(target, value, type_spec, indent)

    def callable_store_lines(self, target: str, value: str, cleanup: list[str], indent: int) -> list[str]:
        """callable 赋值。刚返回的 callable 临时量直接接管那份计数，其余多持一份。"""
        pad = "    " * indent
        if self.adopt_temp_cleanup(value, ast.TypeSpec("SUB"), cleanup):
            return [f"{pad}sa_callable_release({target});", f"{pad}{target} = {value};"]
        return [f"{pad}sa_callable_set(&{target}, {value});"]

    def discarded_call_lines(self, call: str, return_type: ast.TypeSpec, indent: int) -> list[str]:
        """丢弃返回值的调用（TRY CALL f()）：托管类型的返回值归调用方所有，不接就得当场释放。"""
        pad = "    " * indent
        if not self.type_has_managed_resources(return_type):
            return [f"{pad}{call};"]
        temp = self.next_temp()
        return [f"{pad}{self.c_type(return_type)} {temp} = {call};", *self.local_resource_cleanup_lines([(temp, return_type)], indent)]

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
        if is_symbol(stmt.type_spec) or is_sub_type(stmt.type_spec):
            init = "NULL"
        if in_frame:
            if self._is_managed_type(stmt.type_spec):
                lines.append(f"{pad}f->sa_borrowed_{self.c_ident(stmt.name)} = 0;")
            if init.startswith("{"):
                init = f"({self.c_type(stmt.type_spec)}){init}"
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
                lines.extend(self.string_store_lines(name, value, cleanup, indent))
            elif is_sub_type(stmt.type_spec):
                lines.extend(self.callable_store_lines(name, value, cleanup, indent))
            elif stmt.type_spec.name == "ENTITY" and self.type_has_managed_resources(stmt.type_spec):
                lines.extend(self.entity_store_lines(name, value, stmt.type_spec, cleanup, indent))
            else:
                if is_handle(stmt.type_spec) and isinstance(stmt.expr, ast.NullLiteral):
                    value = "0"
                lines.append(f"{pad}{name} = {value};")
            lines.extend(f"{pad}{line}" for line in cleanup)
        return lines

    def ownership_assign_stmt(self, stmt: ast.Assign, indent: int) -> list[str]:
        """`a f= b` / `a m= b`：两侧都是同型托管变量（语义层已保证），指针 / 结构体直接赋过去，
        不走深拷贝。区别只在事后谁负责释放：
        - move：源置空。源保持登记，块尾 free(NULL) / 清零 ERROR / 逐字段 free(NULL) 都安全，
          所以移动对控制流路径不敏感，IF 里移走也不用改登记表。
        - borrow：目标从登记里摘掉。登记按语句顺序处理，借用之前生成的 RETURN / landing pad
          仍会 free 目标（那时它还持有自己的初始值），之后的不会——与运行时路径一致。"""
        pad = "    " * indent
        assert isinstance(stmt.target, ast.VarRef) and isinstance(stmt.expr, ast.VarRef)
        target_type = self.type_of(stmt.target)
        target_c = self.c_value(stmt.target.name)
        source_c = self.c_value(stmt.expr.name)
        lines = [self.source_comment(stmt.line_no, indent)]
        # 目标旧值先释放，复用块尾清理的发射逻辑（按类型 free / sa_symbol_free / sa_error_clear / 逐字段）
        lines.extend(self.local_resource_cleanup_lines([(target_c, target_type)], indent))
        lines.append(f"{pad}{target_c} = {source_c};")
        if stmt.mode == "move":
            lines.extend(self.moved_source_reset_lines(source_c, target_type, indent))
        else:
            self.unregister_local_resource(target_c)
            if self.async_frame_stack and target_c.startswith("f->"):
                lines.append(f"{pad}f->sa_borrowed_{self.c_ident(stmt.target.name)} = 1;")
        return lines

    def assign_stmt(self, stmt: ast.Assign, indent: int) -> list[str]:
        pad = "    " * indent
        # 函数引用不持有任何资源，f= 就是普通赋值（m= 语义层已拦下），不走所有权转移
        fn_ref = isinstance(stmt.expr, ast.SubRef) or (isinstance(stmt.target, ast.VarRef) and is_sub_ptr(self.type_of(stmt.target)))
        if stmt.mode != "copy" and not fn_ref:
            return self.ownership_assign_stmt(stmt, indent)
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
            target_type = self.type_of(stmt.target)
            if target_type.name == "ENTITY" and self.type_has_managed_resources(target_type):
                target_prelude, target_expr, target_cleanup = self.expr_with_prelude(stmt.target.expr)
                lines.extend(f"{pad}{line}" for line in target_prelude)
                # 逐字段复制会反复引用目标，先固定地址，避免指针调用被重复求值。
                target_ptr = self.next_temp()
                lines.append(f"{pad}{self.c_type(target_type)}* {target_ptr} = {target_expr};")
                lines.extend(self.entity_store_lines(f"(*{target_ptr})", value, target_type, cleanup, indent))
                lines.extend(f"{pad}{line}" for line in target_cleanup)
                lines.extend(f"{pad}{line}" for line in cleanup)
                return lines
            target_expr = self.expr(stmt.target.expr)
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
            lines.extend(self.string_store_lines(target_name, value, cleanup, indent))
        elif is_sub_type(target_type):
            lines.extend(self.callable_store_lines(self.c_value(name), value, cleanup, indent))
        elif target_type.name == "ENTITY" and self.type_has_managed_resources(target_type):
            lines.extend(self.entity_store_lines(self.c_value(name), value, target_type, cleanup, indent))
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
        call = f"{self.call_c_name(stmt.call_name)}({', '.join(args)})"
        lines.extend(self.discarded_call_lines(call, self.resolve_called_sub(stmt.call_name).return_type, indent + 1))
        lines.append(f"{pad}    sa_try_top--;")
        lines.append(f"{pad}}} else {{")
        lines.append(f"{pad}    sa_try_top--;")
        lines.append(f"{pad}    sa_set_error(&{self.c_value(stmt.traceback_var)}, &sa_current_error);")

        for index, branch in enumerate(stmt.catches):
            prefix = "if" if index == 0 else "else if"
            condition = "1" if branch.error_type == "ERR_ANY" else f"strcmp(sa_current_error.type, \"{branch.error_type}\") == 0"
            lines.append(f"{pad}    {prefix} ({condition}) {{")
            lines.append(self.source_comment(branch.line_no, indent + 2))
            alias_c = self.c_ident_path(branch.alias)
            if self.async_frame_stack:
                pass
            elif self.current_sub_has_gosub() or self.current_sub_has_goto():
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
            lines.append(f"{pad}        sa_error_clear(&{alias_c});")
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
