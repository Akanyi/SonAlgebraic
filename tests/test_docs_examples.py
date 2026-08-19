"""`docs/` 与 `README.md` 里的 SA 示例必须真能编译。

这套文档烂掉的根因就是示例从来没被跑过——旧 `01-getting-started.md` 的「正确示范」
`DIM counter AS NUM` 自己都过不了语义检查，而它旁边的法则二还在说这是错误写法。
这里把每个 ```basic 代码块参数化成独立 case，以后改语法忘了同步文档，红的是 CI
而不是照着文档抄的人。

演示错误写法、或本身就是不含 `SUB main` 的片段，在代码块上方写：

    <!-- doctest: skip 这里必须说明为什么跳过 -->

HTML 注释在 GitHub 渲染时不可见。原因是强制的——没有原因的 skip 就是把防线关掉。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import pytest

from conftest import REPO_ROOT
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.core.errors import SonCompileError
from sonalgebraic.frontend.parser import parse_program

_SKIP_DIRECTIVE = re.compile(r"<!--\s*doctest:\s*skip\b(?P<reason>[^>]*?)-->", re.IGNORECASE)


@dataclass(frozen=True)
class DocExample:
    path: Path
    line: int
    source: str
    # None = 没有 skip 指令；空串 = 有指令但没写原因（测试里会判失败）
    skip_reason: str | None

    @property
    def ident(self) -> str:
        return f"{self.path.name}:{self.line}"


def _markdown_files() -> list[Path]:
    return [*sorted((REPO_ROOT / "docs").glob("*.md")), REPO_ROOT / "README.md"]


def _skip_reason(lines: list[str], fence_index: int) -> str | None:
    """取代码块上方最近一个非空行里的 doctest 指令。"""
    i = fence_index - 1
    while i >= 0 and not lines[i].strip():
        i -= 1
    if i < 0:
        return None
    match = _SKIP_DIRECTIVE.search(lines[i])
    return match.group("reason").strip() if match else None


def _extract(path: Path) -> list[DocExample]:
    lines = path.read_text(encoding="utf-8").splitlines()
    examples: list[DocExample] = []
    i = 0
    while i < len(lines):
        if lines[i].strip() != "```basic":
            i += 1
            continue
        start = i + 1
        end = start
        while end < len(lines) and lines[end].strip() != "```":
            end += 1
        examples.append(
            DocExample(
                path=path,
                line=start + 1,
                source="\n".join(lines[start:end]) + "\n",
                skip_reason=_skip_reason(lines, i),
            )
        )
        i = end + 1
    return examples


DOC_EXAMPLES = [example for path in _markdown_files() for example in _extract(path)]


def test_doc_examples_are_collected() -> None:
    """抽取逻辑一旦改坏，上面的参数化会静默变成零个 case，整个测试形同虚设。"""
    assert len(DOC_EXAMPLES) >= 20, f"只抽到 {len(DOC_EXAMPLES)} 个示例，抽取逻辑可能坏了"


@pytest.mark.parametrize("example", DOC_EXAMPLES, ids=lambda example: example.ident)
def test_doc_example_compiles(example: DocExample) -> None:
    if example.skip_reason is not None:
        assert example.skip_reason, f"{example.ident}: doctest skip 必须写明原因"
        pytest.skip(example.skip_reason)

    try:
        check_program(parse_program(example.source))
    except SonCompileError as exc:
        pytest.fail(f"{example.ident} 编译失败: {exc}\n--- 示例原文 ---\n{example.source}")
