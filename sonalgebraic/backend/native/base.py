from __future__ import annotations

from dataclasses import dataclass
import struct

from ...analysis.semantics import CheckedProgram, Symbol
from ...analysis.typesys import is_error, is_symbol
from ...core import ast
from ...core.errors import SonCompileError
from ...core.names import c_ident as make_c_ident
from .runtime_decls import RUNTIME_SIGNATURES


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


class NativeGenBase:
    """生成器的共享状态与底层设施。

    所有 mixin 都继承它：状态集中在 __init__ 之外的这一处声明，mixin 里对
    self.lines / self.slots 的访问才有据可查，不是靠运行时凑巧拼出来的。
    """

    checked: CheckedProgram
    main_init_calls: list[str]
    main_free_calls: list[str]
    temp_index: int
    string_index: int
    label_index: int
    string_constants: list[tuple[str, int, str]]
    string_symbols: dict[str, str]
    lines: list[str]
    entry_allocas: list[str]
    terminated: bool
    slots: dict[str, VarSlot]
    current_sub: ast.Subroutine | None
    current_gosub_lines: list[int]
    gosub_stack_ptr: str | None
    gosub_top_ptr: str | None
    used_runtime: set[str]
    used_c_funcs: dict[str, ast.CFunctionDecl]
    used_external_subs: dict[str, ast.Subroutine]
    used_external_consts: dict[str, ast.Declaration]
    scope_resources: list[list[VarSlot]]
    temp_cleanup: list[list[str]]
    aggregate_temp_cleanup: dict[str, str]

    # --- 资源清理基础设施（确定性内存模型，复刻 C 后端） ---

    def begin_stmt(self) -> None:
        self.temp_cleanup.append([])

    def end_stmt(self) -> None:
        for line in self.temp_cleanup.pop():
            self.emit(line)

    def add_temp_cleanup(self, line: str) -> None:
        if self.temp_cleanup:
            self.temp_cleanup[-1].append(line)

    def temp_cleanup_line(self, value: str, type_spec: ast.TypeSpec) -> str | None:
        """STRING / SYMBOL 临时量的释放行。登记（内置函数、SUB 返回值）和接管（adopt）都以这一个文本为准。"""
        if self.is_string_scalar(type_spec):
            return f"  call void @free(ptr {value})"
        if is_symbol(type_spec):
            return f"  call void @sa_symbol_free(ptr {value})"
        return None

    def register_temp_cleanup(self, value: str, type_spec: ast.TypeSpec) -> None:
        """把一个刚算出来的托管堆值登记成本语句的临时量：语句结束没人接管就释放。"""
        line = self.temp_cleanup_line(value, type_spec)
        if line is not None:
            self.use_runtime("free" if self.is_string_scalar(type_spec) else "sa_symbol_free")
            self.add_temp_cleanup(line)
            return
        ptr = self.aggregate_temp_ptr(value, type_spec)
        if ptr is None:
            return
        # 释放 IR 先截下来延后发射；块里只引用 entry 里的 alloca，放到语句尾任何位置都支配得住
        block = "\n".join(self.capture_lines(lambda: self.emit_free_slot(VarSlot("", type_spec, ptr))))
        if block:
            self.aggregate_temp_cleanup[value] = block
            self.add_temp_cleanup(block)

    def emit_free_value(self, value: str, type_spec: ast.TypeSpec) -> None:
        """当场释放一个按值拿着的托管临时量（被语句丢弃的 SUB 返回值）。"""
        line = self.temp_cleanup_line(value, type_spec)
        if line is not None:
            self.use_runtime("free" if self.is_string_scalar(type_spec) else "sa_symbol_free")
            self.emit(line)
            return
        ptr = self.aggregate_temp_ptr(value, type_spec)
        if ptr is not None:
            self.emit_free_slot(VarSlot("", type_spec, ptr))

    def aggregate_temp_ptr(self, value: str, type_spec: ast.TypeSpec) -> str | None:
        """ENTITY/ERROR 是按值传的聚合，free 要逐字段走指针：先把 SSA 值落到栈槽上。"""
        if type_spec.array_size is not None or not self.type_has_managed_resources(type_spec):
            return None
        ptr = self.alloca(self.llvm_type(type_spec))
        self.emit(f"  store {self.llvm_type(type_spec)} {value}, ptr {ptr}")
        return ptr

    def adopt_temp_cleanup(self, value: str, type_spec: ast.TypeSpec) -> bool:
        """value 若是本语句刚生成、登记了释放的堆临时量（F-string、CONCAT、DERIV、SUB 返回值……），
        摘掉释放行、所有权归调用者；与 C 后端 adopt_temp_cleanup 同一思路。"""
        entry = self.temp_cleanup_line(value, type_spec) or self.aggregate_temp_cleanup.get(value)
        if entry is None or not self.temp_cleanup or entry not in self.temp_cleanup[-1]:
            return False
        self.temp_cleanup[-1].remove(entry)
        self.aggregate_temp_cleanup.pop(value, None)
        return True

    def capture_lines(self, emit_body) -> list[str]:
        """把 emit_body 期间发射的 IR 行截下来（不落进函数体），给「登记到语句尾再执行」的清理用。
        alloca 挂在 entry 块、临时量编号全局递增，所以这些行放到后面执行照样合法。"""
        saved, self.lines = self.lines, []
        try:
            emit_body()
            return self.lines
        finally:
            self.lines = saved

    def alloca(self, llvm_type: str, name: str | None = None) -> str:
        """申请一个栈槽，实际的 alloca 行统一挂到函数 entry 块。

        LLVM 里非 entry 块的 alloca 是动态栈分配，函数返回前不回收。F-string 的
        builder、INPUT 的 4KB 缓冲、块内 DIM 一旦落进循环体，每轮迭代都新占一块，
        长循环直接把栈吃穿（同一段程序 C 后端用固定局部变量则完全正常）。挂到
        entry 就是固定帧槽，重复进入不累积，顺带让 mem2reg/SROA 还有机会介入。
        """
        ptr = name or self.next_temp()
        self.entry_allocas.append(f"  {ptr} = alloca {llvm_type}")
        return ptr

    def push_scope(self) -> None:
        self.scope_resources.append([])

    def register_owned(self, slot: VarSlot) -> None:
        if self.scope_resources:
            self.scope_resources[-1].append(slot)

    def is_owned_var(self, name: str) -> bool:
        """变量（或其字段路径的根）是否登记在本帧某层作用域里。全局、AS REF 形参、值传 SYMBOL 形参、
        f= 借来的变量都不在表里——它们的资源不归本帧，RETURN 只能拷贝不能搬走。"""
        key = name.split(".", 1)[0].lower()
        return any(slot.name.lower() == key for resources in self.scope_resources for slot in resources)

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

    def has_active_resources(self) -> bool:
        return any(resources for resources in self.scope_resources)

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

    def error_message_ptr(self, error_ptr: str) -> str:
        field = self.next_temp()
        msg = self.next_temp()
        self.emit(f"  {field} = getelementptr inbounds %SaError, ptr {error_ptr}, i64 0, i32 2")
        self.emit(f"  {msg} = load ptr, ptr {field}")
        return msg

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
