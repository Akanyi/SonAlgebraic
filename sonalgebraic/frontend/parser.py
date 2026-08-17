from __future__ import annotations

import re

from ..core import ast
from ..core.errors import SonCompileError
from .expr_parser import parse_expr
from ..core.lines import SourceLine, read_numbered_lines


_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LABEL_RE = re.compile(r"^::([A-Za-z_][A-Za-z0-9_]*)$")


def parse_program(source: str) -> ast.Program:
    return Parser(read_numbered_lines(source)).parse_program()


class Parser:
    def __init__(self, lines: list[SourceLine]):
        self.lines = lines
        self.i = 0

    def parse_program(self) -> ast.Program:
        program = ast.Program(source_lines={line.no: line.text for line in self.lines})
        while not self.at_end():
            line = self.peek()
            text = line.text.strip()
            upper = text.upper()

            if not text or upper.startswith("REM"):
                self.i += 1
            elif _starts_word(upper, "USEC"):
                program.usec_headers.append(self.parse_usec(line))
                self.i += 1
            elif _starts_word(upper, "USELIB"):
                program.uselibs.append(self.parse_uselib(line))
                self.i += 1
            elif _starts_word(upper, "DECLARE C"):
                program.c_decls.append(self.parse_c_decl(line))
                self.i += 1
            elif _starts_word(upper, "USE"):
                program.uses.append(self.parse_use(line))
                self.i += 1
            elif _starts_word(upper, "DIM") or _starts_word(upper, "CONST"):
                program.declarations.append(self.parse_declaration(line))
                self.i += 1
            elif upper.startswith("FOR ENTITY"):
                program.entities.append(self.parse_entity())
            elif _starts_word(upper, "ENUM"):
                program.enums.append(self.parse_enum())
            elif _starts_word(upper, "SUB"):
                program.subs.append(self.parse_sub())
            else:
                program.top_level.append(self.parse_statement())

        return program

    def parse_sub(self) -> ast.Subroutine:
        header = self.advance()
        name, params, visibility, return_type = self.parse_sub_header(header)
        body: list[ast.Stmt] = []

        while not self.at_end():
            upper = self.peek().text.strip().upper()
            if upper == ".ENDSUB":
                self.i += 1
                return ast.Subroutine(name, params, visibility, return_type, body, header.no)
            # SUB 不能嵌套，撞见下一个 SUB 头说明本 SUB 忘了 .ENDSUB，报头部这行才是根因
            if _starts_word(upper, "SUB"):
                break
            body.append(self.parse_statement())

        raise SonCompileError("SUB 缺少 .ENDSUB", header.no)

    def parse_enum(self) -> ast.EnumDef:
        header = self.advance()
        match = re.match(r"^ENUM\s+([A-Za-z_][A-Za-z0-9_]*)$", header.text.strip(), re.IGNORECASE)
        if not match:
            raise SonCompileError("ENUM 必须写成 `ENUM Name`", header.no)
        members: list[str] = []
        seen: set[str] = set()
        while not self.at_end():
            line = self.peek()
            text = line.text.strip()
            upper = text.upper()
            if upper == ".ENDENUM":
                self.i += 1
                if not members:
                    raise SonCompileError("ENUM 至少要有一个成员", header.no)
                return ast.EnumDef(match.group(1), members, header.no)
            if not text or upper.startswith("REM"):
                self.i += 1
                continue
            _validate_name(text, line.no)
            if text.lower() in seen:
                raise SonCompileError(f"ENUM 成员重复: {text}", line.no)
            seen.add(text.lower())
            members.append(text)
            self.i += 1
        raise SonCompileError("ENUM 缺少 .ENDENUM", header.no)

    def parse_entity(self) -> ast.EntityDef:
        header = self.advance()
        match = re.match(r"^FOR\s+ENTITY\s+AS\s+([A-Za-z_][A-Za-z0-9_.]*)$", header.text, re.IGNORECASE)
        if not match:
            raise SonCompileError("ENTITY 必须写成 `FOR ENTITY AS Name`", header.no)

        fields: list[ast.Declaration] = []
        while not self.at_end():
            line = self.peek()
            text = line.text.strip()
            upper = text.upper()
            if upper == ".ENDENTITY":
                self.i += 1
                return ast.EntityDef(match.group(1), fields, header.no)
            if not _starts_word(upper, "DIM"):
                raise SonCompileError("ENTITY 内部只能包含 DIM 字段声明", line.no)
            fields.append(self.parse_declaration(line))
            self.i += 1

        raise SonCompileError("ENTITY 缺少 .ENDENTITY", header.no)

    def parse_statement(self) -> ast.Stmt:
        line = self.advance()
        text = line.text.strip()
        upper = text.upper()

        if not text or upper.startswith("REM"):
            return ast.NoOp(line.no)
        if upper in {".ENDSUB", "END IF", ".ENDIF"}:
            raise SonCompileError(f"孤立的 `{text}`", line.no)

        if _starts_word(upper, "DIM") or _starts_word(upper, "CONST"):
            decl = self.parse_declaration(line)
            return ast.LocalDeclaration(line.no, decl.name, decl.type_spec, decl.mutable, decl.expr)

        label_match = _LABEL_RE.match(text)
        if label_match:
            return ast.Label(line.no, label_match.group(1))
        if _starts_word(upper, "PRINT"):
            expr_text = text[5:].strip()
            return ast.Print(line.no, parse_expr(expr_text, line.no) if expr_text else None)
        if _starts_word(upper, "CALL"):
            name, args = self.parse_call(text[4:].strip(), line.no)
            return ast.Call(line.no, name, args)
        if _starts_word(upper, "TRY"):
            return self.parse_try(line, text)
        if _starts_word(upper, "IF"):
            return self.parse_if(line, text)
        if _starts_word(upper, "FOR"):
            return self.parse_for(line, text)
        if _starts_word(upper, "WHILE"):
            return self.parse_while(line, text)
        if _starts_word(upper, "GOTO"):
            return ast.Goto(line.no, self.parse_label_ref(text[4:].strip(), line.no))
        if _starts_word(upper, "GOSUB"):
            return ast.Gosub(line.no, self.parse_label_ref(text[5:].strip(), line.no))
        if _starts_word(upper, "RETURN"):
            expr_text = text[6:].strip()
            return ast.Return(line.no, parse_expr(expr_text, line.no) if expr_text else None)
        if upper == "END":
            return ast.End(line.no)
        if upper == "CLS":
            return ast.Cls(line.no)
        if _starts_word(upper, "THROW"):
            return self.parse_throw(line, text)
        if upper.startswith("CATCH") or upper == ".ENDTRY":
            raise SonCompileError(f"孤立的 `{text}`", line.no)
        input_stmt = self.try_parse_input(text, line.no)
        if input_stmt is not None:
            return input_stmt

        assign = self.try_parse_assign(text, line.no)
        if assign is not None:
            return assign

        raise SonCompileError(f"无法解析的语句: {text}", line.no)

    def parse_try(self, line: SourceLine, text: str) -> ast.TryCatch:
        match = re.match(
            r"^TRY\s+CALL\s+(.+)\s+TRACEBACK\s+ERROR\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)$",
            text,
            re.IGNORECASE,
        )
        if not match:
            raise SonCompileError("TRY 必须写成 `TRY CALL name(args) TRACEBACK ERROR AS trap`", line.no)

        call_name, args = self.parse_call(match.group(1).strip(), line.no)
        traceback_var = match.group(2)
        catches: list[ast.CatchBranch] = []

        while not self.at_end():
            current = self.peek()
            current_text = current.text.strip()
            upper = current_text.upper()
            if upper == ".ENDTRY":
                self.i += 1
                if not catches:
                    raise SonCompileError("TRY 至少需要一个 CATCH", line.no)
                return ast.TryCatch(line.no, call_name, args, traceback_var, catches)
            if _is_block_terminator(upper) and not _starts_word(upper, "CATCH"):
                break
            catch_match = re.match(r"^CATCH\s+([A-Za-z_][A-Za-z0-9_]*)\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)$", current_text, re.IGNORECASE)
            if not catch_match:
                raise SonCompileError("TRY 内只能包含 CATCH 分支", current.no)
            self.i += 1
            body: list[ast.Stmt] = []
            while not self.at_end():
                next_upper = self.peek().text.strip().upper()
                if next_upper == ".ENDTRY" or _is_block_terminator(next_upper):
                    break
                body.append(self.parse_statement())
            catches.append(ast.CatchBranch(catch_match.group(1).upper(), catch_match.group(2), body, current.no))

        raise SonCompileError("TRY 缺少 .ENDTRY", line.no)

    def parse_throw(self, line: SourceLine, text: str) -> ast.ThrowNew | ast.ThrowVar:
        rest = text[5:].strip()
        new_match = re.match(r"^NEW\s+([A-Za-z_][A-Za-z0-9_]*)\s*,\s*(.+)$", rest, re.IGNORECASE)
        if new_match:
            return ast.ThrowNew(line.no, new_match.group(1).upper(), parse_expr(new_match.group(2), line.no))
        _validate_name(rest, line.no)
        return ast.ThrowVar(line.no, rest)

    def parse_if(self, line: SourceLine, text: str) -> ast.If:
        match = re.match(r"^IF\s+(.+)\s+THEN$", text, re.IGNORECASE)
        if not match:
            raise SonCompileError("IF 语句必须写成 `IF <条件> THEN`", line.no)

        condition = parse_expr(match.group(1), line.no)
        body = self.parse_if_block()
        elifs: list[ast.ElifBranch] = []
        else_body: list[ast.Stmt] = []

        while not self.at_end():
            current = self.peek()
            upper = current.text.strip().upper()
            if upper in {"END IF", ".ENDIF"}:
                self.i += 1
                return ast.If(line.no, condition, body, elifs, else_body)
            if _starts_word(upper, "ELSE IF"):
                self.i += 1
                elif_match = re.match(r"^ELSE\s+IF\s+(.+)\s+THEN$", current.text.strip(), re.IGNORECASE)
                if not elif_match:
                    raise SonCompileError("ELSE IF 必须写成 `ELSE IF <条件> THEN`", current.no)
                elif_cond = parse_expr(elif_match.group(1), current.no)
                elif_body = self.parse_if_block()
                elifs.append(ast.ElifBranch(elif_cond, elif_body, current.no))
                continue
            if upper == "ELSE":
                self.i += 1
                else_body = self.parse_if_block()
                # ELSE 之后只能是 END IF / .ENDIF
                if self.at_end() or self.peek().text.strip().upper() not in {"END IF", ".ENDIF"}:
                    raise SonCompileError("ELSE 块后必须是 END IF 或 .ENDIF", current.no)
                self.i += 1
                return ast.If(line.no, condition, body, elifs, else_body)
            raise SonCompileError("IF 缺少 END IF 或 .ENDIF", line.no)

        raise SonCompileError("IF 缺少 END IF 或 .ENDIF", line.no)

    def parse_if_block(self) -> list[ast.Stmt]:
        """解析 IF/ELSE IF/ELSE 的语句块，遇到分支关键字或 END IF 即停（不消费）。"""
        body: list[ast.Stmt] = []
        while not self.at_end():
            upper = self.peek().text.strip().upper()
            if _is_block_terminator(upper):
                return body
            body.append(self.parse_statement())
        raise SonCompileError("IF 缺少 END IF 或 .ENDIF", self.lines[-1].no if self.lines else 0)

    def parse_for(self, line: SourceLine, text: str) -> ast.ForLoop:
        match = re.match(
            r"^FOR\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s+TO\s+(.+?)(?:\s+STEP\s+(.+))?$",
            text,
            re.IGNORECASE,
        )
        if not match:
            raise SonCompileError("FOR 必须写成 `FOR 变量 = 起始 TO 结束 [STEP 步长]`", line.no)
        var = match.group(1)
        _validate_name(var, line.no)
        start = parse_expr(match.group(2), line.no)
        end = parse_expr(match.group(3), line.no)
        step = parse_expr(match.group(4), line.no) if match.group(4) else None

        body: list[ast.Stmt] = []
        while not self.at_end():
            upper = self.peek().text.strip().upper()
            if upper == ".ENDFOR":
                self.i += 1
                return ast.ForLoop(line.no, var, start, end, step, body)
            if _is_block_terminator(upper):
                break
            body.append(self.parse_statement())
        raise SonCompileError("FOR 缺少 .ENDFOR", line.no)

    def parse_while(self, line: SourceLine, text: str) -> ast.WhileLoop:
        match = re.match(r"^WHILE\s+(.+)$", text, re.IGNORECASE)
        if not match:
            raise SonCompileError("WHILE 必须写成 `WHILE <条件>`", line.no)
        condition = parse_expr(match.group(1), line.no)

        body: list[ast.Stmt] = []
        while not self.at_end():
            upper = self.peek().text.strip().upper()
            if upper == ".ENDWHILE":
                self.i += 1
                return ast.WhileLoop(line.no, condition, body)
            if _is_block_terminator(upper):
                break
            body.append(self.parse_statement())
        raise SonCompileError("WHILE 缺少 .ENDWHILE", line.no)

    def parse_use(self, line: SourceLine) -> ast.UseModule:
        match = re.match(r"^USE\s+([A-Za-z0-9_.]+)\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)$", line.text, re.IGNORECASE)
        if not match:
            raise SonCompileError("USE 必须写成 `USE 模块名 AS 别名`", line.no)
        return ast.UseModule(match.group(1).upper(), match.group(2), line.no)

    def parse_usec(self, line: SourceLine) -> ast.UseCHeader:
        match = re.match(r'^USEC\s+(.+?)\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)$', line.text, re.IGNORECASE)
        if not match:
            raise SonCompileError("USEC 必须写成 `USEC \"header.h\" AS 别名` 或 `USEC <header> AS 别名`", line.no)
        raw = match.group(1).strip()
        alias = match.group(2)
        if raw.startswith('"') and raw.endswith('"'):
            return ast.UseCHeader(raw[1:-1], alias, False, line.no)
        if raw.startswith("<") and raw.endswith(">"):
            return ast.UseCHeader(raw[1:-1], alias, True, line.no)
        if "." not in raw:
            raw = raw + ".h"
        return ast.UseCHeader(raw, alias, False, line.no)

    def parse_uselib(self, line: SourceLine) -> ast.UseLibrary:
        match = re.match(r'^USELIB\s+(.+?)\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)$', line.text, re.IGNORECASE)
        if not match:
            raise SonCompileError("USELIB 必须写成 `USELIB \"lib\" AS 别名`", line.no)
        lib = match.group(1).strip()
        if lib.startswith('"') and lib.endswith('"'):
            lib = lib[1:-1]
        return ast.UseLibrary(lib, match.group(2), line.no)

    def parse_c_decl(self, line: SourceLine) -> ast.CFunctionDecl:
        match = re.match(r'^DECLARE\s+C\s+(SUB|FUNCTION)\s+([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s+AS\s+(.+)$', line.text, re.IGNORECASE)
        if not match:
            raise SonCompileError("DECLARE C 必须写成 `DECLARE C SUB/FUNCTION 别名.函数名(参数...) AS 返回类型`", line.no)
        alias = match.group(2)
        name = match.group(3)
        params = self.parse_params(match.group(4), line.no)
        return_type = _parse_type_tokens(match.group(5).upper().split(), line.no)
        return ast.CFunctionDecl(alias, name, params, return_type, line.no)

    def parse_declaration(self, line: SourceLine) -> ast.Declaration:
        head, expr_text = _split_top_level_equal(line.text, line.no)
        tokens = head.split()
        kind = tokens[0].upper() if tokens else ""
        if kind not in {"DIM", "CONST"} or len(tokens) < 4:
            raise SonCompileError("声明必须写成 `DIM/CONST 名称 AS 类型 ...`", line.no)

        name = tokens[1]
        array_size = None
        array_match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\[(\d+)\]$", name)
        if array_match:
            name = array_match.group(1)
            array_size = int(array_match.group(2))
            if array_size <= 0:
                raise SonCompileError("数组长度必须是正整数", line.no)
        _validate_name(name, line.no)
        type_tokens = tokens[2:]
        mutable = kind == "DIM"

        if mutable:
            if len(type_tokens) < 2 or type_tokens[-2].upper() != "AS" or type_tokens[-1].upper() != "VAR":
                raise SonCompileError("DIM 声明必须以 `AS VAR` 标明可变性", line.no)
            type_tokens = type_tokens[:-2]
        elif any(t.upper() == "VAR" for t in type_tokens):
            raise SonCompileError("CONST 不能带 `AS VAR`", line.no)
        elif expr_text is None:
            raise SonCompileError("CONST 必须提供初始值", line.no)

        if array_size is not None and expr_text is not None:
            raise SonCompileError("数组声明暂不支持初始值表达式", line.no)

        if not type_tokens or type_tokens[0].upper() != "AS":
            raise SonCompileError("声明缺少类型", line.no)

        element_type = _parse_type_tokens(type_tokens[1:], line.no)
        type_spec = element_type if array_size is None else ast.TypeSpec(
            element_type.name, element_type.subtype, element_type.inner, array_size
        )

        return ast.Declaration(
            name,
            type_spec,
            mutable,
            parse_expr(expr_text, line.no) if expr_text is not None else None,
            line.no,
        )

    def parse_sub_header(self, line: SourceLine) -> tuple[str, list[ast.Param], str, ast.TypeSpec]:
        # 括号前允许空白：贴不贴函数名都行，否则参数表会整段掉进 suffix，
        # 报出「未知 SUB 修饰符: (a」这种完全看不懂的错
        match = re.match(r"^SUB\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\((.*)\))?(.*)$", line.text, re.IGNORECASE)
        if not match:
            raise SonCompileError("SUB 头不完整", line.no)

        name = match.group(1)
        _validate_name(name, line.no)
        params = self.parse_params(match.group(2) or "", line.no)
        suffix_tokens = (match.group(3) or "").strip().split()
        visibility = "PRIVATE"
        return_type = ast.TypeSpec("VOID")

        i = 0
        while i < len(suffix_tokens):
            token = suffix_tokens[i].upper()
            if token == "AS":
                if i + 1 >= len(suffix_tokens):
                    raise SonCompileError("SUB 头不完整", line.no)
                next_token = suffix_tokens[i + 1].upper()
                if next_token in {"PUBLIC", "PRIVATE"}:
                    visibility = next_token
                    i += 2
                    continue
                return_type = _parse_type_tokens(suffix_tokens[i + 1 :], line.no)
                break
            elif token in {"PUBLIC", "PRIVATE"}:
                visibility = token
                i += 1
            else:
                raise SonCompileError(f"未知 SUB 修饰符: {suffix_tokens[i]}", line.no)

        return name, params, visibility, return_type

    def parse_params(self, text: str, line_no: int) -> list[ast.Param]:
        if not text.strip():
            return []

        params: list[ast.Param] = []
        for raw in _split_top_level_list(text, line_no):
            tokens = raw.split()
            if len(tokens) < 3:
                raise SonCompileError("参数必须写成 `name AS Type ...`", line_no)
            name = tokens[0]
            _validate_name(name, line_no)
            type_tokens = tokens[1:]
            by_ref = bool(len(type_tokens) >= 2 and type_tokens[-2].upper() == "AS" and type_tokens[-1].upper() == "REF")
            if by_ref:
                type_tokens = type_tokens[:-2]
            if not type_tokens or type_tokens[0].upper() != "AS":
                raise SonCompileError("参数缺少类型", line_no)
            params.append(ast.Param(name, _parse_type_tokens(type_tokens[1:], line_no), by_ref, line_no))
        return params

    def try_parse_input(self, text: str, line_no: int) -> ast.Input | None:
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\.INPUT\s+(.+)$", text, re.IGNORECASE)
        if not match:
            return None
        rest = match.group(2)
        # `cfg.INPUT = "x"` 是给名叫 INPUT 的 ENTITY 字段赋值，不是 IO.INPUT 语句。
        # 这里不让它继续往下走，否则会被当成缺参数的 IO.INPUT 报一个南辕北辙的错。
        if rest.lstrip().startswith("="):
            return None
        prompt_text, target = _split_top_level_comma(rest, line_no)
        _validate_name(target.strip(), line_no)
        return ast.Input(line_no, match.group(1), parse_expr(prompt_text.strip(), line_no), target.strip())

    def try_parse_assign(self, text: str, line_no: int) -> ast.Assign | None:
        pos = _find_top_level(text, "=", line_no)
        if pos == -1:
            return None
        left_text = text[:pos].strip()
        expr_text = text[pos + 1 :].strip()
        if not left_text or not expr_text:
            return None
        target = parse_expr(left_text, line_no)
        if not isinstance(target, ast.VarRef | ast.Deref | ast.Index):
            return None
        call_expr = self.try_parse_call_expr(expr_text, line_no)
        return ast.Assign(line_no, target, call_expr or parse_expr(expr_text, line_no))

    def parse_call(self, text: str, line_no: int) -> tuple[str, list[ast.Expr]]:
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*)(?:\((.*)\))?$", text, re.IGNORECASE)
        if not match:
            raise SonCompileError("CALL 必须写成 `CALL name` 或 `CALL name(args...)`", line_no)
        name = match.group(1)
        for part in name.split("."):
            _validate_name(part, line_no)
        args_text = match.group(2)
        if args_text is None or not args_text.strip():
            return name, []
        return name, [parse_expr(part, line_no) for part in _split_top_level_list(args_text, line_no)]

    def try_parse_call_expr(self, text: str, line_no: int) -> ast.CallExpr | None:
        if not _starts_word(text.strip().upper(), "CALL"):
            return None
        name, args = self.parse_call(text.strip()[4:].strip(), line_no)
        return ast.CallExpr(line_no, name, args)

    def parse_label_ref(self, text: str, line_no: int) -> str:
        match = _LABEL_RE.match(text)
        if not match:
            raise SonCompileError("标签引用必须写成 `::name`", line_no)
        return match.group(1)

    def peek(self) -> SourceLine:
        return self.lines[self.i]

    def advance(self) -> SourceLine:
        line = self.lines[self.i]
        self.i += 1
        return line

    def at_end(self) -> bool:
        return self.i >= len(self.lines)


