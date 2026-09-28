"""smoke 应接受大小写、目录别名和 Windows 短路径差异，但仍拒绝错误目录。"""
import subprocess
import sys
from pathlib import Path

import pytest

from installer import smoke


@pytest.fixture(params=["plain", "parent-alias", "windows-short"])
def app_path(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    app = tmp_path / "SADK installation"
    app.mkdir()
    if request.param == "plain":
        return app
    if request.param == "parent-alias":
        return tmp_path / ".." / tmp_path.name / app.name
    if sys.platform != "win32":
        pytest.skip("Windows 8.3 短路径只在 Windows 上有意义")

    import ctypes
    from ctypes import wintypes

    get_short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    get_short_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_short_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_short_path(str(app), buffer, len(buffer))
    assert 0 < length < len(buffer), ctypes.get_last_error()
    short_path = Path(buffer.value)
    if str(short_path).casefold() == str(app).casefold():
        pytest.skip("当前文件系统未生成 8.3 短路径")
    assert short_path.samefile(app)
    return short_path


@pytest.mark.parametrize("wrong_path", [False, True], ids=["different-case", "wrong-directory"])
def test_cli_surface_checks_install_path_case_insensitively(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_path: Path, wrong_path: bool) -> None:
    app = app_path
    reported = tmp_path / "OTHER" if wrong_path else app.resolve()

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
def test_toolchain_isolation_checks_path_case_insensitively(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_path: Path, wrong_path: bool) -> None:
    app = app_path
    toolchain = app / "toolchain"
    toolchain.mkdir(parents=True)
    (toolchain / "zig.exe").touch()
    reported = tmp_path / "OTHER" if wrong_path else toolchain.resolve()
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
