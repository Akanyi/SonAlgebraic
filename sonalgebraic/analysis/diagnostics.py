from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Iterable

from ..core.errors import SonCompileError
from ..core.lines import PHYSICAL_LINE_ATTR, apply_lint_source


_NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)(?:\s|$)")


@dataclass(frozen=True)
class Diagnostic:
    message: str
    # line 是 SA 逻辑行号（语言的一部分，用户写在源码里的那个数字）；
    # physical_line 是文件里的物理行号，编辑器跳转、problem matcher、quickfix 只认它。
    # 两者在带头部注释或空行的文件里从第一行就开始偏移，必须分开存。
    line: int | None = None
    column: int = 1
    severity: str = "error"
    length: int = 1
    # 错误来自依赖模块时，这两项指向那个模块的文件，渲染时优先于主文件
    origin_path: str | None = None
    origin_text: str | None = None
    physical_line: int | None = None

    @classmethod
    def from_compile_error(cls, error: SonCompileError, source_text: str | None = None) -> "Diagnostic":
        origin_path: str | None = getattr(error, "origin_path", None)
        origin_text: str | None = getattr(error, "origin_text", None)
        span_text: str = (origin_text or "") if origin_path else (source_text or "")
        # 行号本身还没建立起来时抛的错（lines.py）带的是物理行号，此时不存在 SA 行号
        physical_line: int | None = getattr(error, PHYSICAL_LINE_ATTR, None)
        sa_line = None if physical_line is not None else error.line_no
        if physical_line is None and sa_line is not None and span_text:
            physical_line = physical_line_for_sa(span_text, sa_line)
        column, length = infer_span(error, span_text, physical_line)
        return cls(
            message=error.message,
            line=sa_line,
            column=column,
            length=length,
            origin_path=origin_path,
            origin_text=origin_text,
            physical_line=physical_line,
        )


def diagnostic_from_compile_error(error: SonCompileError, source_text: str | None = None) -> Diagnostic:
    return Diagnostic.from_compile_error(error, source_text)


def physical_line_for_sa(source_text: str, sa_line: int) -> int | None:
    """SA 逻辑行号 → 文件物理行号（1-based），定位不到返回 None。"""
    lines = source_text.splitlines()
    for index, line in enumerate(lines, 1):
        match = _NUMBERED_LINE_RE.match(line)
        if match and int(match.group(1)) == sa_line:
            return index

    # NONE_NUMBER 源码里的 SA 行号是编译前自动补的，文件里根本不存在这些数字。
    # 直接跑一遍同一套补号逻辑再找：apply_lint_source 逐行改写，行数一一对应，
    # 命中行的下标就是原文件的物理行号。
    try:
        numbered = apply_lint_source(source_text)
    except SonCompileError:
        return None
    if numbered is source_text:
        return None
    for index, line in enumerate(numbered.splitlines(), 1):
        match = _NUMBERED_LINE_RE.match(line)
        if match and int(match.group(1)) == sa_line:
            return index
    return None


def infer_span(error: SonCompileError, source_text: str, physical_line: int | None = None) -> tuple[int, int]:
    raised_physical: int | None = getattr(error, PHYSICAL_LINE_ATTR, None)
    sa_line = None if raised_physical is not None else error.line_no
    source_line = _source_line_for_diagnostic(source_text, sa_line, physical_line or raised_physical)
    if source_line is None:
        return 1, 1

    candidates: list[str] = []
    message = error.message
    for prefix in ("变量未声明: ", "不能给 CONST 赋值: ", "未知 SUB 或 C 函数: ", "未知 SUB: ", "未知标签: "):
        if prefix in message:
            candidates.append(message.split(prefix, 1)[1].split()[0])
    if "无法解析的语句:" in message:
        candidates.append(message.split("无法解析的语句:", 1)[1].strip().split()[0])
    if "孤立的 `" in message:
        candidates.append(message.split("孤立的 `", 1)[1].split("`", 1)[0])

    for candidate in candidates:
        if not candidate:
            continue
        pos = source_line.find(candidate)
        if pos >= 0:
            return pos + 1, max(1, len(candidate))

    match = _NUMBERED_LINE_RE.match(source_line)
    if match:
        return min(len(source_line), match.end() + 1), max(1, len(source_line) - match.end())
    return 1, max(1, len(source_line))


