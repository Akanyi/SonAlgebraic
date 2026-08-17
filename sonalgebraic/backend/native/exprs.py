from __future__ import annotations

from ...analysis.typesys import classify_number_literal, is_bool, is_error, is_handle, is_null, is_numeric, is_ptr, is_string, is_symbol, resolve_builtin_const
from ...core import ast
from ...core.errors import SonCompileError
from .base import LLVMValue, NativeGenBase, llvm_double_literal, llvm_int_literal


class ExprsMixin(NativeGenBase):
    """表达式求值：左值求址、运算、F-string、SYMBOL 建树。"""

    def lvalue_ptr(self, expr: ast.Expr) -> str:
        if isinstance(expr, ast.VarRef):
            return self.varref_ptr(expr.name, expr.line_no)[0]
        if isinstance(expr, ast.Deref):
            ptr_value = self.expr(expr.expr)
            return ptr_value.value
        if isinstance(expr, ast.Index):
            return self.index_ptr(expr)
        raise SonCompileError("native 后端赋值目标必须是变量、数组元素或 ^指针", expr.line_no)

    def varref_ptr(self, name: str, line_no: int) -> tuple[str, ast.TypeSpec]:
        parts = name.split(".")
        slot = self.slot(parts[0], line_no)
        ptr = slot.ptr
        current_type = slot.type_spec
        for field_name in parts[1:]:
            index, field = self.entity_field(current_type, field_name, line_no)
            next_ptr = self.next_temp()
            self.emit(f"  {next_ptr} = getelementptr inbounds {self.llvm_type(current_type)}, ptr {ptr}, i64 0, i32 {index}")
            ptr = next_ptr
            current_type = field.type_spec
        return ptr, current_type

    def index_ptr(self, expr: ast.Index) -> str:
        if not isinstance(expr.base, ast.VarRef):
            raise SonCompileError("native 后端暂只支持变量数组下标", expr.line_no)
        base_ptr, base_type = self.varref_ptr(expr.base.name, expr.line_no)
        if base_type.array_size is None:
            raise SonCompileError("native 后端下标访问只能用于数组", expr.line_no)
        index = self.cast_to_i64(self.expr(expr.index))
        ptr = self.next_temp()
        self.emit(f"  {ptr} = getelementptr inbounds {self.llvm_type(base_type)}, ptr {base_ptr}, i64 0, i64 {index.value}")
        return ptr

    def fstring_value(self, expr: ast.FString) -> LLVMValue:
        # F-string 作为值（赋值/传参）：用 SaStringBuilder 拼接。布局 {ptr,i64,i64}=24 字节。
        # 结果是堆串，登记临时清理。
        self.use_runtime("sa_sb_init")
        self.use_runtime("sa_sb_append")
        self.use_runtime("sa_sb_take")
        builder = self.alloca("{ ptr, i64, i64 }")
        self.emit(f"  call void @sa_sb_init(ptr {builder})")
        for part in expr.parts:
            if isinstance(part, str):
                if part:
                    self.emit(f"  call void @sa_sb_append(ptr {builder}, ptr {self.string_ptr(part)})")
                continue
            self.append_to_builder(builder, part)
        result = self.next_temp()
        self.emit(f"  {result} = call ptr @sa_sb_take(ptr {builder})")
        self.use_runtime("free")
        self.add_temp_cleanup(f"  call void @free(ptr {result})")
        return LLVMValue("ptr", result, ast.TypeSpec("STRING"))

    def append_to_builder(self, builder: str, part: ast.Expr) -> None:
        value = self.expr(part)
        if value.type_name == "ptr" and self.is_string_type(value.type_spec):
            self.emit(f"  call void @sa_sb_append(ptr {builder}, ptr {value.value})")
            return
        if value.type_name == "ptr" and is_error(value.type_spec or ast.TypeSpec("VOID")):
            self.emit(f"  call void @sa_sb_append(ptr {builder}, ptr {self.error_message_ptr(value.value)})")
            return
        if value.type_name == "ptr" and is_symbol(value.type_spec or ast.TypeSpec("VOID")):
            piece = self.next_temp()
            self.use_runtime("sa_symbol_to_string")
            self.emit(f"  {piece} = call ptr @sa_symbol_to_string(ptr {value.value})")
            self.emit(f"  call void @sa_sb_append(ptr {builder}, ptr {piece})")
            self.use_runtime("free")
            self.emit(f"  call void @free(ptr {piece})")
            return
        if value.type_name == "ptr":
            piece = self.next_temp()
            self.use_runtime("sa_to_string_pointer")
            self.emit(f"  {piece} = call ptr @sa_to_string_pointer(ptr {value.value})")
            self.emit(f"  call void @sa_sb_append(ptr {builder}, ptr {piece})")
            self.use_runtime("free")
            self.emit(f"  call void @free(ptr {piece})")
            return
        # 数值/布尔先转字符串再 append，临时串当场释放
        piece = self.next_temp()
        if value.type_name == "double":
            self.use_runtime("sa_to_string_double")
            self.emit(f"  {piece} = call ptr @sa_to_string_double(double {value.value})")
        else:
            wide = self.cast_to_i64(value)
            self.use_runtime("sa_to_string_long")
            self.emit(f"  {piece} = call ptr @sa_to_string_long(i64 {wide.value})")
        self.emit(f"  call void @sa_sb_append(ptr {builder}, ptr {piece})")
        self.use_runtime("free")
        self.emit(f"  call void @free(ptr {piece})")

    def c_call_args(self, c_func: ast.CFunctionDecl, args: list[ast.Expr]) -> list[str]:
        values: list[str] = []
        for param, arg in zip(c_func.params, args):
            if param.by_ref:
                if not isinstance(arg, ast.VarRef):
                    raise SonCompileError(f"REF 参数 {param.name} 必须传入变量", arg.line_no)
                if is_bool(param.type_spec):
                    raise SonCompileError("native 后端暂不支持跨 C ABI 的 BOOL AS REF 参数", arg.line_no)
                values.append(f"ptr {self.lvalue_ptr(arg)}")
                continue
            arg_value = self.expr(arg)
            value = self.c_cast_arg(arg_value, param.type_spec)
            if is_bool(param.type_spec):
                extended = self.next_temp()
                self.emit(f"  {extended} = zext i1 {value.value} to i32")
                values.append(f"i32 {extended}")
            else:
                values.append(f"{self.c_abi_type(param.type_spec)} {value.value}")
        return values

    def wrap_call_expr_with_throw_cleanup(self, name: str, sub: ast.Subroutine, args: list[str], raw_name: bool = False, c_abi: bool = False) -> LLVMValue:
        ret_type = self.c_abi_type(sub.return_type) if c_abi else self.llvm_type(sub.return_type)
        result_ptr = self.alloca(ret_type)
        env = self.next_temp()
        frame = self.next_temp()
        sj = self.next_temp()
        is_try = self.next_temp()
        try_label = self.unique_label("expr_call_try")
        catch_label = self.unique_label("expr_call_cleanup")
        end_label = self.unique_label("expr_call_end")
        self.use_runtime("sa_try_push_env")
        self.use_runtime("llvm.frameaddress")
        self.use_runtime("_setjmp")
        self.emit(f"  {env} = call ptr @sa_try_push_env()")
        self.emit(f"  {frame} = call ptr @llvm.frameaddress.p0(i32 0)")
        self.emit(f"  {sj} = call i32 @_setjmp(ptr {env}, ptr {frame})")
        self.emit(f"  {is_try} = icmp eq i32 {sj}, 0")
        self.emit(f"  br i1 {is_try}, label %{try_label}, label %{catch_label}")
        self.terminated = True

        self.emit(f"{try_label}:")
        self.terminated = False
        call_value = self.next_temp()
        callee = name if raw_name else self.sub_name(name)
        self.emit(f"  {call_value} = call {ret_type} @{callee}({', '.join(args)})")
        self.emit(f"  store {ret_type} {call_value}, ptr {result_ptr}")
        self.use_runtime("sa_try_pop")
        self.emit("  call void @sa_try_pop()")
        self.emit(f"  br label %{end_label}")
        self.terminated = True

        self.emit(f"{catch_label}:")
        self.terminated = False
        self.use_runtime("sa_try_pop")
        self.emit("  call void @sa_try_pop()")
        self.emit_active_cleanup()
        self.use_runtime("sa_throw_dispatch")
        self.emit("  call void @sa_throw_dispatch()")
        self.emit("  unreachable")
        self.terminated = True

        self.emit(f"{end_label}:")
        self.terminated = False
        result = self.next_temp()
        self.emit(f"  {result} = load {ret_type}, ptr {result_ptr}")
        if c_abi and is_bool(sub.return_type):
            return self.i32_status(result)
        return LLVMValue(ret_type, result, sub.return_type)

    def symbol_expr(self, expr: ast.Expr) -> LLVMValue:
        if isinstance(expr, ast.NumberLiteral):
            self.use_runtime("sa_symbol_const")
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @sa_symbol_const(ptr {self.string_ptr(expr.value)})")
            return LLVMValue("ptr", temp, ast.TypeSpec("SYMBOL"))
        if isinstance(expr, ast.StringLiteral):
            self.use_runtime("sa_symbol_const")
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @sa_symbol_const(ptr {self.string_ptr(expr.value)})")
            return LLVMValue("ptr", temp, ast.TypeSpec("SYMBOL"))
        if isinstance(expr, ast.VarRef):
            if is_symbol(self.type_of_expr(expr)):
                self.use_runtime("sa_symbol_clone")
                value = self.expr(expr)
                temp = self.next_temp()
                self.emit(f"  {temp} = call ptr @sa_symbol_clone(ptr {value.value})")
                return LLVMValue("ptr", temp, ast.TypeSpec("SYMBOL"))
            self.use_runtime("sa_symbol_var")
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @sa_symbol_var(ptr {self.string_ptr(expr.name)})")
            return LLVMValue("ptr", temp, ast.TypeSpec("SYMBOL"))
        if isinstance(expr, ast.Binary) and expr.op in {"+", "-", "*", "/", "**"}:
            left = self.symbol_expr(expr.left)
            right = self.symbol_expr(expr.right)
            self.use_runtime("sa_symbol_op")
            temp = self.next_temp()
            op = "^" if expr.op == "**" else expr.op
            self.emit(f"  {temp} = call ptr @sa_symbol_op(i8 {ord(op)}, ptr {left.value}, ptr {right.value})")
            return LLVMValue("ptr", temp, ast.TypeSpec("SYMBOL"))
        if isinstance(expr, ast.CallExpr) and self.is_math_function(expr.name, "POW"):
            left = self.symbol_expr(expr.args[0])
            right = self.symbol_expr(expr.args[1])
            self.use_runtime("sa_symbol_op")
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @sa_symbol_op(i8 {ord('^')}, ptr {left.value}, ptr {right.value})")
            return LLVMValue("ptr", temp, ast.TypeSpec("SYMBOL"))
        if isinstance(expr, ast.CallExpr) and is_symbol(self.type_of_expr(expr)):
            value = self.expr(expr)
            self.use_runtime("sa_symbol_clone")
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @sa_symbol_clone(ptr {value.value})")
            return LLVMValue("ptr", temp, ast.TypeSpec("SYMBOL"))
        raise SonCompileError("SYMBOL 只支持变量/数字/+ - * / ** 表达式和 DERIV/SIMPLIFY/SUBST", expr.line_no)

    def expr(self, expr: ast.Expr) -> LLVMValue:
        if is_symbol(self.type_of_expr(expr)) and not isinstance(expr, ast.VarRef | ast.CallExpr):
            return self.symbol_expr(expr)
        if isinstance(expr, ast.NumberLiteral):
            type_spec = classify_number_literal(expr.value)
            if type_spec.subtype == "LONG":
                return LLVMValue("i64", llvm_int_literal(expr.value), type_spec)
            return LLVMValue("double", llvm_double_literal(expr.value), type_spec)
        if isinstance(expr, ast.NullLiteral):
            return LLVMValue("ptr", "null", ast.TypeSpec("NULLT"))
        if isinstance(expr, ast.BoolLiteral):
            return LLVMValue("i1", "1" if expr.value else "0", ast.TypeSpec("BOOL"))
        if isinstance(expr, ast.StringLiteral):
            return LLVMValue("ptr", self.string_ptr(expr.value), ast.TypeSpec("STRING"))
        if isinstance(expr, ast.FString):
            return self.fstring_value(expr)
        if isinstance(expr, ast.VarRef):
            key = expr.name.lower()
            if key in self.checked.enum_members:
                return LLVMValue("i64", str(self.checked.enum_members[key]), ast.TypeSpec("NUM", "LONG"))
            builtin = resolve_builtin_const(expr.name, self.checked.uses)
            if builtin is not None:
                return self.builtin_const_value(builtin[0], builtin[1], expr.line_no)
            external_const = self.resolve_external_const(expr.name)
            if external_const is not None:
                symbol, decl = external_const
                temp = self.next_temp()
                abi_type = self.c_abi_type(decl.type_spec)
                self.emit(f"  {temp} = load {abi_type}, ptr @{symbol}")
                if is_bool(decl.type_spec):
                    return self.i32_status(temp)
                return LLVMValue(abi_type, temp, decl.type_spec)
            ptr, type_spec = self.varref_ptr(expr.name, expr.line_no)
            if type_spec.array_size is not None:
                raise SonCompileError("native 后端数组必须通过下标访问", expr.line_no)
            if is_error(type_spec):
                return LLVMValue("ptr", ptr, type_spec)
            temp = self.next_temp()
            self.emit(f"  {temp} = load {self.llvm_type(type_spec)}, ptr {ptr}")
            return LLVMValue(self.llvm_type(type_spec), temp, type_spec)
        if isinstance(expr, ast.Deref):
            result_type = self.type_of_expr(expr)
            ptr_value = self.expr(expr.expr)
            temp = self.next_temp()
            self.emit(f"  {temp} = load {self.llvm_type(result_type)}, ptr {ptr_value.value}")
            return LLVMValue(self.llvm_type(result_type), temp, result_type)
        if isinstance(expr, ast.AddressOf):
            if not isinstance(expr.expr, ast.VarRef):
                raise SonCompileError("native 后端 @ 只能用于变量", expr.line_no)
            ptr, type_spec = self.varref_ptr(expr.expr.name, expr.line_no)
            return LLVMValue("ptr", ptr, ast.TypeSpec("PTR", inner=type_spec))
        if isinstance(expr, ast.Cast):
            return self.cast_value(self.expr(expr.expr), expr.type_spec)
        if isinstance(expr, ast.Index):
            result_type = self.type_of_expr(expr)
            ptr = self.index_ptr(expr)
            temp = self.next_temp()
            self.emit(f"  {temp} = load {self.llvm_type(result_type)}, ptr {ptr}")
            return LLVMValue(self.llvm_type(result_type), temp, result_type)
        if isinstance(expr, ast.Unary):
            return self.unary_expr(expr)
        if isinstance(expr, ast.Binary):
            return self.binary_expr(expr)
        if isinstance(expr, ast.CallExpr):
            builtin = self.builtin_call(expr)
            if builtin is not None:
                return builtin
            c_func = self.resolve_c_func(expr.name)
            if c_func is not None:
                value = self.c_call_stmt(c_func, expr.args)
                if value is None:
                    raise SonCompileError(f"native 后端 VOID C 函数不能作为表达式: {expr.name}", expr.line_no)
                return value
            sub = self.checked.subs.get(expr.name.lower())
            external = None
            if sub is None:
                external = self.resolve_external_sub(expr.name)
                if external is None:
                    raise SonCompileError(f"native 后端暂不支持表达式调用: {expr.name}", expr.line_no)
                external_name, sub = external
            else:
                external_name = self.sub_name(expr.name)
            is_external = external is not None
            args = self.call_args(sub, expr.args, c_abi=is_external)
            if self.has_active_resources():
                return self.wrap_call_expr_with_throw_cleanup(external_name, sub, args, raw_name=True, c_abi=is_external)
            temp = self.next_temp()
            ret_type = self.c_abi_type(sub.return_type) if is_external else self.llvm_type(sub.return_type)
            self.emit(f"  {temp} = call {ret_type} @{external_name}({', '.join(args)})")
            if is_external and is_bool(sub.return_type):
                return self.i32_status(temp)
            return LLVMValue(ret_type, temp, sub.return_type)
        raise SonCompileError(f"native 后端暂不支持表达式: {type(expr).__name__}", expr.line_no)

    def unary_expr(self, expr: ast.Unary) -> LLVMValue:
        value = self.expr(expr.expr)
        if expr.op == "+":
            return value
        if expr.op == "-":
            temp = self.next_temp()
            if value.type_name == "double":
                self.emit(f"  {temp} = fsub double -0.0, {value.value}")
                return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
            self.emit(f"  {temp} = sub i64 0, {self.cast_value(value, ast.TypeSpec('NUM', 'LONG')).value}")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if expr.op == "NOT":
            truth = self.truthy(value)
            temp = self.next_temp()
            self.emit(f"  {temp} = xor i1 {truth.value}, true")
            return LLVMValue("i1", temp, ast.TypeSpec("BOOL"))
        raise SonCompileError(f"native 后端暂不支持一元运算: {expr.op}", expr.line_no)

    def short_circuit_expr(self, expr: ast.Binary, op: str) -> LLVMValue:
        """AND / OR 的短路求值。

        C 后端把它们映射成 `&&` / `||`，所以右侧只在需要时求值。native 侧如果
        无条件先算两边，`IF d <> 0 AND total / d > 1` 这类守卫就会照样执行除法，
        右侧带副作用（读文件、发请求）的条件也会多跑一次。
        """
        is_and = op == "AND"
        test_label = self.unique_label("sc_test")
        rhs_label = self.unique_label("sc_rhs")
        rhs_done_label = self.unique_label("sc_rhs_done")
        end_label = self.unique_label("sc_end")

        left = self.expr(expr.left)
        lhs = self.truthy(left)
        # 左右两侧自身可能已经开了新基本块，所以各用一个显式块收口，
        # 保证 phi 的前驱标签是确定的。
        self.emit(f"  br label %{test_label}")
        self.emit(f"{test_label}:")
        if is_and:
            self.emit(f"  br i1 {lhs.value}, label %{rhs_label}, label %{end_label}")
        else:
            self.emit(f"  br i1 {lhs.value}, label %{end_label}, label %{rhs_label}")

        self.emit(f"{rhs_label}:")
        right = self.expr(expr.right)
        rhs = self.truthy(right)
        self.emit(f"  br label %{rhs_done_label}")
        self.emit(f"{rhs_done_label}:")
        self.emit(f"  br label %{end_label}")

        self.emit(f"{end_label}:")
        temp = self.next_temp()
        short_value = "false" if is_and else "true"
        self.emit(f"  {temp} = phi i1 [ {short_value}, %{test_label} ], [ {rhs.value}, %{rhs_done_label} ]")
        return LLVMValue("i1", temp, ast.TypeSpec("BOOL"))

    def binary_expr(self, expr: ast.Binary) -> LLVMValue:
        op = expr.op
        if op in {"AND", "OR"}:
            return self.short_circuit_expr(expr, op)
        left = self.expr(expr.left)
        right = self.expr(expr.right)
        if op in {"=", "==", "!=", "<>", "<", "<=", ">", ">="}:
            return self.compare_expr(op, left, right)
        if op == "**":
            self.use_runtime("pow")
            lhs = self.cast_to_double(left)
            rhs = self.cast_to_double(right)
            temp = self.next_temp()
            self.emit(f"  {temp} = call double @pow(double {lhs.value}, double {rhs.value})")
            return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
        if is_ptr(left.type_spec or ast.TypeSpec("VOID")) and right.type_name in {"i64", "i1", "double"} and op in {"+", "-"}:
            offset = self.cast_to_i64(right)
            if op == "-":
                neg = self.next_temp()
                self.emit(f"  {neg} = sub i64 0, {offset.value}")
                offset = LLVMValue("i64", neg, ast.TypeSpec("NUM", "LONG"))
            elem_ty = self.llvm_type(left.type_spec.inner or ast.TypeSpec("NUM", "LONG"))
            temp = self.next_temp()
            self.emit(f"  {temp} = getelementptr inbounds {elem_ty}, ptr {left.value}, i64 {offset.value}")
            return LLVMValue("ptr", temp, left.type_spec)
        if left.type_name == "double" or right.type_name == "double":
            lhs = self.cast_to_double(left)
            rhs = self.cast_to_double(right)
            temp = self.next_temp()
            instr = {"+": "fadd", "-": "fsub", "*": "fmul", "/": "fdiv"}.get(op)
            if instr is None:
                raise SonCompileError(f"native 后端暂不支持运算: {op}", expr.line_no)
            self.emit(f"  {temp} = {instr} double {lhs.value}, {rhs.value}")
            return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
        lhs = self.cast_to_i64(left)
        rhs = self.cast_to_i64(right)
        temp = self.next_temp()
        instr = {"+": "add", "-": "sub", "*": "mul", "/": "sdiv", "BAND": "and", "BOR": "or", "BXOR": "xor", "SHL": "shl", "SHR": "ashr"}.get(op)
        if instr is None:
            raise SonCompileError(f"native 后端暂不支持运算: {op}", expr.line_no)
        self.emit(f"  {temp} = {instr} i64 {lhs.value}, {rhs.value}")
        return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))

    def compare_expr(self, op: str, left: LLVMValue, right: LLVMValue) -> LLVMValue:
        temp = self.next_temp()
        if is_handle(left.type_spec or ast.TypeSpec("VOID")) or is_handle(right.type_spec or ast.TypeSpec("VOID")):
            if op not in {"=", "==", "!=", "<>"}:
                raise SonCompileError(f"native 后端 HANDLE 暂只支持等值比较: {op}")
            left_value = "0" if is_null(left.type_spec or ast.TypeSpec("VOID")) else left.value
            right_value = "0" if is_null(right.type_spec or ast.TypeSpec("VOID")) else right.value
            pred = "eq" if op in {"=", "=="} else "ne"
            self.emit(f"  {temp} = icmp {pred} i64 {left_value}, {right_value}")
            return LLVMValue("i1", temp, ast.TypeSpec("BOOL"))
        # 字符串比较用 strcmp；真指针/CPTR/NULL 走 ptr icmp。
        if self.is_string_type(left.type_spec) and self.is_string_type(right.type_spec):
            if op not in {"=", "==", "!=", "<>"}:
                raise SonCompileError(f"native 后端字符串不支持比较运算: {op}")
            self.use_runtime("strcmp")
            cmp = self.next_temp()
            self.emit(f"  {cmp} = call i32 @strcmp(ptr {left.value}, ptr {right.value})")
            pred = "eq" if op in {"=", "=="} else "ne"
            self.emit(f"  {temp} = icmp {pred} i32 {cmp}, 0")
            return LLVMValue("i1", temp, ast.TypeSpec("BOOL"))
        if self.is_pointer_like_type(left.type_spec) or self.is_pointer_like_type(right.type_spec):
            if op not in {"=", "==", "!=", "<>"}:
                raise SonCompileError(f"native 后端指针暂只支持等值比较: {op}")
            pred = "eq" if op in {"=", "=="} else "ne"
            self.emit(f"  {temp} = icmp {pred} ptr {left.value}, {right.value}")
            return LLVMValue("i1", temp, ast.TypeSpec("BOOL"))
        if left.type_name == "double" or right.type_name == "double":
            lhs = self.cast_to_double(left)
            rhs = self.cast_to_double(right)
            pred = {"=": "oeq", "==": "oeq", "!=": "one", "<>": "one", "<": "olt", "<=": "ole", ">": "ogt", ">=": "oge"}[op]
            self.emit(f"  {temp} = fcmp {pred} double {lhs.value}, {rhs.value}")
            return LLVMValue("i1", temp, ast.TypeSpec("BOOL"))
        lhs = self.cast_to_i64(left)
        rhs = self.cast_to_i64(right)
        pred = {"=": "eq", "==": "eq", "!=": "ne", "<>": "ne", "<": "slt", "<=": "sle", ">": "sgt", ">=": "sge"}[op]
        self.emit(f"  {temp} = icmp {pred} i64 {lhs.value}, {rhs.value}")
        return LLVMValue("i1", temp, ast.TypeSpec("BOOL"))

    def call_args(self, sub: ast.Subroutine, args: list[ast.Expr], c_abi: bool = False) -> list[str]:
        values: list[str] = []
        for param, arg in zip(sub.params, args):
            if param.by_ref:
                if not isinstance(arg, ast.VarRef):
                    raise SonCompileError(f"REF 参数 {param.name} 必须传入变量", arg.line_no)
                if c_abi and is_bool(param.type_spec):
                    raise SonCompileError("native 后端暂不支持跨模块的 BOOL AS REF 参数", arg.line_no)
                values.append(f"ptr {self.lvalue_ptr(arg)}")
            else:
                value = self.cast_value(self.expr(arg), param.type_spec)
                if c_abi and is_bool(param.type_spec):
                    extended = self.next_temp()
                    self.emit(f"  {extended} = zext i1 {value.value} to i32")
                    values.append(f"i32 {extended}")
                else:
                    value_type = self.c_abi_type(param.type_spec) if c_abi else self.llvm_type(param.type_spec)
                    values.append(f"{value_type} {value.value}")
        return values

    def builtin_const_value(self, type_spec: ast.TypeSpec, literal: str, line_no: int) -> LLVMValue:
        if is_string(type_spec):
            if not (literal.startswith('"') and literal.endswith('"')):
                raise SonCompileError("native 后端无法解析内置字符串常量", line_no)
            value = bytes(literal[1:-1], "utf-8").decode("unicode_escape")
            return LLVMValue("ptr", self.string_ptr(value), type_spec)
        if is_numeric(type_spec) and type_spec.subtype == "DOUBLE":
            return LLVMValue("double", llvm_double_literal(literal), type_spec)
        if is_numeric(type_spec):
            cleaned = literal.replace("LL", "").replace("ll", "")
            if cleaned == "(-9223372036854775807 - 1)":
                return LLVMValue("i64", "-9223372036854775808", type_spec)
            return LLVMValue("i64", llvm_int_literal(cleaned), type_spec)
        raise SonCompileError("native 后端暂不支持该内置常量", line_no)
