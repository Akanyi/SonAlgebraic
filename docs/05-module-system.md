以下是 `USE` 语法的完整语义定义与底层 C11 翻译策略。

---

### `USE` (模块加载与命名空间) 的语义定义

*   **语法特征**：
    *   **声明规范**：`USE <ModulePath> AS <Alias>`。
    *   **路径规则**：`<ModulePath>` 使用点号 `.` 作为层级分隔符。例如内置系统模块 `SYS.MATH`，或用户自定义模块 `LIBS.UTILS`。
    *   **强制别名**：可以使用万能关键字 `AS` 为导入的模块指定一个当前文件内的别名（Alias），否则隐式AS为模块自身名。
    *   **行号法则**： `USE` 语句必须带有行号。
*   **代码示例**：
    ```basic
    10 REM 导入系统数学库，并赋予别名 M
    20 USE SYS.MATH AS M
    30 REM 导入同级目录下的自定义模块 data_models.sa
    40 USE DATA_MODELS AS DATA
    50 
    60 SUB main AS PUBLIC AS VOID
    70   DIM radius AS NUM AS DOUBLE AS VAR
    80   DIM area AS NUM AS DOUBLE AS VAR
    90   radius = 5.0
    100  
    110  REM 通过别名明确调用模块内的 PUBLIC 子程序或常量
    120  area = M.PI * M.POW(radius, 2.0)
    130  
    140  REM 使用用户自定义模块中的实体
    150  DIM user AS ENTITY AS DATA.UserRecord AS VAR
    160  
    170  PRINT F"Area: {area}"
    180 .ENDSUB
    ```

*   **编译与运行时语义**：
    *   **纯编译期链接**：`USE` 本身不产生任何运行时开销。SonAlgebraic 禁止模块级别的“裸代码”，因此模块被加载时不会有任何初始化代码被隐式执行。一切逻辑的入口只由显式的 `CALL` 触发。
    *   **访问权限控制**：被导入的模块中，只有被标记为 `AS PUBLIC` 的 `SUB`、`CONST` 或 `ENTITY` 才能被外部通过别名访问。带有 `AS PRIVATE`（或默认）的标识符对外部完全不可见。
    *   **绝对命名空间**：一旦 `USE SYS.MATH AS M`，该文件内绝对不能直接写 `POW()`，必须写成 `M.POW()`。这保证了哪怕多个模块拥有同名函数，也永远不会产生冲突。

*   **C11 翻译策略**：
    *   **文件解析与头文件包含**：
        编译器在处理 `USE` 时，会去寻找对应的 `.sa` 源文件（如 `sys/math.sa` 或 `data_models.sa`）。在翻译到 C11 时，`USE` 语句会被直接翻译为对相应 C 头文件的引入：
        ```c
        #include "sa_sys_math.h"
        #include "sa_user_data_models.h"
        ```
    *   **命名空间抹平 (Name Mangling)**：
        由于 C11 语言本身没有原生的 `namespace` 概念，SonAlgebraic 编译器会在底层通过**前缀重命名 (Mangling)** 技术来模拟命名空间。
        例如，`SYS.MATH` 中的 `SUB POW`，在编译为 C 语言时，其真实函数签名会被重命名为：
        `double sa_mod_sys_math_sub_pow(double sa_var_base, double sa_var_exp);`
    *   **别名替换**：
        当编译器扫描到当前上下文的 `M.POW(a, b)` 时，它会查阅符号表：
        1. `M` 指向 `SYS.MATH`。
        2. `POW` 指向 `SYS.MATH` 下的 `PUBLIC SUB`。
        3. 直接在 C 代码中输出完全限定名：
        ```c
        sa_var_area = sa_mod_sys_math_const_PI * sa_mod_sys_math_sub_pow(sa_var_radius, 2.0);
        ```

### 延伸：被 `USE` 的模块是如何导出的？

作为补充，当用户编写一个被其他人 `USE` 的库文件（如 `data_models.sa`）时，其结构与普通程序无异，仅仅是不需要（也不应该）定义 `SUB main`：

**`data_models.sa` (提供方):**
```basic
10 REM 数据模型库
20 FOR ENTITY AS UserRecord
30   DIM id AS NUM AS LONG AS VAR
40   DIM username AS STRING AS VAR
50 .ENDENTITY
60
70 SUB _internal_hash AS PRIVATE AS VOID
80   REM 此函数不对外暴露
90 .ENDSUB
100
110 SUB initUser(u AS ENTITY AS UserRecord AS REF) AS PUBLIC AS VOID
120  u.id = 0
130  u.username = "Guest"
140 .ENDSUB
```
**编译期行为**：SonAlgebraic 在编译 `data_models.sa` 时，会同步生成一个 `sa_user_data_models.h`（包含结构体定义和外部函数声明）和一个 `.c` 文件，从而无缝衔接 C 编译器的分离编译模式。