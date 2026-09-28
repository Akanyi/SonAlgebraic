from __future__ import annotations

from dataclasses import fields, is_dataclass

from typing import TYPE_CHECKING

from ..core import ast
from ..core.errors import SonCompileError
from ..core.names import entity_c_name, split_module_member

if TYPE_CHECKING:
    from ..core.module_model import ModuleExports
    from .semantics import Symbol


VOID = ast.TypeSpec("VOID")
STRING = ast.TypeSpec("STRING")
ERROR = ast.TypeSpec("ERROR")
SYMBOL = ast.TypeSpec("SYMBOL")
LONG = ast.TypeSpec("NUM", "LONG")
DOUBLE = ast.TypeSpec("NUM", "DOUBLE")
FLOAT = ast.TypeSpec("NUM", "FLOAT")
BOOL = ast.TypeSpec("BOOL")
FILE_HANDLE = ast.TypeSpec("HANDLE", "FILE")
BUFFER_HANDLE = ast.TypeSpec("HANDLE", "BUFFER")
NET_STREAM_HANDLE = ast.TypeSpec("HANDLE", "NET_STREAM")
TCP_LISTENER_HANDLE = ast.TypeSpec("HANDLE", "TCP_LISTENER")
UDP_SOCKET_HANDLE = ast.TypeSpec("HANDLE", "UDP_SOCKET")
LIST_HANDLE = ast.TypeSpec("HANDLE", "LIST")
STR_LIST_HANDLE = ast.TypeSpec("HANDLE", "STR_LIST")
MAP_HANDLE = ast.TypeSpec("HANDLE", "MAP")
STR_MAP_HANDLE = ast.TypeSpec("HANDLE", "STR_MAP")
GUI_WINDOW_HANDLE = ast.TypeSpec("HANDLE", "WINDOW")
GUI_WIDGET_HANDLE = ast.TypeSpec("HANDLE", "WIDGET")
# NULL 字面量的占位类型，可赋给任意指针/CPTR
NULLT = ast.TypeSpec("NULLT")

BUILTIN_MODULES = {"SYS.MATH", "SYS.IO", "SYS.STRING", "SYS.NET", "SYS.FILE", "SYS.DESKTOP", "SYS.BINARY", "SYS.LIST", "SYS.MAP", "SYS.GUI", "SYS.LINT"}
RUNTIME_FEATURE_MODULES = {
    "SYS.NET": "net",
    "SYS.FILE": "file",
    "SYS.DESKTOP": "desktop",
    "SYS.BINARY": "binary",
    "SYS.LIST": "list",
    "SYS.MAP": "map",
    "SYS.GUI": "gui",
}


def runtime_features_for_uses(uses: dict[str, str]) -> set[str]:
    features = {feature for module in uses.values() if (feature := RUNTIME_FEATURE_MODULES.get(module)) is not None}
    # MAP.KEYS() 产出 STR_LIST 句柄，map runtime 直接调用 list runtime，必须连带启用
    if "map" in features:
        features.add("list")
    return features


def runtime_features_for_program(program: ast.Program, uses: dict[str, str]) -> set[str]:
    features = runtime_features_for_uses(uses)
    net_aliases = {alias for alias, module in uses.items() if module == "SYS.NET"}

    def visit(value: object) -> None:
        if isinstance(value, ast.CallExpr):
            split = split_module_member(value.name)
            if split and split[0] in net_aliases:
                member = split[1].upper()
                if member == "TLS_CONNECT":
                    features.add("tls")
                if member in NET_ASYNC_MEMBERS:
                    # 异步 I/O 也要协程运行时，且必伴 net——纯 net async 程序可能一个
                    # 用户 ASYNC SUB 都没有，光靠下面的 is_async 检查会漏掉。
                    features.add("async")
                url_index = {
                    "GET": 0,
                    "STATUS": 0,
                    "POST": 0,
                    "REQUEST": 1,
                    "REQUEST_STATUS": 1,
                    "REQUEST_TIMEOUT": 1,
                    "REQUEST_STATUS_TIMEOUT": 1,
                }.get(member)
                if url_index is not None and url_index < len(value.args):
                    url = value.args[url_index]
                    if not isinstance(url, ast.StringLiteral) or url.value.lower().startswith("https://"):
                        features.add("tls")
        if is_dataclass(value):
            for item in fields(value):
                visit(getattr(value, item.name))
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(program)
    # 语言内建：源码里只要出现 ASYNC SUB 就自动启用协程运行时（不经 USE），按 AST
    # 事实触发才是「语言内建」——不像 SYS.NET 那样挂在 RUNTIME_FEATURE_MODULES 上。
    if any(sub.is_async for sub in program.subs):
        features.add("async")
    return features


def is_numeric(type_spec: ast.TypeSpec) -> bool:
    return type_spec.name == "NUM"


def is_bool(type_spec: ast.TypeSpec) -> bool:
    return type_spec.name == "BOOL"


def is_null(type_spec: ast.TypeSpec) -> bool:
    return type_spec.name == "NULLT"


def classify_number_literal(value: str) -> ast.TypeSpec:
    """根据字面量形态判定数值子类型：十六进制/纯整数 -> LONG；含小数点或科学计数 -> DOUBLE。"""
    lowered = value.lower()
    if lowered.startswith("0x"):
        return LONG
    if "." in lowered or "e" in lowered:
        return DOUBLE
    return LONG


