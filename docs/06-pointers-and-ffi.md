# 指针与 C FFI

SonAlgebraic 的指针不是为了日常编程准备的——普通逻辑用变量、`ENTITY` 和标准库容器就够了。指针存在的理由是**跨越 C 边界**：调用 C 函数、接收 C 返回的缓冲区、把 SA 数据交给 C 读写。

本章覆盖 `CPTR` / `PTR TO T` / `@` / `^` / `CAST`，以及 `USEC` / `USELIB` / `DECLARE C` 这套 FFI 声明。

## 目录

- [两种指针](#两种指针)
- [取址 @ 与解引用 ^](#取址--与解引用-)
- [指针算术](#指针算术)
- [CAST 类型转换](#cast-类型转换)
- [C FFI 声明](#c-ffi-声明)
- [完整示例：手工管理的堆内存](#完整示例手工管理的堆内存)
- [当前限制](#当前限制)

## 两种指针

| 类型 | 映射到 C | 用途 |
|---|---|---|
| `CPTR` | `void*` | 不透明指针。只在 SA 侧传递，不关心指向什么 |
| `PTR TO <类型>` | `<类型>*` | 类型化指针。可以解引用读写 |

```basic
10 DIM raw AS CPTR AS VAR
20 DIM p AS PTR TO NUM AS LONG AS VAR
30 DIM q AS PTR TO NUM AS DOUBLE AS VAR
40 SUB main AS PUBLIC AS VOID
50 raw = NULL
60 p = NULL
70 q = NULL
80 PRINT "declared"
90 .ENDSUB
100 CALL main
110 END
```

`PTR TO` 后面跟完整的类型描述，所以 `PTR TO NUM AS LONG` 里的 `AS LONG` 是子类型的一部分，不是可变性修饰。声明变量时仍然要以 `AS VAR` 结尾。

`NULL` 可赋给两种指针，也可与它们做等值比较。

## 取址 @ 与解引用 ^

```basic
10 DIM x AS NUM AS LONG AS VAR
20 DIM p AS PTR TO NUM AS LONG AS VAR
30 SUB main AS PUBLIC AS VOID
40 x = 42
50 p = @x
60 PRINT ^p
70 ^p = 100
80 PRINT x
90 .ENDSUB
100 CALL main
110 END
```

输出 `42` 然后 `100`——通过指针写入改变了原变量。

规则：

- `@` 的操作数必须是**标识符路径**——变量名或 `实体.字段`。数组元素不行：`@arr[0]` 会报 `@ 只能用于变量`。
- `@x` 的类型是 `PTR TO <x 的类型>`，所以 `@h.hp`（`hp` 是 `NUM AS LONG` 字段）得到 `PTR TO NUM AS LONG`。
- `^p` 只能用于 `PTR TO T`。`^` 可以出现在赋值左侧，此时是通过指针写入。
- **`CPTR` 不能直接解引用。** `^raw` 会报 `^ 只能用于指针类型`——`void*` 不带类型信息，编译器无从知道该读几个字节。先 `CAST` 成 `PTR TO T`。

`^` 和 `@` 都是最高优先级的前缀运算符。位运算之所以用 `BAND` / `BXOR` 这类关键字而不是 `&` / `^` 符号，正是因为这两个符号已经被指针占用了。

## 指针算术

指针可以加减整数偏移，步长是**目标类型的大小**（和 C 一致）。只有 `+` 和 `-` 支持指针操作数，且另一侧必须是数值；指针相减、指针乘除都不允许。

```basic
10 DIM x AS NUM AS LONG AS VAR
20 DIM p AS PTR TO NUM AS LONG AS VAR
30 SUB main AS PUBLIC AS VOID
40 x = 42
50 p = @x
60 p = p + 1
70 p = p - 1
80 PRINT ^p
90 .ENDSUB
100 CALL main
110 END
```

输出 `42`——偏移出去又偏移回来，落回原位。

单个变量的地址加偏移会直接指向不属于你的内存，越界访问**不做任何检查**。指针算术真正有用的场合是遍历一整块连续内存，而 SA 里连续内存来自 C 分配，见[下面的完整示例](#完整示例手工管理的堆内存)。

## CAST 类型转换

```
CAST <目标类型> <表达式>
```

`CAST` 是 SA 里唯一的强制转换手段，也是绕过类型系统的**逃生舱口**。编译器只检查目标类型本身写得合法，**不验证这个转换是否有意义**——它假设你在跨 C 边界，知道自己在做什么。

```basic
10 FOR ENTITY AS Hero
20 DIM hp AS NUM AS LONG AS VAR
30 .ENDENTITY
40 SUB main AS PUBLIC AS VOID
50 DIM h AS ENTITY AS Hero AS VAR
60 DIM raw AS CPTR AS VAR
70 DIM back AS PTR TO ENTITY AS Hero AS VAR
80 DIM view AS ENTITY AS Hero AS VAR
90 h.hp = 99
100 raw = CAST CPTR (@h)
110 back = CAST PTR TO ENTITY AS Hero (raw)
120 view = ^back
130 PRINT view.hp
140 .ENDSUB
150 CALL main
160 END
```

输出 `99`——实体地址擦成 `CPTR` 再还原回去，数据完好。

括号可加可不加，`CAST CPTR @h` 和 `CAST CPTR (@h)` 等价。目标类型支持 `NUM` 及其子类型、`STRING`、`BOOL`、`CPTR`、`PTR TO T`、`HANDLE AS Kind`、`ENTITY AS Name`。

注意第 120 行必须先把 `^back` 整体解引用到一个变量再访问字段。**不能**写 `(^back).hp`——词法层不接受 `)` 后面紧跟 `.`，会报 `无法识别的表达式字符: .`。解引用得到的是整个实体的副本（走深拷贝），字段多的时候要留意这个开销。

### CAST 里不能写 CALL

这是最容易踩的坑：

<!-- doctest: skip 演示错误写法，故意编译不过 -->
```basic
10 REM 错误：CALL 不能出现在表达式中间
20 p = CAST PTR TO NUM AS LONG (CALL CSTD.calloc(1024, 8))
30 REM 正确：表达式里直接写函数名
40 p = CAST PTR TO NUM AS LONG CSTD.calloc(1024, 8)
```

`CALL` 是语句级关键字，只能独立成句或占据整条赋值的右侧，详见[第 3 章](./03-subroutines.md#call-能出现在哪里)。

## C FFI 声明

三个顶层声明配合使用：

| 声明 | 作用 | 生成 |
|---|---|---|
| `USEC "header.h" AS H` | 引入 C 头文件 | `#include "header.h"` |
| `USEC <header> AS H` | 引入系统头文件 | `#include <header>` |
| `USELIB "curl" AS CURL_LIB` | 声明链接依赖 | 链接阶段加 `-lcurl` |
| `DECLARE C SUB H.func(...) AS <返回类型>` | 注册 C 函数签名 | 直接按原名调用 |

`USELIB` 的参数如果是具体文件路径，就直接链接该文件而不是加 `-l` 前缀。模块里的 `USELIB` 会递归汇总参与最终链接。

```basic
10 USEC "stdio.h" AS STDIO
20 USELIB "m" AS M_LIB
30 DECLARE C SUB STDIO.puts(s AS STRING) AS NUM AS LONG
40 SUB main AS PUBLIC AS VOID
50 CALL STDIO.puts("hello from C FFI")
60 .ENDSUB
70 CALL main
80 END
```

`DECLARE C SUB` 注册的函数按普通子程序的方式调用：无返回值用 `CALL`，取返回值就在表达式里直接写名字。类型按[第 9 章的映射表](./09-implementation-notes.md#命名与类型映射)对应到 C。

注意 `DECLARE C` 声明的签名**必须和头文件里的真实签名一致**。编译器不校验这一点（它没读 C 头文件），写错了要到 C 编译阶段才会暴露。好在驱动会用生成 C 里的 `/* SA nnn: ... */` 注释把 C 的报错位置反查回 SA 源码行。

## 完整示例：手工管理的堆内存

```basic
10 USEC "stdlib.h" AS CSTD
20 DECLARE C SUB CSTD.calloc(n AS NUM AS LONG, size AS NUM AS LONG) AS CPTR
30 DECLARE C SUB CSTD.free(p AS CPTR) AS VOID
40 DIM block AS PTR TO NUM AS LONG AS VAR
50 DIM i AS NUM AS LONG AS VAR
60 DIM cursor AS PTR TO NUM AS LONG AS VAR
70 SUB main AS PUBLIC AS VOID
80 block = CAST PTR TO NUM AS LONG CSTD.calloc(4, 8)
90 IF block = NULL THEN
100 PRINT "out of memory"
110 RETURN
120 .ENDIF
130 FOR i = 0 TO 3
140 cursor = block + i
150 ^cursor = i * i
160 .ENDFOR
170 FOR i = 0 TO 3
180 cursor = block + i
190 PRINT ^cursor
200 .ENDFOR
210 CALL CSTD.free(CAST CPTR block)
220 .ENDSUB
230 CALL main
240 END
```

输出 `0`、`1`、`4`、`9`。

几个要点：

- `calloc` 返回 `CPTR`，`CAST` 成 `PTR TO NUM AS LONG` 才能解引用。
- 注意第 80 行**没有** `CALL`——它在赋值右侧的表达式里，而且外面套了 `CAST`。
- 反过来第 210 行的 `free` 不要返回值，用 `CALL` 语句；参数处再 `CAST` 回 `CPTR`。
- 分配失败要检查 `NULL`。
- C 分配的内存**不由编译器托管**，必须自己 `free`。

## 当前限制

- FFI 支持 C 函数调用、`CPTR` 不透明指针和 `PTR TO <类型>` 类型指针。
- **C struct 字段访问、函数回调、字符串所有权转换**都还没有，需要后续扩展。SA 没有函数指针，所以回调式 API（包括经典的 GUI 回调注册）暂时表达不了——`SYS.GUI` 走的是轮询式事件循环，就是这个原因。
- 指针不参与编译器的资源托管。`ENTITY`、`STRING` 那套自动释放对指针指向的内存无效。
