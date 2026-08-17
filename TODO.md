# SonAlgebraic TODO

这份清单按“先让真实项目不炸，再让生态好用”的顺序排。

## P0: 语义与运行时硬坑

- [x] 修复 `RETURN` 出现在 `IF` 内时的语义检查参数传递问题，确保报 SA 编译错误而不是 Python 异常。
- [x] 为非 `VOID SUB` 增加返回路径检查，避免生成缺少返回值的 C 函数。
- [x] 修复 `AS REF` 参数传入 ENTITY 字段时的 C 取址生成，例如 `CALL bump(hero.pos.x)` 应生成字段地址。
- [x] 梳理局部 `STRING` / `SYMBOL` / `ERROR` 的释放策略，避免 SUB 内临时资源泄漏。
- [x] 补强 `ENTITY` 字符串字段的初始化、赋值、深拷贝和释放语义。
- [x] 决定并落地 `GOSUB` 后端策略：改成纯 C 的整数返回栈 + `switch` 分发，避免 GCC/Clang label-address 扩展。
- [x] 补齐 `RUNTIME_HEADER` 缺失的 `sa_list_*` / `sa_strlist_*` / `sa_map_*` / `sa_strmap_*` / `sa_gui_*` 声明：用户模块 + `SYS.LIST`/`MAP`/`GUI` 的组合以前直接编译失败（隐式声明）。已加一致性测试防止再次漂移。
- [x] 修 POSIX 上 `_stricmp` 垫片定义在首个使用点之后的问题：任何启用 `SA_ENABLE_FILE` 的程序在 Linux/macOS 都编不过。
- [x] 修 `GOTO` 击穿非 `VOID SUB` 返回路径分析：标签是控制流汇合点，不能在倒序扫描里当透明跳过，否则生成没有 return 的非 void C 函数（编译零警告、运行返回垃圾值）。
- [x] 给局部声明加块作用域：`IF`/`FOR`/`WHILE` 块内 `DIM` 在块外不再可见（以前通过语义检查但 C 编译失败），兄弟分支同名声明不再被误报重复。
- [x] 让 `check_return` 递归进 `TRY`/`CATCH`：`CATCH` 里的 `RETURN` 以前完全不校验类型，VOID SUB 里写 `RETURN 42` 会生成 `void tmp = 42;`。
- [x] 给 `NUMBER()` / `STRING()` 加参数个数和类型校验：`NUMBER(数值)` 以前会生成把整数当 `const char*` 解引用的调用。
- [x] 常量数组下标越界在编译期报错，不再生成裸越界访问。
- [x] 补齐 `DERIV` 对 `TAN` / `SQRT` 的支持：以前静默返回导数 0，与 `EVAL`/`SIMPLIFY` 的支持面不一致。

## P1: 包系统与模块生态

- [x] 让 `sonc c` 支持 `--pkg`，与 `sonc build --pkg` 行为一致。
- [x] 从主程序和所有用户模块收集 `USELIB`，保证模块内部 FFI 依赖能参与最终链接。
- [x] 为 `.spkg` 增加 hash 校验，解包后验证 manifest 中声明的文件完整性。
- [x] 给 `.spkg` 解包加路径安全检查，阻止 zip 路径穿越。
- [x] 修 `sonc pack <目录>` 必崩：`sa_files` 存的是用户源目录的原始路径而不是拷贝后的包内路径，`relative_to` 直接抛 `ValueError`。多模块目录打包此前从未工作过。
- [x] hash 校验反查覆盖面：清空或省略 `hashes` 条目就能让模块源码零校验参与编译，现在会报错。
- [x] `.spkg` 解包拒绝 Windows 保留设备名（`CON`/`NUL`/`COM1`，含 `NUL.sa` 形式）。
- [x] 收紧 `USELIB`：只接受纯库名和不以 `-` 开头的库文件路径，堵掉第三方包用 `USELIB "-fplugin=./evil.so"` 在构建期加载任意插件的路径。
- [ ] 给 `.slib` 加完整性校验：导出签名来自包内源码重新解析，链接的却是包里的二进制，两者不一致时无人发现。
- [ ] 实现 `.spkg` 二进制 artifact 选择逻辑：target 命中优先二进制，否则 fallback 源码。
- [ ] 支持 `.spkg` 依赖递归 bundle 和版本冲突诊断。
- [x] 为模块循环依赖增加显式检测和可读错误。
- [ ] 完整实现模块级 `PUBLIC` / `PRIVATE` 可见性，覆盖 `SUB`、`CONST`、`ENTITY`。

