"""实体类型。"""
from __future__ import annotations

from ...analysis.typesys import is_error, is_string, is_symbol
from ...core import ast
from ...core.names import split_module_member
from .base import CGenBase


class EntitiesMixin(CGenBase):
    """ENTITY：typedef 发射，以及含托管资源（STRING / ERROR 字段）实体的 init / free / copy 展开。"""

    def generate_entities(self) -> str:
        chunks: list[str] = []
        for entity in self.checked.program.entities:
            chunks.append(self.source_comment(entity.line_no, 0))
            chunks.append("typedef struct {")
            for field in entity.fields:
                chunks.append(self.source_comment(field.line_no, 1))
                # 实体字段同样支持定长数组（如 DIM tensor[3]）；漏掉 array_size 会把数组
                # 字段生成成标量，导致 .field[i] 下标访问编译失败。
                suffix = f"[{field.type_spec.array_size}]" if field.type_spec.array_size is not None else ""
                chunks.append(f"    {self.c_type(field.type_spec)} {field.name}{suffix};")
            chunks.append(f"}} {self.entity_type_name(entity.name)};")
            chunks.append("")
        return "\n".join(chunks).rstrip()

    def type_has_managed_resources(self, type_spec: ast.TypeSpec, inside_entity: bool = False) -> bool:
        if is_string(type_spec) or is_symbol(type_spec) or is_error(type_spec):
            return not (inside_entity and is_symbol(type_spec))
        if type_spec.name != "ENTITY":
            return False
        entity = self.resolve_entity_def(type_spec)
        if entity is None:
            return False
        return any(self.type_has_managed_resources(field.type_spec, inside_entity=True) for field in entity.fields)

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

    def entity_init_lines(self, target: str, type_spec: ast.TypeSpec, indent: int) -> list[str]:
        pad = "    " * indent
        entity = self.resolve_entity_def(type_spec)
        if entity is None:
            return []
        lines: list[str] = []
        for field in entity.fields:
            field_target = f"{target}.{field.name}"
            if is_string(field.type_spec):
                lines.append(f"{pad}{field_target} = sa_strdup(\"\");")
            elif is_error(field.type_spec):
                lines.append(f"{pad}{field_target} = (SaError){{0, \"ERR_NONE\", NULL, 0, NULL}};")
            elif field.type_spec.name == "ENTITY":
                lines.extend(self.entity_init_lines(field_target, field.type_spec, indent))
        return lines

    def entity_free_lines(self, target: str, type_spec: ast.TypeSpec, indent: int) -> list[str]:
        pad = "    " * indent
        entity = self.resolve_entity_def(type_spec)
        if entity is None:
            return []
        lines: list[str] = []
        for field in reversed(entity.fields):
            field_target = f"{target}.{field.name}"
            if is_string(field.type_spec):
                lines.append(f"{pad}free({field_target});")
            elif is_error(field.type_spec):
                lines.append(f"{pad}sa_error_clear(&{field_target});")
            elif field.type_spec.name == "ENTITY":
                lines.extend(self.entity_free_lines(field_target, field.type_spec, indent))
        return lines

    def entity_copy_lines(self, target: str, source: str, type_spec: ast.TypeSpec, indent: int) -> list[str]:
        pad = "    " * indent
        entity = self.resolve_entity_def(type_spec)
        if entity is None:
            return [f"{pad}{target} = {source};"]
        lines: list[str] = []
        for field in entity.fields:
            field_target = f"{target}.{field.name}"
            field_source = f"{source}.{field.name}"
            if is_string(field.type_spec):
                lines.append(f"{pad}sa_set_string(&{field_target}, {field_source});")
            elif is_error(field.type_spec):
                lines.append(f"{pad}sa_set_error(&{field_target}, &{field_source});")
            elif field.type_spec.name == "ENTITY":
                lines.extend(self.entity_copy_lines(field_target, field_source, field.type_spec, indent))
            else:
                lines.append(f"{pad}{field_target} = {field_source};")
        return lines
