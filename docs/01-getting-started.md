# 入门

SonAlgebraic 是一门带行号的 BASIC 方言，编译到 C11 再交给本机 C 编译器（或走 native 后端出 LLVM IR）。它的设计取向是**显式**：类型要写全，可变性要标明，可执行代码必须装在子程序里。

本章带你跑通第一个程序并理解它的每一行。工具链安装见根 [README](../README.md#安装-sadk)。

## 目录

- [入门](#入门)
  - [目录](#目录)
  - [第一个程序](#第一个程序)
  - [三条硬规则](#三条硬规则)
    - [一、每一非空行以唯一递增的正整数行号开头](#一每一非空行以唯一递增的正整数行号开头)
    - [二、变量先声明后使用，声明必须写全](#二变量先声明后使用声明必须写全)
    - [三、可执行代码必须包在 SUB 里](#三可执行代码必须包在-sub-里)
  - [逐行剖析](#逐行剖析)
  - [编译与运行](#编译与运行)
  - [下一步](#下一步)

## 第一个程序

存成 `hello.sa`：

```basic
10 REM 变量声明区
20 DIM counter AS NUM AS LONG AS VAR
30 DIM message AS STRING AS VAR
40 REM 主程序
50 SUB main AS PUBLIC AS VOID
60 counter = 1
70 message = "Hello from SonAlgebraic!"
80 PRINT message
90 WHILE counter <= 5
100 PRINT F"counter is now: {counter}"
110 counter = counter + 1
120 .ENDWHILE
130 PRINT "loop finished."
140 .ENDSUB
150 REM 入口调用
160 CALL main
170 END
```

跑起来：

```powershell
python -m sonalgebraic run hello.sa
```

输出：

```text
Hello from SonAlgebraic!
counter is now: 1
counter is now: 2
counter is now: 3
counter is now: 4
counter is now: 5
loop finished.
```

## 三条硬规则

这三条不是风格建议，违反会直接编译失败。

### 一、每一非空行以唯一递增的正整数行号开头

行号后跟一个空格，然后是语句。行号定义程序的物理结构，也是诊断信息的定位依据——编译器报错时给的就是这个号。

行号必须递增但不必连续，留间隔方便后续插入。`sonc fmt --renumber` 可以整体重排。

不想手写行号的话，在文件开头写 `USE SYS.LINT AS NONE_NUMBER`，编译器会自动补。

### 二、变量先声明后使用，声明必须写全

`DIM` 语句要交代三件事：主类型、子类型（如果该类型有）、可变性。

```basic
10 REM counter 的主类型是 NUM（数值），子类型是 LONG（长整型），它是 VAR（变量）
20 DIM counter AS NUM AS LONG AS VAR
30 SUB main AS PUBLIC AS VOID
40 counter = 1
50 PRINT counter
60 .ENDSUB
70 CALL main
80 END
```

两个常见的编译错误：

- `DIM counter AS NUM` → 报 `NUM 类型必须指定 LONG/DOUBLE/FLOAT`，且缺 `AS VAR`
- `DIM message AS STRING` → 报 `DIM 声明必须以 AS VAR 结尾标明可变性`

常量用 `CONST`，必须带初始值：`CONST MAX AS NUM AS LONG = 100`。

### 三、可执行代码必须包在 SUB 里

顶层只允许声明（`USE` / `DIM` / `CONST` / `ENUM` / `FOR ENTITY` / `DECLARE C`）和最后的入口调用。散落在顶层的 `PRINT "Hello"` 会被拒绝。

程序入口固定是 `SUB main AS PUBLIC AS VOID`，由顶层的 `CALL main` 启动。

`END` 标志程序执行终点，**只能写在顶层**。`SUB` 内部想提前退出用 `RETURN`。

## 逐行剖析

回到第一个程序：

| 行 | 作用 |
|---|---|
| `10` `40` `150` | `REM` 注释。可独占一行，也可跟在语句后面 |
| `20` `30` | 全局声明。类型链 `AS NUM AS LONG AS VAR` 写全了主类型、子类型、可变性 |
| `50` | 入口子程序。`AS PUBLIC` 是可见性，`AS VOID` 是返回类型 |
| `60` `70` | 赋值。不需要 `LET`，直接写 |
| `80` | `PRINT` 输出并换行 |
| `90`–`120` | `WHILE` 循环，`.ENDWHILE` 收尾。条件每轮重新求值 |
| `100` | F-string。`F"..."` 前缀，`{}` 里可以放任意表达式 |
| `140` | `.ENDSUB` 结束子程序。注意带点号——块结束符统一是 `.ENDXXX` 形式 |
| `160` | 启动入口 |
| `170` | 程序终点 |

块结束符有两套写法的只有 `IF`：`END IF` 和 `.ENDIF` 完全同义。其余（`.ENDSUB` / `.ENDFOR` / `.ENDWHILE` / `.ENDTRY` / `.ENDENTITY` / `.ENDENUM`）只有点号形式。

## 编译与运行

四个最常用的命令：

```powershell
# 只做语法和语义检查，不生成任何产物
python -m sonalgebraic check hello.sa

# 编译并立即运行
python -m sonalgebraic run hello.sa

# 编译成可执行文件
python -m sonalgebraic build hello.sa -o hello.exe

# 看看生成的 C 长什么样
python -m sonalgebraic c hello.sa
```

诊断信息会一次报出多个错误，并在源码行下面画出问题位置。完整 CLI 参考见根 [README](../README.md#cli-命令)。

## 下一步

按需要挑：

| 想做的事 | 去哪 |
|---|---|
| 查语法、类型、运算符、控制流 | [第 2 章 语言基础](./02-language-basics.md) |
| 写函数、传参、返回值 | [第 3 章 子程序](./03-subroutines.md) |
| 定义结构体、枚举、数组 | [第 4 章 复合类型](./04-composite-types.md) |
| 处理错误、玩符号求导 | [第 5 章 错误处理与符号代数](./05-errors-and-symbols.md) |
| 调 C 函数、用指针 | [第 6 章 指针与 C FFI](./06-pointers-and-ffi.md) |
| 拆分模块、打包发布 | [第 7 章 模块系统](./07-modules.md) |
| 查标准库 API | [第 8 章 标准库](./08-stdlib.md) |
| 看生成的 C 是什么样 | [第 9 章 实现说明](./09-implementation-notes.md) |

仓库的 `examples/` 目录有三十多个可直接运行的示例。想一次性确认工具链正常，跑这个：

```powershell
python -m sonalgebraic run examples/allexample.sa
```
