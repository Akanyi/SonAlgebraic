from __future__ import annotations

from ..core import ast
from ..core.errors import SonCompileError
from .expr_lexer import Token, tokenize_expr


_PRECEDENCE = {
    "OR": 1,
    "AND": 2,
    "=": 3,
    "==": 3,
    "!=": 3,
    "<>": 3,
    "<": 3,
    "<=": 3,
    ">": 3,
    ">=": 3,
    "BOR": 4,
    "BXOR": 5,
    "BAND": 6,
    "SHL": 7,
    "SHR": 7,
    "+": 8,
    "-": 8,
    "*": 9,
    "/": 9,
    "%": 9,
    "**": 10,
}

# 一元前缀绑定到幂同级，保证 -x ** 2 按 -(x ** 2) 解析。
_UNARY_PREC = 10
_PREFIX_PREC = 11


def parse_expr(text: str, line_no: int) -> ast.Expr:
    parser = ExprParser(tokenize_expr(text, line_no), line_no)
    expr = parser.parse(0)
    parser.expect("EOF")
    return expr


class ExprParser:
    def __init__(self, tokens: list[Token], line_no: int):
        self.tokens = tokens
        self.line_no = line_no
        self.i = 0

    def parse(self, min_prec: int) -> ast.Expr:
        left = self.parse_postfix(self.parse_prefix())

        while True:
            op = self.peek_op()
            prec = _PRECEDENCE.get(op)
            if prec is None or prec < min_prec:
                return left

            self.i += 1
            right = self.parse(prec if op == "**" else prec + 1)
            left = ast.Binary(self.line_no, left, op, right)

    def parse_postfix(self, base: ast.Expr) -> ast.Expr:
        # 连续的下标访问 a[i][j]
        while self.tokens[self.i].kind == "OP" and self.tokens[self.i].value == "[":
            self.i += 1
            index = self.parse(0)
            self.expect_op("]")
            base = ast.Index(self.line_no, base, index)
        return base

    def parse_prefix(self) -> ast.Expr:
        token = self.advance()

        if token.kind == "NUMBER":
            return ast.NumberLiteral(self.line_no, token.value)
        if token.kind == "STRING":
            return ast.StringLiteral(self.line_no, token.value)
        if token.kind == "FSTRING":
            return self.parse_fstring(token.value)
        if token.kind == "IDENT":
            word = token.value.upper()
            if word in {"NOT", "BNOT", "+", "-"}:
                return ast.Unary(self.line_no, word, self.parse(_UNARY_PREC))
            if word == "NULL":
                return ast.NullLiteral(self.line_no)
            if word == "TRUE":
                return ast.BoolLiteral(self.line_no, True)
            if word == "FALSE":
                return ast.BoolLiteral(self.line_no, False)
            if word == "CAST":
                type_spec = self.parse_cast_type()
                return ast.Cast(self.line_no, type_spec, self.parse(_PREFIX_PREC))
            if self.match_op("("):
                args = self.parse_args()
                return ast.CallExpr(self.line_no, token.value, args)
            return ast.VarRef(self.line_no, token.value)
        if token.kind == "OP" and token.value in {"+", "-"}:
            return ast.Unary(self.line_no, token.value, self.parse(_UNARY_PREC))
        if token.kind == "OP" and token.value == "^":
            return ast.Deref(self.line_no, self.parse(_PREFIX_PREC))
        if token.kind == "OP" and token.value == "@":
            return ast.AddressOf(self.line_no, self.parse(_PREFIX_PREC))
        if token.kind == "OP" and token.value == "(":
            expr = self.parse(0)
            self.expect_op(")")
            return expr

        raise SonCompileError("表达式不完整或语法错误", self.line_no)

    def parse_cast_type(self) -> ast.TypeSpec:
        from .parser import _parse_type_parts

        type_tokens: list[str] = []
        while True:
            token = self.tokens[self.i]
            if token.kind == "EOF":
                break
            if token.kind == "IDENT":
                upper = token.value.upper()
                if upper in {"PTR", "TO", "AS", "NUM", "LONG", "DOUBLE", "FLOAT", "STRING", "SYMBOL", "ERROR", "CPTR", "ENTITY", "BOOL"}:
                    type_tokens.append(upper)
                    self.i += 1
                    continue
            break
        if not type_tokens:
            raise SonCompileError("CAST 后面必须跟类型", self.line_no)
        return _parse_type_parts(type_tokens, self.line_no)

    def parse_args(self) -> list[ast.Expr]:
        if self.match_op(")"):
            return []

        args: list[ast.Expr] = []
        while True:
            args.append(self.parse(0))
            if self.match_op(")"):
                return args
            self.expect_op(",")

    def parse_fstring(self, value: str) -> ast.FString:
        parts: list[str | ast.Expr] = []
        buf: list[str] = []
        i = 0

        while i < len(value):
            ch = value[i]
            if ch == "{" and i + 1 < len(value) and value[i + 1] == "{":
                buf.append("{")
                i += 2
                continue
            if ch == "}" and i + 1 < len(value) and value[i + 1] == "}":
                buf.append("}")
                i += 2
                continue
            if ch == "{":
                end = value.find("}", i + 1)
                if end == -1:
                    raise SonCompileError("F-string 缺少右花括号", self.line_no)
                if buf:
                    parts.append("".join(buf))
                    buf.clear()
                inner = value[i + 1 : end].strip()
                if not inner:
                    raise SonCompileError("F-string 插值不能为空", self.line_no)
                parts.append(parse_expr(inner, self.line_no))
                i = end + 1
                continue
            if ch == "}":
                raise SonCompileError("F-string 出现多余的右花括号", self.line_no)
            buf.append(ch)
            i += 1

        if buf:
            parts.append("".join(buf))
        return ast.FString(self.line_no, parts)

    def peek_op(self) -> str | None:
        token = self.tokens[self.i]
        if token.kind == "OP":
            return token.value
        if token.kind == "IDENT" and token.value.upper() in {"AND", "OR", "BAND", "BOR", "BXOR", "SHL", "SHR"}:
            return token.value.upper()
        return None

    def match_op(self, value: str) -> bool:
        if self.tokens[self.i].kind == "OP" and self.tokens[self.i].value == value:
            self.i += 1
            return True
        return False

    def expect_op(self, value: str) -> None:
        if not self.match_op(value):
            raise SonCompileError(f"表达式缺少 `{value}`", self.line_no)

    def expect(self, kind: str) -> None:
        if self.tokens[self.i].kind != kind:
            raise SonCompileError("表达式后存在无法解析的内容", self.line_no)

    def advance(self) -> Token:
        token = self.tokens[self.i]
        self.i += 1
        return token
