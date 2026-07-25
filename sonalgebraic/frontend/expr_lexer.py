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


def tokenize_expr(text: str, line_no: int) -> list[Token]:
    tokens: list[Token] = []
    i = 0

    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
            continue

        if ch in {'"', "'"}:
            value, i = _read_string(text, i, line_no)
            tokens.append(Token("STRING", value, i))
            continue

        if ch in {"F", "f"} and i + 1 < len(text) and text[i + 1] in {'"', "'"}:
            value, i = _read_string(text, i + 1, line_no)
            tokens.append(Token("FSTRING", value, i))
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
            tokens.append(Token("IDENT", text[start:i], start))
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
            escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'"}
            out.append(escapes.get(text[i], text[i]))
        else:
            out.append(ch)
        i += 1

    raise SonCompileError("字符串缺少结束引号", line_no)