def _starts_word(text: str, word: str) -> bool:
    return text == word or text.startswith(word + " ")


# 所有块终结符（外加 SUB 头，它同样不可能出现在块体里）。任何嵌套块的语句循环撞见
# 不属于自己的终结符都要立刻停手且不消费，由外层块去报「缺少对应终结符」。否则终结符
# 会被当成普通语句解析，报出来的是「无法解析的语句: .ENDFOR」这种指向无关行的级联噪音，
# 真正的根因（某个块没闭合）反而一个字都看不到。
_BLOCK_TERMINATORS = {".ENDSUB", ".ENDFOR", ".ENDWHILE", ".ENDIF", "END IF", "ELSE", ".ENDTRY", ".ENDENTITY", ".ENDENUM"}


def _is_block_terminator(upper: str) -> bool:
    return (
        upper in _BLOCK_TERMINATORS
        or _starts_word(upper, "ELSE IF")
        or _starts_word(upper, "CATCH")
        or _starts_word(upper, "SUB")
    )


def _validate_name(name: str, line_no: int) -> None:
    if not _NAME_RE.match(name):
        raise SonCompileError(f"非法名称: {name}", line_no)


def _read_as_parts(tokens: list[str], line_no: int) -> list[str]:
    parts: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i].upper() != "AS" or i + 1 >= len(tokens):
            raise SonCompileError("类型和修饰符必须用 AS 串起来", line_no)
        parts.append(tokens[i + 1].upper())
        i += 2
    return parts


