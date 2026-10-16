"""类型的资源特征不依赖某个变量当时是拥有、借用还是已经移走。"""
from __future__ import annotations

from dataclasses import dataclass, replace

from ..core import ast
from ..core.errors import SonCompileError
from ..core.module_model import ModuleExports
from ..core.names import split_module_member


_HANDLE_FEATURES = {
    'FILE': frozenset({'file'}),
    'BUFFER': frozenset({'binary'}),
    'NET_STREAM': frozenset({'net'}),
    'TCP_LISTENER': frozenset({'net'}),
    'UDP_SOCKET': frozenset({'net'}),
    'WINDOW': frozenset({'gui'}),
}

# 旧的 LIST/MAP kind 不接托管：它们的槽位里只有 double 和 char*，压根没有逐元素析构这回事。
# 要托管容器就写 LIST OF T / MAP OF T，那是另一套 kind、另一张槽位表。
_LEGACY_CONTAINER_KINDS = frozenset({'LIST', 'STR_LIST', 'MAP', 'STR_MAP'})

# runtime 里每个 kind 的 SA_DEFINE_HANDLE_DROP 前缀；析构器、指针 ABI 析构和 CLOSE 清槽都由它派生。
# 没有条目就是「注册了 feature 但还没有析构协议」，声明处直接拒绝，不能靠槽位表碰运气。
_HANDLE_DROPS = {
    'FILE': 'sa_file',
    'BUFFER': 'sa_binary',
    'NET_STREAM': 'sa_net_stream',
    'TCP_LISTENER': 'sa_net_tcp_listener',
    'UDP_SOCKET': 'sa_net_udp',
    'WINDOW': 'sa_gui_window',
}

# 托管容器在 runtime 里的名字前缀。LIST 是连续存储的 vec，MAP 是 STRING key 的 dict。
_CONTAINER_PREFIXES = {
    'LIST': 'sa_vec',
    'MAP': 'sa_dict',
}

# runtime 里已经摆好的元素操作表。这些类型的字节搬运就是全部语义：标量按值拷，裸句柄复制
# 别名本来就合法，嵌套容器的析构转交子容器自己的 drop（子容器构造时已把 ops 存在自己身上）。
# 其余类型（STRING/SYMBOL/ERROR/SUB/ENTITY/托管句柄）要按实际类型生成胶水函数，不在此表。
_PRESET_VALUE_OPS = {
    ('NUM', 'LONG'): 'sa_ops_long',
    ('NUM', 'DOUBLE'): 'sa_ops_double',
    ('NUM', 'FLOAT'): 'sa_ops_float',
    ('BOOL', None): 'sa_ops_bool',
    ('CPTR', None): 'sa_ops_pointer',
}

# 嵌套容器的元素操作表。和 _CONTAINER_PREFIXES 不同构，所以单独列一张而不是去拼前缀。
_CONTAINER_VALUE_OPS = {
    'LIST': 'sa_ops_vec',
    'MAP': 'sa_ops_dict',
}


def preset_value_ops(type_spec: ast.TypeSpec) -> str | None:
    """元素类型对应的现成 ops 表符号；None 表示要生成胶水。

    判定全部落在 TypeSpec 的字段上而不是借 typesys 的谓词：typesys 反向依赖本模块。
    """
    if type_spec.array_size is not None:
        # 定长数组当元素没有安全的字节语义：ops 只有一个 size，逐元素析构无从表达。
        return None
    if type_spec.name in _CONTAINER_VALUE_OPS and type_spec.managed:
        return _CONTAINER_VALUE_OPS[type_spec.name]
    if type_spec.name == 'HANDLE' and not type_spec.managed:
        # 托管句柄独占，复制别名就是双重关闭，必须走 copyable = 0 的生成表。
        return 'sa_ops_handle'
    if type_spec.name == 'PTR':
        return 'sa_ops_pointer'
    return _PRESET_VALUE_OPS.get((type_spec.name, type_spec.subtype))


def element_copyable(type_spec: ast.TypeSpec) -> bool:
    '元素能不能复制进出容器。\n\n必须和生成的 ops 表里那个 copyable 字段完全一致：两处对不上就会出现「编译期放行、\n运行时静默返回 0」——而 runtime 拒绝一次复制只是返回假，没有任何能看出因果的痕迹。\n独占的那两类（托管容器、托管句柄）复制一份句柄就是两个持有者，析构会关两遍。\n判定同样只看 TypeSpec 字段，理由见 preset_value_ops。\n'
    return not (type_spec.managed and type_spec.name in {*_CONTAINER_PREFIXES, 'HANDLE'})


@dataclass(frozen=True)
class ResourceTraits:
    has_resources: bool = False
    copyable: bool = True
    features: frozenset[str] = frozenset()
    explicit: bool = False

    @property
    def move_only(self) -> bool:
        return self.has_resources and not self.copyable


