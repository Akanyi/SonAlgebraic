from __future__ import annotations

from ..core import ast
from ..core.module_model import ModuleExports
from ..core.names import module_header_name


def collect_exports(module: str, program: ast.Program) -> ModuleExports:
    exports = ModuleExports(module=module, header_name=module_header_name(module))

    for entity in program.entities:
        exports.entities[entity.name.lower()] = entity
    for decl in program.declarations:
        if not decl.mutable:
            exports.consts[decl.name.lower()] = decl
    for sub in program.subs:
        if sub.visibility == "PUBLIC":
            exports.subs[sub.name.lower()] = sub

    return exports
