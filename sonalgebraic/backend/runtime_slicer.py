"""把 C 运行时切成可按需注入的片段。

以前的做法是把整份 RUNTIME 文本塞进生成的 .c，靠 `#ifdef SA_ENABLE_*` 让预处理器
裁掉不用的部分。这砍掉了编译量，却没砍掉文本量——一个 `PRINT "hello"` 生成的 .c
里 98.6% 是运行时，其中光 SYMBOL 代数就 300 行，而那个程序根本没有 SYMBOL。

这里把 RUNTIME_IMPL 拆成片段，让调用方只取实际够得着的那些。两种粒度并存：

- feature 区（NET/FILE/LIST/MAP/BINARY/DESKTOP/GUI）按**整块**取舍，不拆函数。
  这些块里藏着切分器啃不动的东西：BINARY 的 12 个 pack/unpack 是 `SA_BIN_PACK_FN(...)`
  宏展开出来的，没有可扫描的定义行；FILE 的 `sa_stricmp_ascii` 只通过
  `#define _stricmp` 被引用，可达性分析必然把它判成死代码；NET/TLS 有四个函数
  按 `#ifdef _WIN32` 给了 Win 和 POSIX 两份完整实现，符号到片段是一对多。
  整块取舍把这些坑全绕开了，而收益几乎没损失——这些块本来就是全有或全无。

- 无条件区（约 640 行）按函数切。这部分结构规整：一律 static、空行分隔、右花括号
  在第 0 列，没有上面那些花样。

片段一律按原始行号输出，不做拓扑重排。这条保证了不会引入原文件没有的前向引用：
子集的相对顺序和原文件一致，而原文件本身编得过。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re

from .c_runtime import RUNTIME_IMPL, RUNTIME_PRELUDE

# 运行时内部符号的两种命名：函数/变量用 sa_xxx，宏和枚举常量用 SA_XXX
_SYMBOL_RE = re.compile(r"\b(sa_[a-z0-9_]+|SA_[A-Z0-9_]+)\b")
_FEATURE_RE = re.compile(r"SA_ENABLE_([A-Z_]+)")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")

# feature 宏名 -> runtime_features_for_program() 用的小写 feature 名。
# GUI_GTK 不在此列：它不是程序特性，是构建期对宿主 GTK 的探测结果，
# 永远嵌在 GUI 块内部，不单独成块。
_FEATURE_NAMES = {
    "NET": "net",
    "TLS": "tls",
    "FILE": "file",
    "DESKTOP": "desktop",
    "BINARY": "binary",
    "LIST": "list",
    "MAP": "map",
    "GUI": "gui",
}


@dataclass(frozen=True)
class Fragment:
    """运行时里一段可独立取舍的代码。"""

    name: str
    text: str
    start: int
    # 这段定义了哪些符号。函数/变量各自一个，enum 是它全部的常量。
    provides: frozenset[str]
    # 这段引用了哪些运行时符号（已排除自己 provides 的和 PRELUDE 提供的）
    depends: frozenset[str]
    # 非空表示这是 feature 块，集合里任一 feature 启用就要它（BINARY|NET 那块是两个）
    features: frozenset[str]


def _strip_comments(text: str) -> str:
    """依赖提取前必须剥注释。

    这个文件注释密度很高且大量提及符号名——比如 sa_str_upper 上方的注释写着
    「sa_binary_range 同样写法」，不剥的话扫到这个名字就会把整个 BINARY 区
    拖进闭包，按需注入直接失效。
    """
    return _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", text))


def _symbols_in(text: str) -> set[str]:
    # SA_ENABLE_* 是 codegen 发的 feature 宏，不是运行时定义的符号，
    # 留在依赖里会永远找不到提供者。
    return {name for name in _SYMBOL_RE.findall(_strip_comments(text)) if not name.startswith("SA_ENABLE_")}


def _defined_globals(text: str) -> set[str]:
    """一段代码里定义的顶层 static 变量。

    每个 feature 块都有自己的槽位数组和错误缓冲（sa_list_slots、
    sa_net_last_error…），它们只被块内引用。不算进 provides 的话，
    整块输出的片段会显示成一堆悬空依赖。
    """
    return set(re.findall(r"^static\s+[\w\s\*]*?\b(sa_[a-z0-9_]+)\s*(?:\[|=|;)", _strip_comments(text), re.MULTILINE))


@lru_cache(maxsize=1)
def prelude_symbols() -> frozenset[str]:
    """PRELUDE 提供的符号。

    PRELUDE 整块保留，所以它定义的东西（SA_SETJMP、SA_SYM_CONST 等）不参与
    依赖闭包，否则会去片段表里找一个根本不在那儿的定义。
    """
    return frozenset(_symbols_in(RUNTIME_PRELUDE))


def _brace_delta(line: str) -> int:
    """数一行里的大括号净增量，跳过字符串和字符字面量里的。"""
    depth = 0
    index = 0
    length = len(line)
    while index < length:
        char = line[index]
        if char == "\\":
            index += 2
            continue
        if char in "\"'":
            quote = char
            index += 1
            while index < length:
                if line[index] == "\\":
                    index += 2
                    continue
                if line[index] == quote:
                    break
                index += 1
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return depth


def _fragment_name(text: str) -> str:
    """从片段首行认出它定义了什么。"""
    head = _strip_comments(text).lstrip()
    match = re.match(r"(?:static\s+)?[A-Za-z_][\w\s\*]*?\b(sa_[a-z0-9_]+)\s*[\(\[=;]", head)
    if match:
        return match.group(1)
    match = re.search(r"\b(sa_[a-z0-9_]+|SA_[A-Z0-9_]+)\b", head)
    return match.group(1) if match else "anonymous"


def _split_unconditional(lines: list[str], start: int, end: int) -> list[Fragment]:
    """把无条件区按顶层构造切开。start/end 是 0-based 半开区间。"""
    fragments: list[Fragment] = []
    index = start
    while index < end:
        if not lines[index].strip():
            index += 1
            continue

        first = index
        line = lines[index]

        # 形态 A：整个函数被 #ifdef _WIN32 之类包着（sa_win_widen / sa_win_narrow）。
        # 必须连 guard 一起切出去，只切函数体的话输出就少了半边条件编译。
        # 块里可能有多个函数，整块当一个片段——多带一个函数无害，漏掉 guard 才致命。
        if line.startswith("#if"):
            depth = 1
            index += 1
            while index < end and depth:
                stripped = lines[index]
                if stripped.startswith(("#if", "#ifdef", "#ifndef")):
                    depth += 1
                elif stripped.startswith("#endif"):
                    depth -= 1
                index += 1
        else:
            braces = _brace_delta(line)
            index += 1
            # 单行就闭合的（`static int x = 0;`、单行函数体）直接收尾；
            # 否则一路数到大括号平衡。
            while index < end and (braces > 0 or not _closes(lines[first:index])):
                braces += _brace_delta(lines[index])
                index += 1
                if braces <= 0 and _closes(lines[first:index]):
                    break

        text = "\n".join(lines[first:index])
        provides = _provides_of(text)
        fragments.append(
            Fragment(
                name=_fragment_name(text),
                text=text,
                start=first + 1,
                provides=frozenset(provides),
                depends=frozenset(_symbols_in(text) - provides - prelude_symbols()),
                features=frozenset(),
            )
        )
    return fragments


def _closes(chunk: list[str]) -> bool:
    """这段文本是不是一个完整的顶层构造。"""
    text = _strip_comments("\n".join(chunk)).rstrip()
    if not text:
        return False
    return text.endswith((";", "}"))


def _provides_of(text: str) -> set[str]:
    """片段定义了哪些符号。

    函数和变量各自一个名字；enum 要把全部常量都算上——SA_HANDLE_* 那 11 个
    常量被所有句柄类 feature 引用，漏了它们闭包就会去找一个不存在的定义。
    """
    clean = _strip_comments(text)
    if clean.lstrip().startswith("enum"):
        return set(re.findall(r"\b(SA_[A-Z0-9_]+)\b", clean))
    if clean.lstrip().startswith("#"):
        # 平台 guard 块：块里定义的函数全算它提供的
        return set(re.findall(r"^(?:static\s+)?[A-Za-z_][\w\s\*]*?\b(sa_[a-z0-9_]+)\s*\(", clean, re.MULTILINE))
    return {_fragment_name(text)}


@lru_cache(maxsize=1)
def fragments() -> tuple[Fragment, ...]:
    """把 RUNTIME_IMPL 解析成片段表。模块级缓存，只算一次。"""
    lines = RUNTIME_IMPL.split("\n")
    result: list[Fragment] = []
    plain_start = 0
    index = 0

    while index < len(lines):
        line = lines[index]
        # 顶层 feature 块的开头。注意不能只看第 0 列的 #ifdef 就配对：
        # 块内部还有 #ifdef _WIN32 这类嵌套（NET 块里就有十几处），必须数深度。
        if line.startswith("#if") and "SA_ENABLE_" in line:
            names = {_FEATURE_NAMES[name] for name in _FEATURE_RE.findall(line) if name in _FEATURE_NAMES}
            if names:
                result.extend(_split_unconditional(lines, plain_start, index))
                first = index
                depth = 1
                index += 1
                while index < len(lines) and depth:
                    current = lines[index]
                    if current.startswith(("#if", "#ifdef", "#ifndef")):
                        depth += 1
                    elif current.startswith("#endif"):
                        depth -= 1
                    index += 1
                text = "\n".join(lines[first:index])
                clean = _strip_comments(text)
                provides = set(re.findall(r"^(?:static\s+)?[A-Za-z_][\w\s\*]*?\b(sa_[a-z0-9_]+)\s*\(", clean, re.MULTILINE))
                provides |= set(re.findall(r"^#define\s+(sa_[a-z0-9_]+|SA_[A-Z0-9_]+)", clean, re.MULTILINE))
                provides |= set(re.findall(r"^\s*(SA_[A-Z0-9_]+)\s*=", clean, re.MULTILINE))
                provides |= _defined_globals(text)
                # 宏展开生成的函数：SA_BIN_PACK_FN(sa_binary_pack_u16_le, 2, 0)
                provides |= set(re.findall(r"^SA_BIN_(?:UN)?PACK_FN\((sa_[a-z0-9_]+)", clean, re.MULTILINE))
                result.append(
                    Fragment(
                        name=f"feature:{'|'.join(sorted(names))}@{first + 1}",
                        text=text,
                        start=first + 1,
                        provides=frozenset(provides),
                        depends=frozenset(_symbols_in(text) - provides - prelude_symbols()),
                        features=frozenset(names),
                    )
                )
                plain_start = index
                continue
        index += 1

    result.extend(_split_unconditional(lines, plain_start, len(lines)))
    return tuple(sorted(result, key=lambda fragment: fragment.start))


@lru_cache(maxsize=1)
def _provider_index() -> dict[str, tuple[Fragment, ...]]:
    """符号 -> 提供它的片段。

    一个符号可能有多个提供者：NET/TLS 区里 sa_net_tls_handshake 这类函数
    按 #ifdef _WIN32 给了两份完整实现。这里返回全部，选的时候一并带上，
    因为切分发生在编译前，我们并不知道最终目标平台是哪个。
    """
    index: dict[str, list[Fragment]] = {}
    for fragment in fragments():
        for symbol in fragment.provides:
            index.setdefault(symbol, []).append(fragment)
    return {symbol: tuple(items) for symbol, items in index.items()}


def select_fragments(roots: set[str], features: set[str]) -> list[Fragment]:
    """从根符号出发算依赖闭包，返回按原始行序排好的片段。

    根集合有两个来源，第二个是正确性关键：被选中的 feature 块本身也要当根扫一遍。
    这些块整块注入，而它们大量引用无条件区的东西——NET/FILE/DESKTOP/GUI 都调
    sa_win_widen，所有句柄类 feature 都用 SA_HANDLE_* 枚举，几乎每块都用 sa_strdup。
    漏掉这一步，程序一 USE SYS.FILE 就编不过。
    """
    index = _provider_index()
    selected: dict[int, Fragment] = {}
    pending = set(roots)

    for fragment in fragments():
        if fragment.features and fragment.features & features:
            selected[fragment.start] = fragment
            pending |= set(fragment.depends)

    seen: set[str] = set()
    while pending:
        symbol = pending.pop()
        if symbol in seen:
            continue
        seen.add(symbol)
        for provider in index.get(symbol, ()):
            # feature 块只由 features 决定去留，不会被符号引用拽进来：
            # 没启用 SYS.NET 却因为撞名把 1600 行 NET 代码拖进来就本末倒置了。
            if provider.features:
                continue
            if provider.start not in selected:
                selected[provider.start] = provider
                pending |= set(provider.depends)

    return [selected[start] for start in sorted(selected)]


def runtime_impl_for(roots: set[str], features: set[str]) -> str:
    """按需拼出 RUNTIME_IMPL 的子集。"""
    return "\n".join(fragment.text for fragment in select_fragments(roots, features))


def runtime_symbols_in(text: str) -> set[str]:
    """从生成的 C 代码里挑出用到的运行时符号，作为闭包的根。"""
    return _symbols_in(text)
