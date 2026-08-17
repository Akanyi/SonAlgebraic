# SonAlgebraic

SonAlgebraic 是一个小语言编译器。完整文档见 [docs/](./docs/)，语言参考见 [docs/02-language-reference.md](./docs/02-language-reference.md)，入门指南见 [docs/01-getting-started.md](./docs/01-getting-started.md)。

当前链路：`.sa` 源码 → C11 源码 → 原生可执行文件。

生成的 C 会在对应位置内联 SA 源码注释，例如：

```c
/* SA 40: PRINT message */
sa_print_string(sa_message);
```

## 目录

- [快速开始](#快速开始)
- [安装 SADK](#安装-sadk)
- [特性概览](#特性概览)
- [CLI 命令](#cli-命令)
  - [检查源码](#检查源码)
  - [编译并运行](#编译并运行)
  - [重排行号](#重排行号)
  - [编译可执行文件](#编译可执行文件)
  - [只生成 C 代码](#只生成-c-代码)
  - [打包 .slib](#打包-slib)
  - [打包 .spkg](#打包-spkg)
  - [交叉编译](#交叉编译)
  - [检查工具链](#检查工具链)
- [诊断系统](#诊断系统)
- [示例程序](#示例程序)
- [模块系统](#模块系统)
  - [USE 引用规则](#use-引用规则)
  - [.slib 单模块包](#slib-单模块包)
  - [.spkg 多模块包](#spkg-多模块包)
- [FFI](#ffi)
- [测试](#测试)
- [运行时语义补充](#运行时语义补充)
  - [ENTITY 字符串字段](#entity-字符串字段)
  - [局部资源清理](#局部资源清理)
  - [SYMBOL](#symbol)
- [当前支持](#当前支持)
- [当前限制](#当前限制)

## 快速开始

```powershell
python -m sonalgebraic build examples/hello.sa -o build/hello.exe
build/hello.exe
```

只生成 C：

```powershell
python -m sonalgebraic c examples/hello.sa -o build/hello.c
```

完整特性演示：

```powershell
python -m sonalgebraic run examples/allexample.sa
```

## 安装 SADK

Windows 上可以装 SADK 安装包，目标机器不需要 Python 环境。构建：

```powershell
python installer/build_installer.py
```

产物是 `build/installer/SADK-Setup-<版本>.exe`，约 17 MB，包含冻结后的 `sonc.exe`、文档、示例和 VSCode 语法高亮扩展。向导里可以选：加入 PATH、关联 `.sa` 文件和右键菜单、把扩展装进 VSCode 用户扩展目录。

`sonc build` / `run` 需要一个 C 编译器，所以向导还提供「下载并安装 Zig C 工具链」（约 92 MB，官方源 + SHA-256 校验）。进入任务页时会先扫一遍 PATH：本机已经有 gcc / clang / zig 就默认不勾，没有才自动勾上。装进 `SADK\toolchain\` 的 zig 不写系统环境变量也能被 `sonc` 找到。

装完确认一下：

```powershell
sonc doctor
```

构建细节和设计取舍见 [installer/README.md](./installer/README.md)。

## 特性概览

- 复古但严格的强制行号语法；可用 `USE SYS.LINT AS NONE_NUMBER` 省略手写行号，编译时自动补齐。
- 稳定 C 后端：`.sa -> C11 -> 原生可执行文件`，生成 C 中保留 SA 源码注释方便定位。
- 实验 native 后端：`.sa -> LLVM IR -> 原生可执行文件`，与 C 后端平行开发。
- `SUB` / `CALL` / `RETURN` / `IF` / `ELSE IF` / `ELSE` / `END IF` / `.ENDIF` / `GOTO` / `GOSUB` 等控制流。
- `FOR ... TO ... STEP` / `WHILE` 结构化循环。
- `NUM`、`STRING`、`BOOL`、`ERROR`、`SYMBOL`、`ENTITY`、`HANDLE AS Kind`、`CPTR`、`PTR TO T` 类型，以及方括号定长数组 `DIM xs[N]`（值类型和 STRING 元素）。
- `TRUE` / `FALSE` / `NULL` 字面量；十六进制、科学计数法、下划线分隔的数值字面量。
- 位运算 `BAND` / `BOR` / `BXOR` / `BNOT` / `SHL` / `SHR`。
- 非 `VOID` 返回值函数、参数、`AS REF` 引用传参（含基于完整 IF/ELSE 的返回路径分析）。
- `ENTITY` 支持嵌套结构、字段访问、字符串字段深拷贝和常规生命周期清理。
- `ERROR` / `TRY` / `CATCH` / `THROW` 结构化异常处理。
- `ENUM` 枚举；`SYS.MATH` / `SYS.IO` / `SYS.STRING` / `SYS.BINARY` / `SYS.LIST` / `SYS.MAP` / `SYS.NET` / `SYS.FILE` / `SYS.DESKTOP` 内置模块。
- `SYMBOL` 完整代数：表达式树捕获、求导 `DERIV`、化简 `SIMPLIFY`、代入 `SUBST`、数值求值 `EVAL`。
- 字符串操作 `SYS.STRING`：LENGTH / CONCAT / SLICE / FIND / UPPER / LOWER / REPLACE。
- 用户模块分离编译、头文件导出、模块循环依赖诊断。
- `.slib` 单模块包，支持源码包、静态库、动态库。
- `.spkg` 多模块源码包，支持 hash 校验和 zip 路径安全解包。
- C FFI：`USEC`、`USELIB`、`DECLARE C`、不透明指针和 typed pointer。
- 多错误诊断：`check/c/build/run` 都会先预检，输出源码下划线。
- VSCode 语法高亮扩展：见 [editors/vscode/](./editors/vscode/)。
- 原生网络栈：HTTP/HTTPS、DNS、TCP client/server、TLS client stream、UDP 和二进制 BUFFER 数据包；C/native 后端共用同一 runtime。
- Windows 原生系统交互：WinHTTP、Schannel、UTF-8 文件路径、消息框、系统打开和 Unicode 文本剪贴板。

> 新增的现代语言特性（ELSE/循环/数组/BOOL/NULL/位运算/字符串/枚举/SYMBOL 代数）详见 [docs/08-language-extensions.md](./docs/08-language-extensions.md)。

## CLI 命令

### 检查源码

```powershell
python -m sonalgebraic check app.sa
```

`check` 只解析和做语义检查，不生成 C，也不调用 C 编译器。需要包时同样可以传 `--pkg`：

```powershell
python -m sonalgebraic check app.sa --pkg build/mathlib.spkg
```

`check` 会尽量一次输出多个错误，并标出对应源码位置：

```text
examples/broken.sa:4:10 error: [SA 40] 变量未声明: missing
40 PRINT missing
         ^^^^^^^
```

`c`、`build`、`run` 也会先执行同一套诊断预检，失败时不会继续生成 C 或调用 C 编译器。

### 编译并运行

```powershell
python -m sonalgebraic run examples/hello.sa
```

`run` 会编译到临时目录后执行生成的程序，并返回程序退出码。`--` 之后的参数会原样转发给被编译的程序：

```powershell
python -m sonalgebraic run app.sa --backend c -- --verbose input.txt
```

### 重排行号

```powershell
python -m sonalgebraic fmt app.sa --renumber 10
```

`fmt` 会重排非空行的行号，空行原样保留。默认原地写回，也可以用 `-o` 输出到另一个文件：

```powershell
python -m sonalgebraic fmt app.sa -o build/app.formatted.sa --renumber 20
```

`USE SYS.LINT AS NONE_NUMBER` 的无行号源码也可以直接 `fmt`，会按 `--renumber` 的步长补齐行号，把草稿固化成带行号的正式源码。

### 编译可执行文件

```powershell
python -m sonalgebraic build app.sa -o build/app.exe
```

默认使用稳定 C 后端。实验性 native 后端可以通过 `--backend native` 启用：

```powershell
python -m sonalgebraic build app.sa -o build/app.exe --backend native
```

native 后端已覆盖数值、字符串、数组/指针、SYMBOL、ERROR/TRY、GOSUB、ENTITY、C FFI、用户模块以及 NET/FILE/DESKTOP runtime 调用；真实构建需要安装 `clang` 或 `zig`。外部模块导出的 ENTITY ABI 仍需按目标平台继续验证。

带用户模块时会生成一个 C 项目目录，里面包含主程序 C、模块 C、模块头文件和 `sa_runtime.h/.c`：

```powershell
python -m sonalgebraic build examples/use_user_module.sa -o build/use_user_module.exe
build/use_user_module.exe
```

### 只生成 C 代码

```powershell
python -m sonalgebraic c examples/use_user_module.sa -o build/use_user_module_project
```

只生成 C 时也可以引用 `.spkg`：

```powershell
python -m sonalgebraic c app.sa -o build/app_project --pkg build/mathlib.spkg
```

生成的 C 只带程序实际够得着的那部分运行时。判定分两级：`SYS.NET` / `SYS.FILE` / `SYS.LIST` / `SYS.MAP` / `SYS.BINARY` / `SYS.DESKTOP` / `SYS.GUI` 这些整块取舍；剩下的公共部分（字符串、异常、SYMBOL 代数、控制台 IO）按符号依赖闭包逐函数取。所以 `PRINT "hi"` 不会背上 300 行的 SYMBOL 求导代码，30 个示例平均下来生成的 C 少了 88%。

### 只生成 LLVM IR

```powershell
python -m sonalgebraic native-ir examples/hello.sa -o build/hello.ll
```

`native-ir` 不要求本机已有 LLVM 工具链，只做前端检查和 LLVM IR 文本生成，方便调试 native 后端。

### 打包 .slib

```powershell
# 源码包
python -m sonalgebraic slib examples/statslib.sa -o examples/statslib.slib

# 带静态二进制库
python -m sonalgebraic slib examples/statslib.sa -o build/statslib_binary.slib --binary

# 带动态库（Windows DLL + import lib / Linux .so / macOS .dylib）
python -m sonalgebraic slib examples/statslib.sa -o build/statslib_dynamic.slib --dynamic
```

### 打包 .spkg

`.spkg` 是自包含的多模块包格式，详细规范见 [docs/06-spkg-format.md](./docs/06-spkg-format.md)。

```powershell
# 单文件包（会作为包的根模块）
python -m sonalgebraic pack examples/mathlib.sa -o build/mathlib.spkg

# 目录包（目录内所有 .sa 作为子模块）
python -m sonalgebraic pack examples/mypkg -o build/mypkg.spkg
```

编译时引用：

```powershell
python -m sonalgebraic build app.sa -o build/app.exe --pkg build/mathlib.spkg
```

### 交叉编译

需要安装 `zig`：

```powershell
python -m sonalgebraic slib examples/statslib.sa -o build/statslib_linux.slib --binary --target x86_64-linux-gnu
python -m sonalgebraic build app.sa -o build/app_linux --target x86_64-linux-gnu
```

### 检查工具链

```powershell
sonc doctor
```

`doctor` 打印 SADK 安装目录、自带工具链目录，以及 `zig` / `gcc` / `clang` / `tcc` / `cl` 各自解析到哪个路径。一个都没有时会说明 `check` / `c` / `fmt` 仍可用、`build` / `run` 会失败，并按当前是安装包还是源码运行给出对应的补救方式。

## 诊断系统

SonAlgebraic 的 CLI 会在 `check`、`c`、`build`、`run` 前先执行诊断预检。发现错误时会停止后续编译动作，并尽量一次输出多条错误。

示例：

```text
examples/broken.sa:2:4 error: [SA 20] F-string 缺少右花括号
20 PRINT F"broken {x"
   ^^^^^^^^^^^^^^^^^^

examples/broken.sa:3:4 error: [SA 30] 无法解析的语句: ELSE
30 ELSE
   ^^^^

examples/broken.sa:4:10 error: [SA 40] 变量未声明: missing
40 PRINT missing
         ^^^^^^^
```

当前诊断策略：

- 语法错误会做轻量恢复：将出错行临时视为 `REM`，继续解析后续行。
- 语义检查会 best-effort 逐语句收集错误，例如多个未声明变量、多个赋值类型不兼容。
- 下划线列号是启发式推断，常见变量名和无法解析语句能较准确定位。
- 遇到结构性大错误时仍可能只能输出部分错误，比如缺失 `.ENDSUB` 或模块本身无法解析。
- 依赖模块内的错误会指向那个模块自己的文件和行，不会被安到主文件的同号行上。
- 诊断文案统一按 UTF-8 输出，管道、重定向和 CI 日志里都不会变成本地代码页乱码。
- `file:行:列` 里的行是**物理行号**，编辑器 ctrl+click、problem matcher 直接可用；SA 逻辑行号（10/20/30…）放在消息开头的 `[SA n]` 里，两者都在，不用二选一。
- 如果 C 编译阶段仍然报错（通常意味着 codegen bug 或 FFI 声明与实际不符），报错末尾会利用生成 C 里的 `/* SA nnn: ... */` 注释，把 C 错误位置映射回可能对应的 SA 源码行。

## 示例程序

仓库内置示例位于 `examples/`：

- `hello.sa`：最小可运行程序，变量声明与输出。
- `functions.sa`：非 `VOID SUB`、参数和 `AS REF`。
- `entity.sa`：基础 `ENTITY` 字段访问。
- `entity_strings.sa`：嵌套 `ENTITY` 字符串字段深拷贝和运行时验证。
- `errors.sa`：`TRY` / `CATCH` / `THROW`。
- `gosub.sa`：`GOSUB` / 无参 `RETURN`。
- `symbol.sa`：`SYMBOL` 公式树捕获和打印。
- `lists.sa`：`SYS.LIST` 数值/字符串动态列表。
- `maps.sa`：`SYS.MAP` 关联容器和 KEYS 遍历。
- `gui_hello.sa`：`SYS.GUI` 窗口、按钮、输入框和事件循环。
- `ptr_basic.sa`、`ptr_arith.sa`、`ptr_cast.sa`：typed pointer、取址、解引用和 CAST。
- `ffi_hello.sa`：C FFI 的 `USEC` / `DECLARE C`。
- `use_math.sa`、`use_io.sa`：内置系统模块导入。
- `mathlib.sa`、`use_user_module.sa`：用户模块分离编译。
- `samath.sa`、`use_samath.sa`：基于 C `math.h` FFI 的 SAMATH 数值计算库，可打包为 `.slib` 原生库。
- `statslib.sa`、`use_statslib.sa`：`.slib` 打包/引用示例。
- `toy.sa`：综合小演示。
- `allexample.sa`：当前主要语言特性的 all-in-one smoke example。

推荐用 `allexample.sa` 快速确认当前工具链：

```powershell
python -m sonalgebraic check examples/allexample.sa
python -m sonalgebraic run examples/allexample.sa
```

## 模块系统

### USE 引用规则

在 SA 源码中：

```basic
10 USE STATSLIB AS ST
```

编译器按以下顺序解析模块：

1. 当前源码目录下的 `.sa` 文件：
   - `USE MATHLIB AS LIB` → 查找 `mathlib.sa`
   - `USE DATA.MODELS AS DATA` → 优先 `data/models.sa`，再回退 `data_models.sa`
2. 当前源码目录下的同名 `.slib`：
   - 例如 `mathlib.slib` 或 `data/models.slib`
3. 编译时通过 `--pkg` 传入的 `.spkg` 包内模块

模块递归加载会检测循环依赖，例如 `A -> B -> A` 会直接报出完整链路：

```text
模块循环依赖: A -> B -> A
```

如果模块内部使用了 `USELIB`，链接阶段会递归汇总主程序和所有用户模块声明的 C 库，避免 FFI 依赖只写在模块里时被漏掉。

### .slib 单模块包

`.slib` 是 zip 包，详细规范见 [docs/07-slib-format.md](./docs/07-slib-format.md)，包含：

- `manifest.json`
- 原始 SA 源码副本
- 生成的模块 C 文件和头文件
- 可选的目标平台静态库/动态库

导出规则：

- `PUBLIC SUB` → 模块头文件
- `CONST` → `extern` 常量
- `ENTITY` → C `typedef struct`
- `DIM` 全局变量 → 模块私有

示例：

```basic
10 USE MATHLIB AS LIB
20 DIM answer AS NUM AS DOUBLE AS VAR
30 SUB main AS PUBLIC AS VOID
40 answer = LIB.SCALE + LIB.twice(4.0)
50 PRINT answer
60 .ENDSUB
70 CALL main
80 END
```

### .spkg 多模块包

`.spkg` 把一个或多个模块（以及未来版本的依赖）bundle 成一个 zip。当前实现为源码包，模块按 `module_to_package` 映射查找。

当前 `.spkg` 已实现的安全校验：

- 解包时拒绝绝对路径、`..`、冒号和反斜杠绕过，避免 zip 路径穿越。
- 解包时拒绝 Windows 保留设备名（`CON` / `NUL` / `COM1` 等，含带扩展名的 `NUL.sa`），避免写到设备而不是磁盘。
- 解包后校验 `manifest.json` 中 `hashes` 声明的 `sha256`。
- 反查每个 `modules[*].source`：实际参与编译的源文件必须被 `hashes` 覆盖，否则报错。省掉条目或把 `hashes` 留空都不能绕过校验。
- 如果 hash 文件缺失、hash 格式不支持或内容被篡改，会直接报编译错误。

`USELIB` 的值会进入 C 编译器命令行，而它可以来自第三方包的源码，所以只接受纯库名（字母数字和 `_ . + -`）和不以 `-` 开头的库文件路径。像 `USELIB "-fplugin=./evil.so"` 这种会被当成编译器选项、在构建期加载任意插件的写法直接报错。

当前 `.spkg` 仍以源码包为主；规范里描述的多 target 二进制 artifact、依赖递归 bundle 和版本冲突处理还在 TODO 中。

> `.slib` 目前**没有**完整性校验：语义检查用的导出签名来自包内 `.sa` 源码的重新解析，而实际链接的是包里的 `.a` / `.dll`，两者不一致时不会被发现。引用来源不明的 `.slib` 前请自行确认。

## FFI

示例：

```basic
10 USEC "stdio.h" AS STDIO
20 USELIB "m" AS M_LIB
30 DECLARE C SUB STDIO.puts(s AS STRING) AS NUM AS LONG
40 SUB main AS PUBLIC AS VOID
50 CALL STDIO.puts("hello from C FFI")
60 .ENDSUB
70 CALL main
80 END
```

语法说明：

- `USEC "header.h" AS H`：生成 `#include "header.h"`
- `USEC <header> AS H`：生成 `#include <header>`
- `USELIB "curl" AS CURL_LIB`：链接阶段加 `-lcurl`；如果是具体文件路径则直接链接该文件
- `DECLARE C SUB H.func(args...) AS return_type`：注册 C 函数签名
- `CPTR`：映射到 C `void*`，可接收和传递不透明指针
- `PTR TO <类型>`：带类型的 C 指针
- `^p`：解引用指针
- `@x`：取变量地址
- `CAST <类型> expr`：强制类型转换
- 指针支持 `+` / `-` 整数偏移

指针示例：

```basic
10 DIM x AS NUM AS LONG AS VAR
20 DIM p AS PTR TO NUM AS LONG AS VAR
30 SUB main AS PUBLIC AS VOID
40 x = 42
50 p = @x
60 ^p = 100
70 PRINT x
80 .ENDSUB
90 CALL main
100 END
```

## 测试

测试基于 pytest，按主题拆分在 `tests/` 下。先装开发依赖：

```powershell
pip install -e .[dev]
```

运行全量回归：

```powershell
python -m pytest
```

需要 C 工具链的测试用标记隔离，CI 上没有编译器时可以跳过：

```powershell
# 纯单元测试，不需要任何 C 编译器
python -m pytest -m "not e2e and not ffi"

# 只跑反向 FFI（C 调 SA 编译出的动态库），需要 gcc
python -m pytest -m ffi
```

测试模块按主题组织：

- `test_codegen.py`：解析 + 语义检查后断言生成的 C 源码。
- `test_semantics.py`：行号、声明、返回路径、alias 等语义约束的负例。
- `test_diagnostics.py`：多错误收集、源码下划线、语法恢复。
- `test_packaging.py`：模块分离编译、`.slib` 三态、`.spkg` 打包/hash/路径安全、循环依赖。
- `test_cli.py`：`check` / `run` / `fmt` 退出码与诊断输出。
- `test_e2e.py`（`e2e` 标记）：`hello.sa`、`entity_strings.sa` 真正编译运行。
- `test_ffi_reverse.py`（`ffi` 标记）：C 程序 `#include` 头并链接调用 SA 编译出的 DLL，验证反向 FFI。
- `test_regressions.py`：审计发现的缺陷回归，含两条结构性防护——
  - runtime 头文件与实现的一致性：codegen 会发射的每个 `sa_*` 都必须在 `RUNTIME_HEADER` 里有声明，防止模块模式下退化成隐式声明；
  - C / native 双后端差分：同一份 `.sa` 用两个后端各跑一遍比对 stdout，专门抓「两边都能跑但结果不同」的偏差（`e2e` 标记）。
- `test_ondemand_runtime.py`：运行时按需注入。守两头——**别多塞**（`PRINT "hi"` 不能带上 SYMBOL 求导代码）和**别少塞**（每个示例都真过一遍 C 编译器）。注入不足会直接编译失败而不是静默出错，所以后者是这里最硬的防线。另有两条不依赖编译器的自检：片段拼回去必须逐行等于原 `RUNTIME_IMPL`，以及没有任何片段引用无处定义的符号。

缺少对应 C 工具链时，`e2e` / `ffi` 测试会自动 skip 而不是失败。

安装包另有一套 super smoke，从 exe 装起，把整套 SDK 过一遍再卸干净：

```powershell
python installer/smoke.py --with-zig --integration
```

它测的是「用户双击安装包之后拿到的东西」——冻结产物有没有漏模块、安装布局对不对、只靠自带 zig 能不能编译、卸载后 PATH 有没有精确还原。这几类问题从源码测试里看不出来，所以它没有接进 pytest。详见 [installer/README.md](./installer/README.md)。

## 运行时语义补充

### ENTITY 字符串字段

`ENTITY` 中的 `STRING` 字段会被编译器按值语义管理：

- 声明局部/全局 ENTITY 时，字符串字段初始化为空字符串。
- `second = first` 这类整体赋值会深拷贝字符串字段。
- 按值参数传入 ENTITY 时，会复制字符串字段，函数内修改不会影响外部对象。
- 局部/全局 ENTITY 生命周期结束时会递归释放字符串字段。
- 嵌套 ENTITY 中的字符串字段也会递归处理。

示例：

```basic
10 FOR ENTITY AS NameBox
20 DIM text AS STRING AS VAR
30 .ENDENTITY
40 FOR ENTITY AS Profile
50 DIM name AS ENTITY AS NameBox AS VAR
60 DIM score AS NUM AS LONG AS VAR
70 .ENDENTITY
80 SUB main AS PUBLIC AS VOID
90 DIM first AS ENTITY AS Profile AS VAR
100 DIM second AS ENTITY AS Profile AS VAR
110 first.name.text = "LANS"
120 second = first
130 second.name.text = "SA"
140 PRINT first.name.text
150 PRINT second.name.text
160 .ENDSUB
170 CALL main
180 END
```

### 局部资源清理

局部 `STRING`、`SYMBOL`、`ERROR` 以及含托管字段的 `ENTITY` 会在常规函数路径上清理。非 `VOID RETURN` 会先保存返回值，再清理局部资源，再返回。

`THROW` 逃逸也会清理当前 SUB 的局部资源：`THROW` 被拆成 `sa_raise_*`（装入错误）→ 清理本帧局部 → `sa_throw_dispatch`（longjmp 或退出）三步，保证跳走前不泄漏。无匹配 `CATCH` 向外层重抛同样会先清理本帧。程序结束时还会释放运行时全局错误对象残留的 message。

异常**穿过**某个 SUB（即该 SUB 调用的下层函数抛出、且本 SUB 没有 `TRY` 接住）时也已覆盖：编译器给每个「持有托管局部、又调用用户/外部 SUB」的语句注入一个只做清理的 per-call landing pad（`sa_try_top++` + `SA_SETJMP`，失败分支释放本帧局部后 `sa_throw_dispatch` 继续向外重抛）。这样局部资源在 `longjmp` 越过该帧之前就被释放，全链路（显式 `THROW`、无匹配 `CATCH` 重抛、穿透传播）在 `-O2` 下均经 malloc/free 计数桩验证净分配为 0。

### SYMBOL

`SYMBOL` 当前用于捕获表达式树并打印公式字符串，例如：

```basic
10 DIM a AS NUM AS LONG AS VAR
20 DIM expr AS SYMBOL AS VAR
30 SUB main AS PUBLIC AS VOID
40 a = 7
50 expr = a + 2
60 PRINT expr
70 .ENDSUB
80 CALL main
90 END
```

输出类似：

```text
(a + 2)
```

当前还没有 `sa_symbol_clone`，所以 `ENTITY` 内的 `SYMBOL` 字段暂不做深层 clone/free 托管。

## 当前支持

- 强制递增行号；`USE SYS.LINT AS NONE_NUMBER` 可省略手写行号
- `DIM` / `CONST`
- `SUB ... .ENDSUB`
- `CALL` / `END`
- `PRINT` / `F"...{expr}..."`
- `IF ... THEN` / `END IF`
- `GOTO ::label` / `::label`
- `RETURN`
- `USE SYS.IO AS IO` / `IO.INPUT`
- `CLS`
- `NUMBER()` / `STRING()`
- 非 `VOID` 返回值 `SUB`
- 子程序参数与 `AS REF`
- 局部 `DIM` / `CONST`
- `FOR ENTITY AS ...` / `.ENDENTITY` 基础结构体和值字段访问
- `ENTITY` 字符串字段初始化、整体赋值深拷贝、局部/全局释放
- 局部 `STRING` / `SYMBOL` / `ERROR` 常规路径清理
- `ERROR` / `TRY CALL ... TRACEBACK ERROR AS ...` / `CATCH` / `THROW`
- `GOSUB ::label` / 无参 `RETURN`（C 后端使用纯 C 的整数返回栈 + `switch` 分发）
- `SYMBOL` 基础符号树捕获、打印、求导、化简、代入和求值，支持 `**` 幂运算
- `PTR TO <类型>`、`^` 解引用、`@` 取址、`CAST`
- 名义化原生句柄 `HANDLE AS <Kind>`；标准库提供 FILE、BUFFER、NET_STREAM、TCP_LISTENER、UDP_SOCKET
- `USE SYS.MATH AS <任意别名>` 的内置 `<别名>.PI` / `<别名>.POW()`；`**` 作为语言级幂运算符
- `USE SYS.IO AS <任意别名>` 的 `<别名>.INPUT`
- `SYS.BINARY`：BUFFER 创建/切片/复制、HEX、大小端 U16/U32/U64、校验和
- `SYS.LIST`：可变长动态列表，数值 LIST 与字符串 STR_LIST 两种句柄 kind，PUSH/POP/GET/SET/INSERT/REMOVE/JOIN
- `SYS.MAP`：STRING key 关联容器，数值 MAP 与字符串 STR_MAP 两种句柄 kind，SET/GET/HAS/REMOVE/KEYS，KEYS 产出 STR_LIST 与 SYS.LIST 打通
- `SYS.GUI`：原生窗口 GUI（WINDOW/BUTTON/LABEL/TEXTBOX），轮询式 `WAIT_EVENT` 事件循环，control id 分发，无需回调；Windows 用 Win32，Linux/macOS 有 GTK3 开发文件时自动启用 GTK 后端
- `SYS.NET`：HTTP/HTTPS、DNS、TCP client/server、TLS client stream、UDP、字符串与 BUFFER 收发
- `SYS.FILE`：文件句柄读写/定位/关闭、文本便捷读写、存在性、目录、删除、当前目录和绝对路径
- `SYS.DESKTOP`：Windows 消息框、系统打开路径/URL、Unicode 文本剪贴板
- 用户模块 `USE MATHLIB AS LIB` 分离编译和头文件导出
- 用户模块循环依赖诊断
- 模块内 `USELIB` 递归汇总参与最终链接
- `.sa` 模块可编译为 `.slib` 包，被另一个 `.sa` 通过 `USE` 引用
- `.slib --binary` 打包目标平台静态库
- `.slib --dynamic` 打包目标平台动态库
- `.spkg` 多模块自包含包，支持 `sonc pack` 和 `sonc build --pkg`，并校验 hash / 安全解包
- FFI：`USEC`、`USELIB`、`DECLARE C`、`CPTR`
- CLI 多错误诊断和源码下划线
- `sonc fmt --renumber` 行号重排

## 当前限制

- 用户模块当前支持 `PUBLIC SUB`、`CONST`、`ENTITY` 的最小导出；模块级可见性和更复杂的跨模块 ENTITY ABI 生命周期还没做。
- 动态库 `.slib` 当前要求运行时可执行文件与 DLL/SO/dylib 同目录（或对应 `rpath` 目录）。
- 动态 `.slib` 暂不支持依赖 net/binary/file/desktop 等进程内 runtime 状态的模块；这类模块请使用源码或静态 `.slib`，避免 DLL 与主程序各持有一套句柄槽位。
- `.spkg` 当前版本为源码包，二进制产物、依赖递归 bundle、版本冲突处理后续再补强。
- FFI 当前支持 C 函数调用、`CPTR` 不透明指针和 `PTR TO <类型>` 类型指针；C struct 字段访问、回调、字符串所有权转换等需要后续扩展。
- 非本机 `--target` 需要 `zig`。
- `SYMBOL` 超越函数直接建树的表层语法还没接入；幂求导内部会生成 `LOG(...)` 节点。`DERIV` 已覆盖 `EVAL` / `SIMPLIFY` 支持的全部六个函数（LOG/EXP/SIN/COS/TAN/SQRT）。
- native 后端暂不支持 `NUM AS FLOAT`，会报明确错误。不把它映射成 `double` 是因为 `ENTITY` 里的 FLOAT 字段会从 4 字节变 8 字节，与 C 后端编出来的模块 struct 布局对不上。这类程序请用 `AS DOUBLE` 或 C 后端。
- 局部声明是块作用域：`IF` / `FOR` / `WHILE` / `CATCH` 块内的 `DIM` 在块外不可见（与生成 C 的 `{ }` 一致），兄弟分支可以声明同名变量，但不允许遮蔽外层已有的同名局部。
- 数组常量下标越界在编译期报错；变量下标越界仍是运行期未定义行为，不做边界检查。
- `ENTITY` 内 `SYMBOL` 字段暂不做深层 clone/free 托管，避免浅拷贝导致双释放；后续需要给 SYMBOL runtime 增加 clone 能力。
- MSVC 支持仍需要完整验证；`GOSUB` 本身已不再依赖 GCC/Clang 的 label-address 扩展。
- `HANDLE` 是可复制的资源 token，不会自动关闭；FILE、BUFFER、LIST、STR_LIST、MAP、STR_MAP、NET_STREAM、TCP_LISTENER、UDP_SOCKET 都必须调用对应 CLOSE。runtime 会让已关闭句柄的旧副本失效，并在进程退出时兜底清理。
- `SYS.LIST` / `SYS.MAP` 数值元素按 `DOUBLE` 存储，约 2^53 以内整数无损；需要精确 64 位整数序列时用 `SYS.BINARY`。
- BUFFER 返回值必须直接赋给 `HANDLE AS BUFFER` 变量（或从同类型 SUB 直接 RETURN），不能匿名嵌套进参数/F-string；否则无法显式 CLOSE。
- POSIX TLS/HTTPS 需要 OpenSSL 开发文件和 `libssl` / `libcrypto`；Windows 使用系统 Schannel，不要求外部 TLS SDK。
- POSIX 网络 runtime 当前只支持 HTTP，HTTPS 仍需接入 TLS 库；`SYS.DESKTOP` 的非 Windows 实现当前返回失败并提供 `LAST_ERROR()`，不会拼 shell 命令执行用户输入。
- `SYS.GUI` 在 Linux/macOS 依赖 GTK3 开发文件（`pkg-config` + `gtk+-3.0`，如 `libgtk-3-dev`）；未安装时编译仍成功，但 GUI 函数运行时返回失败并提供 `LAST_ERROR()`。交叉编译目标不启用 GTK 后端。
