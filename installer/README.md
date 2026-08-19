# SADK 安装包

把 SonAlgebraic 打成 Windows 上双击就能装的 `SADK-Setup-<版本>.exe`：编译器冻结成不依赖
Python 的 `sonc.exe`，配上文档、示例、VSCode 扩展，以及 C 工具链。

## 两种形态

| 产物 | 大小 | zig 从哪来 |
| --- | --- | --- |
| `SADK-Setup-<版本>.exe` | 约 17 MB | 安装向导里下载（x64 约 93 MB / ARM64 约 89 MB） |
| `SADK-Setup-<版本>-with-zig.exe` | 约 72 MB | x64 的已在包内，装完即可 `sonc build`；ARM64 仍回退到在线下载 |

离线版只内置 x64：为一个小众架构给每一份包再塞 90 MB 不值得。两种包装完的
`toolchain/` 布局完全一样，`sonc doctor` 看不出区别。

## 构建

```powershell
python installer/build_installer.py               # 在线版
python installer/build_installer.py --bundle-zig  # 离线版
```

产物落在 `build/installer/`。

需要两样东西：

- `pip install pyinstaller`
- `winget install --id JRSoftware.InnoSetup`（构建脚本会自己找 ISCC.exe，winget 和官网两种安装位置都认）

只改了 `.iss` 想快速重打包时加 `--skip-freeze`，跳过几十秒的 PyInstaller 冻结。两种形态
连着出的话，第二次加上它就能复用同一份冻结产物：

```powershell
python installer/build_installer.py
python installer/build_installer.py --skip-freeze --bundle-zig
```

`--bundle-zig` 会先下载 zig 官方 zip 到 `build/zig-cache/`（校验 SHA-256，命中缓存就复用），
再解压到 `build/zig-extract/`。分成两个目录是为了 CI：缓存只需要背那份 93 MB 的 zip，
解压出来的 380 MB 目录树每次重新展开就好。

> 为什么不把 zip 直接塞进安装包让它自己解压？Inno 的 `extractarchive` 标志只能配
> `external` 用——它解压的是安装时下载的东西，没法展开包内的归档。所以离线版必须在
> 构建时就解压好，按目录树交给 `[Files]`。顺带让 Inno 的 lzma2 接手压缩，比原样塞一个
> deflate 的 zip 更省，93 MB 的 zig 进包后只占约 55 MB。

### 升级 zig 版本

URL、SHA-256 和字节数都在 `sadk.iss` 顶部的 `#define` 里，**那是单一来源**——
`build_installer.py` 的 `--bundle-zig` 也从那里读。改的时候三行一起改，别在别处抄第二份：
抄了就会出现在线版和离线版拿到不同编译器，而且不到安装那一刻发现不了。

摘要对不上时，在线版由 Inno 中止安装，离线版由构建脚本中止打包。两边都是宁可失败，
也不把来路不明的编译器塞给用户。

## CI

`.github/workflows/installer.yml` 在 Windows runner 上把两种形态都构一遍：

- push / PR：产物作为 artifact 上传
- 打 `v*` tag：额外传到 GitHub Release
- zig 归档用 `actions/cache` 缓存，缓存键是 `sadk.iss` 的哈希，所以升级 zig 会自然让键失效

## 文件

| 文件 | 作用 |
| --- | --- |
| `build_installer.py` | 入口：生成图标 →（可选下载解压 zig）→ PyInstaller 冻结 → ISCC 打包，版本号从 `sonalgebraic/__init__.py` 读 |
| `smoke.py` | super smoke：装一遍 → 全面验证 → 卸一遍 |
| `sonc.spec` | PyInstaller 配置，onedir 模式 |
| `sadk.iss` | Inno Setup 脚本：组件、任务、PATH、文件关联、zig 来源（在线/内置双模式） |
| `sadk-env.cmd` | 开始菜单里「SADK 命令提示符」的启动脚本 |
| `make_icon.py` | 用标准库生成 `assets/sadk.ico`，不引 Pillow |
| `languages/` | 简体中文语言文件，见该目录下的 README |

## super smoke

```powershell
python installer/smoke.py                # 用现成的包，只装文件
python installer/smoke.py --build        # 先构建再测
python installer/smoke.py --with-zig     # 连 zig 在线下载和工具链隔离一起测（多下约 93 MB）
python installer/smoke.py --integration  # 连 PATH / 文件关联一起测（会动注册表，测完校验恢复）
python installer/smoke.py --keep         # 跑完不卸载，留着人工看
```