def is_string(type_spec: ast.TypeSpec) -> bool:
    return type_spec.name == "STRING"


def is_error(type_spec: ast.TypeSpec) -> bool:
    return type_spec.name == "ERROR"


def is_symbol(type_spec: ast.TypeSpec) -> bool:
    return type_spec.name == "SYMBOL"


def is_cptr(type_spec: ast.TypeSpec) -> bool:
    return type_spec.name == "CPTR"


def is_ptr(type_spec: ast.TypeSpec) -> bool:
    return type_spec.name == "PTR"


def is_handle(type_spec: ast.TypeSpec) -> bool:
    return type_spec.name == "HANDLE"


def is_promise(type_spec: ast.TypeSpec) -> bool:
    return type_spec.name == "PROMISE"


def is_sub_type(type_spec: ast.TypeSpec) -> bool:
    """callable 实体（NEW SUB 生成）：托管、引用计数，离开作用域释放。"""
    return type_spec.name == "SUB"


def is_sub_ptr(type_spec: ast.TypeSpec) -> bool:
    """函数引用 PTR TO SUB(...)：只读值，不拥有函数代码。"""
    return type_spec.name == "PTR" and type_spec.inner is not None and type_spec.inner.name == "SUB"


def is_callable_value(type_spec: ast.TypeSpec) -> bool:
    return is_sub_type(type_spec) or is_sub_ptr(type_spec)


def sub_signature(type_spec: ast.TypeSpec) -> ast.TypeSpec:
    """函数引用和 callable 实体共用同一种签名表示（SUB spec），调用检查只认签名。"""
    if is_sub_ptr(type_spec):
        assert type_spec.inner is not None
        return type_spec.inner
    return type_spec


def sub_type_for(params: list[ast.Param] | tuple[ast.Param, ...], return_type: ast.TypeSpec) -> ast.TypeSpec:
    return ast.TypeSpec("SUB", inner=return_type, params=tuple(params))


def sub_ref_type(
    name: str,
    subs: dict[str, ast.Subroutine],
    external_modules: dict[str, ModuleExports],
    c_funcs: dict[str, ast.CFunctionDecl],
    line_no: int,
) -> ast.TypeSpec:
    """@name() 的类型：PTR TO SUB(被引用函数的签名)。查找顺序与普通调用一致。"""
    sub = subs.get(name.lower())
    if sub is None:
        split = split_module_member(name)
        module = external_modules.get(split[0]) if split is not None else None
        if module is not None and split is not None:
            sub = module.subs.get(split[1].lower())
    if sub is not None:
        if sub.is_async:
            raise SonCompileError(f"不能对 ASYNC SUB 取函数引用: {name}（协程入口返回 PROMISE，签名与普通 SUB 不同）", line_no)
        return ast.TypeSpec("PTR", inner=sub_type_for(sub.params, sub.return_type))
    c_func = resolve_c_func(name, c_funcs)
    if c_func is not None:
        return ast.TypeSpec("PTR", inner=sub_type_for(c_func.params, c_func.return_type))
    raise SonCompileError(f"@{name}() 找不到可取引用的 SUB 或 C 函数（内置模块函数不能取引用）", line_no)


def callable_symbol_type(
    name: str,
    symbols: dict[str, Symbol],
    entities: dict[str, ast.EntityDef],
    line_no: int,
) -> ast.TypeSpec | None:
    """name 是 callable 实体 / 函数引用变量（可带 ENTITY 字段路径）时返回其类型，否则 None。

    解析失败也返回 None 而不是抛错：调用点在查完 SUB / C 函数之后才轮到这里，
    查不到应该落回调用点原有的「未知 SUB」诊断，而不是冒出一条字段错误把人带偏。
    """
    if name.split(".")[0].lower() not in symbols and name.lower() not in symbols:
        return None
    try:
        type_spec = resolve_path_type(name, symbols, entities, line_no)
    except SonCompileError:
        return None
    return type_spec if is_callable_value(type_spec) and type_spec.array_size is None else None


def same_handle_kind(left: ast.TypeSpec, right: ast.TypeSpec) -> bool:
    return is_handle(left) and is_handle(right) and (left.subtype or "").lower() == (right.subtype or "").lower()


def same_type_spec(left: ast.TypeSpec, right: ast.TypeSpec) -> bool:
    """两个类型是否逐层完全一致：NUM 连子类型、PTR 连内层、数组连长度一起比。

    给 REF 传参这类「共用同一块内存」的场景用；普通赋值仍走 require_assignable 的宽松规则。
    """
    if left.name != right.name or left.array_size != right.array_size:
        return False
    if (left.subtype or "").lower() != (right.subtype or "").lower():
        return False
    if (left.inner is None) != (right.inner is None):
        return False
    if left.inner is not None and right.inner is not None and not same_type_spec(left.inner, right.inner):
        return False
    # SUB 签名：参数名只是文档，不参与比较；传参方式和类型必须逐个一致，
    # 否则经函数引用调用时按错的 ABI 传参。
    left_params = left.params or ()
    right_params = right.params or ()
    if len(left_params) != len(right_params):
        return False
    return all(
        lp.by_ref == rp.by_ref and same_type_spec(lp.type_spec, rp.type_spec)
        for lp, rp in zip(left_params, right_params)
    )


