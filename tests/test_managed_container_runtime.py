"""托管容器的 runtime 侧行为：槽位安全、元素所有权、标量增长与越界。

容器的值语义全在 `SaValueOps` 那一小张表里（size / copyable / copy / drop），容器本体只按
size 搬字节。所以这一层出错的形态很单调：**多释放一次**或**少释放一次**。两者都不会崩在
出事的那一行上——多释放要等到下一个持有者踩到那块内存，少释放要等到进程退出才发现流还
开着。这组测试直接调 runtime 的 C 接口，用自定义 ops 表把每一次 copy/drop 记下来。

六件事分开测，因为它们各自有独立的坏法：

- 析构是 generation 安全的（复用了槽位也认得旧 token），且不碰 LAST_ERROR；
- dict 扩容要重挂链表，而且 key 是**自己 strdup 的**，不是借用调用方那个指针；
- dict 的值（这里用托管句柄）走和 vec 一样的所有权规则；
- 元素的 copy / drop 恰好各一次，`TAKE` 之后尾槽必须清零；
- move-only 的表（copyable = 0）不开放复制接口，但移动接口照常；
- 标量容器按需增长，越界一律是「报错 + 返回假」，不是越界写。

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

# 托管容器整块在 SA_ENABLE_CONTAINER 里（旧的 SA_ENABLE_LIST / SA_ENABLE_MAP 是另一套槽位表，
# 元素只有 double 和 char*）。FILE 块另外开：探针拿真句柄当「独占元素」。
_PROBE_PREFIX = "#define SA_ENABLE_CONTAINER\n#define SA_ENABLE_FILE\n"


_PROBE_SUPPORT = """#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* 用 PROBE 而不是 assert：assert 在 -O2（NDEBUG）下整段消失，可这组测试恰恰要在两个
   优化档下都数一遍 copy/drop。失败时打行号再退出，两档行为一致。 */
