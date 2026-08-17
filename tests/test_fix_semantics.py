"""语义层收紧修复的回归测试。

每条都配一正一反：非法写法必须报 SA 诊断（而不是漏到 C 编译器或 codegen 崩溃），
合法写法必须仍然通过——收紧类型检查最容易误伤原本正确的程序。
"""
from __future__ import annotations

from conftest import expect_error
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.frontend.parser import parse_program


def accept(source: str) -> None:
    """断言源码能通过完整语义检查。"""
    check_program(parse_program(source))


# --- REF 参数必须类型精确匹配 -------------------------------------------------
# codegen 对 REF 实参直接 &(var) 取址，形参实参共用内存，没有值传递那层隐式转换兜底。

def test_ref_param_rejects_numeric_widening() -> None:
    expect_error(
        "10 SUB bump(value AS NUM AS DOUBLE AS REF) AS VOID\n"
        "20 value = value + 1.0\n"
        "30 .ENDSUB\n"
        "40 DIM counter AS NUM AS LONG AS VAR\n"
        "50 SUB main AS PUBLIC AS VOID\n"
        "60 CALL bump(counter)\n"
        "70 .ENDSUB\n"
        "80 CALL main\n"
        "90 END",
        "REF 参数 value 需要 NUM AS DOUBLE 变量",
    )


def test_ref_param_rejects_bool_for_long() -> None:
    expect_error(
        "10 SUB bump(value AS NUM AS LONG AS REF) AS VOID\n"
        "20 value = value + 1\n"
        "30 .ENDSUB\n"
        "40 DIM flag AS BOOL AS VAR\n"
        "50 SUB main AS PUBLIC AS VOID\n"
        "60 CALL bump(flag)\n"
        "70 .ENDSUB\n"
        "80 CALL main\n"
        "90 END",
        "REF 不做隐式转换",
    )


def test_ref_param_rejects_entity_kind_mismatch() -> None:
    expect_error(
        "10 FOR ENTITY AS Vec\n"
        "20 DIM x AS NUM AS DOUBLE AS VAR\n"
        "30 .ENDENTITY\n"
        "40 FOR ENTITY AS Dot\n"
        "50 DIM x AS NUM AS DOUBLE AS VAR\n"
        "60 .ENDENTITY\n"
        "70 SUB move(point AS ENTITY AS Vec AS REF) AS VOID\n"
        "80 point.x = point.x + 1.0\n"
        "90 .ENDSUB\n"
        "100 DIM d AS ENTITY AS Dot AS VAR\n"
        "110 SUB main AS PUBLIC AS VOID\n"
        "120 CALL move(d)\n"
        "130 .ENDSUB\n"
        "140 CALL main\n"
        "150 END",
        "REF 参数 point 需要 ENTITY AS Vec 变量",
    )


def test_ref_param_accepts_exact_types() -> None:
    accept(
        "10 FOR ENTITY AS Vec\n"
        "20 DIM x AS NUM AS DOUBLE AS VAR\n"
        "30 .ENDENTITY\n"
        "40 SUB bumpl(value AS NUM AS LONG AS REF) AS VOID\n"
        "50 value = value + 1\n"
        "60 .ENDSUB\n"
        "70 SUB bumpd(value AS NUM AS DOUBLE AS REF) AS VOID\n"
        "80 value = value + 1.0\n"
        "90 .ENDSUB\n"
        "100 SUB flip(flag AS BOOL AS REF) AS VOID\n"
        "110 flag = FALSE\n"
        "120 .ENDSUB\n"
        "130 SUB move(point AS ENTITY AS Vec AS REF) AS VOID\n"
        "140 point.x = point.x + 1.0\n"
        "150 .ENDSUB\n"
        "160 SUB rename(text AS STRING AS REF) AS VOID\n"
        "170 text = \"changed\"\n"
        "180 .ENDSUB\n"
        # REF 形参再往下传 REF：转发时类型一致，不能被误判
        "190 SUB relay(inner AS NUM AS LONG AS REF) AS VOID\n"
        "200 CALL bumpl(inner)\n"
        "210 .ENDSUB\n"
        "220 DIM n AS NUM AS LONG AS VAR\n"
        "230 DIM d AS NUM AS DOUBLE AS VAR\n"
        "240 DIM b AS BOOL AS VAR\n"
        "250 DIM v AS ENTITY AS Vec AS VAR\n"
        "260 DIM s AS STRING AS VAR\n"
        "270 SUB main AS PUBLIC AS VOID\n"
        "280 CALL bumpl(n)\n"
        "290 CALL bumpd(d)\n"
        "300 CALL flip(b)\n"
        "310 CALL move(v)\n"
        "320 CALL rename(s)\n"
        "330 CALL relay(n)\n"
        "340 .ENDSUB\n"
        "350 CALL main\n"
        "360 END"
    )


