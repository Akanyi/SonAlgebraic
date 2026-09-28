"""示例清单必须全覆盖；模块依赖、双后端构建与自动示例输出都从真实目录验收。"""
import json
from pathlib import Path
import re
import subprocess

import pytest

from conftest import REPO_ROOT, build_temp, requires_c_compiler, requires_native_compiler
from sonalgebraic.analysis.semantics import check_program
from sonalgebraic.driver.compiler import build_exe, check_source
from sonalgebraic.frontend.parser import parse_program


EXAMPLES = REPO_ROOT / "examples"
CATALOG = json.loads((EXAMPLES / "catalog.json").read_text(encoding="utf-8"))
ENTRIES = CATALOG["examples"]
PROGRAMS = [entry for entry in ENTRIES if entry["kind"] == "program"]


def test_catalog_covers_every_source_and_documents_run_requirements() -> None:
    assert CATALOG["version"] == 1
    paths = [entry["path"] for entry in ENTRIES]
    assert len(paths) == len(set(paths))
    assert set(paths) == {path.relative_to(EXAMPLES).as_posix() for path in EXAMPLES.rglob("*.sa")}
    for entry in ENTRIES:
        path = Path(entry["path"])
        assert not path.is_absolute() and ".." not in path.parts
        assert len(path.parts) >= 2 and entry["title"]
        assert entry["kind"] in {"program", "module"}
        assert entry["run"] in ({"none"} if entry["kind"] == "module" else {"auto", "manual"})
        if entry["run"] == "manual":
            assert entry.get("reason")
        if entry["kind"] == "module":
            assert entry["path"].startswith("modules/")
            assert (path.parent / "main.sa").as_posix() in paths


def test_examples_readme_links_cover_all_sources() -> None:
    text = (EXAMPLES / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", text)
    for target in links:
        if "://" not in target:
            assert (EXAMPLES / target).exists(), target
    assert {entry["path"] for entry in ENTRIES} <= set(links)


def test_installer_reads_nested_catalog() -> None:
    from installer.smoke import example_catalog

    installed_entries = example_catalog(REPO_ROOT)
    assert installed_entries == ENTRIES


def test_installer_runs_only_automatic_examples_in_isolated_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from installer import smoke

    calls = []
    automatic = {entry["path"]: entry for entry in PROGRAMS if entry["run"] == "auto"}

    def fake_run(command, *, timeout, cwd=None):
        calls.append((command, cwd))
        if command[1:2] == ["build"]:
            Path(command[command.index("-o") + 1]).touch()
        if command[1:2] == ["run"] and Path(command[2]).is_relative_to(EXAMPLES):
            name = Path(command[2]).relative_to(EXAMPLES).as_posix()
            assert name in automatic, f"无人值守启动了非自动示例: {name}"
            assert cwd == tmp_path
            output = "\n".join(automatic[name].get("expect", []))
        else:
            output = "\n".join(smoke.PROBE_EXPECT)
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(smoke, "run", fake_run)
    report = smoke.Report()
    smoke.check_end_to_end(report, tmp_path / "sonc.exe", REPO_ROOT, tmp_path, True)

    assert not report.failed
    executed = [Path(command[2]).relative_to(EXAMPLES).as_posix() for command, _ in calls
                if command[1:2] == ["run"] and Path(command[2]).is_relative_to(EXAMPLES)]
    assert sorted(executed) == sorted(automatic)
    assert {name for name, _ in report.skipped} == {
        f"run {entry['path']}" for entry in PROGRAMS if entry["run"] == "manual"
    }


def test_installer_catalog_detects_missing_nested_module(tmp_path: Path) -> None:
    from installer.smoke import example_catalog

    root = tmp_path / "examples"
    root.mkdir()
    (root / "catalog.json").write_text(json.dumps({
        "version": 1,
        "examples": [{"path": "modules/basic/mathlib.sa", "kind": "module", "run": "none"}],
    }), encoding="utf-8")
    with pytest.raises(AssertionError, match="漏装"):
        example_catalog(tmp_path)


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda entry: entry["path"])
def test_example_source_and_module_dependencies_check(entry: dict) -> None:
    path = EXAMPLES / entry["path"]
    if entry["kind"] == "module":
        checked = check_program(parse_program(path.read_text(encoding="utf-8")), require_main=False)
        assert not any(sub.name.lower() == "main" for sub in checked.program.subs)
    else:
        check_source(path)


@pytest.mark.parametrize("backend", [
    pytest.param("c", marks=requires_c_compiler),
    pytest.param("native", marks=requires_native_compiler),
])
@pytest.mark.parametrize("entry", PROGRAMS, ids=lambda entry: entry["path"])
def test_example_build_and_automatic_output(entry: dict, backend: str) -> None:
    with build_temp("example-", "example-tests") as temp:
        root = Path(temp)
        exe = root / "example.exe"
        build_exe(EXAMPLES / entry["path"], exe, keep_c=False, backend=backend)
        if entry["run"] != "auto":
            return
        # 文件示例只能在隔离目录写入；交互与联网项仅构建。
        result = subprocess.run([str(exe)], cwd=root, text=True, encoding="utf-8",
                                errors="replace", capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
        assert result.returncode == 0, result.stderr
        for expected in entry.get("expect", []):
            assert expected in result.stdout, result.stdout
