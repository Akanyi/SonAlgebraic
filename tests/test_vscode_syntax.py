"""编辑器语法必须跟随语言表面语法，不让新关键字退化成普通变量。"""

import json
from pathlib import Path
import re

from conftest import REPO_ROOT
from sonalgebraic import __version__


EXTENSION = REPO_ROOT / "editors" / "vscode" / "sonalgebraic"
GRAMMAR = json.loads((EXTENSION / "syntaxes" / "sonalgebraic.tmLanguage.json").read_text(encoding="utf-8"))
RULES = GRAMMAR["repository"]


def test_extension_version_and_grammar_registration() -> None:
    manifest = json.loads((EXTENSION / "package.json").read_text(encoding="utf-8"))
    assert manifest["version"] == __version__
    assert manifest["contributes"]["grammars"][0]["scopeName"] == GRAMMAR["scopeName"]
    assert all(pattern["include"][1:] in RULES for pattern in GRAMMAR["patterns"])


def test_new_language_tokens_have_dedicated_scopes() -> None:
    for group, examples in {
        "declaration-keywords": ["ASYNC SUB", "NEW SUB", "DECLARE C", "FOR ENTITY"],
        "control-keywords": ["AWAIT", "SYNC", "CALLRET", "END IF", "ELSE IF"],
        "type-keywords": ["PROMISE", "SUB", "FLOAT", "NET_STREAM"],
        "modifier-keywords": ["OF", "FROM", "REF"],
        "operators": ["f=", "m=", "**", "%"],
    }.items():
        pattern = re.compile(RULES[group]["match"])
        for token in examples:
            assert pattern.fullmatch(token), f"{group} 没有完整识别 {token}"


def test_fstrings_and_module_calls_support_current_syntax() -> None:
    opener = re.compile(RULES["fstring"]["begin"])
    for text in ('F"value {x}"', "f'val {x}'"):
        assert opener.match(text)
    assert not opener.search('PREFIXF"not an f-string"')
    member = re.compile(RULES["module-call"]["match"])
    for text in ("N.CONNECT_ASYNC(host, 9000)", "SYS.NET.SEND_ASYNC(stream, text)"):
        assert member.search(text)
