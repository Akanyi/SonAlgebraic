from __future__ import annotations

from ...analysis.typesys import is_error, is_string, is_symbol
from ...core import ast
from ...core.errors import SonCompileError
from ...core.names import entity_c_name, module_symbol_prefix, split_module_member
from .base import NativeGenBase


class EntitiesMixin(NativeGenBase):
    """ENTITY 的类型声明、字段解析和 init/copy/free 生命周期。"""

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

    def entity_source_ptr(self, expr: ast.Expr, type_spec: ast.TypeSpec) -> str:
        if isinstance(expr, ast.VarRef | ast.Deref | ast.Index):
            return self.lvalue_ptr(expr)
        value = self.cast_value(self.expr(expr), type_spec)
        ptr = self.alloca(self.llvm_type(type_spec))
        self.emit(f"  store {self.llvm_type(type_spec)} {value.value}, ptr {ptr}")
        return ptr
