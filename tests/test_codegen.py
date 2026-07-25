"""代码生成相关测试：解析 + 语义检查后断言生成的 C 源码。"""
from __future__ import annotations

from conftest import compile_c


def test_valid_program_generates_c() -> None:
    source = """10 DIM counter AS NUM AS LONG AS VAR
20 DIM message AS STRING AS VAR
30 SUB main AS PUBLIC AS VOID
40 counter = 1
50 message = "hi"
60 PRINT F"{message}: {counter}"
70 .ENDSUB
80 CALL main
90 END
"""
    c = compile_c(source)
    assert "static void sa_main(void)" in c
    assert "sa_print_string" in c
    assert "/* SA 60: PRINT F\"{message}: {counter}\" */" in c


def test_non_void_sub_with_params_generates_c() -> None:
    source = """10 SUB area(width AS NUM AS DOUBLE, height AS NUM AS DOUBLE) AS NUM AS DOUBLE
20 RETURN width * height
30 .ENDSUB
40 DIM result AS NUM AS DOUBLE AS VAR
50 SUB main AS PUBLIC AS VOID
60 result = CALL area(3.0, 4.0)
70 PRINT result
80 .ENDSUB
90 CALL main
100 END
"""
    c = compile_c(source)
    assert "static double sa_area(double sa_width, double sa_height)" in c
    assert "sa_result = sa_area(3.0, 4.0);" in c


def test_ref_param_generates_pointer_call() -> None:
    source = """10 SUB bump(value AS NUM AS LONG AS REF) AS VOID
20 value = value + 1
30 .ENDSUB
40 DIM x AS NUM AS LONG AS VAR
50 SUB main AS PUBLIC AS VOID
60 x = 1
70 CALL bump(x)
80 PRINT x
90 .ENDSUB
100 CALL main
110 END
"""
    c = compile_c(source)
    assert "static void sa_bump(long long* sa_value)" in c
    assert "(*sa_value) = ((*sa_value) + 1);" in c
    assert "sa_bump(&(sa_x));" in c


def test_ref_param_accepts_entity_field_path() -> None:
    source = """10 FOR ENTITY AS Position
20 DIM x AS NUM AS LONG AS VAR
30 .ENDENTITY
40 FOR ENTITY AS Hero
50 DIM pos AS ENTITY AS Position AS VAR
60 .ENDENTITY
70 SUB bump(value AS NUM AS LONG AS REF) AS VOID
80 value = value + 1
90 .ENDSUB
100 SUB main AS PUBLIC AS VOID
110 DIM hero AS ENTITY AS Hero AS VAR
120 CALL bump(hero.pos.x)
130 .ENDSUB
140 CALL main
150 END
"""
    c = compile_c(source)
    assert "sa_bump(&(sa_hero.pos.x));" in c
    assert "sa_hero_pos_x" not in c


def test_entity_fields_generate_struct_access() -> None:
    source = """10 FOR ENTITY AS Vector2D
20 DIM x AS NUM AS DOUBLE AS VAR
30 DIM y AS NUM AS DOUBLE AS VAR
40 .ENDENTITY
50 SUB move(point AS ENTITY AS Vector2D AS REF) AS VOID
60 point.x = point.x + 1.5
70 .ENDSUB
80 SUB main AS PUBLIC AS VOID
90 DIM hero AS ENTITY AS Vector2D AS VAR
100 hero.x = 2.0
110 hero.y = 3.0
120 CALL move(hero)
130 PRINT F"x={hero.x}, y={hero.y}"
140 .ENDSUB
150 CALL main
160 END
"""
    c = compile_c(source)
    assert "typedef struct" in c
    assert "double x;" in c
    assert "static void sa_move(SaEntity_vector2d* sa_point)" in c
    assert "(*sa_point).x = ((*sa_point).x + 1.5);" in c


def test_entity_array_field_keeps_dimension() -> None:
    # 回归：实体内定长数组字段必须保留 [N] 维度，否则生成标量、.field[i] 下标访问编译失败
    source = """10 FOR ENTITY AS Tensor
20 DIM data[3] AS NUM AS DOUBLE AS VAR
30 DIM label AS STRING AS VAR
40 .ENDENTITY
50 SUB main AS PUBLIC AS VOID
60 DIM t AS ENTITY AS Tensor AS VAR
70 t.data[0] = 1.5
80 t.data[1] = t.data[0] + 2.0
90 PRINT F"d0={t.data[0]} d1={t.data[1]}"
100 .ENDSUB
110 CALL main
120 END
"""
    c = compile_c(source)
    assert "double data[3];" in c
    assert "sa_t.data[0] = 1.5;" in c


