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


def value_ops_c_name(described: str) -> str:
    """托管容器元素操作表的符号名，输入是 `describe_type` 的输出。

拿规范描述当种子，而不是自己拼一套编码：`describe_type` 已经保证同类型同名、
异类型异名（它的输出能照抄回源码），所以压成标识符后天然不会撞名。
自己编一套就得再维护一份「类型 → 名字」的双射，还得保证它和诊断里显示的一致。
"""
    safe = "".join(ch if ch.isalnum() else "_" for ch in described.lower())
    return "sa_ops_" + "_".join(part for part in safe.split("_") if part)


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
