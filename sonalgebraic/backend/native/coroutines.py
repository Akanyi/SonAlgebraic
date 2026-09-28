"""手写 LLVM 无栈协程：堆帧保存数据，resume 的 switch 保存控制流。"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

from ...analysis.typesys import is_error, is_sub_type, is_symbol
from ...core import ast
from ...core.errors import SonCompileError
from .base import LLVMValue, NativeGenBase, VarSlot, sub_gosub_lines


@dataclass
class CoroResource:
    slot: VarSlot
    index: int
    borrowed_ptr: str
    borrowed_index: int
    cleanup_name: str


@dataclass
class AsyncContext:
    sub: ast.Subroutine
    frame_type: str
    fields: list[tuple[str, str]] = field(default_factory=list)
    resources: dict[str, CoroResource] = field(default_factory=dict)
    temporaries: dict[str, CoroResource] = field(default_factory=dict)
    resume_points: list[str] = field(default_factory=list)
    params: list[VarSlot] = field(default_factory=list)


class CoroutinesMixin(NativeGenBase):
    async_context: AsyncContext | None = None

    def require_async_frame_type(self, kind: ast.TypeSpec, line_no: int, seen: frozenset[str] = frozenset()) -> None:
        if kind.name != "ENTITY" or kind.subtype in seen:
            return
        entity = self.resolve_entity_def(kind)
        if entity is None:
            return
        for member in entity.fields:
            field_type = member.type_spec
            if field_type.name == "PROMISE" or (field_type.array_size is not None and self.type_has_managed_resources(field_type)):
                raise SonCompileError("native ASYNC 帧暂不支持含 PROMISE 或托管数组字段的 ENTITY", line_no)
            self.require_async_frame_type(field_type, line_no, seen | {kind.subtype})

    def validate_async_jumps(self, sub: ast.Subroutine) -> None:
        labels: dict[str, tuple[int, ...]] = {}
        jumps: list[tuple[ast.Goto | ast.Gosub, tuple[int, ...]]] = []

        def visit(body: list[ast.Stmt], scope: tuple[int, ...]) -> None:
            for stmt in body:
                if isinstance(stmt, ast.Label):
                    labels[stmt.name.lower()] = scope
                elif isinstance(stmt, ast.Goto | ast.Gosub):
                    jumps.append((stmt, scope))
                elif isinstance(stmt, ast.If):
                    visit(stmt.body, (*scope, id(stmt.body)))
                    for branch in stmt.elifs:
                        visit(branch.body, (*scope, id(branch.body)))
                    visit(stmt.else_body, (*scope, id(stmt.else_body)))
                elif isinstance(stmt, ast.ForLoop | ast.WhileLoop):
                    visit(stmt.body, (*scope, id(stmt.body)))
                elif isinstance(stmt, ast.TryCatch):
                    for branch in stmt.catches:
                        visit(branch.body, (*scope, id(branch.body)))

        visit(sub.body, ())
        for stmt, scope in jumps:
            if labels.get(stmt.label.lower()) != scope:
                raise SonCompileError("native ASYNC SUB 的 GOTO/GOSUB 暂不支持跨 IF/FOR/WHILE/TRY 作用域跳转", stmt.line_no)

    def stmt(self, stmt: ast.Stmt) -> None:
        if self.async_context is not None and self.terminated and not isinstance(stmt, ast.Label | ast.NoOp):
            # 标签可从别处跳入后面的代码；继续收集帧槽，但把落空路径放入不可达块。
            self.emit(f"{self.unique_label('coro_unreachable')}:")
            self.terminated = False
        super().stmt(stmt)

    def cast_value(self, value: LLVMValue, target: ast.TypeSpec) -> LLVMValue:
        # ERROR 表达式历史上返回结构地址，但值传 ABI 需要真正的聚合值。
        if is_error(target):
            if value.type_name == "ptr":
                copied = self.next_temp()
                self.emit(f"  {copied} = load %SaError, ptr {value.value}")
                return LLVMValue("%SaError", copied, target)
            if value.type_name == "%SaError":
                return value
        return super().cast_value(value, target)

    def local_declaration(self, stmt: ast.LocalDeclaration) -> None:
        if self.async_context is not None:
            self.require_async_frame_type(stmt.type_spec, stmt.line_no)
        start = len(self.lines)
        super().local_declaration(stmt)
        if self.async_context is not None:
            slot = self.slots[stmt.name.lower()]
            resource = self.async_context.resources.get(slot.ptr)
            if resource is not None:
                # GOTO 回到声明也会重新初始化；旧实例必须先释放，不能覆盖堆指针。
                self.lines.insert(start, f"  call void @{resource.cleanup_name}(ptr %sa_frame)")

    def run_block(self, body: list[ast.Stmt]) -> None:
        saved = self.slots.copy() if self.async_context is not None else None
        super().run_block(body)
        if saved is not None:
            self.slots = saved

    def alloca(self, llvm_type: str, name: str | None = None) -> str:
        ctx = self.async_context
        if ctx is None:
            return super().alloca(llvm_type, name)
        # 每次进入 resume 都重新计算帧地址，因而这些 GEP 支配所有 switch 恢复入口。
        ptr = name or self.next_temp()
        if any(existing == ptr for _, existing in ctx.fields):
            ptr = self.next_temp()
        ctx.fields.append((llvm_type, ptr))
        index = len(ctx.fields)
        self.entry_allocas.append(f"  {ptr} = getelementptr inbounds {ctx.frame_type}, ptr %sa_frame, i64 0, i32 {index}")
        if llvm_type == "{ ptr, i64, i64 }":
            # StringBuilder 的首字段就是 data；take 会将它清空，异常时仍需兜底。
            resource = self.coro_resource(VarSlot("", ast.TypeSpec("STRING"), ptr))
            self.emit(f"  call void @{resource.cleanup_name}(ptr %sa_frame)")
        return ptr

    def coro_resource(self, slot: VarSlot) -> CoroResource:
        ctx = self.async_context
        assert ctx is not None
        if slot.ptr not in ctx.resources:
            index = next(i for i, (_, ptr) in enumerate(ctx.fields, 1) if ptr == slot.ptr)
            flag = self.alloca("i1")
            ctx.resources[slot.ptr] = CoroResource(
                slot, index, flag, len(ctx.fields),
                f"{self.sub_name(ctx.sub.name)}_release_{index}",
            )
        return ctx.resources[slot.ptr]

    def register_owned(self, slot: VarSlot) -> None:
        if self.async_context is not None:
            self.coro_resource(slot)
        super().register_owned(slot)

    def emit_free_slot(self, slot: VarSlot) -> None:
        if self.async_context is not None and slot.ptr in self.async_context.resources:
            resource = self.async_context.resources[slot.ptr]
            self.emit(f"  call void @{resource.cleanup_name}(ptr %sa_frame)")
            return
        super().emit_free_slot(slot)

    def emit_active_cleanup(self) -> None:
        # 异常可能被当前协程内的 TRY 捕获，不能提前销毁整个帧；真正终结由 settle 兜底。
        if self.async_context is None:
            super().emit_active_cleanup()

    def add_temp_cleanup(self, line: str) -> None:
        if self.async_context is not None:
            match = re.fullmatch(r"  call void @(free|sa_symbol_free|sa_callable_release|sa_promise_release)\((?:ptr|i64) ([^ )]+)\)", line)
            if match:
                kind = {"free": "STRING", "sa_symbol_free": "SYMBOL", "sa_callable_release": "SUB", "sa_promise_release": "PROMISE"}[match[1]]
                self.register_temp_cleanup(match[2], ast.TypeSpec(kind))
                return
        super().add_temp_cleanup(line)

    def register_temp_cleanup(self, value: str, type_spec: ast.TypeSpec) -> None:
        ctx = self.async_context
        if ctx is None:
            super().register_temp_cleanup(value, type_spec)
            return
        if not self.temp_cleanup or value in ctx.temporaries:
            return
        if not self.type_has_managed_resources(type_spec) and type_spec.name != "PROMISE":
            return
        ptr = self.alloca(self.llvm_type(type_spec))
        resource = self.coro_resource(VarSlot("", type_spec, ptr))
        # 内层 CATCH 后可能再次执行同一求值点，先处理上轮异常留下的临时所有权。
        self.emit(f"  call void @{resource.cleanup_name}(ptr %sa_frame)")
        self.emit(f"  store {self.llvm_type(type_spec)} {value}, ptr {ptr}")
        ctx.temporaries[value] = resource
        # 释放过程放到小型 IR helper，异常分支和成功分支可复用，避免复制 SSA 定义。
        super().add_temp_cleanup(f"  call void @{resource.cleanup_name}(ptr %sa_frame)")

    def adopt_temp_cleanup(self, value: str, type_spec: ast.TypeSpec) -> bool:
        ctx = self.async_context
        if ctx is None:
            return super().adopt_temp_cleanup(value, type_spec)
        resource = ctx.temporaries.get(value)
        if resource is None or not self.temp_cleanup:
            return False
        line = f"  call void @{resource.cleanup_name}(ptr %sa_frame)"
        if line not in self.temp_cleanup[-1]:
            return False
        self.temp_cleanup[-1].remove(line)
        self.emit(f"  store {self.llvm_type(type_spec)} zeroinitializer, ptr {resource.slot.ptr}")
        del ctx.temporaries[value]
        return True

    def ownership_assign_stmt(self, stmt: ast.Assign) -> None:
        super().ownership_assign_stmt(stmt)
        if self.async_context is not None and stmt.mode == "borrow":
            target = self.slot(stmt.target.name, stmt.line_no)
            resource = self.async_context.resources[target.ptr]
            self.emit(f"  store i1 true, ptr {resource.borrowed_ptr}")

    def symbol_expr(self, expr: ast.Expr) -> LLVMValue:
        if self.async_context is None:
            return super().symbol_expr(expr)
        # 子树在右操作数求值失败时仍有主人；op 接管后立即清空两个临时所有权槽。
        power = isinstance(expr, ast.CallExpr) and self.is_math_function(expr.name, "POW")
        if (isinstance(expr, ast.Binary) and expr.op in {"+", "-", "*", "/", "**"}) or power:
            left = self.symbol_expr(expr.args[0] if power else expr.left)
            right = self.symbol_expr(expr.args[1] if power else expr.right)
            result = self.next_temp()
            op = "^" if power or expr.op == "**" else expr.op
            self.use_runtime("sa_symbol_op")
            self.emit(f"  {result} = call ptr @sa_symbol_op(i8 {ord(op)}, ptr {left.value}, ptr {right.value})")
            kind = ast.TypeSpec("SYMBOL")
            self.adopt_temp_cleanup(left.value, kind)
            self.adopt_temp_cleanup(right.value, kind)
            value = LLVMValue("ptr", result, kind)
        else:
            value = super().symbol_expr(expr)
        self.register_temp_cleanup(value.value, ast.TypeSpec("SYMBOL"))
        return value

    def await_expr(self, expr: ast.AwaitExpr) -> LLVMValue:
        ctx = self.async_context
        if ctx is None:
            raise SonCompileError("AWAIT 只能在 ASYNC SUB 内使用", expr.line_no)
        handle = self.expr(expr.operand)
        self.adopt_temp_cleanup(handle.value, self.type_of_expr(expr.operand))
        # start 已经完成按值快照，实参临时量不能带着旧 SSA 值留到下次 resume。
        self.emit(f"  store i64 {handle.value}, ptr %sa_coro_awaited")
        for cleanup in self.temp_cleanup[-1]:
            self.emit(cleanup)
        self.temp_cleanup[-1].clear()
        point = len(ctx.resume_points) + 1
        label = self.unique_label("await_resume")
        ctx.resume_points.append(label)
        self.emit(f"  store i32 {point}, ptr %sa_coro_state")
        self.use_runtime("sa_try_pop")
        self.use_runtime("sa_coro_await")
        self.emit("  call void @sa_try_pop()")
        self.emit(f"  call void @sa_coro_await(ptr %sa_frame, i64 {handle.value})")
        self.emit("  ret void")
        self.emit(f"{label}:")
        self.terminated = False
        resumed = self.next_temp()
        self.emit(f"  {resumed} = load i64, ptr %sa_coro_awaited")
        # take 失败会释放句柄并重抛；清零后 reject 的帧清理不会重复消费该子任务。
        self.emit("  store i64 0, ptr %sa_coro_awaited")
        inner = self.type_of_expr(expr.operand).inner or ast.TypeSpec("VOID")
        return self.promise_take_value(resumed, inner)

    def return_stmt(self, stmt: ast.Return) -> None:
        if self.async_context is None:
            super().return_stmt(stmt)
            return
        self.emit(self.source_comment(stmt.line_no))
        if stmt.expr is None and self.current_gosub_lines:
            self.emit_gosub_return_dispatch()
        kind = self.async_context.sub.return_type
        self.begin_stmt()
        value = None
        if stmt.expr is not None:
            value = self.owned_return_value(stmt.expr, kind) if self.is_string_scalar(kind) else self.cast_value(self.expr(stmt.expr), kind)
        self.end_stmt()
        handle = self.next_temp()
        self.emit(f"  {handle} = load i64, ptr %sa_coro_self")
        args = f"i64 {handle}"
        if value is None:
            suffix = "void"
        elif self.is_string_scalar(kind):
            suffix = "str"
            args += f", ptr {value.value}"
        elif kind.name == "HANDLE":
            suffix = "handle"
            args += f", i64 {value.value}"
        elif kind.name == "NUM" and kind.subtype != "LONG":
            suffix = "double"
            args += f", double {self.cast_to_double(value).value}"
        else:
            suffix = "long"
            args += f", i64 {self.cast_to_i64(value).value}"
        self.use_runtime("sa_try_pop")
        self.use_runtime(f"sa_promise_fulfill_{suffix}")
        self.emit("  call void @sa_try_pop()")
        self.emit(f"  call void @sa_promise_fulfill_{suffix}({args})")
        self.emit("  ret void")
        self.terminated = True

    def async_subroutine(self, sub: ast.Subroutine) -> str:
        self.validate_async_jumps(sub)
        ctx = AsyncContext(sub, f"%SaCoro_{self.sub_name(sub.name)}")
        self.current_sub = sub
        self.lines, self.entry_allocas = [], []
        self.terminated = False
        self.slots = self.global_slots()
        self.scope_resources, self.temp_cleanup = [], []
        self.async_context = ctx
        self.push_scope()
        self.current_gosub_lines = sub_gosub_lines(sub)
        self.gosub_stack_ptr = self.alloca("[64 x i64]") if self.current_gosub_lines else None
        self.gosub_top_ptr = self.alloca("i64") if self.current_gosub_lines else None
        for param in sub.params:
            if param.by_ref:
                raise SonCompileError("ASYNC SUB 不支持 AS REF 参数", param.line_no)
            self.require_async_frame_type(param.type_spec, param.line_no)
            slot = VarSlot(param.name, param.type_spec, self.alloca(self.llvm_type(param.type_spec)))
            ctx.params.append(slot)
            self.slots[param.name.lower()] = slot
            if self.type_has_managed_resources(param.type_spec) or param.type_spec.name == "PROMISE":
                self.register_owned(slot)
        self.emit("sa_coro_body:")
        for stmt in sub.body:
            self.stmt(stmt)
        if not self.terminated:
            if sub.return_type.name != "VOID":
                # 语义已保证非 VOID 的真实路径必返；结构化 IF 的空汇合块不可达。
                self.emit("  unreachable")
            else:
                self.return_stmt(ast.Return(sub.line_no, None))
        body = self.lines
        gep_lines = self.entry_allocas
        self.async_context = None
        self.scope_resources, self.temp_cleanup = [], []
        self.current_sub = None
        self.current_gosub_lines = []
        self.gosub_stack_ptr = self.gosub_top_ptr = None
        self.async_type_declarations.append(f"{ctx.frame_type} = type {{ %SaCoroBase{''.join(', ' + ty for ty, _ in ctx.fields)} }}")
        return "\n\n".join([self.coro_resume(ctx, gep_lines, body), self.coro_cleanup(ctx), self.coro_start(ctx)]) + "\n"

    def coro_base_geps(self) -> list[str]:
        return [f"  %sa_coro_{name} = getelementptr inbounds %SaCoroBase, ptr %sa_frame, i64 0, i32 {index}" for name, index in (("state", 0), ("self", 3), ("awaited", 4))]

    def coro_resume(self, ctx: AsyncContext, geps: list[str], body: list[str]) -> str:
        for name in ("sa_try_push_env", "llvm.frameaddress", "_setjmp", "sa_try_pop", "sa_promise_reject_error", "sa_current_error", "sa_error_clear"):
            self.use_runtime(name)
        cases = " ".join(["i32 0, label %sa_coro_body", *(f"i32 {i}, label %{label}" for i, label in enumerate(ctx.resume_points, 1))])
        return "\n".join([
            f"define void @{self.sub_name(ctx.sub.name)}_resume(ptr %sa_frame) {{", "entry:",
            *self.coro_base_geps(), *geps,
            "  %sa_env = call ptr @sa_try_push_env()",
            "  %sa_native_frame = call ptr @llvm.frameaddress.p0(i32 0)",
            "  %sa_sj = call i32 @_setjmp(ptr %sa_env, ptr %sa_native_frame)",
            "  %sa_ok = icmp eq i32 %sa_sj, 0",
            "  br i1 %sa_ok, label %sa_coro_dispatch, label %sa_coro_reject",
            "sa_coro_reject:", "  call void @sa_try_pop()",
            "  %sa_failed = load i64, ptr %sa_coro_self",
            "  call void @sa_promise_reject_error(i64 %sa_failed, ptr @sa_current_error)",
            "  call void @sa_error_clear(ptr @sa_current_error)", "  ret void",
            "sa_coro_dispatch:", "  %sa_state = load i32, ptr %sa_coro_state",
            f"  switch i32 %sa_state, label %sa_coro_invalid [ {cases} ]",
            "sa_coro_invalid:", "  call void @sa_try_pop()", "  ret void", *body, "}",
        ])

    def coro_cleanup(self, ctx: AsyncContext) -> str:
        chunks = []
        for resource in ctx.resources.values():
            self.lines, self.entry_allocas = [], []
            self.emit(f"define internal void @{resource.cleanup_name}(ptr %sa_frame) {{")
            self.emit("entry:")
            self.emit(f"  %sa_slot = getelementptr inbounds {ctx.frame_type}, ptr %sa_frame, i64 0, i32 {resource.index}")
            self.emit(f"  %sa_flag = getelementptr inbounds {ctx.frame_type}, ptr %sa_frame, i64 0, i32 {resource.borrowed_index}")
            self.emit("  %sa_borrowed = load i1, ptr %sa_flag")
            self.emit("  br i1 %sa_borrowed, label %done, label %release")
            self.emit("release:")
            super().emit_free_slot(VarSlot("", resource.slot.type_spec, "%sa_slot"))
            self.emit("  br label %done")
            self.emit("done:")
            self.emit(f"  store {self.llvm_type(resource.slot.type_spec)} zeroinitializer, ptr %sa_slot")
            self.emit("  store i1 false, ptr %sa_flag")
            self.emit("  ret void")
            self.emit("}")
            chunks.append("\n".join(self.lines))
        chunks.append("\n".join([
            f"define void @{self.sub_name(ctx.sub.name)}_cleanup(ptr %sa_frame) {{", "entry:",
            *(f"  call void @{resource.cleanup_name}(ptr %sa_frame)" for resource in reversed(list(ctx.resources.values()))),
            "  ret void", "}",
        ]))
        return "\n\n".join(chunks)

    def coro_start(self, ctx: AsyncContext) -> str:
        self.lines, self.entry_allocas = [], []
        name = self.sub_name(ctx.sub.name)
        self.emit(f"define i64 @{name}_start({', '.join(self.param_decl(p) for p in ctx.sub.params)}) {{")
        self.emit("entry:")
        entry = len(self.lines)
        for runtime in ("calloc", "exit", "sa_promise_alloc", "sa_coro_schedule"):
            self.use_runtime(runtime)
        self.emit(f"  %sa_size_ptr = getelementptr {ctx.frame_type}, ptr null, i64 1")
        self.emit("  %sa_size = ptrtoint ptr %sa_size_ptr to i64")
        self.emit("  %sa_frame = call ptr @calloc(i64 1, i64 %sa_size)")
        self.emit("  %sa_oom = icmp eq ptr %sa_frame, null")
        self.emit("  br i1 %sa_oom, label %oom, label %init")
        self.emit("oom:")
        self.emit("  call void @exit(i32 1)")
        self.emit("  unreachable")
        self.emit("init:")
        for field_index, suffix in ((1, "resume"), (2, "cleanup")):
            ptr = self.next_temp()
            self.emit(f"  {ptr} = getelementptr inbounds %SaCoroBase, ptr %sa_frame, i64 0, i32 {field_index}")
            self.emit(f"  store ptr @{name}_{suffix}, ptr {ptr}")
        for param, slot in zip(ctx.sub.params, ctx.params):
            index = next(i for i, (_, ptr) in enumerate(ctx.fields, 1) if ptr == slot.ptr)
            target = self.next_temp()
            self.emit(f"  {target} = getelementptr inbounds {ctx.frame_type}, ptr %sa_frame, i64 0, i32 {index}")
            kind, value = param.type_spec, f"%{self.c_ident(param.name)}"
            if self.is_string_scalar(kind) or is_symbol(kind) or is_sub_type(kind):
                runtime = "sa_strdup" if self.is_string_scalar(kind) else "sa_symbol_clone" if is_symbol(kind) else "sa_callable_retain"
                self.use_runtime(runtime)
                copied = self.next_temp()
                self.emit(f"  {copied} = call ptr @{runtime}(ptr {value})")
                self.emit(f"  store ptr {copied}, ptr {target}")
            elif is_error(kind) or self.is_entity_scalar(kind) and self.type_has_managed_resources(kind):
                source = self.alloca(self.llvm_type(kind))
                self.emit(f"  store {self.llvm_type(kind)} {value}, ptr {source}")
                if is_error(kind):
                    self.use_runtime("sa_set_error")
                    self.emit(f"  call void @sa_set_error(ptr {target}, ptr {source})")
                else:
                    self.emit_entity_copy(target, source, kind)
            else:
                self.emit(f"  store {self.llvm_type(kind)} {value}, ptr {target}")
        self.emit("  %sa_promise = call i64 @sa_promise_alloc(ptr %sa_frame)")
        self.emit("  call void @sa_coro_schedule(i64 %sa_promise)")
        self.emit("  ret i64 %sa_promise")
        self.emit("}")
        self.lines[entry:entry] = self.entry_allocas
        return "\n".join(self.lines)