def diagnostics_to_json(source_path: str | Path, diagnostics: Iterable[Diagnostic]) -> str:
    """机器可读输出。`line` 取物理行号（编辑器直接用），SA 行号另放 `sa_line`，
    两者都在，消费方不必知道本语言的行号规则也能正确跳转。"""
    payload = [
        {
            "file": diagnostic.origin_path or str(source_path),
            "line": diagnostic.physical_line if diagnostic.physical_line is not None else diagnostic.line,
            "sa_line": diagnostic.line,
            "column": max(1, diagnostic.column),
            "length": max(1, diagnostic.length),
            "severity": diagnostic.severity,
            "message": diagnostic.message,
        }
        for diagnostic in diagnostics
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_diagnostics(source_path: str | Path, source_text: str, diagnostics: Iterable[Diagnostic]) -> str:
    path_text = str(source_path)
    rendered: list[str] = []

    for diagnostic in diagnostics:
        if rendered:
            rendered.append("")
        rendered.extend(_render_diagnostic(path_text, source_text, diagnostic))

    return "\n".join(rendered)


def _render_diagnostic(source_path: str, source_text: str, diagnostic: Diagnostic) -> list[str]:
    # 依赖模块的错误要指回它自己的文件，否则行号会落在主文件的无关代码上
    if diagnostic.origin_path is not None:
        source_path = diagnostic.origin_path
        source_text = diagnostic.origin_text or ""
    lines = [_diagnostic_header(source_path, diagnostic)]
    source_line = _source_line_for_diagnostic(source_text, diagnostic.line, diagnostic.physical_line)
    if source_line is not None:
        lines.append(source_line)
        lines.append(_underline(source_line, diagnostic.column, diagnostic.length))
    return lines


def _diagnostic_header(source_path: str, diagnostic: Diagnostic) -> str:
    location = source_path
    message = diagnostic.message
    if diagnostic.physical_line is not None:
        # `file:line:col` 是给工具用的既定约定，冒号位置必须是物理行，否则 VSCode
        # ctrl+click / quickfix 全部跳错。SA 行号是语言的一部分、用户也靠它定位，
        # 所以移进消息里而不是丢掉。
        location += f":{diagnostic.physical_line}:{max(1, diagnostic.column)}"
        if diagnostic.line is not None:
            message = f"[SA {diagnostic.line}] {message}"
    elif diagnostic.line is not None:
        location += f":{diagnostic.line}:{max(1, diagnostic.column)}"
    return f"{location} {diagnostic.severity}: {message}"


def _source_line_for_diagnostic(source_text: str, sa_line: int | None, physical_line: int | None = None) -> str | None:
    source_lines = source_text.splitlines()
    if physical_line is not None:
        return source_lines[physical_line - 1] if 1 <= physical_line <= len(source_lines) else None
    if sa_line is None:
        return None
    for line in source_lines:
        match = _NUMBERED_LINE_RE.match(line)
        if match and int(match.group(1)) == sa_line:
            return line
    # 定位不到就什么都不显示：以前这里回退到 source_lines[sa_line - 1]，等于把 SA 行号
    # 当物理行号用，NONE_NUMBER 文件里必然指向一段毫不相干的代码。
    return None


def _underline(source_line: str, column: int, length: int) -> str:
    start = max(0, min(max(1, column) - 1, len(source_line)))
    marker_width = max(1, length)
    prefix = "".join("\t" if char == "\t" else " " for char in source_line[:start])
    return f"{prefix}{'^' * marker_width}"
