"""Experimental native backend package.

实现按职责分在 base / types / entities / stmts / exprs / builtins / gen 几个模块里，
对外只暴露 generate_native_llvm_ir 这一个入口。
"""

from .gen import NativeLLVMGen, generate_native_llvm_ir

__all__ = ["NativeLLVMGen", "generate_native_llvm_ir"]
