from __future__ import annotations

from dataclasses import dataclass
import re

from .errors import SonCompileError


_LINE_RE = re.compile(r"^\s*(\d+)(?:\s+(.*))?$")
_LINT_USE_RE = re.compile(r"^USE\s+SYS\.LINT\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)$", re.IGNORECASE)
LINT_OPTIONS = frozenset({"NONE_NUMBER"})


def strip_trailing_comment(text: str) -> str:
    """剥离行尾 REM 注释，返回语句部分（已 rstrip）。

    REM 标记「该行剩余部分」为注释，可出现在语句之后（如 `x = 1 REM 说明`）。
    扫描时必须跳过字符串字面量内部的 REM（如 `PRINT "PREMIUM"`），且只有当
    REM 作为独立单词出现（前为行首/空白、后为行尾/空白）时才视为注释起点，
    避免误伤 `PREMIUM`、`REMOTE` 这类标识符。整行注释（行首即 REM）剥离后
    返回空串，由上层按空行跳过。
    """
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in {'"', "'"}:
            # 跳过字符串字面量，与 expr_lexer 的转义规则一致（反斜杠转义）
            quote = ch
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        # REM 必须是独立单词：前一个字符是行首或空白
        if (ch in {"R", "r"}
                and text[i:i + 3].upper() == "REM"
                and (i == 0 or text[i - 1].isspace())):
            after = text[i + 3:i + 4]
            if after == "" or after.isspace():
                return text[:i].rstrip()
        i += 1
    return text



@dataclass(frozen=True)
class SourceLine:
    no: int
    text: str
    physical_no: int


def statement_body(raw: str) -> str:
    """去掉可选行号和行尾 REM 后的语句正文。"""
    text = strip_trailing_comment(raw.strip())
    match = _LINE_RE.match(text)
    if match:
        return (match.group(2) or "").strip()
    return text


def detect_lint_options(source: str) -> set[str]:
    """扫描源码中的 `USE SYS.LINT AS <option>`，不要求该行本身已有行号。"""
    options: set[str] = set()
    for physical_no, raw in enumerate(source.splitlines(), 1):
        if not raw.strip():
            continue
        body = statement_body(raw)
        if not body:
            continue
        match = _LINT_USE_RE.match(body)
        if match is None:
            continue
        option = match.group(1).upper()
        if option not in LINT_OPTIONS:
            known = ", ".join(sorted(LINT_OPTIONS))
            raise SonCompileError(f"未知 SYS.LINT 选项: {option}；当前支持: {known}", physical_no)
        options.add(option)
    return options


def apply_lint_source(source: str, step: int = 10, start: int = 10) -> str:
    """按 lint 选项改写源码。NONE_NUMBER 会在编译前为所有非空行自动补行号。"""
    options = detect_lint_options(source)
    if "NONE_NUMBER" not in options:
        return source
    if step <= 0 or start <= 0:
        raise SonCompileError("自动行号的步长和起始值必须是正整数")

    out: list[str] = []
    next_no = start
    for raw in source.splitlines():
        if not raw.strip():
            out.append("")
            continue
        body = statement_body(raw)
        out.append(f"{next_no} {body}" if body else str(next_no))
        next_no += step

    result = "\n".join(out)
    if source.endswith(("\n", "\r")):
        result += "\n"
    return result


def read_numbered_lines(source: str) -> list[SourceLine]:
    source = apply_lint_source(source)
    lines: list[SourceLine] = []
    previous_no = 0

    for physical_no, raw in enumerate(source.splitlines(), 1):
        if not raw.strip():
            continue

        match = _LINE_RE.match(raw)
        if not match:
            raise SonCompileError(
                "每一行都必须以递增的正整数行号开头，后面跟一个空格；"
                "若要省略行号，请先写 `USE SYS.LINT AS NONE_NUMBER`",
                physical_no,
            )

        line_no = int(match.group(1))
        if line_no <= 0:
            raise SonCompileError("行号必须是正整数", physical_no)
        if line_no <= previous_no:
            raise SonCompileError("行号必须严格递增", line_no)

        previous_no = line_no
        body = strip_trailing_comment((match.group(2) or "").strip())
        lines.append(SourceLine(line_no, body, physical_no))

    return lines
