"""异步异常的原错误、帧所有权和异常后调度回归。"""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from conftest import build_temp, compile_c, requires_gcc
from test_coroutines import _LEAK_SHIM


def _run(source: str, driver: str = "", opt: str = "-O2"):
    text = compile_c(source)
    if driver:
        text = text.replace("int main(void) {", "int sa_unused_main(void) {", 1)
        text += "\n" + driver
    text = _LEAK_SHIM + "\n#include <assert.h>\n" + text
    with build_temp("async-errors-", subdir="coroutine-tests") as temp:
        c = Path(temp) / "case.c"
        exe = Path(temp) / ("case.exe" if sys.platform == "win32" else "case")
        c.write_text(text, encoding="utf-8")
        result = subprocess.run([shutil.which("gcc"), str(c), opt, "-std=c11", "-o", str(exe), "-lm"], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        return subprocess.run([str(exe)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)


_SOURCE = '''10 ASYNC SUB leaf(s AS STRING) AS VOID
20 DIM local AS STRING AS VAR
30 local = "owned"
40 THROW NEW ERR_ORIGINAL, "original message"
50 .ENDSUB
60 ASYNC SUB middle() AS VOID
70 DIM local AS STRING AS VAR
80 local = "parent"
90 AWAIT leaf("argument")
100 .ENDSUB
110 ASYNC SUB outer() AS VOID
120 AWAIT middle()
130 .ENDSUB
140 SUB main AS PUBLIC AS VOID
150 SYNC outer()
160 .ENDSUB
170 CALL main
180 END
'''


@requires_gcc
def test_uncaught_preserves_original_error():
    proc = _run(_SOURCE)
    assert proc.returncode == 1
    assert "ERR_ORIGINAL at line 40: original message" in proc.stderr


@requires_gcc
def test_failed_child_releases_frames_before_global_shutdown():
    proc = _run(_SOURCE, '''
int main(void) {
    SaHandle p = sa_outer_start();
    sa_event_loop_run_until(p);
    sa_promise_release(p);
    sa_error_clear(&sa_current_error);
    assert(sa__live == 0);
    return 0;
}
''')
    assert proc.returncode == 0, proc.stderr


@requires_gcc
@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_repeated_failure_preserves_all_error_fields_and_keeps_scheduling(opt):
    source = _SOURCE.replace("140 SUB main", "131 ASYNC SUB good() AS STRING\n132 RETURN \"still running\"\n133 .ENDSUB\n140 SUB main")
    proc = _run(source, '''
int main(void) {
    for (int i = 0; i < 600; ++i) {
        SaHandle p = sa_outer_start();
        sa_event_loop_run_until(p);
        sa_try_top++;
        if (SA_SETJMP(sa_try_stack[sa_try_top - 1].env) == 0) {
            sa_promise_take_void(p);
            assert(0);
        } else {
            sa_try_top--;
            assert(strcmp(sa_current_error.type, "ERR_ORIGINAL") == 0);
            assert(strcmp(sa_current_error.message, "original message") == 0);
            assert(sa_current_error.err_code == sa_error_code("ERR_ORIGINAL"));
            assert(sa_current_error.line_number == 40);
            assert(strcmp(sa_current_error.sub_name, "leaf") == 0);
            sa_error_clear(&sa_current_error);
        }
        assert(sa_async_slot(p) == NULL);
        assert(sa_try_top == 0);
        SaHandle next = sa_good_start();
        sa_event_loop_run_until(next);
        char* result = sa_promise_take_str(next);
        assert(strcmp(result, "still running") == 0);
        free(result);
        sa_promise_release(next);
        assert(sa__live == 0);
    }
    return 0;
}
''', opt)
    assert proc.returncode == 0, proc.stderr


@requires_gcc
@pytest.mark.parametrize("opt", ["-O0", "-O2"])
@pytest.mark.parametrize("finish", ["return", "throw", "child", "cancel"])
def test_block_and_temporary_resources_release_once(finish, opt):
    tail = {
        "return": '100 RETURN\n',
        "throw": '100 THROW NEW ERR_BLOCK, F"failure {7}"\n',
        "child": '100 AWAIT fail()\n',
        "cancel": '100 AWAIT tick(F"child {9}")\n',
    }[finish]
    source = '''10 ASYNC SUB tick(s AS STRING) AS VOID
20 RETURN
30 .ENDSUB
40 ASYNC SUB worker(s AS STRING) AS VOID
50 IF TRUE THEN
60 DIM block AS STRING AS VAR
70 block = F"block {s}"
80 AWAIT tick(F"temporary {s}")
90 PRINT block
''' + tail + '''110 .ENDIF
120 .ENDSUB
130 ASYNC SUB fail() AS VOID
140 THROW NEW ERR_CHILD, "child failure"
150 .ENDSUB
160 SUB main AS PUBLIC AS VOID
170 .ENDSUB
180 CALL main
190 END
'''
    action = '''
        sa_ready_head = (sa_ready_head + 1) % SA_ASYNC_SLOT_COUNT;
        SaCoroBase* frame = sa_async_slot(p)->coro;
        frame->resume(frame);
        SaHandle child = frame->awaited;
        assert(child && sa_async_slot(child)->waiter == p);
        sa_promise_release(p);
        assert(sa_async_slot(child) == NULL);
    ''' if finish == "cancel" else '''
        sa_event_loop_run_until(p);
        assert(sa_async_slot(p)->coro == NULL);
        sa_promise_release(p);
    '''
    proc = _run(source, '''int main(void) {
        SaHandle p = sa_worker_start("argument");
    ''' + action + '''
        sa_error_clear(&sa_current_error);
        assert(sa__live == 0);
        assert(sa_try_top == 0);
        return 0;
    }''', opt)
    assert proc.returncode == 0, proc.stderr


@requires_gcc
@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_error_variable_and_throwing_expression_temporaries(opt):
    source = '''10 SUB fail() AS STRING
20 THROW NEW ERR_EXPR, "expression failed"
30 RETURN "unreachable"
40 .ENDSUB
50 ASYNC SUB worker(original AS ERROR) AS VOID
60 DIM local AS ERROR AS VAR
70 REM 错误参数在启动时已保存快照
80 IF TRUE THEN
90 DIM s AS STRING AS VAR
100 s = F"allocated before {fail()}"
110 .ENDIF
120 THROW original
130 .ENDSUB
140 SUB main AS PUBLIC AS VOID
150 .ENDSUB
160 CALL main
170 END
'''
    for body, expected in [(source, "ERR_EXPR"), (source.replace('100 s = F"allocated before {fail()}"', '100 s = "value"'), "ERR_SAVED")]:
        proc = _run(body, '''int main(void) {
            SaError original = {77, "ERR_SAVED", sa_strdup("saved"), 321, "original_sub"};
            SaHandle p = sa_worker_start(original);
            sa_error_clear(&original);
            sa_event_loop_run_until(p);
            SaAsyncSlot* slot = sa_async_slot(p);
            assert(strcmp(slot->error.type, "''' + expected + '''") == 0);
            if (slot->error.err_code == 77) {
                assert(slot->error.line_number == 321);
                assert(strcmp(slot->error.sub_name, "original_sub") == 0);
            }
            sa_promise_release(p);
            assert(sa__live == 0);
            return 0;
        }''', opt)
        assert proc.returncode == 0, proc.stderr


@requires_gcc
def test_language_catch_original_type_then_sync_again():
    source = _SOURCE[:_SOURCE.index("140 SUB main")] + '''140 SUB attempt() AS VOID
150 SYNC outer()
160 .ENDSUB
170 SUB main AS PUBLIC AS VOID
180 DIM trap AS ERROR AS VAR
190 DIM i AS NUM AS LONG AS VAR
200 FOR i = 1 TO 300
210 TRY CALL attempt() TRACEBACK ERROR AS trap
220 CATCH ERR_ORIGINAL AS caught
230 PRINT caught
240 .ENDTRY
250 .ENDFOR
260 .ENDSUB
270 CALL main
280 END
'''
    proc = _run(source)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["original message"] * 300


@requires_gcc
@pytest.mark.parametrize("reject_child", [False, True])
def test_cancel_after_child_settles_and_duplicate_reject(reject_child):
    source = _SOURCE.replace('40 THROW NEW ERR_ORIGINAL, "original message"', "40 RETURN") if not reject_child else _SOURCE
    proc = _run(source, '''int main(void) {
        SaHandle p = sa_middle_start();
        sa_ready_head = (sa_ready_head + 1) % SA_ASYNC_SLOT_COUNT;
        SaCoroBase* parent = sa_async_slot(p)->coro;
        parent->resume(parent);
        SaHandle child = parent->awaited;
        sa_event_loop_run_until(child);
        assert(sa_async_slot(p)->status == SA_PROMISE_PENDING);
        assert(sa_async_slot(child)->coro == NULL);
        sa_promise_release(p);
        assert(sa_async_slot(child) == NULL);
        assert(sa__live == 0);
        SaHandle failed = sa_promise_alloc(NULL);
        sa_promise_reject(failed, "first");
        long live = sa__live;
        sa_promise_reject(failed, "second");
        assert(sa__live == live);
        assert(strcmp(sa_async_slot(failed)->error.message, "first") == 0);
        sa_promise_release(failed);
        assert(sa__live == 0);
        return 0;
    }''')
    assert proc.returncode == 0, proc.stderr


@requires_gcc
def test_sync_argument_temporary_is_released_before_failure():
    source = _SOURCE[:_SOURCE.index("140 SUB main")] + '''140 SUB attempt() AS VOID
150 SYNC leaf(F"temporary {7}")
160 .ENDSUB
170 SUB main AS PUBLIC AS VOID
180 .ENDSUB
190 CALL main
200 END
'''
    proc = _run(source, '''int main(void) {
        sa_try_top++;
        if (SA_SETJMP(sa_try_stack[sa_try_top - 1].env) == 0) {
            sa_attempt();
            assert(0);
        } else {
            sa_try_top--;
            sa_error_clear(&sa_current_error);
        }
        assert(sa__live == 0);
        return 0;
    }''')
    assert proc.returncode == 0, proc.stderr


@requires_gcc
def test_symbol_local_adoption_and_borrowed_string_cleanup():
    source = '''10 ASYNC SUB worker() AS VOID
20 DIM tree AS SYMBOL AS VAR
30 DIM owner AS STRING AS VAR
40 DIM borrowed AS STRING AS VAR
50 tree = 2 * 3 + 1
60 owner = "owned once"
70 borrowed f= owner
80 THROW NEW ERR_LOCAL, "failed"
90 .ENDSUB
100 SUB main AS PUBLIC AS VOID
110 .ENDSUB
120 CALL main
130 END
'''
    proc = _run(source, '''int main(void) {
        SaHandle p = sa_worker_start();
        sa_event_loop_run_until(p);
        sa_promise_release(p);
        assert(sa__live == 0);
        return 0;
    }''')
    assert proc.returncode == 0, proc.stderr


def _entity_async_source(action: str) -> str:
    body = '''
FOR ENTITY AS Box
DIM tree AS SYMBOL AS VAR
DIM text AS STRING AS VAR
.ENDENTITY
FOR ENTITY AS Nest
DIM box AS ENTITY AS Box AS VAR
.ENDENTITY
SUB make() AS ENTITY AS Nest
DIM value AS ENTITY AS Nest AS VAR
value.box.tree = 2 * 3 + 1
value.box.text = "snapshot"
RETURN value
.ENDSUB
SUB forward() AS ENTITY AS Nest
RETURN make()
.ENDSUB
SUB fail(value AS ENTITY AS Nest) AS VOID
PRINT value.box.tree
THROW NEW ERR_ENTITY, "entity failure"
.ENDSUB
ASYNC SUB child(value AS ENTITY AS Nest) AS VOID
PRINT value.box.tree
RETURN
.ENDSUB
ASYNC SUB tick() AS VOID
RETURN
.ENDSUB
ASYNC SUB worker(value AS ENTITY AS Nest) AS STRING
DIM local AS ENTITY AS Nest AS VAR = forward()
DIM copy AS ENTITY AS Nest AS VAR
copy = value
copy = copy
AWAIT tick()
PRINT local.box.tree
PRINT copy.box.tree
''' + action + '''
RETURN local.box.text
.ENDSUB
SUB main AS PUBLIC AS VOID
.ENDSUB
CALL main
END
'''
    return "\n".join(f"{i * 10} {line.strip()}" for i, line in enumerate(body.strip().splitlines(), 1)) + "\n"


@requires_gcc
@pytest.mark.parametrize("opt", ["-O0", "-O2"])
@pytest.mark.parametrize("action", ["AWAIT child(forward())", "CALL fail(forward())", "AWAIT child(local)"])
def test_entity_symbol_async_temporary_and_return_adoption(action, opt):
    proc = _run(_entity_async_source(action), '''int main(void) {
        for (int i = 0; i < 20; ++i) {
            SaEntity_nest original = sa_make();
            SaHandle p = sa_worker_start(original);
            sa_symbol_free(original.box.tree);
            free(original.box.text);
            sa_event_loop_run_until(p);
            SaAsyncSlot* slot = sa_async_slot(p);
            assert(slot->coro == NULL);
            if (slot->status == SA_PROMISE_FULFILLED) {
                char* result = sa_promise_take_str(p);
                assert(strcmp(result, "snapshot") == 0);
                free(result);
            } else {
                assert(strcmp(slot->error.type, "ERR_ENTITY") == 0);
            }
            sa_promise_release(p);
            assert(sa__live == 0);
        }
        return 0;
    }''', opt)
    assert proc.returncode == 0, proc.stderr
    assert "((2 * 3) + 1)" in proc.stdout


@requires_gcc
@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_entity_symbol_temporary_survives_suspend_and_cancel(opt):
    proc = _run(_entity_async_source("AWAIT child(forward())"), '''int main(void) {
        SaEntity_nest original = sa_make();
        SaHandle p = sa_worker_start(original);
        sa_symbol_free(original.box.tree);
        free(original.box.text);
        sa_ready_head = (sa_ready_head + 1) % SA_ASYNC_SLOT_COUNT;
        SaCoroBase* frame = sa_async_slot(p)->coro;
        frame->resume(frame);
        sa_event_loop_run_until(frame->awaited);
        sa_ready_head = (sa_ready_head + 1) % SA_ASYNC_SLOT_COUNT;
        frame->resume(frame);
        SaHandle child = frame->awaited;
        assert(sa_async_slot(child)->status == SA_PROMISE_PENDING);
        sa_promise_release(p);
        assert(sa_async_slot(child) == NULL);
        assert(sa__live == 0);
        return 0;
    }''', opt)
    assert proc.returncode == 0, proc.stderr


@requires_gcc
@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_hoisting_preserves_c_like_text_and_duplicate_aliases(opt):
    literal = "sa_tmp_1; char* sa_tmp_2 = NULL; free(sa_tmp_3); /* sa_tmp_4 */"
    source = _entity_async_source(f'PRINT "{literal}"\nAWAIT child(forward())')
    proc = _run(source, '''int main(void) {
        SaEntity_nest original = sa_make();
        SaHandle p = sa_worker_start(original);
        sa_symbol_free(original.box.tree);
        free(original.box.text);
        sa_event_loop_run_until(p);
        char* text = sa_promise_take_str(p);
        free(text);
        sa_promise_release(p);
        assert(sa__live == 0);
        return 0;
    }''', opt)
    assert proc.returncode == 0, proc.stderr
    assert literal in proc.stdout.splitlines()
