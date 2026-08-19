# 语言基础

本章覆盖 SonAlgebraic 的源码结构、声明、类型、字面量、运算符和控制流。子程序见[第 3 章](./03-subroutines.md)，复合类型见[第 4 章](./04-composite-types.md)。

## 目录

- [语言基础](#语言基础)
  - [目录](#目录)
  - [源码结构](#源码结构)
  - [声明 DIM 与 CONST](#声明-dim-与-const)
    - [万能关键字 AS](#万能关键字-as)
  - [类型系统](#类型系统)
  - [字面量](#字面量)
  - [F-string](#f-string)
  - [运算符与优先级](#运算符与优先级)
  - [控制流](#控制流)
    - [IF / ELSE IF / ELSE](#if--else-if--else)
    - [FOR](#for)
    - [WHILE](#while)
    - [GOTO 与标签](#goto-与标签)
    - [END](#end)
  - [输入输出](#输入输出)
  - [内置转换函数](#内置转换函数)
  - [关键字总表](#关键字总表)
    - [已废弃](#已废弃)

## 源码结构

一份 SA 源文件由三部分组成，顺序固定：

1. **顶层声明**：`USE` / `USEC` / `USELIB` / `DECLARE C` / 全局 `DIM` / `CONST` / `ENUM` / `FOR ENTITY`
2. **子程序定义**：`SUB ... .ENDSUB`
3. **入口调用**：`CALL main` 和 `END`

三条硬规则：

**每一非空行以唯一递增的正整数行号开头，后跟一个空格。**

```basic
10 DIM counter AS NUM AS LONG AS VAR
20 SUB main AS PUBLIC AS VOID
30 counter = 1
40 PRINT counter
50 .ENDSUB
60 CALL main
70 END
```

行号后可以有额外缩进，只有行号的空行也合法。行号必须递增但不必连续——留间隔方便后续插入，`sonc fmt --renumber` 可以整体重排。

不想手写行号时用 `SYS.LINT`：

```basic
USE SYS.LINT AS NONE_NUMBER
SUB main AS PUBLIC AS VOID
PRINT "no line numbers required"
.ENDSUB
CALL main
END
```

编译器会在解析前按出现顺序补上 `10, 20, 30...`。源码里已有的行号会被整体重写，不予保留。

**所有变量先声明后使用，声明必须写全可变性。**

**所有可执行代码包在 `SUB` 里，入口是 `SUB main AS PUBLIC AS VOID`。** 顶层只能有声明和最后的 `CALL main` / `END`。

`REM` 标记注释，可独占一行，也可跟在语句之后：

```basic
10 REM 整行注释
20 DIM x AS NUM AS LONG AS VAR
30 SUB main AS PUBLIC AS VOID
40 x = 1 REM 行尾注释
50 .ENDSUB
60 CALL main
70 END
```

## 声明 DIM 与 CONST

`DIM` 声明变量，类型和修饰符用 `AS` 串起来，**结尾必须是 `AS VAR`**：

```basic
10 DIM counter AS NUM AS LONG AS VAR
20 DIM message AS STRING AS VAR
30 DIM ratio AS NUM AS DOUBLE AS VAR
40 SUB main AS PUBLIC AS VOID
50 counter = 1
60 message = "hello"
70 ratio = 0.5
80 .ENDSUB
90 CALL main
100 END
```

漏掉 `AS VAR` 会报 `DIM 声明必须以 AS VAR 结尾标明可变性`。`NUM` 也不能单独出现，必须带 `LONG` / `DOUBLE` / `FLOAT` 子类型。

`CONST` 声明常量，必须带初始值：

```basic
10 CONST MAX_RETRY AS NUM AS LONG = 3
20 CONST GREETING AS STRING = "hi"
30 SUB main AS PUBLIC AS VOID
40 PRINT MAX_RETRY
50 PRINT GREETING
60 .ENDSUB
70 CALL main
80 END
```

`DIM` 和 `CONST` 都可以写在 `SUB` 内部作为局部声明。局部声明是**块作用域**：`IF` / `FOR` / `WHILE` / `CATCH` 块内声明的变量在块外不可见（与生成 C 的 `{ }` 一致）。兄弟分支可以声明同名变量，但不允许遮蔽外层已有的同名局部。

### 万能关键字 AS

`AS` 在所有上下文里都是「被视为 / 作为」：

| 用法 | 示例 |
|---|---|
| 主类型 | `DIM name AS STRING AS VAR` |
| 子类型 | `DIM price AS NUM AS DOUBLE AS VAR` |
| 可变性 | `... AS VAR` |
| 可见性 | `SUB myApi AS PUBLIC AS VOID` |
| 返回类型 | `SUB getName AS STRING` |
| 引用传参 | `SUB bump(n AS NUM AS LONG AS REF) AS VOID` |
| 模块别名 | `USE SYS.IO AS IO` |
| 句柄 kind | `DIM f AS HANDLE AS FILE AS VAR` |
| 实体类型 | `DIM hero AS ENTITY AS Player AS VAR` |
| 错误容器 | `TRACEBACK ERROR AS trap` |
| 错误别名 | `CATCH ERR_ANY AS e` |

## 类型系统

| 类型 | 子类型 | 说明 |
|---|---|---|
| `NUM` | `LONG` / `DOUBLE` / `FLOAT` | 数值。子类型必填 |
| `STRING` | — | UTF-8 字符串 |
| `BOOL` | — | 布尔，字面量 `TRUE` / `FALSE` |
| `SYMBOL` | — | 代数表达式树，见[第 5 章](./05-errors-and-symbols.md) |
| `ERROR` | — | 错误对象，见[第 5 章](./05-errors-and-symbols.md) |
| `ENTITY AS Name` | 实体名 | 结构体，见[第 4 章](./04-composite-types.md) |
| `HANDLE AS Kind` | kind 名 | 名义化资源句柄，见[第 4 章](./04-composite-types.md) |
| `CPTR` | — | 不透明指针，见[第 6 章](./06-pointers-and-ffi.md) |
| `PTR TO T` | 目标类型 | 类型化指针，见[第 6 章](./06-pointers-and-ffi.md) |
| `VOID` | — | 只用于 `SUB` 返回类型 |

比较运算和逻辑运算的结果类型是 `BOOL`。`BOOL` 与数值可互相赋值——比较结果赋给 `LONG`、整数当条件用，都合法。

`NULL` 可赋给任意 `PTR TO T`、`CPTR` 或 `HANDLE AS Kind`，也可与它们做等值比较：

```basic
10 DIM done AS BOOL AS VAR
20 DIM p AS PTR TO NUM AS LONG AS VAR
30 SUB main AS PUBLIC AS VOID
40 done = TRUE
50 p = NULL
60 IF p = NULL AND done THEN
70 PRINT "null and done"
80 END IF
90 .ENDSUB
100 CALL main
110 END
```

## 字面量

**数值**：

- 十进制整数：`42`、`-7`
- 小数：`3.14`
- 十六进制：`0xFF`、`0x1A2B`
- 科学计数法：`1.5e3`、`2E-5`
- 下划线分隔：`1_000_000`（生成 C 时去掉）

形态决定类型：含小数点或 `e` 的是 `DOUBLE`，十六进制和纯整数是 `LONG`。

**字符串**：双引号或单引号包裹。转义序列：

| 写法 | 含义 |
|---|---|
| `\n` `\r` `\t` | 换行、回车、制表 |
| `\\` | 反斜杠本身 |
| `\"` `\'` | 引号 |
| `\{` `\}` | 花括号（F-string 里用） |

未知转义序列会**报错**而不是静默吞掉反斜杠——`"C:\data"` 这种 Windows 路径必须写成 `"C:\\data"`，否则编译期就会被拦下。

**布尔与空值**：`TRUE` / `FALSE` / `NULL` 是语言关键字，无需 `USE`。

## F-string

`F"..."` 前缀，`{}` 内可写任意表达式，包括函数调用：

```basic
10 USE SYS.STRING AS S
20 DIM name AS STRING AS VAR
30 DIM score AS NUM AS LONG AS VAR
40 SUB main AS PUBLIC AS VOID
50 name = "LANS"
60 score = 99
70 PRINT F"user={name} score={score} len={S.LENGTH(name)}"
80 PRINT F"literal braces: {{ and }}"
90 .ENDSUB
100 CALL main
110 END
```

`{{` 和 `}}` 输出字面花括号，`\{` / `\}` 是等效的第二种写法。插值内部可以出现字符串字面量（含同款引号），扫描器会正确配平。

## 运算符与优先级

从高到低：

| 优先级 | 运算符 | 说明 |
|---|---|---|
| 最高 | `^x` `@x` `CAST T x` | 解引用、取址、类型转换 |
| | `**` | 幂，右结合 |
| | 一元 `+` `-` `BNOT` | |
| | `*` `/` `%` | 乘、除、取模 |
| | `+` `-` | 加减；指针可加减整数偏移 |
| | `SHL` `SHR` | 左移、右移 |
| | `BAND` | 按位与 |
| | `BXOR` | 按位异或 |
| | `BOR` | 按位或 |
| | `=` `==` `!=` `<>` `<` `<=` `>` `>=` | 比较。`=` 和 `==` 同义，`!=` 和 `<>` 同义 |
| | `NOT` | 逻辑非 |
| | `AND` | 逻辑与 |
| 最低 | `OR` | 逻辑或 |

位运算用关键字而不是符号，因为 `^` 已经是解引用、`@` 已经是取址：

```basic
10 DIM flags AS NUM AS LONG AS VAR
20 DIM shifted AS NUM AS LONG AS VAR
30 SUB main AS PUBLIC AS VOID
40 flags = 0x01 BOR 0x04
50 IF flags BAND 0x04 THEN
60 PRINT "bit set"
70 END IF
80 shifted = 1 SHL 8
90 PRINT shifted
100 .ENDSUB
110 CALL main
120 END
```

两个容易踩的细节：

- `NOT` 的操作数按比较级解析，所以 `NOT a = b` 读作 `NOT (a = b)`，和经典 BASIC、Python 一致。位取反 `BNOT` 则绑定在一元级。
- `-x ** 2` 读作 `-(x ** 2)`，因为一元负号和幂同级。

## 控制流

### IF / ELSE IF / ELSE

结束符写 `END IF` 或 `.ENDIF`，两者完全同义：

```basic
10 SUB grade(score AS NUM AS LONG) AS NUM AS LONG
20 IF score >= 90 THEN
30 RETURN 4
40 ELSE IF score >= 80 THEN
50 RETURN 3
60 ELSE IF score >= 60 THEN
70 RETURN 2
80 ELSE
90 RETURN 0
100 END IF
110 .ENDSUB
120 DIM g AS NUM AS LONG AS VAR
130 SUB main AS PUBLIC AS VOID
140 g = CALL grade(85)
150 PRINT g
160 .ENDSUB
170 CALL main
180 END
```

返回路径分析会识别「必定返回」：当 `IF` 同时有 then 分支、全部 `ELSE IF` 分支和 `ELSE` 分支，且每个分支都保证 `RETURN` 时，整条 `IF` 算作必定返回——上面的 `grade` 没有末尾兜底 `RETURN` 也能通过检查。没有 `ELSE` 的 `IF` 仍然无法保证返回。

### FOR

```basic
10 DIM i AS NUM AS LONG AS VAR
20 SUB main AS PUBLIC AS VOID
30 FOR i = 0 TO 10 STEP 2
40 PRINT i
50 .ENDFOR
60 FOR i = 10 TO 0 STEP -5
70 PRINT i
80 .ENDFOR
90 .ENDSUB
100 CALL main
110 END
```

`FOR var = start TO end [STEP step]` / `.ENDFOR`。循环变量必须是已声明的数值变量。边界和步长在进入循环前求值一次（BASIC 语义），中途改动不影响循环次数。步长可正可负，循环条件自动处理方向。

### WHILE

```basic
10 DIM i AS NUM AS LONG AS VAR
20 SUB main AS PUBLIC AS VOID
30 i = 3
40 WHILE i > 0
50 PRINT i
60 i = i - 1
70 .ENDWHILE
80 .ENDSUB
90 CALL main
100 END
```

条件每次迭代重新求值。

### GOTO 与标签

标签写作 `::name`，独占一行：

```basic
10 DIM counter AS NUM AS LONG AS VAR
20 SUB main AS PUBLIC AS VOID
30 counter = 1
40 ::loop_start
50 IF counter > 5 THEN
60 GOTO ::loop_end
70 END IF
80 PRINT F"counter is now: {counter}"
90 counter = counter + 1
100 GOTO ::loop_start
110 ::loop_end
120 PRINT "loop finished."
130 .ENDSUB
140 CALL main
150 END
```

`GOSUB` 也跳到标签，但会记住返回点，见[第 3 章](./03-subroutines.md#gosub-与标签)。

### END

`END` 结束程序执行，**只能放在顶层**。`SUB` 内部想提前退出用 `RETURN`。

## 输入输出

`PRINT` 输出到标准输出并换行。`CLS` 清屏。读取输入需要 `SYS.IO`：

```basic
10 USE SYS.IO AS CONSOLE
20 DIM name AS STRING AS VAR
30 SUB main AS PUBLIC AS VOID
40 CONSOLE.INPUT "Name: ", name
50 PRINT F"hello {name}"
60 .ENDSUB
70 CALL main
80 END
```

`<别名>.INPUT "提示", 变量` 是语句而不是表达式，别名可以任取。

## 内置转换函数

两个全局内置函数，不需要 `USE`：

| 函数 | 返回 | 说明 |
|---|---|---|
| `NUMBER(s)` | `DOUBLE` | 字符串转数值 |
| `STRING(x)` | `STRING` | 任意值转字符串 |

```basic
10 DIM n AS NUM AS DOUBLE AS VAR
20 DIM s AS STRING AS VAR
30 SUB main AS PUBLIC AS VOID
40 n = NUMBER("123.45")
50 s = STRING(n)
60 PRINT s
70 .ENDSUB
80 CALL main
90 END
```

字符串拼接、查找、切片等操作在 `SYS.STRING`，见[第 8 章](./08-stdlib.md#sysstring-字符串)。

## 关键字总表

**程序结构与流程控制**

| 关键字 | 用途 |
|---|---|
| `SUB` / `.ENDSUB` | 定义子程序 |
| `CALL` | 调用子程序。只能作为独立语句，或整条赋值的右侧 |
| `RETURN` | 从函数返回值；无参形式配合 `GOSUB` |
| `IF` / `THEN` / `ELSE IF` / `ELSE` / `END IF` / `.ENDIF` | 条件分支 |
| `FOR` / `TO` / `STEP` / `.ENDFOR` | 计数循环 |
| `WHILE` / `.ENDWHILE` | 条件循环 |
| `GOTO` | 无条件跳转 |
| `GOSUB` | 跳转并记住返回点 |
| `::name` | 标签 |
| `END` | 程序终点，只能在顶层 |

**声明与类型**

| 关键字 | 用途 |
|---|---|
| `DIM` | 声明变量 |
| `CONST` | 声明常量 |
| `VAR` | 标明可变，`DIM` 的必需结尾 |
| `NUM` / `LONG` / `DOUBLE` / `FLOAT` | 数值类型与子类型 |
| `STRING` / `BOOL` / `SYMBOL` / `ERROR` / `VOID` | 其余基本类型 |
| `ENTITY` / `FOR ENTITY` / `.ENDENTITY` | 结构体 |
| `ENUM` / `.ENDENUM` | 枚举 |
| `HANDLE AS Kind` | 名义化资源句柄 |
| `CPTR` / `PTR TO` / `CAST` | 指针与类型转换 |

**子程序修饰符**

| 关键字 | 用途 |
|---|---|
| `PUBLIC` | 可被外部模块访问 |
| `PRIVATE` | 仅当前文件可见（默认） |
| `REF` | 参数按引用传递 |

**异常处理**

| 关键字 | 用途 |
|---|---|
| `TRY` / `.ENDTRY` | 受监控块，必须紧跟 `CALL` |
| `TRACEBACK` | 指定接收错误对象的变量 |
| `CATCH` | 按错误类型分支 |
| `THROW` / `NEW` | 抛出错误 |

**模块与 FFI**

| 关键字 | 用途 |
|---|---|
| `USE` | 加载 SA 模块 |
| `USEC` | 引入 C 头文件 |
| `USELIB` | 声明链接库 |
| `DECLARE C` | 注册 C 函数签名 |

**I/O 与其他**

| 关键字 | 用途 |
|---|---|
| `PRINT` | 输出 |
| `CLS` | 清屏 |
| `REM` | 注释 |
| `F"..."` | 格式化字符串 |
| `TRUE` / `FALSE` / `NULL` | 字面量 |

### 已废弃

| 关键字 | 替代 |
|---|---|
| `LET` | 直接赋值 `x = 10` |
| `DIM ... AS VAL` | `CONST NAME AS NUM AS LONG = 10` |
