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
