from __future__ import annotations

from pydantic import BaseModel, Field

from . import ast


class ModuleExports(BaseModel):
    module: str
    header_name: str
    entities: dict[str, ast.EntityDef] = Field(default_factory=dict)
    consts: dict[str, ast.Declaration] = Field(default_factory=dict)
    subs: dict[str, ast.Subroutine] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}


class ModuleUnit(BaseModel):
    module: str
    source_path: str
    c_path: str | None = None
    h_path: str
    lib_path: str | None = None
    dll_path: str | None = None
    target: str | None = None
    link_libs: list[str] = Field(default_factory=list)
    runtime_features: list[str] = Field(default_factory=list)
    exports: ModuleExports

    model_config = {"arbitrary_types_allowed": True}
