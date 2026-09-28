from __future__ import annotations

from ...analysis.typesys import callable_symbol_type, is_bool, is_error, is_numeric, is_sub_ptr, is_sub_type, is_symbol, same_type_spec, sub_signature
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
        if isinstance(stmt, ast.AwaitStmt):
            self.begin_stmt()
            self.emit(self.source_comment(stmt.line_no))
            self.expr(stmt.expr)
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
        if isinstance(stmt, ast.NewSub):
            self.new_sub_stmt(stmt)
            return
        if isinstance(stmt, ast.CallRet):
            self.callret_stmt(stmt)
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
                self.store_string(ptr, self.cast_value(self.expr(stmt.expr), stmt.type_spec))
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
        if is_sub_type(stmt.type_spec):
            self.emit(f"  store ptr null, ptr {ptr}")
            self.register_owned(slot)
            if stmt.expr is not None:
                self.begin_stmt()
                self.store_callable(ptr, self.expr(stmt.expr))
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
                    self.store_entity(ptr, stmt.expr, stmt.type_spec)
                else:
                    value = self.cast_value(self.expr(stmt.expr), stmt.type_spec)
                    self.emit(f"  store {self.llvm_type(stmt.type_spec)} {value.value}, ptr {ptr}")
                self.end_stmt()
            return
        self.emit(f"  store {self.llvm_type(stmt.type_spec)} {self.default_value(stmt.type_spec)}, ptr {ptr}")
        if stmt.type_spec.name == "PROMISE":
            self.register_owned(slot)
        if stmt.expr is not None:
            self.begin_stmt()
            value = self.cast_value(self.expr(stmt.expr), stmt.type_spec)
            if stmt.type_spec.name == "PROMISE":
                self.adopt_temp_cleanup(value.value, stmt.type_spec)
            self.emit(f"  store {self.llvm_type(stmt.type_spec)} {value.value}, ptr {ptr}")
            self.end_stmt()

    def ownership_assign_stmt(self, stmt: ast.Assign) -> None:
        """`a f= b` / `a m= b`：与 C 后端同一套思路——按 llvm_type 整体 load/store，不深拷贝；
        move 把源清零（源保持登记，块尾 free(null) / 清零 ERROR 都安全），borrow 把目标从本块
        登记里摘掉（语义层保证目标与借用语句同块 DIM，所以一定在 scope_resources[-1]）。"""
        assert isinstance(stmt.target, ast.VarRef) and isinstance(stmt.expr, ast.VarRef)
        target_ptr, target_type = self.varref_ptr(stmt.target.name, stmt.line_no)
        source_ptr, _ = self.varref_ptr(stmt.expr.name, stmt.line_no)
        self.emit(self.source_comment(stmt.line_no))
        self.emit_free_slot(VarSlot(stmt.target.name, target_type, target_ptr))
        llvm_ty = self.llvm_type(target_type)
        value = self.next_temp()
        self.emit(f"  {value} = load {llvm_ty}, ptr {source_ptr}")
        self.emit(f"  store {llvm_ty} {value}, ptr {target_ptr}")
        if stmt.mode == "move":
            empty = "null" if llvm_ty == "ptr" else "zeroinitializer"
            self.emit(f"  store {llvm_ty} {empty}, ptr {source_ptr}")
            return
        key = stmt.target.name.lower()
        self.scope_resources[-1] = [slot for slot in self.scope_resources[-1] if slot.name.lower() != key]

    def assign_stmt(self, stmt: ast.Assign) -> None:
        # 函数引用是瘦指针，不持有资源；f= 和普通赋值等价。
        fn_ref = isinstance(stmt.expr, ast.SubRef) or (
            isinstance(stmt.target, ast.VarRef) and is_sub_ptr(self.type_of_expr(stmt.target))
        )
        if stmt.mode != "copy" and not fn_ref:
            self.ownership_assign_stmt(stmt)
            return
        target_type = self.type_of_expr(stmt.target)
        target_ptr = self.lvalue_ptr(stmt.target)
        self.emit(self.source_comment(stmt.line_no))
        if isinstance(stmt.expr, ast.AwaitExpr):
            # 保留左值只求一次的顺序；恢复入口不能使用挂起前定义的字段/下标 GEP。
            saved_target = self.alloca("ptr")
            self.emit(f"  store ptr {target_ptr}, ptr {saved_target}")
            value = self.cast_value(self.expr(stmt.expr), target_type)
            target_ptr = self.next_temp()
            self.emit(f"  {target_ptr} = load ptr, ptr {saved_target}")
            if self.is_string_scalar(target_type):
                self.store_string(target_ptr, value)
            else:
                self.emit(f"  store {self.llvm_type(target_type)} {value.value}, ptr {target_ptr}")
            return
        if is_symbol(target_type):
            self.assign_symbol(target_ptr, stmt.expr)
            return
        if is_sub_type(target_type):
            self.store_callable(target_ptr, self.expr(stmt.expr))
            return
        if is_error(target_type):
            value = self.cast_value(self.expr(stmt.expr), target_type)
            source = self.alloca("%SaError")
            self.emit(f"  store %SaError {value.value}, ptr {source}")
            # sa_set_error 先 free 旧消息，不可直接拿别名源；先复制独立快照。
            copy = self.alloca("%SaError")
            self.emit(f"  store %SaError zeroinitializer, ptr {copy}")
            self.use_runtime("sa_set_error")
            self.use_runtime("sa_error_clear")
            self.emit(f"  call void @sa_set_error(ptr {copy}, ptr {source})")
            self.emit(f"  call void @sa_error_clear(ptr {target_ptr})")
            result = self.next_temp()
            self.emit(f"  {result} = load %SaError, ptr {copy}")
            self.emit(f"  store %SaError {result}, ptr {target_ptr}")
            self.emit(f"  store %SaError zeroinitializer, ptr {copy}")
            return
        if self.is_entity_scalar(target_type) and self.type_has_managed_resources(target_type):
            self.store_entity(target_ptr, stmt.expr, target_type)
            return
        value = self.cast_value(self.expr(stmt.expr), target_type)
        if target_type.name == "PROMISE":
            self.adopt_temp_cleanup(value.value, target_type)
        if self.is_string_scalar(target_type):
            self.store_string(target_ptr, value)
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
        if getattr(self, "async_context", None) is not None:
            self.adopt_temp_cleanup(new_tree.value, ast.TypeSpec("SYMBOL"))
        self.emit(f"  store ptr {new_tree.value}, ptr {target_ptr}")

    def store_callable(self, target_ptr: str, value: LLVMValue) -> None:
        self.use_runtime("sa_callable_set")
        self.emit(f"  call void @sa_callable_set(ptr {target_ptr}, ptr {value.value})")

    def new_sub_stmt(self, stmt: ast.NewSub) -> None:
        self.emit(self.source_comment(stmt.line_no))
        source = self.expr(stmt.source)
        self.use_runtime("sa_callable_new")
        value = self.next_temp()
        self.emit(f"  {value} = call ptr @sa_callable_new(ptr {source.value})")
        type_spec = sub_signature(source.type_spec or ast.TypeSpec("SUB"))
        slot = VarSlot(stmt.name, type_spec, self.alloca("ptr", f"%{self.c_ident(stmt.name)}.addr"))
        self.slots[stmt.name.lower()] = slot
        self.emit(f"  store ptr {value}, ptr {slot.ptr}")
        self.register_owned(slot)

    def callret_stmt(self, stmt: ast.CallRet) -> None:
        self.emit(self.source_comment(stmt.line_no))
        type_spec = callable_symbol_type(stmt.name, self.current_symbols(), self.checked.entities, stmt.line_no)
        assert type_spec is not None
        self.begin_stmt()
        result = self.call_indirect(stmt.name, stmt.args, stmt.line_no, type_spec)
        if self.current_sub is not None and self.current_sub.return_type.name != "VOID":
            self.adopt_temp_cleanup(result.value, self.current_sub.return_type)
        self.end_stmt()
        self.emit_active_cleanup()
        if self.current_sub is None or self.current_sub.return_type.name == "VOID":
            self.emit("  ret void")
        else:
            self.emit(f"  ret {self.llvm_type(self.current_sub.return_type)} {result.value}")
        self.terminated = True

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
        if value.type_name == "float":
            # 与 C 后端 sa_print_double 一致：float 无损提升 double 后走 %.15g
            value = self.cast_to_double(value)
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
                callable_type = callable_symbol_type(stmt.name, self.current_symbols(), self.checked.entities, stmt.line_no)
                if callable_type is not None:
                    self.call_indirect(stmt.name, stmt.args, stmt.line_no, callable_type)
                    return
                raise SonCompileError(f"native 后端暂不支持外部 CALL: {stmt.name}", stmt.line_no)
            external_name, sub = external
        else:
            external_name = self.sub_name(stmt.name)
        is_external = external is not None
        args = self.call_args(sub, stmt.args, c_abi=is_external)
        if self.has_active_resources() or (self.temp_cleanup and self.temp_cleanup[-1]):
            self.wrap_call_with_throw_cleanup(external_name, sub, args, raw_name=True, c_abi=is_external)
            return
        self.emit_call(external_name, sub, args, raw_name=True, c_abi=is_external)

    def emit_call(self, name: str, sub: ast.Subroutine, args: list[str], raw_name: bool = False, c_abi: bool = False) -> None:
        ret_type = self.c_abi_type(sub.return_type) if c_abi else self.llvm_type(sub.return_type)
        callee = name if raw_name else self.sub_name(name)
        if sub.return_type.name == "VOID":
            self.emit(f"  call {ret_type} @{callee}({', '.join(args)})")
        else:
            # 语句形式丢弃返回值（TRY CALL f()）：托管返回值归调用方，当场释放。不能挂到语句尾——
            # TRY 的 end 块还有 CATCH 分支汇入，try 块里定义的 SSA 值在那里不支配。
            temp = self.next_temp()
            self.emit(f"  {temp} = call {ret_type} @{callee}({', '.join(args)})")
            self.emit_free_value(temp, sub.return_type)

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
        pending_cleanup = list(self.temp_cleanup[-1]) if self.temp_cleanup else []
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
        for line in pending_cleanup:
            self.emit(line)
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
            raw = self.next_temp()
            self.emit(f"  {raw} = call double @sa_number(ptr {buf})")
            # sa_number 统一给 double，按目标子类型收窄（LONG→fptosi、FLOAT→fptrunc），
            # 否则 FLOAT 槽位会被 store double 撑爆类型
            value = self.cast_value(LLVMValue("double", raw, ast.TypeSpec("NUM", "DOUBLE")), target.type_spec)
            self.emit(f"  store {value.type_name} {value.value}, ptr {target.ptr}")
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
        self.emit_call(self.sub_name(stmt.call_name), sub, args, raw_name=True)
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
        # 同一 SUB 可以有多个 CATCH 使用相同别名；LLVM 局部 SSA 名必须唯一。
        alias_ptr = self.next_temp()
        alias_slot = VarSlot(branch.alias, ast.TypeSpec("ERROR"), alias_ptr)
        self.alloca("%SaError", alias_ptr)
        if getattr(self, "async_context", None) is not None:
            resource = self.coro_resource(alias_slot)
            self.emit(f"  call void @{resource.cleanup_name}(ptr %sa_frame)")
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
            return_type = self.current_sub.return_type
            # 返回值先求成 SSA 值，再照常跑语句级临时清理（返回值本身要么已经从临时表里
            # 接管出来、要么是拷贝/搬出来的新值，不在表里），最后释放本帧局部。
            self.begin_stmt()
            if self.type_has_managed_resources(return_type):
                value = self.owned_return_value(stmt.expr, return_type)
            else:
                value = self.cast_value(self.expr(stmt.expr), return_type)
                if return_type.name == "PROMISE":
                    self.adopt_temp_cleanup(value.value, return_type)
                    if isinstance(stmt.expr, ast.VarRef) and self.is_owned_var(stmt.expr.name):
                        ptr, _ = self.varref_ptr(stmt.expr.name, stmt.expr.line_no)
                        self.emit(f"  store i64 0, ptr {ptr}")
            self.end_stmt()
            self.emit_active_cleanup()
            self.emit(f"  ret {self.llvm_type(return_type)} {value.value}")
        self.terminated = True

    def owned_return_value(self, expr: ast.Expr, return_type: ast.TypeSpec) -> LLVMValue:
        """托管类型的 RETURN 值：交给调用方的必须是一份独立所有权。

        本帧登记过的局部（含其字段路径）整体搬出去、原位清零，随后的帧清理 free(null) 无害——
        以前是 `tmp = s; free(s); return tmp`，调用方拿到的是悬空指针。搬不动的（全局、REF 形参、
        f= 借来的、数组元素、值传 SYMBOL 形参）深拷贝；本语句刚算出的临时量（F-string、CONCAT、
        SUB 返回值）直接接管。与 C 后端 owned_value_lines 一一对应。
        """
        llvm_ty = self.llvm_type(return_type)
        # 类型必须严格一致才能整体搬走：SYMBOL SUB 里 RETURN s（s 是 STRING）合法，但那是拿 s 当变量名建树
        if isinstance(expr, ast.VarRef) and self.is_owned_var(expr.name) and same_type_spec(self.type_of_expr(expr), return_type):
            ptr, _ = self.varref_ptr(expr.name, expr.line_no)
            value = self.next_temp()
            self.emit(f"  {value} = load {llvm_ty}, ptr {ptr}")
            self.emit(f"  store {llvm_ty} {'null' if llvm_ty == 'ptr' else 'zeroinitializer'}, ptr {ptr}")
            return LLVMValue(llvm_ty, value, return_type)
        if is_symbol(return_type):
            # symbol_expr 自己分辨：变量→clone，临时树→接管，数字/串→常量节点
            return self.symbol_expr(expr)
        if self.is_string_scalar(return_type):
            value = self.cast_value(self.expr(expr), return_type)
            if self.adopt_temp_cleanup(value.value, return_type):
                return value
            self.use_runtime("sa_strdup")
            dup = self.next_temp()
            self.emit(f"  {dup} = call ptr @sa_strdup(ptr {value.value})")
            return LLVMValue("ptr", dup, return_type)
        if is_sub_type(return_type):
            value = self.expr(expr)
            if self.adopt_temp_cleanup(value.value, return_type):
                return value
            self.use_runtime("sa_callable_retain")
            retained = self.next_temp()
            self.emit(f"  {retained} = call ptr @sa_callable_retain(ptr {value.value})")
            return LLVMValue("ptr", retained, return_type)
        # ENTITY / ERROR 聚合：能接管就接管，否则零初始化一份新的再逐字段深拷贝
        if isinstance(expr, ast.VarRef | ast.Deref | ast.Index):
            source_ptr = self.lvalue_ptr(expr)
        else:
            value = self.cast_value(self.expr(expr), return_type)
            if self.adopt_temp_cleanup(value.value, return_type):
                return value
            source_ptr = self.alloca(llvm_ty)
            self.emit(f"  store {llvm_ty} {value.value}, ptr {source_ptr}")
        target_ptr = self.alloca(llvm_ty)
        self.emit(f"  store {llvm_ty} zeroinitializer, ptr {target_ptr}")
        if is_error(return_type):
            self.use_runtime("sa_set_error")
            self.emit(f"  call void @sa_set_error(ptr {target_ptr}, ptr {source_ptr})")
        else:
            self.emit_entity_copy(target_ptr, source_ptr, return_type)
        result = self.next_temp()
        self.emit(f"  {result} = load {llvm_ty}, ptr {target_ptr}")
        return LLVMValue(llvm_ty, result, return_type)

    def store_string(self, target_ptr: str, value: LLVMValue) -> None:
        """STRING 存入变量/字段/元素：值若是本语句的堆临时量就直接接管（免一次 strdup+free），
        否则 sa_set_string 拷贝。新值先算完再 free 旧值，`s = F"[{s}]"` 这种自引用才不会 UAF。"""
        if self.adopt_temp_cleanup(value.value, ast.TypeSpec("STRING")):
            old = self.next_temp()
            self.emit(f"  {old} = load ptr, ptr {target_ptr}")
            self.use_runtime("free")
            self.emit(f"  call void @free(ptr {old})")
            self.emit(f"  store ptr {value.value}, ptr {target_ptr}")
            return
        self.use_runtime("sa_set_string")
        self.emit(f"  call void @sa_set_string(ptr {target_ptr}, ptr {value.value})")

    def store_entity(self, target_ptr: str, expr: ast.Expr, type_spec: ast.TypeSpec) -> None:
        """带托管字段的 ENTITY 赋值：左值来源逐字段深拷贝；SUB 返回的聚合临时量释放旧字段后整体接管，
        否则先拷贝、临时量在语句尾释放。"""
        if isinstance(expr, ast.VarRef | ast.Deref | ast.Index):
            self.emit_entity_copy(target_ptr, self.lvalue_ptr(expr), type_spec)
            return
        llvm_ty = self.llvm_type(type_spec)
        value = self.cast_value(self.expr(expr), type_spec)
        if self.adopt_temp_cleanup(value.value, type_spec):
            self.emit_entity_free(target_ptr, type_spec)
            self.emit(f"  store {llvm_ty} {value.value}, ptr {target_ptr}")
            return
        source_ptr = self.alloca(llvm_ty)
        self.emit(f"  store {llvm_ty} {value.value}, ptr {source_ptr}")
        self.emit_entity_copy(target_ptr, source_ptr, type_spec)

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
        if var_ty not in {"i64", "double", "float"}:
            raise SonCompileError("native 后端 FOR 循环变量必须是数值类型", stmt.line_no)
        self.emit(self.source_comment(stmt.line_no))
        # 边界与步长只求一次；协程恢复可能绕过这里，额外保存到堆帧。
        self.begin_stmt()
        start = self.for_cast(self.expr(stmt.start), var_ty)
        self.emit(f"  store {var_ty} {start.value}, ptr {slot.ptr}")
        end = self.for_cast(self.expr(stmt.end), var_ty)
        if stmt.step is not None:
            step = self.for_cast(self.expr(stmt.step), var_ty)
        else:
            step = LLVMValue(var_ty, "1" if var_ty == "i64" else "1.0")
        end_ptr = step_ptr = None
        if getattr(self, "async_context", None) is not None:
            end_ptr, step_ptr = self.alloca(var_ty), self.alloca(var_ty)
            self.emit(f"  store {var_ty} {end.value}, ptr {end_ptr}")
            self.emit(f"  store {var_ty} {step.value}, ptr {step_ptr}")
        self.end_stmt()
        cond_label = self.unique_label("for_cond")
        body_label = self.unique_label("for_body")
        end_label = self.unique_label("for_end")
        self.emit(f"  br label %{cond_label}")

        self.emit(f"{cond_label}:")
        self.terminated = False
        if end_ptr is not None:
            end_value, step_value = self.next_temp(), self.next_temp()
            self.emit(f"  {end_value} = load {var_ty}, ptr {end_ptr}")
            self.emit(f"  {step_value} = load {var_ty}, ptr {step_ptr}")
            end, step = LLVMValue(var_ty, end_value), LLVMValue(var_ty, step_value)
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
            # fcmp 对 float/double 写法相同，只差类型串，跟着循环变量走
            self.emit(f"  {pos} = fcmp oge {var_ty} {step.value}, 0.0")
            self.emit(f"  {le} = fcmp ole {var_ty} {cur}, {end.value}")
            self.emit(f"  {ge} = fcmp oge {var_ty} {cur}, {end.value}")
        self.emit(f"  {cond} = select i1 {pos}, i1 {le}, i1 {ge}")
        self.emit(f"  br i1 {cond}, label %{body_label}, label %{end_label}")
        self.terminated = True

        self.emit(f"{body_label}:")
        self.terminated = False
        self.run_block(stmt.body)
        if not self.terminated:
            if step_ptr is not None:
                step_value = self.next_temp()
                self.emit(f"  {step_value} = load {var_ty}, ptr {step_ptr}")
                step = LLVMValue(var_ty, step_value)
            nv = self.next_temp()
            inc = self.next_temp()
            self.emit(f"  {nv} = load {var_ty}, ptr {slot.ptr}")
            if var_ty == "i64":
                self.emit(f"  {inc} = add i64 {nv}, {step.value}")
            else:
                self.emit(f"  {inc} = fadd {var_ty} {nv}, {step.value}")
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