def test_entity_string_fields_are_managed() -> None:
    source = '''10 FOR ENTITY AS Profile
20 DIM name AS STRING AS VAR
30 DIM score AS NUM AS LONG AS VAR
40 .ENDENTITY
50 DIM global_profile AS ENTITY AS Profile AS VAR
60 SUB rename(item AS ENTITY AS Profile) AS VOID
70 item.name = "copy"
80 .ENDSUB
90 SUB main AS PUBLIC AS VOID
100 DIM first AS ENTITY AS Profile AS VAR
110 DIM second AS ENTITY AS Profile AS VAR
120 first.name = "LANS"
130 second = first
140 CALL rename(second)
150 .ENDSUB
160 CALL main
170 END
'''
    c = compile_c(source)
    assert "sa_global_profile.name = sa_strdup(\"\");" in c
    assert "sa_first.name = sa_strdup(\"\");" in c
    assert "sa_second.name = sa_strdup(\"\");" in c
    assert "sa_set_string(&sa_second.name, sa_first.name);" in c
    assert "free(sa_first.name);" in c
    assert "free(sa_second.name);" in c
    assert "free(sa_global_profile.name);" in c
    assert "SaEntity_profile sa_tmp_" in c
    assert "sa_set_string(&sa_item.name" in c


def test_nested_entity_string_fields_are_managed() -> None:
    source = '''10 FOR ENTITY AS NameBox
20 DIM text AS STRING AS VAR
30 .ENDENTITY
40 FOR ENTITY AS Profile
50 DIM name AS ENTITY AS NameBox AS VAR
60 .ENDENTITY
70 SUB main AS PUBLIC AS VOID
80 DIM first AS ENTITY AS Profile AS VAR
90 DIM second AS ENTITY AS Profile AS VAR
100 first.name.text = "LANS"
110 second = first
120 .ENDSUB
130 CALL main
140 END
'''
    c = compile_c(source)
    assert "sa_first.name.text = sa_strdup(\"\");" in c
    assert "sa_second.name.text = sa_strdup(\"\");" in c
    assert "sa_set_string(&sa_second.name.text, sa_first.name.text);" in c
    assert "free(sa_first.name.text);" in c
    assert "free(sa_second.name.text);" in c


def test_entity_symbol_fields_are_not_deep_copied_without_clone_support() -> None:
    source = '''10 FOR ENTITY AS FormulaBox
20 DIM expr AS SYMBOL AS VAR
30 .ENDENTITY
40 SUB main AS PUBLIC AS VOID
50 DIM first AS ENTITY AS FormulaBox AS VAR
60 DIM second AS ENTITY AS FormulaBox AS VAR
70 second = first
80 .ENDSUB
90 CALL main
100 END
'''
    c = compile_c(source)
    assert "sa_symbol_free(sa_first.expr);" not in c
    assert "sa_symbol_free(sa_second.expr);" not in c


def test_try_catch_throw_generates_setjmp_flow() -> None:
    source = """10 DIM trap AS ERROR AS VAR
20 SUB risky AS VOID
30 THROW NEW ERR_TEST, "boom"
40 .ENDSUB
50 SUB main AS PUBLIC AS VOID
60 TRY CALL risky TRACEBACK ERROR AS trap
70 CATCH ERR_TEST AS e
80 PRINT F"caught {e}"
90 .ENDTRY
100 .ENDSUB
110 CALL main
120 END
"""
    c = compile_c(source)
    assert "SA_SETJMP(sa_try_stack" in c
    assert "sa_raise_new(\"ERR_TEST\"" in c
    assert "sa_throw_dispatch();" in c
    assert "sa_set_error(&sa_e, &sa_current_error);" in c


