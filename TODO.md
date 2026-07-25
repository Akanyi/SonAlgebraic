# SonAlgebraic TODO

这份清单按“先让真实项目不炸，再让生态好用”的顺序排。

## P0: 语义与运行时硬坑

- [x] 修复 `RETURN` 出现在 `IF` 内时的语义检查参数传递问题，确保报 SA 编译错误而不是 Python 异常。
- [x] 为非 `VOID SUB` 增加返回路径检查，避免生成缺少返回值的 C 函数。
- [x] 修复 `AS REF` 参数传入 ENTITY 字段时的 C 取址生成，例如 `CALL bump(hero.pos.x)` 应生成字段地址。
- [x] 梳理局部 `STRING` / `SYMBOL` / `ERROR` 的释放策略，避免 SUB 内临时资源泄漏。
- [x] 补强 `ENTITY` 字符串字段的初始化、赋值、深拷贝和释放语义。
- [x] 决定并落地 `GOSUB` 后端策略：改成纯 C 的整数返回栈 + `switch` 分发，避免 GCC/Clang label-address 扩展。

## P1: 包系统与模块生态

- [x] 让 `sonc c` 支持 `--pkg`，与 `sonc build --pkg` 行为一致。
- [x] 从主程序和所有用户模块收集 `USELIB`，保证模块内部 FFI 依赖能参与最终链接。
- [x] 为 `.spkg` 增加 hash 校验，解包后验证 manifest 中声明的文件完整性。
- [x] 给 `.spkg` 解包加路径安全检查，阻止 zip 路径穿越。
- [ ] 实现 `.spkg` 二进制 artifact 选择逻辑：target 命中优先二进制，否则 fallback 源码。
- [ ] 支持 `.spkg` 依赖递归 bundle 和版本冲突诊断。
- [x] 为模块循环依赖增加显式检测和可读错误。
- [ ] 完整实现模块级 `PUBLIC` / `PRIVATE` 可见性，覆盖 `SUB`、`CONST`、`ENTITY`。

## P1: 语言能力

- [x] 增加基础数组或固定长度 buffer 语法，避免用户直接靠指针模拟一切集合（`DIM xs[N]` 定长数组，值类型元素）。
- [x] 增加可变长集合：`SYS.LIST` 动态列表（数值 LIST + 字符串 STR_LIST 两种句柄 kind，C/native 双后端）。
- [x] 增加关联容器：`SYS.MAP` STRING key 哈希映射（数值 MAP + 字符串 STR_MAP，KEYS 产出 STR_LIST，C/native 双后端）。
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

## P2: 测试与兼容性

- [x] 增加端到端运行测试，不只断言生成 C 字符串。
- [x] 增加诊断负例测试：多语法/语义错误、CLI 退出码、源码下划线。
- [ ] 增加负例测试集：重复符号、循环依赖、错误返回路径、REF 非左值、ENTITY 字段错用。
- [x] 增加 `.slib` / `.spkg` 隔离目录回归测试，覆盖源码包、静态库、动态库和包内模块（`tests/test_packaging.py`）。
- [ ] 建立 Windows / Linux / macOS / Zig 的 CI 矩阵。
- [ ] 明确 MSVC 支持状态；`GOSUB` 已移除非标准 C 扩展，但整体工具链仍需验证。
