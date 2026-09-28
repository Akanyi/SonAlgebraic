from __future__ import annotations

from dataclasses import replace

from ...analysis.typesys import is_handle, is_promise, is_string
from ...core import ast
from ...core.errors import SonCompileError
from .base import LLVMValue, NativeGenBase


class PromiseMixin(NativeGenBase):
    """Promise 是单消费者的 i64 句柄，临时值与局部值都必须明确移交释放责任。"""

    def temp_cleanup_line(self, value: str, type_spec: ast.TypeSpec) -> str | None:
        if is_promise(type_spec):
            return f"  call void @sa_promise_release(i64 {value})"
        return super().temp_cleanup_line(value, type_spec)

    def register_temp_cleanup(self, value: str, type_spec: ast.TypeSpec) -> None:
        if is_promise(type_spec):
            self.use_runtime("sa_promise_release")
            self.add_temp_cleanup(self.temp_cleanup_line(value, type_spec))
            return
        super().register_temp_cleanup(value, type_spec)

    def emit_free_value(self, value: str, type_spec: ast.TypeSpec) -> None:
        if is_promise(type_spec):
            self.use_runtime("sa_promise_release")
            self.emit(self.temp_cleanup_line(value, type_spec))
            return
        super().emit_free_value(value, type_spec)

    def async_call_expr(self, sub: ast.Subroutine, args: list[ast.Expr], name: str, c_abi: bool = False) -> LLVMValue:
        """name 是已解析的函数符号、不含 _start；本地 ABI 与外部 C ABI 在实参处区分。"""
        values = self.call_args(sub, args, c_abi=c_abi)
        promise_type = ast.TypeSpec("PROMISE", inner=sub.return_type)
        start = f"{name}_start"
        if self.promise_needs_throw_cleanup():
            signature = replace(sub, return_type=promise_type, is_async=False)
            value = self.wrap_call_expr_with_throw_cleanup(start, signature, values, raw_name=True, c_abi=c_abi)
        else:
            result = self.next_temp()
            self.emit(f"  {result} = call i64 @{start}({', '.join(values)})")
            value = LLVMValue("i64", result, promise_type)
        self.adopt_promise_arguments(sub.params, values)
        self.register_temp_cleanup(value.value, promise_type)
        return value

    def adopt_promise_arguments(self, params, values: list[str], result: LLVMValue | None = None) -> None:
        # 等全部实参求完且调用成功后才移交；后续实参抛错时，前面的 Promise 仍可回收。
        # async start 接管参数；同步 SUB 的参数是借用，仅返回原句柄时移交给返回值。
        if result is not None and not is_promise(result.type_spec or ast.TypeSpec("VOID")):
            return
        for param, value in zip(params, values):
            if not param.by_ref and is_promise(param.type_spec):
                handle = value.split(" ", 1)[1]
                if self.adopt_temp_cleanup(handle, param.type_spec) and result is not None:
                    # pass(a, b) 可能返回任意一个参数，也可能返回新任务；未返回的临时照常释放。
                    same, unused = self.next_temp(), self.next_temp()
                    self.emit(f"  {same} = icmp eq i64 {handle}, {result.value}")
                    self.emit(f"  {unused} = select i1 {same}, i64 0, i64 {handle}")
                    self.use_runtime("sa_promise_release")
                    self.emit(f"  call void @sa_promise_release(i64 {unused})")

    def promise_needs_throw_cleanup(self) -> bool:
        # resume 的最外层 landing pad 会 reject 并统一清帧；再加同步垫会重复析构帧字段。
        return getattr(self, "async_context", None) is None and (
            self.has_active_resources() or any(self.temp_cleanup)
        )

    def sync_expr(self, expr: ast.SyncExpr) -> LLVMValue:
        cleanup_start = len(self.temp_cleanup[-1]) if self.temp_cleanup else 0
        promise = self.expr(expr.operand)
        promise_type = self.type_of_expr(expr.operand)
        self.adopt_temp_cleanup(promise.value, promise_type)
        # start 已复制实参。take 失败会 longjmp，必须在驱动/取值前释放这些临时量，
        # 同时保留外层表达式仍在使用的临时值（例如拼接表达式的左操作数）。
        if self.temp_cleanup:
            argument_cleanup = self.temp_cleanup[-1][cleanup_start:]
            del self.temp_cleanup[-1][cleanup_start:]
            for line in argument_cleanup:
                self.emit(line)
        self.use_runtime("sa_event_loop_run_until")
        self.emit(f"  call void @sa_event_loop_run_until(i64 {promise.value})")
        return self.promise_take_value(promise.value, promise_type.inner or ast.TypeSpec("VOID"))

    def promise_take_value(self, handle: str, inner: ast.TypeSpec) -> LLVMValue:
        """成功取值立即释放句柄；失败由 runtime 释放并原样重抛 SaError。"""
        if inner.name == "VOID":
            function, result_type = "sa_promise_take_void", "void"
        elif is_string(inner):
            function, result_type = "sa_promise_take_str", "ptr"
        elif is_handle(inner):
            function, result_type = "sa_promise_take_handle", "i64"
        elif inner.name == "BOOL" or (inner.name == "NUM" and inner.subtype == "LONG"):
            function, result_type = "sa_promise_take_long", "i64"
        elif inner.name == "NUM":
            function, result_type = "sa_promise_take_double", "double"
        else:
            raise SonCompileError("AWAIT/SYNC 暂不支持该 PROMISE 结果类型")
        self.adopt_temp_cleanup(handle, ast.TypeSpec("PROMISE", inner=inner))
        self.use_runtime(function)
        self.use_runtime("sa_promise_release")
        guard = self.promise_needs_throw_cleanup()
        pending_cleanup = [line for group in self.temp_cleanup for line in group]
        if guard:
            env, frame, status, normal = (self.next_temp() for _ in range(4))
            try_label = self.unique_label("promise_take")
            catch_label = self.unique_label("promise_cleanup")
            end_label = self.unique_label("promise_done")
            for symbol in ("sa_try_push_env", "llvm.frameaddress", "_setjmp", "sa_try_pop", "sa_throw_dispatch"):
                self.use_runtime(symbol)
            self.emit(f"  {env} = call ptr @sa_try_push_env()")
            self.emit(f"  {frame} = call ptr @llvm.frameaddress.p0(i32 0)")
            self.emit(f"  {status} = call i32 @_setjmp(ptr {env}, ptr {frame})")
            self.emit(f"  {normal} = icmp eq i32 {status}, 0")
            self.emit(f"  br i1 {normal}, label %{try_label}, label %{catch_label}")
            self.emit(f"{try_label}:")
        result = self.next_temp() if result_type != "void" else ""
        assignment = f"{result} = " if result else ""
        self.emit(f"  {assignment}call {result_type} @{function}(i64 {handle})")
        self.emit(f"  call void @sa_promise_release(i64 {handle})")
        if guard:
            # alloca 位于 entry：setjmp 的失败边不使用正常分支中产生的 SSA 结果。
            result_ptr = self.alloca(result_type) if result else None
            if result_ptr is not None:
                self.emit(f"  store {result_type} {result}, ptr {result_ptr}")
            self.emit("  call void @sa_try_pop()")
            self.emit(f"  br label %{end_label}")
            self.emit(f"{catch_label}:")
            self.emit("  call void @sa_try_pop()")
            self.emit_pending_temp_cleanup(pending_cleanup)
            self.emit_active_cleanup()
            self.emit("  call void @sa_throw_dispatch()")
            self.emit("  unreachable")
            self.emit(f"{end_label}:")
            if result_ptr is not None:
                result = self.next_temp()
                self.emit(f"  {result} = load {result_type}, ptr {result_ptr}")
        self.terminated = False
        value = LLVMValue(result_type, result, inner)
        if inner.name == "BOOL":
            value = self.truthy(value)
        elif inner.name == "NUM" and inner.subtype == "FLOAT":
            value = self.cast_to_float(value)
        if is_string(inner):
            self.register_temp_cleanup(value.value, inner)
        return value