def describe_type(type_spec: ast.TypeSpec) -> str:
    """把 TypeSpec 还原成接近源码写法的形式，让诊断信息能直接照抄改。"""
    if type_spec.name == "SUB":
        params = ", ".join(
            f"{param.name} AS {describe_type(param.type_spec)}{' AS REF' if param.by_ref else ''}"
            for param in type_spec.params or ()
        )
        ret = type_spec.inner or VOID
        text = f"SUB({params}) AS {describe_type(ret)}"
        return f"{text}[{type_spec.array_size}]" if type_spec.array_size is not None else text
    text = type_spec.name
    if type_spec.inner is not None:
        text = f"{text} TO {describe_type(type_spec.inner)}"
    elif type_spec.subtype:
        text = f"{text} AS {type_spec.subtype}"
    if type_spec.array_size is not None:
        text = f"{text}[{type_spec.array_size}]"
    return text


def type_of(
    expr: ast.Expr,
    symbols: dict[str, Symbol],
    subs: dict[str, ast.Subroutine] | None = None,
    entities: dict[str, ast.EntityDef] | None = None,
    uses: dict[str, str] | None = None,
    external_modules: dict[str, ModuleExports] | None = None,
    c_funcs: dict[str, ast.CFunctionDecl] | None = None,
) -> ast.TypeSpec:
    subs = subs or {}
    entities = entities or {}
    uses = uses or {}
    external_modules = external_modules or {}
    c_funcs = c_funcs or {}
    if isinstance(expr, ast.NumberLiteral):
        return classify_number_literal(expr.value)
    if isinstance(expr, ast.NullLiteral):
        return NULLT
    if isinstance(expr, ast.BoolLiteral):
        return BOOL
    if isinstance(expr, ast.StringLiteral | ast.FString):
        return STRING
    if isinstance(expr, ast.VarRef):
        builtin = resolve_builtin_const(expr.name, uses)
        if builtin is not None:
            return builtin[0]
        external_const = resolve_external_const_type(expr.name, external_modules)
        if external_const is not None:
            return external_const
        return resolve_path_type(expr.name, symbols, entities, expr.line_no)
    if isinstance(expr, ast.Unary):
        return BOOL if expr.op == "NOT" else type_of(expr.expr, symbols, subs, entities, uses, external_modules, c_funcs)
    if isinstance(expr, ast.Deref):
        ptr_type = type_of(expr.expr, symbols, subs, entities, uses, external_modules, c_funcs)
        if is_sub_ptr(ptr_type):
            raise SonCompileError("^ 不能用于函数引用；要调用它直接写 name(args)", expr.line_no)
        if is_ptr(ptr_type) and ptr_type.inner is not None:
            return ptr_type.inner
        if is_cptr(ptr_type):
            return ast.TypeSpec("NUM", "LONG")
        raise SonCompileError("^ 只能用于指针类型", expr.line_no)
    if isinstance(expr, ast.AddressOf):
        if not isinstance(expr.expr, ast.VarRef):
            raise SonCompileError("@ 只能用于变量", expr.line_no)
        inner = resolve_path_type(expr.expr.name, symbols, entities, expr.line_no)
        # PTR TO SUB 已经被「函数引用」占用：让 @callable 也得到这个类型，C 层的
        # SaCallable** 就会被当成 SaSubFn 调用。
        if is_sub_type(inner):
            raise SonCompileError(f"不能对 callable 实体取址: {expr.expr.name}（要传递它请直接赋值或按值传参）", expr.line_no)
        return ast.TypeSpec("PTR", inner=inner)
    if isinstance(expr, ast.SubRef):
        return sub_ref_type(expr.name, subs, external_modules, c_funcs, expr.line_no)
    if isinstance(expr, ast.Cast):
        return expr.type_spec
    if isinstance(expr, ast.Index):
        base_type = type_of(expr.base, symbols, subs, entities, uses, external_modules, c_funcs)
        if base_type.array_size is None:
            raise SonCompileError("下标访问只能用于数组", expr.line_no)
        # 元素类型 = 去掉 array_size 的同一类型
        return ast.TypeSpec(base_type.name, base_type.subtype, base_type.inner)
    if isinstance(expr, ast.Binary):
        if expr.op in {"=", "==", "!=", "<>", "<", "<=", ">", ">=", "AND", "OR"}:
            return BOOL
        if expr.op in {"BAND", "BOR", "BXOR", "SHL", "SHR"}:
            return LONG
        left = type_of(expr.left, symbols, subs, entities, uses, external_modules, c_funcs)
        right = type_of(expr.right, symbols, subs, entities, uses, external_modules, c_funcs)
        if is_symbol(left) or is_symbol(right):
            return SYMBOL
        if expr.op == "**":
            return DOUBLE
        if is_ptr(left) and not is_sub_ptr(left) and is_numeric(right) and expr.op in {"+", "-"}:
            return left
        return wider_numeric(left, right, expr.line_no)
    if isinstance(expr, ast.AwaitExpr | ast.SyncExpr):
        # AWAIT / SYNC 剥掉一层 PROMISE，取出异步实例的结果类型。
        operand_type = type_of(expr.operand, symbols, subs, entities, uses, external_modules, c_funcs)
        if not is_promise(operand_type) or operand_type.inner is None:
            raise SonCompileError("AWAIT / SYNC 只能作用于 PROMISE 或对 ASYNC SUB 的调用", expr.line_no)
        return operand_type.inner
    if isinstance(expr, ast.CallExpr):
        name = expr.name.upper()
        if name == "NUMBER":
            return DOUBLE
        if name == "STRING":
            return STRING
        if name in {"DERIV", "SIMPLIFY", "SUBST"}:
            return SYMBOL
        if name == "EVAL":
            return DOUBLE
        if is_math_function(expr.name, "POW", uses):
            if len(expr.args) == 2:
                left = type_of(expr.args[0], symbols, subs, entities, uses, external_modules, c_funcs)
                right = type_of(expr.args[1], symbols, subs, entities, uses, external_modules, c_funcs)
                if is_symbol(left) or is_symbol(right):
                    return SYMBOL
            return DOUBLE
        string_fn = resolve_string_function(expr.name, uses)
        if string_fn is not None:
            return string_fn[1]
        net_fn = resolve_net_function(expr.name, uses)
        if net_fn is not None:
            return net_fn[1]
        file_fn = resolve_file_function(expr.name, uses)
        if file_fn is not None:
            return file_fn[1]
        desktop_fn = resolve_desktop_function(expr.name, uses)
        if desktop_fn is not None:
            return desktop_fn[1]
        binary_fn = resolve_binary_function(expr.name, uses)
        if binary_fn is not None:
            return binary_fn[1]
        list_fn = resolve_list_function(expr.name, uses)
        if list_fn is not None:
            return list_fn[1]
        map_fn = resolve_map_function(expr.name, uses)
        if map_fn is not None:
            return map_fn[1]
        gui_fn = resolve_gui_function(expr.name, uses)
        if gui_fn is not None:
            return gui_fn[1]
        c_func = resolve_c_func(expr.name, c_funcs)
        if c_func is not None:
            return c_func.return_type
        external_sub = resolve_external_sub_type(expr.name, external_modules)
        if external_sub is not None:
            return external_sub
        sub = subs.get(expr.name.lower())
        if sub is not None:
            # ASYNC SUB 的调用产出 PROMISE OF <返回类型>，而非直接的返回值——
            # 取现要靠 AWAIT / SYNC 剥掉这层 PROMISE。
            if sub.is_async:
                return ast.TypeSpec("PROMISE", inner=sub.return_type)
            return sub.return_type
        callable_type = callable_symbol_type(expr.name, symbols, entities, expr.line_no)
        if callable_type is not None:
            return sub_signature(callable_type).inner or VOID
    raise SonCompileError("无法推断表达式类型", expr.line_no)


