# 示例导航

按主题选择入口；下面命令均从仓库根目录运行。已安装 SADK 时，将 `python -m sonalgebraic` 换成 `sonc`，路径从安装目录起算。

## 推荐顺序

1. [变量与输出](basics/hello.sa) → [子程序与引用参数](basics/functions.sa) → [函数引用与 callable](basics/function_values.sa)。
2. [分支与字面量](basics/conditions_and_literals.sa) → [数组与循环](basics/arrays_and_loops.sa)。
3. [实体深拷贝](data/entity_strings.sa) → [复制、借用、移动](data/ownership.sa) → [符号代数](symbolic/algebra_and_enums.sa)。
4. [模块项目](modules/basic/main.sa) → [Promise 与 AWAIT](async/basics.sa) → [异步错误传播](async/error_propagation.sa)。
5. [语言综合演示](showcase/language_tour.sa)。

```powershell
python -m sonalgebraic run examples/basics/hello.sa
python -m sonalgebraic run examples/async/basics.sa --backend native
python -m sonalgebraic check examples/showcase/language_tour.sa
```

## 全部示例

标记为 **自动** 的程序会自行退出，可在隔离的工作目录中批量运行；**手动** 程序需要输入、桌面交互或网络。两类程序都参与解析与构建检查。模块库通过同目录入口验证。

### 基础语法 `basics/`

| 文件 | 内容 | 运行方式 |
|---|---|---|
| [hello.sa](basics/hello.sa) | 最小变量与输出 | 自动，输出 `Hello World!` |
| [functions.sa](basics/functions.sa) | 返回值、参数、AS REF | 自动 |
| [function_values.sa](basics/function_values.sa) | `@SUB()` 函数引用、`NEW SUB` callable、`CALLRET`、`f=` / `m=` | 自动，输出两个 `42` |
| [conditions_and_literals.sa](basics/conditions_and_literals.sa) | IF/ELSE、BOOL、NULL、十六进制与科学计数法 | 自动 |
| [arrays_and_loops.sa](basics/arrays_and_loops.sa) | 定长数组、FOR/WHILE、负步长 | 自动 |
| [bitwise_and_strings.sa](basics/bitwise_and_strings.sa) | 位运算权限标志、SYS.STRING | 自动 |
| [math_constants.sa](basics/math_constants.sa) | SYS.MATH 别名、常量与 POW | 自动 |
| [errors.sa](basics/errors.sa) | THROW 与 TRY/CATCH | 自动 |
| [gosub.sa](basics/gosub.sa) | 标签子段与无参 RETURN | 自动 |
| [console_input.sa](basics/console_input.sa) | SYS.IO.INPUT | 手动，输入姓名后回车 |

### 数据结构 `data/`

| 文件 | 内容 |
|---|---|
| [entity.sa](data/entity.sa) | 实体字段和引用参数 |
| [entity_strings.sa](data/entity_strings.sa) | 嵌套实体的字符串深拷贝 |
| [ownership.sa](data/ownership.sa) | `=` 复制、`f=` 借用、`m=` 移动及实体深拷贝 |
| [lists.sa](data/lists.sa) | 数值/字符串列表与显式关闭 |
| [maps.sa](data/maps.sa) | 数值/字符串字典、KEYS 列表 |

这组均为自动程序。

### 指针与 C FFI

| 文件 | 内容 |
|---|---|
| [pointers/basic.sa](pointers/basic.sa) | 取址与解引用 |
| [pointers/arithmetic.sa](pointers/arithmetic.sa) | 按元素偏移地址；不解引用对象外地址 |
| [pointers/cast_and_heap.sa](pointers/cast_and_heap.sa) | malloc/free、CPTR 与类型指针转换 |
| [ffi/puts.sa](ffi/puts.sa) | USEC / DECLARE C 调用 puts |

这组均为自动程序；C FFI 的声明必须与目标平台函数签名兼容。

### 符号与科学计算 `symbolic/`

| 文件 | 内容 |
|---|---|
| [expressions.sa](symbolic/expressions.sa) | 捕获变量名和表达式树 |
| [algebra_and_enums.sa](symbolic/algebra_and_enums.sa) | 求导、化简、代入求值，附枚举示例 |
| [fluid_equations.sa](symbolic/fluid_equations.sa) | 伯努利、连续性与流量表达式 |
| [pipe_flow.sa](symbolic/pipe_flow.sa) | 雷诺数、压降和剪切应力表达式 |
| [fluid_derivatives.sa](symbolic/fluid_derivatives.sa) | 流体表达式求导与完整参数代入 |
| [pde_residuals.sa](symbolic/pde_residuals.sa) | 验证给定多项式的热/波/拉普拉斯方程残差，输出三个零 |

