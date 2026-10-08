"""托管容器的元素操作表。

容器只按 ops->size 搬字节，元素的值语义全在这张表里：copy 决定 PUSH 复制出什么，
drop 决定移除和析构时释放什么，copyable 决定容器开不开放复制接口。

runtime 预置了标量、裸句柄、指针和嵌套容器那几张（元素的全部语义就是搬字节），其余
类型要按实际类型生成胶水。两条硬约束决定了下面这些函数长什么样：

1. **copy 的目标是只写的生料字节**。`sa_vec_insert` 给的是 memmove 腾出来的位置，
   `sa_vec_set` 给的是 malloc 暂存区，两者都没有可读的旧值。所以不能用 `sa_set_string`
   `sa_set_error` 这类赋值助手——它们会先 free 目标的旧内容，那是读未初始化内存。
2. **ops 表是 `static const`，必须定义在使用之前**。const 对象没法先声明后定义，
   所以元素类型要提前一次收全（`managed_container_elements`），不能边发射边登记。
"""
from __future__ import annotations

from ...analysis.resource_traits import handle_drop_value_function, preset_value_ops
from ...analysis.typesys import c_type, describe_type, is_managed_handle, managed_container_elements
from ...core import ast
from ...core.errors import SonCompileError
from ...core.names import value_ops_c_name
from .base import CGenBase


class ValueOpsMixin(CGenBase):
    """按元素类型发射 copy / drop 胶水和 SaValueOps 常量。"""

    def generate_value_ops(self) -> str:
        chunks: list[str] = []
        for element in managed_container_elements(self.checked.program):
            if preset_value_ops(element) is not None:
                continue
            chunks.extend(self.value_ops_definition(element))
            chunks.append("")
        return "\n".join(chunks).rstrip()

    def value_ops_ref(self, element: ast.TypeSpec) -> str:
        """构造容器时传给 runtime 的那张表，取地址形式。"""
        preset = preset_value_ops(element)
        name = preset if preset is not None else value_ops_c_name(describe_type(element))
        return f"&{name}"

    def value_ops_definition(self, element: ast.TypeSpec) -> list[str]:
        name = value_ops_c_name(describe_type(element))
        ctype = c_type(element)
        # 指针类元素的 copy/drop 只有两行，差别仅在克隆和释放用哪个函数；拉一张小表
        # 比写四份结构相同的分支清楚。SYMBOL 和 SaCallable* 本身就是指针类型。
        pointer_like = {
            "STRING": ("sa_strdup", "free"),
            "SYMBOL": ("sa_symbol_clone", "sa_symbol_free"),
            "SUB": ("sa_callable_retain", "sa_callable_release"),
        }.get(element.name)
        if pointer_like is not None:
            clone, release = pointer_like
            return [
                f"/* 容器元素 {describe_type(element)} */",
                f"static void {name}_copy(void* target, const void* source) {{",
                # const 修饰的是源槽位本身，不是指针指向的内容：`{ctype} const*` 而不是
                # `const {ctype}*`——后者对 char* 会展开成 const char**，等于悄悄丢掉源的 const。
                f"    *({ctype}*)target = {clone}(*({ctype} const*)source);",
                "}",
                f"static void {name}_drop(void* value) {{",
                f"    {release}(*({ctype}*)value);",
                # 置空不是为了好看：移除元素后那格字节可能被 memmove 搬到别处再析构一次，
                # 清掉才不会释放第二遍。
                f"    *({ctype}*)value = NULL;",
                "}",
                f"static const SaValueOps {name} = {{ sizeof({ctype}), 1, {name}_copy, {name}_drop }};",
            ]
        if element.name == "ERROR":
            return [
                f"/* 容器元素 {describe_type(element)} */",
                f"static void {name}_copy(void* target, const void* source) {{",
                "    SaError* out = (SaError*)target;",
                "    const SaError* in = (const SaError*)source;",
                # 整体拷贝再单独补 message：只有它是堆上的。不走 sa_set_error，
                # 那个函数会 free(out->message)，而 out 这会儿还是生料。
                "    *out = *in;",
                "    out->message = sa_strdup(in->message);",
                "}",
                f"static void {name}_drop(void* value) {{",
                "    sa_error_clear((SaError*)value);",
                "}",
                f"static const SaValueOps {name} = {{ sizeof(SaError), 1, {name}_copy, {name}_drop }};",
            ]
        if is_managed_handle(element):
            # 一张表就够，不用生成任何函数：runtime 的 _drop_value 签名正好是 SaValueOps.drop
            # 要的那个，而独占资源没有 copy——复制一个句柄等于两个持有者，析构会关两遍。
            # copyable = 0 让容器自己挡掉 PUSH/GET 这些复制接口，只放行 _MOVE 形态。
            return [
                f"/* 容器元素 {describe_type(element)}：独占，只能移动 */",
                f"static const SaValueOps {name} = "
                f"{{ sizeof(SaHandle), 0, NULL, {handle_drop_value_function(element)} }};",
            ]
        raise SonCompileError(f"托管容器元素 {describe_type(element)} 的 ops 胶水还没接入", 0)
