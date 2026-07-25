from __future__ import annotations

from dataclasses import dataclass
import struct

from ...analysis.semantics import CheckedProgram, Symbol
from ...analysis.typesys import BUILTIN_MODULES, classify_number_literal, is_bool, is_cptr, is_error, is_handle, is_null, is_numeric, is_ptr, is_string, is_symbol, resolve_builtin_const, resolve_net_function, type_of
from ...core import ast
from ...core.errors import SonCompileError
from ...core.names import c_ident as make_c_ident, entity_c_name, module_symbol_prefix, split_module_member


@dataclass(frozen=True)
class LLVMValue:
    type_name: str
    value: str
    type_spec: ast.TypeSpec | None = None


@dataclass(frozen=True)
class VarSlot:
    name: str
    type_spec: ast.TypeSpec
    ptr: str
    by_ref: bool = False


class NativeLLVMGen:
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
        self.lines: list[str] = []
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

    # --- 资源清理基础设施（确定性内存模型，复刻 C 后端） ---

    def begin_stmt(self) -> None:
        self.temp_cleanup.append([])

    def end_stmt(self) -> None:
        for line in self.temp_cleanup.pop():
            self.emit(line)

    def discard_stmt(self) -> None:
        # RETURN 携带返回值时，C 后端丢弃表达式 cleanup（返回值可能就是临时量）。
        # 这里同样丢弃不释放，避免释放正被返回的指针。
        self.temp_cleanup.pop()

    def add_temp_cleanup(self, line: str) -> None:
        if self.temp_cleanup:
            self.temp_cleanup[-1].append(line)

    def push_scope(self) -> None:
        self.scope_resources.append([])

    def register_owned(self, slot: VarSlot) -> None:
        if self.scope_resources:
            self.scope_resources[-1].append(slot)

    def array_element_type(self, type_spec: ast.TypeSpec) -> ast.TypeSpec:
        return ast.TypeSpec(type_spec.name, type_spec.subtype, type_spec.inner)

    def is_string_array(self, type_spec: ast.TypeSpec) -> bool:
        return is_string(type_spec) and type_spec.array_size is not None

    def is_string_scalar(self, type_spec: ast.TypeSpec) -> bool:
        return is_string(type_spec) and type_spec.array_size is None

    def is_entity_scalar(self, type_spec: ast.TypeSpec) -> bool:
        return type_spec.name == "ENTITY" and type_spec.array_size is None

    def is_owned_string_resource(self, type_spec: ast.TypeSpec) -> bool:
        return is_string(type_spec)

    def emit_free_slot(self, slot: VarSlot) -> None:
        if self.is_string_array(slot.type_spec):
            elem_ty = self.array_element_type(slot.type_spec)
            for index in range(slot.type_spec.array_size):
                element_ptr = self.next_temp()
                self.emit(f"  {element_ptr} = getelementptr inbounds {self.llvm_type(slot.type_spec)}, ptr {slot.ptr}, i64 0, i64 {index}")
                tmp = self.next_temp()
                self.emit(f"  {tmp} = load {self.llvm_type(elem_ty)}, ptr {element_ptr}")
                self.use_runtime("free")
                self.emit(f"  call void @free(ptr {tmp})")
            return
        if self.is_string_scalar(slot.type_spec):
            tmp = self.next_temp()
            self.emit(f"  {tmp} = load ptr, ptr {slot.ptr}")
            self.use_runtime("free")
            self.emit(f"  call void @free(ptr {tmp})")
            return
        if is_symbol(slot.type_spec):
            tmp = self.next_temp()
            self.emit(f"  {tmp} = load ptr, ptr {slot.ptr}")
            self.use_runtime("sa_symbol_free")
            self.emit(f"  call void @sa_symbol_free(ptr {tmp})")
            return
        if is_error(slot.type_spec):
            self.use_runtime("sa_error_clear")
            self.emit(f"  call void @sa_error_clear(ptr {slot.ptr})")
            return
        if self.is_entity_scalar(slot.type_spec) and self.type_has_managed_resources(slot.type_spec):
            self.emit_entity_free(slot.ptr, slot.type_spec)

    def emit_scope_cleanup(self, resources: list[VarSlot]) -> None:
        for slot in reversed(resources):
            self.emit_free_slot(slot)

    def pop_scope_with_cleanup(self) -> None:
        resources = self.scope_resources.pop()
        if not self.terminated:
            self.emit_scope_cleanup(resources)

    def emit_active_cleanup(self) -> None:
        # RETURN 出口：逆序释放所有活跃作用域的资源
        for resources in reversed(self.scope_resources):
            self.emit_scope_cleanup(resources)

    def run_block(self, body: list[ast.Stmt]) -> None:
        # IF/FOR/WHILE 体：压资源作用域，块正常退出时释放本层（循环内局部串每轮释放）
        self.push_scope()
        for inner in body:
            self.stmt(inner)
        self.pop_scope_with_cleanup()

    @property
    def source_lines(self) -> dict[int, str]:
        return self.checked.program.source_lines

    def use_runtime(self, name: str) -> None:
        if name not in RUNTIME_SIGNATURES:
            raise SonCompileError(f"native 后端引用了未登记的运行时函数: {name}")
        self.used_runtime.add(name)

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

    def entity_type_declarations(self) -> list[str]:
        lines: list[str] = []
        for entity in self.checked.program.entities:
            fields = ", ".join(self.llvm_type(field.type_spec) for field in entity.fields)
            lines.append(f"{self.entity_type_name(entity.name)} = type {{ {fields} }}")
        return lines

    def entity_type_name(self, name: str) -> str:
        return f"%SaEntity_{entity_c_name(name)}"

    def entity_type_from_spec(self, type_spec: ast.TypeSpec) -> str:
        subtype = type_spec.subtype or ""
        split = split_module_member(subtype)
        if split:
            alias, member = split
            module = self.checked.external_modules.get(alias)
            if module is None:
                raise SonCompileError(f"native 后端未知外部 ENTITY 模块: {alias}")
            return f"%{module_symbol_prefix(module.module)}_entity_{entity_c_name(member)}"
        return self.entity_type_name(subtype)

    def resolve_entity_def(self, type_spec: ast.TypeSpec) -> ast.EntityDef | None:
        if type_spec.name != "ENTITY":
            return None
        subtype = type_spec.subtype or ""
        split = split_module_member(subtype)
        if split:
            alias, member = split
            module = self.checked.external_modules.get(alias)
            return module.entities.get(member.lower()) if module is not None else None
        return self.checked.entities.get(subtype.lower())

    def entity_field(self, type_spec: ast.TypeSpec, field_name: str, line_no: int) -> tuple[int, ast.Declaration]:
        entity = self.resolve_entity_def(type_spec)
        if entity is None:
            raise SonCompileError(f"native 后端未知 ENTITY: {type_spec.subtype}", line_no)
        for index, field in enumerate(entity.fields):
            if field.name.lower() == field_name.lower():
                return index, field
        raise SonCompileError(f"ENTITY {entity.name} 没有字段: {field_name}", line_no)

    def type_has_managed_resources(self, type_spec: ast.TypeSpec, inside_entity: bool = False) -> bool:
        if is_string(type_spec) or is_error(type_spec):
            return True
        if is_symbol(type_spec):
            return not inside_entity
        if type_spec.name != "ENTITY":
            return False
        entity = self.resolve_entity_def(type_spec)
        if entity is None:
            return False
        return any(self.type_has_managed_resources(field.type_spec, inside_entity=True) for field in entity.fields)

    def emit_entity_init(self, ptr: str, type_spec: ast.TypeSpec) -> None:
        entity = self.resolve_entity_def(type_spec)
        if entity is None:
            return
        for index, field in enumerate(entity.fields):
            field_ptr = self.next_temp()
            self.emit(f"  {field_ptr} = getelementptr inbounds {self.llvm_type(type_spec)}, ptr {ptr}, i64 0, i32 {index}")
            if self.is_string_scalar(field.type_spec):
                self.use_runtime("sa_strdup")
                dup = self.next_temp()
                self.emit(f"  {dup} = call ptr @sa_strdup(ptr @.sa_empty)")
                self.emit(f"  store ptr {dup}, ptr {field_ptr}")
            elif is_error(field.type_spec):
                self.emit(f"  store %SaError zeroinitializer, ptr {field_ptr}")
            elif self.is_entity_scalar(field.type_spec):
                self.emit_entity_init(field_ptr, field.type_spec)

    def emit_entity_free(self, ptr: str, type_spec: ast.TypeSpec) -> None:
        entity = self.resolve_entity_def(type_spec)
        if entity is None:
            return
        for index, field in reversed(list(enumerate(entity.fields))):
            field_ptr = self.next_temp()
            self.emit(f"  {field_ptr} = getelementptr inbounds {self.llvm_type(type_spec)}, ptr {ptr}, i64 0, i32 {index}")
            if self.is_string_scalar(field.type_spec):
                tmp = self.next_temp()
                self.use_runtime("free")
                self.emit(f"  {tmp} = load ptr, ptr {field_ptr}")
                self.emit(f"  call void @free(ptr {tmp})")
            elif is_error(field.type_spec):
                self.use_runtime("sa_error_clear")
                self.emit(f"  call void @sa_error_clear(ptr {field_ptr})")
            elif self.is_entity_scalar(field.type_spec):
                self.emit_entity_free(field_ptr, field.type_spec)

    def emit_entity_copy(self, target_ptr: str, source_ptr: str, type_spec: ast.TypeSpec) -> None:
        entity = self.resolve_entity_def(type_spec)
        if entity is None:
            tmp = self.next_temp()
            self.emit(f"  {tmp} = load {self.llvm_type(type_spec)}, ptr {source_ptr}")
            self.emit(f"  store {self.llvm_type(type_spec)} {tmp}, ptr {target_ptr}")
            return
        for index, field in enumerate(entity.fields):
            target_field = self.next_temp()
            source_field = self.next_temp()
            field_ty = self.llvm_type(field.type_spec)
            self.emit(f"  {target_field} = getelementptr inbounds {self.llvm_type(type_spec)}, ptr {target_ptr}, i64 0, i32 {index}")
            self.emit(f"  {source_field} = getelementptr inbounds {self.llvm_type(type_spec)}, ptr {source_ptr}, i64 0, i32 {index}")
            if self.is_string_scalar(field.type_spec):
                value = self.next_temp()
                self.use_runtime("sa_set_string")
                self.emit(f"  {value} = load ptr, ptr {source_field}")
                self.emit(f"  call void @sa_set_string(ptr {target_field}, ptr {value})")
            elif is_error(field.type_spec):
                self.use_runtime("sa_set_error")
                self.emit(f"  call void @sa_set_error(ptr {target_field}, ptr {source_field})")
            elif self.is_entity_scalar(field.type_spec):
                self.emit_entity_copy(target_field, source_field, field.type_spec)
            else:
                value = self.next_temp()
                self.emit(f"  {value} = load {field_ty}, ptr {source_field}")
                self.emit(f"  store {field_ty} {value}, ptr {target_field}")

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
        self.push_scope()
        if self.current_gosub_lines:
            self.emit("  %sa_gosub_stack = alloca [64 x i64]")
            self.emit("  %sa_gosub_top = alloca i64")
            self.emit("  store i64 0, ptr %sa_gosub_top")
        for param in sub.params:
            name = self.c_ident(param.name)
            if param.by_ref:
                self.slots[param.name.lower()] = VarSlot(param.name, param.type_spec, f"%{name}", True)
                continue
            ptr = f"%{name}.addr"
            self.emit(f"  {ptr} = alloca {self.llvm_type(param.type_spec)}")
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
                self.emit(f"  {source} = alloca {self.llvm_type(param.type_spec)}")
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
        return "\n".join(self.lines) + "\n"

    def c_main(self) -> str:
        self.current_sub = None
        self.lines = []
        self.terminated = False
        self.slots = self.global_slots()
        self.scope_resources = []
        self.emit("define i32 @main() {")
        self.emit("entry:")
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
        ptr = f"%{name}.addr"
        self.emit(self.source_comment(stmt.line_no))
        self.emit(f"  {ptr} = alloca {self.llvm_type(stmt.type_spec)}")
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

    def entity_source_ptr(self, expr: ast.Expr, type_spec: ast.TypeSpec) -> str:
        if isinstance(expr, ast.VarRef | ast.Deref | ast.Index):
            return self.lvalue_ptr(expr)
        value = self.cast_value(self.expr(expr), type_spec)
        ptr = self.next_temp()
        self.emit(f"  {ptr} = alloca {self.llvm_type(type_spec)}")
        self.emit(f"  store {self.llvm_type(type_spec)} {value.value}, ptr {ptr}")
        return ptr

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

    def fstring_value(self, expr: ast.FString) -> LLVMValue:
        # F-string 作为值（赋值/传参）：用 SaStringBuilder 拼接。布局 {ptr,i64,i64}=24 字节。
        # 结果是堆串，登记临时清理。
        self.use_runtime("sa_sb_init")
        self.use_runtime("sa_sb_append")
        self.use_runtime("sa_sb_take")
        builder = self.next_temp()
        self.emit(f"  {builder} = alloca {{ ptr, i64, i64 }}")
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

    def is_math_function(self, name: str, function_name: str) -> bool:
        split = split_module_member(name)
        return bool(split and self.checked.uses.get(split[0]) == "SYS.MATH" and split[1].upper() == function_name.upper())

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

    def c_cast_arg(self, value: LLVMValue, param_type: ast.TypeSpec) -> LLVMValue:
        return self.cast_value(value, param_type)

    def has_active_resources(self) -> bool:
        return any(resources for resources in self.scope_resources)

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

    def wrap_call_expr_with_throw_cleanup(self, name: str, sub: ast.Subroutine, args: list[str], raw_name: bool = False, c_abi: bool = False) -> LLVMValue:
        ret_type = self.c_abi_type(sub.return_type) if c_abi else self.llvm_type(sub.return_type)
        result_ptr = self.next_temp()
        self.emit(f"  {result_ptr} = alloca {ret_type}")
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

    def input_stmt(self, stmt: ast.Input) -> None:
        self.emit(self.source_comment(stmt.line_no))
        prompt = self.expr(stmt.prompt)
        self.emit_print_value(prompt, newline=False)
        target = self.slot(stmt.target, stmt.line_no)
        buf = self.next_temp()
        self.emit(f"  {buf} = alloca [4096 x i8]")
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
        self.emit(f"  {alias_ptr} = alloca %SaError")
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

    def for_cast(self, value: LLVMValue, var_ty: str) -> LLVMValue:
        if var_ty == "i64":
            return self.cast_to_i64(value)
        return self.cast_to_double(value)

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

    def builtin_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        """全局内置 NUMBER/STRING、SYS.MATH.POW、SYS.STRING/SYS.NET 函数 -> 运行时调用。"""
        name = expr.name.upper()
        if name == "NUMBER":
            self.use_runtime("sa_number")
            arg = self.expr(expr.args[0])
            temp = self.next_temp()
            self.emit(f"  {temp} = call double @sa_number(ptr {arg.value})")
            return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
        if name == "STRING":
            return self.string_conversion(expr)
        if self.is_math_function(expr.name, "POW"):
            if is_symbol(self.type_of_expr(expr)):
                return self.symbol_expr(expr)
            self.use_runtime("pow")
            a = self.cast_to_double(self.expr(expr.args[0]))
            b = self.cast_to_double(self.expr(expr.args[1]))
            temp = self.next_temp()
            self.emit(f"  {temp} = call double @pow(double {a.value}, double {b.value})")
            return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
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
        return self.gui_function_call(expr)

    def symbol_algebra_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        name = expr.name.upper()
        if name not in {"DERIV", "SIMPLIFY", "SUBST", "EVAL"}:
            return None
        sym = self.expr(expr.args[0])
        if name == "EVAL":
            self.use_runtime("sa_symbol_eval")
            temp = self.next_temp()
            self.emit(f"  {temp} = call double @sa_symbol_eval(ptr {sym.value})")
            return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
        if name == "SIMPLIFY":
            self.use_runtime("sa_symbol_simplify")
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @sa_symbol_simplify(ptr {sym.value})")
        elif name == "DERIV":
            self.use_runtime("sa_symbol_deriv")
            temp = self.next_temp()
            var = expr.args[1]
            assert isinstance(var, ast.StringLiteral)
            self.emit(f"  {temp} = call ptr @sa_symbol_deriv(ptr {sym.value}, ptr {self.string_ptr(var.value)})")
        else:
            self.use_runtime("sa_symbol_subst")
            temp = self.next_temp()
            var = expr.args[1]
            assert isinstance(var, ast.StringLiteral)
            value = self.cast_to_double(self.expr(expr.args[2]))
            self.emit(f"  {temp} = call ptr @sa_symbol_subst(ptr {sym.value}, ptr {self.string_ptr(var.value)}, double {value.value})")
        self.use_runtime("sa_symbol_free")
        self.add_temp_cleanup(f"  call void @sa_symbol_free(ptr {temp})")
        return LLVMValue("ptr", temp, ast.TypeSpec("SYMBOL"))

    def string_conversion(self, expr: ast.CallExpr) -> LLVMValue:
        """STRING(x)：按参数类型转字符串。堆返回的登记临时清理。"""
        value = self.expr(expr.args[0])
        if value.type_name == "ptr" and self.is_string_type(value.type_spec):
            return value  # 已经是字符串，直接透传
        if value.type_name == "ptr" and is_error(value.type_spec or ast.TypeSpec("VOID")):
            return LLVMValue("ptr", self.error_message_ptr(value.value), ast.TypeSpec("STRING"))
        temp = self.next_temp()
        if value.type_name == "ptr" and is_symbol(value.type_spec or ast.TypeSpec("VOID")):
            self.use_runtime("sa_symbol_to_string")
            self.emit(f"  {temp} = call ptr @sa_symbol_to_string(ptr {value.value})")
        elif value.type_name == "ptr":
            self.use_runtime("sa_to_string_pointer")
            self.emit(f"  {temp} = call ptr @sa_to_string_pointer(ptr {value.value})")
        elif value.type_name == "i1":
            wide = self.cast_to_i64(value)
            self.use_runtime("sa_to_string_long")
            self.emit(f"  {temp} = call ptr @sa_to_string_long(i64 {wide.value})")
        elif value.type_name == "i64":
            self.use_runtime("sa_to_string_long")
            self.emit(f"  {temp} = call ptr @sa_to_string_long(i64 {value.value})")
        else:  # double
            self.use_runtime("sa_to_string_double")
            self.emit(f"  {temp} = call ptr @sa_to_string_double(double {value.value})")
        self.use_runtime("free")
        self.add_temp_cleanup(f"  call void @free(ptr {temp})")
        return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))

    def string_function_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        """SYS.STRING 内置函数 -> 运行时调用。返回 None 表示不是字符串函数。"""
        split = split_module_member(expr.name)
        if split is None:
            return None
        alias, member = split
        if self.checked.uses.get(alias) != "SYS.STRING":
            return None
        member = member.upper()
        args = [LLVMValue("i64", "0", ast.TypeSpec("HANDLE")) if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        heap_returning = {
            "CONCAT": ("sa_str_concat", "ptr {0}, ptr {1}"),
            "SLICE": ("sa_str_slice", "ptr {0}, i64 {1}, i64 {2}"),
            "UPPER": ("sa_str_upper", "ptr {0}"),
            "LOWER": ("sa_str_lower", "ptr {0}"),
            "REPLACE": ("sa_str_replace", "ptr {0}, ptr {1}, ptr {2}"),
        }
        if member in heap_returning:
            fn, fmt = heap_returning[member]
            self.use_runtime(fn)
            call_args = fmt.format(*(self.coerce_str_arg(a) for a in args))
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @{fn}({call_args})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member == "LENGTH":
            self.use_runtime("sa_str_length")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_str_length(ptr {args[0].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "FIND":
            self.use_runtime("sa_str_find")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_str_find(ptr {args[0].value}, ptr {args[1].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        return None

    def net_function_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        """SYS.NET 内置函数 -> runtime 调用。当前支持阻塞 HTTP GET/STATUS。"""
        split = split_module_member(expr.name)
        if split is None:
            return None
        alias, member = split
        if self.checked.uses.get(alias) != "SYS.NET":
            return None
        member = member.upper()
        args = [LLVMValue("i64", "0", ast.TypeSpec("HANDLE")) if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        if member == "GET":
            self.use_runtime("sa_net_http_get")
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @sa_net_http_get(ptr {args[0].value})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member == "STATUS":
            self.use_runtime("sa_net_http_status")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_net_http_status(ptr {args[0].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "POST":
            self.use_runtime("sa_net_http_post")
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @sa_net_http_post(ptr {args[0].value}, ptr {args[1].value}, ptr {args[2].value})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member == "REQUEST":
            self.use_runtime("sa_net_http_request")
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @sa_net_http_request(ptr {args[0].value}, ptr {args[1].value}, ptr {args[2].value}, ptr {args[3].value})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member == "REQUEST_STATUS":
            self.use_runtime("sa_net_http_request_status")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_net_http_request_status(ptr {args[0].value}, ptr {args[1].value}, ptr {args[2].value}, ptr {args[3].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "REQUEST_TIMEOUT":
            self.use_runtime("sa_net_http_request_timeout")
            temp = self.next_temp()
            timeout = self.cast_to_i64(args[4])
            self.emit(f"  {temp} = call ptr @sa_net_http_request_timeout(ptr {args[0].value}, ptr {args[1].value}, ptr {args[2].value}, ptr {args[3].value}, i64 {timeout.value})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member == "REQUEST_STATUS_TIMEOUT":
            self.use_runtime("sa_net_http_request_status_timeout")
            temp = self.next_temp()
            timeout = self.cast_to_i64(args[4])
            self.emit(f"  {temp} = call i64 @sa_net_http_request_status_timeout(ptr {args[0].value}, ptr {args[1].value}, ptr {args[2].value}, ptr {args[3].value}, i64 {timeout.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member in {"LAST_HEADERS", "LAST_ERROR"}:
            fn = "sa_net_last_headers_copy" if member == "LAST_HEADERS" else "sa_net_last_error_copy"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @{fn}()")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member in {"LAST_PEER_HOST", "DNS"}:
            fn = "sa_net_last_peer_host_copy" if member == "LAST_PEER_HOST" else "sa_net_dns"
            self.use_runtime(fn)
            temp = self.next_temp()
            call_args = "" if member == "LAST_PEER_HOST" else f"ptr {args[0].value}"
            self.emit(f"  {temp} = call ptr @{fn}({call_args})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member in {"LAST_CODE", "LAST_PEER_PORT"}:
            fn = "sa_net_last_code_value" if member == "LAST_CODE" else "sa_net_last_peer_port_value"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}()")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "URLENCODE":
            self.use_runtime("sa_net_urlencode")
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @sa_net_urlencode(ptr {args[0].value})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member in {"TCP_CONNECT", "TLS_CONNECT", "TCP_LISTEN"}:
            fn = {
                "TCP_CONNECT": "sa_net_tcp_connect",
                "TLS_CONNECT": "sa_net_tls_connect",
                "TCP_LISTEN": "sa_net_tcp_listen",
            }[member]
            self.use_runtime(fn)
            second = self.cast_to_i64(args[1])
            third = self.cast_to_i64(args[2])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(ptr {args[0].value}, i64 {second.value}, i64 {third.value})")
            kind = "NET_STREAM" if member != "TCP_LISTEN" else "TCP_LISTENER"
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", kind))
        if member == "TCP_ACCEPT":
            self.use_runtime("sa_net_tcp_accept")
            timeout = self.cast_to_i64(args[1])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_net_tcp_accept(i64 {args[0].value}, i64 {timeout.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "NET_STREAM"))
        if member in {"TCP_LISTENER_CLOSE", "STREAM_CLOSE", "UDP_CLOSE"}:
            fn = {
                "TCP_LISTENER_CLOSE": "sa_net_tcp_listener_close",
                "STREAM_CLOSE": "sa_net_stream_close",
                "UDP_CLOSE": "sa_net_udp_close",
            }[member]
            self.use_runtime(fn)
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(i64 {args[0].value})")
            return self.i32_status(raw)
        if member in {"STREAM_SEND", "UDP_SEND"}:
            fn = "sa_net_stream_send" if member == "STREAM_SEND" else "sa_net_udp_send"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(i64 {args[0].value}, ptr {args[1].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member in {"STREAM_RECV", "UDP_RECV"}:
            fn = "sa_net_stream_recv" if member == "STREAM_RECV" else "sa_net_udp_recv"
            self.use_runtime(fn)
            limit = self.cast_to_i64(args[1])
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @{fn}(i64 {args[0].value}, i64 {limit.value})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member in {"STREAM_SEND_BUFFER", "UDP_SEND_BUFFER"}:
            fn = "sa_net_stream_send_buffer" if member == "STREAM_SEND_BUFFER" else "sa_net_udp_send_buffer"
            self.use_runtime(fn)
            offset = self.cast_to_i64(args[2])
            count = self.cast_to_i64(args[3])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(i64 {args[0].value}, i64 {args[1].value}, i64 {offset.value}, i64 {count.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member in {"STREAM_RECV_BUFFER", "UDP_RECV_BUFFER"}:
            fn = "sa_net_stream_recv_buffer" if member == "STREAM_RECV_BUFFER" else "sa_net_udp_recv_buffer"
            self.use_runtime(fn)
            limit = self.cast_to_i64(args[1])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(i64 {args[0].value}, i64 {limit.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "BUFFER"))
        if member == "UDP_OPEN":
            self.use_runtime("sa_net_udp_open")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_net_udp_open()")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "UDP_SOCKET"))
        if member in {"UDP_BIND", "UDP_CONNECT"}:
            fn = "sa_net_udp_bind" if member == "UDP_BIND" else "sa_net_udp_connect"
            self.use_runtime(fn)
            port = self.cast_to_i64(args[2])
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(i64 {args[0].value}, ptr {args[1].value}, i64 {port.value})")
            return self.i32_status(raw)
        if member == "UDP_SEND_TO":
            self.use_runtime("sa_net_udp_send_to")
            port = self.cast_to_i64(args[2])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_net_udp_send_to(i64 {args[0].value}, ptr {args[1].value}, i64 {port.value}, ptr {args[3].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "UDP_SEND_BUFFER_TO":
            self.use_runtime("sa_net_udp_send_buffer_to")
            port = self.cast_to_i64(args[2])
            offset = self.cast_to_i64(args[4])
            count = self.cast_to_i64(args[5])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_net_udp_send_buffer_to(i64 {args[0].value}, ptr {args[1].value}, i64 {port.value}, i64 {args[3].value}, i64 {offset.value}, i64 {count.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member in {"LOCAL_PORT", "UDP_LOCAL_PORT"}:
            fn = "sa_net_tcp_listener_local_port" if member == "LOCAL_PORT" else "sa_net_udp_local_port"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(i64 {args[0].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        return None

    def file_function_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.FILE":
            return None
        member = split[1].upper()
        args = [LLVMValue("i64", "0", ast.TypeSpec("HANDLE")) if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        handle = "0" if args and is_null(args[0].type_spec or ast.TypeSpec("VOID")) else (args[0].value if args else "0")
        if member == "OPEN":
            self.use_runtime("sa_file_open")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_file_open(ptr {args[0].value}, ptr {args[1].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "FILE"))
        if member == "READ":
            self.use_runtime("sa_file_read")
            temp = self.next_temp()
            count = self.cast_to_i64(args[1])
            self.emit(f"  {temp} = call ptr @sa_file_read(i64 {handle}, i64 {count.value})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member == "WRITE":
            self.use_runtime("sa_file_write")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_file_write(i64 {handle}, ptr {args[1].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "SEEK":
            self.use_runtime("sa_file_seek")
            raw = self.next_temp()
            offset = self.cast_to_i64(args[1])
            self.emit(f"  {raw} = call i32 @sa_file_seek(i64 {handle}, i64 {offset.value}, ptr {args[2].value})")
            return self.i32_status(raw)
        if member in {"TELL", "SIZE"}:
            fn = "sa_file_tell" if member == "TELL" else "sa_file_size"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(i64 {handle})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "CLOSE":
            self.use_runtime("sa_file_close")
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @sa_file_close(i64 {handle})")
            return self.i32_status(raw)
        if member in {"WRITE_TEXT", "APPEND_TEXT"}:
            fn = "sa_file_write_text" if member == "WRITE_TEXT" else "sa_file_append_text"
            self.use_runtime(fn)
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(ptr {args[0].value}, ptr {args[1].value})")
            return self.i32_status(raw)
        if member in {"EXISTS", "IS_FILE", "IS_DIR", "DELETE", "MKDIR"}:
            fn = {
                "EXISTS": "sa_file_exists", "IS_FILE": "sa_file_is_file", "IS_DIR": "sa_file_is_dir",
                "DELETE": "sa_file_delete", "MKDIR": "sa_file_mkdir",
            }[member]
            self.use_runtime(fn)
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(ptr {args[0].value})")
            return self.i32_status(raw)
        if member in {"READ_TEXT", "CWD", "ABSOLUTE", "LAST_ERROR"}:
            fn = {
                "READ_TEXT": "sa_file_read_text", "CWD": "sa_file_cwd",
                "ABSOLUTE": "sa_file_absolute", "LAST_ERROR": "sa_file_last_error_copy",
            }[member]
            self.use_runtime(fn)
            temp = self.next_temp()
            call_args = "" if not args else f"ptr {args[0].value}"
            self.emit(f"  {temp} = call ptr @{fn}({call_args})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        return None

    def desktop_function_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.DESKTOP":
            return None
        member = split[1].upper()
        args = [LLVMValue("i64", "0", ast.TypeSpec("HANDLE")) if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        if member in {"MESSAGE", "OPEN", "CLIPBOARD_SET"}:
            fn = {"MESSAGE": "sa_desktop_message", "OPEN": "sa_desktop_open", "CLIPBOARD_SET": "sa_desktop_clipboard_set"}[member]
            self.use_runtime(fn)
            raw = self.next_temp()
            call_args = ", ".join(f"ptr {arg.value}" for arg in args)
            self.emit(f"  {raw} = call i32 @{fn}({call_args})")
            return self.i32_status(raw)
        if member in {"CLIPBOARD_GET", "LAST_ERROR"}:
            fn = "sa_desktop_clipboard_get" if member == "CLIPBOARD_GET" else "sa_desktop_last_error_copy"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @{fn}()")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        return None

    def binary_function_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.BINARY":
            return None
        member = split[1].upper()
        args = [LLVMValue("i64", "0", ast.TypeSpec("HANDLE")) if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        if member == "NEW":
            self.use_runtime("sa_binary_new")
            length = self.cast_to_i64(args[0])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_binary_new(i64 {length.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "BUFFER"))
        if member == "CLOSE":
            self.use_runtime("sa_binary_close")
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @sa_binary_close(i64 {args[0].value})")
            return self.i32_status(raw)
        if member == "LENGTH":
            self.use_runtime("sa_binary_length")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_binary_length(i64 {args[0].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "SLICE":
            self.use_runtime("sa_binary_slice")
            offset = self.cast_to_i64(args[1])
            count = self.cast_to_i64(args[2])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_binary_slice(i64 {args[0].value}, i64 {offset.value}, i64 {count.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "BUFFER"))
        if member == "COPY":
            self.use_runtime("sa_binary_copy")
            target_offset = self.cast_to_i64(args[1])
            source_offset = self.cast_to_i64(args[3])
            count = self.cast_to_i64(args[4])
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @sa_binary_copy(i64 {args[0].value}, i64 {target_offset.value}, i64 {args[2].value}, i64 {source_offset.value}, i64 {count.value})")
            return self.i32_status(raw)
        if member == "HEX_DECODE":
            self.use_runtime("sa_binary_hex_decode")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_binary_hex_decode(ptr {args[0].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "BUFFER"))
        if member in {"HEX_ENCODE", "LAST_ERROR"}:
            fn = "sa_binary_hex_encode" if member == "HEX_ENCODE" else "sa_binary_last_error_copy"
            self.use_runtime(fn)
            temp = self.next_temp()
            call_args = f"i64 {args[0].value}" if args else ""
            self.emit(f"  {temp} = call ptr @{fn}({call_args})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        pack_functions = {
            "PACK_U16_LE": "sa_binary_pack_u16_le", "PACK_U16_BE": "sa_binary_pack_u16_be",
            "PACK_U32_LE": "sa_binary_pack_u32_le", "PACK_U32_BE": "sa_binary_pack_u32_be",
            "PACK_U64_LE": "sa_binary_pack_u64_le", "PACK_U64_BE": "sa_binary_pack_u64_be",
        }
        if member in pack_functions:
            fn = pack_functions[member]
            self.use_runtime(fn)
            offset = self.cast_to_i64(args[1])
            value = self.cast_to_i64(args[2])
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(i64 {args[0].value}, i64 {offset.value}, i64 {value.value})")
            return self.i32_status(raw)
        unpack_functions = {
            "UNPACK_U16_LE": "sa_binary_unpack_u16_le", "UNPACK_U16_BE": "sa_binary_unpack_u16_be",
            "UNPACK_U32_LE": "sa_binary_unpack_u32_le", "UNPACK_U32_BE": "sa_binary_unpack_u32_be",
            "UNPACK_U64_LE": "sa_binary_unpack_u64_le", "UNPACK_U64_BE": "sa_binary_unpack_u64_be",
        }
        if member in unpack_functions:
            fn = unpack_functions[member]
            self.use_runtime(fn)
            offset = self.cast_to_i64(args[1])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(i64 {args[0].value}, i64 {offset.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "CHECKSUM8":
            self.use_runtime("sa_binary_checksum8")
            offset = self.cast_to_i64(args[1])
            count = self.cast_to_i64(args[2])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_binary_checksum8(i64 {args[0].value}, i64 {offset.value}, i64 {count.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        return None

    def i32_status(self, value: str) -> LLVMValue:
        temp = self.next_temp()
        self.emit(f"  {temp} = icmp ne i32 {value}, 0")
        return LLVMValue("i1", temp, ast.TypeSpec("BOOL"))

    def list_function_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.LIST":
            return None
        member = split[1].upper()
        args = [LLVMValue("i64", "0", ast.TypeSpec("HANDLE")) if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        if member in {"NEW", "NEW_STR"}:
            fn = "sa_list_new" if member == "NEW" else "sa_strlist_new"
            kind = "LIST" if member == "NEW" else "STR_LIST"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}()")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", kind))
        if member == "PUSH":
            self.use_runtime("sa_list_push")
            value = self.cast_to_double(args[1])
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @sa_list_push(i64 {args[0].value}, double {value.value})")
            return self.i32_status(raw)
        if member in {"POP", "GET"}:
            fn = "sa_list_pop" if member == "POP" else "sa_list_get"
            self.use_runtime(fn)
            temp = self.next_temp()
            if member == "GET":
                index = self.cast_to_i64(args[1])
                self.emit(f"  {temp} = call double @{fn}(i64 {args[0].value}, i64 {index.value})")
            else:
                self.emit(f"  {temp} = call double @{fn}(i64 {args[0].value})")
            return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
        if member in {"SET", "INSERT"}:
            fn = "sa_list_set" if member == "SET" else "sa_list_insert"
            self.use_runtime(fn)
            index = self.cast_to_i64(args[1])
            value = self.cast_to_double(args[2])
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(i64 {args[0].value}, i64 {index.value}, double {value.value})")
            return self.i32_status(raw)
        if member in {"REMOVE", "REMOVE_STR"}:
            fn = "sa_list_remove" if member == "REMOVE" else "sa_strlist_remove"
            self.use_runtime(fn)
            index = self.cast_to_i64(args[1])
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(i64 {args[0].value}, i64 {index.value})")
            return self.i32_status(raw)
        if member in {"LENGTH", "LENGTH_STR"}:
            fn = "sa_list_length" if member == "LENGTH" else "sa_strlist_length"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(i64 {args[0].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member in {"CLEAR", "CLOSE", "CLEAR_STR", "CLOSE_STR"}:
            fn = {
                "CLEAR": "sa_list_clear",
                "CLOSE": "sa_list_close",
                "CLEAR_STR": "sa_strlist_clear",
                "CLOSE_STR": "sa_strlist_close",
            }[member]
            self.use_runtime(fn)
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(i64 {args[0].value})")
            return self.i32_status(raw)
        if member == "PUSH_STR":
            self.use_runtime("sa_strlist_push")
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @sa_strlist_push(i64 {args[0].value}, ptr {args[1].value})")
            return self.i32_status(raw)
        if member in {"SET_STR", "INSERT_STR"}:
            fn = "sa_strlist_set" if member == "SET_STR" else "sa_strlist_insert"
            self.use_runtime(fn)
            index = self.cast_to_i64(args[1])
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(i64 {args[0].value}, i64 {index.value}, ptr {args[2].value})")
            return self.i32_status(raw)
        if member in {"POP_STR", "GET_STR", "JOIN_STR", "LAST_ERROR"}:
            fn = {
                "POP_STR": "sa_strlist_pop",
                "GET_STR": "sa_strlist_get",
                "JOIN_STR": "sa_strlist_join",
                "LAST_ERROR": "sa_list_last_error_copy",
            }[member]
            self.use_runtime(fn)
            temp = self.next_temp()
            if member == "GET_STR":
                index = self.cast_to_i64(args[1])
                self.emit(f"  {temp} = call ptr @{fn}(i64 {args[0].value}, i64 {index.value})")
            elif member == "JOIN_STR":
                self.emit(f"  {temp} = call ptr @{fn}(i64 {args[0].value}, ptr {args[1].value})")
            elif member == "POP_STR":
                self.emit(f"  {temp} = call ptr @{fn}(i64 {args[0].value})")
            else:
                self.emit(f"  {temp} = call ptr @{fn}()")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        return None

    def map_function_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.MAP":
            return None
        member = split[1].upper()
        args = [LLVMValue("i64", "0", ast.TypeSpec("HANDLE")) if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        if member in {"NEW", "NEW_STR"}:
            fn = "sa_map_new" if member == "NEW" else "sa_strmap_new"
            kind = "MAP" if member == "NEW" else "STR_MAP"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}()")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", kind))
        if member == "SET":
            self.use_runtime("sa_map_set")
            value = self.cast_to_double(args[2])
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @sa_map_set(i64 {args[0].value}, ptr {args[1].value}, double {value.value})")
            return self.i32_status(raw)
        if member == "SET_STR":
            self.use_runtime("sa_strmap_set")
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @sa_strmap_set(i64 {args[0].value}, ptr {args[1].value}, ptr {args[2].value})")
            return self.i32_status(raw)
        if member == "GET":
            self.use_runtime("sa_map_get")
            temp = self.next_temp()
            self.emit(f"  {temp} = call double @sa_map_get(i64 {args[0].value}, ptr {args[1].value})")
            return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
        if member in {"HAS", "HAS_STR", "REMOVE", "REMOVE_STR"}:
            fn = {
                "HAS": "sa_map_has",
                "HAS_STR": "sa_strmap_has",
                "REMOVE": "sa_map_remove",
                "REMOVE_STR": "sa_strmap_remove",
            }[member]
            self.use_runtime(fn)
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(i64 {args[0].value}, ptr {args[1].value})")
            return self.i32_status(raw)
        if member in {"LENGTH", "LENGTH_STR"}:
            fn = "sa_map_length" if member == "LENGTH" else "sa_strmap_length"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(i64 {args[0].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member in {"KEYS", "KEYS_STR"}:
            fn = "sa_map_keys" if member == "KEYS" else "sa_strmap_keys"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(i64 {args[0].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "STR_LIST"))
        if member in {"CLEAR", "CLOSE", "CLEAR_STR", "CLOSE_STR"}:
            fn = {
                "CLEAR": "sa_map_clear",
                "CLOSE": "sa_map_close",
                "CLEAR_STR": "sa_strmap_clear",
                "CLOSE_STR": "sa_strmap_close",
            }[member]
            self.use_runtime(fn)
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(i64 {args[0].value})")
            return self.i32_status(raw)
        if member in {"GET_STR", "LAST_ERROR"}:
            fn = "sa_strmap_get" if member == "GET_STR" else "sa_map_last_error_copy"
            self.use_runtime(fn)
            temp = self.next_temp()
            if member == "GET_STR":
                self.emit(f"  {temp} = call ptr @{fn}(i64 {args[0].value}, ptr {args[1].value})")
            else:
                self.emit(f"  {temp} = call ptr @{fn}()")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        return None

    def gui_function_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.GUI":
            return None
        member = split[1].upper()
        args = [LLVMValue("i64", "0", ast.TypeSpec("HANDLE")) if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        if member == "WINDOW":
            self.use_runtime("sa_gui_window")
            width = self.cast_to_i64(args[1])
            height = self.cast_to_i64(args[2])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_gui_window(ptr {args[0].value}, i64 {width.value}, i64 {height.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "WINDOW"))
        if member == "BUTTON":
            self.use_runtime("sa_gui_button")
            values = [self.cast_to_i64(args[i]).value for i in (1, 3, 4, 5, 6)]
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_gui_button(i64 {args[0].value}, i64 {values[0]}, ptr {args[2].value}, i64 {values[1]}, i64 {values[2]}, i64 {values[3]}, i64 {values[4]})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "WIDGET"))
        if member == "LABEL":
            self.use_runtime("sa_gui_label")
            values = [self.cast_to_i64(args[i]).value for i in (2, 3, 4, 5)]
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_gui_label(i64 {args[0].value}, ptr {args[1].value}, i64 {values[0]}, i64 {values[1]}, i64 {values[2]}, i64 {values[3]})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "WIDGET"))
        if member == "TEXTBOX":
            self.use_runtime("sa_gui_textbox")
            values = [self.cast_to_i64(args[i]).value for i in (1, 2, 3, 4)]
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_gui_textbox(i64 {args[0].value}, i64 {values[0]}, i64 {values[1]}, i64 {values[2]}, i64 {values[3]})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "WIDGET"))
        if member == "SET_TEXT":
            self.use_runtime("sa_gui_set_text")
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @sa_gui_set_text(i64 {args[0].value}, ptr {args[1].value})")
            return self.i32_status(raw)
        if member == "WAIT_EVENT":
            self.use_runtime("sa_gui_wait_event")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_gui_wait_event()")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "CLOSE":
            self.use_runtime("sa_gui_close")
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @sa_gui_close(i64 {args[0].value})")
            return self.i32_status(raw)
        if member in {"GET_TEXT", "LAST_ERROR"}:
            fn = "sa_gui_get_text" if member == "GET_TEXT" else "sa_gui_last_error_copy"
            self.use_runtime(fn)
            temp = self.next_temp()
            if member == "GET_TEXT":
                self.emit(f"  {temp} = call ptr @{fn}(i64 {args[0].value})")
            else:
                self.emit(f"  {temp} = call ptr @{fn}()")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        return None

    def coerce_str_arg(self, value: LLVMValue) -> str:
        # SLICE 的 start/count 是 i64，其余字符串参数是 ptr。这里按已求值的类型直接取值，
        # 数值参数确保是 i64（SLICE 第 2、3 参数）。
        if value.type_name == "ptr":
            return value.value
        return self.cast_to_i64(value).value

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

    def binary_expr(self, expr: ast.Binary) -> LLVMValue:
        left = self.expr(expr.left)
        right = self.expr(expr.right)
        op = expr.op
        if op in {"AND", "OR"}:
            lhs = self.truthy(left)
            rhs = self.truthy(right)
            temp = self.next_temp()
            instr = "and" if op == "AND" else "or"
            self.emit(f"  {temp} = {instr} i1 {lhs.value}, {rhs.value}")
            return LLVMValue("i1", temp, ast.TypeSpec("BOOL"))
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

    def cast_value(self, value: LLVMValue, target: ast.TypeSpec) -> LLVMValue:
        if is_symbol(target):
            if value.type_name != "ptr":
                raise SonCompileError("native 后端 SYMBOL 赋值需要 SYMBOL 值")
            return LLVMValue("ptr", value.value, target)
        if is_ptr(target) or is_cptr(target):
            if value.type_name == "ptr":
                return LLVMValue("ptr", value.value, target)
            int_value = self.cast_to_i64(value)
            temp = self.next_temp()
            self.emit(f"  {temp} = inttoptr i64 {int_value.value} to ptr")
            return LLVMValue("ptr", temp, target)
        if is_handle(target):
            if value.type_name == "i64":
                return LLVMValue("i64", value.value, target)
            if value.type_name == "ptr" and is_null(value.type_spec or ast.TypeSpec("VOID")):
                return LLVMValue("i64", "0", target)
            raise SonCompileError("native 后端 HANDLE 只能接收同 kind HANDLE 或 NULL")
        if target.name == "STRING":
            if value.type_name != "ptr":
                raise SonCompileError("native 后端字符串赋值需要字符串值")
            return LLVMValue("ptr", value.value, target)
        if target.name == "ENTITY":
            if value.type_name != self.llvm_type(target):
                raise SonCompileError("native 后端 ENTITY 赋值需要同类型 ENTITY 值")
            return LLVMValue(value.type_name, value.value, target)
        if target.name == "BOOL":
            return self.truthy(value)
        if target.name == "NUM" and target.subtype == "DOUBLE":
            return self.cast_to_double(value)
        if target.name == "NUM":
            return self.cast_to_i64(value)
        if target.name == "VOID":
            return value
        raise SonCompileError(f"native 后端暂不支持类型转换: {target.name}")

    def cast_to_i64(self, value: LLVMValue) -> LLVMValue:
        if value.type_name == "i64":
            return LLVMValue("i64", value.value, ast.TypeSpec("NUM", "LONG"))
        temp = self.next_temp()
        if value.type_name == "i1":
            self.emit(f"  {temp} = zext i1 {value.value} to i64")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if value.type_name == "double":
            self.emit(f"  {temp} = fptosi double {value.value} to i64")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if value.type_name == "ptr":
            self.emit(f"  {temp} = ptrtoint ptr {value.value} to i64")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        raise SonCompileError(f"native 后端无法转为 LONG: {value.type_name}")

    def cast_to_double(self, value: LLVMValue) -> LLVMValue:
        if value.type_name == "double":
            return LLVMValue("double", value.value, ast.TypeSpec("NUM", "DOUBLE"))
        temp = self.next_temp()
        if value.type_name == "i64":
            self.emit(f"  {temp} = sitofp i64 {value.value} to double")
            return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
        if value.type_name == "i1":
            wide = self.cast_to_i64(value)
            self.emit(f"  {temp} = sitofp i64 {wide.value} to double")
            return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
        if value.type_name == "ptr":
            wide = self.cast_to_i64(value)
            self.emit(f"  {temp} = sitofp i64 {wide.value} to double")
            return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
        raise SonCompileError(f"native 后端无法转为 DOUBLE: {value.type_name}")

    def truthy(self, value: LLVMValue) -> LLVMValue:
        if value.type_name == "i1":
            return LLVMValue("i1", value.value, ast.TypeSpec("BOOL"))
        temp = self.next_temp()
        if value.type_name == "i64":
            self.emit(f"  {temp} = icmp ne i64 {value.value}, 0")
            return LLVMValue("i1", temp, ast.TypeSpec("BOOL"))
        if value.type_name == "double":
            self.emit(f"  {temp} = fcmp one double {value.value}, 0.0")
            return LLVMValue("i1", temp, ast.TypeSpec("BOOL"))
        if value.type_name == "ptr":
            self.emit(f"  {temp} = icmp ne ptr {value.value}, null")
            return LLVMValue("i1", temp, ast.TypeSpec("BOOL"))
        raise SonCompileError(f"native 后端无法转为 BOOL: {value.type_name}")

    def global_slots(self) -> dict[str, VarSlot]:
        return {
            decl.name.lower(): VarSlot(decl.name, decl.type_spec, f"@{self.c_ident(decl.name)}")
            for decl in self.checked.program.declarations
        }

    def slot(self, name: str, line_no: int) -> VarSlot:
        key = name.lower()
        if key not in self.slots:
            raise SonCompileError(f"native 后端变量未声明: {name}", line_no)
        return self.slots[key]

    def current_symbols(self) -> dict[str, Symbol]:
        return {
            key: Symbol(slot.name, slot.type_spec, True, slot.by_ref)
            for key, slot in self.slots.items()
        }

    def type_of_expr(self, expr: ast.Expr) -> ast.TypeSpec:
        return type_of(
            expr,
            self.current_symbols(),
            self.checked.subs,
            self.checked.entities,
            self.checked.uses,
            self.checked.external_modules,
            self.checked.c_funcs,
        )

    def is_string_type(self, type_spec: ast.TypeSpec | None) -> bool:
        return type_spec is not None and self.is_string_scalar(type_spec)

    def is_pointer_like_type(self, type_spec: ast.TypeSpec | None) -> bool:
        return type_spec is not None and (is_ptr(type_spec) or is_cptr(type_spec) or is_null(type_spec))

    def error_message_ptr(self, error_ptr: str) -> str:
        field = self.next_temp()
        msg = self.next_temp()
        self.emit(f"  {field} = getelementptr inbounds %SaError, ptr {error_ptr}, i64 0, i32 2")
        self.emit(f"  {msg} = load ptr, ptr {field}")
        return msg

    def llvm_type(self, type_spec: ast.TypeSpec) -> str:
        if type_spec.array_size is not None:
            return f"[{type_spec.array_size} x {self.llvm_type(self.array_element_type(type_spec))}]"
        if type_spec.name == "VOID":
            return "void"
        if type_spec.name == "BOOL":
            return "i1"
        if type_spec.name == "HANDLE":
            return "i64"
        if type_spec.name in {"STRING", "PTR", "CPTR", "SYMBOL"}:
            return "ptr"
        if type_spec.name == "ERROR":
            return "%SaError"
        if type_spec.name == "ENTITY":
            return self.entity_type_from_spec(type_spec)
        if type_spec.name == "NUM" and type_spec.subtype == "DOUBLE":
            return "double"
        if type_spec.name == "NUM":
            return "i64"
        raise SonCompileError(f"native 后端暂不支持类型: {type_spec.name}")

    def c_abi_type(self, type_spec: ast.TypeSpec) -> str:
        return "i32" if is_bool(type_spec) else self.llvm_type(type_spec)

    def c_abi_param_decl(self, param: ast.Param) -> str:
        name = self.c_ident(param.name)
        if param.by_ref:
            return f"ptr %{name}"
        return f"{self.c_abi_type(param.type_spec)} %{name}"

    def default_value(self, type_spec: ast.TypeSpec) -> str:
        if type_spec.name == "VOID":
            return ""
        if type_spec.array_size is not None:
            return "zeroinitializer"
        if is_string(type_spec):
            return "@.sa_empty"
        if is_ptr(type_spec) or is_cptr(type_spec) or is_symbol(type_spec):
            return "null"
        if is_error(type_spec):
            return "zeroinitializer"
        if type_spec.name == "ENTITY":
            return "zeroinitializer"
        if is_bool(type_spec):
            return "0"
        if is_numeric(type_spec) and type_spec.subtype == "DOUBLE":
            return "0.0"
        return "0"

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

    def param_decl(self, param: ast.Param) -> str:
        name = self.c_ident(param.name)
        if param.by_ref:
            return f"ptr %{name}"
        return f"{self.llvm_type(param.type_spec)} %{name}"

    def string_ptr(self, value: str) -> str:
        escaped, size = llvm_string_literal(value)
        name = f"@.sa_str_{self.string_index}"
        self.string_index += 1
        self.string_constants.append((name, size, escaped))
        return name

    def string_constant_lines(self) -> list[str]:
        return [f"{name} = private unnamed_addr constant [{size} x i8] c\"{escaped}\"" for name, size, escaped in self.string_constants]

    def source_comment(self, line_no: int) -> str:
        source = self.source_lines.get(line_no)
        if source is None:
            return ""
        return f"  ; SA {line_no}: {source.replace(chr(10), ' ')}"

    def sub_name(self, name: str) -> str:
        return self.c_ident(name)

    def c_ident(self, name: str) -> str:
        return make_c_ident(name)

    def label_name(self, name: str) -> str:
        return "sa_label_" + name.lower().replace(".", "_")

    def unique_label(self, prefix: str) -> str:
        self.label_index += 1
        return f"sa_{prefix}_{self.label_index}"

    def next_temp(self) -> str:
        self.temp_index += 1
        return f"%sa_tmp_{self.temp_index}"

    def emit(self, line: str) -> None:
        if line:
            self.lines.append(line)


def generate_native_llvm_ir(checked: CheckedProgram, main_init_calls: list[str] | None = None, main_free_calls: list[str] | None = None) -> str:
    return NativeLLVMGen(checked, main_init_calls=main_init_calls, main_free_calls=main_free_calls).generate()


# native 后端不在 IR 里重写算法，而是 declare + 链接 C 运行时（c_runtime.py 的
# RUNTIME_SOURCE 已分离编译就绪）。下表是用到的运行时函数对应的 LLVM 声明，
# generate() 只发射 used_runtime 里实际登记过的那些。
RUNTIME_SIGNATURES: dict[str, str] = {
    # 字符串
    "sa_strdup": "declare ptr @sa_strdup(ptr)",
    "sa_str_length": "declare i64 @sa_str_length(ptr)",
    "sa_str_concat": "declare ptr @sa_str_concat(ptr, ptr)",
    "sa_str_slice": "declare ptr @sa_str_slice(ptr, i64, i64)",
    "sa_str_find": "declare i64 @sa_str_find(ptr, ptr)",
    "sa_str_upper": "declare ptr @sa_str_upper(ptr)",
    "sa_str_lower": "declare ptr @sa_str_lower(ptr)",
    "sa_str_replace": "declare ptr @sa_str_replace(ptr, ptr, ptr)",
    "sa_net_http_get": "declare ptr @sa_net_http_get(ptr)",
    "sa_net_http_status": "declare i64 @sa_net_http_status(ptr)",
    "sa_net_http_post": "declare ptr @sa_net_http_post(ptr, ptr, ptr)",
    "sa_net_http_request": "declare ptr @sa_net_http_request(ptr, ptr, ptr, ptr)",
    "sa_net_http_request_status": "declare i64 @sa_net_http_request_status(ptr, ptr, ptr, ptr)",
    "sa_net_http_request_timeout": "declare ptr @sa_net_http_request_timeout(ptr, ptr, ptr, ptr, i64)",
    "sa_net_http_request_status_timeout": "declare i64 @sa_net_http_request_status_timeout(ptr, ptr, ptr, ptr, i64)",
    "sa_net_last_headers_copy": "declare ptr @sa_net_last_headers_copy()",
    "sa_net_last_error_copy": "declare ptr @sa_net_last_error_copy()",
    "sa_net_last_code_value": "declare i64 @sa_net_last_code_value()",
    "sa_net_last_peer_host_copy": "declare ptr @sa_net_last_peer_host_copy()",
    "sa_net_last_peer_port_value": "declare i64 @sa_net_last_peer_port_value()",
    "sa_net_urlencode": "declare ptr @sa_net_urlencode(ptr)",
    "sa_net_dns": "declare ptr @sa_net_dns(ptr)",
    "sa_net_tcp_connect": "declare i64 @sa_net_tcp_connect(ptr, i64, i64)",
    "sa_net_tls_connect": "declare i64 @sa_net_tls_connect(ptr, i64, i64)",
    "sa_net_tcp_listen": "declare i64 @sa_net_tcp_listen(ptr, i64, i64)",
    "sa_net_tcp_accept": "declare i64 @sa_net_tcp_accept(i64, i64)",
    "sa_net_tcp_listener_close": "declare i32 @sa_net_tcp_listener_close(i64)",
    "sa_net_tcp_listener_local_port": "declare i64 @sa_net_tcp_listener_local_port(i64)",
    "sa_net_stream_send": "declare i64 @sa_net_stream_send(i64, ptr)",
    "sa_net_stream_recv": "declare ptr @sa_net_stream_recv(i64, i64)",
    "sa_net_stream_send_buffer": "declare i64 @sa_net_stream_send_buffer(i64, i64, i64, i64)",
    "sa_net_stream_recv_buffer": "declare i64 @sa_net_stream_recv_buffer(i64, i64)",
    "sa_net_stream_close": "declare i32 @sa_net_stream_close(i64)",
    "sa_net_udp_open": "declare i64 @sa_net_udp_open()",
    "sa_net_udp_bind": "declare i32 @sa_net_udp_bind(i64, ptr, i64)",
    "sa_net_udp_connect": "declare i32 @sa_net_udp_connect(i64, ptr, i64)",
    "sa_net_udp_send": "declare i64 @sa_net_udp_send(i64, ptr)",
    "sa_net_udp_send_to": "declare i64 @sa_net_udp_send_to(i64, ptr, i64, ptr)",
    "sa_net_udp_recv": "declare ptr @sa_net_udp_recv(i64, i64)",
    "sa_net_udp_send_buffer": "declare i64 @sa_net_udp_send_buffer(i64, i64, i64, i64)",
    "sa_net_udp_send_buffer_to": "declare i64 @sa_net_udp_send_buffer_to(i64, ptr, i64, i64, i64, i64)",
    "sa_net_udp_recv_buffer": "declare i64 @sa_net_udp_recv_buffer(i64, i64)",
    "sa_net_udp_close": "declare i32 @sa_net_udp_close(i64)",
    "sa_net_udp_local_port": "declare i64 @sa_net_udp_local_port(i64)",
    # BINARY
    "sa_binary_new": "declare i64 @sa_binary_new(i64)",
    # LIST
    "sa_list_new": "declare i64 @sa_list_new()",
    "sa_list_push": "declare i32 @sa_list_push(i64, double)",
    "sa_list_pop": "declare double @sa_list_pop(i64)",
    "sa_list_get": "declare double @sa_list_get(i64, i64)",
    "sa_list_set": "declare i32 @sa_list_set(i64, i64, double)",
    "sa_list_insert": "declare i32 @sa_list_insert(i64, i64, double)",
    "sa_list_remove": "declare i32 @sa_list_remove(i64, i64)",
    "sa_list_length": "declare i64 @sa_list_length(i64)",
    "sa_list_clear": "declare i32 @sa_list_clear(i64)",
    "sa_list_close": "declare i32 @sa_list_close(i64)",
    "sa_strlist_new": "declare i64 @sa_strlist_new()",
    "sa_strlist_push": "declare i32 @sa_strlist_push(i64, ptr)",
    "sa_strlist_pop": "declare ptr @sa_strlist_pop(i64)",
    "sa_strlist_get": "declare ptr @sa_strlist_get(i64, i64)",
    "sa_strlist_set": "declare i32 @sa_strlist_set(i64, i64, ptr)",
    "sa_strlist_insert": "declare i32 @sa_strlist_insert(i64, i64, ptr)",
    "sa_strlist_remove": "declare i32 @sa_strlist_remove(i64, i64)",
    "sa_strlist_length": "declare i64 @sa_strlist_length(i64)",
    "sa_strlist_clear": "declare i32 @sa_strlist_clear(i64)",
    "sa_strlist_close": "declare i32 @sa_strlist_close(i64)",
    "sa_strlist_join": "declare ptr @sa_strlist_join(i64, ptr)",
    "sa_list_last_error_copy": "declare ptr @sa_list_last_error_copy()",
    # MAP
    "sa_map_new": "declare i64 @sa_map_new()",
    "sa_map_set": "declare i32 @sa_map_set(i64, ptr, double)",
    "sa_map_get": "declare double @sa_map_get(i64, ptr)",
    "sa_map_has": "declare i32 @sa_map_has(i64, ptr)",
    "sa_map_remove": "declare i32 @sa_map_remove(i64, ptr)",
    "sa_map_length": "declare i64 @sa_map_length(i64)",
    "sa_map_keys": "declare i64 @sa_map_keys(i64)",
    "sa_map_clear": "declare i32 @sa_map_clear(i64)",
    "sa_map_close": "declare i32 @sa_map_close(i64)",
    "sa_strmap_new": "declare i64 @sa_strmap_new()",
    "sa_strmap_set": "declare i32 @sa_strmap_set(i64, ptr, ptr)",
    "sa_strmap_get": "declare ptr @sa_strmap_get(i64, ptr)",
    "sa_strmap_has": "declare i32 @sa_strmap_has(i64, ptr)",
    "sa_strmap_remove": "declare i32 @sa_strmap_remove(i64, ptr)",
    "sa_strmap_length": "declare i64 @sa_strmap_length(i64)",
    "sa_strmap_keys": "declare i64 @sa_strmap_keys(i64)",
    "sa_strmap_clear": "declare i32 @sa_strmap_clear(i64)",
    "sa_strmap_close": "declare i32 @sa_strmap_close(i64)",
    "sa_map_last_error_copy": "declare ptr @sa_map_last_error_copy()",
    # GUI
    "sa_gui_window": "declare i64 @sa_gui_window(ptr, i64, i64)",
    "sa_gui_button": "declare i64 @sa_gui_button(i64, i64, ptr, i64, i64, i64, i64)",
    "sa_gui_label": "declare i64 @sa_gui_label(i64, ptr, i64, i64, i64, i64)",
    "sa_gui_textbox": "declare i64 @sa_gui_textbox(i64, i64, i64, i64, i64)",
    "sa_gui_set_text": "declare i32 @sa_gui_set_text(i64, ptr)",
    "sa_gui_get_text": "declare ptr @sa_gui_get_text(i64)",
    "sa_gui_wait_event": "declare i64 @sa_gui_wait_event()",
    "sa_gui_close": "declare i32 @sa_gui_close(i64)",
    "sa_gui_last_error_copy": "declare ptr @sa_gui_last_error_copy()",
    "sa_binary_close": "declare i32 @sa_binary_close(i64)",
    "sa_binary_length": "declare i64 @sa_binary_length(i64)",
    "sa_binary_slice": "declare i64 @sa_binary_slice(i64, i64, i64)",
    "sa_binary_copy": "declare i32 @sa_binary_copy(i64, i64, i64, i64, i64)",
    "sa_binary_hex_decode": "declare i64 @sa_binary_hex_decode(ptr)",
    "sa_binary_hex_encode": "declare ptr @sa_binary_hex_encode(i64)",
    "sa_binary_pack_u16_le": "declare i32 @sa_binary_pack_u16_le(i64, i64, i64)",
    "sa_binary_pack_u16_be": "declare i32 @sa_binary_pack_u16_be(i64, i64, i64)",
    "sa_binary_pack_u32_le": "declare i32 @sa_binary_pack_u32_le(i64, i64, i64)",
    "sa_binary_pack_u32_be": "declare i32 @sa_binary_pack_u32_be(i64, i64, i64)",
    "sa_binary_pack_u64_le": "declare i32 @sa_binary_pack_u64_le(i64, i64, i64)",
    "sa_binary_pack_u64_be": "declare i32 @sa_binary_pack_u64_be(i64, i64, i64)",
    "sa_binary_unpack_u16_le": "declare i64 @sa_binary_unpack_u16_le(i64, i64)",
    "sa_binary_unpack_u16_be": "declare i64 @sa_binary_unpack_u16_be(i64, i64)",
    "sa_binary_unpack_u32_le": "declare i64 @sa_binary_unpack_u32_le(i64, i64)",
    "sa_binary_unpack_u32_be": "declare i64 @sa_binary_unpack_u32_be(i64, i64)",
    "sa_binary_unpack_u64_le": "declare i64 @sa_binary_unpack_u64_le(i64, i64)",
    "sa_binary_unpack_u64_be": "declare i64 @sa_binary_unpack_u64_be(i64, i64)",
    "sa_binary_checksum8": "declare i64 @sa_binary_checksum8(i64, i64, i64)",
    "sa_binary_last_error_copy": "declare ptr @sa_binary_last_error_copy()",
    # FILE
    "sa_file_open": "declare i64 @sa_file_open(ptr, ptr)",
    "sa_file_read": "declare ptr @sa_file_read(i64, i64)",
    "sa_file_write": "declare i64 @sa_file_write(i64, ptr)",
    "sa_file_seek": "declare i32 @sa_file_seek(i64, i64, ptr)",
    "sa_file_tell": "declare i64 @sa_file_tell(i64)",
    "sa_file_size": "declare i64 @sa_file_size(i64)",
    "sa_file_close": "declare i32 @sa_file_close(i64)",
    "sa_file_read_text": "declare ptr @sa_file_read_text(ptr)",
    "sa_file_write_text": "declare i32 @sa_file_write_text(ptr, ptr)",
    "sa_file_append_text": "declare i32 @sa_file_append_text(ptr, ptr)",
    "sa_file_exists": "declare i32 @sa_file_exists(ptr)",
    "sa_file_is_file": "declare i32 @sa_file_is_file(ptr)",
    "sa_file_is_dir": "declare i32 @sa_file_is_dir(ptr)",
    "sa_file_delete": "declare i32 @sa_file_delete(ptr)",
    "sa_file_mkdir": "declare i32 @sa_file_mkdir(ptr)",
    "sa_file_cwd": "declare ptr @sa_file_cwd()",
    "sa_file_absolute": "declare ptr @sa_file_absolute(ptr)",
    "sa_file_last_error_copy": "declare ptr @sa_file_last_error_copy()",
    # DESKTOP
    "sa_desktop_message": "declare i32 @sa_desktop_message(ptr, ptr)",
    "sa_desktop_open": "declare i32 @sa_desktop_open(ptr)",
    "sa_desktop_clipboard_set": "declare i32 @sa_desktop_clipboard_set(ptr)",
    "sa_desktop_clipboard_get": "declare ptr @sa_desktop_clipboard_get()",
    "sa_desktop_last_error_copy": "declare ptr @sa_desktop_last_error_copy()",
    "sa_set_string": "declare void @sa_set_string(ptr, ptr)",
    "sa_number": "declare double @sa_number(ptr)",
    "sa_to_string_long": "declare ptr @sa_to_string_long(i64)",
    "sa_to_string_double": "declare ptr @sa_to_string_double(double)",
    "sa_to_string_pointer": "declare ptr @sa_to_string_pointer(ptr)",
    "sa_sb_init": "declare void @sa_sb_init(ptr)",
    "sa_sb_append": "declare void @sa_sb_append(ptr, ptr)",
    "sa_sb_take": "declare ptr @sa_sb_take(ptr)",
    "strcmp": "declare i32 @strcmp(ptr, ptr)",
    # 标准 C
    "free": "declare void @free(ptr)",
    "exit": "declare void @exit(i32)",
    "pow": "declare double @pow(double, double)",
    # SYMBOL
    "sa_symbol_const": "declare ptr @sa_symbol_const(ptr)",
    "sa_symbol_var": "declare ptr @sa_symbol_var(ptr)",
    "sa_symbol_op": "declare ptr @sa_symbol_op(i8, ptr, ptr)",
    "sa_symbol_clone": "declare ptr @sa_symbol_clone(ptr)",
    "sa_symbol_eval": "declare double @sa_symbol_eval(ptr)",
    "sa_symbol_subst": "declare ptr @sa_symbol_subst(ptr, ptr, double)",
    "sa_symbol_deriv": "declare ptr @sa_symbol_deriv(ptr, ptr)",
    "sa_symbol_simplify": "declare ptr @sa_symbol_simplify(ptr)",
    "sa_symbol_free": "declare void @sa_symbol_free(ptr)",
    "sa_symbol_to_string": "declare ptr @sa_symbol_to_string(ptr)",
    # IO
    "sa_read_line": "declare void @sa_read_line(ptr, i64)",
    "sa_cls": "declare void @sa_cls()",
    "sa_setup_console": "declare void @sa_setup_console()",
    # ERROR / TRY-CATCH
    "sa_try_push_env": "declare ptr @sa_try_push_env()",
    "sa_try_pop": "declare void @sa_try_pop()",
    "sa_current_error": "@sa_current_error = external global %SaError",
    "sa_set_error": "declare void @sa_set_error(ptr, ptr)",
    "sa_error_clear": "declare void @sa_error_clear(ptr)",
    "sa_raise_new": "declare void @sa_raise_new(ptr, ptr, i32, ptr)",
    "sa_raise_error": "declare void @sa_raise_error(ptr)",
    "sa_throw_dispatch": "declare void @sa_throw_dispatch()",
    "_setjmp": "declare i32 @_setjmp(ptr, ptr)",
    "llvm.frameaddress": "declare ptr @llvm.frameaddress.p0(i32 immarg)",
}


def llvm_string_literal(value: str) -> tuple[str, int]:
    data = value.encode("utf-8") + b"\0"
    escaped = "".join(llvm_escape_byte(item) for item in data)
    return escaped, len(data)


def llvm_escape_byte(value: int) -> str:
    if 32 <= value <= 126 and value not in {34, 92}:
        return chr(value)
    return f"\\{value:02X}"


def llvm_int_literal(value: str) -> str:
    raw = value.replace("_", "")
    if raw.lower().startswith("0x"):
        return str(int(raw, 16))
    return str(int(raw, 10))


def llvm_double_literal(value: str) -> str:
    # 用 IEEE754 位模式（0x + 16 hex）发射 double 常量。
    # 直接打印十进制（如 ".17g"）会丢小数点（0.0 -> "0"，被当成整型字面量），
    # 或因往返精度问题被 LLVM 拒绝；位模式表示精确且永远合法。
    bits = struct.unpack("<Q", struct.pack("<d", float(value.replace("_", ""))))[0]
    return f"0x{bits:016X}"


def sub_gosub_lines(sub: ast.Subroutine) -> list[int]:
    lines: list[int] = []
    for stmt in sub.body:
        lines.extend(stmt_gosub_lines(stmt))
    return list(dict.fromkeys(lines))


def stmt_gosub_lines(stmt: ast.Stmt) -> list[int]:
    if isinstance(stmt, ast.Gosub):
        return [stmt.line_no]
    if isinstance(stmt, ast.If):
        lines: list[int] = []
        for inner in stmt.body:
            lines.extend(stmt_gosub_lines(inner))
        for branch in stmt.elifs:
            for inner in branch.body:
                lines.extend(stmt_gosub_lines(inner))
        for inner in stmt.else_body:
            lines.extend(stmt_gosub_lines(inner))
        return lines
    if isinstance(stmt, ast.ForLoop | ast.WhileLoop):
        lines: list[int] = []
        for inner in stmt.body:
            lines.extend(stmt_gosub_lines(inner))
        return lines
    return []