全开时 51 项检查，覆盖安装布局、CLI 表层、诊断、代码生成、端到端编译运行、打包、工具链隔离、
卸载残留和注册表清理。默认全程只碰一个临时目录，跑完自动卸载；失败不中断，一次列完所有问题。

**和 pytest 那套的分工。** `tests/` 测的是编译器逻辑，跑的是仓库里的 Python 源码。smoke 测的是
「用户双击安装包之后拿到的东西」——冻结产物有没有漏模块、安装布局对不对、卸载干不干净。这几类
问题从源码测试里一个都看不出来，所以它没有接进 pytest：要真装真卸、耗时以十分钟计、还依赖一个
已经构建好的安装包。

几项值得单独说的检查：

- **模块项目的 C 生成**：`jinja2` 只在生成模块头文件时才被用到，单文件路径根本不碰它。冻结漏掉
  jinja2 时，只有走到这一步才会炸。
- **`sadk-env.cmd` 纯 ASCII**：踩过的坑，见下面的设计取舍。
- **工具链隔离**：把 PATH 砍到只剩 `System32`，验证只靠 SADK 自带的 zig 也能编译运行——这是
  「用户机器上什么都没装」这个场景的唯一真实证明。
- **PATH 精确恢复**：卸载后逐字节比对安装前的用户 PATH，并确认值类型仍是 `REG_EXPAND_SZ`。
- **库模块识别**：`examples/` 里混着 `mathlib.sa` 这类没有 `SUB main` 的库，smoke 按有没有 main
  自动区分，库走 slib / spkg 那几项覆盖，不会拿去当主程序跑。


## 安装后的布局

```
SADK/
  bin/
    sonc.exe          冻结后的编译器
    _internal/        PyInstaller 运行时
    sadk-env.cmd
  docs/  examples/  editors/
  toolchain/
    zig-x86_64-windows-<ver>/    勾选了才有
```

`sonc.exe` 启动时会把 `toolchain/` 下的目录前置进本进程 PATH（`sonalgebraic/core/sdk_env.py`），
所以自带的 zig 不需要写进系统环境变量也能被找到。`sonc doctor` 打印这套探测的结果。

## 几个决定的理由

**为什么 zig 是在线下载而不是打进包里。** zig 解压后 383 MB，塞进安装包会让分发体积涨到
200 MB 以上；而多数开发机上本来就有 gcc/clang/zig。所以做成向导里的一个复选框，并且在
进入任务页时扫一遍 PATH——本机已经有编译器就默认不勾，没有才自动勾上。

**为什么下载走 `[Files]` 的 `download` 标志，而不是 `[Code]` 里的 `TDownloadWizardPage`。**
后者挂在 `NextButtonClick` 上，而这个回调在 `/VERYSILENT` 安装时根本不触发——静默安装的用户
勾了任务也拿不到工具链，而且不会有任何报错。`download` 标志走安装器内建流程，两种模式行为
一致，还自带进度条和 SHA-256 校验。

**为什么解压出来的 `zig-x86_64-windows-<ver>/` 不重命名成 `zig/`。** 重命名会让卸载日志里记录
的路径对不上，卸载后留下一堆删不掉的残留。工具链探测本来就认带版本号的目录名。

**为什么 PATH 的卸载清理要自己写。** Inno 能回滚它写过的注册表值，但这里的 PATH 是
「旧值 + 新目录」拼接写入的，整体回滚会把用户在安装之后加的其它路径一起抹掉。所以卸载时
只做精确的字符串摘除，并且保持 `REG_EXPAND_SZ` 类型不变——退化成 `REG_SZ` 会让 PATH 里的
`%VAR%` 引用失效。

**为什么 `sadk-env.cmd` 必须是纯 ASCII。** cmd.exe 按控制台原始代码页解析文件开头几行，
那是在 `chcp 65001` 生效之前。中文注释写在那里会被 GBK 解成乱码，还可能吞掉行尾，把下一行的
`rem` 顶成命令名。

## 升级 zig 版本

改 `sadk.iss` 顶部的 `ZigVersion` / `ZigUrl*` / `ZigHash*` / `ZigSize*`。hash 从官方
`https://ziglang.org/download/index.json` 取。对不上安装器会直接中止——这是有意的，宁可装不上，
也不能把来路不明的编译器塞进用户机器。
