"""实体符号树必须独占；输出正确之外还检查两端退出时的净分配。"""
from pathlib import Path
import subprocess

import pytest

from conftest import build_temp, compile_user_c, expect_error, requires_gcc
from test_coroutines import _build_and_run
from test_native_backend import _native_live_allocs, compile_native_ir, requires_clang_lld


def _source(body: str) -> str:
    return "\n".join(f"{index * 10} {line}" for index, line in enumerate(
        (line.strip() for line in body.splitlines() if line.strip()), 1
    )) + "\n"


_TYPES = """
FOR ENTITY AS Box
DIM expr AS SYMBOL AS VAR
.ENDENTITY
FOR ENTITY AS Nest
DIM left AS ENTITY AS Box AS VAR
DIM right AS ENTITY AS Box AS VAR
.ENDENTITY
DIM x AS NUM AS LONG AS VAR
DIM globalbox AS ENTITY AS Box AS VAR
"""

_COPY = _source(_TYPES + """
SUB alias(a AS ENTITY AS Nest AS REF, b AS ENTITY AS Nest AS REF) AS VOID
a = b
.ENDSUB
SUB mutate(b AS ENTITY AS Nest) AS VOID
b.left.expr = x + 9
PRINT b.left.expr
.ENDSUB
SUB main AS PUBLIC AS VOID
DIM a AS ENTITY AS Nest AS VAR
a.left.expr = x + 1
a.right.expr = x * 2
DIM b AS ENTITY AS Nest AS VAR = a
a = a
CALL alias(a, a)
CALL mutate(a)
b.left.expr = x + 3
b.right = a.left
a.left.expr = x + 4
PRINT b.left.expr
PRINT b.right.expr
PRINT a.left.expr
PRINT a.right.expr
globalbox = b.right
.ENDSUB
CALL main
END
""")

_RETURNS = _source(_TYPES + """
SUB make() AS ENTITY AS Box
DIM a AS ENTITY AS Box AS VAR
a.expr = x + 1
RETURN a
.ENDSUB
SUB value(a AS ENTITY AS Box) AS ENTITY AS Box
DIM b AS ENTITY AS Box AS VAR
b m= a
RETURN b
.ENDSUB
SUB borrowed(a AS ENTITY AS Box AS REF) AS ENTITY AS Box
DIM b AS ENTITY AS Box AS VAR
b.expr = x + 99
b f= a
RETURN b
.ENDSUB
SUB reference(a AS ENTITY AS Box AS REF) AS ENTITY AS Box
RETURN a
.ENDSUB
SUB globalvalue() AS ENTITY AS Box
RETURN globalbox
.ENDSUB
SUB forward() AS ENTITY AS Box
RETURN make()
.ENDSUB
SUB field() AS SYMBOL
DIM a AS ENTITY AS Nest AS VAR
a.left.expr = x * 7
RETURN a.left.expr
.ENDSUB
SUB main AS PUBLIC AS VOID
DIM a AS ENTITY AS Box AS VAR = make()
DIM b AS ENTITY AS Box AS VAR
b.expr = x + 88
b m= a
DIM c AS ENTITY AS Box AS VAR = value(b)
c = borrowed(b)
b.expr = x + 2
PRINT c.expr
c = reference(b)
b.expr = x + 3
PRINT c.expr
globalbox = b
c = globalvalue()
globalbox.expr = x + 4
PRINT c.expr
c = forward()
PRINT c.expr
PRINT field()
DIM trap AS ERROR AS VAR
TRY CALL make() TRACEBACK ERROR AS trap
CATCH ERR_ANY AS e
PRINT "unexpected"
.ENDTRY
.ENDSUB
CALL main
END
""")

_THROW = _source(_TYPES + """
SUB fail() AS VOID
THROW NEW ERR_TEST, "boom"
.ENDSUB
SUB inner(a AS ENTITY AS Nest) AS VOID
DIM b AS ENTITY AS Nest AS VAR = a
DIM c AS ENTITY AS Nest AS VAR
c m= b
DIM view AS ENTITY AS Nest AS VAR
view f= c
CALL fail()
.ENDSUB
SUB main AS PUBLIC AS VOID
DIM a AS ENTITY AS Nest AS VAR
a.left.expr = x + 1
a.right.expr = x * 2
DIM trap AS ERROR AS VAR
TRY CALL inner(a) TRACEBACK ERROR AS trap
CATCH ERR_TEST AS e
PRINT a.left.expr
PRINT a.right.expr
.ENDTRY
.ENDSUB
CALL main
END
""")

