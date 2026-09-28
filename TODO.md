# SonAlgebraic TODO

这份清单按“先让真实项目不炸，再让生态好用”的顺序排。

## P0: 语义与运行时硬坑

- [x] 补齐协程异常终结、取消与临时资源清理，跨 Promise 保留原错误类型、消息和位置；失败取值释放 Promise 后重抛，覆盖重复失败、多层 AWAIT 和异常后继续调度。
- [x] ENTITY 内 SYMBOL 字段接入 C/native 深拷贝与释放，覆盖嵌套、自赋值、REF 别名、指针解引用整体赋值、返回及借用/移动。
- [x] 异步返回值与 PROMISE 结果类型限制前移至语义检查，`sonc check` 提前报告不支持的类型。

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
- [x] 函数模型首期：`PTR TO SUB` 函数引用（FFI 上即 C 函数指针）、`NEW SUB ... FROM` 引用计数 callable、`CALLRET`、`SYS.GUI.ON_CLICK` / `RUN` 回调式事件循环（C / native 后端）。
- [ ] 函数模型后续：Lambda 与闭包捕获、`ENTITY` 方法、`ASYNC SUB` 里的 `NEW SUB` / `CALLRET`、`TRY CALL` 经 callable 调用、`SUB` 元素数组。
- [x] native 手写 LLVM IR 异步状态机：堆帧、start/resume/cleanup、PROMISE/CALL/AWAIT/SYNC、原错误传播与取消回收，覆盖跨挂起控制流、模块启动 ABI 和异步网络。

### native 异步：剩余边界拆解

