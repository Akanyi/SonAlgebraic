# 错误处理与符号代数

本章覆盖两个 SonAlgebraic 特有的类型：`ERROR` 与配套的 `TRY` / `CATCH` / `THROW`，以及 `SYMBOL` 代数表达式树。

## 目录

- [ERROR 类型](#error-类型)
- [TRY / CATCH](#try--catch)
- [THROW](#throw)
- [错误类型名](#错误类型名)
- [SYMBOL 代数](#symbol-代数)

## ERROR 类型

`ERROR` 是错误处理的一等公民，携带类型码、描述信息和发生位置（行号 + 子程序名）：

```basic
10 DIM trap AS ERROR AS VAR
20 SUB main AS PUBLIC AS VOID
30 PRINT "declared"
40 .ENDSUB
50 CALL main
60 END
```

`ERROR` 变量由 `TRACEBACK` 接收或 `CATCH` 绑定填充，不直接手工构造。

## TRY / CATCH

`TRY` **只能监控一条 `CALL` 语句**，格式固定：

```
TRY CALL <子程序>(<参数>) TRACEBACK ERROR AS <变量>
```

这个限制是有意的——它把「哪段代码可能出错」钉死在一个调用上，而不是一大块随意的代码里。

```basic
10 SUB risky AS VOID
20 THROW NEW ERR_DIV_ZERO, "divide by zero"
30 .ENDSUB
40 DIM trap AS ERROR AS VAR
50 SUB main AS PUBLIC AS VOID
60 TRY CALL risky TRACEBACK ERROR AS trap
70 CATCH ERR_DIV_ZERO AS e
80 PRINT F"math error: {e}"
90 CATCH ERR_ANY AS e
100 PRINT "unknown error"
110 .ENDTRY
120 .ENDSUB
130 CALL main
140 END
```

语义：

- `TRY CALL` 建立异常拦截点。被调子程序内部（或它调用的更深层）执行 `THROW` 时，控制权立即跳回这里。
- `TRACEBACK ERROR AS <变量>` 指定接收错误对象的变量。
- `CATCH` 按书写顺序**从上往下**匹配错误类型，命中即停。
- `ERR_ANY` 是兜底分支，相当于 `catch(...)`。
- 一个 `CATCH` 都没匹配上时，错误继续向外层 `TRY` 传播；一路无人接手就打印 `Uncaught ...` 并以退出码 1 结束。
- `CATCH` 绑定的别名（上面的 `e`）在分支内可用，直接 `PRINT` 会输出格式化的错误描述。

`CATCH` 块内的 `DIM` 是块作用域，出了分支不可见。

## THROW

两种形式：

| 写法 | 用途 |
|---|---|
| `THROW NEW <类型>, "<消息>"` | 抛出新错误 |
| `THROW <错误变量>` | 重新抛出已捕获的错误 |

```basic
10 SUB inner AS VOID
20 THROW NEW ERR_PARSE, "bad input"
30 .ENDSUB
40 DIM trap AS ERROR AS VAR
50 SUB middle AS VOID
60 DIM inner_trap AS ERROR AS VAR
70 TRY CALL inner TRACEBACK ERROR AS inner_trap
80 CATCH ERR_ANY AS e
90 PRINT "middle saw it, rethrowing"
100 THROW e
110 .ENDTRY
120 .ENDSUB
130 SUB main AS PUBLIC AS VOID
140 TRY CALL middle TRACEBACK ERROR AS trap
150 CATCH ERR_PARSE AS e
160 PRINT "main handled it"
170 .ENDTRY
180 .ENDSUB
190 CALL main
200 END
```

输出 `middle saw it, rethrowing` 然后 `main handled it`。

异常逃逸时，沿途每一层 `SUB` 的局部托管资源（字符串、符号树、错误对象、含托管字段的实体）都会在跳走之前释放，包括那些自己没有 `TRY`、只是被异常「穿过」的帧。实现见[第 9 章](./09-implementation-notes.md#异常setjmp-跳转与三步-throw)。

## 错误类型名

错误类型名是**任意标识符**，没有预定义白名单。`ERR_DIV_ZERO`、`ERR_PARSE`、`ERR_MY_OWN_THING` 都合法，`CATCH` 靠字符串比较匹配。

唯一有特殊含义的是 `ERR_ANY`——它编译成无条件的兜底分支，必须放在 `CATCH` 链的最后才有意义（放在前面会吃掉后续所有分支）。

约定俗成用 `ERR_` 前缀 + 全大写，但这只是习惯，编译器不强制。

## SYMBOL 代数

`SYMBOL` 不存储计算结果，而是捕获**表达式树**。赋值时不求值，把结构记下来：

```basic
10 DIM a AS NUM AS LONG AS VAR
20 DIM expr AS SYMBOL AS VAR
30 SUB main AS PUBLIC AS VOID
40 a = 7
50 expr = a + 2
60 PRINT expr
70 .ENDSUB
80 CALL main
90 END
```

输出 `(a + 2)`——注意打印的是**公式**，不是 `9`。变量按名字捕获，不按当前值。

### 代数操作

幂运算用 `**`（`^` 已经被指针解引用占用）。四个内置函数：

| 函数 | 签名 | 说明 |
|---|---|---|
| `DERIV(sym, "var")` | `SYMBOL, 字符串字面量 -> SYMBOL` | 对变量求符号导数 |
| `SIMPLIFY(sym)` | `SYMBOL -> SYMBOL` | 代数化简（常量折叠、单位元/零元、幂单位规则） |
| `SUBST(sym, "var", value)` | `SYMBOL, 字符串字面量, NUM -> SYMBOL` | 把自由变量代入数值 |
| `EVAL(sym)` | `SYMBOL -> DOUBLE` | 数值求值，应先 `SUBST` 消掉自由变量 |

```basic
10 DIM x AS NUM AS LONG AS VAR
20 DIM f AS SYMBOL AS VAR
30 DIM df AS SYMBOL AS VAR
40 DIM v AS NUM AS DOUBLE AS VAR
50 SUB main AS PUBLIC AS VOID
60 x = 0
70 f = x ** 3 + 2 * x
80 PRINT F"f = {f}"
90 df = SIMPLIFY(DERIV(f, "x"))
100 PRINT F"f' = {df}"
110 v = EVAL(SUBST(f, "x", 3))
120 PRINT F"f(3) = {v}"
130 .ENDSUB
140 CALL main
150 END
```

输出：

```text
f = ((x ** 3) + (2 * x))
f' = ((3 * (x ** 2)) + 2)
f(3) = 33
```

### 求导规则覆盖

`DERIV` 支持：

- 四则运算 `+` `-` `*` `/`（和差法则、乘积法则、商法则）
- 幂 `**`：`u ** v` 按 `u ** v * (v' * LOG(u) + v * u' / u)` 生成链式求导树
- 六个超越函数节点：`LOG`、`EXP`、`SIN`、`COS`、`TAN`、`SQRT`

最后一条要说清楚：这六个函数**目前还不能在 SA 源码里直接写**。`f = SIN(x)` 不是合法语法。它们作为符号树节点存在，由内部规则产生——比如上面的幂求导会生成 `LOG(...)` 节点，`SIMPLIFY` 和 `EVAL` 也认得这些节点。把超越函数接进表层语法是后续工作。

### 重赋值

给已持有符号树的变量重新赋值是安全的，即使右值引用了变量自身：

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

编译器保证「先构建新树 → 再释放旧树 → 最后接管」的顺序，细节见[第 9 章](./09-implementation-notes.md#symbol-重赋值的自引用安全)。

符号树的生命周期由编译器自动管理，不需要手工释放。当前限制：`ENTITY` 内的 `SYMBOL` 字段不做深层托管。