## P1: 语言能力

- [x] 增加基础数组或固定长度 buffer 语法，避免用户直接靠指针模拟一切集合（`DIM xs[N]` 定长数组，值类型元素）。
- [x] 增加可变长集合：`SYS.LIST` 动态列表（数值 LIST + 字符串 STR_LIST 两种句柄 kind，C/native 双后端）。
- [x] 增加关联容器：`SYS.MAP` STRING key 哈希映射（数值 MAP + 字符串 STR_MAP，KEYS 产出 STR_LIST，C/native 双后端）。
- [x] 增加窗口 GUI：`SYS.GUI` Win32 原生控件 + 轮询式 WAIT_EVENT 事件循环（POSIX 返回失败 + LAST_ERROR）。
- [x] 增加 `ELSE` / `ELSE IF`，补齐基础条件分支（并改进非 VOID SUB 返回路径分析）。
- [x] 增加基础循环语法，至少提供比 `GOTO` 更可维护的循环形式（`FOR ... TO ... STEP` / `WHILE`）。
- [x] 引入 `BOOL` 或明确布尔表达式统一类型规则（`BOOL` 类型 + `TRUE`/`FALSE`，比较/逻辑运算返回 BOOL）。
- [~] 补强数值字面量：科学计数法、十六进制、下划线分隔已支持；非法数字格式诊断仍待补。
- [x] 为字符串增加标准操作：`SYS.STRING` 提供 LENGTH/CONCAT/SLICE/FIND/UPPER/LOWER/REPLACE。
- [x] 推进 `SYMBOL` 的代数接口：化简、求导、代入、数值求值（SIMPLIFY/DERIV/SUBST/EVAL 全部实现）。
- [x] 新增 `NULL` 字面量、位运算（BAND/BOR/BXOR/BNOT/SHL/SHR）、`ENUM` 枚举、完整集内置常量（PI/E/TAU/MAX_LONG/NEWLINE/TAB 等）。

## P2: CLI 与开发体验