# --- @ 不能对没有存储位置的东西取址 -------------------------------------------
# 以前这两种都能过语义检查，然后在 codegen 的 c_value 里抛 Python KeyError。

def test_address_of_enum_member_is_rejected() -> None:
    expect_error(
        "10 ENUM Color\n"
        "20 RED\n"
        "30 GREEN\n"
        "40 .ENDENUM\n"
        "50 DIM p AS PTR TO NUM AS LONG AS VAR\n"
        "60 SUB main AS PUBLIC AS VOID\n"
        "70 p = @Color.RED\n"
        "80 .ENDSUB\n"
        "90 CALL main\n"
        "100 END",
        "不能对枚举成员取地址",
    )


def test_address_of_builtin_const_is_rejected() -> None:
    expect_error(
        "10 USE SYS.MATH AS m\n"
        "20 DIM p AS PTR TO NUM AS DOUBLE AS VAR\n"
        "30 SUB main AS PUBLIC AS VOID\n"
        "40 p = @m.PI\n"
        "50 .ENDSUB\n"
        "60 CALL main\n"
        "70 END",
        "不能对模块常量取地址",
    )


def test_address_of_variable_and_entity_field_still_works() -> None:
    accept(
        "10 FOR ENTITY AS Vec\n"
        "20 DIM x AS NUM AS DOUBLE AS VAR\n"
        "30 .ENDENTITY\n"
        "40 ENUM Color\n"
        "50 RED\n"
        "60 GREEN\n"
        "70 .ENDENUM\n"
        "80 DIM n AS NUM AS LONG AS VAR\n"
        "90 DIM v AS ENTITY AS Vec AS VAR\n"
        "100 DIM p AS PTR TO NUM AS LONG AS VAR\n"
        "110 DIM q AS PTR TO NUM AS DOUBLE AS VAR\n"
        "120 SUB main AS PUBLIC AS VOID\n"
        # 枚举成员当值用没问题，被禁的只有取址
        "130 n = Color.GREEN\n"
        "140 p = @n\n"
        "150 q = @v.x\n"
        "160 .ENDSUB\n"
        "170 CALL main\n"
        "180 END"
    )


# --- 一元运算符的操作数类型 ---------------------------------------------------

def test_unary_minus_on_string_is_rejected() -> None:
    expect_error(
        "10 DIM s AS STRING AS VAR\n"
        "20 SUB main AS PUBLIC AS VOID\n"
        "30 s = -\"abc\"\n"
        "40 .ENDSUB\n"
        "50 CALL main\n"
        "60 END",
        "一元 - 只能用于数值、BOOL 或 SYMBOL",
    )


def test_unary_minus_on_entity_is_rejected() -> None:
    expect_error(
        "10 FOR ENTITY AS Vec\n"
        "20 DIM x AS NUM AS DOUBLE AS VAR\n"
        "30 .ENDENTITY\n"
        "40 DIM v AS ENTITY AS Vec AS VAR\n"
        "50 DIM w AS ENTITY AS Vec AS VAR\n"
        "60 SUB main AS PUBLIC AS VOID\n"
        "70 w = -v\n"
        "80 .ENDSUB\n"
        "90 CALL main\n"
        "100 END",
        "一元 - 只能用于数值、BOOL 或 SYMBOL",
    )


def test_not_on_string_is_rejected() -> None:
    # codegen 发 `!ptr`（判指针非空），与 `IF s` 的「非空串」口径相反，
    # 是静默给错答案，宁可拒绝。
    expect_error(
        "10 DIM s AS STRING AS VAR\n"
        "20 SUB main AS PUBLIC AS VOID\n"
        "30 s = \"hi\"\n"
        "40 IF NOT s THEN\n"
        "50 PRINT \"empty\"\n"
        "60 .ENDIF\n"
        "70 .ENDSUB\n"
        "80 CALL main\n"
        "90 END",
        "NOT 只能用于 BOOL、数值或指针/句柄",
    )


