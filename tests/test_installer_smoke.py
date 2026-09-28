"""Windows 会规范化路径大小写；smoke 应接受同一路径，但仍拒绝错误目录。"""
import subprocess
from pathlib import Path

import pytest

from installer import smoke


@pytest.mark.parametrize("wrong_path", [False, True], ids=["different-case", "wrong-directory"])
def test_cli_surface_checks_install_path_case_insensitively(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wrong_path: bool) -> None:
    app = tmp_path / "SADK"
    reported = tmp_path / "OTHER" if wrong_path else app

    def fake_run(command, *, timeout):
        output = {
            "--version": "sonc 0.2.1",
            "--help": "check build run pack slib doctor",
            "doctor": f"SADK 安装目录: {str(reported).swapcase()}",
            "definitely-not-a-command": "未知子命令",
        }[command[1]]
        return subprocess.CompletedProcess(command, int(command[1] == "definitely-not-a-command"), output, "")

    monkeypatch.setattr(smoke, "run", fake_run)
    report = smoke.Report()
    smoke.check_cli_surface(report, app / "bin" / "sonc.exe", app)

    assert not report.skipped
    assert len(report.passed) == (3 if wrong_path else 4)
    assert [name for name, _ in report.failed] == (["sonc doctor 认出自己是安装包"] if wrong_path else [])


@pytest.mark.parametrize("wrong_path", [False, True], ids=["different-case", "wrong-directory"])
def test_toolchain_isolation_checks_path_case_insensitively(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wrong_path: bool) -> None:
    app = tmp_path / "SADK"
    toolchain = app / "toolchain"
    toolchain.mkdir(parents=True)
    (toolchain / "zig.exe").touch()
    reported = tmp_path / "OTHER" if wrong_path else toolchain
    isolated_env = {"PATH": "isolated"}

    def fake_run(command, *, timeout, env, cwd=None):
        assert env == isolated_env
        if command[1] == "doctor":
            output = f"自带工具链目录: {str(reported).swapcase()}\n  gcc    未找到\n状态: 就绪"
        else:
            assert command[1] == "run" and cwd == tmp_path
            output = "\n".join(smoke.PROBE_EXPECT)
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(smoke, "isolated_path_env", lambda: isolated_env)
    monkeypatch.setattr(smoke, "run", fake_run)
    report = smoke.Report()
    smoke.check_toolchain_isolation(report, app / "bin" / "sonc.exe", app, tmp_path)

    assert not report.skipped
    assert len(report.passed) == (1 if wrong_path else 2)
    assert [name for name, _ in report.failed] == (["屏蔽系统 PATH 后仍能找到自带 zig"] if wrong_path else [])
