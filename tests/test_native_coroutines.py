"""手写 LLVM 协程：真实挂起、结果 ABI，以及全局退出兜底之前的资源回收。"""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from conftest import build_temp, requires_native_compiler
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.analysis.typesys import runtime_features_for_program
from sonalgebraic.backend.c_runtime import RUNTIME_HEADER, RUNTIME_SOURCE
from sonalgebraic.backend.native import generate_native_llvm_ir
from sonalgebraic.core.errors import SonCompileError
from sonalgebraic.driver.compiler import build_exe
from sonalgebraic.frontend.parser import parse_program
from test_async_errors import _SOURCE as _ERROR_SOURCE, _entity_async_source
from test_coroutines import _FIB_SOURCE, _JOIN_SOURCE, _LOOP_NESTED_SOURCE, _STRING_SOURCE, _VOID_SOURCE


_LLD = "lld-link" if sys.platform == "win32" else "ld.lld"
requires_wrap = pytest.mark.skipif(
    shutil.which("clang") is None or shutil.which(_LLD) is None,
    reason="native 分配插桩需要 clang + lld",
)


def _numbered(body: str) -> str:
    return "\n".join(f"{i * 10} {line.strip()}" for i, line in enumerate(body.strip().splitlines(), 1)) + "\n"


def _ir(source: str) -> str:
    return generate_native_llvm_ir(check_program(parse_program(source)))


_WRAP = r"""
#include <stdlib.h>
long sa_test_live = 0;
void* __real_malloc(size_t);
void* __real_calloc(size_t, size_t);
void* __real_realloc(void*, size_t);
void __real_free(void*);
void* __wrap_malloc(size_t n) {
    void* p = __real_malloc(n); if (p) ++sa_test_live; return p;
}
void* __wrap_calloc(size_t n, size_t m) {
    void* p = __real_calloc(n, m); if (p) ++sa_test_live; return p;
}
void* __wrap_realloc(void* p, size_t n) {
    if (!p) return __wrap_malloc(n);
    if (!n) { __real_free(p); --sa_test_live; return NULL; }
    return __real_realloc(p, n);
}
void __wrap_free(void* p) {
    if (p) { --sa_test_live; __real_free(p); }
}
"""


_PROBE = r"""
#include <assert.h>
extern long sa_test_live;
static void check_clean(void) {
    /* live 槽不一定占堆；同时检查槽和分配，避免 atexit 掩盖丢失的 promise。 */
    for (int i = 0; i < SA_ASYNC_SLOT_COUNT; ++i) assert(!sa_async_slots[i].live);
    assert(sa_try_top == 0);
    fprintf(stderr, "SA_LIVE=%ld\n", sa_test_live);
    assert(sa_test_live == 0);
}
static SaCoroBase* resume_queued(SaHandle p) {
    assert(sa_ready_head != sa_ready_tail);
    assert(sa_ready_queue[sa_ready_head] == p);
    sa_ready_head = (sa_ready_head + 1) % SA_ASYNC_SLOT_COUNT;
    SaCoroBase* frame = sa_async_slot(p)->coro;
    assert(frame && frame->resume && frame->cleanup);
    frame->resume(frame);
    return sa_async_slot(p)->coro;
}
"""