def test_unary_on_numeric_bool_pointer_still_works() -> None:
    accept(
        "10 DIM n AS NUM AS LONG AS VAR\n"
        "20 DIM d AS NUM AS DOUBLE AS VAR\n"
        "30 DIM b AS BOOL AS VAR\n"
        "40 DIM sym AS SYMBOL AS VAR\n"
        "50 DIM p AS PTR TO NUM AS LONG AS VAR\n"
        "60 SUB main AS PUBLIC AS VOID\n"
        "70 n = -5\n"
        "80 d = -1.5\n"
        "90 n = -n\n"
        "100 n = BNOT n\n"
        "110 b = NOT b\n"
        "120 b = NOT n\n"
        "130 p = @n\n"
        "140 b = NOT p\n"
        "150 sym = d\n"
        "160 .ENDSUB\n"
        "170 CALL main\n"
        "180 END"
    )


def test_unary_minus_on_symbol_is_not_a_semantic_error() -> None:
    # SYMBOL 能不能带一元负号是 codegen 的事（symbol_expr 目前不接 Unary，会自己报错），
    # 语义层不该抢在前面拒绝，否则以后 codegen 补上支持还得回来改这里。
    accept(
        "10 DIM sym AS SYMBOL AS VAR\n"
        "20 DIM other AS SYMBOL AS VAR\n"
        "30 SUB main AS PUBLIC AS VOID\n"
        "40 other = -sym\n"
        "50 .ENDSUB\n"
        "60 CALL main\n"
        "70 END"
    )


# --- 变量名不能撞 USE 别名 ----------------------------------------------------
# VarRef 先查模块常量再查符号表，重名时 `m.e` 会静默变成 SYS.MATH 的 E。

def test_global_variable_conflicting_with_use_alias_is_rejected() -> None:
    expect_error(
        "10 USE SYS.MATH AS m\n"
        "20 FOR ENTITY AS Box\n"
        "30 DIM e AS NUM AS DOUBLE AS VAR\n"
        "40 .ENDENTITY\n"
        "50 DIM m AS ENTITY AS Box AS VAR\n"
        "60 SUB main AS PUBLIC AS VOID\n"
        "70 m.e = 5.0\n"
        "80 .ENDSUB\n"
        "90 CALL main\n"
        "100 END",
        "变量名与 USE 别名冲突: m",
    )


def test_local_variable_conflicting_with_use_alias_is_rejected() -> None:
    expect_error(
        "10 USE SYS.STRING AS s\n"
        "20 SUB main AS PUBLIC AS VOID\n"
        "30 DIM s AS STRING AS VAR\n"
        "40 .ENDSUB\n"
        "50 CALL main\n"
        "60 END",
        "变量名与 USE 别名冲突: s",
    )


def test_sub_param_conflicting_with_use_alias_is_rejected() -> None:
    expect_error(
        "10 USE SYS.MATH AS m\n"
        "20 SUB f(m AS NUM AS LONG) AS VOID\n"
        "30 PRINT m\n"
        "40 .ENDSUB\n"
        "50 SUB main AS PUBLIC AS VOID\n"
        "60 CALL f(1)\n"
        "70 .ENDSUB\n"
        "80 CALL main\n"
        "90 END",
        "变量名与 USE 别名冲突: m",
    )


def test_non_conflicting_names_and_lint_alias_still_work() -> None:
    # SYS.LINT 的「别名」是 lint 选项名，不参与 alias.member 解析，不该算冲突
    accept(
        "10 USE SYS.MATH AS m\n"
        "20 USE SYS.LINT AS NONE_NUMBER\n"
        "30 DIM none_number AS NUM AS LONG AS VAR\n"
        "40 DIM radius AS NUM AS DOUBLE AS VAR\n"
        "50 DIM out AS NUM AS DOUBLE AS VAR\n"
        "60 SUB area(r AS NUM AS DOUBLE) AS NUM AS DOUBLE\n"
        "70 RETURN m.PI * r * r\n"
        "80 .ENDSUB\n"
        "90 SUB main AS PUBLIC AS VOID\n"
        "100 none_number = 1\n"
        "110 radius = 2.0\n"
        "120 out = CALL area(radius)\n"
        "130 .ENDSUB\n"
        "140 CALL main\n"
        "150 END"
    )
