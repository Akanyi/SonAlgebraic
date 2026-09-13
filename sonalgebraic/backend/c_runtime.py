"""SonAlgebraic C 运行时的加载器。

运行时本体是一对真 C 文件（backend/runtime/sa_runtime.{h,c}）：clang 能单独编、
clangd 能索引、clang-format 能格式化。以前这坨 ~4600 行 C 内联成本文件里的 Python
三引号字符串，IDE 只当它是一段文本，改运行时全靠肉眼加全量重编。

这里在 import 时把那对文件读回来，还原成下游一直在用的五个常量。所有消费方（codegen、
slicer、native 驱动、模块编译、.slib 打包）只认这些常量的值，不关心它们来自字面量还是磁盘。

**单文件模式与分离编译共用同一段前导**（平台头、跳转宏、公共类型）：以前头文件里另抄了
一份，两份手工同步的结果就是 signal.h、NI_MAXHOST 兜底、gtk 头漂移丢失。现在前导只在
sa_runtime.h 里存一份、用一对 marker 注释圈出来，本模块再从中切出 RUNTIME_PRELUDE——
单一事实源，不会再漂。
"""
from __future__ import annotations

from importlib.resources import files

# sa_runtime.h 里圈住前导的 marker（生成时插入、加载时剥掉），必须与文件里的字面逐字一致。
_PRELUDE_BEGIN = "/* >>> SA_RUNTIME_PRELUDE_BEGIN（单文件 RUNTIME 与本头共用，勿删此标记） */\n"
_PRELUDE_END = "/* <<< SA_RUNTIME_PRELUDE_END */\n"
# sa_runtime.c 的头一行：分离编译时实现要 include 头，单文件模式不需要，加载时剥掉。
_IMPL_INCLUDE = '#include "sa_runtime.h"\n'


def _read(name: str) -> str:
    # 用 importlib.resources 而非 __file__：wheel、editable、PyInstaller 冻结态都能定位包内资源。
    # 默认文本模式读 = universal newline，把 Windows 检出的 CRLF 归一回 LF，对齐历史字面量的字节。
    return (files("sonalgebraic.backend") / "runtime" / name).read_text(encoding="utf-8")


_HEADER_RAW = _read("sa_runtime.h")
_IMPL_RAW = _read("sa_runtime.c")

# 前导：两个 marker 之间那段，就是当年内联的 RUNTIME_PRELUDE 本体。
RUNTIME_PRELUDE = _HEADER_RAW.split(_PRELUDE_BEGIN, 1)[1].split(_PRELUDE_END, 1)[0]
# 头文件：只抹掉两行 marker，其余一字不动。marker 是注释、剥不剥都不影响编译，抹掉纯粹是为了
# 让 RUNTIME_HEADER 跟历史字节完全一致，结构断言（PRELUDE in HEADER 等）才恒真。
RUNTIME_HEADER = _HEADER_RAW.replace(_PRELUDE_BEGIN, "").replace(_PRELUDE_END, "")
# 实现：磁盘上的 sa_runtime.c 是「include 头 + 去 static 的实现」。剥掉 include 行就是纯实现体。
# 注意它已去 static——这是外部化带来的唯一语义变化：真 C 工程的 .c 要既 include 头又能单独编译，
# 就不能带 static（否则「static 声明跟在非 static 声明后」编不过），符号改走外部链接。
RUNTIME_IMPL = _IMPL_RAW[len(_IMPL_INCLUDE):] if _IMPL_RAW.startswith(_IMPL_INCLUDE) else _IMPL_RAW

# 单文件模式：前导直接拼实现，塞进一个 .c 编。
RUNTIME = RUNTIME_PRELUDE + RUNTIME_IMPL
# 分离编译模式：前导由 sa_runtime.h 提供，这里只要实现。RUNTIME_IMPL 已去 static，这步 replace
# 现在是幂等 no-op——保留它，是让 module_compiler / native 驱动那两处同样的 .replace("static ","")
# 一致成立，「去 static」这个不变量在所有调用点写法统一，谁都不用改。
RUNTIME_SOURCE = RUNTIME_IMPL.replace("static ", "")
