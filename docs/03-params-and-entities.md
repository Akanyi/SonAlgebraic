以下是关于 **传参语法（子程序参数）** 与 **结构体语法（`ENTITY` 实体）** 的正式语义定义与到底层 C11 的翻译策略。

---

### 1. 传参语法 (Parameter Passing) 的语义定义

SonAlgebraic 摒弃了现代语言中过于随意的参数传递机制。在声明子程序参数时，必须严格使用 `(...)` 包裹，并且每一个参数都必须像独立的 `DIM` 语句一样，使用万能关键字 `AS` 完整描述其类型签名。

*   **语法特征**：
    *   **参数声明**：`SUB <Name>(<param1> AS <Type> [AS <Subtype>], ...)`
    *   **引用传递修饰符**：通过追加 `AS REF` 明确声明该参数为引用（地址）传递，允许在子程序内部修改外部变量。默认不加则为值传递（传拷贝）。
    *   **调用传参**：`CALL <Name>(<arg1>, <arg2>)` 
*   **代码示例**：
    ```basic
    10 REM 声明一个带参数并有返回值的子程序
    20 SUB calculateArea(width AS NUM AS DOUBLE, height AS NUM AS DOUBLE) AS NUM AS DOUBLE
    30   RETURN width * height
    40 .ENDSUB
    50 
    60 REM 声明一个使用引用传递 (AS REF) 修改外部状态的子程序
    70 SUB swap(a AS NUM AS LONG AS REF, b AS NUM AS LONG AS REF) AS VOID
    80   DIM temp AS NUM AS LONG AS VAR
    90   temp = a
    100  a = b
    110  b = temp
    120 .ENDSUB
    130
    140 SUB main AS PUBLIC AS VOID
    150  DIM x AS NUM AS LONG AS VAR
    160  DIM y AS NUM AS LONG AS VAR
    170  x = 10
    180  y = 20
    190  CALL swap(x, y)
    200  PRINT F"x={x}, y={y}"  REM 输出 x=20, y=10
    210 .ENDSUB
    ```
*   **运行时语义**：
    *   **传值 (Pass-by-Value)**：默认行为。将实参的值拷贝入子程序的新栈帧中，子程序内的修改不影响外部。
    *   **传引用 (Pass-by-Reference)**：带有 `AS REF` 的参数在调用时，必须传入一个合法的左值（如变量），不能是常量或字面量。子程序内对该参数的操作直接作用于原变量。
*   **C11 翻译策略**：
    *   普通参数直接映射为 C 语言的函数参数：`double sa_sub_calculateArea(double sa_var_width, double sa_var_height)`。
    *   `AS REF` 参数映射为 C 语言的指针类型：`void sa_sub_swap(long* sa_var_a, long* sa_var_b)`。
    *   对于 `AS REF` 的调用，编译器会自动在实参前加上取址符 `&`：`sa_sub_swap(&sa_var_x, &sa_var_y);`，并在函数体内部自动解引用 `*sa_var_a = *sa_var_b;`。

---

### 2. 结构体语法 (`ENTITY` 实体) 的语义定义

SonAlgebraic 使用 `FOR ENTITY AS ...` 语法来创造复杂数据结构。这里 `FOR` 并不代表循环，而是取其英文本意 **"For (the purpose of creating an) Entity as (Name)"**。

*   **语法特征**：
    *   **定义实体**：以 `FOR ENTITY AS <EntityName>` 开始，以 `.ENDENTITY` 结束。
    *   **成员声明**：实体内部只能包含 `DIM` 语句，用于严格定义实体的“属性”（字段）。
    *   **实例化类型**：在主程序中使用 `AS ENTITY AS <EntityName>` 进行变量声明。
    *   **成员访问**：使用标准的点号 `.` 操作符。
*   **代码示例**：
    ```basic
    10 REM 宣告一个二维向量实体
    20 FOR ENTITY AS Vector2D
    30   DIM x AS NUM AS DOUBLE AS VAR
    40   DIM y AS NUM AS DOUBLE AS VAR
    50 .ENDENTITY
    60 
    70 REM 宣告一个玩家实体，嵌套使用其他实体
    80 FOR ENTITY AS Player
    90   DIM name AS STRING AS VAR
    100  DIM position AS ENTITY AS Vector2D AS VAR
    110  DIM health AS NUM AS LONG AS VAR
    120 .ENDENTITY
    130
    140 SUB main AS PUBLIC AS VOID
    150  REM 实例化 Player 实体
    160  DIM hero AS ENTITY AS Player AS VAR
    170  
    180  REM 成员属性赋值与嵌套访问
    190  hero.name = "Arthur"
    200  hero.health = 100
    210  hero.position.x = 15.5
    220  hero.position.y = 30.0
    230  
    240  PRINT F"Player {hero.name} is at ({hero.position.x}, {hero.position.y})"
    250 .ENDSUB
    ```
*   **运行时语义**：
    *   `ENTITY` 是一种复合值类型。默认情况下，它被分配在栈上（与数值类型行为一致）。
    *   当通过 `=` 将一个 `ENTITY` 赋值给另一个同类型的 `ENTITY` 变量时，发生的是**深拷贝 (Deep Copy)**，即所有成员的内存块会被完整复制。
    *   实体内的成员在实体被实例化 (`DIM`) 时，会自动被初始化为安全零值（数值为 `0`，字符串为 `""`，指针/引用为 `NULL`）。
*   **C11 翻译策略**：
    *   `FOR ENTITY AS ...` 结构将在 C 语言的头文件区被前置翻译为 `typedef struct` 声明，以保证严格的内存对齐和类型安全。
        ```c
        typedef struct {
            double x;
            double y;
        } SaEntity_Vector2D;

        typedef struct {
            SaString name;
            SaEntity_Vector2D position;
            long health;
        } SaEntity_Player;
        ```
    *   `DIM hero AS ENTITY AS Player` 会被翻译为 C 语言的局部结构体变量，并附带清零初始化代码：
        ```c
        SaEntity_Player sa_var_hero;
        memset(&sa_var_hero, 0, sizeof(SaEntity_Player));
        ```
    *   字段访问 `hero.position.x` 将完美一比一映射为 C11 中的 `sa_var_hero.position.x`。对于字符串的赋值操作，编译器底层的 String 模块会接管内存的引用计数或拷贝逻辑。