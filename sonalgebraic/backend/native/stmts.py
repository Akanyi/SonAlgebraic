from __future__ import annotations

from ...analysis.typesys import is_bool, is_error, is_numeric, is_symbol
from ...core import ast
from ...core.errors import SonCompileError
from .base import LLVMValue, NativeGenBase, VarSlot


class StmtsMixin(NativeGenBase):
    """语句发射：分发、控制流、异常、GOSUB、IO。"""

    def stmt(self, stmt: ast.Stmt) -> None:
        if isinstance(stmt, ast.NoOp):
            if self.source_lines.get(stmt.line_no):
                self.emit(self.source_comment(stmt.line_no))
            return
        if self.terminated and not isinstance(stmt, ast.Label):
            return
        if isinstance(stmt, ast.LocalDeclaration):
            self.local_declaration(stmt)
            return
        if isinstance(stmt, ast.Assign):
            self.begin_stmt()
            self.assign_stmt(stmt)
            self.end_stmt()
            return
        if isinstance(stmt, ast.Print):
            self.begin_stmt()
            self.print_stmt(stmt)
            self.end_stmt()
            return
        if isinstance(stmt, ast.Call):
            self.begin_stmt()
            self.call_stmt(stmt)
            self.end_stmt()
            return
        if isinstance(stmt, ast.TryCatch):
            self.begin_stmt()
            self.try_catch_stmt(stmt)
            self.end_stmt()
            return
        if isinstance(stmt, ast.ThrowNew):
            self.throw_new_stmt(stmt)
            return
        if isinstance(stmt, ast.ThrowVar):
            self.throw_var_stmt(stmt)
            return
        if isinstance(stmt, ast.Input):
            self.begin_stmt()
            self.input_stmt(stmt)
            self.end_stmt()
            return
        if isinstance(stmt, ast.Cls):
            self.emit(self.source_comment(stmt.line_no))
            self.use_runtime("sa_cls")
            self.emit("  call void @sa_cls()")
            return
        if isinstance(stmt, ast.Return):
            self.return_stmt(stmt)
            return
        if isinstance(stmt, ast.Goto):
            self.emit(self.source_comment(stmt.line_no))
            self.emit(f"  br label %{self.label_name(stmt.label)}")
            self.terminated = True
            return
        if isinstance(stmt, ast.Gosub):
            self.gosub_stmt(stmt)
            return
        if isinstance(stmt, ast.Label):
            if not self.terminated:
                self.emit(f"  br label %{self.label_name(stmt.name)}")
            self.emit(self.source_comment(stmt.line_no))
            self.emit(f"{self.label_name(stmt.name)}:")
            self.terminated = False
            return
        if isinstance(stmt, ast.If):
            self.if_stmt(stmt)
            return
        if isinstance(stmt, ast.ForLoop):
            self.for_stmt(stmt)
            return
        if isinstance(stmt, ast.WhileLoop):
            self.while_stmt(stmt)
            return
        raise SonCompileError(f"native 后端暂不支持语句: {type(stmt).__name__}", stmt.line_no)

    def local_declaration(self, stmt: ast.LocalDeclaration) -> None:
        name = self.c_ident(stmt.name)
        self.emit(self.source_comment(stmt.line_no))
        ptr = self.alloca(self.llvm_type(stmt.type_spec), f"%{name}.addr")
        slot = VarSlot(stmt.name, stmt.type_spec, ptr)
        self.slots[stmt.name.lower()] = slot
        if stmt.type_spec.array_size is not None:
            self.emit(f"  store {self.llvm_type(stmt.type_spec)} zeroinitializer, ptr {ptr}")
            if self.is_string_array(stmt.type_spec):
                elem_ty = self.array_element_type(stmt.type_spec)
                self.use_runtime("sa_strdup")
                for index in range(stmt.type_spec.array_size):
                    element_ptr = self.next_temp()
                    dup = self.next_temp()
                    self.emit(f"  {element_ptr} = getelementptr inbounds {self.llvm_type(stmt.type_spec)}, ptr {ptr}, i64 0, i64 {index}")
                    self.emit(f"  {dup} = call ptr @sa_strdup(ptr @.sa_empty)")
                    self.emit(f"  store {self.llvm_type(elem_ty)} {dup}, ptr {element_ptr}")
                self.register_owned(slot)
            return
        if self.is_string_scalar(stmt.type_spec):
            # STRING 局部：初始化为 owned 空串并登记，作用域退出时 free（复刻 C 后端）
            self.use_runtime("sa_strdup")
            dup = self.next_temp()
            self.emit(f"  {dup} = call ptr @sa_strdup(ptr @.sa_empty)")
            self.emit(f"  store ptr {dup}, ptr {ptr}")
            self.register_owned(slot)
            if stmt.expr is not None:
                self.begin_stmt()
                value = self.cast_value(self.expr(stmt.expr), stmt.type_spec)
                self.use_runtime("sa_set_string")
                self.emit(f"  call void @sa_set_string(ptr {ptr}, ptr {value.value})")
                self.end_stmt()
            return
        if is_symbol(stmt.type_spec):
            self.emit(f"  store ptr null, ptr {ptr}")
            self.register_owned(slot)
            if stmt.expr is not None:
                self.begin_stmt()
                self.assign_symbol(slot.ptr, stmt.expr)
                self.end_stmt()
            return
        if is_error(stmt.type_spec):
            self.emit(f"  store {self.llvm_type(stmt.type_spec)} zeroinitializer, ptr {ptr}")
            self.register_owned(slot)
            return
        if self.is_entity_scalar(stmt.type_spec):
            self.emit(f"  store {self.llvm_type(stmt.type_spec)} zeroinitializer, ptr {ptr}")
            if self.type_has_managed_resources(stmt.type_spec):
                self.emit_entity_init(ptr, stmt.type_spec)
                self.register_owned(slot)
            if stmt.expr is not None:
                self.begin_stmt()
                if self.type_has_managed_resources(stmt.type_spec):
                    source_ptr = self.entity_source_ptr(stmt.expr, stmt.type_spec)
                    self.emit_entity_copy(ptr, source_ptr, stmt.type_spec)
                else:
                    value = self.cast_value(self.expr(stmt.expr), stmt.type_spec)
                    self.emit(f"  store {self.llvm_type(stmt.type_spec)} {value.value}, ptr {ptr}")
                self.end_stmt()
            return
        self.emit(f"  store {self.llvm_type(stmt.type_spec)} {self.default_value(stmt.type_spec)}, ptr {ptr}")
        if stmt.expr is not None:
            self.begin_stmt()
            value = self.cast_value(self.expr(stmt.expr), stmt.type_spec)
            self.emit(f"  store {self.llvm_type(stmt.type_spec)} {value.value}, ptr {ptr}")
            self.end_stmt()

    def assign_stmt(self, stmt: ast.Assign) -> None:
        target_type = self.type_of_expr(stmt.target)
        target_ptr = self.lvalue_ptr(stmt.target)
        self.emit(self.source_comment(stmt.line_no))
        if is_symbol(target_type):
            self.assign_symbol(target_ptr, stmt.expr)
            return
        if self.is_entity_scalar(target_type) and self.type_has_managed_resources(target_type):
            source_ptr = self.entity_source_ptr(stmt.expr, target_type)
            self.emit_entity_copy(target_ptr, source_ptr, target_type)
            return
        value = self.cast_value(self.expr(stmt.expr), target_type)
        if self.is_string_scalar(target_type):
            # 先 free 旧串再 dup 新串（sa_set_string 内部处理），owned 语义
            self.use_runtime("sa_set_string")
            self.emit(f"  call void @sa_set_string(ptr {target_ptr}, ptr {value.value})")
            return
        self.emit(f"  store {self.llvm_type(target_type)} {value.value}, ptr {target_ptr}")

    def assign_symbol(self, target_ptr: str, expr: ast.Expr) -> None:
        # 新树先完整求值到 SSA 临时，再释放旧树，最后接管。这个顺序是为了避免
        # `wave = wave * x` 这类自引用赋值先 free LHS 后 clone RHS 造成 UAF。
        new_tree = self.symbol_expr(expr)
        old_tree = self.next_temp()
        self.emit(f"  {old_tree} = load ptr, ptr {target_ptr}")
        self.use_runtime("sa_symbol_free")
        self.emit(f"  call void @sa_symbol_free(ptr {old_tree})")
        self.emit(f"  store ptr {new_tree.value}, ptr {target_ptr}")

    def print_stmt(self, stmt: ast.Print) -> None:
        self.emit(self.source_comment(stmt.line_no))
        if stmt.expr is None:
            self.emit('  call i32 (ptr, ...) @printf(ptr @.sa_fmt_newline)')
            return
        if isinstance(stmt.expr, ast.FString):
            self.print_fstring(stmt.expr)
            return
        value = self.expr(stmt.expr)
        self.emit_print_value(value, newline=True)

    def print_fstring(self, expr: ast.FString) -> None:
        for part in expr.parts:
            if isinstance(part, str):
                if part:
                    self.emit_print_value(LLVMValue("ptr", self.string_ptr(part), ast.TypeSpec("STRING")), newline=False)
                continue
            self.emit_print_value(self.expr(part), newline=False)
        self.emit('  call i32 (ptr, ...) @printf(ptr @.sa_fmt_newline)')

    def emit_print_value(self, value: LLVMValue, newline: bool) -> None:
        if value.type_name == "i1":
            extended = self.next_temp()
            self.emit(f"  {extended} = zext i1 {value.value} to i64")
            value = LLVMValue("i64", extended)
        if value.type_name == "i64":
            fmt = "@.sa_fmt_i64" if newline else "@.sa_fmt_i64_part"
            self.emit(f"  call i32 (ptr, ...) @printf(ptr {fmt}, i64 {value.value})")
            return
        if value.type_name == "double":
            fmt = "@.sa_fmt_f64" if newline else "@.sa_fmt_f64_part"
            self.emit(f"  call i32 (ptr, ...) @printf(ptr {fmt}, double {value.value})")
            return
        if value.type_name == "ptr":
            if is_error(value.type_spec or ast.TypeSpec("VOID")):
                self.emit_print_value(LLVMValue("ptr", self.error_message_ptr(value.value), ast.TypeSpec("STRING")), newline=newline)
                return
            if is_symbol(value.type_spec or ast.TypeSpec("VOID")):
                self.use_runtime("sa_symbol_to_string")
                self.use_runtime("free")
                temp = self.next_temp()
                self.emit(f"  {temp} = call ptr @sa_symbol_to_string(ptr {value.value})")
                self.emit_print_value(LLVMValue("ptr", temp, ast.TypeSpec("STRING")), newline=newline)
                self.emit(f"  call void @free(ptr {temp})")
                return
            if not self.is_string_type(value.type_spec):
                self.use_runtime("sa_to_string_pointer")
                self.use_runtime("free")
                temp = self.next_temp()
                self.emit(f"  {temp} = call ptr @sa_to_string_pointer(ptr {value.value})")
                self.emit_print_value(LLVMValue("ptr", temp, ast.TypeSpec("STRING")), newline=newline)
                self.emit(f"  call void @free(ptr {temp})")
                return
            fmt = "@.sa_fmt_str" if newline else "@.sa_fmt_str_part"
            self.emit(f"  call i32 (ptr, ...) @printf(ptr {fmt}, ptr {value.value})")
            return
        raise SonCompileError(f"native 后端暂不支持 PRINT 类型: {value.type_name}")

    def call_stmt(self, stmt: ast.Call) -> None:
        self.emit(self.source_comment(stmt.line_no))
        c_func = self.resolve_c_func(stmt.name)
        if c_func is not None:
            self.c_call_stmt(c_func, stmt.args)
            return
        sub = self.checked.subs.get(stmt.name.lower())
        external = None
        if sub is None:
            external = self.resolve_external_sub(stmt.name)
            if external is None:
                raise SonCompileError(f"native 后端暂不支持外部 CALL: {stmt.name}", stmt.line_no)
            external_name, sub = external
        else:
            external_name = self.sub_name(stmt.name)
        is_external = external is not None
        args = self.call_args(sub, stmt.args, c_abi=is_external)
        if self.has_active_resources():
            self.wrap_call_with_throw_cleanup(external_name, sub, args, raw_name=True, c_abi=is_external)
            return
        self.emit_call(external_name, sub, args, raw_name=True, c_abi=is_external)

    def emit_call(self, name: str, sub: ast.Subroutine, args: list[str], raw_name: bool = False, c_abi: bool = False) -> None:
        ret_type = self.c_abi_type(sub.return_type) if c_abi else self.llvm_type(sub.return_type)
        callee = name if raw_name else self.sub_name(name)
        if sub.return_type.name == "VOID":
            self.emit(f"  call {ret_type} @{callee}({', '.join(args)})")
        else:
            temp = self.next_temp()
            self.emit(f"  {temp} = call {ret_type} @{callee}({', '.join(args)})")

    def c_call_stmt(self, c_func: ast.CFunctionDecl, args: list[ast.Expr]) -> LLVMValue | None:
        self.use_c_func(c_func)
        call_args = self.c_call_args(c_func, args)
        ret_type = self.c_abi_type(c_func.return_type)
        if c_func.return_type.name == "VOID":
            self.emit(f"  call {ret_type} @{c_func.name}({', '.join(call_args)})")
            return None
        temp = self.next_temp()
        self.emit(f"  {temp} = call {ret_type} @{c_func.name}({', '.join(call_args)})")
        if is_bool(c_func.return_type):
            return self.i32_status(temp)
        return LLVMValue(ret_type, temp, c_func.return_type)

    def wrap_call_with_throw_cleanup(self, name: str, sub: ast.Subroutine, args: list[str], raw_name: bool = False, c_abi: bool = False) -> None:
        env = self.next_temp()
        frame = self.next_temp()
        sj = self.next_temp()
        is_try = self.next_temp()
        try_label = self.unique_label("call_try")
        catch_label = self.unique_label("call_cleanup")
        end_label = self.unique_label("call_end")
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
        self.emit_call(name, sub, args, raw_name=raw_name, c_abi=c_abi)
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

    def input_stmt(self, stmt: ast.Input) -> None:
        self.emit(self.source_comment(stmt.line_no))
        prompt = self.expr(stmt.prompt)
        self.emit_print_value(prompt, newline=False)
        target = self.slot(stmt.target, stmt.line_no)
        buf = self.alloca("[4096 x i8]")
        self.use_runtime("sa_read_line")
        self.emit(f"  call void @sa_read_line(ptr {buf}, i64 4096)")
        if self.is_string_scalar(target.type_spec):
            self.use_runtime("sa_set_string")
            self.emit(f"  call void @sa_set_string(ptr {target.ptr}, ptr {buf})")
            return
        if is_numeric(target.type_spec):
            self.use_runtime("sa_number")
            value = self.next_temp()
            self.emit(f"  {value} = call double @sa_number(ptr {buf})")
            if target.type_spec.subtype == "LONG":
                long_value = self.next_temp()
                self.emit(f"  {long_value} = fptosi double {value} to i64")
                self.emit(f"  store i64 {long_value}, ptr {target.ptr}")
            else:
                self.emit(f"  store double {value}, ptr {target.ptr}")
            return
        raise SonCompileError("IO.INPUT 当前只支持 STRING 和 NUM", stmt.line_no)

    def try_catch_stmt(self, stmt: ast.TryCatch) -> None:
        self.emit(self.source_comment(stmt.line_no))
        sub = self.checked.subs.get(stmt.call_name.lower())
        if sub is None:
            raise SonCompileError(f"native 后端暂不支持外部 TRY CALL: {stmt.call_name}", stmt.line_no)
        args = self.call_args(sub, stmt.args)
        env = self.next_temp()
        frame = self.next_temp()
        sj = self.next_temp()
        is_try = self.next_temp()
        try_label = self.unique_label("try_body")
        catch_label = self.unique_label("catch_dispatch")
        end_label = self.unique_label("try_end")
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
        self.emit(f"  call {self.llvm_type(sub.return_type)} @{self.sub_name(stmt.call_name)}({', '.join(args)})")
        self.use_runtime("sa_try_pop")
        self.emit("  call void @sa_try_pop()")
        self.emit(f"  br label %{end_label}")
        self.terminated = True

        self.emit(f"{catch_label}:")
        self.terminated = False
        self.use_runtime("sa_try_pop")
        self.emit("  call void @sa_try_pop()")
        trace_slot = self.slot(stmt.traceback_var, stmt.line_no)
        self.use_runtime("sa_set_error")
        self.use_runtime("sa_current_error")
        self.emit(f"  call void @sa_set_error(ptr {trace_slot.ptr}, ptr @sa_current_error)")
        self.emit_catch_chain(stmt.catches, 0, end_label)

        self.emit(f"{end_label}:")
        self.terminated = False

    def emit_catch_chain(self, catches: list[ast.CatchBranch], index: int, end_label: str) -> None:
        if index >= len(catches):
            self.emit_active_cleanup()
            self.use_runtime("sa_throw_dispatch")
            self.emit("  call void @sa_throw_dispatch()")
            self.emit("  unreachable")
            self.terminated = True
            return
        branch = catches[index]
        body_label = self.unique_label("catch_body")
        next_label = self.unique_label("catch_next")
        if branch.error_type in {"ANY", "ERR_ANY"}:
            self.emit(f"  br label %{body_label}")
        else:
            self.use_runtime("strcmp")
            type_field = self.next_temp()
            current_type = self.next_temp()
            cmp = self.next_temp()
            matched = self.next_temp()
            self.emit(f"  {type_field} = getelementptr inbounds %SaError, ptr @sa_current_error, i64 0, i32 1")
            self.emit(f"  {current_type} = load ptr, ptr {type_field}")
            self.emit(f"  {cmp} = call i32 @strcmp(ptr {current_type}, ptr {self.string_ptr(branch.error_type)})")
            self.emit(f"  {matched} = icmp eq i32 {cmp}, 0")
            self.emit(f"  br i1 {matched}, label %{body_label}, label %{next_label}")
        self.terminated = True

        self.emit(f"{body_label}:")
        self.terminated = False
        self.emit(self.source_comment(branch.line_no))
        alias_ptr = f"%{self.c_ident(branch.alias)}.catch"
        alias_slot = VarSlot(branch.alias, ast.TypeSpec("ERROR"), alias_ptr)
        self.alloca("%SaError", alias_ptr)
        self.emit(f"  store %SaError zeroinitializer, ptr {alias_ptr}")
        self.use_runtime("sa_set_error")
        self.emit(f"  call void @sa_set_error(ptr {alias_ptr}, ptr @sa_current_error)")
        saved_slots = self.slots.copy()
        self.slots[branch.alias.lower()] = alias_slot
        self.push_scope()
        self.register_owned(alias_slot)
        for inner in branch.body:
            self.stmt(inner)
        self.pop_scope_with_cleanup()
        self.slots = saved_slots
        if not self.terminated:
            self.emit(f"  br label %{end_label}")
            self.terminated = True

        self.emit(f"{next_label}:")
        self.terminated = False
        self.emit_catch_chain(catches, index + 1, end_label)

    def throw_new_stmt(self, stmt: ast.ThrowNew) -> None:
        self.emit(self.source_comment(stmt.line_no))
        self.begin_stmt()
        message = self.expr(stmt.message)
        self.use_runtime("sa_raise_new")
        self.emit(f"  call void @sa_raise_new(ptr {self.string_ptr(stmt.error_type)}, ptr {message.value}, i32 {stmt.line_no}, ptr {self.string_ptr(self.current_sub.name if self.current_sub else '<main>')})")
        self.end_stmt()
        self.emit_active_cleanup()
        self.use_runtime("sa_throw_dispatch")
        self.emit("  call void @sa_throw_dispatch()")
        self.emit("  unreachable")
        self.terminated = True

    def throw_var_stmt(self, stmt: ast.ThrowVar) -> None:
        self.emit(self.source_comment(stmt.line_no))
        slot = self.slot(stmt.name, stmt.line_no)
        self.use_runtime("sa_raise_error")
        self.emit(f"  call void @sa_raise_error(ptr {slot.ptr})")
        self.emit_active_cleanup()
        self.use_runtime("sa_throw_dispatch")
        self.emit("  call void @sa_throw_dispatch()")
        self.emit("  unreachable")
        self.terminated = True

    def gosub_stmt(self, stmt: ast.Gosub) -> None:
        if self.gosub_stack_ptr is None or self.gosub_top_ptr is None:
            raise SonCompileError("内部错误: GOSUB 栈未初始化", stmt.line_no)
        self.emit(self.source_comment(stmt.line_no))
        top = self.next_temp()
        overflow = self.next_temp()
        overflow_label = self.unique_label("gosub_overflow")
        push_label = self.unique_label("gosub_push")
        self.emit(f"  {top} = load i64, ptr {self.gosub_top_ptr}")
        self.emit(f"  {overflow} = icmp sge i64 {top}, 64")
        self.emit(f"  br i1 {overflow}, label %{overflow_label}, label %{push_label}")
        self.terminated = True

        self.emit(f"{overflow_label}:")
        self.terminated = False
        self.emit_print_value(LLVMValue("ptr", self.string_ptr("SonAlgebraic runtime: GOSUB stack overflow"), ast.TypeSpec("STRING")), newline=True)
        self.use_runtime("exit")
        self.emit("  call void @exit(i32 1)")
        self.emit("  unreachable")
        self.terminated = True

        self.emit(f"{push_label}:")
        self.terminated = False
        slot = self.next_temp()
        next_top = self.next_temp()
        self.emit(f"  {slot} = getelementptr inbounds [64 x i64], ptr {self.gosub_stack_ptr}, i64 0, i64 {top}")
        self.emit(f"  store i64 {stmt.line_no}, ptr {slot}")
        self.emit(f"  {next_top} = add i64 {top}, 1")
        self.emit(f"  store i64 {next_top}, ptr {self.gosub_top_ptr}")
        self.emit(f"  br label %{self.label_name(stmt.label)}")
        self.terminated = True
        self.emit(f"{self.gosub_return_label(stmt.line_no)}:")
        self.terminated = False

    def gosub_return_label(self, line_no: int) -> str:
        return f"sa_gosub_return_{line_no}"

    def return_stmt(self, stmt: ast.Return) -> None:
        self.emit(self.source_comment(stmt.line_no))
        if stmt.expr is None:
            if self.current_gosub_lines:
                self.emit_gosub_return_dispatch()
            self.emit_active_cleanup()
            self.emit("  ret void")
        else:
            assert self.current_sub is not None
            # 返回值先求到 SSA 值再做作用域清理。丢弃临时帧：返回值本身可能是临时堆串，
            # 不能 free（与 C 后端 return 的 _cleanup 丢弃行为一致）。
            self.begin_stmt()
            value = self.cast_value(self.expr(stmt.expr), self.current_sub.return_type)
            self.discard_stmt()
            self.emit_active_cleanup()
            self.emit(f"  ret {self.llvm_type(self.current_sub.return_type)} {value.value}")
        self.terminated = True

    def emit_gosub_return_dispatch(self) -> None:
        assert self.gosub_stack_ptr is not None and self.gosub_top_ptr is not None
        top = self.next_temp()
        has_return = self.next_temp()
        dispatch_label = self.unique_label("gosub_dispatch")
        done_label = self.unique_label("gosub_return_done")
        invalid_label = self.unique_label("gosub_invalid")
        self.emit(f"  {top} = load i64, ptr {self.gosub_top_ptr}")
        self.emit(f"  {has_return} = icmp sgt i64 {top}, 0")
        self.emit(f"  br i1 {has_return}, label %{dispatch_label}, label %{done_label}")
        self.terminated = True

        self.emit(f"{dispatch_label}:")
        self.terminated = False
        new_top = self.next_temp()
        slot = self.next_temp()
        line = self.next_temp()
        self.emit(f"  {new_top} = sub i64 {top}, 1")
        self.emit(f"  store i64 {new_top}, ptr {self.gosub_top_ptr}")
        self.emit(f"  {slot} = getelementptr inbounds [64 x i64], ptr {self.gosub_stack_ptr}, i64 0, i64 {new_top}")
        self.emit(f"  {line} = load i64, ptr {slot}")
        cases = " ".join(f"i64 {item}, label %{self.gosub_return_label(item)}" for item in self.current_gosub_lines)
        self.emit(f"  switch i64 {line}, label %{invalid_label} [ {cases} ]")
        self.terminated = True

        self.emit(f"{invalid_label}:")
        self.terminated = False
        self.emit_print_value(LLVMValue("ptr", self.string_ptr("SonAlgebraic runtime: invalid GOSUB return address"), ast.TypeSpec("STRING")), newline=True)
        self.use_runtime("exit")
        self.emit("  call void @exit(i32 1)")
        self.emit("  unreachable")
        self.terminated = True

        self.emit(f"{done_label}:")
        self.terminated = False

    def if_stmt(self, stmt: ast.If) -> None:
        then_label = self.unique_label("if_then")
        else_label = self.unique_label("if_else") if stmt.elifs or stmt.else_body else self.unique_label("if_end")
        end_label = self.unique_label("if_end")
        self.begin_stmt()
        condition = self.truthy(self.expr(stmt.condition))
        self.end_stmt()
        self.emit(self.source_comment(stmt.line_no))
        self.emit(f"  br i1 {condition.value}, label %{then_label}, label %{else_label}")
        self.terminated = True

        self.emit(f"{then_label}:")
        self.terminated = False
        self.run_block(stmt.body)
        if not self.terminated:
            self.emit(f"  br label %{end_label}")

        self.emit(f"{else_label}:")
        self.terminated = False
        if stmt.elifs:
            first = stmt.elifs[0]
            nested = ast.If(first.line_no, first.condition, first.body, stmt.elifs[1:], stmt.else_body)
            self.if_stmt(nested)
        else:
            self.run_block(stmt.else_body)
        if not self.terminated:
            self.emit(f"  br label %{end_label}")

        self.emit(f"{end_label}:")
        self.terminated = False

    def for_stmt(self, stmt: ast.ForLoop) -> None:
        slot = self.slot(stmt.var, stmt.line_no)
        var_ty = self.llvm_type(slot.type_spec)
        if var_ty not in {"i64", "double"}:
            raise SonCompileError("native 后端 FOR 循环变量必须是数值类型", stmt.line_no)
        self.emit(self.source_comment(stmt.line_no))
        # 边界与步长进循环前只求值一次（BASIC 语义）。native 是 SSA 直接发射，
        # 这些值算在前置块里，天然支配后续 cond/body 块，不需要额外存储。
        start = self.for_cast(self.expr(stmt.start), var_ty)
        self.emit(f"  store {var_ty} {start.value}, ptr {slot.ptr}")
        end = self.for_cast(self.expr(stmt.end), var_ty)
        if stmt.step is not None:
            step = self.for_cast(self.expr(stmt.step), var_ty)
        else:
            step = LLVMValue(var_ty, "1" if var_ty == "i64" else "1.0")
        cond_label = self.unique_label("for_cond")
        body_label = self.unique_label("for_body")
        end_label = self.unique_label("for_end")
        self.emit(f"  br label %{cond_label}")

        self.emit(f"{cond_label}:")
        self.terminated = False
        cur = self.next_temp()
        self.emit(f"  {cur} = load {var_ty}, ptr {slot.ptr}")
        # 步长正负都支持：正步长用 <=，负步长用 >=，运行时用 select 选择。
        pos = self.next_temp()
        le = self.next_temp()
        ge = self.next_temp()
        cond = self.next_temp()
        if var_ty == "i64":
            self.emit(f"  {pos} = icmp sge i64 {step.value}, 0")
            self.emit(f"  {le} = icmp sle i64 {cur}, {end.value}")
            self.emit(f"  {ge} = icmp sge i64 {cur}, {end.value}")
        else:
            self.emit(f"  {pos} = fcmp oge double {step.value}, 0.0")
            self.emit(f"  {le} = fcmp ole double {cur}, {end.value}")
            self.emit(f"  {ge} = fcmp oge double {cur}, {end.value}")
        self.emit(f"  {cond} = select i1 {pos}, i1 {le}, i1 {ge}")
        self.emit(f"  br i1 {cond}, label %{body_label}, label %{end_label}")
        self.terminated = True

        self.emit(f"{body_label}:")
        self.terminated = False
        self.run_block(stmt.body)
        if not self.terminated:
            nv = self.next_temp()
            inc = self.next_temp()
            self.emit(f"  {nv} = load {var_ty}, ptr {slot.ptr}")
            if var_ty == "i64":
                self.emit(f"  {inc} = add i64 {nv}, {step.value}")
            else:
                self.emit(f"  {inc} = fadd double {nv}, {step.value}")
            self.emit(f"  store {var_ty} {inc}, ptr {slot.ptr}")
            self.emit(f"  br label %{cond_label}")

        self.emit(f"{end_label}:")
        self.terminated = False

    def while_stmt(self, stmt: ast.WhileLoop) -> None:
        cond_label = self.unique_label("while_cond")
        body_label = self.unique_label("while_body")
        end_label = self.unique_label("while_end")
        self.emit(self.source_comment(stmt.line_no))
        self.emit(f"  br label %{cond_label}")

        # 条件每次迭代都重新求值，所以求值放在 cond 块内。条件里的临时堆串每轮释放。
        self.emit(f"{cond_label}:")
        self.terminated = False
        self.begin_stmt()
        cond = self.truthy(self.expr(stmt.condition))
        self.end_stmt()
        self.emit(f"  br i1 {cond.value}, label %{body_label}, label %{end_label}")
        self.terminated = True

        self.emit(f"{body_label}:")
        self.terminated = False
        self.run_block(stmt.body)
        if not self.terminated:
            self.emit(f"  br label %{cond_label}")

        self.emit(f"{end_label}:")
        self.terminated = False
