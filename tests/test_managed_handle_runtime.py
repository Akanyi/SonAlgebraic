"""托管句柄在 runtime 一侧的两条契约：析构安全、PROMISE 结果的所有权。

语义层把 `FILE` 这类托管句柄的所有权算得很细，但算得对不对只有 runtime 的行为能证明。
这里不跑 SA 程序，直接拿 runtime 的 C 接口当真被测对象——它同时是 C 后端和 native 后端的
运行时，这里错了两边一起错，而 SA 层的测试只会把它误诊成 codegen 的问题。

两条契约各自对应一个很安静的坏法：

1. `sa_file_drop` 是**析构**，不是用户发起的操作。它得认得出「这个 token 已经过期」——槽位
   被回收、generation 递增，旧 token 即便在槽位重新有主之后也依然是死的——并把重复析构
   变成空操作；同时它不能碰 `LAST_ERROR` / `errno`，否则自动清理会把正在诊断的现场踩掉。
2. `PROMISE` 在结果被取走之前**拥有**它。`fulfill_owned_handle` 接过来的句柄由槽位负责销毁；
   `take` 之后责任转给调用方，槽位不能再碰。少一处是关两遍，多一处是漏关。

恢复说明：原文件在 10 月初丢失，测试侧没有字节码可比对。这份是按幸存的 pytest 节点名
配合恢复后的 `backend/runtime/sa_runtime.c` 重建的，不是逐字找回。详见 `.recovery/RECOVERY.md`。
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from conftest import build_temp
from sonalgebraic.backend.c_runtime import RUNTIME_HEADER, RUNTIME_SOURCE

# FILE 块与 ASYNC 块都要开：前者给 FILE 槽位，后者给 PROMISE 槽位和整套 settle/take。
_PROBE_PREFIX = "#define SA_ENABLE_FILE\n#define SA_ENABLE_ASYNC\n"


def _driver(label: str) -> str:
    """探针只用 runtime 自己的接口：开一个真文件、关掉、再故意析构旧 token。

    伪句柄不用手拼——`sa_file_open` 会复用刚回收的槽位，于是「旧 token」和「复用了同一个
    槽位的新句柄」就是一对天然的 stale / fresh，这比自造位模式更接近真实调用路径会拿到的东西。
    """
    return f'''
#include <assert.h>

static SaHandle probe_open(void) {{
    SaHandle handle = sa_file_open("probe-handle.bin", "WRITE");
    assert(handle != 0);
    assert(sa_file_slot(handle) != NULL);
    assert((unsigned int)(handle >> 56) == SA_HANDLE_FILE);
    return handle;
}}

static int probe_slot_live(SaHandle handle) {{
    return sa_file_slot(handle) != NULL;
}}

static void probe_owned_handle_is_taken_out(void) {{
    SaHandle promise = sa_promise_alloc(NULL);
    SaHandle owned = probe_open();
    sa_promise_fulfill_owned_handle(promise, owned, sa_file_drop_value);
    /* 还没取走：所有权在槽位上，句柄必须还活着 */
    assert(probe_slot_live(owned));
    assert(sa_promise_take_handle(promise) == owned);
    /* 取走之后责任归调用方，槽位不该再动它 */
    assert(probe_slot_live(owned));
    sa_file_drop(owned);
    assert(!probe_slot_live(owned));
    sa_promise_release(promise);
}}

static void probe_ownerless_result_is_disposed(void) {{
    SaHandle promise = sa_promise_alloc(NULL);
    SaHandle owned = probe_open();
    sa_promise_fulfill_owned_handle(promise, owned, sa_file_drop_value);
    /* 没有 take 就释放：槽位自己销毁结果，否则这个句柄永远关不掉 */
    sa_promise_release(promise);
    assert(!probe_slot_live(owned));
    /* 再析构一次是空操作，而不是第二次 fclose */
    sa_file_drop(owned);
}}

static void probe_void_result_drops_handle_payload(void) {{
    SaHandle promise = sa_promise_alloc(NULL);
    SaHandle owned = probe_open();
    sa_promise_fulfill_owned_handle(promise, owned, sa_file_drop_value);
    assert(probe_slot_live(owned));
    /* 结果类型是 VOID：take_void 不交出句柄，但要把槽位持有的它收掉 */
    sa_promise_take_void(promise);
    assert(!probe_slot_live(owned));
    sa_file_drop(owned);
}}

int main(void) {{
    SaHandle handle = probe_open();
    SaHandle token = handle;

    assert(sa_file_close_owned(&handle) == 1);
    assert(handle == 0);
    assert(!probe_slot_live(token));

    /* 析构不是用户操作：不许改 LAST_ERROR，也不许还回 errno */
    sa_set_error(&sa_current_error, &(SaError){{7, "ERR_PROBE", (char*)"live error", 3, "probe"}});
    int saved_errno = errno;
    sa_file_drop(token);
    assert(!probe_slot_live(token));
    assert(sa_current_error.err_code == 7);
    assert(strcmp(sa_current_error.type, "ERR_PROBE") == 0);
    assert(strcmp(sa_current_error.message, "live error") == 0);
    assert(sa_current_error.line_number == 3);
    assert(strcmp(sa_current_error.sub_name, "probe") == 0);
    assert(errno == saved_errno);

    /* 同一个槽位发了新句柄：旧 token 依然过期，析构它不能碰新主人 */
    SaHandle fresh = probe_open();
    assert((token & 0xffffu) == (fresh & 0xffffu));
    assert(((token >> 16) & 0xffffffffu) != ((fresh >> 16) & 0xffffffffu));
    sa_file_drop(token);
    assert(probe_slot_live(fresh));
    assert(!probe_slot_live(token));
    sa_file_close_owned(&fresh);
    assert(fresh == 0);
    sa_file_drop(fresh);

    probe_owned_handle_is_taken_out();
    probe_ownerless_result_is_disposed();
    probe_void_result_drops_handle_payload();

    sa_error_clear(&sa_current_error);
    printf("HANDLE-RUNTIME-OK {label}\\n");
    return 0;
}}
'''


def _run_probe(opt: str) -> subprocess.CompletedProcess[str]:
    """单文件模式：前导 + 实现 + 驱动编成一个 TU，探针才能看见 FILE / PROMISE 槽位表。

    两个后端在这里是同一件事：被测的是 runtime 的句柄协议，而它就是那一份 C 实现。
    `-O0` / `-O2` 才是真正要分开看的东西——`SA_DEFINE_HANDLE_DROP` 里那几行
    save/restore 全靠「反正 closer 不改 errno」的推理撑着，删掉照样编得过，-O2 下
    更容易让这种推理失效。
    """
    compiler = shutil.which("clang") or shutil.which("gcc")
    assert compiler is not None
    with build_temp("handle-runtime-", "handle-tests") as temp:
        root = Path(temp)
        source = root / "probe.c"
        source.write_text(_PROBE_PREFIX + RUNTIME_HEADER + "\n" + RUNTIME_SOURCE + _driver("probe"),
                          encoding="utf-8")
        exe = root / ("probe.exe" if sys.platform == "win32" else "probe")
        command = [compiler, str(source), opt, "-std=c11", "-o", str(exe)]
        if sys.platform != "win32":
            command.append("-lm")
        compiled = subprocess.run(command, text=True, capture_output=True, timeout=120)
        assert compiled.returncode == 0, compiled.stderr
        return subprocess.run([str(exe)], cwd=root, text=True, encoding="utf-8",
                              capture_output=True, timeout=60)


@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_file_drop_is_generation_safe_and_preserves_errors(opt: str) -> None:
    """关掉 / 槽位复用 / 再析构这一串走完：不许碰到新主人，也不许动诊断现场。"""
    result = _run_probe(opt)
    assert result.returncode == 0, result.stderr
    assert "HANDLE-RUNTIME-OK probe" in result.stdout


@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_promise_owns_result_until_take_or_discard(opt: str) -> None:
    """结果在谁手上，句柄就活到谁手上为止：take 转出、release 销毁、take_void 收掉。

    三种出口都必须让句柄恰好关一次。漏掉 release 那一支，被丢弃的 future 就把流挂到
    进程退出；漏掉 take 那一支，槽位会去关一个已经交给调用方的句柄。
    """
    result = _run_probe(opt)
    assert result.returncode == 0, result.stderr
    assert "HANDLE-RUNTIME-OK probe" in result.stdout