def wider_numeric(left: ast.TypeSpec, right: ast.TypeSpec, line_no: int) -> ast.TypeSpec:
    if not is_numeric(left) or not is_numeric(right):
        raise SonCompileError("当前版本只支持数值之间做算术运算", line_no)
    ranks = {"LONG": 0, "FLOAT": 1, "DOUBLE": 2}
    subtype = left.subtype if ranks[left.subtype or "LONG"] >= ranks[right.subtype or "LONG"] else right.subtype
    return ast.TypeSpec("NUM", subtype)


def c_type(type_spec: ast.TypeSpec) -> str:
    if type_spec.name == "VOID":
        return "void"
    if type_spec.name == "BOOL":
        return "int"
    if type_spec.name == "STRING":
        return "char*"
    if type_spec.name == "ERROR":
        return "SaError"
    if type_spec.name == "SYMBOL":
        return "SaSymbol"
    if type_spec.name == "CPTR":
        return "void*"
    if type_spec.name == "HANDLE":
        return "SaHandle"
    if type_spec.name == "PROMISE":
        # PROMISE 底层就是协程实例的 SaHandle（uint64 槽位句柄）
        return "SaHandle"
    if type_spec.name == "NUM" and type_spec.subtype == "LONG":
        return "long long"
    if type_spec.name == "NUM" and type_spec.subtype == "FLOAT":
        return "float"
    if type_spec.name == "NUM" and type_spec.subtype == "DOUBLE":
        return "double"
    if type_spec.name == "ENTITY":
        return f"SaEntity_{entity_c_name(type_spec.subtype or '')}"
    if type_spec.name == "SUB":
        return "SaCallable*"
    if type_spec.name == "PTR":
        if type_spec.inner is None:
            raise SonCompileError("PTR 类型缺少内部类型", 0)
        if type_spec.inner.name == "SUB":
            # C 的函数指针声明是 `ret (*name)(args)`，名字夹在类型中间，套不进生成器里
            # 到处在用的 `{ctype} {name}` 模板。统一擦成 SaSubFn，调用点再按签名强转回去。
            return "SaSubFn"
        return c_type(type_spec.inner) + "*"
    raise SonCompileError(f"无法映射到 C 类型: {type_spec.name}")


def resolve_path_type(
    name: str,
    symbols: dict[str, Symbol],
    entities: dict[str, ast.EntityDef],
    line_no: int,
) -> ast.TypeSpec:
    # 完整点名优先（枚举成员）
    if name.lower() in symbols:
        return symbols[name.lower()].type_spec
    parts = name.split(".")
    key = parts[0].lower()
    if key not in symbols:
        raise SonCompileError(f"变量未声明: {parts[0]}", line_no)
    current_type = symbols[key].type_spec

    for field_name in parts[1:]:
        if current_type.name != "ENTITY":
            raise SonCompileError(f"{field_name} 不是 ENTITY 字段", line_no)
        entity = entities.get((current_type.subtype or "").lower())
        if entity is None:
            raise SonCompileError(f"未知 ENTITY: {current_type.subtype}", line_no)
        field = next((item for item in entity.fields if item.name.lower() == field_name.lower()), None)
        if field is None:
            raise SonCompileError(f"ENTITY {entity.name} 没有字段: {field_name}", line_no)
        current_type = field.type_spec
    return current_type