def _wrapped_run(source: str, driver: str = "", opt: str = "-O2", extra_ir: str = "") -> subprocess.CompletedProcess[str]:
    """IR 与完整 runtime 分别链接；探针同 runtime TU，可在退出兜底前观察槽位。"""
    checked = check_program(parse_program(source))
    ir = generate_native_llvm_ir(checked)
    ir, replacements = re.subn(r"\bdefine i32 @main\(", "define i32 @sa_unused_main(", ir, count=1)
    assert replacements == 1
    ir += extra_ir
    features = runtime_features_for_program(checked.program, checked.uses)
    assert "net" not in features, "这些状态机测试必须完全离线"
    prefix = "#define SA_ENABLE_ASYNC\n" + "".join(
        f"#define SA_ENABLE_{feature.upper()}\n" for feature in sorted(features - {"async"})
    )
    driver = driver or """
        extern void sa_main(void);
        int main(void) {
            sa_main();
            sa_error_clear(&sa_current_error);
            check_clean();
            return 0;
        }
    """
    with build_temp("native-coro-probe-", subdir="coroutine-tests") as temp:
        root = Path(temp)
        ll = root / "case.ll"
        rt = root / "runtime_probe.c"
        wrap = root / "wrap.c"
        exe = root / ("case.exe" if sys.platform == "win32" else "case")
        ll.write_text(ir, encoding="utf-8")
        rt.write_text(prefix + RUNTIME_HEADER + "\n" + RUNTIME_SOURCE + _PROBE + driver, encoding="utf-8")
        wrap.write_text(_WRAP, encoding="utf-8")
        wrap_flags = [
            f"-Wl,-wrap:{name}" if sys.platform == "win32" else f"-Wl,--wrap={name}"
            for name in ("malloc", "calloc", "realloc", "free")
        ]
        command = [shutil.which("clang"), "-fuse-ld=lld", str(ll), str(rt), str(wrap), opt,
                   "-fno-builtin", "-D_CRT_SECURE_NO_WARNINGS", *wrap_flags, "-o", str(exe)]
        if sys.platform != "win32":
            command.append("-lm")
        compiled = subprocess.run(command, capture_output=True, text=True, timeout=90)
        assert compiled.returncode == 0, compiled.stderr
        return subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)


