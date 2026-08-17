from __future__ import annotations

from ...analysis.typesys import is_bool, is_cptr, is_error, is_handle, is_null, is_numeric, is_ptr, is_string, is_symbol, type_of
from ...core import ast
from ...core.errors import SonCompileError
from .base import LLVMValue, NativeGenBase


class TypesMixin(NativeGenBase):
    """SA 类型系统到 LLVM 类型的映射，以及值的转换与真值判定。"""

    def array_element_type(self, type_spec: ast.TypeSpec) -> ast.TypeSpec:
        return ast.TypeSpec(type_spec.name, type_spec.subtype, type_spec.inner)

    def is_string_array(self, type_spec: ast.TypeSpec) -> bool:
        return is_string(type_spec) and type_spec.array_size is not None

    def is_string_scalar(self, type_spec: ast.TypeSpec) -> bool:
        return is_string(type_spec) and type_spec.array_size is None

    def is_entity_scalar(self, type_spec: ast.TypeSpec) -> bool:
        return type_spec.name == "ENTITY" and type_spec.array_size is None

    def is_owned_string_resource(self, type_spec: ast.TypeSpec) -> bool:
        return is_string(type_spec)

    def c_cast_arg(self, value: LLVMValue, param_type: ast.TypeSpec) -> LLVMValue:
        return self.cast_value(value, param_type)

    def for_cast(self, value: LLVMValue, var_ty: str) -> LLVMValue:
        if var_ty == "i64":
            return self.cast_to_i64(value)
        return self.cast_to_double(value)

    def cast_value(self, value: LLVMValue, target: ast.TypeSpec) -> LLVMValue:
        if is_symbol(target):
            if value.type_name != "ptr":
                raise SonCompileError("native 后端 SYMBOL 赋值需要 SYMBOL 值")
            return LLVMValue("ptr", value.value, target)
        if is_ptr(target) or is_cptr(target):
            if value.type_name == "ptr":
                return LLVMValue("ptr", value.value, target)
            int_value = self.cast_to_i64(value)
            temp = self.next_temp()
            self.emit(f"  {temp} = inttoptr i64 {int_value.value} to ptr")
            return LLVMValue("ptr", temp, target)
        if is_handle(target):
            if value.type_name == "i64":
                return LLVMValue("i64", value.value, target)
            if value.type_name == "ptr" and is_null(value.type_spec or ast.TypeSpec("VOID")):
                return LLVMValue("i64", "0", target)
            raise SonCompileError("native 后端 HANDLE 只能接收同 kind HANDLE 或 NULL")
        if target.name == "STRING":
            if value.type_name != "ptr":
                raise SonCompileError("native 后端字符串赋值需要字符串值")
            return LLVMValue("ptr", value.value, target)
        if target.name == "ENTITY":
            if value.type_name != self.llvm_type(target):
                raise SonCompileError("native 后端 ENTITY 赋值需要同类型 ENTITY 值")
            return LLVMValue(value.type_name, value.value, target)
        if target.name == "BOOL":
            return self.truthy(value)
        if target.name == "NUM" and target.subtype == "DOUBLE":
            return self.cast_to_double(value)
        if target.name == "NUM":
            return self.cast_to_i64(value)
        if target.name == "VOID":
            return value
        raise SonCompileError(f"native 后端暂不支持类型转换: {target.name}")

    def cast_to_i64(self, value: LLVMValue) -> LLVMValue:
        if value.type_name == "i64":
            return LLVMValue("i64", value.value, ast.TypeSpec("NUM", "LONG"))
        temp = self.next_temp()
        if value.type_name == "i1":
            self.emit(f"  {temp} = zext i1 {value.value} to i64")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if value.type_name == "double":
            self.emit(f"  {temp} = fptosi double {value.value} to i64")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if value.type_name == "ptr":
            self.emit(f"  {temp} = ptrtoint ptr {value.value} to i64")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        raise SonCompileError(f"native 后端无法转为 LONG: {value.type_name}")

    def cast_to_double(self, value: LLVMValue) -> LLVMValue:
        if value.type_name == "double":
            return LLVMValue("double", value.value, ast.TypeSpec("NUM", "DOUBLE"))
        temp = self.next_temp()
        if value.type_name == "i64":
            self.emit(f"  {temp} = sitofp i64 {value.value} to double")
            return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
        if value.type_name == "i1":
            wide = self.cast_to_i64(value)
            self.emit(f"  {temp} = sitofp i64 {wide.value} to double")
            return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
        if value.type_name == "ptr":
            wide = self.cast_to_i64(value)
            self.emit(f"  {temp} = sitofp i64 {wide.value} to double")
            return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
        raise SonCompileError(f"native 后端无法转为 DOUBLE: {value.type_name}")

    def truthy(self, value: LLVMValue) -> LLVMValue:
        if value.type_name == "i1":
            return LLVMValue("i1", value.value, ast.TypeSpec("BOOL"))
        temp = self.next_temp()
        if value.type_name == "i64":
            self.emit(f"  {temp} = icmp ne i64 {value.value}, 0")
            return LLVMValue("i1", temp, ast.TypeSpec("BOOL"))
        if value.type_name == "double":
            self.emit(f"  {temp} = fcmp one double {value.value}, 0.0")
            return LLVMValue("i1", temp, ast.TypeSpec("BOOL"))
        if value.type_name == "ptr":
            self.emit(f"  {temp} = icmp ne ptr {value.value}, null")
            return LLVMValue("i1", temp, ast.TypeSpec("BOOL"))
        raise SonCompileError(f"native 后端无法转为 BOOL: {value.type_name}")

    def type_of_expr(self, expr: ast.Expr) -> ast.TypeSpec:
        return type_of(
            expr,
            self.current_symbols(),
            self.checked.subs,
            self.checked.entities,
            self.checked.uses,
            self.checked.external_modules,
            self.checked.c_funcs,
        )

    def is_string_type(self, type_spec: ast.TypeSpec | None) -> bool:
        return type_spec is not None and self.is_string_scalar(type_spec)

    def is_pointer_like_type(self, type_spec: ast.TypeSpec | None) -> bool:
        return type_spec is not None and (is_ptr(type_spec) or is_cptr(type_spec) or is_null(type_spec))

    def llvm_type(self, type_spec: ast.TypeSpec) -> str:
        if type_spec.array_size is not None:
            return f"[{type_spec.array_size} x {self.llvm_type(self.array_element_type(type_spec))}]"
        if type_spec.name == "VOID":
            return "void"
        if type_spec.name == "BOOL":
            return "i1"
        if type_spec.name == "HANDLE":
            return "i64"
        if type_spec.name in {"STRING", "PTR", "CPTR", "SYMBOL"}:
            return "ptr"
        if type_spec.name == "ERROR":
            return "%SaError"
        if type_spec.name == "ENTITY":
            return self.entity_type_from_spec(type_spec)
        if type_spec.name == "NUM" and type_spec.subtype == "DOUBLE":
            return "double"
        if type_spec.name == "NUM" and type_spec.subtype == "FLOAT":
            # 以前 FLOAT 落到下面的 i64 分支，1.5 被 fptosi 存成 1，而 C 后端是真 float。
            # 不能简单改映射成 double：ENTITY 里的 FLOAT 字段会从 4 字节变 8 字节，
            # 与 C 后端编出来的模块 struct 布局对不上，比截断更糟。
            raise SonCompileError("native 后端暂不支持 NUM AS FLOAT，请改用 AS DOUBLE 或改用 C 后端")
        if type_spec.name == "NUM":
            return "i64"
        raise SonCompileError(f"native 后端暂不支持类型: {type_spec.name}")

    def c_abi_type(self, type_spec: ast.TypeSpec) -> str:
        return "i32" if is_bool(type_spec) else self.llvm_type(type_spec)

    def c_abi_param_decl(self, param: ast.Param) -> str:
        name = self.c_ident(param.name)
        if param.by_ref:
            return f"ptr %{name}"
        return f"{self.c_abi_type(param.type_spec)} %{name}"

    def default_value(self, type_spec: ast.TypeSpec) -> str:
        if type_spec.name == "VOID":
            return ""
        if type_spec.array_size is not None:
            return "zeroinitializer"
        if is_string(type_spec):
            return "@.sa_empty"
        if is_ptr(type_spec) or is_cptr(type_spec) or is_symbol(type_spec):
            return "null"
        if is_error(type_spec):
            return "zeroinitializer"
        if type_spec.name == "ENTITY":
            return "zeroinitializer"
        if is_bool(type_spec):
            return "0"
        if is_numeric(type_spec) and type_spec.subtype == "DOUBLE":
            return "0.0"
        return "0"

    def param_decl(self, param: ast.Param) -> str:
        name = self.c_ident(param.name)
        if param.by_ref:
            return f"ptr %{name}"
        return f"{self.llvm_type(param.type_spec)} %{name}"
