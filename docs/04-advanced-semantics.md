以下是对 `SYMBOL`、`ERROR`、`TRY/CATCH/THROW`、`GOSUB` 以及 **非 VOID 返回值函数** 的正式语义定义与 C11 实现策略。

---

### 1. `SYMBOL` (代数符号类型) 的语义定义

`SYMBOL` 是 SonAlgebraic 中最特殊的类型之一，它不直接存储计算后的数值结果，而是存储**数学表达式的抽象语法树 (AST) 或符号表示**，支持延迟计算 (Lazy Evaluation)。

*   **语法特征**：
    *   声明：`DIM expr AS SYMBOL AS VAR`
    *   赋值：可以接受数值、变量引用或带有数学运算符的表达式。
*   **运行时语义**：
    *   当一个表达式赋值给 `SYMBOL` 时，编译器不会立即求值，而是将其捕获为符号树结构。
    *   它允许在运行时保存数学公式，未来可接入求导、化简等代数运算接口。
*   **C11 翻译策略**：
    *   在 C11 中，`SYMBOL` 被映射为一个不透明指针 `typedef struct SaSymbolNode* SaSymbol;`。
    *   该结构体包含节点类型（常量、变量引用、操作符）和左右子节点。
    *   赋值操作会被编译为 AST 节点的动态构建。例如 `expr = a + 2` 会被翻译为 `expr = sa_symbol_add(sa_symbol_var(&a), sa_symbol_const_num(2));`。
    *   **重赋值的资源顺序（自引用安全）**：对一个已持有符号树的变量重新赋值时，必须**先把新树构建到临时量、再释放旧树、最后接管**：
        ```c
        SaSymbol __new = /* 新树，可能内含 sa_symbol_clone(wave) */;
        sa_symbol_free(wave);   /* 此时旧树已不再被引用 */
        wave = __new;
        ```
        若顺序写反（先 `sa_symbol_free(wave)` 再构建新树），而右值又引用了左值自身——如 `wave = wave * t + SIMPLIFY(DERIV(wave, "t"))`——`sa_symbol_clone(wave)` 就会克隆一棵**已被释放**的树，造成 use-after-free：简单表达式可能因未定义行为侥幸跑通，复杂表达式则直接段错误或打印出 `<null-symbol>`。这条规则对 SYMBOL 局部变量、全局变量和 `AS REF` 引用参数一致适用。

---

### 2. `ERROR` (错误对象类型) 的语义定义

`ERROR` 是 SonAlgebraic 处理所有异常流的一等公民。

*   **语法特征**：
    *   声明：`DIM e AS ERROR AS VAR`
    *   实例化：由 `NEW <ErrType>, "<Message>"` 创建，或通过 `CATCH` 捕获产生。
*   **运行时语义**：
    *   `ERROR` 类型是一个复合数据结构，包含了**错误类型码 (Error Code)**、**描述信息 (Message)** 以及**发生位置 (Line Number / Traceback)**。
*   **C11 翻译策略**：
    *   在 C11 中映射为标准的结构体：
        ```c
        typedef struct {
            int err_code;
            const char* message;
            int line_number;
            const char* sub_name;
        } SaError;
        ```

---

### 3. `TRY / CATCH / THROW` (结构化异常处理) 的语义定义

SonAlgebraic 强制要求 `TRY` 只能用于监控 `CALL` 语句，这种设计避免了代码块中随意抛出异常造成的逻辑混乱。

*   **语法规则**：
    ```basic
    10 DIM trap AS ERROR AS VAR
    20 TRY CALL _riskySub TRACEBACK ERROR AS trap
    30   CATCH ERR_DIV_ZERO AS e
    40     PRINT F"Math Error: {e}"
    50   CATCH ERR_ANY AS e
    60     PRINT "Unknown Error"
    70     THROW e  REM 重新抛出
    80 .ENDTRY
    ```
*   **运行时语义**：
    *   `TRY CALL` 会建立一个异常拦截点。如果 `_riskySub` 内部（或其调用的深层子程序）执行了 `THROW`，程序控制权将立即跳回拦截点。
    *   `TRACEBACK ERROR AS <var>` 指定了接收捕获到的 `ERROR` 对象的变量。
    *   `CATCH` 会按顺序匹配错误类型。`ERR_ANY` 充当默认捕获器 (类似 `catch(...)`)。
    *   `THROW NEW <ErrType>, "<Msg>"` 会中止当前控制流并向外层寻找最近的 `TRY` 节点。