def _assert_clean(proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode == 0, proc.stderr
    assert "SA_LIVE=0" in proc.stderr, proc.stderr


def test_native_async_emits_independent_state_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    from sonalgebraic.backend.codegen import CGen

    def forbidden(*args, **kwargs):
        pytest.fail("生成 native 协程 IR 不应调用 C 后端或外部编译器")

    monkeypatch.setattr(CGen, "generate", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    ir = _ir(_FIB_SOURCE)
    assert re.search(r"define\s+(?:internal\s+)?i64 @sa_fib_start\(", ir)
    assert re.search(r"define\s+(?:internal\s+)?void @sa_fib_cleanup\(", ir)
    resume = re.search(r"define[^\n]*void @sa_fib_resume\([^\n]*\)\s*\{(.*?)\n\}", ir, re.S)
    assert resume is not None
    assert re.search(r"switch i32 .*?\[.*?i32 1, label .*?i32 2, label", resume.group(1), re.S)
    assert resume.group(1).count("call void @sa_coro_await(") == 2
    assert "@sa_event_loop_run_until" not in resume.group(1)
    assert "call ptr @calloc(" in ir
    assert "call void @sa_event_loop_run_until(" in ir
    assert "call void @sa_promise_reject_error(" in ir


@requires_native_compiler
@pytest.mark.parametrize("source,expected", [
    pytest.param(_FIB_SOURCE, ["55"], id="fib"),
    pytest.param(_STRING_SOURCE, ["hello from coroutine"], id="string"),
    pytest.param(_VOID_SOURCE, ["tick", "after tick"], id="void"),
])
def test_native_async_driver_builds_real_executable(source: str, expected: list[str]) -> None:
    with build_temp("native-coro-driver-", subdir="coroutine-tests") as temp:
        src = Path(temp) / "case.sa"
        exe = Path(temp) / ("case.exe" if sys.platform == "win32" else "case")
        src.write_text(source, encoding="utf-8")
        build_exe(src, exe, keep_c=False, backend="native")
        proc = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == expected


_CONTROL_SOURCE = _numbered('''
ASYNC SUB identity(n AS NUM AS LONG) AS NUM AS LONG
RETURN n
.ENDSUB
ASYNC SUB worker() AS NUM AS LONG
DIM end AS NUM AS LONG AS VAR = 6
DIM step AS NUM AS LONG AS VAR = 2
DIM i AS NUM AS LONG AS VAR
DIM v AS NUM AS LONG AS VAR
DIM total AS NUM AS LONG AS VAR = 0
FOR i = 2 TO end STEP step
v = AWAIT identity(i)
end = 0
step = 99
IF v = 2 THEN
v = AWAIT identity(v + 1)
ELSE IF v = 4 THEN
v = AWAIT identity(v + 2)
ELSE
v = AWAIT identity(v + 3)
.ENDIF
total = total + v
.ENDFOR
FOR i = 6 TO 2 STEP -2
v = AWAIT identity(i)
total = total + v
.ENDFOR
WHILE i < 3
v = AWAIT identity(1)
total = total + v
i = i + 1
.ENDWHILE
RETURN total
.ENDSUB
SUB main AS PUBLIC AS VOID
DIM result AS NUM AS LONG AS VAR
result = SYNC worker()
PRINT result
.ENDSUB
CALL main
END
''')


@requires_wrap
@pytest.mark.parametrize("source,expected", [
    pytest.param(_FIB_SOURCE, "55", id="fib"),
    pytest.param(_STRING_SOURCE, "hello from coroutine", id="string"),
    pytest.param(_VOID_SOURCE, "tick\nafter tick", id="void"),
    pytest.param(_JOIN_SOURCE, "60", id="join"),
    pytest.param(_LOOP_NESTED_SOURCE, "16", id="nested-for"),
    pytest.param(_CONTROL_SOURCE, "33", id="if-for-while-bound-step"),
])
def test_native_async_results_clean_before_shutdown(source: str, expected: str) -> None:
    proc = _wrapped_run(source)
    _assert_clean(proc)
    assert proc.stdout.strip() == expected


_PROMISE_SOURCE = _numbered('''
ASYNC SUB value() AS NUM AS LONG
RETURN 21
.ENDSUB
SUB make() AS PROMISE OF NUM AS LONG
RETURN value()
.ENDSUB
ASYNC SUB consume(p AS PROMISE OF NUM AS LONG) AS NUM AS LONG
DIM n AS NUM AS LONG AS VAR
n = AWAIT p
RETURN n * 2
.ENDSUB
SUB main AS PUBLIC AS VOID
DIM p AS PROMISE OF NUM AS LONG AS VAR
DIM result AS NUM AS LONG AS VAR
p = make()
result = SYNC consume(p)
PRINT result
.ENDSUB
CALL main
END
''')


@requires_wrap
def test_native_promise_parameter_and_synchronous_promise_return() -> None:
    proc = _wrapped_run(_PROMISE_SOURCE)
    _assert_clean(proc)
    assert proc.stdout.strip() == "42"


_RESULT_TYPES_SOURCE = _numbered('''
USE SYS.BINARY AS B
ASYNC SUB boolean() AS BOOL
RETURN TRUE
.ENDSUB
ASYNC SUB floating() AS NUM AS FLOAT
RETURN 1.25
.ENDSUB
ASYNC SUB buffer() AS HANDLE AS BUFFER
DIM value AS HANDLE AS BUFFER AS VAR
value = B.NEW(7)
RETURN value
.ENDSUB
ASYNC SUB worker() AS VOID
DIM flag AS BOOL AS VAR
DIM real AS NUM AS FLOAT AS VAR
DIM handle AS HANDLE AS BUFFER AS VAR
DIM ok AS BOOL AS VAR
flag = AWAIT boolean()
real = AWAIT floating()
handle = AWAIT buffer()
PRINT F"{flag} {real} {B.LENGTH(handle)}"
ok = B.CLOSE(handle)
RETURN
.ENDSUB
SUB main AS PUBLIC AS VOID
DIM flag AS BOOL AS VAR
DIM real AS NUM AS FLOAT AS VAR
DIM handle AS HANDLE AS BUFFER AS VAR
DIM ok AS BOOL AS VAR
flag = SYNC boolean()
real = SYNC floating()
handle = SYNC buffer()
PRINT F"{flag} {real} {B.LENGTH(handle)}"
ok = B.CLOSE(handle)
SYNC worker()
.ENDSUB
CALL main
END
''')


@requires_wrap
def test_native_bool_float_handle_results_through_await_and_sync() -> None:
    proc = _wrapped_run(_RESULT_TYPES_SOURCE)
    _assert_clean(proc)
    assert proc.stdout.splitlines() == ["1 1.25 7", "1 1.25 7"]


@requires_native_compiler
def test_native_uncaught_async_preserves_original_error() -> None:
    with build_temp("native-coro-uncaught-", subdir="coroutine-tests") as temp:
        src = Path(temp) / "case.sa"
        exe = Path(temp) / ("case.exe" if sys.platform == "win32" else "case")
        src.write_text(_ERROR_SOURCE, encoding="utf-8")
        build_exe(src, exe, keep_c=False, backend="native")
        proc = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 1
    assert "ERR_ORIGINAL at line 40: original message" in proc.stderr


@requires_wrap
@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_native_repeated_failure_preserves_saerror_and_scheduler(opt: str) -> None:
    source = '''1 USEC <stdlib.h> AS P
2 DECLARE C SUB P.check_original(err AS ERROR AS REF) AS VOID
''' + _ERROR_SOURCE[:_ERROR_SOURCE.index("140 SUB main")] + '''140 ASYNC SUB good() AS STRING
150 RETURN "still running"
160 .ENDSUB
170 SUB attempt() AS VOID
180 SYNC outer()
190 .ENDSUB
200 SUB main AS PUBLIC AS VOID
210 DIM trap AS ERROR AS VAR
220 DIM i AS NUM AS LONG AS VAR
230 DIM text AS STRING AS VAR
240 FOR i = 1 TO 300
250 TRY CALL attempt() TRACEBACK ERROR AS trap
260 CATCH ERR_ORIGINAL AS caught
270 CALL P.check_original(trap)
280 .ENDTRY
290 text = SYNC good()
300 PRINT text
310 .ENDFOR
320 .ENDSUB
330 CALL main
340 END
'''
    proc = _wrapped_run(source, r'''
        static int caught_count = 0;
        void check_original(SaError* error) {
            assert(strcmp(error->type, "ERR_ORIGINAL") == 0);
            assert(strcmp(error->message, "original message") == 0);
            assert(error->err_code == sa_error_code("ERR_ORIGINAL"));
            assert(error->line_number == 40);
            assert(strcmp(error->sub_name, "leaf") == 0);
            for (int i = 0; i < SA_ASYNC_SLOT_COUNT; ++i) assert(!sa_async_slots[i].live);
            ++caught_count;
        }
        extern void sa_main(void);
        int main(void) {
            sa_main();
            assert(caught_count == 300);
            sa_error_clear(&sa_current_error);
            check_clean();
            return 0;
        }
    ''', opt)
    _assert_clean(proc)
    assert proc.stdout.splitlines() == ["still running"] * 300


_SNAPSHOT_SOURCE = _numbered('''
USEC <stdlib.h> AS P
DECLARE C SUB P.seed_error(value AS ERROR AS REF) AS VOID
DECLARE C SUB P.check_error(value AS ERROR AS REF) AS VOID
DECLARE C SUB P.mutate_error(value AS ERROR AS REF) AS VOID
DECLARE C SUB P.mutate_tree(value AS SYMBOL) AS VOID
DECLARE C SUB P.mutate_text(value AS STRING) AS VOID
FOR ENTITY AS Box
DIM text AS STRING AS VAR
DIM tree AS SYMBOL AS VAR
DIM err AS ERROR AS VAR
.ENDENTITY
FOR ENTITY AS Nest
DIM box AS ENTITY AS Box AS VAR
.ENDENTITY
ASYNC SUB tick() AS VOID
RETURN
.ENDSUB
ASYNC SUB worker(text AS STRING, tree AS SYMBOL, value AS ENTITY AS Nest) AS NUM AS LONG
AWAIT tick()
PRINT text
PRINT tree
PRINT value.box.text
PRINT value.box.tree
PRINT value.box.err
CALL P.check_error(value.box.err)
RETURN 7
.ENDSUB
SUB prepare() AS PROMISE OF NUM AS LONG
DIM text AS STRING AS VAR = "original text"
DIM tree AS SYMBOL AS VAR
DIM x AS NUM AS LONG AS VAR
DIM value AS ENTITY AS Nest AS VAR
DIM p AS PROMISE OF NUM AS LONG AS VAR
tree = x + 2 * 3
value.box.text = text
value.box.tree = tree
CALL P.seed_error(value.box.err)
p = CALL worker(text, tree, value)
CALL P.mutate_text(text)
CALL P.mutate_tree(tree)
CALL P.mutate_text(value.box.text)
CALL P.mutate_tree(value.box.tree)
CALL P.mutate_error(value.box.err)
RETURN p
.ENDSUB
SUB main AS PUBLIC AS VOID
.ENDSUB
CALL main
END
''')


_SNAPSHOT_HELPERS = r'''
    void seed_error(SaError* e) {
        *e = (SaError){77, "ERR_SNAPSHOT", sa_strdup("original error"), 321, "origin"};
    }
    void check_error(SaError* e) {
        assert(e->err_code == 77 && e->line_number == 321);
        assert(strcmp(e->type, "ERR_SNAPSHOT") == 0);
        assert(strcmp(e->message, "original error") == 0);
        assert(strcmp(e->sub_name, "origin") == 0);
    }
    void mutate_error(SaError* e) { e->message[0] = 'X'; }
    void mutate_text(char* s) { s[0] = 'X'; }
    void mutate_tree(SaSymbol s) {
        if (!s) return;
        if (s->text && s->text[0]) s->text[0] = 'X';
        mutate_tree(s->left);
        mutate_tree(s->right);
    }
    extern SaHandle sa_prepare(void);
'''


@requires_wrap
@pytest.mark.parametrize("opt", ["-O0", "-O2"])
@pytest.mark.parametrize("lifecycle", ["complete", "unstarted", "suspended", "child_settled"])
def test_native_managed_snapshots_and_exact_cancellation(lifecycle: str, opt: str) -> None:
    action = {
        "complete": '''
            sa_event_loop_run_until(p);
            assert(sa_async_slot(p)->coro == NULL);
            assert(sa_promise_take_long(p) == 7);
            sa_promise_release(p);
        ''',
        "unstarted": '''
            assert(sa_async_slot(p)->coro->state == 0);
            sa_promise_release(p);
        ''',
        "suspended": '''
            SaCoroBase* frame = resume_queued(p);
            assert(frame && frame->state > 0);
            SaHandle child = frame->awaited;
            assert(sa_async_slot(child)->status == SA_PROMISE_PENDING);
            assert(sa_async_slot(child)->waiter == p);
            sa_promise_release(p);
            assert(sa_async_slot(child) == NULL);
        ''',
        "child_settled": '''
            SaCoroBase* frame = resume_queued(p);
            assert(frame && frame->state > 0);
            SaHandle child = frame->awaited;
            sa_event_loop_run_until(child);
            assert(sa_async_slot(child)->coro == NULL);
            assert(sa_async_slot(p)->status == SA_PROMISE_PENDING);
            sa_promise_release(p);
            assert(sa_async_slot(child) == NULL);
        ''',
    }[lifecycle]
    proc = _wrapped_run(_SNAPSHOT_SOURCE, _SNAPSHOT_HELPERS + '''
        int main(void) {
            SaHandle p = sa_prepare();
    ''' + action + '''
            check_clean();
            return 0;
        }
    ''', opt)
    _assert_clean(proc)
    expected = ["original text", "(x + (2 * 3))"] * 2 + ["original error"]
    assert proc.stdout.splitlines() == (expected if lifecycle == "complete" else [])


@requires_wrap
@pytest.mark.parametrize("cancel", [False, True])
def test_native_error_parameter_snapshot_through_internal_abi(cancel: bool) -> None:
    # 前端目前拒绝 ERROR 按值实参表达式；桥接只负责将 C 指针装载为 native 内部聚合值。
    source = _numbered('''
USEC <stdlib.h> AS P
DECLARE C SUB P.check_error(value AS ERROR AS REF) AS VOID
ASYNC SUB tick() AS VOID
RETURN
.ENDSUB
ASYNC SUB worker(err AS ERROR) AS NUM AS LONG
AWAIT tick()
CALL P.check_error(err)
PRINT err
RETURN 7
.ENDSUB
SUB main AS PUBLIC AS VOID
.ENDSUB
CALL main
END
''')
    bridge = '''
define i64 @sa_error_probe_start(ptr %original) {
entry:
  %value = load %SaError, ptr %original
  %promise = call i64 @sa_worker_start(%SaError %value)
  ret i64 %promise
}
'''
    action = '''
        SaCoroBase* frame = resume_queued(p);
        assert(frame && frame->state > 0);
        sa_promise_release(p);
    ''' if cancel else '''
        sa_event_loop_run_until(p);
        assert(sa_promise_take_long(p) == 7);
        sa_promise_release(p);
    '''
    proc = _wrapped_run(source, _SNAPSHOT_HELPERS + '''
        extern SaHandle sa_error_probe_start(SaError*);
        int main(void) {
            SaError original = {0};
            seed_error(&original);
            SaHandle p = sa_error_probe_start(&original);
            mutate_error(&original);
            sa_error_clear(&original);
    ''' + action + '''
            check_clean();
            return 0;
        }
    ''', extra_ir=bridge)
    _assert_clean(proc)
    assert proc.stdout.splitlines() == ([] if cancel else ["original error"])


def _resource_source(finish: str) -> str:
    tail = {
        "return": "RETURN",
        "throw": 'THROW NEW ERR_BLOCK, F"failure {7}"',
        "child": "AWAIT fail()",
        "cancel": 'AWAIT tick(F"child {9}")',
    }[finish]
    return _numbered('''
ASYNC SUB tick(text AS STRING) AS VOID
RETURN
.ENDSUB
ASYNC SUB fail() AS VOID
THROW NEW ERR_CHILD, "child failure"
.ENDSUB
ASYNC SUB worker(text AS STRING) AS VOID
DIM owner AS STRING AS VAR = "owned once"
DIM borrowed AS STRING AS VAR
DIM tree AS SYMBOL AS VAR
borrowed f= owner
tree = 2 * 3 + 1
IF TRUE THEN
DIM block AS STRING AS VAR
block = F"block {text}"
AWAIT tick(F"temporary {text}")
PRINT block
PRINT borrowed
''' + tail + '''
.ENDIF
.ENDSUB
SUB main AS PUBLIC AS VOID
.ENDSUB
CALL main
END
''')


@requires_wrap
@pytest.mark.parametrize("opt", ["-O0", "-O2"])
@pytest.mark.parametrize("finish", ["return", "throw", "child", "cancel"])
def test_native_block_borrow_and_statement_temporaries(finish: str, opt: str) -> None:
    if finish == "cancel":
        action = '''
            SaCoroBase* frame = resume_queued(p);
            assert(frame && frame->awaited);
            sa_event_loop_run_until(frame->awaited);
            frame = resume_queued(p);
            assert(frame && frame->state > 0);
            SaHandle child = frame->awaited;
            assert(sa_async_slot(child)->status == SA_PROMISE_PENDING);
            assert(sa_async_slot(child)->waiter == p);
            sa_promise_release(p);
            assert(sa_async_slot(child) == NULL);
        '''
    else:
        status = "SA_PROMISE_FULFILLED" if finish == "return" else "SA_PROMISE_REJECTED"
        action = f'''
            sa_event_loop_run_until(p);
            assert(sa_async_slot(p)->coro == NULL);
            assert(sa_async_slot(p)->status == {status});
            sa_promise_release(p);
        '''
    proc = _wrapped_run(_resource_source(finish), '''
        extern SaHandle sa_worker_start(char*);
        int main(void) {
            SaHandle p = sa_worker_start("argument");
    ''' + action + '''
            sa_error_clear(&sa_current_error);
            check_clean();
            return 0;
        }
    ''', opt)
    _assert_clean(proc)
    assert proc.stdout.splitlines() == ["block argument", "owned once"]


@requires_wrap
@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_native_throwing_expression_and_sync_argument_release_temporaries(opt: str) -> None:
    source = _numbered('''
USE SYS.STRING AS S
SUB fail(text AS STRING) AS STRING
THROW NEW ERR_EXPR, "expression failed"
RETURN "unreachable"
.ENDSUB
ASYNC SUB worker(text AS STRING) AS VOID
DIM value AS STRING AS VAR
value = F"allocated before {fail(S.CONCAT(text, \" suffix\"))}"
.ENDSUB
SUB attempt() AS VOID
SYNC worker(S.CONCAT("temporary ", "argument"))
.ENDSUB
SUB main AS PUBLIC AS VOID
DIM trap AS ERROR AS VAR
TRY CALL attempt() TRACEBACK ERROR AS trap
CATCH ERR_EXPR AS caught
PRINT caught
.ENDTRY
.ENDSUB
CALL main
END
''')
    proc = _wrapped_run(source, opt=opt)
    _assert_clean(proc)
    assert proc.stdout.strip() == "expression failed"


def _entity_probe_source(action: str) -> str:
    # 参数/返回值通过 SA 包装调用，避免 C probe 假定 native 内部聚合值 ABI。
    source = _entity_async_source(action)
    checked = check_program(parse_program(source))
    main_line = next(sub.line_no for sub in checked.program.subs if sub.name.lower() == "main")
    body = "\n".join(line.split(" ", 1)[1] for line in source.splitlines() if int(line.split(" ", 1)[0]) < main_line)
    return _numbered(body + '''
SUB prepare() AS PROMISE OF STRING
RETURN worker(forward())
.ENDSUB
SUB main AS PUBLIC AS VOID
.ENDSUB
CALL main
END
''')


@requires_wrap
@pytest.mark.parametrize("action", ["AWAIT child(forward())", "CALL fail(forward())"])
def test_native_entity_temporary_across_await_or_failure(action: str) -> None:
    error = "1" if action.startswith("CALL") else "0"
    proc = _wrapped_run(_entity_probe_source(action), r'''
        extern SaHandle sa_prepare(void);
        int main(void) {
            for (int i = 0; i < 20; ++i) {
                SaHandle p = sa_prepare();
                sa_event_loop_run_until(p);
                SaAsyncSlot* slot = sa_async_slot(p);
                assert(slot->coro == NULL);
                if (''' + error + r''') {
                    assert(slot->status == SA_PROMISE_REJECTED);
                    assert(strcmp(slot->error.type, "ERR_ENTITY") == 0);
                } else {
                    char* text = sa_promise_take_str(p);
                    assert(strcmp(text, "snapshot") == 0);
                    free(text);
                }
                sa_promise_release(p);
                sa_error_clear(&sa_current_error);
                check_clean();
            }
            return 0;
        }
    ''')
    _assert_clean(proc)
    assert "((2 * 3) + 1)" in proc.stdout


@requires_wrap
def test_native_entity_temporary_and_locals_release_when_cancelled() -> None:
    proc = _wrapped_run(_entity_probe_source("AWAIT child(forward())"), r'''
        extern SaHandle sa_prepare(void);
        int main(void) {
            SaHandle p = sa_prepare();
            SaCoroBase* frame = resume_queued(p);
            assert(frame && frame->awaited);
            sa_event_loop_run_until(frame->awaited);
            frame = resume_queued(p);
            assert(frame && frame->state > 0);
            SaHandle child = frame->awaited;
            assert(sa_async_slot(child)->status == SA_PROMISE_PENDING);
            assert(sa_async_slot(child)->waiter == p);
            sa_promise_release(p);
            assert(sa_async_slot(child) == NULL);
            check_clean();
            return 0;
        }
    ''')
    _assert_clean(proc)
    assert proc.stdout.splitlines() == ["((2 * 3) + 1)"] * 2


@requires_wrap
def test_native_await_preserves_assignment_address_and_scope_slots() -> None:
    source = _numbered('''
FOR ENTITY AS Box
DIM text AS STRING AS VAR
.ENDENTITY
DIM calls AS NUM AS LONG AS VAR
SUB locate(value AS NUM AS LONG AS REF) AS PTR TO NUM AS LONG
calls = calls + 1
RETURN @value
.ENDSUB
ASYNC SUB produce_num() AS NUM AS LONG
RETURN 42
.ENDSUB
ASYNC SUB text() AS STRING
RETURN "held across await"
.ENDSUB
ASYNC SUB worker() AS VOID
DIM value AS NUM AS LONG AS VAR
DIM values[2] AS NUM AS LONG AS VAR
DIM box AS ENTITY AS Box AS VAR
^locate(value) = AWAIT produce_num()
values[1] = AWAIT produce_num()
box.text = AWAIT text()
IF TRUE THEN
DIM scoped AS STRING AS VAR
scoped = AWAIT text()
PRINT scoped
ELSE
DIM scoped AS NUM AS LONG AS VAR
scoped = AWAIT produce_num()
PRINT scoped
.ENDIF
PRINT F"{value} {values[1]} {calls} {box.text}"
.ENDSUB
SUB main AS PUBLIC AS VOID
SYNC worker()
.ENDSUB
CALL main
END
''')
    proc = _wrapped_run(source)
    _assert_clean(proc)
    assert proc.stdout.splitlines() == ["held across await", "42 42 1 held across await"]


@requires_wrap
def test_native_gosub_return_stack_survives_await() -> None:
    source = _numbered('''
ASYNC SUB tick() AS VOID
PRINT "tick"
.ENDSUB
ASYNC SUB worker() AS VOID
GOSUB ::part
PRINT "returned"
RETURN
::part
AWAIT tick()
RETURN
.ENDSUB
SUB main AS PUBLIC AS VOID
SYNC worker()
.ENDSUB
CALL main
END
''')
    proc = _wrapped_run(source)
    _assert_clean(proc)
    assert proc.stdout.splitlines() == ["tick", "returned"]


@requires_wrap
def test_native_coroutine_catches_sync_failure_then_awaits() -> None:
    source = _numbered('''
SUB fail() AS VOID
THROW NEW ERR_RECOVER, "recovered"
.ENDSUB
ASYNC SUB tick() AS VOID
PRINT "resumed"
.ENDSUB
ASYNC SUB worker() AS VOID
DIM trap AS ERROR AS VAR
DIM text AS STRING AS VAR = "still owned"
TRY CALL fail() TRACEBACK ERROR AS trap
CATCH ERR_RECOVER AS caught
PRINT caught
.ENDTRY
AWAIT tick()
PRINT text
.ENDSUB
SUB main AS PUBLIC AS VOID
SYNC worker()
.ENDSUB
CALL main
END
''')
    proc = _wrapped_run(source)
    _assert_clean(proc)
    assert proc.stdout.splitlines() == ["recovered", "resumed", "still owned"]


def test_native_async_cross_scope_jump_reports_supported_boundary() -> None:
    source = _numbered('''
ASYNC SUB worker() AS VOID
IF TRUE THEN
GOTO ::done
.ENDIF
::done
RETURN
.ENDSUB
SUB main AS PUBLIC AS VOID
SYNC worker()
.ENDSUB
CALL main
END
''')
    with pytest.raises(SonCompileError, match="GOTO/GOSUB.*作用域"):
        _ir(source)
