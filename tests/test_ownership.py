"""`f=`（借用）与 `m=`（移动）赋值。

解析、所有权预检的每条规则、两个后端的生成产物，以及端到端的「输出正确 + 零泄漏」。
这批规则的价值全在负例：每条都对应 codegen 里一个具体的踩雷方式（双重 free、UAF、泄漏），
测试名里写的是规则，注释里写的是不这么拦会炸在哪。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import build_temp, compile_user_c, expect_error, requires_gcc
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.backend.native import generate_native_llvm_ir
from sonalgebraic.core import ast
from sonalgebraic.core.errors import SonCompileError
from sonalgebraic.frontend.parser import parse_program
from test_coroutines import _build_and_run
from test_native_backend import _native_live_allocs, requires_clang_lld


def _main(body: str) -> str:
    return f"10 SUB main AS PUBLIC AS VOID\n{body}\n900 .ENDSUB\n910 CALL main\n920 END\n"


_BOX = "1 FOR ENTITY AS Box\n2 DIM text AS STRING AS VAR\n3 DIM n AS NUM AS LONG AS VAR\n4 .ENDENTITY\n"


# --- 解析 ---

def test_parse_modes_and_case_insensitive() -> None:
    prog = parse_program(_main(
        "20 DIM x AS STRING AS VAR\n30 DIM y AS STRING AS VAR\n40 x f= y\n50 x M= y\n60 x = y"
    ))
    assigns = [stmt for stmt in prog.subs[0].body if isinstance(stmt, ast.Assign)]
    assert [stmt.mode for stmt in assigns] == ["borrow", "move", "copy"]
    assert all(isinstance(stmt.expr, ast.VarRef) and stmt.expr.name == "y" for stmt in assigns)


def test_parse_letter_glued_to_name_or_alone_is_plain_assign() -> None:
    # `xf = 1` 的 f 是变量名的一部分；`f=1` / `m = 2` 的 f/m 本身就是目标变量。三者都不是所有权标记。
    prog = parse_program(_main(
        "20 DIM xf AS NUM AS LONG AS VAR\n30 DIM f AS NUM AS LONG AS VAR\n40 DIM m AS NUM AS LONG AS VAR\n"
        "50 xf = 1\n60 f=1\n70 m = 2"
    ))
    assigns = [stmt for stmt in prog.subs[0].body if isinstance(stmt, ast.Assign)]
    assert [(stmt.target.name, stmt.mode) for stmt in assigns] == [("xf", "copy"), ("f", "copy"), ("m", "copy")]


def test_parse_rejects_ownership_in_declaration_initializer() -> None:
    with pytest.raises(SonCompileError, match="DIM/CONST 初始化只支持"):
        parse_program(_main("20 DIM y AS STRING AS VAR\n30 DIM x AS STRING AS VAR f= y"))
    with pytest.raises(SonCompileError, match="DIM/CONST 初始化只支持"):
        parse_program("1 DIM y AS STRING AS VAR\n2 CONST x AS STRING m= y\n" + _main(""))


# --- 语义：合法程序 ---

def test_accepts_move_and_borrow_on_all_managed_types() -> None:
    check_program(parse_program(_BOX + _main(
        "20 DIM a AS STRING AS VAR\n30 DIM b AS STRING AS VAR\n40 a m= b\n"
        "50 DIM c AS STRING AS VAR\n60 c f= a\n"
        "70 DIM s1 AS SYMBOL AS VAR\n80 DIM s2 AS SYMBOL AS VAR\n90 s2 m= s1\n"
        "100 DIM e1 AS ERROR AS VAR\n110 DIM e2 AS ERROR AS VAR\n120 e2 m= e1\n"
        "130 DIM b1 AS ENTITY AS Box AS VAR\n140 DIM b2 AS ENTITY AS Box AS VAR\n150 b2 m= b1\n"
        "160 PRINT a\n170 PRINT c\n180 PRINT b2.text"
    )))


def test_borrow_source_unfreezes_when_borrower_block_ends() -> None:
    # 借用者 v 死在 IF 块尾，之后再改 s 是安全的——冻结必须跟着借用者的生命周期走，
    # 否则 IF 里借一次、后面整个 SUB 都不能碰 s，规则就没法用了。
    check_program(parse_program(_main(
        "20 DIM s AS STRING AS VAR\n30 IF 1 THEN\n40 DIM v AS STRING AS VAR\n50 v f= s\n60 PRINT v\n70 .ENDIF\n"
        '80 s = "changed"'
    )))


def test_borrowed_value_can_be_read_copied_and_passed_by_value() -> None:
    check_program(parse_program(
        "5 SUB show(t AS STRING) AS VOID\n6 PRINT t\n7 .ENDSUB\n" + _main(
            "20 DIM s AS STRING AS VAR\n30 DIM v AS STRING AS VAR\n40 v f= s\n"
            "50 DIM copy AS STRING AS VAR\n60 copy = v\n70 CALL show(v)\n80 PRINT v"
        )
    ))


def test_move_target_may_be_global_ref_param_or_field() -> None:
    check_program(parse_program(_BOX +
        "5 DIM g AS STRING AS VAR\n"
        "10 SUB fill(out AS STRING AS REF, box AS ENTITY AS Box AS REF) AS VOID\n"
        "20 DIM tmp AS STRING AS VAR\n30 tmp = \"x\"\n40 out m= tmp\n"
        "50 DIM tmp2 AS STRING AS VAR\n60 box.text m= tmp2\n"
        "70 DIM tmp3 AS STRING AS VAR\n80 g m= tmp3\n"
        "90 .ENDSUB\n"
        "100 SUB main AS PUBLIC AS VOID\n110 .ENDSUB\n120 CALL main\n130 END\n"
    ))


def test_move_inside_loop_of_loop_local_is_fine() -> None:
    check_program(parse_program(_main(
        "20 DIM i AS NUM AS LONG AS VAR\n30 FOR i = 1 TO 3\n40 DIM b AS STRING AS VAR\n"
        "50 DIM a AS STRING AS VAR\n60 a m= b\n70 PRINT a\n80 .ENDFOR"
    )))


# --- 语义：每条规则一个负例 ---

def test_rejects_value_types() -> None:
    expect_error(_main("20 DIM a AS NUM AS LONG AS VAR\n30 DIM b AS NUM AS LONG AS VAR\n40 a m= b"), "只适用于 STRING / SYMBOL / ERROR")
    expect_error(_main("20 DIM a AS HANDLE AS FILE AS VAR\n30 DIM b AS HANDLE AS FILE AS VAR\n40 a f= b"), "只适用于 STRING / SYMBOL / ERROR")


def test_rejects_arrays_and_promises() -> None:
    expect_error(_main("20 DIM a[2] AS STRING AS VAR\n30 DIM b[2] AS STRING AS VAR\n40 a m= b"), "只适用于 STRING / SYMBOL / ERROR")
    expect_error(
        "5 ASYNC SUB f() AS NUM AS LONG\n6 RETURN 1\n7 .ENDSUB\n" + _main(
            "20 DIM p AS PROMISE OF NUM AS LONG AS VAR\n30 DIM q AS PROMISE OF NUM AS LONG AS VAR\n40 p m= q"
        ),
        "只适用于 STRING / SYMBOL / ERROR",
    )


def test_rejects_entity_without_managed_fields() -> None:
    expect_error(
        "1 FOR ENTITY AS Pt\n2 DIM x AS NUM AS LONG AS VAR\n3 .ENDENTITY\n" + _main(
            "20 DIM a AS ENTITY AS Pt AS VAR\n30 DIM b AS ENTITY AS Pt AS VAR\n40 a m= b"
        ),
        "只适用于 STRING / SYMBOL / ERROR",
    )


def test_rejects_type_mismatch() -> None:
    expect_error(_main("20 DIM a AS STRING AS VAR\n30 DIM b AS SYMBOL AS VAR\n40 a m= b"), "两侧类型必须完全一致")


def test_rejects_expression_or_field_path_as_source() -> None:
    expect_error(_main('20 DIM a AS STRING AS VAR\n30 a m= "lit"'), "右侧必须是一个变量")
    expect_error(_BOX + _main("20 DIM a AS STRING AS VAR\n30 DIM b AS ENTITY AS Box AS VAR\n40 a m= b.text"), "右侧必须是一个变量")


def test_rejects_self_assign() -> None:
    expect_error(_main("20 DIM a AS STRING AS VAR\n30 a f= a"), "不能是同一个变量")
    expect_error(_main("20 DIM a AS STRING AS VAR\n30 a m= a"), "不能是同一个变量")


def test_rejects_read_after_move() -> None:
    # codegen 把源置成 NULL：再读就是 sa_print_string(NULL) 或 strcmp(NULL, ...)
    expect_error(_main("20 DIM a AS STRING AS VAR\n30 DIM b AS STRING AS VAR\n40 a m= b\n50 PRINT b"), "已被 m= 移走: b")


def test_rejects_field_write_after_move() -> None:
    expect_error(_BOX + _main(
        "20 DIM a AS ENTITY AS Box AS VAR\n30 DIM b AS ENTITY AS Box AS VAR\n40 a m= b\n50 b.text = \"x\""
    ), "已被 m= 移走: b")


def test_rejects_use_after_move_in_any_branch() -> None:
    # 任一分支移走就算死：运行期走不走那条分支编译器不知道，保守取并集
    expect_error(_main(
        "20 DIM a AS STRING AS VAR\n30 DIM b AS STRING AS VAR\n40 IF 1 THEN\n50 a m= b\n60 .ENDIF\n70 PRINT b"
    ), "已被 m= 移走: b")


def test_rejects_moving_outer_variable_inside_loop() -> None:
    # 第二轮迭代会读到已置空的 b
    expect_error(_main(
        "20 DIM b AS STRING AS VAR\n30 DIM i AS NUM AS LONG AS VAR\n40 FOR i = 1 TO 3\n"
        "50 DIM a AS STRING AS VAR\n60 a m= b\n70 .ENDFOR"
    ), "循环体内不能移走循环外声明的变量")
    expect_error(_main(
        "20 DIM b AS STRING AS VAR\n40 WHILE 0\n50 DIM a AS STRING AS VAR\n60 a m= b\n70 .ENDWHILE"
    ), "循环体内不能移走循环外声明的变量")


def test_rejects_global_ref_param_or_const_as_move_source() -> None:
    expect_error("5 DIM g AS STRING AS VAR\n" + _main("20 DIM a AS STRING AS VAR\n30 a m= g"), "不能是全局变量")
    expect_error(
        "5 SUB f(p AS STRING AS REF) AS VOID\n6 DIM a AS STRING AS VAR\n7 a m= p\n8 .ENDSUB\n" + _main(""),
        "不能是 AS REF 参数",
    )
    expect_error(_main('20 CONST k AS STRING = "x"\n30 DIM a AS STRING AS VAR\n40 a m= k'), "不能是 CONST")


def test_rejects_by_value_symbol_param_as_move_source_or_target() -> None:
    # SYMBOL / ERROR 按值参数只是调用方指针的浅拷贝，本帧不持有：移走它等于把调用方的树交出去，
    # 释放它（作为目标时的旧值）等于释放调用方的树。STRING 参数入口就 strdup 过，则允许。
    expect_error(
        "5 SUB f(p AS SYMBOL) AS VOID\n6 DIM a AS SYMBOL AS VAR\n7 a m= p\n8 .ENDSUB\n" + _main(""),
        "按值传入的 SYMBOL / ERROR 参数",
    )
    expect_error(
        "5 SUB f(p AS ERROR) AS VOID\n6 DIM a AS ERROR AS VAR\n7 p m= a\n8 .ENDSUB\n" + _main(""),
        "按值传入的 SYMBOL / ERROR 参数",
    )
    check_program(parse_program(
        "5 SUB f(p AS STRING) AS VOID\n6 DIM a AS STRING AS VAR\n7 a m= p\n8 .ENDSUB\n" + _main("")
    ))


def test_rejects_modifying_source_while_borrowed() -> None:
    # 源被改 = 旧串被 free，借用者手里的指针悬空
    body = "20 DIM s AS STRING AS VAR\n30 DIM v AS STRING AS VAR\n40 v f= s\n"
    expect_error(_main(body + '50 s = "z"'), "已被 f= 借出")
    expect_error(_main(body + "50 DIM w AS STRING AS VAR\n60 w m= s"), "已被 f= 借出")
    expect_error(
        "5 USE SYS.IO AS IO\n" + _main(body + '50 IO.INPUT "? ", s'),
        "已被 f= 借出",
    )
    expect_error(
        "5 SUB f(p AS STRING AS REF) AS VOID\n6 .ENDSUB\n" + _main(body + "50 CALL f(s)"),
        "已被 f= 借出",
    )
    expect_error(_main(body + "50 DIM p AS PTR TO STRING AS VAR\n60 p = @s"), "已被 f= 借出")


def test_rejects_writing_through_borrowed_variable() -> None:
    # 借用者不持有：写它会 free 掉源的串
    body = "20 DIM s AS STRING AS VAR\n30 DIM v AS STRING AS VAR\n40 v f= s\n"
    expect_error(_main(body + '50 v = "z"'), "借用来的变量是只读的")
    expect_error(_main(body + "50 DIM w AS STRING AS VAR\n60 w m= v"), "不持有所有权，不能被 m= 移走")
    expect_error(
        "5 SUB f(p AS STRING AS REF) AS VOID\n6 .ENDSUB\n" + _main(body + "50 CALL f(v)"),
        "借用来的变量是只读的",
    )
    expect_error(_BOX + _main(
        "20 DIM s AS ENTITY AS Box AS VAR\n30 DIM v AS ENTITY AS Box AS VAR\n40 v f= s\n50 v.text = \"z\""
    ), "借用来的变量是只读的")


def test_rejects_borrow_target_outside_current_block() -> None:
    # 借用靠「把目标从块尾清理登记摘掉」实现，登记按块静态生成：目标在外层块而借用在 IF 里，
    # 没走这个分支时目标自己的初始串就没人 free 了
    expect_error(_main(
        "20 DIM s AS STRING AS VAR\n30 DIM v AS STRING AS VAR\n40 IF 1 THEN\n50 v f= s\n60 .ENDIF"
    ), "同一个块里 DIM 的局部变量")


def test_rejects_global_as_borrow_source_and_field_as_borrow_target() -> None:
    expect_error("5 DIM g AS STRING AS VAR\n" + _main("20 DIM v AS STRING AS VAR\n30 v f= g"), "不能是全局变量")
    expect_error(_BOX + _main(
        "20 DIM s AS STRING AS VAR\n30 DIM b AS ENTITY AS Box AS VAR\n40 b.text f= s"
    ), "不能是字段路径")


def test_rejects_ownership_ops_in_sub_with_jumps() -> None:
    body = "20 DIM a AS STRING AS VAR\n30 DIM b AS STRING AS VAR\n40 a m= b\n"
    expect_error(_main(body + "50 GOTO ::done\n60 ::done"), "含 GOTO / GOSUB 的 SUB")
    expect_error(_main(body + "50 GOSUB ::sub1\n60 RETURN\n70 ::sub1\n80 RETURN"), "含 GOTO / GOSUB 的 SUB")


def test_plain_checks_still_apply_to_ownership_assign() -> None:
    expect_error(_main('20 CONST k AS STRING = "x"\n30 DIM a AS STRING AS VAR\n40 k m= a'), "不能给 CONST 赋值")
    expect_error(_main("20 DIM a AS STRING AS VAR\n30 a m= nope"), "变量未声明")


def test_diagnostics_collect_ownership_error_once_per_sub() -> None:
    from sonalgebraic.analysis.semantics import collect_program_diagnostics

    diagnostics = collect_program_diagnostics(parse_program(_main(
        "20 DIM a AS STRING AS VAR\n30 DIM b AS STRING AS VAR\n40 a m= b\n50 PRINT b\n60 PRINT b"
    )))
    messages = [d.message for d in diagnostics]
    assert sum("已被 m= 移走" in m for m in messages) == 1


# --- 生成 C ---

def test_c_move_is_pointer_handoff_and_null_source() -> None:
    c_text = compile_user_c(_main('20 DIM a AS STRING AS VAR\n30 DIM b AS STRING AS VAR\n40 b = "x"\n50 a m= b\n60 PRINT a'))
    assert "sa_a = sa_b;" in c_text
    assert "sa_b = NULL;" in c_text
    assert "sa_set_string(&sa_a, sa_b)" not in c_text


def test_c_borrow_target_is_dropped_from_block_cleanup() -> None:
    c_text = compile_user_c(_main("20 DIM s AS STRING AS VAR\n30 DIM v AS STRING AS VAR\n40 v f= s\n50 PRINT v"))
    body = c_text[c_text.find("static void sa_main"):]
    # 借用前的旧值要 free 一次（初始空串），之后块尾不能再 free
    assert body.count("free(sa_v);") == 1
    assert body.index("free(sa_v);") < body.index("sa_v = sa_s;")
    assert "free(sa_s);" in body


def test_c_entity_move_uses_struct_copy_and_memset() -> None:
    c_text = compile_user_c(_BOX + _main("20 DIM a AS ENTITY AS Box AS VAR\n30 DIM b AS ENTITY AS Box AS VAR\n40 a m= b"))
    assert "sa_a = sa_b;" in c_text
    assert "memset(&sa_b, 0, sizeof(sa_b));" in c_text


def test_c_symbol_and_error_move() -> None:
    c_text = compile_user_c(_main(
        "20 DIM a AS SYMBOL AS VAR\n30 DIM b AS SYMBOL AS VAR\n40 a m= b\n"
        "50 DIM e AS ERROR AS VAR\n60 DIM f AS ERROR AS VAR\n70 e m= f"
    ))
    assert "sa_symbol_free(sa_a);\n    sa_a = sa_b;\n    sa_b = NULL;" in c_text
    assert 'sa_error_clear(&sa_e);\n    sa_e = sa_f;\n    sa_f = (SaError){0, "ERR_NONE", NULL, 0, NULL};' in c_text


def test_c_landing_pad_after_borrow_excludes_target() -> None:
    # 借用之后的 per-call landing pad 不能 free 借用者，否则异常穿透时源的串被双重释放
    c_text = compile_user_c(
        "5 SUB f() AS VOID\n6 .ENDSUB\n" + _main(
            "20 DIM s AS STRING AS VAR\n30 DIM v AS STRING AS VAR\n40 CALL f()\n50 v f= s\n60 CALL f()"
        )
    )
    body = c_text[c_text.find("static void sa_main"):]
    before, after = body.split("sa_v = sa_s;")
    assert "free(sa_v);" in before  # 借用前的 landing pad 仍然释放 v 自己的初始串
    assert "free(sa_v);" not in after


# --- 端到端：输出 + 零泄漏 ---

# 两次借用都放在自己的 IF 块里：借用者死在块尾、源随之解冻，最后才能改 a。
_E2E_STRING = _main(
    "20 DIM a AS STRING AS VAR\n30 DIM b AS STRING AS VAR\n40 b = \"moved\"\n50 a m= b\n60 PRINT a\n"
    "70 IF 1 THEN\n80 DIM v AS STRING AS VAR\n90 v f= a\n100 PRINT v\n110 .ENDIF\n"
    "120 IF 1 THEN\n130 DIM w AS STRING AS VAR\n140 w f= a\n150 PRINT w\n160 .ENDIF\n"
    "170 a = \"changed\"\n180 PRINT a"
)

_E2E_SYMBOL = _main(
    "20 DIM x AS NUM AS LONG AS VAR\n30 DIM s1 AS SYMBOL AS VAR\n40 DIM s2 AS SYMBOL AS VAR\n"
    "50 s1 = x * 2 + 1\n60 s2 m= s1\n70 PRINT s2\n"
    "80 DIM v AS SYMBOL AS VAR\n90 v f= s2\n100 PRINT v"
)

_E2E_ENTITY = _BOX + _main(
    "20 DIM a AS ENTITY AS Box AS VAR\n30 DIM b AS ENTITY AS Box AS VAR\n40 b.text = \"inner\"\n50 b.n = 7\n"
    "60 a m= b\n70 PRINT a.text\n80 PRINT a.n\n"
    "90 DIM v AS ENTITY AS Box AS VAR\n100 v f= a\n110 PRINT v.text"
)

_E2E_ASYNC = (
    "10 ASYNC SUB work() AS STRING\n20 DIM s AS STRING AS VAR\n30 DIM t AS STRING AS VAR\n"
    "40 s = \"frame\"\n50 t m= s\n60 RETURN t\n70 .ENDSUB\n"
    "80 SUB main AS PUBLIC AS VOID\n90 DIM r AS STRING AS VAR\n100 r = SYNC work()\n110 PRINT r\n120 .ENDSUB\n"
    "130 CALL main\n140 END\n"
)

_E2E_THROW = (
    "1 SUB boom() AS VOID\n2 THROW NEW ERR_RUNTIME, \"boom\"\n3 .ENDSUB\n" + _main(
        "20 DIM s AS STRING AS VAR\n30 s = \"kept\"\n40 DIM v AS STRING AS VAR\n50 v f= s\n"
        "60 DIM trap AS ERROR AS VAR\n70 TRY CALL boom() TRACEBACK ERROR AS trap\n80 CATCH ERR_ANY AS e\n90 PRINT e\n100 .ENDTRY\n"
        "110 PRINT v"
    )
)


@pytest.mark.e2e
@requires_gcc
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (_E2E_STRING, ["moved", "moved", "moved", "changed"]),
        (_E2E_SYMBOL, ["((x * 2) + 1)", "((x * 2) + 1)"]),
        (_E2E_ENTITY, ["inner", "7", "inner"]),
        (_E2E_ASYNC, ["frame"]),
        (_E2E_THROW, ["boom", "kept"]),
    ],
    ids=["string", "symbol", "entity", "async", "throw"],
)
def test_c_backend_runs_without_leaks(source: str, expected: list[str]) -> None:
    with build_temp("own-", "ownership-tests") as temp:
        result = _build_and_run(source, temp, "own", opt="-O0", leak=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == expected
    assert "SA_LIVE=0" in result.stderr


# --- native ---

def test_native_ir_move_stores_null_and_borrow_skips_free() -> None:
    ir = generate_native_llvm_ir(check_program(parse_program(_main(
        "20 DIM a AS STRING AS VAR\n30 DIM b AS STRING AS VAR\n40 a m= b\n50 DIM v AS STRING AS VAR\n60 v f= a\n70 PRINT v"
    ))))
    assert "store ptr null, ptr %sa_b.addr" in ir
    # v 只在借用前 free 过一次（初始空串），函数尾不再 free
    tail = ir[ir.find("store ptr null, ptr %sa_b.addr"):]
    loads_of_v = [line for line in tail.splitlines() if "load ptr, ptr %sa_v.addr" in line]
    frees = tail.count("call void @free(")
    # 尾部清理：a、b 各一次 + 借用前 v 一次 = 3，借用后 v 不再释放
    assert frees == 3, tail
    assert loads_of_v


def test_native_ir_entity_move_zeroes_source() -> None:
    ir = generate_native_llvm_ir(check_program(parse_program(_BOX + _main(
        "20 DIM a AS ENTITY AS Box AS VAR\n30 DIM b AS ENTITY AS Box AS VAR\n40 a m= b"
    ))))
    assert "store %SaEntity_box zeroinitializer, ptr %sa_b.addr" in ir


@pytest.mark.e2e
@requires_clang_lld
@pytest.mark.parametrize("source", [_E2E_STRING, _E2E_SYMBOL, _E2E_ENTITY], ids=["string", "symbol", "entity"])
def test_native_backend_runs_without_leaks(source: str) -> None:
    with build_temp("own-native-", "ownership-tests") as temp:
        assert _native_live_allocs(Path(temp), source) == 0