_DEREF = _source(_TYPES + """
DIM calls AS NUM AS LONG AS VAR
SUB locate(p AS PTR TO ENTITY AS Nest) AS PTR TO ENTITY AS Nest
calls = calls + 1
RETURN p
.ENDSUB
SUB make() AS ENTITY AS Nest
DIM a AS ENTITY AS Nest AS VAR
a.left.expr = x + 7
a.right.expr = x * 8
RETURN a
.ENDSUB
SUB main AS PUBLIC AS VOID
DIM a AS ENTITY AS Nest AS VAR
DIM b AS ENTITY AS Nest AS VAR
DIM p AS PTR TO ENTITY AS Nest AS VAR
DIM q AS PTR TO ENTITY AS Nest AS VAR
a.left.expr = x + 1
a.right.expr = x * 2
b.left.expr = x + 99
b.right.expr = x * 99
p = @b
q = @b
^p = a
a.left.expr = x + 3
a.right.expr = x * 4
PRINT b.left.expr
PRINT b.right.expr
^p = ^p
^p = ^q
^p = b
PRINT b.left.expr
PRINT b.right.expr
^locate(p) = a
PRINT calls
PRINT b.left.expr
PRINT b.right.expr
^p = make()
PRINT b.left.expr
PRINT b.right.expr
a = ^p
b.left.expr = x + 9
PRINT a.left.expr
.ENDSUB
CALL main
END
""")

_CASES = [
    pytest.param(_COPY, ["(x + 9)", "(x + 3)", "(x + 1)", "(x + 4)", "(x * 2)"], id="嵌套与别名拷贝"),
    pytest.param(_RETURNS, ["(x + 1)", "(x + 2)", "(x + 3)", "(x + 1)", "(x * 7)"], id="返回移动借用"),
    pytest.param(_THROW, ["(x + 1)", "(x * 2)"], id="异常清理"),
    pytest.param(_DEREF, ["(x + 1)", "(x * 2)", "(x + 1)", "(x * 2)", "1",
                         "(x + 3)", "(x * 4)", "(x + 7)", "(x * 8)", "(x + 7)"], id="解引用赋值与别名"),
]


@pytest.mark.e2e
@requires_gcc
@pytest.mark.parametrize("source, expected", _CASES)
@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_c_entity_symbol_lifecycle(source: str, expected: list[str], opt: str) -> None:
    with build_temp("entity-symbol-", "ownership-tests") as temp:
        result = _build_and_run(source, temp, "entity", opt=opt, leak=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == expected
    assert "SA_LIVE=0" in result.stderr


@pytest.mark.e2e
@requires_clang_lld
@pytest.mark.parametrize("source, expected", _CASES)
def test_native_entity_symbol_lifecycle(source: str, expected: list[str]) -> None:
    with build_temp("entity-symbol-native-", "ownership-tests") as temp:
        assert _native_live_allocs(Path(temp), source) == 0
        result = subprocess.run([str(Path(temp) / "leak.exe")], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == expected
    assert "SA_LIVE=0" in result.stderr


def test_entity_symbol_initialization_and_copy_order() -> None:
    c = compile_user_c(_COPY)
    assert "sa_a.left.expr = NULL;" in c
    start = c.index("sa_symbol_clone(sa_a.left.expr)", c.index("static void sa_main(void) {"))
    assert start < c.index("sa_symbol_free(sa_b.left.expr)", start)
    ir = compile_native_ir(_COPY)
    assert "call ptr @sa_symbol_clone(" in ir
    assert "call void @sa_symbol_free(" in ir


@pytest.mark.parametrize("statement, error", [
    ("b f= a\na.expr = x", "变量已被 f= 借出"),
    ("b f= a\nb.expr = x", "借用来的变量是只读"),
    ("b m= a\nPRINT a.expr", "变量已被 m= 移走"),
])
def test_symbol_only_entity_ownership_rules(statement: str, error: str) -> None:
    expect_error(_source(_TYPES + """
SUB main AS PUBLIC AS VOID
DIM a AS ENTITY AS Box AS VAR
DIM b AS ENTITY AS Box AS VAR
""" + statement + "\n.ENDSUB\nCALL main\nEND"), error)