def test_throw_frees_local_resources_before_dispatch() -> None:
    # THROW 逃逸前必须清理当前 SUB 的局部托管资源，否则 longjmp 跳走泄漏
    source = """10 DIM trap AS ERROR AS VAR
20 SUB leaky AS PRIVATE AS VOID
30 DIM msg AS STRING AS VAR
40 msg = "leak me"
50 THROW NEW ERR_BOOM, "boom"
60 .ENDSUB
70 SUB main AS PUBLIC AS VOID
80 TRY CALL leaky TRACEBACK ERROR AS trap
90 CATCH ERR_ANY AS e
100 PRINT F"caught {e}"
110 .ENDTRY
120 .ENDSUB
130 CALL main
140 END
"""
    c = compile_c(source)
    # raise -> free(局部) -> dispatch 顺序
    raise_idx = c.index("sa_raise_new(\"ERR_BOOM\"")
    free_idx = c.index("free(sa_msg);", raise_idx)
    dispatch_idx = c.index("sa_throw_dispatch();", raise_idx)
    assert raise_idx < free_idx < dispatch_idx
    # 程序结束清理运行时全局错误对象
    assert "sa_error_clear(&sa_current_error);" in c


def test_uncaught_dispatch_clears_error_before_exit() -> None:
    # uncaught 分支走 exit(1) 不经 sa_program_end，必须在退出前 sa_error_clear，否则错误 message 泄漏
    source = """10 SUB main AS PUBLIC AS VOID
20 THROW NEW ERR_X, "boom"
30 .ENDSUB
40 CALL main
50 END
"""
    c = compile_c(source)
    # 定位 uncaught 分支：打印 "Uncaught" 之后、exit(1) 之前必须有 sa_error_clear
    uncaught_idx = c.index("Uncaught SonAlgebraic error")
    exit_idx = c.index("exit(1);", uncaught_idx)
    clear_idx = c.index("sa_error_clear(&sa_current_error);", uncaught_idx)
    assert uncaught_idx < clear_idx < exit_idx



def test_gosub_generates_local_return_stack() -> None:
    source = """10 SUB main AS PUBLIC AS VOID
20 GOSUB ::helper
30 PRINT "back"
40 RETURN
50 ::helper
60 PRINT "in helper"
70 RETURN
80 .ENDSUB
90 CALL main
100 END
"""
    c = compile_c(source)
    assert "int sa_gosub_stack[64];" in c
    assert "switch (sa_gosub_stack[--sa_gosub_top])" in c
    assert "case 20: goto sa_gosub_return_20;" in c


def test_symbol_assignment_generates_symbol_tree() -> None:
    source = """10 DIM a AS NUM AS LONG AS VAR
20 DIM expr AS SYMBOL AS VAR
30 SUB main AS PUBLIC AS VOID
40 a = 7
50 expr = a + 2
60 PRINT expr
70 .ENDSUB
80 CALL main
90 END
"""
    c = compile_c(source)
    assert "SaSymbol sa_expr = NULL;" in c
    # 符号赋值必须「先把新树求值到临时量 -> 释放旧树 -> 接管」，规避 wave = f(wave) 的 use-after-free。
    assert "SaSymbol sa_tmp_1 = sa_symbol_op('+', sa_symbol_var(\"a\"), sa_symbol_const(\"2\"));" in c
    free_idx = c.index("sa_symbol_free(sa_expr);")
    build_idx = c.index("SaSymbol sa_tmp_1 = sa_symbol_op('+'")
    adopt_idx = c.index("sa_expr = sa_tmp_1;")
    assert build_idx < free_idx < adopt_idx


def test_symbol_self_referential_assignment_is_use_after_free_safe() -> None:
    # 回归：RHS 引用 LHS 自身（wave = wave * t + ...）时，必须先克隆旧树到临时量再释放，
    # 否则 sa_symbol_clone 会克隆已被 free 的指针，运行时段错误 / <null-symbol>。
    source = """10 SUB mutate(w AS SYMBOL AS REF) AS VOID
20 DIM t AS NUM AS DOUBLE AS VAR
30 w = w * t + DERIV(w, "t")
40 .ENDSUB
50 SUB main AS PUBLIC AS VOID
60 DIM t AS NUM AS DOUBLE AS VAR
70 DIM wave AS SYMBOL AS VAR
80 wave = t * t
90 CALL mutate(wave)
100 .ENDSUB
110 CALL main
120 END
"""
    c = compile_c(source)
    # mutate 内的自引用赋值：clone((*sa_w)) 必须出现在 sa_symbol_free((*sa_w)) 之前
    build_idx = c.index("sa_symbol_clone((*sa_w))")
    free_idx = c.index("sa_symbol_free((*sa_w));")
    assert build_idx < free_idx