*   **C11 翻译策略**：
    *   **跳转原语**：使用编译器内建的 `__builtin_setjmp` / `__builtin_longjmp`（GCC/Clang），并封装为 `SaJmpBuf` / `SA_SETJMP` / `SA_LONGJMP` 宏；不支持内建版本的编译器回退到标准 `<setjmp.h>`。
        *   选用内建版本是为了走纯寄存器跳转模型，**绕开 Windows MinGW 的 SEH 帧展开**。标准 `setjmp/longjmp` 在含不可归约控制流（如 `GOTO` 跳进循环、`GOSUB` 回跳）的函数里被 `-O2` 优化后，SEH 展开会损坏栈帧导致崩溃——这是用优化档「压住」治标不治本的常见坑，内建版从根上消除该 UB。
    *   编译器维护一个固定容量的 `SaTryFrame sa_try_stack[64]` 栈与 `sa_try_top` 游标，支持嵌套 `TRY`。
    *   `TRY CALL` 翻译为：
        ```c
        sa_try_top++;
        if (SA_SETJMP(sa_try_stack[sa_try_top - 1].env) == 0) {
            sa_sub_risky();        /* 受监控的 CALL */
            sa_try_top--;          /* 正常返回，弹出拦截点 */
        } else {
            sa_try_top--;          /* 异常落地，先弹栈再按类型匹配 CATCH */
            /* CATCH 分派逻辑 */
        }
        ```
    *   **`THROW` 拆成三步**，确保 `longjmp` 跳走之前当前帧不泄漏：
        1.  `sa_raise_new(type, msg, line, sub)` / `sa_raise_error(err)`：把错误装入全局 `sa_current_error`，**但不跳转**。重抛（`err == &sa_current_error`）时跳过自拷贝，避免 use-after-free。
        2.  清理当前 `SUB` 已分配的局部托管资源（`free` 字符串、`sa_symbol_free` 符号、实体析构等）。
        3.  `sa_throw_dispatch()`：若 `sa_try_top > 0` 则 `SA_LONGJMP` 到最近拦截点；否则打印 `Uncaught ...`，**在 `exit(1)` 前调用 `sa_error_clear(&sa_current_error)`** 释放错误自身的 message，与正常退出路径（`sa_program_end` 处的清理）保持一致，避免泄漏检测工具误报。
    *   **异常穿透的 per-call landing pad**：当一个 `SUB` 自己没有 `TRY`、却持有存活的局部托管资源、又调用了可能抛异常的用户/外部 `SUB` 时，异常会「穿过」该帧。编译器为这类语句单独注入一个**只做清理**的 landing pad：
        ```c
        sa_try_top++;
        if (SA_SETJMP(sa_try_stack[sa_try_top - 1].env) == 0) {
            sa_sub_callee();
            sa_try_top--;
        } else {
            sa_try_top--;
            /* 释放本帧局部资源 */
            sa_throw_dispatch();   /* 继续向外层重抛 */
        }
        ```
        *   触发条件由 codegen 静态判定：语句形如 `CALL ...` / `x = CALL ...` / `PRINT ...CALL...`，且目标是用户或外部模块 `SUB`（纯 C FFI 函数不抛 SA 异常，不包裹），且当前帧此刻确有存活的托管资源。
        *   这样无论异常被显式 `THROW`、无匹配 `CATCH` 重抛、还是完全无人捕获，沿途每一层的局部资源都在 `longjmp` 越过该帧之前被释放。全链路在 `-O2` 下经 malloc/free 计数桩验证净分配为 0。

---

### 4. `GOSUB` 与其搭配的 `RETURN` 的语义定义

`GOSUB` 是一种经典的局部子程序调用，它不同于 `CALL` (调用独立的 `SUB`)，它在**当前 `SUB` 的作用域内**进行代码复用。

*   **语法规则**：
    ```basic
    10 SUB main AS PUBLIC AS VOID
    20   GOSUB ::helper
    30   PRINT "Back!"
    40   END
    50   
    60   ::helper
    70     PRINT "In helper"
    80     RETURN
    90 .ENDSUB
    ```
*   **运行时语义**：
    *   遇到 `GOSUB ::label` 时，记录当前的下一条行号（返回地址），然后跳转到 `::label` 处执行。
    *   遇到**无参数**的 `RETURN` 时，从最近的一次 `GOSUB` 记录的返回地址处继续执行。