def resolve_external_sub_type(name: str, external_modules: dict[str, ModuleExports]) -> ast.TypeSpec | None:
    split = split_module_member(name)
    if split is None:
        return None
    alias, member = split
    module = external_modules.get(alias)
    if module is None:
        return None
    sub = module.subs.get(member.lower())
    if sub is None:
        return None
    # imported async sub 的调用同样产出 PROMISE OF <返回类型>（与本地 async 对齐），
    # 这样跨模块的 SYNC/AWAIT 语义检查（靠 is_promise 接住）和取现类型都自然成立——
    # 否则 imported async 会被当成普通 sub、直接返回内层类型，SYNC/AWAIT 反而报错。
    if sub.is_async:
        return ast.TypeSpec("PROMISE", inner=sub.return_type)
    return sub.return_type


def resolve_c_func(name: str, c_funcs: dict[str, ast.CFunctionDecl]) -> ast.CFunctionDecl | None:
    from ..core.names import split_module_member

    split = split_module_member(name)
    if split is None:
        return None
    alias, member = split
    return c_funcs.get(f"{alias.lower()}.{member.lower()}")


def resolve_external_const_type(name: str, external_modules: dict[str, ModuleExports]) -> ast.TypeSpec | None:
    split = split_module_member(name)
    if split is None:
        return None
    alias, member = split
    module = external_modules.get(alias)
    if module is None:
        return None
    const = module.consts.get(member.lower())
    return const.type_spec if const is not None else None


def is_math_const(name: str, uses: dict[str, str]) -> bool:
    split = split_module_member(name)
    return bool(split and uses.get(split[0]) == "SYS.MATH" and split[1].upper() == "PI")


# 内置模块常量表：模块 -> {常量名 -> (类型, C 字面量)}
BUILTIN_CONSTS: dict[str, dict[str, tuple[ast.TypeSpec, str]]] = {
    "SYS.MATH": {
        "PI": (DOUBLE, "3.14159265358979323846"),
        "E": (DOUBLE, "2.71828182845904523536"),
        "TAU": (DOUBLE, "6.28318530717958647692"),
        "EPSILON": (DOUBLE, "2.2204460492503131e-16"),
        "MAX_LONG": (LONG, "9223372036854775807LL"),
        "MIN_LONG": (LONG, "(-9223372036854775807LL - 1)"),
    },
    "SYS.STRING": {
        "NEWLINE": (STRING, '"\\n"'),
        "TAB": (STRING, '"\\t"'),
        "CR": (STRING, '"\\r"'),
        "EMPTY": (STRING, '""'),
    },
}


def resolve_builtin_const(name: str, uses: dict[str, str]) -> tuple[ast.TypeSpec, str] | None:
    """若 name 是经内置模块别名访问的常量，返回 (类型, C 字面量)。"""
    split = split_module_member(name)
    if split is None:
        return None
    alias, member = split
    module = uses.get(alias)
    if module is None:
        return None
    return BUILTIN_CONSTS.get(module, {}).get(member.upper())


def is_math_function(name: str, function_name: str, uses: dict[str, str]) -> bool:
    split = split_module_member(name)
    return bool(split and uses.get(split[0]) == "SYS.MATH" and split[1].upper() == function_name.upper())


# SYS.STRING 内置函数签名：函数名 -> (参数类型列表, 返回类型)
STRING_FUNCTIONS: dict[str, tuple[list[ast.TypeSpec], ast.TypeSpec]] = {
    "LENGTH": ([STRING], LONG),
    "CONCAT": ([STRING, STRING], STRING),
    "SLICE": ([STRING, LONG, LONG], STRING),
    "FIND": ([STRING, STRING], LONG),
    "UPPER": ([STRING], STRING),
    "LOWER": ([STRING], STRING),
    "REPLACE": ([STRING, STRING, STRING], STRING),
}

