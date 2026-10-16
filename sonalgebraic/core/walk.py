"""语句的结构子块只有一套定义，防止新块绕过诊断、资源检查或某个后端。"""
from __future__ import annotations

from collections.abc import Iterator

from . import ast


def child_bodies(stmt: ast.Stmt) -> list[list[ast.Stmt]]:
    if isinstance(stmt, ast.If):
        return [stmt.body, *(branch.body for branch in stmt.elifs), stmt.else_body]
    if isinstance(stmt, ast.Scope | ast.ForLoop | ast.WhileLoop):
        return [stmt.body]
    if isinstance(stmt, ast.TryCatch):
        return [branch.body for branch in stmt.catches]
    return []


def walk_statements(body: list[ast.Stmt]) -> Iterator[ast.Stmt]:
    for stmt in body:
        yield stmt
        for nested in child_bodies(stmt):
            yield from walk_statements(nested)
