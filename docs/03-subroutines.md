# 子程序

`SUB` 是 SonAlgebraic 唯一的可执行代码容器。本章覆盖定义、参数传递、返回值，以及 `GOSUB` 这种块内跳转。

## 目录

- [子程序](#子程序)
  - [目录](#目录)
  - [定义与调用](#定义与调用)
  - [CALL 能出现在哪里](#call-能出现在哪里)
  - [参数](#参数)
  - [引用传参 AS REF](#引用传参-as-ref)
  - [返回值](#返回值)
  - [可见性](#可见性)
  - [GOSUB 与标签](#gosub-与标签)

## 定义与调用

最简形式是无参无返回值：

```basic
10 SUB greet AS VOID
20 PRINT "hello"
30 .ENDSUB
40 SUB main AS PUBLIC AS VOID
50 CALL greet
60 .ENDSUB
70 CALL main
80 END
```

程序入口固定是 `SUB main AS PUBLIC AS VOID`，由顶层的 `CALL main` 启动，最后 `END` 收尾。

带参数时参数列表用 `(...)` 包裹，每个参数像独立的 `DIM` 一样用 `AS` 写全类型签名：

```basic
10 SUB area(width AS NUM AS DOUBLE, height AS NUM AS DOUBLE) AS NUM AS DOUBLE
20 RETURN width * height
30 .ENDSUB
40 DIM result AS NUM AS DOUBLE AS VAR
50 SUB main AS PUBLIC AS VOID
60 result = CALL area(3.0, 4.0)
70 PRINT result
80 .ENDSUB
90 CALL main
100 END
```

注意参数**不写** `AS VAR`——可变性是 `DIM` 的概念，参数用 `AS REF` 表达传递方式。

## CALL 能出现在哪里

`CALL` 是**语句级**关键字，只有两个合法位置：

```basic
10 SUB make AS NUM AS LONG
20 RETURN 42
30 .ENDSUB
40 SUB log(n AS NUM AS LONG) AS VOID
50 PRINT n
60 .ENDSUB
70 DIM v AS NUM AS LONG AS VAR
80 SUB main AS PUBLIC AS VOID
90 REM 一：独立语句
100 CALL log(1)
110 REM 二：整条赋值的右侧
120 v = CALL make
130 PRINT v
140 .ENDSUB
150 CALL main
160 END
```

**不能**把 `CALL` 嵌进表达式中间。在表达式里调用有返回值的函数，直接写名字就行，不加 `CALL`：

<!-- doctest: skip 演示错误写法，故意编译不过 -->
```basic
10 REM 错误：CALL 出现在表达式中间
20 v = CAST PTR TO NUM AS LONG (CALL CSTD.calloc(1024, 8))
30 REM 正确：表达式里直接写函数名
40 v = CAST PTR TO NUM AS LONG CSTD.calloc(1024, 8)
```

理由很直接：`CALL` 表示对子程序 `SUB` 的显式调用。它只能出现在独立语句或赋值右侧；当出现在赋值右侧时，赋值语义会取得该 `SUB` 的返回值。`CALL` 本身不是普通值表达式，因此不能嵌入 `CAST`、函数参数、`F-string` 或其他表达式，天然只能站在语句级语法位置；而表达式要的是一个**值**。这两者撞一起必然冲突。写错时编译器会明确提示 `CALL 不能出现在表达式中间`。

同理，F-string 插值、函数实参、`IF` 条件里都不能写 `CALL`。

## 参数

默认按**值**传递：实参的值拷贝进子程序的新栈帧，内部修改不影响外部。

```basic
10 SUB bump_by_value(n AS NUM AS LONG) AS VOID
20 n = n + 1
30 PRINT F"inside={n}"
40 .ENDSUB
50 DIM counter AS NUM AS LONG AS VAR
60 SUB main AS PUBLIC AS VOID
70 counter = 10
80 CALL bump_by_value(counter)
90 PRINT F"outside={counter}"
100 .ENDSUB
110 CALL main
120 END
```

输出 `inside=11` 和 `outside=10`——外部的 `counter` 没被动过。

`ENTITY` 按值传参时会复制字符串字段，函数内改动同样不影响外部对象。

## 引用传参 AS REF

参数末尾加 `AS REF` 改成引用传递，子程序内的操作直接作用于原变量：

```basic
10 SUB swap(a AS NUM AS LONG AS REF, b AS NUM AS LONG AS REF) AS VOID
20 DIM temp AS NUM AS LONG AS VAR
30 temp = a
40 a = b
50 b = temp
60 .ENDSUB
70 DIM x AS NUM AS LONG AS VAR
80 DIM y AS NUM AS LONG AS VAR
90 SUB main AS PUBLIC AS VOID
100 x = 10
110 y = 20
120 CALL swap(x, y)
130 PRINT F"x={x}, y={y}"
140 .ENDSUB
150 CALL main
160 END
```

输出 `x=20, y=10`。

`AS REF` 参数在调用时**必须传左值**（变量、实体字段、数组元素），不能传常量或字面量。调用处不需要写取址符，编译器自动处理。

## 返回值

`SUB` 的返回类型写在参数列表之后。非 `VOID` 的 `SUB` 必须用带表达式的 `RETURN <expr>`：

```basic
10 SUB total AS NUM AS DOUBLE
20 DIM result AS NUM AS DOUBLE AS VAR
30 result = 10.5 * 2.0
40 RETURN result
50 .ENDSUB
60 DIM sum AS NUM AS DOUBLE AS VAR
70 SUB main AS PUBLIC AS VOID
80 sum = CALL total
90 PRINT sum
100 .ENDSUB
110 CALL main
120 END
```

规则：

- 返回值类型与声明不完全匹配时，只要在安全转换范围内（如 `LONG` 到 `DOUBLE`）编译器自动隐式转换，否则编译期报错。
- 编译器做返回路径分析，非 `VOID` 的 `SUB` 所有路径都必须返回。`IF` 的全分支覆盖会被识别为「必定返回」，详见[第 2 章](./02-language-basics.md#if--else-if--else)。
- `VOID` 的 `SUB` 里裸 `RETURN` 表示提前退出。
- 返回值先算出来存进临时量，再清理本帧局部资源，最后返回——所以返回堆分配的 `STRING` 是安全的。

字符串可以直接返回，生命周期由编译器接管：

```basic
10 USE SYS.STRING AS S
20 SUB wrap(body AS STRING) AS STRING
30 RETURN F"[{body}:{S.LENGTH(body)}]"
40 .ENDSUB
50 DIM tag AS STRING AS VAR
60 SUB main AS PUBLIC AS VOID
70 tag = CALL wrap("abc")
80 PRINT tag
90 .ENDSUB
100 CALL main
110 END
```

输出 `[abc:3]`。

## 可见性

| 修饰符 | 含义 |
|---|---|
| `AS PUBLIC` | 可被其他模块通过别名访问 |
| `AS PRIVATE` | 仅当前文件可见 |
| 不写 | 等同 `PRIVATE` |

```basic
10 SUB helper AS PRIVATE AS VOID
20 PRINT "internal"
30 .ENDSUB
40 SUB api AS PUBLIC AS VOID
50 CALL helper
60 .ENDSUB
70 SUB main AS PUBLIC AS VOID
80 CALL api
90 .ENDSUB
100 CALL main
110 END
```

模块导出规则见[第 7 章](./07-modules.md)。

## GOSUB 与标签

`GOSUB` 是经典 BASIC 的局部子程序调用。它和 `CALL` 的区别在于：`CALL` 调用独立的 `SUB`，`GOSUB` 在**当前 `SUB` 的作用域内**跳转复用代码，共享同一批局部变量。

```basic
10 SUB main AS PUBLIC AS VOID
20 GOSUB ::helper
30 PRINT "Back!"
40 RETURN
50 ::helper
60 PRINT "In helper"
70 RETURN
80 .ENDSUB
90 CALL main
100 END
```

输出 `In helper` 然后 `Back!`。

语义：

- `GOSUB ::label` 记录返回地址后跳到标签处执行。
- 遇到**无参** `RETURN` 时回到最近一次 `GOSUB` 的下一条语句。
- 返回栈固定 64 层深，越界会报运行时错误退出。
- 没有待返回的 `GOSUB` 时，无参 `RETURN` 退化成普通的函数返回——上面第 40 行就是靠这条在 `helper` 之前收住主流程，注意这里**不能**写 `END`（`END` 只能在顶层）。

实现细节（整数返回栈 + `switch` 分发，以及和异常处理的交互）见[第 9 章](./09-implementation-notes.md#gosub整数返回栈与-switch-分发)。