- [x] 增加 `sonc check <source>`，只做解析和语义检查，不生成 C，不调用 C 编译器。
- [x] 增加 `sonc run <source>`，编译并执行程序。
- [x] 增加行号重排工具，例如 `sonc fmt app.sa --renumber 10`。
- [ ] 增加 `sonc init`，生成最小项目结构。
- [x] 将 C 编译错误尽量映射回 SA 源码行号。
- [x] 为 `check/c/build/run` 增加多错误诊断预检和源码下划线显示。
- [x] VSCode 语法高亮扩展（`editors/vscode/`，tmLanguage）。
- [ ] 增加正式项目配置文件，例如 `sonalgebraic.toml`。
- [x] 增加 Python 分发配置，声明 `jinja2`、`pydantic` 等依赖（`pyproject.toml`，含 `dev` 组的 pytest）。
- [x] Windows 安装包 SADK（`installer/`）：PyInstaller 冻结出不依赖 Python 的 `sonc.exe`，Inno Setup 做向导——组件选择、PATH 注册、`.sa` 关联与右键菜单、VSCode 扩展。C 工具链不捆绑而是按需从 zig 官方源下载并校验 SHA-256（本机已有 gcc/clang/zig 时默认不勾），装进 `toolchain/` 的 zig 由 `core/sdk_env.py` 前置进进程 PATH，不写系统环境变量也能用。新增 `sonc doctor` 报告探测结果。
- [ ] 给安装包做代码签名：现在没证书，Windows SmartScreen 会对下载来的 `SADK-Setup-*.exe` 报「未知发布者」。
- [x] 诊断输出统一钉成 UTF-8：Windows 上管道/重定向以前退回本地代码页，中文全是乱码，西文 locale 更会直接 `UnicodeEncodeError`。
- [x] CLI 不再对文件不存在、路径是目录、非 UTF-8 源码抛裸 Python traceback。
- [x] `sonc fmt` 支持 `USE SYS.LINT AS NONE_NUMBER` 无行号源码——这恰恰是补行号最该覆盖的场景。
- [x] 依赖模块内的诊断指回模块自己的文件和行，不再被安到主文件的同号行上（以前文件、行内容、下划线三者全错）。
- [x] `sonc run` 支持把 `--` 之后的参数转发给被编译的程序。
- [x] 修 native 后端 alloca 重构的半成品：alloca 行收集到 `entry_allocas` 后从没插回 entry 块，`c_main` 更是连重置都漏了，会继承上一个函数的残留。当时 6 个 native 测试全红。顺带把 F-string builder、INPUT 4KB 缓冲、块内 DIM 这些真正落在循环体里的 alloca 也迁完——重构本来就是为它们做的。
- [x] 拆开 `native/llvmir.py`（3007 行 / 单类 130+ 方法）：按职责切成 `base`（状态与发射设施）、`types`、`entities`、`stmts`、`exprs`、`builtins`、`runtime_decls` 七个模块 + `gen` 主干，mixin 组合。对外只经 `backend.native` 包导出 `generate_native_llvm_ir`。拆分以「生成的 IR 逐字节不变」为验收标准，28 个示例全部通过。
- [x] 运行时按需注入：以前整份 RUNTIME 文本都塞进生成的 .c，靠 `#ifdef` 让预处理器裁——砍了编译量没砍文本量，`PRINT "hi"` 的 .c 里 98.6% 是运行时，还白背 300 行 SYMBOL 求导。改成 Python 侧就只输出够得着的部分：feature 区整块取舍，无条件区按符号依赖闭包逐函数取。30 个示例平均降 88%（hello 4193 → 270 行）。三个注入点（单文件 / 模块 / native）统一走 `backend/runtime_slicer.py`。
- [x] 模块模式加链接期裁剪（`-ffunction-sections` + `--gc-sections`，macOS 用 `-dead_strip`，MSVC 用 `/Gy` + `/OPT:REF`）。Windows/MinGW 上实测是负收益——ld 确实丢了 95 个节区，但 PE 的节区对齐开销比裁掉的还多（113 KB → 115 KB），所以该平台不给这组 flag。

## P2: 测试与兼容性

- [x] 增加端到端运行测试，不只断言生成 C 字符串。
- [x] 增加诊断负例测试：多语法/语义错误、CLI 退出码、源码下划线。
- [ ] 增加负例测试集：重复符号、循环依赖、REF 非左值、ENTITY 字段错用（返回路径、块作用域、CATCH 返回、数组越界已在 `tests/test_regressions.py` 覆盖）。
- [x] 增加 `.slib` / `.spkg` 隔离目录回归测试，覆盖源码包、静态库、动态库和包内模块（`tests/test_packaging.py`）。
- [x] 增加 C / native 双后端差分测试：同一份 `.sa` 两个后端比对 stdout，抓「两边都能跑但结果不同」的偏差。已借此定位并修掉 AND/OR 不短路和 `NUM AS FLOAT` 截断。
- [x] 增加 runtime 头文件与实现的一致性测试，防止 `RUNTIME_HEADER` 再次漏声明导致模块模式退化成隐式声明。
- [x] 安装包 super smoke（`installer/smoke.py`）：从 exe 装起，验证安装布局、CLI、诊断、代码生成、端到端编译运行、`.slib`/`.spkg` 打包、只靠自带 zig 的隔离编译，再卸载并逐字节比对用户 PATH 是否精确还原。测的是冻结产物和安装形态，这类问题源码测试一个都看不出来（比如 `jinja2` 只在模块头文件生成路径上被用到，冻结漏包时单文件示例全绿）。
- [ ] 建立 Windows / Linux / macOS / Zig 的 CI 矩阵。
- [ ] 明确 MSVC 支持状态；`GOSUB` 已移除非标准 C 扩展，但整体工具链仍需验证。
- [ ] native 后端补齐 `NUM AS FLOAT`：需要真 `float` 类型（含 ENTITY 字段布局和 FFI 调用约定），当前是明确报错而不是静默截断。