def _parse_type_parts(parts: list[str], line_no: int) -> ast.TypeSpec:
    return _parse_type_tokens(parts, line_no)


def _parse_type_tokens(tokens: list[str], line_no: int) -> ast.TypeSpec:
    if not tokens:
        raise SonCompileError("声明缺少类型", line_no)
    first = tokens[0].upper()
    if first == "NUM":
        if len(tokens) != 3 or tokens[1].upper() != "AS" or tokens[2].upper() not in {"LONG", "DOUBLE", "FLOAT"}:
            raise SonCompileError("NUM 声明必须指定 LONG/DOUBLE/FLOAT", line_no)
        return ast.TypeSpec("NUM", tokens[2].upper())
    if first == "ENTITY":
        if len(tokens) != 3 or tokens[1].upper() != "AS":
            raise SonCompileError("ENTITY 声明必须写成 `AS ENTITY AS Name`", line_no)
        return ast.TypeSpec("ENTITY", tokens[2])
    if first == "HANDLE":
        if len(tokens) != 3 or tokens[1].upper() != "AS" or not _NAME_RE.match(tokens[2]):
            raise SonCompileError("HANDLE 声明必须写成 `HANDLE AS Kind`", line_no)
        return ast.TypeSpec("HANDLE", tokens[2].upper())
    if first == "PTR" and len(tokens) > 1 and tokens[1].upper() == "TO":
        inner = _parse_type_tokens(tokens[2:], line_no)
        return ast.TypeSpec("PTR", inner=inner)
    if len(tokens) == 1 and first in {"STRING", "SYMBOL", "ERROR", "CPTR", "VOID", "BOOL"}:
        return ast.TypeSpec(first)
    raise SonCompileError("无法识别的类型声明", line_no)