现状与使用限制见 [异步文档 12.6](./docs/12-async.md#126-native-后端的实现与边界)。下列任务按可独立实现、验收的小步拆分；优先级标在任务内，依赖使用 `NA-xx` 编号。

#### 所有权与前端前置项

- [ ] **NA-01 · P0：明确并落实 Promise 单消费者所有权。** 统一 C/native 对 `q = p`、参数传递、返回、重复等待和覆盖旧任务的规则；明确哪些操作移交、借用或拒绝，并对齐块退出时的释放时机。
  - 验收：别名消费、重复消费、覆盖未完成任务、跨块持有和失败后重用均有受检行为；合法路径在退出兜底前无存活 Promise 槽、无净分配。以此作为集合内 Promise 的所有权基础。
- [ ] **NA-02 · P1：打通 ERROR 按值实参的前端与资源语义。** 核对 `analysis/typesys.py` 的可赋值检查与 ERROR 参数声明规则，使合法的 ERROR → ERROR 按值调用可直接从 SA 源码进入同步/异步后端。
  - 验收：替换当前直接 ERROR 快照测试中的 IR 桥接，用真实 SA 调用验证消息独立、来源元数据保留，以及调用方改值/释放后完成、失败、取消均安全。

#### 数组与 ENTITY 帧资源

- [ ] **NA-03 · P1：支持异步定长值类型数组按值参数。** 将数组实参复制进堆帧，先覆盖 NUM/BOOL/HANDLE 等无需递归析构的元素，补齐声明、调用和索引访问链路。
  - 验收：调用方在启动后改写数组不影响任务，跨 AWAIT 后仍读到快照；未启动取消及正常完成回收帧。HANDLE 元素继续遵守显式关闭约定。
- [ ] **NA-04 · P1：支持托管数组参数及 ENTITY 内托管数组字段。** 依赖 NA-03；按前端允许的元素类型统一逐元素初始化、深拷贝/retain、移动清零和释放，覆盖嵌套 ENTITY。
  - 验收：STRING 数组与嵌套托管数组的自赋值、快照、临时值、借用/移动、跨 AWAIT、失败和取消均无泄漏或重复释放，再移除对应帧类型拒绝。
- [ ] **NA-05 · P1：支持 PROMISE 数组的逐元素所有权。** 依赖 NA-01；补齐元素初始化、存入/取出、消费、替换和数组析构，沿用确定的移交规则处理整体操作。
  - 验收：数组混合未启动、挂起、完成、失败任务时，逐项等待或整体清理都正确；不合法的复制有明确诊断，级联取消后等待关系与槽位清空。
- [ ] **NA-06 · P1：支持 ENTITY 内 PROMISE 字段。** 依赖 NA-01，数组字段同时依赖 NA-05；将 Promise 纳入嵌套实体的资源判定和递归清理，明确整体赋值、按值传参、返回及借用/移动规则。
  - 验收：含 Promise 的实体在协程快照、临时接管、跨 AWAIT 和取消路径中始终只有约定的持有者；普通字段与 Promise 字段组合不漏清理、不重复消费。

#### 跨 C ABI 聚合参数

- [ ] **NA-07 · P1：统一外部 ENTITY 的声明与布局。** 在 native 中完整生成实际使用的外部实体及嵌套类型声明，区分内部布局与 C ABI 的 BOOL、FLOAT、数组、对齐和填充。
  - 验收：用 C 的 `sizeof` / `offsetof` 与 native 探针核对字段布局；跨模块嵌套实体可寻址，差异通过显式转换处理。
- [ ] **NA-08 · P1：实现异步 `_start` 的 ENTITY/ERROR 按值参数 ABI 转换。** 依赖 NA-07，ERROR 的真实 SA 验收依赖 NA-02；按目标 ABI 将内部聚合值转换成实际 C 参数传递形式，启动器取得独立资源快照。
  - 验收：主程序手写 IR 调用 C 模块的聚合异步参数，覆盖含 STRING/SYMBOL/ERROR 的实体、BOOL/FLOAT 字段、临时实参，以及完成/异常/取消；通过后移除当前跨 C ABI 拒绝。

#### 跨结构化作用域跳转

- [ ] **NA-09 · P2：定义协程跨作用域跳转规则与分析。** 为标签和 GOTO/GOSUB 建立作用域、初始化状态和异常区域信息；明确允许的跳出/跳入路径及禁止绕过的声明、借用和 TRY 边界。
  - 验收：合法与非法跳转都有语义用例，错误指向 SA 行号；不以简单删除 `validate_async_jumps()` 拒绝检查作为完成。
- [ ] **NA-10 · P2：实现跨作用域 GOTO 的资源清理与恢复。** 依赖 NA-09；跳出时清理退出的资源层，允许的重入路径正确初始化，所有控制流入口满足 LLVM SSA 支配要求。
  - 验收：IF/FOR/WHILE 的前向/回跳、声明重新执行、AWAIT 前后跳转及异常区域边界均按规则运行；未执行声明不读垃圾值，重复跳转无泄漏。
- [ ] **NA-11 · P2：实现跨作用域 GOSUB 与返回恢复。** 依赖 NA-09/NA-10；明确往返调用期间哪些调用方资源保持存活，返回栈保存必要的作用域状态，返回、异常和取消分别执行对应清理。
  - 验收：嵌套 GOSUB 跨块并在子段 AWAIT 后正确返回；资源不会在暂离时提前释放，异常/取消不遗留引用或破坏异常栈。

#### 目标平台异常 ABI

- [ ] **NA-12 · P1：抽象 native 的目标布局与异常跳转 ABI。** 将当前固定的 `_setjmp(env, frameaddress)`、异常缓冲布局和相关 LLVM 属性收敛到目标配置，保证生成 IR 与链接 runtime 使用同一 ABI。
  - 验收：现有 Windows x64 clang 回归保持通过；不支持的目标在构建阶段明确诊断。不得将 setjmp 包进返回后栈帧失效的普通 C wrapper。
- [ ] **NA-13 · P2：逐目标适配并验收异常 ABI。** 依赖 NA-12；分别推进 Windows GNU/MinGW、Linux、macOS，并验证各平台可用的 Zig 工具链路径。
  - 验收：每个宣称支持的目标均真实运行 `-O0/-O2` 的 TRY/CATCH、AWAIT/SYNC 失败、多层传播、取消和栈平衡测试；跨 C/native 异常先建立有效的纯 C 对照探针。
- [ ] **NA-14 · P2：将异步验收接入平台 CI 矩阵。** 依赖对应目标的 NA-13；接入下方“Windows / Linux / macOS / Zig 的 CI 矩阵”任务，持续跑状态机、资源清理、模块 ABI 和本机 TCP 用例。
  - 验收：工具链缺失与功能失败分开报告，目标用例有真实执行记录；采用各平台可用的分配插桩，在退出兜底前检查 Promise 槽、异常栈及净分配。

建议先做 NA-01/NA-02；数组链、聚合 ABI 链、目标 ABI 链随后可独立推进，跨作用域跳转在规则明确后实施。各项完成时同步更新 `docs/12-async.md` 的限制及对应拒绝测试。

## P2: CLI 与开发体验

- [x] 增加正式异步用户文档 `docs/12-async.md`，完整示例纳入文档检查，说明三种取值、并发、网络、错误传播、单次消费及取消边界。

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
