"""程序级发射：generate() 的顶层编排与全局变量的初始化 / 释放。"""
from __future__ import annotations

from ...analysis.typesys import is_cptr, is_error, is_handle, is_ptr, is_string, is_symbol
from ...core import ast
from ...core.names import module_header_name, module_symbol_prefix
from ..c_runtime import RUNTIME_PRELUDE
from ..runtime_slicer import runtime_impl_for, runtime_symbols_in
from .base import CGenBase, stmt_has_goto


class ProgramMixin(CGenBase):
    """整份 .c 的骨架：前导、全局、原型、每个 SUB、main / 模块 init-free。"""

    def generate(self) -> str:
        # 先生成用户代码，再据此决定注入哪些运行时片段。顺序不能反：
        # preamble 要知道 body 用到了哪些 sa_* 才能算依赖闭包。
        body = ["", self.generate_entities(), "", self.generate_globals(), "", self.generate_async_frame_decls(), "", self.generate_prototypes(), ""]
        for sub in self.checked.program.subs:
            body.append(self.generate_sub(sub))
            body.append("")
        if self.module_name and not self.include_main:
            body.append(self.generate_module_init())
            body.append("")
            body.append(self.generate_module_free())
        if self.include_main:
            body.append(self.generate_c_main())
        chunks = self.generate_preamble("\n".join(body))
        chunks.extend(body)
        return "\n".join(chunks).rstrip() + "\n"

    def generate_preamble(self, body: str = "") -> list[str]:
        if self.include_runtime:
            prefix = self.runtime_feature_prefix()
            runtime = RUNTIME_PRELUDE.strip() + "\n\n" + runtime_impl_for(
                runtime_symbols_in(body), self.runtime_features()
            )
            lines = [(prefix + runtime).rstrip()]
        else:
            lines = []
            lines.extend(self.runtime_feature_defines())
            lines.append('#include "sa_runtime.h"')
            if self.dynamic and self.module_name:
                build_macro = f"SA_BUILD_{module_symbol_prefix(self.module_name).upper().replace('-', '_')}"
                lines.append(f"#define {build_macro}")
                lines.append(f'#include "{module_header_name(self.module_name)}"')
        for header in self.usec_includes():
            lines.append(header)
        lines.extend(f'#include "{header}"' for header in self.include_headers)
        return lines

    def usec_includes(self) -> list[str]:
        lines: list[str] = []
        for header in self.c_headers.values():
            if header.is_system:
                lines.append(f"#include <{header.header}>")
            else:
                lines.append(f'#include "{header.header}"')
        return lines

    def generate_globals(self) -> str:
        lines: list[str] = []
        for decl in self.checked.program.declarations:
            c_name = self.global_c_name(decl)
            ctype = self.c_type(decl.type_spec)
            if decl.type_spec.array_size is not None:
                lines.append(self.source_comment(decl.line_no, 0))
                storage = "" if self.is_exported_const(decl) else "static "
                lines.append(f"{storage}{ctype} {c_name}[{decl.type_spec.array_size}] = {{0}};")
                continue
            init = "NULL" if is_string(decl.type_spec) or is_cptr(decl.type_spec) or is_ptr(decl.type_spec) else "0"
            if decl.type_spec.name == "ENTITY":
                init = "{0}"
            if is_error(decl.type_spec):
                init = '{0, "ERR_NONE", NULL, 0, NULL}'
            if is_symbol(decl.type_spec):
                init = "NULL"
            lines.append(self.source_comment(decl.line_no, 0))
            storage = "" if self.is_exported_const(decl) else "static "
            lines.append(f"{storage}{ctype} {c_name} = {init};")
        return "\n".join(lines)

    def generate_prototypes(self) -> str:
        return "\n".join(self.sub_signature(sub) + ";" for sub in self.checked.program.subs if not sub.is_async)

    def generate_sub(self, sub: ast.Subroutine) -> str:
        if sub.is_async:
            return self.generate_async_sub(sub)
        self.push_sub_scope(sub)
        self.sub_name_stack.append(sub.name)
        self.sub_return_type_stack.append(sub.return_type)
        gosub_lines = self.sub_gosub_lines(sub)
        self.sub_gosub_stack.append(bool(gosub_lines))
        self.sub_gosub_lines_stack.append(gosub_lines)
        self.sub_has_goto_stack.append(any(stmt_has_goto(stmt) for stmt in sub.body))
        self.hoisted_catch_vars = {}
        self.local_resource_stack.append([])
        body = self.prepare_value_param_resources(sub, 1)
        for stmt in sub.body:
            body.extend(self.stmt(stmt, 1))
        body.extend(self.local_resource_cleanup_lines(self.local_resource_stack[-1], 1))
        self.local_resource_stack.pop()
        if self.hoisted_catch_vars:
            hoist = [f"    SaError {name} = {{0, \"ERR_NONE\", NULL, 0, NULL}};" for name in self.hoisted_catch_vars.values()]
            body = [*hoist, *body]
            # SUB 末尾兜底清理提升的 CATCH 变量：若控制流被 GOSUB RETURN 或 GOTO 跳过了块尾
            # 的 sa_error_clear，最后一次捕获的 message 仍残留在函数作用域变量里。clear 幂等，
            # 与正常路径的块尾清理叠加不会双重 free。
            body.extend(f"    sa_error_clear(&{name});" for name in self.hoisted_catch_vars.values())
        if self.sub_gosub_stack[-1]:
            body = ["    int sa_gosub_stack[64];", "    int sa_gosub_top = 0;", *body]

        self.sub_gosub_lines_stack.pop()
        self.sub_gosub_stack.pop()
        self.sub_has_goto_stack.pop()
        self.sub_return_type_stack.pop()
        self.sub_name_stack.pop()
        self.scope_stack.pop()
        if not body:
            body = ["    return;" if sub.return_type.name == "VOID" else f"    return {self.default_value(sub.return_type)};"]
        return "\n".join([self.source_comment(sub.line_no, 0), self.sub_signature(sub) + " {", *body, "}"])

    def generate_c_main(self) -> str:
        lines = ["int main(void) {", "    sa_setup_console();"]
        lines.extend(f"    {call}();" for call in self.main_init_calls)
        lines.extend(self.init_string_globals())
        lines.extend(self.init_entity_globals())
        lines.extend(self.init_global_values())
        lines.extend(self.emit_top_level())
        lines.append("sa_program_end:")
        lines.extend(self.free_string_globals())
        lines.extend(f"    {call}();" for call in reversed(self.main_free_calls))
        # 释放运行时全局错误对象残留的 message：最后一次未捕获/已捕获错误的 strdup 副本
        lines.append("    sa_error_clear(&sa_current_error);")
        lines.append("    return 0;")
        lines.append("}")
        return "\n".join(lines)

    def generate_module_init(self) -> str:
        lines = [f"void {module_symbol_prefix(self.module_name or '')}_init(void) {{"]
        lines.extend(self.init_string_globals())
        lines.extend(self.init_entity_globals())
        lines.extend(self.init_global_values())
        lines.append("}")
        return "\n".join(lines)

    def generate_module_free(self) -> str:
        lines = [f"void {module_symbol_prefix(self.module_name or '')}_free(void) {{"]
        lines.extend(self.free_string_globals())
        lines.append("}")
        return "\n".join(lines)

    def init_string_globals(self) -> list[str]:
        lines: list[str] = []
        for decl in self.checked.program.declarations:
            if not is_string(decl.type_spec):
                continue
            lines.append(self.source_comment(decl.line_no, 1))
            if decl.type_spec.array_size is not None:
                idx = self.next_temp()
                name = self.global_c_name(decl)
                lines.append(f"    for (long long {idx} = 0; {idx} < {decl.type_spec.array_size}; {idx}++) {{")
                lines.append(f"        {name}[{idx}] = sa_strdup(\"\");")
                lines.append(f"    }}")
            else:
                lines.append(f"    {self.global_c_name(decl)} = sa_strdup(\"\");")
        return lines

    def init_entity_globals(self) -> list[str]:
        lines: list[str] = []
        for decl in self.checked.program.declarations:
            if decl.type_spec.name != "ENTITY" or not self.type_has_managed_resources(decl.type_spec):
                continue
            lines.append(self.source_comment(decl.line_no, 1))
            lines.extend(self.entity_init_lines(self.global_c_name(decl), decl.type_spec, 1))
        return lines

    def init_global_values(self) -> list[str]:
        lines: list[str] = []
        for decl in self.checked.program.declarations:
            if decl.expr is None:
                continue
            prelude, value, cleanup = self.expr_with_prelude(decl.expr)
            lines.append(self.source_comment(decl.line_no, 1))
            lines.extend(f"    {line}" for line in prelude)
            if is_string(decl.type_spec):
                lines.append(f"    sa_set_string(&{self.global_c_name(decl)}, {value});")
            elif decl.type_spec.name == "ENTITY" and self.type_has_managed_resources(decl.type_spec):
                lines.extend(self.entity_copy_lines(self.global_c_name(decl), value, decl.type_spec, 1))
            else:
                if is_handle(decl.type_spec) and isinstance(decl.expr, ast.NullLiteral):
                    value = "0"
                lines.append(f"    {self.global_c_name(decl)} = {value};")
            lines.extend(f"    {line}" for line in cleanup)
        return lines

    def emit_top_level(self) -> list[str]:
        lines: list[str] = []
        ended = False
        for stmt in self.checked.program.top_level:
            if ended:
                break
            lines.extend(self.stmt(stmt, 1))
            if isinstance(stmt, ast.End):
                ended = True
        return lines

    def free_string_globals(self) -> list[str]:
        lines: list[str] = []
        for decl in self.checked.program.declarations:
            if is_string(decl.type_spec):
                if decl.type_spec.array_size is not None:
                    idx = self.next_temp()
                    name = self.global_c_name(decl)
                    lines.append(f"    for (long long {idx} = 0; {idx} < {decl.type_spec.array_size}; {idx}++) {{")
                    lines.append(f"        free({name}[{idx}]);")
                    lines.append(f"    }}")
                else:
                    lines.append(f"    free({self.global_c_name(decl)});")
            elif is_error(decl.type_spec):
                lines.append(f"    sa_error_clear(&{self.global_c_name(decl)});")
            elif is_symbol(decl.type_spec):
                lines.append(f"    sa_symbol_free({self.global_c_name(decl)});")
            elif decl.type_spec.name == "ENTITY" and self.type_has_managed_resources(decl.type_spec):
                lines.extend(self.entity_free_lines(self.global_c_name(decl), decl.type_spec, 1))
        return lines
