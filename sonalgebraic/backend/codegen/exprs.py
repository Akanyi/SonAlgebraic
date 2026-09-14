"""表达式发射。带 prelude / cleanup 的临时量协议在这里进出。"""
from __future__ import annotations

from ...analysis.typesys import is_cptr, is_error, is_handle, is_ptr, is_string, is_symbol, resolve_builtin_const
from ...core import ast
from ...core.errors import SonCompileError
from .base import c_number, c_string, CGenBase


class ExprsMixin(CGenBase):
    """表达式发射：字面量、变量、运算、调用分发、f-string、SYMBOL 树构建。"""

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
        if isinstance(expr, ast.AwaitExpr):
            return self.await_expr(expr)
        if isinstance(expr, ast.SyncExpr):
            return self.sync_expr(expr)
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
            if sub.is_async:
                return f"{self.call_c_name(expr.name)}_start({', '.join(args)})"
            return self.owned_call_result(f"{self.call_c_name(expr.name)}({', '.join(args)})", sub.return_type)
        raise SonCompileError(f"未知内置函数: {expr.name}", expr.line_no)

    def owned_call_result(self, call: str, return_type: ast.TypeSpec) -> str:
        """SUB 返回的 STRING / SYMBOL / 含托管字段的 ENTITY 是调用方独占的堆资源（被调方 RETURN 时已
        移出或拷了一份）：落到临时量并登记释放，语句结束时 free。要接管的消费者（STRING / ENTITY 赋值、
        RETURN、SYMBOL 建树）用 adopt_temp_cleanup 把释放行摘走就行，不必再拷。裸着返回调用文本的话，
        PRINT wrap() 这类用法会把返回值直接漏掉，entity_copy_lines 逐字段展开还会把调用重复执行好几遍。"""
        if not self.type_has_managed_resources(return_type):
            return call
        temp = self.next_temp()
        self.add_prelude(f"{self.c_type(return_type)} {temp} = {call};")
        for line in self.local_resource_cleanup_lines([(temp, return_type)], 0):
            self.add_cleanup(line)
        return temp

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
            value = self.expr(expr)
            # DERIV 等内置和用户 SUB 的返回值都是登记了释放的临时树：直接接管，
            # 不必「克隆一份、原树等语句尾 free」
            if self.cleanup_stack and self.adopt_temp_cleanup(value, ast.TypeSpec("SYMBOL"), self.cleanup_stack[-1]):
                return value
            return f"sa_symbol_clone({value})"
        raise SonCompileError("SYMBOL 只支持变量/数字/+ - * / ** 表达式和 DERIV/SIMPLIFY/SUBST", expr.line_no)
