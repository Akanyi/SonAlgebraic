from __future__ import annotations

from ...analysis.semantics import CheckedProgram
from ...analysis.typesys import BUILTIN_MODULES, is_error, is_string, is_symbol
from ...core import ast
from ...core.errors import SonCompileError
from ...core.names import module_symbol_prefix, split_module_member
from .base import NativeGenBase, VarSlot, sub_gosub_lines
from .builtins import BuiltinsMixin
from .entities import EntitiesMixin
from .exprs import ExprsMixin
from .runtime_decls import RUNTIME_SIGNATURES
from .stmts import StmtsMixin
from .types import TypesMixin


class NativeLLVMGen(BuiltinsMixin, StmtsMixin, ExprsMixin, EntitiesMixin, TypesMixin, NativeGenBase):
    """Experimental textual LLVM IR backend.

    This backend intentionally starts small. It is a parallel path to the C backend,
    not a replacement yet.
    """

    def __init__(self, checked: CheckedProgram, main_init_calls: list[str] | None = None, main_free_calls: list[str] | None = None) -> None:
        self.checked = checked
        self.main_init_calls = main_init_calls or []
        self.main_free_calls = main_free_calls or []
        self.temp_index = 0
        self.string_index = 0
        self.label_index = 0
        self.string_constants: list[tuple[str, int, str]] = []
        # 字面量 -> 已发射的全局常量符号。同一个串（THROW 的 error_type、CATCH 的
        # strcmp 目标、循环体里的 PRINT 文本）在程序里出现几十次很常见，不去重的话
        # IR 体积和解析时间跟着线性膨胀。
        self.string_symbols: dict[str, str] = {}
        self.lines: list[str] = []
        # 当前函数所有 alloca 行，函数收尾时统一插到 entry: 之后（见 alloca()）
        self.entry_allocas: list[str] = []
        self.terminated = False
        self.slots: dict[str, VarSlot] = {}
        self.current_sub: ast.Subroutine | None = None
        self.current_gosub_lines: list[int] = []
        self.gosub_stack_ptr: str | None = None
        self.gosub_top_ptr: str | None = None
        # 用到的 C 运行时函数名集合，最后据此发射 declare（链接器再裁掉没用上的）
        self.used_runtime: set[str] = set()
        self.used_c_funcs: dict[str, ast.CFunctionDecl] = {}
        self.used_external_subs: dict[str, ast.Subroutine] = {}
        self.used_external_consts: dict[str, ast.Declaration] = {}
        # 资源作用域栈：每个作用域记录本层登记的 owned 堆资源 slot（目前是字符串）。
        # 函数入口/块入口压栈，块正常退出释放本层，RETURN/函数末尾释放所有活跃层。
        # 复刻 C 后端 local_resource_stack 的确定性清理模型。
        self.scope_resources: list[list[VarSlot]] = []
        # 语句级临时清理帧：表达式求值过程中产生的临时堆值（CONCAT/SLICE/STRING() 结果）
        # 登记到栈顶帧，语句发射完主效果后逐条释放。复刻 C 后端 prelude/cleanup。
        self.temp_cleanup: list[list[str]] = []

    def generate(self) -> str:
        self.validate_supported_program()
        # 先生成全部函数体：过程中会登记用到的 runtime 函数与字符串常量。
        # 必须在组装 declare/常量块之前完成，否则 main 里新增的常量/调用会漏。
        global_lines = self.global_declarations()
        sub_chunks = [self.subroutine(sub) for sub in self.checked.program.subs]
        main_chunk = self.c_main()

        header: list[str] = [
            "; ModuleID = 'sonalgebraic-native'",
            "source_filename = \"sonalgebraic-native\"",
            "",
            "%SaError = type { i32, ptr, ptr, i32, ptr }",
        ]
        header.extend(self.entity_type_declarations())
        header.extend(["", "declare i32 @printf(ptr, ...)"])
        for name in sorted(self.used_runtime):
            header.append(RUNTIME_SIGNATURES[name])
        for key in sorted(self.used_c_funcs):
            header.append(self.c_func_declare(self.used_c_funcs[key]))
        header.extend(self.external_module_declarations())
        header.extend([
            "",
            '@.sa_fmt_i64 = private unnamed_addr constant [6 x i8] c"%lld\\0A\\00"',
            '@.sa_fmt_f64 = private unnamed_addr constant [7 x i8] c"%.15g\\0A\\00"',
            '@.sa_fmt_str = private unnamed_addr constant [4 x i8] c"%s\\0A\\00"',
            '@.sa_fmt_i64_part = private unnamed_addr constant [5 x i8] c"%lld\\00"',
            '@.sa_fmt_f64_part = private unnamed_addr constant [6 x i8] c"%.15g\\00"',
            '@.sa_fmt_str_part = private unnamed_addr constant [3 x i8] c"%s\\00"',
            '@.sa_fmt_newline = private unnamed_addr constant [2 x i8] c"\\0A\\00"',
            '@.sa_empty = private unnamed_addr constant [1 x i8] zeroinitializer',
            "",
        ])

        chunks: list[str] = [*header]
        chunks.extend(global_lines)
        chunks.append("")
        chunks.extend(self.string_constant_lines())
        if self.string_constants:
            chunks.append("")
        chunks.extend(sub_chunks)
        chunks.append(main_chunk)
        return "\n".join(chunks).rstrip() + "\n"

    def validate_supported_program(self) -> None:
        for use in self.checked.program.uses:
            if use.module not in BUILTIN_MODULES and use.alias.lower() not in self.checked.external_modules:
                raise SonCompileError("native 后端暂不支持用户模块", use.line_no)
        for decl in self.checked.program.declarations:
            self.require_supported_type(decl.type_spec, decl.line_no)
        for sub in self.checked.program.subs:
            self.require_supported_type(sub.return_type, sub.line_no, allow_void=True)
            for param in sub.params:
                self.require_supported_type(param.type_spec, param.line_no)
            for stmt in sub.body:
                self.require_supported_stmt(stmt)
        for stmt in self.checked.program.top_level:
            if not isinstance(stmt, ast.Call | ast.End | ast.NoOp):
                raise SonCompileError("native 后端顶层暂只支持 CALL/END", stmt.line_no)

    def require_supported_type(self, type_spec: ast.TypeSpec, line_no: int, allow_void: bool = False) -> None:
        if allow_void and type_spec.name == "VOID":
            return
        if type_spec.array_size is not None:
            self.require_supported_type(self.array_element_type(type_spec), line_no)
            return
        if type_spec.name in {"NUM", "BOOL", "STRING", "CPTR", "SYMBOL", "ERROR", "ENTITY", "HANDLE"}:
            return
        if type_spec.name == "PTR" and type_spec.inner is not None:
            self.require_supported_type(type_spec.inner, line_no)
            return
        raise SonCompileError(f"native 后端暂不支持类型: {type_spec.name}", line_no)

    def require_supported_stmt(self, stmt: ast.Stmt) -> None:
        if isinstance(stmt, ast.TryCatch):
            for branch in stmt.catches:
                for inner in branch.body:
                    self.require_supported_stmt(inner)
            return
        if isinstance(stmt, ast.NoOp | ast.Print | ast.Assign | ast.Call | ast.ThrowNew | ast.ThrowVar | ast.Return | ast.Goto | ast.Gosub | ast.Label | ast.Input | ast.Cls):
            return
        if isinstance(stmt, ast.LocalDeclaration):
            self.require_supported_type(stmt.type_spec, stmt.line_no)
            return
        if isinstance(stmt, ast.If):
            for inner in [*stmt.body, *stmt.else_body]:
                self.require_supported_stmt(inner)
            for branch in stmt.elifs:
                for inner in branch.body:
                    self.require_supported_stmt(inner)
            return
        if isinstance(stmt, ast.ForLoop | ast.WhileLoop):
            for inner in stmt.body:
                self.require_supported_stmt(inner)
            return
        raise SonCompileError(f"native 后端暂不支持语句: {type(stmt).__name__}", stmt.line_no)

    def global_declarations(self) -> list[str]:
        lines: list[str] = []
        for decl in self.checked.program.declarations:
            init = self.default_value(decl.type_spec)
            lines.append(self.source_comment(decl.line_no))
            lines.append(f"@{self.c_ident(decl.name)} = global {self.llvm_type(decl.type_spec)} {init}")
        return lines

    def external_module_declarations(self) -> list[str]:
        lines: list[str] = []
        for name in sorted({*self.main_init_calls, *self.main_free_calls}):
            lines.append(f"declare void @{name}()")
        for symbol, const in sorted(self.used_external_consts.items()):
            lines.append(f"@{symbol} = external global {self.c_abi_type(const.type_spec)}")
        for symbol, sub in sorted(self.used_external_subs.items()):
            params = ", ".join(self.c_abi_param_decl(param) for param in sub.params)
            lines.append(f"declare {self.c_abi_type(sub.return_type)} @{symbol}({params})")
        return lines

    def subroutine(self, sub: ast.Subroutine) -> str:
        self.current_sub = sub
        self.lines = []
        self.entry_allocas = []
        self.terminated = False
        self.slots = self.global_slots()
        self.scope_resources = []
        self.current_gosub_lines = sub_gosub_lines(sub)
        self.gosub_stack_ptr = "%sa_gosub_stack" if self.current_gosub_lines else None
        self.gosub_top_ptr = "%sa_gosub_top" if self.current_gosub_lines else None
        params = ", ".join(self.param_decl(param) for param in sub.params)
        self.emit(self.source_comment(sub.line_no))
        self.emit(f"define {self.llvm_type(sub.return_type)} @{self.sub_name(sub.name)}({params}) {{")
        self.emit("entry:")
        entry_index = len(self.lines)
        self.push_scope()
        if self.current_gosub_lines:
            self.alloca("[64 x i64]", "%sa_gosub_stack")
            self.alloca("i64", "%sa_gosub_top")
            self.emit("  store i64 0, ptr %sa_gosub_top")
        for param in sub.params:
            name = self.c_ident(param.name)
            if param.by_ref:
                self.slots[param.name.lower()] = VarSlot(param.name, param.type_spec, f"%{name}", True)
                continue
            ptr = self.alloca(self.llvm_type(param.type_spec), f"%{name}.addr")
            slot = VarSlot(param.name, param.type_spec, ptr)
            self.slots[param.name.lower()] = slot
            if is_string(param.type_spec):
                # 值传 STRING 形参：拷贝一份归本帧所有，函数退出时释放（复刻 C 后端
                # prepare_value_param_resources），避免释放调用方的串。
                self.use_runtime("sa_strdup")
                dup = self.next_temp()
                self.emit(f"  {dup} = call ptr @sa_strdup(ptr %{name})")
                self.emit(f"  store ptr {dup}, ptr {ptr}")
                self.register_owned(slot)
            elif self.is_entity_scalar(param.type_spec) and self.type_has_managed_resources(param.type_spec):
                source = f"%{name}.param"
                self.alloca(self.llvm_type(param.type_spec), source)
                self.emit(f"  store {self.llvm_type(param.type_spec)} %{name}, ptr {source}")
                self.emit(f"  store {self.llvm_type(param.type_spec)} zeroinitializer, ptr {ptr}")
                self.emit_entity_init(ptr, param.type_spec)
                self.emit_entity_copy(ptr, source, param.type_spec)
                self.register_owned(slot)
            else:
                self.emit(f"  store {self.llvm_type(param.type_spec)} %{name}, ptr {ptr}")
        for stmt in sub.body:
            self.stmt(stmt)
        if not self.terminated:
            self.emit_scope_cleanup(self.scope_resources[-1])
            if sub.return_type.name == "VOID":
                self.emit("  ret void")
            else:
                self.emit(f"  ret {self.llvm_type(sub.return_type)} {self.default_value(sub.return_type)}")
        self.scope_resources.pop()
        self.emit("}")
        self.current_sub = None
        self.current_gosub_lines = []
        self.gosub_stack_ptr = None
        self.gosub_top_ptr = None
        self.lines[entry_index:entry_index] = self.entry_allocas
        return "\n".join(self.lines) + "\n"

    def c_main(self) -> str:
        self.current_sub = None
        self.lines = []
        self.entry_allocas = []
        self.terminated = False
        self.slots = self.global_slots()
        self.scope_resources = []
        self.emit("define i32 @main() {")
        self.emit("entry:")
        entry_index = len(self.lines)
        self.use_runtime("sa_setup_console")
        self.emit("  call void @sa_setup_console()")
        for call in self.main_init_calls:
            self.emit(f"  call void @{call}()")
        # 全局 STRING 初始化为 owned 空串（复刻 C 后端 init_string_globals）
        global_strings = [decl for decl in self.checked.program.declarations if is_string(decl.type_spec)]
        global_symbols = [decl for decl in self.checked.program.declarations if is_symbol(decl.type_spec)]
        global_errors = [decl for decl in self.checked.program.declarations if is_error(decl.type_spec)]
        global_entities = [decl for decl in self.checked.program.declarations if self.is_entity_scalar(decl.type_spec) and self.type_has_managed_resources(decl.type_spec)]
        for decl in global_strings:
            self.init_global_string(decl)
        for decl in global_entities:
            self.emit_entity_init(f"@{self.c_ident(decl.name)}", decl.type_spec)
        for decl in self.checked.program.declarations:
            if decl.expr is not None:
                self.init_global_value(decl)
        for stmt in self.checked.program.top_level:
            if isinstance(stmt, ast.Call):
                self.begin_stmt()
                self.call_stmt(stmt)
                self.end_stmt()
            elif isinstance(stmt, ast.End):
                break
        # 程序结束释放全局 STRING（复刻 C 后端 free_string_globals）
        for decl in global_strings:
            self.free_global_string(decl)
        for decl in global_symbols:
            self.free_global_symbol(decl)
        for decl in global_errors:
            self.free_global_error(decl)
        for decl in global_entities:
            self.emit_entity_free(f"@{self.c_ident(decl.name)}", decl.type_spec)
        # 捕获过的最后一个运行时全局错误也持有 message 副本，C 后端 main 末尾同样清理。
        self.use_runtime("sa_current_error")
        self.use_runtime("sa_error_clear")
        self.emit("  call void @sa_error_clear(ptr @sa_current_error)")
        for call in reversed(self.main_free_calls):
            self.emit(f"  call void @{call}()")
        self.emit("  ret i32 0")
        self.emit("}")
        self.lines[entry_index:entry_index] = self.entry_allocas
        return "\n".join(self.lines) + "\n"

    def init_global_string(self, decl: ast.Declaration) -> None:
        self.use_runtime("sa_strdup")
        name = f"@{self.c_ident(decl.name)}"
        if self.is_string_array(decl.type_spec):
            elem_ty = self.array_element_type(decl.type_spec)
            for index in range(decl.type_spec.array_size or 0):
                element_ptr = self.next_temp()
                dup = self.next_temp()
                self.emit(f"  {element_ptr} = getelementptr inbounds {self.llvm_type(decl.type_spec)}, ptr {name}, i64 0, i64 {index}")
                self.emit(f"  {dup} = call ptr @sa_strdup(ptr @.sa_empty)")
                self.emit(f"  store {self.llvm_type(elem_ty)} {dup}, ptr {element_ptr}")
            return
        dup = self.next_temp()
        self.emit(f"  {dup} = call ptr @sa_strdup(ptr @.sa_empty)")
        self.emit(f"  store ptr {dup}, ptr {name}")

    def free_global_string(self, decl: ast.Declaration) -> None:
        self.use_runtime("free")
        name = f"@{self.c_ident(decl.name)}"
        if self.is_string_array(decl.type_spec):
            elem_ty = self.array_element_type(decl.type_spec)
            for index in range(decl.type_spec.array_size or 0):
                element_ptr = self.next_temp()
                tmp = self.next_temp()
                self.emit(f"  {element_ptr} = getelementptr inbounds {self.llvm_type(decl.type_spec)}, ptr {name}, i64 0, i64 {index}")
                self.emit(f"  {tmp} = load {self.llvm_type(elem_ty)}, ptr {element_ptr}")
                self.emit(f"  call void @free(ptr {tmp})")
            return
        tmp = self.next_temp()
        self.emit(f"  {tmp} = load ptr, ptr {name}")
        self.emit(f"  call void @free(ptr {tmp})")

    def free_global_symbol(self, decl: ast.Declaration) -> None:
        self.use_runtime("sa_symbol_free")
        tmp = self.next_temp()
        self.emit(f"  {tmp} = load ptr, ptr @{self.c_ident(decl.name)}")
        self.emit(f"  call void @sa_symbol_free(ptr {tmp})")

    def free_global_error(self, decl: ast.Declaration) -> None:
        self.use_runtime("sa_error_clear")
        self.emit(f"  call void @sa_error_clear(ptr @{self.c_ident(decl.name)})")

    def init_global_value(self, decl: ast.Declaration) -> None:
        target = f"@{self.c_ident(decl.name)}"
        self.emit(self.source_comment(decl.line_no))
        self.begin_stmt()
        if is_symbol(decl.type_spec):
            self.assign_symbol(target, decl.expr)
            self.end_stmt()
            return
        if self.is_entity_scalar(decl.type_spec) and self.type_has_managed_resources(decl.type_spec):
            source_ptr = self.entity_source_ptr(decl.expr, decl.type_spec)
            self.emit_entity_copy(target, source_ptr, decl.type_spec)
        else:
            value = self.cast_value(self.expr(decl.expr), decl.type_spec)
        if self.is_string_scalar(decl.type_spec):
            self.use_runtime("sa_set_string")
            self.emit(f"  call void @sa_set_string(ptr {target}, ptr {value.value})")
        elif not (self.is_entity_scalar(decl.type_spec) and self.type_has_managed_resources(decl.type_spec)):
            self.emit(f"  store {self.llvm_type(decl.type_spec)} {value.value}, ptr {target}")
        self.end_stmt()

    def resolve_c_func(self, name: str) -> ast.CFunctionDecl | None:
        split = split_module_member(name)
        if split is None:
            return None
        alias, member = split
        return self.checked.c_funcs.get(f"{alias.lower()}.{member.lower()}")

    def resolve_external_sub(self, name: str) -> tuple[str, ast.Subroutine] | None:
        split = split_module_member(name)
        if split is None:
            return None
        alias, member = split
        module = self.checked.external_modules.get(alias)
        if module is None:
            return None
        sub = module.subs.get(member.lower())
        if sub is None:
            return None
        symbol = f"{module_symbol_prefix(module.module)}_sub_{sub.name.lower()}"
        self.used_external_subs[symbol] = sub
        return symbol, sub

    def resolve_external_const(self, name: str) -> tuple[str, ast.Declaration] | None:
        split = split_module_member(name)
        if split is None:
            return None
        alias, member = split
        module = self.checked.external_modules.get(alias)
        if module is None:
            return None
        const = module.consts.get(member.lower())
        if const is None:
            return None
        symbol = f"{module_symbol_prefix(module.module)}_const_{const.name.lower()}"
        self.used_external_consts[symbol] = const
        return symbol, const

    def c_func_declare(self, c_func: ast.CFunctionDecl) -> str:
        params = ", ".join("ptr" if param.by_ref else self.c_abi_type(param.type_spec) for param in c_func.params)
        return f"declare {self.c_abi_type(c_func.return_type)} @{c_func.name}({params})"

    def use_c_func(self, c_func: ast.CFunctionDecl) -> None:
        key = f"{c_func.alias.lower()}.{c_func.name.lower()}"
        self.used_c_funcs[key] = c_func


def generate_native_llvm_ir(checked: CheckedProgram, main_init_calls: list[str] | None = None, main_free_calls: list[str] | None = None) -> str:
    return NativeLLVMGen(checked, main_init_calls=main_init_calls, main_free_calls=main_free_calls).generate()
