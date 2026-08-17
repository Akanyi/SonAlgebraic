from __future__ import annotations

from dataclasses import dataclass

from ..core.errors import SonCompileError


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    pos: int


_TWO_CHAR_OPS = {"<=", ">=", "<>", "!=", "==", "**"}
_ONE_CHAR_OPS = set("+-*/%(),=<>^@[]")

# `{`/`}` 映射到自身，让 F-string 的 `\{` 和普通字符串保持同一张表
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'", "{": "{", "}": "}"}


def tokenize_expr(text: str, line_no: int) -> list[Token]:
    tokens: list[Token] = []
    i = 0

    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
            continue

        if ch in {'"', "'"}:
            start = i
            value, i = _read_string(text, i, line_no)
            tokens.append(Token("STRING", value, start))
            continue

        if ch in {"F", "f"} and i + 1 < len(text) and text[i + 1] in {'"', "'"}:
            start = i
            value, i = _read_fstring(text, i + 1, line_no)
            tokens.append(Token("FSTRING", value, start))
            continue

        if ch.isdigit():
            start = i
            # 十六进制：0x / 0X 前缀，后跟 hex 数字与下划线分隔符
            if ch == "0" and i + 1 < len(text) and text[i + 1] in {"x", "X"}:
                i += 2
                while i < len(text) and (text[i] in "0123456789abcdefABCDEF_"):
                    i += 1
                tokens.append(Token("NUMBER", text[start:i], start))
                continue
            # 十进制整数/小数部分，允许下划线分隔
            i += 1
            while i < len(text) and (text[i].isdigit() or text[i] in "._"):
                i += 1
            # 科学计数法：e/E 后可带符号
            if i < len(text) and text[i] in {"e", "E"}:
                i += 1
                if i < len(text) and text[i] in {"+", "-"}:
                    i += 1
                while i < len(text) and (text[i].isdigit() or text[i] == "_"):
                    i += 1
            tokens.append(Token("NUMBER", text[start:i], start))
            continue

        if ch.isalpha() or ch == "_":
            start = i
            i += 1
            while i < len(text) and (text[i].isalnum() or text[i] in "_."):
                i += 1
            word = text[start:i]
            # 词法层挡住 `y.` / `a..b`：放过去的话语义层只能拼出字段名是空串的诊断
            if word.endswith(".") or ".." in word:
                raise SonCompileError(f"标识符中的成员路径不完整: {word}", line_no)
            tokens.append(Token("IDENT", word, start))
            continue

        two = text[i : i + 2]
        if two in _TWO_CHAR_OPS:
            tokens.append(Token("OP", two, i))
            i += 2
            continue

        if ch in _ONE_CHAR_OPS:
            tokens.append(Token("OP", ch, i))
            i += 1
            continue

        raise SonCompileError(f"无法识别的表达式字符: {ch}", line_no)

    tokens.append(Token("EOF", "", len(text)))
    return tokens


def find_interp_end(value: str, open_index: int, line_no: int) -> int:
    """返回 value[open_index] 这个 `{` 所对应的 `}` 下标。

    插值表达式里可以出现字符串字面量，字符串内部的 `}` 不能算插值结束；嵌套花括号
    同理。直接 find("}") 会在错误的位置断开，报出来的错和真实原因完全对不上。
    """
    depth = 0
    i = open_index
    while i < len(value):
        ch = value[i]
        if ch in {'"', "'"}:
            end = _scan_string_end(value, i)
            if end is None:
                # 插值里的字符串没闭合，说明这个 `{` 压根没等到自己的 `}`
                raise SonCompileError("F-string 缺少右花括号", line_no)
            i = end
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise SonCompileError("F-string 缺少右花括号", line_no)


def _read_string(text: str, start: int, line_no: int) -> tuple[str, int]:
    quote = text[start]
    i = start + 1
    out: list[str] = []

    while i < len(text):
        ch = text[i]
        if ch == quote:
            return "".join(out), i + 1
        if ch == "\\":
            i += 1
            if i >= len(text):
                raise SonCompileError("字符串转义不完整", line_no)
            out.append(_escape_char(text[i], line_no))
        else:
            out.append(ch)
        i += 1

    raise SonCompileError("字符串缺少结束引号", line_no)