NET_FUNCTIONS: dict[str, tuple[list[ast.TypeSpec], ast.TypeSpec]] = {
    "GET": ([STRING], STRING),
    "STATUS": ([STRING], LONG),
    "POST": ([STRING, STRING, STRING], STRING),
    "REQUEST": ([STRING, STRING, STRING, STRING], STRING),
    "REQUEST_STATUS": ([STRING, STRING, STRING, STRING], LONG),
    "REQUEST_TIMEOUT": ([STRING, STRING, STRING, STRING, LONG], STRING),
    "REQUEST_STATUS_TIMEOUT": ([STRING, STRING, STRING, STRING, LONG], LONG),
    "LAST_HEADERS": ([], STRING),
    "LAST_ERROR": ([], STRING),
    "LAST_CODE": ([], LONG),
    "LAST_PEER_HOST": ([], STRING),
    "LAST_PEER_PORT": ([], LONG),
    "URLENCODE": ([STRING], STRING),
    "DNS": ([STRING], STRING),
    "TCP_CONNECT": ([STRING, LONG, LONG], NET_STREAM_HANDLE),
    "TLS_CONNECT": ([STRING, LONG, LONG], NET_STREAM_HANDLE),
    "TCP_LISTEN": ([STRING, LONG, LONG], TCP_LISTENER_HANDLE),
    "TCP_ACCEPT": ([TCP_LISTENER_HANDLE, LONG], NET_STREAM_HANDLE),
    "TCP_LISTENER_CLOSE": ([TCP_LISTENER_HANDLE], BOOL),
    "STREAM_SEND": ([NET_STREAM_HANDLE, STRING], LONG),
    "STREAM_RECV": ([NET_STREAM_HANDLE, LONG], STRING),
    "STREAM_SEND_BUFFER": ([NET_STREAM_HANDLE, BUFFER_HANDLE, LONG, LONG], LONG),
    "STREAM_RECV_BUFFER": ([NET_STREAM_HANDLE, LONG], BUFFER_HANDLE),
    "STREAM_CLOSE": ([NET_STREAM_HANDLE], BOOL),
    "UDP_OPEN": ([], UDP_SOCKET_HANDLE),
    "UDP_BIND": ([UDP_SOCKET_HANDLE, STRING, LONG], BOOL),
    "UDP_CONNECT": ([UDP_SOCKET_HANDLE, STRING, LONG], BOOL),
    "UDP_SEND": ([UDP_SOCKET_HANDLE, STRING], LONG),
    "UDP_SEND_TO": ([UDP_SOCKET_HANDLE, STRING, LONG, STRING], LONG),
    "UDP_RECV": ([UDP_SOCKET_HANDLE, LONG], STRING),
    "UDP_SEND_BUFFER": ([UDP_SOCKET_HANDLE, BUFFER_HANDLE, LONG, LONG], LONG),
    "UDP_SEND_BUFFER_TO": ([UDP_SOCKET_HANDLE, STRING, LONG, BUFFER_HANDLE, LONG, LONG], LONG),
    "UDP_RECV_BUFFER": ([UDP_SOCKET_HANDLE, LONG], BUFFER_HANDLE),
    "UDP_CLOSE": ([UDP_SOCKET_HANDLE], BOOL),
    "LOCAL_PORT": ([TCP_LISTENER_HANDLE], LONG),
    "UDP_LOCAL_PORT": ([UDP_SOCKET_HANDLE], LONG),
    # 异步 I/O 三件套：返回 PROMISE OF X，由 AWAIT/SYNC 剥层取现。inner 就是对应
    # 同步版的返回类型——ACCEPT_ASYNC 产 PROMISE OF NET_STREAM，RECV_ASYNC 产
    # PROMISE OF STRING，SEND_ASYNC 产 PROMISE OF NUM。协程运行时靠这些触发（见
    # runtime_features_for_program），用户不必写 USE。
    "ACCEPT_ASYNC": ([TCP_LISTENER_HANDLE], ast.TypeSpec("PROMISE", inner=NET_STREAM_HANDLE)),
    "RECV_ASYNC": ([NET_STREAM_HANDLE, LONG], ast.TypeSpec("PROMISE", inner=STRING)),
    "SEND_ASYNC": ([NET_STREAM_HANDLE, STRING], ast.TypeSpec("PROMISE", inner=LONG)),
    # 异步客户端：先发起非阻塞 connect() 再 poll writable，产 PROMISE OF NET_STREAM。
    "CONNECT_ASYNC": ([STRING, LONG], ast.TypeSpec("PROMISE", inner=NET_STREAM_HANDLE)),
}

# SYS.NET 里返回 PROMISE 的成员：程序一旦用到就要开协程运行时，哪怕没写 ASYNC SUB
# （纯转发 promise 的场景）。与 is_async sub 一起构成 async feature 的两个触发点。
NET_ASYNC_MEMBERS = {"ACCEPT_ASYNC", "RECV_ASYNC", "SEND_ASYNC", "CONNECT_ASYNC"}

FILE_FUNCTIONS: dict[str, tuple[list[ast.TypeSpec], ast.TypeSpec]] = {
    "OPEN": ([STRING, STRING], FILE_HANDLE),
    "READ": ([FILE_HANDLE, LONG], STRING),
    "WRITE": ([FILE_HANDLE, STRING], LONG),
    "SEEK": ([FILE_HANDLE, LONG, STRING], BOOL),
    "TELL": ([FILE_HANDLE], LONG),
    "SIZE": ([FILE_HANDLE], LONG),
    "CLOSE": ([FILE_HANDLE], BOOL),
    "READ_TEXT": ([STRING], STRING),
    "WRITE_TEXT": ([STRING, STRING], BOOL),
    "APPEND_TEXT": ([STRING, STRING], BOOL),
    "EXISTS": ([STRING], BOOL),
    "IS_FILE": ([STRING], BOOL),
    "IS_DIR": ([STRING], BOOL),
    "DELETE": ([STRING], BOOL),
    "MKDIR": ([STRING], BOOL),
    "CWD": ([], STRING),
    "ABSOLUTE": ([STRING], STRING),
    "LAST_ERROR": ([], STRING),
}

DESKTOP_FUNCTIONS: dict[str, tuple[list[ast.TypeSpec], ast.TypeSpec]] = {
    "MESSAGE": ([STRING, STRING], BOOL),
    "OPEN": ([STRING], BOOL),
    "CLIPBOARD_GET": ([], STRING),
    "CLIPBOARD_SET": ([STRING], BOOL),
    "LAST_ERROR": ([], STRING),
}

