from __future__ import annotations

import re

from ..core.errors import SonCompileError


_NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)(?:[ \t](.*))?$")


def renumber_source(source: str, step: int = 10, start: int | None = None) -> str:
    if step <= 0:
        raise SonCompileError("行号步长必须是正整数")
    next_no = start if start is not None else step
    if next_no <= 0:
        raise SonCompileError("起始行号必须是正整数")

    out: list[str] = []
    for physical_no, raw in enumerate(source.splitlines(), 1):
        if not raw.strip():
            out.append("")
            continue
        match = _NUMBERED_LINE_RE.match(raw.rstrip())
        if not match:
            raise SonCompileError("每一行都必须以正整数行号开头", physical_no)
        body = match.group(2)
        out.append(f"{next_no} {body}" if body else str(next_no))
        next_no += step

    result = "\n".join(out)
    if source.endswith(("\n", "\r")):
        result += "\n"
    return result


def renumber_file(source_path, output_path=None, step: int = 10, start: int | None = None):
    source = source_path.read_text(encoding="utf-8-sig")
    result = renumber_source(source, step=step, start=start)
    target = output_path or source_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result, encoding="utf-8")
    return target