这组均为自动程序。SYMBOL 保留符号名，不会把普通变量当前的数值自动代入；求值用 `SUBST` / `EVAL`。PDE 示例验证给定函数，不是通用方程求解器。

### 自包含模块项目 `modules/`

每个目录都有 **`main.sa` 运行入口** 和配套库，整体复制目录即可使用。库文件没有入口，不要单独 `run`。

| 项目入口 | 配套库 | 内容 |
|---|---|---|
| [basic/main.sa](modules/basic/main.sa) | [mathlib.sa](modules/basic/mathlib.sa) | 最小 USE 与 PUBLIC SUB |
| [statistics/main.sa](modules/statistics/main.sa) | [statslib.sa](modules/statistics/statslib.sa) | 均值、加权分数与 `.slib` 打包素材 |
| [native_math/main.sa](modules/native_math/main.sa) | [samath.sa](modules/native_math/samath.sa) | C `math.h` 的数值函数封装 |
| [numerical_methods/main.sa](modules/numerical_methods/main.sa) | [mathlib_enhanced.sa](modules/numerical_methods/mathlib_enhanced.sa) | 参数化泰勒展开、牛顿迭代和 2×2 矩阵标量计算 |

```powershell
python -m sonalgebraic run examples/modules/basic/main.sa
python -m sonalgebraic slib examples/modules/statistics/statslib.sa -o build/statslib.slib
python -m sonalgebraic pack examples/modules/basic/mathlib.sa -o build/mathlib.spkg
```

数值方法库使用固定迭代次数，是算法教学用实现；完整数值 API 看 `native_math/`。包产物放到 `build/`，引用包时按[模块解析规则](../docs/07-modules.md)放置或使用 `--pkg`。

### 系统交互 `platform/`

| 文件 | 运行条件与副作用 |
|---|---|
| [file_io.sa](platform/file_io.sa) | 自动；在**进程工作目录**写入/覆盖 `sa-note.txt` |
| [desktop.sa](platform/desktop.sa) | 手动；改写剪贴板并弹出消息框，需要桌面会话 |
| [gui_hello.sa](platform/gui_hello.sa) | 手动；打开窗口与控件，关闭窗口后退出；POSIX 真窗口需要 GTK 环境 |
| [gui_callbacks.sa](platform/gui_callbacks.sa) | 手动；`ON_CLICK` 托管按钮回调与 `RUN` 派发，点击 Greet 后关闭窗口；POSIX 真窗口需要 GTK 环境 |

### 网络与异步

| 文件 | 运行条件 |
|---|---|
| [network/tls_client.sa](network/tls_client.sa) | 手动；连接 `example.com:443`，POSIX 需要 OpenSSL 开发库 |
| [network/http_server.sa](network/http_server.sa) | 手动；监听 `127.0.0.1:8080`，浏览器访问 `/quit` 结束 |
| [async/basics.sa](async/basics.sa) | 自动；纯计算 Promise 启动与并发等待，输出 `60` |
| [async/error_propagation.sa](async/error_propagation.sa) | 自动；`THROW` 跨 Promise 传播并在 `SYNC` 边界捕获原错误 |
| [async/echo_server.sa](async/echo_server.sa) | 手动；监听 `127.0.0.1:8090`，接收并回显一条连接的数据后退出 |

异步支持 C/native；native 的平台与类型边界见[异步章节](../docs/12-async.md#126-native-后端的实现与边界)。

### 综合示例 `showcase/`

- [language_tour.sa](showcase/language_tour.sa)：主要语言特性的综合运行检查。
- [symbols_errors_pointers.sa](showcase/symbols_errors_pointers.sa)：较短的符号表达式、异常、指针与标签组合。

## 维护约定

- 新示例按用途放入子目录，用内容命名；模块项目的入口统一为 `main.sa`，依赖与入口同目录。
- [catalog.json](catalog.json) 是测试与安装包使用的完整清单：`kind` 区分程序/库，`run` 区分自动/手动/不直接运行，手动项必须说明原因。运行方式不代表后端支持矩阵。
- 添加或移动 `.sa` 时同步清单、本页及引用路径；新交互/联网示例须显式标成 `manual`，避免无人值守运行挂起。
- 构建与运行产物放在 `build/` 或临时工作目录，示例目录只保留源码与说明。
- `python -m pytest -q tests/test_examples.py` 检查清单覆盖、全部源码、双后端构建及自动示例输出；窗口、外网和监听程序只构建，不自动启动。