BINARY_FUNCTIONS: dict[str, tuple[list[ast.TypeSpec], ast.TypeSpec]] = {
    "NEW": ([LONG], BUFFER_HANDLE),
    "CLOSE": ([BUFFER_HANDLE], BOOL),
    "LENGTH": ([BUFFER_HANDLE], LONG),
    "SLICE": ([BUFFER_HANDLE, LONG, LONG], BUFFER_HANDLE),
    "COPY": ([BUFFER_HANDLE, LONG, BUFFER_HANDLE, LONG, LONG], BOOL),
    "HEX_DECODE": ([STRING], BUFFER_HANDLE),
    "HEX_ENCODE": ([BUFFER_HANDLE], STRING),
    "PACK_U16_LE": ([BUFFER_HANDLE, LONG, LONG], BOOL),
    "PACK_U16_BE": ([BUFFER_HANDLE, LONG, LONG], BOOL),
    "PACK_U32_LE": ([BUFFER_HANDLE, LONG, LONG], BOOL),
    "PACK_U32_BE": ([BUFFER_HANDLE, LONG, LONG], BOOL),
    "PACK_U64_LE": ([BUFFER_HANDLE, LONG, LONG], BOOL),
    "PACK_U64_BE": ([BUFFER_HANDLE, LONG, LONG], BOOL),
    "UNPACK_U16_LE": ([BUFFER_HANDLE, LONG], LONG),
    "UNPACK_U16_BE": ([BUFFER_HANDLE, LONG], LONG),
    "UNPACK_U32_LE": ([BUFFER_HANDLE, LONG], LONG),
    "UNPACK_U32_BE": ([BUFFER_HANDLE, LONG], LONG),
    "UNPACK_U64_LE": ([BUFFER_HANDLE, LONG], LONG),
    "UNPACK_U64_BE": ([BUFFER_HANDLE, LONG], LONG),
    "CHECKSUM8": ([BUFFER_HANDLE, LONG, LONG], LONG),
    "LAST_ERROR": ([], STRING),
}


def resolve_string_function(name: str, uses: dict[str, str]) -> tuple[list[ast.TypeSpec], ast.TypeSpec] | None:
    """若 name 是经 SYS.STRING 别名调用的内置字符串函数，返回其 (参数类型, 返回类型)。"""
    split = split_module_member(name)
    if split is None:
        return None
    alias, member = split
    if uses.get(alias) != "SYS.STRING":
        return None
    return STRING_FUNCTIONS.get(member.upper())


def resolve_net_function(name: str, uses: dict[str, str]) -> tuple[list[ast.TypeSpec], ast.TypeSpec] | None:
    """若 name 是经 SYS.NET 别名调用的内置网络函数，返回其 (参数类型, 返回类型)。"""
    split = split_module_member(name)
    if split is None:
        return None
    alias, member = split
    if uses.get(alias) != "SYS.NET":
        return None
    return NET_FUNCTIONS.get(member.upper())


def resolve_file_function(name: str, uses: dict[str, str]) -> tuple[list[ast.TypeSpec], ast.TypeSpec] | None:
    split = split_module_member(name)
    if split is None:
        return None
    alias, member = split
    if uses.get(alias) != "SYS.FILE":
        return None
    return FILE_FUNCTIONS.get(member.upper())


def resolve_desktop_function(name: str, uses: dict[str, str]) -> tuple[list[ast.TypeSpec], ast.TypeSpec] | None:
    split = split_module_member(name)
    if split is None:
        return None
    alias, member = split
    if uses.get(alias) != "SYS.DESKTOP":
        return None
    return DESKTOP_FUNCTIONS.get(member.upper())


def resolve_binary_function(name: str, uses: dict[str, str]) -> tuple[list[ast.TypeSpec], ast.TypeSpec] | None:
    split = split_module_member(name)
    if split is None:
        return None
    alias, member = split
    if uses.get(alias) != "SYS.BINARY":
        return None
    return BINARY_FUNCTIONS.get(member.upper())


# SYS.LIST 动态列表：数值列表(LIST，元素按 DOUBLE 存储)和字符串列表(STR_LIST)分成
# 两个 HANDLE kind，把“字符串列表传给数值函数”这类错误留在编译期。
LIST_FUNCTIONS: dict[str, tuple[list[ast.TypeSpec], ast.TypeSpec]] = {
    "NEW": ([], LIST_HANDLE),
    "PUSH": ([LIST_HANDLE, DOUBLE], BOOL),
    "POP": ([LIST_HANDLE], DOUBLE),
    "GET": ([LIST_HANDLE, LONG], DOUBLE),
    "SET": ([LIST_HANDLE, LONG, DOUBLE], BOOL),
    "INSERT": ([LIST_HANDLE, LONG, DOUBLE], BOOL),
    "REMOVE": ([LIST_HANDLE, LONG], BOOL),
    "LENGTH": ([LIST_HANDLE], LONG),
    "CLEAR": ([LIST_HANDLE], BOOL),
    "CLOSE": ([LIST_HANDLE], BOOL),
    "NEW_STR": ([], STR_LIST_HANDLE),
    "PUSH_STR": ([STR_LIST_HANDLE, STRING], BOOL),
    "POP_STR": ([STR_LIST_HANDLE], STRING),
    "GET_STR": ([STR_LIST_HANDLE, LONG], STRING),
    "SET_STR": ([STR_LIST_HANDLE, LONG, STRING], BOOL),
    "INSERT_STR": ([STR_LIST_HANDLE, LONG, STRING], BOOL),
    "REMOVE_STR": ([STR_LIST_HANDLE, LONG], BOOL),
    "LENGTH_STR": ([STR_LIST_HANDLE], LONG),
    "CLEAR_STR": ([STR_LIST_HANDLE], BOOL),
    "CLOSE_STR": ([STR_LIST_HANDLE], BOOL),
    "JOIN_STR": ([STR_LIST_HANDLE, STRING], STRING),
    "LAST_ERROR": ([], STRING),
}


