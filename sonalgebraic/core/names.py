from __future__ import annotations

from pathlib import Path


def c_ident(name: str) -> str:
    ident = name.lower().replace(".", "_")
    # POSIX signal.h 把 sa_handler / sa_sigaction 定义成字段访问宏。
    # 普通标识符始终小写，冲突名改用大写尾部，既避开宏也不会撞上其他用户名字。
    if ident in {"handler", "sigaction"}:
        ident = ident.upper()
    return "sa_" + ident


def entity_c_name(name: str) -> str:
    return name.lower().replace(".", "_")


def module_c_name(module: str) -> str:
    return module.lower().replace(".", "_")


def module_symbol_prefix(module: str) -> str:
    return f"sa_mod_{module_c_name(module)}"


def split_module_member(name: str) -> tuple[str, str] | None:
    parts = name.split(".")
    if len(parts) != 2:
        return None
    return parts[0].lower(), parts[1]


def module_path_to_source(root: Path, module: str) -> Path:
    parts = module.lower().split(".")
    nested = root.joinpath(*parts).with_suffix(".sa")
    if nested.exists():
        return nested
    flat = root.joinpath("_".join(parts)).with_suffix(".sa")
    return flat


def module_path_to_slib(root: Path, module: str) -> Path:
    parts = module.lower().split(".")
    nested = root.joinpath(*parts).with_suffix(".slib")
    if nested.exists():
        return nested
    flat = root.joinpath("_".join(parts)).with_suffix(".slib")
    return flat


def module_header_name(module: str) -> str:
    return f"sa_user_{module_c_name(module)}.h"


def header_guard(name: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in name.upper())
    return f"SONALGEBRAIC_{safe}_H"