def _split_top_level_equal(text: str, line_no: int) -> tuple[str, str | None]:
    pos = _find_top_level(text, "=", line_no)
    if pos == -1:
        return text.strip(), None
    return text[:pos].strip(), text[pos + 1 :].strip()


def _split_top_level_comma(text: str, line_no: int) -> tuple[str, str]:
    pos = _find_top_level(text, ",", line_no)
    if pos == -1:
        raise SonCompileError("IO.INPUT 必须提供提示文本和目标变量", line_no)
    return text[:pos], text[pos + 1 :]


def _split_top_level_list(text: str, line_no: int) -> list[str]:
    parts: list[str] = []
    start = 0
    while start <= len(text):
        pos = _find_top_level(text[start:], ",", line_no)
        if pos == -1:
            part = text[start:].strip()
            if part:
                parts.append(part)
            return parts
        part = text[start : start + pos].strip()
        if not part:
            raise SonCompileError("参数列表中存在空项", line_no)
        parts.append(part)
        start += pos + 1


def _find_top_level(text: str, needle: str, line_no: int) -> int:
    quote: str | None = None
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in {'"', "'"}:
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise SonCompileError("括号不匹配", line_no)
        elif ch == needle and depth == 0:
            return i
        i += 1
    if quote or depth != 0:
        raise SonCompileError("字符串或括号没有闭合", line_no)
    return -1