def test_use_sys_math_generates_pow_call() -> None:
    source = """10 USE SYS.MATH AS ALG
20 DIM area AS NUM AS DOUBLE AS VAR
30 SUB main AS PUBLIC AS VOID
40 area = ALG.PI * ALG.POW(2.0, 2.0)
50 PRINT area
60 .ENDSUB
70 CALL main
80 END
"""
    c = compile_c(source)
    assert "pow(2.0, 2.0)" in c
    assert "3.14159265358979323846" in c


def test_power_operator_generates_pow_call() -> None:
    source = """10 DIM n AS NUM AS DOUBLE AS VAR
20 SUB main AS PUBLIC AS VOID
30 n = 2.0 ** 8.0
40 PRINT n
50 .ENDSUB
60 CALL main
70 END
"""
    c = compile_c(source)
    assert "pow(2.0, 8.0)" in c


def test_usec_declare_c_generates_include_and_call() -> None:
    source = '''10 USEC "stdio.h" AS STDIO
20 DECLARE C SUB STDIO.puts(s AS STRING) AS NUM AS LONG
30 SUB main AS PUBLIC AS VOID
40 CALL STDIO.puts("hi")
50 .ENDSUB
60 CALL main
70 END
'''
    c = compile_c(source)
    assert '#include "stdio.h"' in c
    assert "puts(" in c


def test_cptr_type_is_supported() -> None:
    source = '''10 DIM handle AS CPTR AS VAR
20 SUB main AS PUBLIC AS VOID
30 handle = 0
40 PRINT handle
50 .ENDSUB
60 CALL main
70 END
'''
    c = compile_c(source)
    assert "void* sa_handle" in c


def test_ptr_to_generates_typed_pointer() -> None:
    source = '''10 DIM p AS PTR TO NUM AS LONG AS VAR
20 SUB main AS PUBLIC AS VOID
30 p = 0
40 .ENDSUB
50 CALL main
60 END
'''
    c = compile_c(source)
    assert "long long* sa_p" in c


def test_deref_and_address_of() -> None:
    source = '''10 DIM x AS NUM AS LONG AS VAR
20 DIM p AS PTR TO NUM AS LONG AS VAR
30 SUB main AS PUBLIC AS VOID
40 x = 42
50 p = @x
60 ^p = 100
70 PRINT x
80 .ENDSUB
90 CALL main
100 END
'''
    c = compile_c(source)
    assert "sa_p = (&sa_x);" in c
    assert "(*(" in c and ")) = 100;" in c


def test_cast_between_ptr_and_cptr() -> None:
    source = '''10 DIM raw AS CPTR AS VAR
20 DIM p AS PTR TO NUM AS LONG AS VAR
30 SUB main AS PUBLIC AS VOID
40 raw = 0
50 p = CAST PTR TO NUM AS LONG raw
60 .ENDSUB
70 CALL main
80 END
'''
    c = compile_c(source)
    assert "(long long*)(sa_raw)" in c


def test_local_resources_are_cleaned_on_return_and_sub_end() -> None:
    source = '''10 SUB compute AS NUM AS LONG
20 DIM local_text AS STRING AS VAR
30 DIM local_symbol AS SYMBOL AS VAR
40 DIM local_error AS ERROR AS VAR
50 RETURN 7
60 .ENDSUB
70 SUB main AS PUBLIC AS VOID
80 DIM tail_text AS STRING AS VAR
90 DIM tail_symbol AS SYMBOL AS VAR
100 DIM tail_error AS ERROR AS VAR
110 .ENDSUB
120 CALL main
130 END
'''
    c = compile_c(source)
    assert "free(sa_local_text);" in c
    assert "sa_symbol_free(sa_local_symbol);" in c
    assert "sa_error_clear(&sa_local_error);" in c
    assert "free(sa_tail_text);" in c
    assert "sa_symbol_free(sa_tail_symbol);" in c
    assert "sa_error_clear(&sa_tail_error);" in c