def _read_fstring(text: str, start: int, line_no: int) -> tuple[str, int]:
    """读取 F-string 字面量，插值段原样保留。

    不能直接复用 _read_string：一是它不跟踪花括号，插值里出现同款引号就会把字面量
    在半截截断；二是它会展开转义，而插值内容随后还要交给 parse_expr 再解析一次，
    转义展开两遍会把插值里的反斜杠写法弄坏。所以这里只对花括号外的部分做转义展开，
    花括号内一律逐字搬运（只跟踪引号和花括号配平）。
    """
    quote = text[start]
    i = start + 1
    out: list[str] = []
    depth = 0

    while i < len(text):
        ch = text[i]

        if depth > 0:
            # 插值内部：逐字搬运，字符串整段跳过，避免里面的引号和 `}` 干扰配平
            if ch in {'"', "'"} or (ch == "\\" and i + 1 < len(text) and text[i + 1] in {'"', "'"}):
                scanned = _scan_interp_string(text, i)
                if scanned is None:
                    # 撞上的多半是 F-string 自己的收尾引号，真正缺的是 `}`
                    raise SonCompileError("F-string 缺少右花括号", line_no)
                chunk, i = scanned
                out.append(chunk)
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            out.append(ch)
            i += 1
            continue

        if ch == quote:
            return "".join(out), i + 1
        if ch == "\\":
            i += 1
            if i >= len(text):
                raise SonCompileError("字符串转义不完整", line_no)
            expanded = _escape_char(text[i], line_no)
            # `\{` 展开成 `{` 会被后面的 parse_fstring 当成插值起点，重新写成 `{{`
            # 交给它去还原，用户才有除 `{{` 之外的第二种转义手段
            out.append(expanded * 2 if expanded in {"{", "}"} else expanded)
            i += 1
            continue
        if ch == "{":
            if i + 1 < len(text) and text[i + 1] == "{":
                out.append("{{")
                i += 2
                continue
            depth = 1
        out.append(ch)
        i += 1

    if depth > 0:
        raise SonCompileError("F-string 缺少右花括号", line_no)
    raise SonCompileError("字符串缺少结束引号", line_no)


def _scan_interp_string(text: str, start: int) -> tuple[str, int] | None:
    """扫描插值里的一段字符串字面量，返回 (规范化后的文本, 结束后下标)；没闭合返回 None。

    两种写法都得认：`"a"`，以及历史遗留的 `\\"a\\"`——旧版整个 F-string 走的是普通字符串
    读取器，插值里用同款引号必须先转义（examples/net_tls.sa 就是这么写的）。这里统一把
    那层多余的反斜杠摘掉，下游的 parse_fstring 和 parse_expr 只需要面对一种形态。
    """
    escaped = text[start] == "\\"
    quote_at = start + 1 if escaped else start
    quote = text[quote_at]
    out = [quote]
    i = quote_at + 1

    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            if escaped and text[i + 1] == quote:
                out.append(quote)
                return "".join(out), i + 2
            out.append(text[i : i + 2])
            i += 2
            continue
        if not escaped and ch == quote:
            out.append(quote)
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    return None


def _scan_string_end(text: str, start: int) -> int | None:
    """跳过 text[start] 开头的字符串字面量，返回结束引号之后的下标；没闭合返回 None。

    调用方都在 F-string 的插值里，缺引号和缺花括号是同一个错，报哪个由调用方决定。
    """
    quote = text[start]
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i + 1
        i += 1
    return None


def _escape_char(ch: str, line_no: int) -> str:
    value = _ESCAPES.get(ch)
    if value is None:
        # 静默吞掉反斜杠是最坏的处理方式：`"C:\data"` 会变成 `C:data`，用户毫无察觉
        raise SonCompileError(f"未知转义序列 `\\{ch}`，反斜杠本身请写成 `\\\\`", line_no)
    return value
