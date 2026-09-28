"""协程发射。同步 codegen 通过 async_frame_field 这一个闸口被复用到协程体里。"""
from __future__ import annotations

import re

from ...analysis.typesys import is_error, is_handle, is_promise, is_string, is_sub_type, is_symbol
from ...core import ast
from ...core.errors import SonCompileError
from .base import AsyncFrameCtx, CGenBase


class CoroutinesMixin(CGenBase):
    """ASYNC SUB 展开成无栈状态机：帧结构体、resume / cleanup / start 三件套、AWAIT / SYNC。"""

    # ===================== ASYNC SUB：无栈状态机协程 =====================
    # 一个 ASYNC SUB 不编译成「从头跑到尾的函数」，而是三样东西：帧结构体（存跨 AWAIT
    # 存活的状态）、resume 函数（顶部 switch 跳回上次挂起点）、start 函数（分配帧+promise、
    # 投入就绪队列、返回 promise）。运行时（c_runtime 的 SA_ENABLE_ASYNC 块）提供 promise
    # 槽位、就绪队列、事件循环。

    def async_frame_type(self, name: str) -> str:
        return f"SaCoro_{self.c_ident(name)}"

    def local_resource_cleanup_lines(self, resources: list[tuple[str, ast.TypeSpec]], indent: int) -> list[str]:
        lines: list[str] = []
        for name, type_spec in reversed(resources):
            if self.async_frame_stack and re.fullmatch(r"sa_tmp_\d+(?:_result)?", name):
                self.async_frame_stack[-1].temp_resources[name] = type_spec
            lines.extend(super().local_resource_cleanup_lines([(name, type_spec)], indent))
            # THROW、块退出和终结兜底可能经过同一个字段，实际所有权只能释放一次。
            if self.async_frame_stack and name.startswith("f->"):
                lines.append(f"{'    ' * indent}memset(&{name}, 0, sizeof({name}));")
        return lines

    def add_cleanup(self, line: str) -> None:
        # 旧表达式接口有少量直接登记单条释放行的调用，同样转换为类型化所有权元数据。
        if self.async_frame_stack:
            match = re.fullmatch(r"(free|sa_symbol_free|sa_callable_release)\((sa_tmp_\d+(?:_result)?)\);", line)
            if match:
                kind = {"free": "STRING", "sa_symbol_free": "SYMBOL", "sa_callable_release": "SUB"}[match[1]]
                self.async_frame_stack[-1].temp_resources[match[2]] = ast.TypeSpec(kind)
        super().add_cleanup(line)

    def hoist_async_temporaries(self, body: list[str], ctx: AsyncFrameCtx) -> tuple[list[str], list[str], list[tuple[str, str]]]:
        """只变换 C 代码词法片段，字符串、字符和注释原样保留；清理由所有权登记驱动。"""
        tokens = re.split(r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*)', "\n".join(body), flags=re.S)
        fields: dict[str, str] = {}
        declaration = re.compile(r"(?m)^(\s*)(char\s*\*|SaSymbol|SaError|SaCallable\s*\*|SaStringBuilder|SaHandle|SaEntity_\w+|long long|double|int)\s+(sa_tmp_\d+(?:_result)?)\s*(?==|;)")

        def declare(match: re.Match[str]) -> str:
            pad, kind, name = match.groups()
            fields[name] = kind
            # 无初始化声明只保留空语句；有初始化时保留赋值左值。
            following = match.string[match.end():].lstrip()
            return pad + (name + " " if following.startswith("=") else "")

        for i in range(0, len(tokens), 2):
            tokens[i] = declaration.sub(declare, tokens[i])
        for i in range(0, len(tokens), 2):
            code = tokens[i]
            # 聚合零初始化声明改成赋值后需要显式复合字面量类型。
            code = re.sub(r"\b(sa_tmp_\d+)\s*=\s*(?=\{)", lambda m: f"{m[1]} = ({fields[m[1]]})" if m[1] in fields else m[0], code)
            # 真正执行释放的位置清空指针；别名中间量没有清理登记，不进入兜底集合。
            code = re.sub(r"\b(free|sa_symbol_free|sa_callable_release)\((sa_tmp_\d+(?:_result)?(?:\.\w+)*)\);", lambda m: f"{m[0]} {m[2]} = NULL;", code)
            code = re.sub(r"\bsa_tmp_\d+(?:_result)?\b", lambda m: f"f->{m[0]}" if m[0] in fields else m[0], code)
            tokens[i] = code
        cleanup: list[str] = []
        for name, kind in reversed(list(ctx.temp_resources.items())):
            if name in fields:
                cleanup.extend(self.local_resource_cleanup_lines([(f"f->{name}", kind)], 1))
        for name, kind in fields.items():
            if kind == "SaStringBuilder":
                cleanup.append(f"    free(f->{name}.data);")
        return "".join(tokens).splitlines(), cleanup, [(kind, name) for name, kind in fields.items()]

    def adopt_temp_cleanup(self, value: str, type_spec: ast.TypeSpec, cleanup: list[str]) -> bool:
        adopted = super().adopt_temp_cleanup(value, type_spec, cleanup)
        if adopted and self.async_frame_stack:
            cleanup.extend(self.moved_source_reset_lines(value, type_spec, 0))
        return adopted

    def async_resume_c_name(self, name: str) -> str:
        return f"{self.c_ident(name)}_resume"

    def async_cleanup_c_name(self, name: str) -> str:
        return f"{self.c_ident(name)}_cleanup"

    def async_start_c_name(self, name: str) -> str:
        return f"{self.c_ident(name)}_start"

    def async_start_export_name(self, sub: ast.Subroutine) -> str:
        # 导出感知：模块内 PUBLIC async sub 的 start 用 sa_mod_<模块>_sub_<名>_start（非 static、
        # 可跨 TU 链接），私有 / 主文件 async sub 退回 sa_<名>_start（static）。命名刻意与普通
        # SUB 的 sub_c_name 对齐，好让跨模块调用端用 call_c_name 拼出同一个符号名。
        return f"{self.sub_c_name(sub.name)}_start"

    def async_start_params(self, sub: ast.Subroutine) -> str:
        return ", ".join(self.param_decl(param) for param in sub.params) or "void"

    def _collect_local_decls(self, stmt: ast.Stmt) -> list[ast.LocalDeclaration]:
        result: list[ast.LocalDeclaration] = []
        if isinstance(stmt, ast.LocalDeclaration):
            result.append(stmt)
        elif isinstance(stmt, ast.If):
            for inner in stmt.body:
                result.extend(self._collect_local_decls(inner))
            for branch in stmt.elifs:
                for inner in branch.body:
                    result.extend(self._collect_local_decls(inner))
            for inner in stmt.else_body:
                result.extend(self._collect_local_decls(inner))
        elif isinstance(stmt, ast.ForLoop | ast.WhileLoop):
            for inner in stmt.body:
                result.extend(self._collect_local_decls(inner))
        elif isinstance(stmt, ast.TryCatch):
            for branch in stmt.catches:
                result.append(ast.LocalDeclaration(branch.line_no, branch.alias, ast.TypeSpec("ERROR"), False, None))
                for inner in branch.body:
                    result.extend(self._collect_local_decls(inner))
        return result

    def _collect_for_loops(self, stmt: ast.Stmt) -> list[ast.ForLoop]:
        """递归收集一条语句里的所有 FOR（含嵌套），用于给 async 帧预留上界/步长字段。
        覆盖面必须和 for_stmt 生成时会遇到的 FOR 一致——两处都以本函数收集的 line_no
        集合为准，天然对齐，漏收的 FOR 两边都退回栈局部（那种只可能在 TRY 内，其中禁 AWAIT）。"""
        result: list[ast.ForLoop] = []
        if isinstance(stmt, ast.ForLoop):
            result.append(stmt)
            for inner in stmt.body:
                result.extend(self._collect_for_loops(inner))
        elif isinstance(stmt, ast.WhileLoop):
            for inner in stmt.body:
                result.extend(self._collect_for_loops(inner))
        elif isinstance(stmt, ast.If):
            for inner in stmt.body:
                result.extend(self._collect_for_loops(inner))
            for branch in stmt.elifs:
                for inner in branch.body:
                    result.extend(self._collect_for_loops(inner))
            for inner in stmt.else_body:
                result.extend(self._collect_for_loops(inner))
        elif isinstance(stmt, ast.TryCatch):
            for branch in stmt.catches:
                for inner in branch.body:
                    result.extend(self._collect_for_loops(inner))
        return result

    def _collect_for_loops_in_sub(self, sub: ast.Subroutine) -> list[ast.ForLoop]:
        result: list[ast.ForLoop] = []
        for stmt in sub.body:
            result.extend(self._collect_for_loops(stmt))
        return result

    def _for_frame_names(self, line_no: int) -> tuple[str, str]:
        """FOR 的上界/步长在协程帧里的字段名，按 FOR 的 line_no 唯一。裸名（不过 c_ident），
        `sa_floop_` 前缀不与用户变量撞车。"""
        return f"sa_floop_end_{line_no}", f"sa_floop_step_{line_no}"

    def async_frame_fields(self, sub: ast.Subroutine) -> list[tuple[str, ast.TypeSpec]]:
        """帧字段 = 参数 + 所有局部（首期无脑全提升，不做活跃变量分析：正确性无损、帧略大）。"""
        fields: list[tuple[str, ast.TypeSpec]] = [(p.name, p.type_spec) for p in sub.params]
        seen = {p.name.lower() for p in sub.params}
        for stmt in sub.body:
            for decl in self._collect_local_decls(stmt):
                if decl.name.lower() not in seen:
                    seen.add(decl.name.lower())
                    fields.append((decl.name, decl.type_spec))
        return fields

    def async_frame_field(self, root: str) -> str | None:
        """命中当前协程帧变量集合的标识符 -> f-><c_ident>；否则 None（走普通解析）。"""
        if self.async_frame_stack and root.lower() in self.async_frame_stack[-1].frame_vars:
            return f"f->{self.c_ident(root)}"
        return None

    def _is_managed_type(self, type_spec: ast.TypeSpec) -> bool:
        return (
            is_string(type_spec) or is_symbol(type_spec) or is_error(type_spec) or is_promise(type_spec) or is_sub_type(type_spec)
            or (type_spec.name == "ENTITY" and self.type_has_managed_resources(type_spec))
        )

    def generate_async_frame_decls(self) -> str:
        """所有 ASYNC SUB 的帧 typedef 与 resume/start 原型，放在普通原型区之前——帧类型
        被 resume/start 引用，start 之间也会互相调用（async sub 调 async sub）。"""
        chunks: list[str] = []
        self._async_generated = {}
        self._async_temp_fields = {}
        for sub in self.checked.program.subs:
            if not sub.is_async:
                continue
            self._async_generated[sub.name] = self.generate_async_sub(sub)
            frame_type = self.async_frame_type(sub.name)
            lines = ["typedef struct {", "    SaCoroBase base;"]
            for name, type_spec in self.async_frame_fields(sub):
                suffix = f"[{type_spec.array_size}]" if type_spec.array_size is not None else ""
                lines.append(f"    {self.c_type(type_spec)} {self.c_ident(name)}{suffix};")
                if self._is_managed_type(type_spec):
                    lines.append(f"    int sa_borrowed_{self.c_ident(name)};")
            lines.extend(f"    {kind} {name};" for kind, name in self._async_temp_fields[sub.name])
            for loop in self._collect_for_loops_in_sub(sub):
                end_name, step_name = self._for_frame_names(loop.line_no)
                lines.append(f"    long long {end_name};")
                lines.append(f"    long long {step_name};")
            lines.append(f"}} {frame_type};")
            chunks.append("\n".join(lines))
            chunks.append(f"static void {self.async_resume_c_name(sub.name)}(SaCoroBase* base);")
            chunks.append(f"static void {self.async_cleanup_c_name(sub.name)}(SaCoroBase* base);")
            start_storage = "" if self.is_exported_sub(sub) else "static "
            chunks.append(f"{start_storage}SaHandle {self.async_start_export_name(sub)}({self.async_start_params(sub)});")
        return "\n".join(chunks)

    def generate_async_sub(self, sub: ast.Subroutine) -> str:
        if sub.name in getattr(self, "_async_generated", {}):
            return self._async_generated[sub.name]
        frame_type = self.async_frame_type(sub.name)
        fields = self.async_frame_fields(sub)
        ctx = AsyncFrameCtx(sub, frame_type, {name.lower() for name, _ in fields})
        ctx.for_frame_lines = {loop.line_no for loop in self._collect_for_loops_in_sub(sub)}

        self.async_frame_stack.append(ctx)
        self.push_sub_scope(sub)
        self.sub_name_stack.append(sub.name)
        self.sub_return_type_stack.append(sub.return_type)
        # async sub 首期不支持 GOSUB/GOTO（与状态机的 switch/goto 交织复杂）——按「无」处理，
        # 语义层已可另行拒绝；这里保持栈平衡即可。
        self.sub_gosub_stack.append(False)
        self.sub_gosub_lines_stack.append([])
        self.sub_has_goto_stack.append(False)
        self.local_resource_stack.append([])

        body_lines: list[str] = ["    sa_coro_body:;"]
        # 参数里的托管资源：start 已 strdup/copy 进帧，登记到本帧清理集合，resume 终结点统一释放
        for param in sub.params:
            if self._is_managed_type(param.type_spec):
                self.register_local_resource(self.c_ident_path(param.name), param.type_spec)
        for stmt in sub.body:
            body_lines.extend(self.stmt(stmt, 1))
        if sub.return_type.name == "VOID":
            body_lines.extend(self.async_terminate_void(1))

        switch_lines = self.async_switch_lines(ctx, 1)
        # 块作用域的登记此时已经弹出，兜底必须遍历全部帧字段；借用字段不拥有资源。
        cleanup_body = []
        for name, kind in reversed(fields):
            if self._is_managed_type(kind):
                cleanup_body.append(f"    if (!f->sa_borrowed_{self.c_ident(name)}) {{")
                cleanup_body.extend(self.local_resource_cleanup_lines([(f"f->{self.c_ident(name)}", kind)], 2))
                cleanup_body.append("    }")

        body_lines, temp_cleanup, temp_fields = self.hoist_async_temporaries(body_lines, ctx)
        cleanup_body.extend(temp_cleanup)
        self._async_temp_fields[sub.name] = temp_fields

        self.local_resource_stack.pop()
        self.sub_has_goto_stack.pop()
        self.sub_gosub_lines_stack.pop()
        self.sub_gosub_stack.pop()
        self.sub_return_type_stack.pop()
        self.sub_name_stack.pop()
        self.scope_stack.pop()
        self.async_frame_stack.pop()

        resume = self.async_resume_def(sub, frame_type, switch_lines, body_lines)
        cleanup = self.async_cleanup_def(sub, frame_type, cleanup_body)
        start = self.async_start_def(sub, frame_type)
        return "\n".join([self.source_comment(sub.line_no, 0), resume, "", cleanup, "", start])

    def async_switch_lines(self, ctx: AsyncFrameCtx, indent: int) -> list[str]:
        pad = "    " * indent
        lines = [f"{pad}switch (f->base.state) {{", f"{pad}    case 0: goto sa_coro_body;"]
        for point in ctx.resume_points:
            lines.append(f"{pad}    case {point}: goto sa_await_resume_{point};")
        lines.append(f"{pad}}}")
        return lines

    def async_resume_def(self, sub: ast.Subroutine, frame_type: str, switch_lines: list[str], body_lines: list[str]) -> str:
        return "\n".join([
            f"static void {self.async_resume_c_name(sub.name)}(SaCoroBase* base) {{",
            f"    {frame_type}* f = ({frame_type}*)base;",
            # landing：协程体内未被内层 CATCH 捕获的 THROW 冒泡到这里 -> reject 本 promise。
            # 每次 resume 重新压入（上次挂起时已弹出），故 sa_try_top 在 resume 返回时与进入
            # 时相等。挂起点绝不 longjmp 进来（那个 setjmp 帧已随 return 失效），靠「AWAIT 禁于
            # TRY 内」保证跨 await 的异常不会发生。
            f"    sa_try_top++;",
            f"    if (SA_SETJMP(sa_try_stack[sa_try_top - 1].env) != 0) {{",
            f"        sa_try_top--;",
            f"        sa_promise_reject_error(f->base.self, &sa_current_error);",
            f"        sa_error_clear(&sa_current_error);",
            f"        return;",
            f"    }}",
            *switch_lines,
            *body_lines,
            "}",
        ])

    def async_cleanup_def(self, sub: ast.Subroutine, frame_type: str, cleanup_body: list[str]) -> str:
        """正常返回、失败和取消统一兜底；字段释放后归零，未执行到的声明保持 calloc 的零值。"""
        return "\n".join([
            f"static void {self.async_cleanup_c_name(sub.name)}(SaCoroBase* base) {{",
            f"    {frame_type}* f = ({frame_type}*)base;",
            "    (void)f;",
            *cleanup_body,
            "}",
        ])

    def async_start_def(self, sub: ast.Subroutine, frame_type: str) -> str:
        lines = [
            f'{"" if self.is_exported_sub(sub) else "static "}SaHandle {self.async_start_export_name(sub)}({self.async_start_params(sub)}) {{',
            # calloc 而非 malloc：挂起中被 drop 时 cleanup 要清理帧内托管资源，还没执行到的
            # DIM 局部必须是 NULL 才能安全 free(NULL)——malloc 的垃圾值会让 cleanup 崩。
            f"    {frame_type}* f = ({frame_type}*)calloc(1, sizeof({frame_type}));",
            '    if (!f) { fputs("SonAlgebraic runtime: out of memory\\n", stderr); exit(1); }',
            "    f->base.state = 0;",
            f"    f->base.resume = {self.async_resume_c_name(sub.name)};",
            f"    f->base.cleanup = {self.async_cleanup_c_name(sub.name)};",
            "    f->base.awaited = 0;",
        ]
        # 拷参进帧：STRING / SYMBOL / ERROR / ENTITY 深拷贝，callable retain——帧持有所有权（调用方的实参
        # 生命周期与协程无关，协程可能在调用返回后很久才跑）
        for param in sub.params:
            name = self.c_ident(param.name)
            field = f"f->{name}"
            if is_string(param.type_spec):
                lines.append(f"    {field} = sa_strdup({name});")
            elif is_symbol(param.type_spec):
                lines.append(f"    {field} = sa_symbol_clone({name});")
            elif is_error(param.type_spec):
                lines.append(f"    sa_set_error(&{field}, &{name});")
            elif is_sub_type(param.type_spec):
                lines.append(f"    {field} = sa_callable_retain({name});")
            elif param.type_spec.name == "ENTITY" and self.type_has_managed_resources(param.type_spec):
                lines.extend(self.entity_init_lines(field, param.type_spec, 1))
                lines.extend(self.entity_copy_lines(field, name, param.type_spec, 1))
            else:
                lines.append(f"    {field} = {name};")
        promise = self.next_temp()
        lines.extend([
            f"    SaHandle {promise} = sa_promise_alloc(&f->base);",
            f"    sa_coro_schedule({promise});",
            f"    return {promise};",
            "}",
        ])
        return "\n".join(lines)

    def async_terminate_void(self, indent: int) -> list[str]:
        """VOID 协程走到体末尾 = 正常完成。清理帧、弹 landing、fulfill_void（内含帧回收）。"""
        pad = "    " * indent
        return [
            *self.active_local_resource_cleanup_lines(indent),
            f"{pad}sa_try_top--;",
            f"{pad}sa_promise_fulfill_void(f->base.self);",
            f"{pad}return;",
        ]

    def async_return_stmt(self, stmt: ast.Return, indent: int) -> list[str]:
        pad = "    " * indent
        if stmt.expr is None:
            return [
                self.source_comment(stmt.line_no, indent),
                *self.active_local_resource_cleanup_lines(indent),
                f"{pad}sa_try_top--;",
                f"{pad}sa_promise_fulfill_void(f->base.self);",
                f"{pad}return;",
            ]
        return_type = self.current_sub_return_type()
        # 与同步 RETURN 同一套求值：STRING 结果是移出 / 接管 / 拷贝得到的独立指针，交给 slot 后帧照常
        # 清理（被移出的局部已置 NULL）。以前这里无脑 strdup 一份，正确但多一次拷贝。
        value_lines, temp = self.return_value_lines(stmt.expr, indent)
        lines = [self.source_comment(stmt.line_no, indent), *value_lines]
        lines.extend(self.active_local_resource_cleanup_lines(indent))
        lines.append(f"{pad}sa_try_top--;")
        if is_string(return_type):
            # 返回值临时量也提升进帧；settle 清帧之前先移交给栈上的交付值。
            lines.append(f"{pad}char* sa_result = {temp};")
            lines.append(f"{pad}{temp} = NULL;")
            lines.append(f"{pad}sa_promise_fulfill_str(f->base.self, sa_result);")
        else:
            lines.append(f"{pad}{self.async_fulfill_call(return_type, temp)};")
        lines.append(f"{pad}return;")
        return lines

    def async_fulfill_call(self, return_type: ast.TypeSpec, temp: str) -> str:
        self_h = "f->base.self"
        if is_handle(return_type):
            return f"sa_promise_fulfill_handle({self_h}, {temp})"
        if return_type.name == "NUM" and return_type.subtype == "LONG":
            return f"sa_promise_fulfill_long({self_h}, {temp})"
        if return_type.name == "NUM":
            return f"sa_promise_fulfill_double({self_h}, {temp})"
        if return_type.name == "BOOL":
            return f"sa_promise_fulfill_long({self_h}, {temp})"
        raise SonCompileError("ASYNC SUB 暂不支持返回该类型（首期支持 NUM / BOOL / STRING / HANDLE / VOID）")

    def await_stmt(self, stmt: ast.AwaitStmt, indent: int) -> list[str]:
        """独立成句的 AWAIT / SYNC（结果被丢弃）。照 CALL 语句：求值即执行副作用。"""
        pad = "    " * indent
        prelude, value, cleanup = self.expr_with_prelude(stmt.expr)
        lines = [self.source_comment(stmt.line_no, indent), *(f"{pad}{line}" for line in prelude)]
        if value:
            lines.append(f"{pad}(void)({value});")
        lines.extend(f"{pad}{line}" for line in cleanup)
        return lines

    def await_expr(self, expr: ast.AwaitExpr) -> str:
        ctx = self.async_frame_stack[-1]
        promise = self.expr(expr.operand)
        point = ctx.next_resume_point()
        # 挂起序列作为「表达式求值前奏」发出：设好子 promise 与恢复点、弹 landing、登记等待、
        # return 回调度器；恢复点标签紧随其后，下次 resume 从这里继续把结果取出来。
        self.add_prelude(f"f->base.awaited = {promise};")
        self.add_prelude(f"f->base.state = {point};")
        self.add_prelude("sa_try_top--;")
        self.add_prelude("sa_coro_await(&f->base, f->base.awaited);")
        self.add_prelude("return;")
        self.add_prelude(f"sa_await_resume_{point}:;")
        inner = self.await_result_type(expr.operand)
        return self._promise_take_value("f->base.awaited", inner)

    def sync_expr(self, expr: ast.SyncExpr) -> str:
        cleanup_start = len(self.cleanup_stack[-1])
        promise = self.expr(expr.operand)
        handle = self.next_temp()
        self.add_prelude(f"SaHandle {handle} = {promise};")
        # start 已快照参数。取结果可能 longjmp，实参临时量必须在那之前释放。
        argument_cleanup = self.cleanup_stack[-1][cleanup_start:]
        del self.cleanup_stack[-1][cleanup_start:]
        for line in argument_cleanup:
            self.add_prelude(line)
        self.add_prelude(f"sa_event_loop_run_until({handle});")
        inner = self.await_result_type(expr.operand)
        return self._promise_take_value(handle, inner)

    def await_result_type(self, operand: ast.Expr) -> ast.TypeSpec:
        """AWAIT/SYNC 结果类型 = operand 的 PROMISE 内层类型。"""
        promise_type = self.type_of(operand)
        return promise_type.inner if promise_type.inner is not None else ast.TypeSpec("VOID")

    def _promise_take_value(self, promise_expr: str, inner: ast.TypeSpec) -> str:
        if inner.name == "VOID":
            self.add_prelude(f"sa_promise_take_void({promise_expr});")
            self.add_cleanup(f"sa_promise_release({promise_expr});")
            return ""
        if is_string(inner):
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = sa_promise_take_str({promise_expr});")
            self.add_cleanup(f"sa_promise_release({promise_expr});")
            self.add_cleanup(f"free({temp});")
            return temp
        if is_handle(inner):
            take = f"sa_promise_take_handle({promise_expr})"
        elif inner.name == "NUM" and inner.subtype == "LONG":
            take = f"sa_promise_take_long({promise_expr})"
        elif inner.name == "NUM":
            take = f"sa_promise_take_double({promise_expr})"
        elif inner.name == "BOOL":
            take = f"sa_promise_take_long({promise_expr})"
        else:
            raise SonCompileError("AWAIT/SYNC 暂不支持该 PROMISE 结果类型")
        temp = self.next_temp()
        self.add_prelude(f"{self.c_type(inner)} {temp} = {take};")
        self.add_cleanup(f"sa_promise_release({promise_expr});")
        return temp