#define PROBE(condition) do { if (!(condition)) { fprintf(stderr, "PROBE failed at line %d: %s\\n", __LINE__, #condition); exit(1); } } while (0)
"""


def _header(prelude: str) -> str:
    return _PROBE_PREFIX + RUNTIME_HEADER + '\n' + RUNTIME_SOURCE + '\n' + _PROBE_SUPPORT + prelude


# 记数的 ops 表：把每一次 copy / drop 都写进静态计数器，退出时打印。
# copy 的目标是只写的生料字节（见 value_ops.py 的两条硬约束），所以这里只能写，
# 不能读旧值——探针按同样的规矩写，才和生成代码走同一条路。
_COUNTING_OPS = r'''

typedef struct { long long payload; long long tag; } ProbeValue;

/* copies / drops 数的是容器调了回调几次。不维护「当前有几份活值」这种计数：值一旦
   交给容器，谁在什么时候释放它就说不清了，那种计数只会出错。真正要抓的是**重复释放**
   ——drop 拿到的元素已经归零，说明同一份资源被释放了两次。 */
static long probe_copies = 0;
static long probe_drops = 0;
static long probe_double_drops = 0;

static void probe_copy(void* target, const void* source) {
    const ProbeValue* from = (const ProbeValue*)source;
    ProbeValue* to = (ProbeValue*)target;
    ++probe_copies;
    to->payload = from->payload;
    to->tag = from->tag;
}

static void probe_drop(void* value) {
    ProbeValue* slot = (ProbeValue*)value;
    ++probe_drops;
    if (!slot->tag) ++probe_double_drops;   /* 已经释放过的又释放了一次 */
    slot->payload = 0;
    slot->tag = 0;
}

static void probe_construct(void* target, long long payload) {
    ProbeValue* slot = (ProbeValue*)target;
    slot->payload = payload;
    slot->tag = payload;
}

static const SaValueOps probe_ops = { sizeof(ProbeValue), 1, probe_copy, probe_drop };

/* KEYS 交出来的那个 vec：元素是 char*，析构就是释放 key 的副本。它和 probe_ops 是两张表
   ——表描述的是**元素**的析构方式。把值表套在 key 上（或反过来），析构时会按错的尺寸读、
   用错的指针 free，表现成堆损坏（0xC0000374），而不是一句清楚的报错，排查会绕很远。 */
static long probe_key_taken = 0;
static void probe_key_copy(void* target, const void* source) {
    /* 必须真复制出一份新的字符串。表里写 copyable = 1 而 copy 留 NULL 的话，
       sa_value_copy 会退化回 memcpy——vec 里存的就是 dict 自己那个指针，两张表析构
       同一块内存，堆直接坏掉（0xC0000374）。这里出的错不会报在任何一行上。 */
    char* const* from = (char* const*)source;
    char** to = (char**)target;
    *to = *from ? strdup(*from) : NULL;
    if (*from && !*to) exit(1);
}
static void probe_key_free(void* value) {
    char** slot = (char**)value;
    if (!*slot) { ++probe_double_drops; return; }   /* 同一份 key 释放了两次 */
    ++probe_key_taken;
    free(*slot);
    *slot = NULL;
}
static const SaValueOps key_ops = { sizeof(char*), 1, probe_key_copy, probe_key_free };
'''


def _run_probe(prelude: str, opt: str, extra_defines: str = "") -> subprocess.CompletedProcess[str]:
    """单文件模式编译（前导 + 实现 + 探针同一个 TU），探针才能看见槽位表和计数变量。"""
    compiler = shutil.which("clang") or shutil.which("gcc")
    assert compiler is not None
    with build_temp("container-runtime-", "container-tests") as temp:
        root = Path(temp)
        source = root / "probe.c"
        source.write_text(extra_defines + _header(prelude), encoding="utf-8")
        exe = root / ("probe.exe" if sys.platform == "win32" else "probe")
        command = [compiler, str(source), opt, "-std=c11", "-o", str(exe)]
        if sys.platform != "win32":
            command.append("-lm")
        compiled = subprocess.run(command, text=True, capture_output=True, timeout=180)
        assert compiled.returncode == 0, compiled.stderr
        return subprocess.run([str(exe)], cwd=root, text=True, encoding="utf-8",
                              capture_output=True, timeout=60)


_DROP_PROBE = _COUNTING_OPS + r'''
int main(void) {
    /* 同一槽位复用两次：旧 token 即便在槽位重新有主之后也必须是死的 */
    SaHandle first = sa_vec_new(&probe_ops);
    SaHandle stale = first;
    PROBE(first != 0);
    ProbeValue value;
    probe_construct(&value, 7);
    PROBE(sa_vec_push(first, &value));
    PROBE(sa_vec_length(first) == 1);

    PROBE(sa_vec_close_owned(&first) == 1);
    PROBE(first == 0);
    PROBE(sa_vec_length(stale) == -1);
    PROBE(probe_drops == 1);

    sa_set_error(&sa_current_error, &(SaError){9, "ERR_PROBE", (char*)"live", 5, "probe"});
    int saved_errno = errno;
    sa_vec_drop(stale);              /* 显式析构一个过期 token：不许碰槽位、更不许自杀 */
    PROBE(sa_vec_length(stale) == -1);
    PROBE(strcmp(sa_current_error.message, "live") == 0);
    PROBE(sa_current_error.err_code == 9);
    PROBE(errno == saved_errno);
    PROBE(probe_drops == 1);

    /* 槽位被下一个容器接手：旧 token 依然死，新容器不受影响 */
    SaHandle fresh = sa_vec_new(&probe_ops);
    PROBE((fresh & 0xffffu) == (stale & 0xffffu));
    PROBE(((fresh >> 16) & 0xffffffffu) != ((stale >> 16) & 0xffffffffu));
    sa_vec_drop(stale);
    PROBE(sa_vec_length(fresh) == 0);
    PROBE(sa_vec_length(stale) == -1);
    PROBE(sa_vec_close_owned(&fresh) == 1);
    PROBE(probe_drops == 1);
    /* 两次析构都作用在过期 token 上：既不能重复释放元素，也不能碰后来那个容器 */
    PROBE(probe_double_drops == 0);
    printf("DROP-OK copies=%ld drops=%ld double=%ld\n",
           probe_copies, probe_drops, probe_double_drops);
    return 0;
}
'''


_DICT_KEY_PROBE = _COUNTING_OPS + r'''
int main(void) {
    /* key 必须被 dict 自己复制：调用方那个缓冲区随时可能被覆写/释放 */
    SaHandle dict = sa_dict_new(&probe_ops);
    PROBE(dict != 0);
    char key[24];   /* 要装得下最长那个字面量加结尾的 \0，否则 strcpy 写穿栈 */
    strcpy(key, "alpha");
    ProbeValue value;
    probe_construct(&value, 42);
    PROBE(sa_dict_set(dict, key, &value));
    strcpy(key, "zzzzz");            /* 覆写调用方缓冲区：dict 里的 key 必须还是 alpha */

    PROBE(sa_dict_has(dict, "alpha"));
    PROBE(!sa_dict_has(dict, "zzzzz"));
    ProbeValue out;
    memset(&out, 0, sizeof(out));
    PROBE(sa_dict_get(dict, "alpha", &out));
    PROBE(out.payload == 42);

    /* 扩容要重挂链表：先塞出远超初始桶数的量，再逐个查回来 */
    for (long long i = 0; i < 64; ++i) {
        char name[24];
        sprintf(name, "key-%lld", i);
        ProbeValue item;
        probe_construct(&item, 100 + i);
        PROBE(sa_dict_set(dict, name, &item));
    }
    PROBE(sa_dict_length(dict) == 65);
    for (long long i = 0; i < 64; ++i) {
        char name[24];
        sprintf(name, "key-%lld", i);
        ProbeValue item;
        memset(&item, 0, sizeof(item));
        PROBE(sa_dict_get(dict, name, &item));
        PROBE(item.payload == 100 + i);
    }
    PROBE(sa_dict_has(dict, "alpha"));

    /* KEYS 的 ops 表必须按 char* 来：塞 ProbeValue 那张表会按 16 字节读 8 字节的指针 */
    SaHandle keys = sa_dict_keys(dict, &key_ops);
    PROBE(keys != 0);
    PROBE(sa_vec_length(keys) == 65);
    PROBE(sa_vec_close_owned(&keys) == 1);
    PROBE(sa_dict_length(dict) == 65);

    PROBE(sa_dict_close_owned(&dict) == 1);
    PROBE(dict == 0);
    /* 每个值恰好 drop 一次：64 个 key-N 加 alpha */
    PROBE(probe_drops == 65);
    printf("DICT-OK drops=%ld copies=%ld\n", probe_drops, probe_copies);
    return 0;
}
'''


_DICT_VALUE_PROBE = _COUNTING_OPS + r'''
int main(void) {
    /* dict 的值和 vec 的元素走同一套规则：SET 复制、TAKE 转出、REMOVE 释放 */
    SaHandle dict = sa_dict_new(&probe_ops);
    PROBE(dict != 0);
    ProbeValue value;
    probe_construct(&value, 5);
    PROBE(sa_dict_set(dict, "k", &value));
    PROBE(probe_copies == 1);
    PROBE(probe_double_drops == 0);

    /* SET 覆盖同一个 key：旧值必须先释放，否则每次覆盖漏一份 */
    ProbeValue replacement;
    probe_construct(&replacement, 6);
    PROBE(sa_dict_set(dict, "k", &replacement));
    PROBE(sa_dict_length(dict) == 1);
    PROBE(probe_drops == 1);

    ProbeValue taken;
    memset(&taken, 0, sizeof(taken));
    PROBE(sa_dict_take(dict, "k", &taken));
    PROBE(taken.payload == 6);
    PROBE(sa_dict_length(dict) == 0);
    /* TAKE 之后槽位已清零：容器析构不能再 drop 一次 */
    long drops_after_take = probe_drops;
    PROBE(sa_dict_close_owned(&dict) == 1);
    PROBE(probe_drops == drops_after_take);

    /* REMOVE 则是「容器替你释放」 */
    SaHandle other = sa_dict_new(&probe_ops);
    ProbeValue third;
    probe_construct(&third, 8);
    PROBE(sa_dict_set(other, "gone", &third));
    long before = probe_drops;
    PROBE(sa_dict_remove(other, "gone"));
    PROBE(probe_drops == before + 1);
    PROBE(sa_dict_length(other) == 0);
    PROBE(sa_dict_close_owned(&other) == 1);
    printf("DICTVAL-OK copies=%ld drops=%ld\n", probe_copies, probe_drops);
    return 0;
}
'''


_ELEMENT_OPS_PROBE = _COUNTING_OPS + r'''
int main(void) {
    SaHandle vec = sa_vec_new(&probe_ops);
    PROBE(vec != 0);
    ProbeValue a, b, c;
    probe_construct(&a, 1);
    probe_construct(&b, 2);
    probe_construct(&c, 3);

    /* PUSH 复制：调用方那份不动 */
    PROBE(sa_vec_push(vec, &a));
    PROBE(sa_vec_push(vec, &b));
    PROBE(sa_vec_push(vec, &c));
    PROBE(probe_copies == 3);
    PROBE(a.tag == 1 && b.tag == 2 && c.tag == 3);

    /* INSERT 复制并后移；GET 也是复制——调用方拿到一份独立的值 */
    ProbeValue d;
    probe_construct(&d, 4);
    PROBE(sa_vec_insert(vec, 1, &d));
    PROBE(sa_vec_length(vec) == 4);
    ProbeValue got;
    memset(&got, 0, sizeof(got));
    long copies_before = probe_copies;
    PROBE(sa_vec_get(vec, 1, &got));
    PROBE(got.payload == 4);
    PROBE(probe_copies == copies_before + 1);
    probe_drop(&got);   /* GET 给的是我们自己的一份，收尾也归我们 */

    /* SET：旧值释放一次、新值复制一次，不泄漏也不多放 */
    long drops_before = probe_drops;
    ProbeValue e;
    probe_construct(&e, 9);
    PROBE(sa_vec_set(vec, 1, &e));
    PROBE(probe_drops == drops_before + 1);
    memset(&got, 0, sizeof(got));
    PROBE(sa_vec_get(vec, 1, &got));
    PROBE(got.payload == 9);

    /* TAKE：值转出容器，尾槽清零，析构时不再释放它 */
    long drops_at_take = probe_drops;
    memset(&got, 0, sizeof(got));
    PROBE(sa_vec_take(vec, 1, &got));
    PROBE(got.tag == 9 && got.payload == 9);
    PROBE(probe_drops == drops_at_take);

    /* 把转出的值再 PUSH 回去，然后清空：三个副本各释放一次 */
    PROBE(sa_vec_push(vec, &got));
    PROBE(sa_vec_clear(vec) == 1);
    PROBE(sa_vec_length(vec) == 0);
    /* 清空后元素没了，但容器还活着（ops 还在），可以继续用 */
    PROBE(sa_vec_push(vec, &a));
    PROBE(sa_vec_length(vec) == 1);
    PROBE(sa_vec_close_owned(&vec) == 1);

    /* 全程没出现重复释放，而且每次释放都恰好对应一次放进容器的复制 */
    PROBE(probe_double_drops == 0);
    PROBE(probe_drops == probe_copies - 2);   /* 探针自己留着 a、e 两份额外持有 */
    printf("ELEMOPS-OK copies=%ld drops=%ld double=%ld\n",
           probe_copies, probe_drops, probe_double_drops);
    return 0;
}
'''


_MOVE_ONLY_PROBE = _COUNTING_OPS + r'''
/* 移动专用表：copyable = 0，copy 为 NULL。托管句柄用的就是这种表。 */
static const SaValueOps move_only_ops = { sizeof(SaHandle), 0, NULL, sa_file_drop_value };

int main(void) {
    SaHandle vec = sa_vec_new(&move_only_ops);
    PROBE(vec != 0);
    SaHandle owned = sa_file_open("move-only.bin", "WRITE");
    PROBE(owned != 0);
    SaHandle moved = owned;

    /* MOVE 形态放行：值搬进容器，源被清零 */
    PROBE(sa_vec_push_move(vec, &owned));
    PROBE(owned == 0);
    PROBE(sa_vec_length(vec) == 1);

    /* 复制形态一律拒绝——静态检查是主防线，这里是安全网 */
    SaHandle dup = sa_file_open("dup.bin", "WRITE");
    PROBE(!sa_vec_push(vec, &dup));
    PROBE(!sa_vec_insert(vec, 0, &dup));
    PROBE(!sa_vec_set(vec, 0, &dup));
    PROBE(strstr(sa_container_last_error, "move-only") != NULL);
    sa_file_drop(dup);

    /* 借用拿到的是元素地址，不是副本；容器析构时由 ops->drop 收掉 */
    SaHandle* borrowed = (SaHandle*)sa_vec_borrow(vec, 0);
    PROBE(borrowed != NULL);
    PROBE(*borrowed == moved);
    PROBE(sa_file_slot(moved) != NULL);

    PROBE(sa_vec_close_owned(&vec) == 1);
    PROBE(sa_file_slot(moved) == NULL);
    sa_file_drop(moved);

    /* 嵌套容器同理：子容器句柄只能移动，析构转交给子容器自己的 drop */
    SaHandle outer = sa_vec_new(&sa_ops_vec);
    SaHandle inner = sa_vec_new(&sa_ops_long);
    PROBE(outer != 0 && inner != 0);
    long long number = 11;
    PROBE(sa_vec_push(inner, &number));
    SaHandle moved_inner = inner;
    PROBE(sa_vec_push_move(outer, &inner));
    PROBE(inner == 0);
    PROBE(sa_vec_length(outer) == 1);
    PROBE(sa_vec_length(moved_inner) == 1);
    PROBE(sa_vec_close_owned(&outer) == 1);
    PROBE(sa_vec_length(moved_inner) == -1);   /* 子容器随外层析构一起回收 */
    printf("MOVEONLY-OK\n");
    return 0;
}
'''


_SCALAR_PROBE = r'''
int main(void) {
    SaHandle vec = sa_vec_new(&sa_ops_long);
    PROBE(vec != 0);
    PROBE(sa_vec_length(vec) == 0);
    /* 空容器不预分配：data/capacity 都是 0，第一次插入才 malloc */
    PROBE(sa_vec_slots[0].data == NULL);
    PROBE(sa_vec_slots[0].capacity == 0);

    for (long long i = 0; i < 100; ++i) {
        long long value = i * 3;
        PROBE(sa_vec_push(vec, &value));
    }
    PROBE(sa_vec_length(vec) == 100);
    for (long long i = 0; i < 100; ++i) {
        long long out = -1;
        PROBE(sa_vec_get(vec, i, &out));
        PROBE(out == i * 3);
    }

    /* 越界一律「报错 + 返回假」，不是越界写 */
    long long scratch = 0;
    PROBE(!sa_vec_get(vec, 100, &scratch));
    PROBE(strstr(sa_container_last_error, "out of range") != NULL);
    PROBE(!sa_vec_get(vec, -1, &scratch));
    PROBE(!sa_vec_set(vec, 100, &scratch));
    PROBE(!sa_vec_take(vec, 100, &scratch));
    PROBE(!sa_vec_remove(vec, 100));
    PROBE(sa_vec_borrow(vec, 100) == NULL);
    PROBE(sa_vec_length(vec) == 100);          /* 失败的调用一个元素都没动 */

    /* INSERT 允许插在末尾（index == length），越一位就不行 */
    long long tail = 999;
    PROBE(sa_vec_insert(vec, 100, &tail));
    PROBE(sa_vec_length(vec) == 101);
    PROBE(!sa_vec_insert(vec, 102, &tail));
    PROBE(sa_vec_length(vec) == 101);

    PROBE(sa_vec_close_owned(&vec) == 1);
    PROBE(sa_vec_length(vec) == -1);
    printf("SCALAR-OK\n");
    return 0;
}
'''


@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_container_drop_is_generation_safe_and_preserves_errors(opt: str) -> None:
    """容器析构和句柄析构同一条契约：认过期 token，且不动 LAST_ERROR / errno。

    容器这里还多一层：`sa_vec_slot` 用 `ops != NULL` 当存活标志，析构时必须把它清掉——
    只 free 不置空的话，同一个槽位会被认成「活的空容器」，长度查询会安静地返回 0。
    """
    result = _run_probe(_DROP_PROBE, opt)
    assert result.returncode == 0, result.stderr
    assert "DROP-OK copies=1 drops=1 double=0" in result.stdout


@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_dict_rehashes_and_owns_its_keys(opt: str) -> None:
    """key 由 dict 复制（调用方缓冲区复用后仍能查回来），扩容后链表要重挂。"""
    result = _run_probe(_DICT_KEY_PROBE, opt)
    assert result.returncode == 0, result.stderr
    assert "DICT-OK drops=65 copies=130" in result.stdout


@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_dict_values_follow_the_same_ownership_rules(opt: str) -> None:
    """SET 覆盖释放旧值、TAKE 转出后槽位清零、REMOVE 由容器释放。"""
    result = _run_probe(_DICT_VALUE_PROBE, opt)
    assert result.returncode == 0, result.stderr
    assert "DICTVAL-OK copies=3 drops=2" in result.stdout


@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_element_ops_govern_copy_move_and_release(opt: str) -> None:
    """copy / drop 各恰好一次：PUSH 复制、GET 不复制、SET 先释放旧值、TAKE 转出。"""
    result = _run_probe(_ELEMENT_OPS_PROBE, opt)
    assert result.returncode == 0, result.stderr
    assert "ELEMOPS-OK copies=9 drops=7 double=0" in result.stdout


@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_move_only_elements_refuse_the_copy_interface(opt: str) -> None:
    """copyable = 0 的表只开放 MOVE 形态；嵌套容器的析构转交子容器自己的 drop。"""
    result = _run_probe(_MOVE_ONLY_PROBE, opt)
    assert result.returncode == 0, result.stderr
    assert "MOVEONLY-OK" in result.stdout


@pytest.mark.parametrize("opt", ["-O0", "-O2"])
def test_scalar_container_grows_and_bounds_check(opt: str) -> None:
    """按需增长到 100 个元素仍能逐格取回，越界一律返回假且不改状态。"""
    result = _run_probe(_SCALAR_PROBE, opt)
    assert result.returncode == 0, result.stderr
    assert "SCALAR-OK" in result.stdout
