"""C 后端 codegen 包。

以前是一个 2200 行的 codegen.py、一个 CGen 类 136 个方法，找东西全靠搜。现在按职责拆成
Mixin，布局照抄 native 后端：base 放状态与底层设施，其余每个文件一个关注点，gen 组装。
对外接口不变：CGen 与 generate_c 仍从这里导入，调用方一行不用改。
"""
from .base import AsyncFrameCtx, c_comment_text, c_number, c_string, stmt_gosub_lines, stmt_has_gosub, stmt_has_goto
from .gen import CGen, generate_c

__all__ = [
    "AsyncFrameCtx",
    "CGen",
    "c_comment_text",
    "c_number",
    "c_string",
    "generate_c",
    "stmt_gosub_lines",
    "stmt_has_gosub",
    "stmt_has_goto",
]
