"""SADK 安装包的 super smoke：从 exe 装起，把整套 SDK 过一遍，再卸干净。

    python installer/smoke.py                # 用 build/installer 里现成的包
    python installer/smoke.py --build        # 先构建再测
    python installer/smoke.py --with-zig     # 连 zig 在线下载一起测（多下 ~93 MB）
    python installer/smoke.py --integration  # 连 PATH / 文件关联一起测（会动注册表，测完校验恢复）

和 pytest 那套的分工：那边测的是编译器逻辑，跑的是仓库里的 Python 源码；这边测的是
"用户双击安装包之后拿到的东西"——冻结产物有没有漏模块、安装布局对不对、卸载干不干净。
这两类问题从源码测试里一个都看不出来。

默认全程只碰一个临时目录，不装任何系统集成，跑完自动卸载。
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

INSTALLER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = INSTALLER_DIR.parent
EXAMPLES_IN_REPO = PROJECT_ROOT / "examples"

# 不能无人值守跑的示例。这些不是坏掉了，是天生需要人或外部环境。
UNATTENDED_BLOCKLIST = {
    "gui_hello": "开真窗口并进事件循环，没人点就不退出",
    "use_io": "SYS.IO 的 INPUT 阻塞等 stdin",
    "net_tls": "要连外网，网络不通的失败跟 SDK 无关",
    "desktop": "弹系统消息框，需要人点掉",
    "web_server": "占端口 8080 等请求，没人访问就要空转到 accept 超时",
}

# 输出里出现这些字样才算特性真的跑通了，而不只是"没崩"。
# 内容取自实际运行结果：符号树打印、LIST JOIN、MAP 取值、GOSUB 返回、异常捕获。
# 这里只放语义稳定的示例——它们的输出是语言语义的体现，不会被随手改动。
EXPECTED_OUTPUT = {
    "entity_strings": ["LANS: 99", "SA: 100"],
    "allexample": ["derivative f'", "caught error", "ffi puts ok"],
    "symbol": ["(a + (2 * b))"],
    "lists": ["len=3", "alpha,middle,beta"],
    "maps": ["alice", "nihao"],
    "gosub": ["In helper", "Back!"],
    "errors": ["caught: boom"],
}

# 验证"编译链路本身是对的"用自带探针，不借 examples 里的东西。
# hello.sa 就正好在一次构建和一次 smoke 之间被简化过——拿入门示例的具体文案当
# 基准，改的人没错，红的却是 smoke。探针内容由 smoke 自己控制，覆盖变量、
# F-string、FOR 循环和字符串输出，足够证明整条链路活着。
PROBE_SOURCE = """10 DIM i AS NUM AS LONG AS VAR
20 DIM label AS STRING AS VAR
30 SUB main AS PUBLIC AS VOID
40 label = "sadk-probe"
50 FOR i = 1 TO 3
60 PRINT F"{label} {i}"
70 .ENDFOR
80 .ENDSUB
90 CALL main
100 END
"""
PROBE_EXPECT = ["sadk-probe 1", "sadk-probe 2", "sadk-probe 3"]

BAD_SOURCE = """10 PRINT F"broken {x"
20 ELSE
30 PRINT missing
40 END
"""


def force_utf8_output() -> None:
    """报告正文是中文，而 Windows 下 Python 默认按本地代码页写 stdout。

    cp936 控制台里还凑合，重定向到文件或在 UTF-8 终端里看就是一片乱码。和 sonc
    自己的做法保持一致，统一钉成 UTF-8。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def is_library_module(path: Path) -> bool:
    """判断一个 .sa 是库模块还是可执行程序。

    examples/ 里混着两类东西：mathlib、statslib 这些是给别人 USE 的库，压根没有
    `SUB main`，拿去 check/run 必然报"程序必须定义 SUB main"。它们的覆盖在
    slib / spkg 那几项，以及引用它们的 use_*.sa 里。
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return re.search(r"\bSUB\s+main\b", text, re.IGNORECASE) is None


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


@dataclass
class Report:
    passed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def ok(self, name: str) -> None:
        self.passed.append(name)
        print(f"  [PASS] {name}", flush=True)

    def fail(self, name: str, reason: str) -> None:
        self.failed.append((name, reason))
        print(f"  [FAIL] {name}\n         {reason}", flush=True)

    def skip(self, name: str, reason: str) -> None:
        self.skipped.append((name, reason))
        print(f"  [SKIP] {name} — {reason}", flush=True)


@contextmanager
def check(report: Report, name: str):
    """一项检查。失败只记录不中断——smoke 的价值在于一次看完所有问题。"""
    try:
        yield
    except AssertionError as exc:
        report.fail(name, str(exc) or "断言失败")
    except subprocess.TimeoutExpired:
        report.fail(name, "超时")
    except Exception as exc:  # noqa: BLE001 - smoke 要把任何异常都变成一条失败记录
        report.fail(name, f"{type(exc).__name__}: {exc}")
    else:
        report.ok(name)


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}", flush=True)


# ---------------------------------------------------------------------------
# 执行辅助
# ---------------------------------------------------------------------------


def run(command: list[str], timeout: int = 300, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """统一入口。stdin 钉死 DEVNULL：任何一个示例不小心读了 stdin 都会挂住整个 smoke。"""
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        env=env,
    )


def assert_ok(proc: subprocess.CompletedProcess[str], what: str) -> str:
    assert proc.returncode == 0, f"{what} 退出码 {proc.returncode}\n{tail(proc)}"
    return proc.stdout


def tail(proc: subprocess.CompletedProcess[str], lines: int = 12) -> str:
    text = (proc.stdout or "") + (proc.stderr or "")
    return "\n".join(text.strip().splitlines()[-lines:])


def isolated_path_env() -> dict[str, str]:
    """一个只剩系统目录的 PATH，用来验证 SDK 不靠开发机上的 gcc/clang 也能干活。"""
    env = dict(os.environ)
    system_root = env.get("SystemRoot", r"C:\Windows")
    env["PATH"] = os.pathsep.join([f"{system_root}\\System32", system_root])
    return env


# ---------------------------------------------------------------------------
# 安装 / 卸载
# ---------------------------------------------------------------------------


def find_setup() -> Path:
    candidates = list((PROJECT_ROOT / "build" / "installer").glob("SADK-Setup-*.exe"))
    if not candidates:
        raise SystemExit(
            "找不到安装包。先跑 python installer/build_installer.py，或给 smoke.py 加 --build。"
        )
    # 按文件名排会让 0.10.0 排在 0.9.0 前面，取最近构建的那个更符合直觉
    return max(candidates, key=lambda p: p.stat().st_mtime)


def install(setup: Path, target: Path, tasks: str, log: Path) -> subprocess.CompletedProcess[str]:
    return run(
        [
            str(setup),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            f"/DIR={target}",
            f"/TASKS={tasks}",
            f"/LOG={log}",
        ],
        timeout=1800,  # 勾了 zig 就要下 ~93 MB
    )


def uninstall(install_dir: Path) -> bool:
    """卸载并等目录真正消失。

    unins000.exe 会先把自己复制到 temp 再回头删安装目录，所以父进程返回时目录
    往往还在——直接断言"目录已消失"会假报失败。
    """
    uninstaller = install_dir / "unins000.exe"
    if not uninstaller.is_file():
        return False
    run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=600)
    for _ in range(60):
        if not install_dir.exists():
            return True
        time.sleep(0.5)
    return not install_dir.exists()


# ---------------------------------------------------------------------------
# 各阶段检查
# ---------------------------------------------------------------------------


def check_layout(report: Report, app: Path) -> None:
    section("安装布局")

    for relative in ("bin/sonc.exe", "bin/sadk-env.cmd", "README.md", "unins000.exe"):
        with check(report, f"文件存在: {relative}"):
            assert (app / relative).is_file(), f"缺少 {relative}"

    for relative in ("bin/_internal", "docs", "examples", "editors"):
        with check(report, f"目录非空: {relative}"):
            path = app / relative
            assert path.is_dir(), f"缺少目录 {relative}"
            assert any(path.iterdir()), f"{relative} 是空的"

    with check(report, "examples 里的模块依赖齐全"):
        # use_user_module 这类示例要求同目录有被引用的模块源码，漏装就是编译失败
        for name in ("mathlib", "statslib", "samath", "mathlib_enhanced"):
            assert (app / "examples" / f"{name}.sa").is_file(), f"examples 缺少 {name}.sa"

    with check(report, "sadk-env.cmd 是纯 ASCII"):
        # 踩过的坑：cmd.exe 按控制台原始代码页解析开头几行，那是在 chcp 65001 生效之前。
        # 中文写在那儿会被 GBK 解成乱码，还能吞掉行尾把下一行的 rem 顶成命令名。
        data = (app / "bin" / "sadk-env.cmd").read_bytes()
        offenders = [i for i, byte in enumerate(data) if byte > 127]
        assert not offenders, f"含 {len(offenders)} 个非 ASCII 字节，首个在偏移 {offenders[0]}"

    with check(report, "sadk-env.cmd 能跑通且不报错"):
        proc = run(["cmd", "/c", str(app / "bin" / "sadk-env.cmd")], timeout=60)
        assert_ok(proc, "sadk-env.cmd")
        assert "SonAlgebraic SDK" in proc.stdout, f"输出不含横幅:\n{tail(proc)}"
        assert str(app) in proc.stdout, "横幅里的 SADK_HOME 不是安装目录"
        combined = proc.stdout + proc.stderr
        assert "不是内部或外部命令" not in combined and "not recognized" not in combined, (
            f"批处理解析出错:\n{tail(proc)}"
        )


def check_cli_surface(report: Report, sonc: Path, app: Path) -> None:
    section("CLI 表层")

    with check(report, "sonc --version"):
        out = assert_ok(run([str(sonc), "--version"], timeout=120), "--version")
        assert re.match(r"^sonc \d+\.\d+", out.strip()), f"版本行不对: {out!r}"

    with check(report, "sonc --help"):
        out = assert_ok(run([str(sonc), "--help"], timeout=120), "--help")
        for command in ("check", "build", "run", "pack", "slib", "doctor"):
            assert command in out, f"帮助里没有 {command}"

    with check(report, "sonc doctor 认出自己是安装包"):
        # 守 sdk_env.sdk_home() 的 bin 布局判定：认错了会给出方向相反的补救提示
        out = assert_ok(run([str(sonc), "doctor"], timeout=120), "doctor")
        assert str(app) in out, f"doctor 没报出安装目录:\n{out}"

    with check(report, "未知子命令退出码非 0"):
        proc = run([str(sonc), "definitely-not-a-command"], timeout=120)
        assert proc.returncode != 0, "未知命令居然成功了"


def check_frontend(report: Report, sonc: Path, app: Path, work: Path) -> None:
    section("前端与诊断")

    examples = sorted(p for p in (app / "examples").glob("*.sa") if not is_library_module(p))
    with check(report, f"check 全部 {len(examples)} 个可执行示例"):
        assert examples, "examples 目录里没有可执行的 .sa"
        broken = []
        for path in examples:
            proc = run([str(sonc), "check", str(path)], timeout=180)
            if proc.returncode != 0:
                broken.append(f"{path.name}: {tail(proc, 4)}")
        assert not broken, "以下示例 check 失败:\n" + "\n".join(broken)

    bad = work / "broken.sa"
    bad.write_text(BAD_SOURCE, encoding="utf-8")

    with check(report, "坏源码报 SA 诊断而不是 traceback"):
        proc = run([str(sonc), "check", str(bad)], timeout=120)
        assert proc.returncode == 1, f"期望退出码 1，实际 {proc.returncode}"
        combined = proc.stdout + proc.stderr
        assert "[SA " in combined, f"没有 SA 错误码:\n{tail(proc)}"
        assert "Traceback" not in combined, f"漏出了 Python traceback:\n{tail(proc)}"

    with check(report, "check --json 产出合法 JSON"):
        # 编辑器和 CI 靠这个吃诊断，混进一句人类可读文本就全废了
        proc = run([str(sonc), "check", "--json", str(bad)], timeout=120)
        payload = json.loads(proc.stdout)
        assert payload, "JSON 是空的"

    with check(report, "文件不存在时报友好错误"):
        proc = run([str(sonc), "check", str(work / "nope.sa")], timeout=120)
        assert proc.returncode != 0
        assert "Traceback" not in proc.stdout + proc.stderr, "漏出了 Python traceback"

    with check(report, "fmt 重排行号"):
        target = work / "fmt_out.sa"
        assert_ok(
            run([str(sonc), "fmt", str(app / "examples" / "hello.sa"), "-o", str(target), "--renumber", "20"], timeout=120),
            "fmt",
        )
        numbers = [
            int(match.group(1))
            for line in target.read_text(encoding="utf-8-sig").splitlines()
            if (match := re.match(r"^(\d+)\s", line))
        ]
        assert numbers, "重排后没有任何行号"
        assert numbers == sorted(numbers), "行号不是递增的"
        assert numbers[0] == 20 and numbers[1] == 40, f"步长不是 20: {numbers[:3]}"


def check_codegen(report: Report, sonc: Path, app: Path, work: Path) -> None:
    section("代码生成")

    with check(report, "c 生成单文件 C 且保留 SA 注释"):
        out_c = work / "hello.c"
        assert_ok(run([str(sonc), "c", str(app / "examples" / "hello.sa"), "-o", str(out_c)], timeout=180), "c")
        text = out_c.read_text(encoding="utf-8")
        assert "/* SA " in text, "生成的 C 里没有 SA 源码行注释"
        assert "int main" in text, "生成的 C 里没有 main"

    with check(report, "c 生成模块项目（覆盖 jinja2 头文件渲染）"):
        # 冻结产物漏掉 jinja2 时，只有走到模块头文件生成这一步才会炸——单文件路径根本不碰它
        project = work / "module_project"
        assert_ok(
            run([str(sonc), "c", str(app / "examples" / "use_user_module.sa"), "-o", str(project)], timeout=180),
            "c 模块项目",
        )
        names = {p.name for p in project.rglob("*") if p.is_file()}
        assert any(n.endswith(".h") for n in names), f"项目里没有头文件: {sorted(names)}"
        assert "sa_runtime.c" in names or "sa_runtime.h" in names, f"项目里没有 runtime: {sorted(names)}"

    with check(report, "native-ir 生成 LLVM IR"):
        out_ll = work / "hello.ll"
        assert_ok(run([str(sonc), "native-ir", str(app / "examples" / "hello.sa"), "-o", str(out_ll)], timeout=180), "native-ir")
        text = out_ll.read_text(encoding="utf-8")
        assert "define" in text and "@main" in text, "IR 里没有 main 定义"

    with check(report, "生成的 C 只带够得着的运行时"):
        # 按需注入退化回"整份运行时都塞进去"时，这条会先响
        text = (work / "hello.c").read_text(encoding="utf-8")
        assert "sa_symbol_deriv" not in text, "PRINT/循环的程序背上了 SYMBOL 求导代码"
        assert "sa_net_" not in text, "没用网络的程序背上了 NET 运行时"


def check_end_to_end(report: Report, sonc: Path, app: Path, work: Path, has_compiler: bool) -> None:
    section("端到端编译运行")

    if not has_compiler:
        report.skip("端到端编译运行", "这台机器没有可用的 C 编译器")
        return

    examples = sorted(p for p in (app / "examples").glob("*.sa") if not is_library_module(p))
    covered = set(EXPECTED_OUTPUT) | {"use_user_module"}

    for name, expected in EXPECTED_OUTPUT.items():
        source = app / "examples" / f"{name}.sa"
        if not source.is_file():
            report.skip(f"run {name}.sa", "示例不存在")
            continue
        with check(report, f"run {name}.sa 输出符合预期"):
            proc = run([str(sonc), "run", str(source)], timeout=600, cwd=work)
            assert_ok(proc, f"run {name}")
            for fragment in expected:
                assert fragment in proc.stdout, f"输出里没有 {fragment!r}:\n{tail(proc)}"

    with check(report, "run 带用户模块的示例（模块分离编译 + 链接）"):
        proc = run([str(sonc), "run", str(app / "examples" / "use_user_module.sa")], timeout=600, cwd=work)
        assert_ok(proc, "run use_user_module")
        assert proc.stdout.strip(), "模块示例没有任何输出"

    # 剩下的只断言"能跑完"。上面那些有精确断言的就不重复编译了，每个示例都是一次完整
    # 的 C 编译 + 链接，重复跑纯属浪费。
    rest = [p for p in examples if p.stem not in UNATTENDED_BLOCKLIST and p.stem not in covered]
    with check(report, f"run 其余 {len(rest)} 个示例全部退出码 0"):
        broken = []
        for path in rest:
            proc = run([str(sonc), "run", str(path)], timeout=600, cwd=work)
            if proc.returncode != 0:
                broken.append(f"{path.name}: 退出码 {proc.returncode}\n{tail(proc, 6)}")
        assert not broken, "以下示例运行失败:\n" + "\n\n".join(broken)

    probe = work / "probe.sa"
    probe.write_text(PROBE_SOURCE, encoding="utf-8")

    with check(report, "build 产出可独立运行的 exe"):
        exe = work / "probe_built.exe"
        assert_ok(run([str(sonc), "build", str(probe), "-o", str(exe), "--discard-c"], timeout=600), "build")
        assert exe.is_file(), "build 没产出 exe"
        proc = run([str(exe)], timeout=120, cwd=work)
        assert_ok(proc, "运行 build 出来的 exe")
        for fragment in PROBE_EXPECT:
            assert fragment in proc.stdout, f"输出里没有 {fragment!r}:\n{tail(proc)}"

    with check(report, "run -- 之后的参数原样转发给程序"):
        proc = run([str(sonc), "run", str(probe), "--", "--verbose", "extra"], timeout=600, cwd=work)
        assert_ok(proc, "run 转发参数")
        assert PROBE_EXPECT[0] in proc.stdout, f"转发参数后程序没正常跑:\n{tail(proc)}"


def check_packaging(report: Report, sonc: Path, app: Path, work: Path, has_compiler: bool) -> None:
    section("打包")

    # zipfile / hashlib 这些只在打包路径上才被 import，冻结漏包时前面所有检查都发现不了
    with check(report, "pack 产出 .spkg"):
        spkg = work / "mathlib.spkg"
        assert_ok(run([str(sonc), "pack", str(app / "examples" / "mathlib.sa"), "-o", str(spkg)], timeout=180), "pack")
        assert spkg.is_file() and spkg.stat().st_size > 0, "spkg 是空的"

    with check(report, "--pkg 引用 .spkg 编译"):
        # 必须挪到独立目录：留在 examples 里会优先解析到同目录的 mathlib.sa，
        # 那样测的就不是 spkg 路径了
        isolated = work / "pkg_consumer"
        isolated.mkdir(exist_ok=True)
        consumer = isolated / "use_user_module.sa"
        shutil.copy2(app / "examples" / "use_user_module.sa", consumer)
        proc = run(
            [str(sonc), "check", str(consumer), "--pkg", str(work / "mathlib.spkg")],
            timeout=180,
        )
        assert_ok(proc, "check --pkg")

    with check(report, "slib 产出源码包"):
        slib = work / "statslib.slib"
        assert_ok(run([str(sonc), "slib", str(app / "examples" / "statslib.sa"), "-o", str(slib)], timeout=300), "slib")
        assert slib.is_file() and slib.stat().st_size > 0, "slib 是空的"

    with check(report, "USE 引用 .slib 编译"):
        isolated = work / "slib_consumer"
        isolated.mkdir(exist_ok=True)
        shutil.copy2(app / "examples" / "use_statslib.sa", isolated / "use_statslib.sa")
        shutil.copy2(work / "statslib.slib", isolated / "statslib.slib")
        assert_ok(run([str(sonc), "check", str(isolated / "use_statslib.sa")], timeout=180), "check 引用 slib")

    if not has_compiler:
        report.skip("slib --binary", "这台机器没有可用的 C 编译器")
        return

    with check(report, "slib --binary 产出带静态库的包"):
        binary_slib = work / "statslib_binary.slib"
        assert_ok(
            run([str(sonc), "slib", str(app / "examples" / "statslib.sa"), "-o", str(binary_slib), "--binary"], timeout=900),
            "slib --binary",
        )
        with zipfile.ZipFile(binary_slib) as archive:
            names = archive.namelist()
        assert any(n.endswith((".a", ".lib")) for n in names), f"包里没有静态库: {names}"


def check_toolchain_isolation(report: Report, sonc: Path, app: Path, work: Path) -> None:
    section("自带工具链隔离验证")

    toolchain = app / "toolchain"
    if not toolchain.is_dir() or not any(toolchain.iterdir()):
        report.skip("自带工具链隔离验证", "本次安装没有勾选 zig（加 --with-zig 可覆盖）")
        return

    env = isolated_path_env()

    with check(report, "屏蔽系统 PATH 后仍能找到自带 zig"):
        proc = run([str(sonc), "doctor"], timeout=180, env=env)
        assert_ok(proc, "doctor（隔离 PATH）")
        assert str(toolchain) in proc.stdout, f"doctor 没报出自带工具链:\n{proc.stdout}"
        # 确认 PATH 真被隔离了，否则下面那条"只靠自带 zig"其实是系统 gcc 在干活
        assert re.search(r"^\s*gcc\s+未找到", proc.stdout, re.MULTILINE), (
            f"PATH 没被真正隔离，还能看到系统 gcc:\n{proc.stdout}"
        )
        assert "状态: 就绪" in proc.stdout, f"隔离后状态不是就绪:\n{proc.stdout}"

    with check(report, "只靠自带 zig 完成编译运行"):
        # 这条是"用户机器上什么都没装"这个场景的唯一真实证明
        probe = work / "isolated_probe.sa"
        probe.write_text(PROBE_SOURCE, encoding="utf-8")
        proc = run([str(sonc), "run", str(probe)], timeout=900, cwd=work, env=env)
        assert_ok(proc, "run（隔离 PATH）")
        for fragment in PROBE_EXPECT:
            assert fragment in proc.stdout, f"输出里没有 {fragment!r}:\n{tail(proc)}"


# ---------------------------------------------------------------------------
# 系统集成（可选，会动注册表）
# ---------------------------------------------------------------------------


def read_user_path() -> str | None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        try:
            value, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return None
    return value


def check_integration(report: Report, app: Path) -> None:
    section("系统集成")

    import winreg

    with check(report, "安装把 bin 加进了用户 PATH"):
        current = read_user_path() or ""
        assert str(app / "bin").lower() in current.lower(), "PATH 里没有 SADK 的 bin"

    with check(report, "PATH 值类型仍是 REG_EXPAND_SZ"):
        # 退化成 REG_SZ 会让 PATH 里其它条目的 %VAR% 引用整体失效
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            _, kind = winreg.QueryValueEx(key, "Path")
        assert kind == winreg.REG_EXPAND_SZ, f"值类型变成了 {kind}"

    with check(report, ".sa 文件类型已注册"):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\.sa") as key:
            value, _ = winreg.QueryValueEx(key, "")
        assert value == "SonAlgebraic.Source", f"关联指向了 {value!r}"

    with check(report, "右键菜单已注册"):
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Classes\SonAlgebraic.Source\shell\soncrun\command"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "")
        assert "sonc.exe" in value, f"右键命令不对: {value!r}"


def check_integration_cleanup(report: Report, before: str | None) -> None:
    section("卸载后的系统集成清理")

    import winreg

    with check(report, "PATH 精确恢复到安装前"):
        after = read_user_path()
        assert after == before, (
            "PATH 没有精确还原\n"
            f"  之前: {before}\n"
            f"  之后: {after}"
        )

    with check(report, ".sa 关联已清理"):
        key_exists = True
        try:
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\SonAlgebraic.Source").Close()
        except FileNotFoundError:
            key_exists = False
        assert not key_exists, "SonAlgebraic.Source 键还在"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    force_utf8_output()

    parser = argparse.ArgumentParser(description="SADK 安装包 super smoke")
    parser.add_argument("--setup", type=Path, help="指定安装包路径，默认取 build/installer 里最新的")
    parser.add_argument("--build", action="store_true", help="先跑 build_installer.py 再测")
    parser.add_argument("--with-zig", action="store_true", help="连 zig 在线下载一起测（多下约 93 MB）")
    parser.add_argument("--integration", action="store_true", help="连 PATH / 文件关联一起测（会动注册表，测完校验恢复）")
    parser.add_argument("--keep", action="store_true", help="跑完不卸载，留着人工检查")
    args = parser.parse_args()

    if sys.platform != "win32":
        raise SystemExit("SADK 安装包是 Windows 专有的，这个 smoke 只能在 Windows 上跑")

    if args.build:
        section("构建安装包")
        result = subprocess.run([sys.executable, str(INSTALLER_DIR / "build_installer.py")], cwd=PROJECT_ROOT)
        if result.returncode != 0:
            raise SystemExit("构建失败，smoke 中止")

    setup = args.setup or find_setup()
    tasks = ",".join(
        (["zig"] if args.with_zig else []) + (["addtopath", "assoc"] if args.integration else [])
    )

    print(f"安装包: {setup}  ({setup.stat().st_size / 1048576:.1f} MB)")
    print(f"附加任务: {tasks or '（无，只装文件）'}")

    path_before = read_user_path() if args.integration else None

    sandbox = Path(tempfile.mkdtemp(prefix="sadk-smoke-"))
    app = sandbox / "SADK"
    work = sandbox / "work"
    work.mkdir()
    report = Report()

    try:
        section("安装")
        proc = install(setup, app, tasks, sandbox / "install.log")
        if proc.returncode != 0:
            report.fail("静默安装", f"退出码 {proc.returncode}\n{tail(proc)}")
            raise SystemExit(summarize(report))
        report.ok("静默安装")

        sonc = app / "bin" / "sonc.exe"
        if not sonc.is_file():
            report.fail("安装产物", f"{sonc} 不存在")
            raise SystemExit(summarize(report))

        check_layout(report, app)
        check_cli_surface(report, sonc, app)

        doctor = run([str(sonc), "doctor"], timeout=120)
        has_compiler = "状态: 就绪" in doctor.stdout

        check_frontend(report, sonc, app, work)
        check_codegen(report, sonc, app, work)
        check_end_to_end(report, sonc, app, work, has_compiler)
        check_packaging(report, sonc, app, work, has_compiler)
        check_toolchain_isolation(report, sonc, app, work)

        if args.integration:
            check_integration(report, app)

        if args.keep:
            print(f"\n--keep：安装保留在 {app}")
        else:
            section("卸载")
            with check(report, "卸载后目录无残留"):
                assert uninstall(app), f"卸载后 {app} 仍然存在"
            if args.integration:
                check_integration_cleanup(report, path_before)

    finally:
        if not args.keep:
            # 卸载失败也要把沙箱收掉，别在用户的 temp 里越堆越多
            if app.exists():
                uninstall(app)
            shutil.rmtree(sandbox, ignore_errors=True)

    return summarize(report)


def summarize(report: Report) -> int:
    total = len(report.passed) + len(report.failed)
    print("\n" + "=" * 60)
    print(f"通过 {len(report.passed)}/{total}    跳过 {len(report.skipped)}")
    if report.failed:
        print("\n失败项:")
        for name, reason in report.failed:
            print(f"  - {name}\n      {reason}")
    print("=" * 60)
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