def resolve_list_function(name: str, uses: dict[str, str]) -> tuple[list[ast.TypeSpec], ast.TypeSpec] | None:
    split = split_module_member(name)
    if split is None:
        return None
    alias, member = split
    if uses.get(alias) != "SYS.LIST":
        return None
    return LIST_FUNCTIONS.get(member.upper())


# SYS.MAP 关联容器：STRING key，值分数值(MAP)和字符串(STR_MAP)两个 HANDLE kind。
# KEYS 返回 STR_LIST 句柄，与 SYS.LIST 打通遍历。
MAP_FUNCTIONS: dict[str, tuple[list[ast.TypeSpec], ast.TypeSpec]] = {
    "NEW": ([], MAP_HANDLE),
    "SET": ([MAP_HANDLE, STRING, DOUBLE], BOOL),
    "GET": ([MAP_HANDLE, STRING], DOUBLE),
    "HAS": ([MAP_HANDLE, STRING], BOOL),
    "REMOVE": ([MAP_HANDLE, STRING], BOOL),
    "LENGTH": ([MAP_HANDLE], LONG),
    "KEYS": ([MAP_HANDLE], STR_LIST_HANDLE),
    "CLEAR": ([MAP_HANDLE], BOOL),
    "CLOSE": ([MAP_HANDLE], BOOL),
    "NEW_STR": ([], STR_MAP_HANDLE),
    "SET_STR": ([STR_MAP_HANDLE, STRING, STRING], BOOL),
    "GET_STR": ([STR_MAP_HANDLE, STRING], STRING),
    "HAS_STR": ([STR_MAP_HANDLE, STRING], BOOL),
    "REMOVE_STR": ([STR_MAP_HANDLE, STRING], BOOL),
    "LENGTH_STR": ([STR_MAP_HANDLE], LONG),
    "KEYS_STR": ([STR_MAP_HANDLE], STR_LIST_HANDLE),
    "CLEAR_STR": ([STR_MAP_HANDLE], BOOL),
    "CLOSE_STR": ([STR_MAP_HANDLE], BOOL),
    "LAST_ERROR": ([], STRING),
}


def resolve_map_function(name: str, uses: dict[str, str]) -> tuple[list[ast.TypeSpec], ast.TypeSpec] | None:
    split = split_module_member(name)
    if split is None:
        return None
    alias, member = split
    if uses.get(alias) != "SYS.MAP":
        return None
    return MAP_FUNCTIONS.get(member.upper())


# 按钮点击回调的签名：SUB onClick(id AS NUM AS LONG)。id 就是 BUTTON 创建时给的 control id，
# 一个 SUB 能服务多个按钮。
GUI_CLICK_HANDLER = ast.TypeSpec("SUB", inner=VOID, params=(ast.Param("id", LONG, False, 0),))

# SYS.GUI 窗口模块，两种事件模型共存、共用同一个事件队列：
# - 轮询：BUTTON 创建时带用户自定义 control id，WAIT_EVENT 阻塞返回被点击的 id，
#   0 表示所有窗口已关闭；
# - 回调：ON_CLICK 给按钮挂一个 callable，RUN 跑事件循环并派发，直到窗口全关。
# Windows 走 Win32，Linux 可选 GTK3（SA_ENABLE_GUI_GTK）；别的平台全部失败并给 LAST_ERROR。
GUI_FUNCTIONS: dict[str, tuple[list[ast.TypeSpec], ast.TypeSpec]] = {
    "WINDOW": ([STRING, LONG, LONG], GUI_WINDOW_HANDLE),
    "BUTTON": ([GUI_WINDOW_HANDLE, LONG, STRING, LONG, LONG, LONG, LONG], GUI_WIDGET_HANDLE),
    "LABEL": ([GUI_WINDOW_HANDLE, STRING, LONG, LONG, LONG, LONG], GUI_WIDGET_HANDLE),
    "TEXTBOX": ([GUI_WINDOW_HANDLE, LONG, LONG, LONG, LONG], GUI_WIDGET_HANDLE),
    "SET_TEXT": ([GUI_WIDGET_HANDLE, STRING], BOOL),
    "GET_TEXT": ([GUI_WIDGET_HANDLE], STRING),
    "WAIT_EVENT": ([], LONG),
    "ON_CLICK": ([GUI_WIDGET_HANDLE, GUI_CLICK_HANDLER], BOOL),
    "RUN": ([], BOOL),
    "CLOSE": ([GUI_WINDOW_HANDLE], BOOL),
    "LAST_ERROR": ([], STRING),
}


def resolve_gui_function(name: str, uses: dict[str, str]) -> tuple[list[ast.TypeSpec], ast.TypeSpec] | None:
    split = split_module_member(name)
    if split is None:
        return None
    alias, member = split
    if uses.get(alias) != "SYS.GUI":
        return None
    return GUI_FUNCTIONS.get(member.upper())
