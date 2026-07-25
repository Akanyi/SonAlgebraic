from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable

from ..core.errors import SonCompileError


_NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)(?:\s|$)")


@dataclass(frozen=True)
class Diagnostic:
    message: str
    line: int | None = None
    column: int = 1
    severity: str = "error"
    length: int = 1

    @classmethod
    def from_compile_error(cls, error: SonCompileError, source_text: str | None = None) -> "Diagnostic":
        column, length = infer_span(error, source_text or "")
        return cls(message=error.message, line=error.line_no, column=column, length=length)


class DiagnosticError(Exception):
    def __init__(self, diagnostics: Iterable[Diagnostic]):
        self.diagnostics = list(diagnostics)
        message = "; ".join(diagnostic.message for diagnostic in self.diagnostics) or "diagnostic error"
        super().__init__(message)


def diagnostic_from_compile_error(error: SonCompileError, source_text: str | None = None) -> Diagnostic:
    return Diagnostic.from_compile_error(error, source_text)


def infer_span(error: SonCompileError, source_text: str) -> tuple[int, int]:
    source_line = _source_line_for_diagnostic(source_text, error.line_no)
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


def render_diagnostics(source_path: str | Path, source_text: str, diagnostics: Iterable[Diagnostic]) -> str:
    path_text = str(source_path)
    rendered: list[str] = []

    for diagnostic in diagnostics:
        if rendered:
            rendered.append("")
        rendered.extend(_render_diagnostic(path_text, source_text, diagnostic))

    return "\n".join(rendered)


def _render_diagnostic(source_path: str, source_text: str, diagnostic: Diagnostic) -> list[str]:
    lines = [_diagnostic_header(source_path, diagnostic)]
    source_line = _source_line_for_diagnostic(source_text, diagnostic.line)
    if source_line is not None:
        lines.append(source_line)
        lines.append(_underline(source_line, diagnostic.column, diagnostic.length))
    return lines


def _diagnostic_header(source_path: str, diagnostic: Diagnostic) -> str:
    location = source_path
    if diagnostic.line is not None:
        location += f":{diagnostic.line}:{max(1, diagnostic.column)}"
    return f"{location} {diagnostic.severity}: {diagnostic.message}"


def _source_line_for_diagnostic(source_text: str, line_no: int | None) -> str | None:
    if line_no is None:
        return None

    source_lines = source_text.splitlines()
    for line in source_lines:
        match = _NUMBERED_LINE_RE.match(line)
        if match and int(match.group(1)) == line_no:
            return line

    if 1 <= line_no <= len(source_lines):
        return source_lines[line_no - 1]
    return None


def _underline(source_line: str, column: int, length: int) -> str:
    start = max(0, min(max(1, column) - 1, len(source_line)))
    marker_width = max(1, length)
    prefix = "".join("\t" if char == "\t" else " " for char in source_line[:start])
    return f"{prefix}{'^' * marker_width}"