*   **C11 翻译策略**：
    *   C 原生的 `goto` 是静态跳转，无法表达「从哪个 `GOSUB` 跳来就回到哪」的动态返回。方案是用一个**纯 C 整数返回栈**记录返回点，配合 `switch` 分发回跳，不依赖任何非标准扩展（早期曾用 GCC 的 label-as-value `&&label` + `goto *ptr`，因可移植性差且与优化档冲突已弃用）。
    *   含 `GOSUB` 的 `SUB` 在函数开头注入返回栈：
        ```c
        int sa_gosub_stack[64];
        int sa_gosub_top = 0;
        ```
    *   `GOSUB ::helper`（位于第 20 行）翻译为：压入**本 `GOSUB` 语句自己的行号**作为返回票据，再静态跳到标签：
        ```c
        if (sa_gosub_top >= 64) { fputs("SonAlgebraic runtime: GOSUB stack overflow\n", stderr); exit(1); }
        sa_gosub_stack[sa_gosub_top++] = 20;
        goto sa_label_helper;
        sa_gosub_return_20: ;   /* GOSUB 之后的落点，RETURN 凭票据跳回这里 */
        ```
    *   无参数 `RETURN` 翻译为弹出票据并 `switch` 分发回对应落点：
        ```c
        if (sa_gosub_top > 0) {
            switch (sa_gosub_stack[--sa_gosub_top]) {
                case 20: goto sa_gosub_return_20;
                /* ……该 SUB 内每个 GOSUB 行号一个 case…… */
            }
        }
        /* sa_gosub_top == 0 时，无参 RETURN 退化为函数返回 */
        ```
    *   返回栈固定 64 深度，压栈前检查溢出并在越界时报运行时错误退出。
    *   **与异常处理的交互坑（CATCH 变量提升）**：若某 `SUB` 含 `GOSUB` **或** `GOTO`，`CATCH` 绑定的 `SaError` 别名会被提升到函数作用域（`hoisted_catch_vars`），并在 `SUB` 末尾兜底 `sa_error_clear`。原因有二：
        *   `GOSUB` 的回跳 `goto` 可能跨过已失效的块作用域，落点处再去 `free` 一个自动存储期已结束的 `SaError` 会造成野指针 free——必须提升才能让该变量在回跳后依然有效。
        *   `GOTO` 可能从 `CATCH` 块**内部直接跳出** `.ENDTRY`，从而跳过块尾的 `sa_error_clear(&e)`，使最后一次捕获的 error message 泄漏。提升到函数作用域后，由 `SUB` 末尾的兜底清理收尾；`sa_error_clear` 幂等，与正常路径的块尾清理叠加不会双重 `free`。

---

### 5. 非 `VOID` 返回值函数的语义定义

这是对 [01-getting-started.md](./01-getting-started.md) 中“结构的王权”的延伸，用于定义携带明确返回类型的 `SUB`。

*   **语法规则**：
    ```basic
    10 SUB calculateTotal AS NUM AS DOUBLE
    20   DIM result AS NUM AS DOUBLE AS VAR
    30   result = 10.5 * 2.0
    40   RETURN result
    50 .ENDSUB
    ```
*   **运行时语义**：
    *   声明 `SUB` 时，`AS <Type>` 明确了函数向调用者返回的数据类型。
    *   在非 `VOID` 函数中，**必须**使用带表达式的 `RETURN <expr>`（与 `GOSUB` 的无参 `RETURN` 形成严格区分）。
    *   如果 `<expr>` 的类型与声明的返回类型不完全匹配，只要在安全转换范围内（如 `LONG` 到 `DOUBLE`），编译器会自动隐式转换；否则引发编译期类型错误。
    *   执行到 `RETURN <expr>` 时，计算表达式的值，销毁当前函数的局部变量作用域，并将该值传递给外层调用方。
*   **C11 翻译策略**：
    *   无缝映射到 C 语言的带返回值函数。
    *   上述代码翻译为：
        ```c
        double sa_sub_calculateTotal() {
            double sa_var_result;
            sa_var_result = 10.5 * 2.0;
            return sa_var_result;
        }
        ```
    *   如果调用方使用：`total = CALL _calculateTotal`，则直接翻译为 `sa_var_total = sa_sub_calculateTotal();`。