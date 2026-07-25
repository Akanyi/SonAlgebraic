from __future__ import annotations

from ..core import ast
from .c_templates import render_header
from ..core.module_model import ModuleExports
from ..core.names import header_guard, module_header_name, module_symbol_prefix
from ..analysis.typesys import c_type


def generate_header(exports: ModuleExports, dynamic: bool = False) -> str:
    entity = entity_lines(exports, dynamic)
    consts = const_lines(exports, dynamic)
    subs = sub_lines(exports, dynamic)
    lines = []
    if dynamic:
        lines.append(dynamic_api_macro(exports.module))
    return render_header(
        guard=header_guard(exports.header_name),
        entity_lines=lines + entity,
        const_lines=consts,
        sub_lines=subs,
    )


def dynamic_api_macro(module: str) -> str:
    build_macro = f"SA_BUILD_{module_symbol_prefix(module).upper().replace('-', '_')}"
    return (
        "#ifdef _WIN32\n"
        f"#ifdef {build_macro}\n"
        "#define SA_API __declspec(dllexport)\n"
        "#else\n"
        "#define SA_API __declspec(dllimport)\n"
        "#endif\n"
        "#elif defined(__GNUC__) && __GNUC__ >= 4\n"
        "#define SA_API __attribute__((visibility(\"default\")))\n"
        "#else\n"
        "#define SA_API\n"
        "#endif\n"
    )


def entity_lines(exports: ModuleExports, dynamic: bool = False) -> list[str]:
    lines: list[str] = []
    prefix = module_symbol_prefix(exports.module)
    for entity in exports.entities.values():
        lines.append(f"typedef struct {{")
        for field in entity.fields:
            # 与主 codegen 一致：导出实体的定长数组字段必须保留 [N] 维度
            suffix = f"[{field.type_spec.array_size}]" if field.type_spec.array_size is not None else ""
            lines.append(f"    {external_c_type(field.type_spec)} {field.name}{suffix};")
        lines.append(f"}} {prefix}_entity_{entity.name.lower()};")
        lines.append("")
    return lines


def const_lines(exports: ModuleExports, dynamic: bool = False) -> list[str]:
    prefix = module_symbol_prefix(exports.module)
    api = "SA_API " if dynamic else ""
    return [f"extern {api}{external_c_type(decl.type_spec)} {prefix}_const_{decl.name.lower()};" for decl in exports.consts.values()]


def sub_lines(exports: ModuleExports, dynamic: bool = False) -> list[str]:
    prefix = module_symbol_prefix(exports.module)
    api = "SA_API " if dynamic else ""
    lines = [f"{api}void {prefix}_init(void);", f"{api}void {prefix}_free(void);"]
    lines.extend(f"{api}{external_c_type(sub.return_type)} {prefix}_sub_{sub.name.lower()}({params_signature(sub)});" for sub in exports.subs.values())
    return lines


def params_signature(sub: ast.Subroutine) -> str:
    if not sub.params:
        return "void"
    parts: list[str] = []
    for param in sub.params:
        ctype = external_c_type(param.type_spec)
        pointer = "*" if param.by_ref else ""
        parts.append(f"{ctype}{pointer} sa_{param.name.lower()}")
    return ", ".join(parts)


def external_c_type(type_spec: ast.TypeSpec) -> str:
    if type_spec.name == "ENTITY" and type_spec.subtype and "." in type_spec.subtype:
        alias, entity = type_spec.subtype.split(".", 1)
        return f"sa_mod_{alias.lower()}_entity_{entity.lower()}"
    return c_type(type_spec)


def header_name_for_module(module: str) -> str:
    return module_header_name(module)
