"""把各 Mixin 组装成 CGen。"""
from __future__ import annotations

from ...analysis.semantics import CheckedProgram
from .base import CGenBase
from .builtins import BuiltinsMixin
from .coroutines import CoroutinesMixin
from .entities import EntitiesMixin
from .exprs import ExprsMixin
from .program import ProgramMixin
from .stmts import StmtsMixin


class CGen(ProgramMixin, CoroutinesMixin, StmtsMixin, ExprsMixin, BuiltinsMixin, EntitiesMixin, CGenBase):
    """C 后端生成器：把 CheckedProgram 变成一份 .c 文本。

    状态字段全在 CGenBase（dataclass）里，这里不再 @dataclass：Mixin 不带字段，再装饰一次
    只是把同一批字段重新生成一遍 __init__，没意义，还容易让人以为字段可以散落到各 Mixin 去。
    """


def generate_c(checked: CheckedProgram) -> str:
    generator = CGen(checked)
    return generator.generate()