def handle_drop_prefix(type_spec: ast.TypeSpec) -> str:
    """托管资源在 runtime 里的名字前缀。析构三件套的函数名都从它派生。"""
    if type_spec.name in _CONTAINER_PREFIXES and type_spec.managed:
        return _CONTAINER_PREFIXES[type_spec.name]
    subtype = (type_spec.subtype or '').upper()
    if type_spec.name == 'HANDLE' and type_spec.managed and subtype in _LEGACY_CONTAINER_KINDS:
        raise SonCompileError(f'HANDLE AS {subtype} 不能托管；托管容器请写 LIST OF <元素类型> 或 MAP OF <元素类型>')
    prefix = _HANDLE_DROPS.get(subtype) if type_spec.name == 'HANDLE' and type_spec.managed else None
    if prefix is None:
        raise SonCompileError(f'托管 HANDLE 尚未接入析构器: {type_spec.subtype}')
    return prefix


def handle_drop_function(type_spec: ast.TypeSpec) -> str:
    return handle_drop_prefix(type_spec) + '_drop'


def handle_drop_value_function(type_spec: ast.TypeSpec) -> str:
    """指针 ABI 的析构：先清空槽位再关，签名正好是 SaValueOps.drop 要的那个。"""
    return handle_drop_prefix(type_spec) + '_drop_value'


def handle_close_owned_function(type_spec: ast.TypeSpec) -> str:
    return handle_drop_prefix(type_spec) + '_close_owned'


def container_new_function(type_spec: ast.TypeSpec) -> str:
    """`NEW LIST OF T` / `NEW MAP OF T` 落到 runtime 的构造函数。"""
    if type_spec.name not in _CONTAINER_PREFIXES or not type_spec.managed:
        raise SonCompileError(f'不是托管容器类型: {type_spec.name}')
    return _CONTAINER_PREFIXES[type_spec.name] + '_new'


def has_explicit_resources(type_spec: ast.TypeSpec, entities: dict[str, ast.EntityDef],
                           external_modules: dict[str, ModuleExports]) -> bool:
    return resource_traits(type_spec, entities, external_modules).explicit


def resource_traits(
    type_spec: ast.TypeSpec,
    entities: dict[str, ast.EntityDef],
    external_modules: dict[str, ModuleExports],
    *,
    line_no: int | None = None,
) -> ResourceTraits:
    def visit(kind: ast.TypeSpec, local_entities: dict[str, ast.EntityDef], seen: frozenset[int]) -> ResourceTraits:
        if kind.array_size is not None:
            return visit(replace(kind, array_size=None), local_entities, seen)
        if kind.name == 'HANDLE' and kind.managed:
            subtype = (kind.subtype or '').upper()
            if subtype in _LEGACY_CONTAINER_KINDS:
                raise SonCompileError(
                    f'HANDLE AS {subtype} 不能托管；托管容器请写 LIST OF <元素类型> 或 MAP OF <元素类型>', line_no)
            features = _HANDLE_FEATURES.get(subtype)
            if features is None:
                raise SonCompileError(f'不支持独立托管的 HANDLE kind: {kind.subtype}', line_no)
            return ResourceTraits(True, False, features, explicit=True)
        if kind.name in _CONTAINER_PREFIXES:
            if kind.inner is None:
                raise SonCompileError(f'{kind.name} 必须写成 `{kind.name} OF <元素类型>`', line_no)
            # 容器句柄本身永远只能移动。元素可不可复制是另一个维度——它决定容器开放哪些接口，
            # 由后端生成 ops 表时按元素类型自己算，不往上冒泡成容器的 copyable。
            element = visit(kind.inner, local_entities, seen)
            return ResourceTraits(True, False, element.features | {'container'}, explicit=True)
        if kind.name in {'STRING', 'SYMBOL', 'ERROR', 'SUB'}:
            return ResourceTraits(has_resources=True)
        if kind.name == 'PROMISE':
            result = visit(kind.inner, local_entities, seen) if kind.inner is not None else ResourceTraits()
            return ResourceTraits(True, False, result.features | {'async'}, explicit=result.explicit)
        # PTR 只指向布局，不拥有 pointee；函数签名也不拥有它的参数和返回类型。
        if kind.name != 'ENTITY':
            return ResourceTraits()
        name = kind.subtype or ''
        split = split_module_member(name)
        definitions = local_entities
        if split is not None:
            module = external_modules.get(split[0])
            definitions = module.entities if module is not None else {}
            name = split[1]
        entity = definitions.get(name.lower())
        if entity is None:
            raise SonCompileError(f'未知 ENTITY: {kind.subtype}', line_no)
        if id(entity) in seen:
            raise SonCompileError(f'ENTITY {entity.name} 存在递归拥有布局；间接引用请使用 PTR', line_no or entity.line_no)
        children = [visit(member.type_spec, definitions, seen | {id(entity)}) for member in entity.fields]
        return ResourceTraits(
            any(child.has_resources for child in children),
            all(child.copyable for child in children),
            frozenset().union(*(child.features for child in children)),
            any(child.explicit for child in children),
        )

    return visit(type_spec, entities, frozenset())
