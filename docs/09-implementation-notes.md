# 实现说明：SA 到 C11 的翻译

这一章记录编译器实际生成什么样的 C 代码。读它的场景有三种：拿 `sonc c` 出来的文件排查问题、给编译器提交改动、或者写需要和 SA 产物链接的 C 代码。

> **符号名是实现细节，不是 ABI 承诺。** 只有一个例外：用户模块导出的 `sa_mod_<模块>_*` 符号和 `sa_user_<模块>.h` 头文件是稳定接口，反向 FFI（C 调用 SA 编译出的动态库）依赖它们。除此之外的命名随时可能变，不要在外部代码里硬编码。

## 目录

- [命名与类型映射](#命名与类型映射)
- [行号溯源注释](#行号溯源注释)
- [模块的两条不同路径](#模块的两条不同路径)
- [托管资源的清理](#托管资源的清理)
- [异常：setjmp 跳转与三步 THROW](#异常setjmp-跳转与三步-throw)
- [GOSUB：整数返回栈与 switch 分发](#gosub整数返回栈与-switch-分发)
- [SYMBOL 重赋值的自引用安全](#symbol-重赋值的自引用安全)

## 命名与类型映射

标识符规则很简单：**加 `sa_` 前缀，全部小写**。没有 `_sub_` / `_var_` 之类的中缀。

```basic
10 SUB calculateArea(width AS NUM AS DOUBLE, height AS NUM AS DOUBLE) AS NUM AS DOUBLE
20 RETURN width * height
30 .ENDSUB
40 DIM area AS NUM AS DOUBLE AS VAR
50 SUB main AS PUBLIC AS VOID
60 area = CALL calculateArea(3.0, 4.0)
70 .ENDSUB
80 CALL main
90 END
```

生成：

```c
static double sa_calculatearea(double sa_width, double sa_height) {
    double sa_tmp_1 = (sa_width * sa_height);
    return sa_tmp_1;
}

static void sa_main(void) {
    sa_area = sa_calculatearea(3.0, 4.0);
}
```

注意 `calculateArea` 变成了 `sa_calculatearea` —— SA 标识符大小写不敏感，codegen 统一小写化。表达式中间结果落在 `sa_tmp_<n>` 上，编号在每个 `SUB` 内递增。

类型映射：

| SA 类型 | C 类型 | 备注 |
|---|---|---|
| `NUM AS LONG` | `long long` | 不是 `long`——Windows 上 `long` 只有 32 位 |
| `NUM AS DOUBLE` | `double` | |
| `NUM AS FLOAT` | `float` | native 后端不支持，见 README 的当前限制 |
| `STRING` | `char*` | UTF-8，堆分配，由编译器登记释放 |
| `BOOL` | `int` | |
| `CPTR` | `void*` | |
| `PTR TO T` | `T*` | 递归展开，`PTR TO NUM AS LONG` → `long long*` |
| `HANDLE AS Kind` | `SaHandle`（即 `uint64_t`） | kind 只在 SA 侧检查，C 侧统一是 64 位 token |
| `SYMBOL` | `SaSymbol`（即 `SaSymbolNode*`） | |
| `ERROR` | `SaError` | 结构体，非指针 |
| `ENTITY AS Name` | `SaEntity_<小写名>` | |
| `DIM xs[N] AS T` | `T name[N]` | 聚合初始化 `= {0}` |

`ENTITY` 的 typedef 和实例化：

```c
typedef struct {
    double x;
    double y;
} SaEntity_vector2d;

/* DIM v AS ENTITY AS Vector2D AS VAR */
SaEntity_vector2d sa_v = {0};
```

零初始化走聚合初始化器，不是 `memset`。

`ERROR` 变量声明带完整初始化器：

```c
typedef struct {
    int err_code;
    const char* type;
    char* message;
    int line_number;
    const char* sub_name;
} SaError;

/* DIM trap AS ERROR AS VAR */
SaError sa_trap = {0, "ERR_NONE", NULL, 0, NULL};
```

## 行号溯源注释

每条语句前都会发射 `/* SA <行号>: <原始源码> */`。这不只是给人看的——C 编译阶段如果报错（通常意味着 codegen bug 或 FFI 声明与实际头文件不符），驱动会拿这些注释把 C 的错误位置反查回 SA 源码行，诊断里直接指出可能对应的那一行。

## 模块的两条不同路径

内置 `SYS.*` 模块和用户模块的处理方式**完全不同**，这是读生成 C 时最容易困惑的地方。

**内置 `SYS.*`：编译期直接 lowering，不产生任何模块符号或头文件。**

```basic
10 USE SYS.MATH AS M
20 DIM radius AS NUM AS DOUBLE AS VAR
30 DIM area AS NUM AS DOUBLE AS VAR
40 SUB main AS PUBLIC AS VOID
50 radius = 5.0
60 area = M.PI * M.POW(radius, 2.0)
70 .ENDSUB
80 CALL main
90 END
```

生成的是：

```c
sa_area = (3.14159265358979323846 * pow(sa_radius, 2.0));
```

`M.PI` 被替换成字面量，`M.POW` 直接映射到 C 标准库的 `pow`。**没有** `sa_mod_sys_math_*` 这类符号，也**不会**生成 `sa_sys_math.h`。别名 `M` 只在编译期的符号表里存在。其他内置模块同理：`SYS.STRING` 的函数落到 `sa_str_*` runtime 函数，`SYS.NET` 落到 `sa_net_*`，等等。

**用户模块：分离编译，生成头文件 + 前缀符号。**

`USE MATHLIB AS LIB` 会让编译器编出 `sa_user_mathlib.h` 和 `sa_user_mathlib.c`，模块内的导出符号带 `sa_mod_mathlib_` 前缀。主程序 `#include "sa_user_mathlib.h"` 后调用。这套命名是稳定接口，反向 FFI 就是靠它。

按需注入还有一层：runtime 不是整块塞进去的，而是按程序实际用到的特性切片注入。`PRINT "hi"` 不会带上 SYMBOL 求导的代码。

## 托管资源的清理

局部 `STRING`、`SYMBOL`、`ERROR`，以及含托管字段的 `ENTITY`，在作用域结束时释放。

非 `VOID` 的 `RETURN` 顺序是：**先算出返回值存进临时量 → 再清理本帧局部 → 最后返回**。否则返回的可能是刚被 free 掉的指针。

### RETURN 交出去的是一份独立所有权

托管类型（`STRING` / `SYMBOL` / `ERROR` / 含托管字段的 `ENTITY`）的返回值，调用方拿到手就归调用方所有。返回值按来源分三种处理：

- **本帧拥有的局部**（`DIM` 的局部、值传 `STRING` / `ENTITY` 形参，含它们的字段路径 `b.text`）：整体搬出去、原位清零——和 `m=` 一样，`sa_tmp = sa_s; sa_s = NULL;`，随后的帧清理 `free(NULL)` 无害。以前这里是 `tmp = sa_s; free(sa_s); return tmp;`，调用方拿到的是悬空指针。
- **搬不动的**：全局、`AS REF` 形参、`f=` 借来的变量、数组元素、值传 `SYMBOL` 形参——它们的资源不归本帧，深拷贝一份（`sa_strdup` / `sa_symbol_clone` / `sa_set_error` / 逐字段拷贝）。类型必须与返回类型严格一致才会搬：`SYMBOL` SUB 里 `RETURN s`（`s` 是 `STRING`）合法，但那是拿 `s` 当变量名建树。
- **本语句刚算出的临时量**（F-string、`STR.CONCAT`、`DERIV`、另一个 SUB 的返回值）：直接接管，把它的释放行从语句级清理表里摘掉，省一次拷贝加释放。

调用方那边，SUB 的托管返回值先当**语句级临时量**登记（和内置函数的堆返回值一样）；赋值（`x = CALL f()`、`DIM x ... = f()`）、`RETURN`、`SYMBOL` 建树会把它接管走，`PRINT f()`、实参、条件、`TRY CALL f()` 丢弃的都在语句尾释放。native 后端的 `TRY CALL` 例外：丢弃值紧跟着 call 释放，因为 `try_end` 块还有 `CATCH` 分支汇入，try 块里定义的 SSA 值拖到语句尾就不支配了。

### 借用与移动如何接进清理登记

codegen 为每个块维护一张「本块要在块尾释放的变量」登记表（`local_resource_stack`），`DIM` 一个托管变量就登记一条；块尾、`RETURN`、异常穿透的 landing pad 都是照这张表发 `free`。[`f=` / `m=`](./02-language-basics.md#赋值复制借用移动) 就是在这张表上做文章，两侧都不深拷贝：

- **`a m= b`**：先按类型释放 `a` 的旧值，然后 `sa_a = sa_b;`，最后把源置空——`STRING` / `SYMBOL` 写 `NULL`，`ERROR` 写空的 `SaError`，`ENTITY` 整段 `memset` 归零。**源保持登记不动**：块尾对它 `free(NULL)`、`sa_symbol_free(NULL)`、`sa_error_clear` 空结构、逐字段 `free(NULL)` 都是安全的空操作。这就是移动对控制流不敏感的原因：`IF` 里移走也不用改任何一张登记表，不管哪条路径走到块尾都对。
- **`a f= b`**：同样先释放 `a` 的旧值（它自己的初始空串），`sa_a = sa_b;`，然后**把 `a` 从当前块的登记表里摘掉**。登记表是按语句顺序处理的：借用之前生成的 `RETURN` / landing pad 仍然会 `free(sa_a)`（那时它还持有自己的串），之后的都不会——与运行时可能走到的路径一一对应。

借用目标必须与借用语句**同块 `DIM`**，这条限制直接来自「摘登记」的实现方式：登记表是静态、按块的，如果 `a` 在外层块声明、借用在 `IF` 里，摘掉的是外层块的登记，那么运行时没走这个分支的路径就把 `a` 自己的初始串漏掉了。移动没有这个问题，因为它不动登记表。

协程帧里的变量走同一套：登记里存的名字本来就是 `f->sa_a`，挂起中被 drop 时的清理体也是从同一张表抓出来的。

### ENTITY 的字符串字段

`ENTITY` 里的 `STRING` 字段按值语义管理：

- 声明局部/全局 `ENTITY` 时，字符串字段初始化为空串
- `second = first` 这类整体赋值**深拷贝**字符串字段
- 按值传参时复制字符串字段，函数内修改不影响外部
- 生命周期结束时递归释放
- 嵌套 `ENTITY` 里的字符串字段同样递归处理

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

输出 `LANS` 和 `SA`——深拷贝生效，改 `second` 不会动到 `first`。

`ENTITY` 内的 `SYMBOL` 字段目前**不做**深层 clone/free 托管。runtime 的 `sa_symbol_clone` 能力是有的，但还没接进实体的拷贝/析构路径，所以暂时按浅拷贝处理以避免双重释放。

## 异常：setjmp 跳转与三步 THROW

### 跳转原语

按目标运行时分两套，不是简单的「GCC/Clang 用内建、其余用标准」：

```c
#if defined(__MINGW32__) && (defined(__GNUC__) || defined(__clang__))
typedef void* SaJmpBuf[5];
#define SA_SETJMP(buf) __builtin_setjmp(buf)
#define SA_LONGJMP(buf) __builtin_longjmp((buf), 1)
#else
#include <setjmp.h>
typedef jmp_buf SaJmpBuf;
#define SA_SETJMP(buf) setjmp(buf)
#define SA_LONGJMP(buf) longjmp((buf), 1)
#endif
```

- **MinGW 用 `__builtin_setjmp`**：MinGW 的标准 `setjmp` 走 SEH 帧展开（`_setjmpex` / `RtlUnwindEx`）。含不可归约控制流的函数（`GOTO` 跳进循环、`GOSUB` 回跳）在 `-O2` 下会让展开表损坏，直接 access violation。内建版走简单寄存器保存模型，不碰 SEH，能稳过 `-O2`。`__builtin_longjmp` 的第二参数硬性要求是 1。
- **其余（MSVC ABI，含 `clang --target=...-msvc`）用标准 `setjmp`**：`__builtin_setjmp` 在 Windows x64 MSVC 下缓冲区和 SEH 假设不匹配，同样会 access violation；而 MSVC 的 `setjmp` 本就是 SEH-aware 的 `_setjmpex`，在自家工具链下正确且优化安全。

拦截点栈是固定容量的 `SaTryFrame sa_try_stack[64]` 配 `sa_try_top` 游标，支持嵌套 `TRY`。

### TRY CALL 的形态

```c
sa_try_top++;
if (SA_SETJMP(sa_try_stack[sa_try_top - 1].env) == 0) {
    sa_middle();       /* 受监控的 CALL */
    sa_try_top--;      /* 正常返回，弹出拦截点 */
} else {
    sa_try_top--;      /* 异常落地，先弹栈 */
    sa_set_error(&sa_trap, &sa_current_error);
    if (strcmp(sa_current_error.type, "ERR_DIV_ZERO") == 0) {
        SaError sa_e = {0, "ERR_NONE", NULL, 0, NULL};
        sa_set_error(&sa_e, &sa_current_error);
        /* CATCH 体 */
        sa_error_clear(&sa_e);
    }
    else {
        sa_throw_dispatch();   /* 无匹配 CATCH，向外层重抛 */
    }
}
```

`CATCH` 按书写顺序生成 `if / else if` 链，`ERR_ANY` 作为兜底分支。

### THROW 拆成三步

保证 `longjmp` 跳走之前当前帧不泄漏：

1. `sa_raise_new(type, msg, line, sub)` 或 `sa_raise_error(err)`——把错误装进全局 `sa_current_error`，**但不跳转**。重抛时（`err == &sa_current_error`）跳过自拷贝，避免 use-after-free。
2. 清理当前 `SUB` 已分配的局部托管资源。
3. `sa_throw_dispatch()`——`sa_try_top > 0` 就 `SA_LONGJMP` 到最近拦截点；否则打印 `Uncaught ...`，并在 `exit(1)` 前调 `sa_error_clear(&sa_current_error)` 释放错误自身的 message，与正常退出路径保持一致，免得泄漏检测工具误报。

### 异常穿透的 per-call landing pad

一个 `SUB` 自己没有 `TRY`、却持有存活的局部托管资源、又调用了可能抛异常的 `SUB` 时，异常会「穿过」这一帧。编译器给这类语句单独注入一个**只做清理**的落地点：

```c
static void sa_middle(void) {
    char* sa_held = NULL;
    sa_held = sa_strdup("");
    /* ...给 sa_held 赋值... */
    sa_try_top++;
    if (SA_SETJMP(sa_try_stack[sa_try_top - 1].env) == 0) {
        sa_risky();
        sa_try_top--;
    } else {
        sa_try_top--;
        free(sa_held);          /* 释放本帧资源 */
        sa_throw_dispatch();    /* 继续向外层重抛 */
    }
    free(sa_held);              /* 正常路径的清理 */
}
```

触发条件由 codegen 静态判定：语句形如 `CALL ...` / `x = CALL ...` / `PRINT ...CALL...`，目标是用户或外部模块 `SUB`（纯 C FFI 函数不抛 SA 异常，不包裹），且当前帧此刻确有存活的托管资源。

这样无论异常是被显式 `THROW`、无匹配 `CATCH` 重抛、还是完全无人捕获，沿途每层的局部资源都在 `longjmp` 越过该帧之前释放。全链路在 `-O2` 下经 malloc/free 计数桩验证净分配为 0。

### CATCH 变量提升

若某 `SUB` 含 `GOSUB` **或** `GOTO`，`CATCH` 绑定的 `SaError` 别名会被提升到函数作用域，并在 `SUB` 末尾兜底 `sa_error_clear`。两个原因：

- `GOSUB` 的回跳 `goto` 可能跨过已失效的块作用域，落点处再去清理一个自动存储期已结束的 `SaError` 就是野指针 free
- `GOTO` 可能从 `CATCH` 块内部直接跳出 `.ENDTRY`，跳过块尾的 `sa_error_clear(&e)`，让最后一次捕获的 message 泄漏

提升后由 `SUB` 末尾兜底收尾。`sa_error_clear` 是幂等的，和正常路径的块尾清理叠加不会双重 free。

## GOSUB：整数返回栈与 switch 分发

C 的 `goto` 是静态跳转，表达不了「从哪个 `GOSUB` 跳来就回到哪」。方案是纯 C 的整数返回栈配 `switch` 分发，不依赖任何非标准扩展（早期用过 GCC 的 label-as-value `&&label` + `goto *ptr`，因可移植性差且与优化档冲突已弃用）。

含 `GOSUB` 的 `SUB` 在函数开头注入返回栈：

```c
int sa_gosub_stack[64];
int sa_gosub_top = 0;
```

`GOSUB ::helper`（写在第 20 行）压入**本语句自己的行号**作为返回票据，再静态跳到标签：

```c
if (sa_gosub_top >= 64) { fputs("SonAlgebraic runtime: GOSUB stack overflow\n", stderr); exit(1); }
sa_gosub_stack[sa_gosub_top++] = 20;
goto sa_label_helper;
sa_gosub_return_20:;    /* RETURN 凭票据跳回这里 */
```

无参 `RETURN` 弹票据并分发：

```c
if (sa_gosub_top > 0) {
    switch (sa_gosub_stack[--sa_gosub_top]) {
        case 20: goto sa_gosub_return_20;
        /* ……该 SUB 内每个 GOSUB 行号一个 case…… */
        default: fputs("SonAlgebraic runtime: invalid GOSUB return address\n", stderr); exit(1);
    }
}
return;    /* 栈空时，无参 RETURN 退化为函数返回 */
```

## SYMBOL 重赋值的自引用安全

给一个已持有符号树的变量重新赋值时，顺序必须是**先把新树构建到临时量 → 再释放旧树 → 最后接管**：

```basic
10 DIM t AS NUM AS LONG AS VAR
20 DIM wave AS SYMBOL AS VAR
30 SUB main AS PUBLIC AS VOID
40 t = 0
50 wave = t + 1
60 wave = wave * t + SIMPLIFY(DERIV(wave, "t"))
70 PRINT wave
80 .ENDSUB
90 CALL main
100 END
```

第 60 行生成：

```c
SaSymbol sa_tmp_2 = sa_symbol_deriv(sa_wave, "t");
SaSymbol sa_tmp_3 = sa_symbol_simplify(sa_tmp_2);
SaSymbol sa_tmp_4 = sa_symbol_op('+',
    sa_symbol_op('*', sa_symbol_clone(sa_wave), sa_symbol_var("t")),
    sa_symbol_clone(sa_tmp_3));
sa_symbol_free(sa_wave);     /* 此时旧树已不再被引用 */
sa_wave = sa_tmp_4;
sa_symbol_free(sa_tmp_2);
sa_symbol_free(sa_tmp_3);
```

顺序写反（先 `sa_symbol_free(sa_wave)` 再构建新树）的话，右值里的 `sa_symbol_clone(sa_wave)` 会去克隆一棵**已被释放**的树：简单表达式可能靠未定义行为侥幸跑通，复杂表达式直接段错误或打印出 `<null-symbol>`。这条规则对 `SYMBOL` 局部变量、全局变量和 `AS REF` 引用参数一致适用。
