# 复合类型

本章覆盖 `ENTITY` 结构体、`ENUM` 枚举、定长数组和 `HANDLE` 资源句柄。可变长容器（`SYS.LIST` / `SYS.MAP`）属于标准库，见[第 8 章](./08-stdlib.md)。

## 目录

- [ENTITY 结构体](#entity-结构体)
- [ENUM 枚举](#enum-枚举)
- [定长数组](#定长数组)
- [HANDLE 资源句柄](#handle-资源句柄)

## ENTITY 结构体

`FOR ENTITY AS <名字>` 开始，`.ENDENTITY` 结束。这里的 `FOR` 不是循环，取英文本意「for (the purpose of creating an) entity as (name)」。

实体内部**只能**包含 `DIM` 字段声明：

```basic
10 FOR ENTITY AS Vector2D
20 DIM x AS NUM AS DOUBLE AS VAR
30 DIM y AS NUM AS DOUBLE AS VAR
40 .ENDENTITY
50 FOR ENTITY AS Player
60 DIM name AS STRING AS VAR
70 DIM position AS ENTITY AS Vector2D AS VAR
80 DIM health AS NUM AS LONG AS VAR
90 .ENDENTITY
100 SUB main AS PUBLIC AS VOID
110 DIM hero AS ENTITY AS Player AS VAR
120 hero.name = "Arthur"
130 hero.health = 100
140 hero.position.x = 15.5
150 hero.position.y = 30.0
160 PRINT F"{hero.name} at ({hero.position.x}, {hero.position.y})"
170 .ENDSUB
180 CALL main
190 END
```

- 实例化：`DIM <变量> AS ENTITY AS <实体名> AS VAR`
- 字段访问：点号 `.`，可任意层嵌套
- 实体可以嵌套实体，如上面的 `Player.position`

### 值语义

`ENTITY` 是**复合值类型**，不是引用：

- 字段在实例化时自动初始化为安全零值：数值 `0`，字符串 `""`，指针和句柄 `NULL`。
- `b = a` 这类整体赋值是**深拷贝**，包括字符串字段和嵌套实体里的字符串字段。
- 按值传参同样深拷贝，函数内修改不影响外部对象。
- 生命周期结束时递归释放托管字段。

```basic
10 FOR ENTITY AS NameBox
20 DIM text AS STRING AS VAR
30 .ENDENTITY
40 SUB main AS PUBLIC AS VOID
50 DIM first AS ENTITY AS NameBox AS VAR
60 DIM second AS ENTITY AS NameBox AS VAR
70 first.text = "LANS"
80 second = first
90 second.text = "SA"
100 PRINT first.text
110 PRINT second.text
120 .ENDSUB
130 CALL main
140 END
```

输出 `LANS` 和 `SA`——改 `second` 不会动到 `first`。

要按引用修改，用 `AS REF` 参数：

```basic
10 FOR ENTITY AS Counter
20 DIM hits AS NUM AS LONG AS VAR
30 .ENDENTITY
40 SUB bump(c AS ENTITY AS Counter AS REF) AS VOID
50 c.hits = c.hits + 1
60 .ENDSUB
70 SUB main AS PUBLIC AS VOID
80 DIM c AS ENTITY AS Counter AS VAR
90 CALL bump(c)
100 CALL bump(c)
110 PRINT c.hits
120 .ENDSUB
130 CALL main
140 END
```

输出 `2`。

托管字段的实现细节见[第 9 章](./09-implementation-notes.md#entity-的字符串字段)。当前限制：`ENTITY` 内的 `SYMBOL` 字段不做深层 clone/free 托管。

## ENUM 枚举

```basic
10 ENUM Color
20 RED
30 GREEN
40 BLUE
50 .ENDENUM
60 DIM c AS NUM AS LONG AS VAR
70 SUB main AS PUBLIC AS VOID
80 c = Color.GREEN
90 IF c = Color.GREEN THEN
100 PRINT "green"
110 END IF
120 .ENDSUB
130 CALL main
140 END
```

- 成员按出现顺序从 0 自增：`RED`=0、`GREEN`=1、`BLUE`=2。
- 成员是 `LONG` 常量，通过 `枚举名.成员` 访问。
- 编译期直接替换成整数字面量，没有独立的枚举类型——接收枚举值的变量声明成 `NUM AS LONG`。

## 定长数组

方括号声明，长度必须是正整数字面量：

```basic
10 DIM scores[5] AS NUM AS DOUBLE AS VAR
20 DIM names[3] AS STRING AS VAR
30 DIM i AS NUM AS LONG AS VAR
40 SUB main AS PUBLIC AS VOID
50 FOR i = 0 TO 4
60 scores[i] = i * 1.5
70 .ENDFOR
80 names[0] = "LANS"
90 PRINT F"first={scores[0]} name={names[0]}"
100 .ENDSUB
110 CALL main
120 END
```

- 元素类型支持值类型（`NUM` / `BOOL` / `HANDLE` / `CPTR` / `PTR`）和 `STRING`。
- 下标必须是整数表达式，从 0 开始。
- 值类型数组零初始化。
- `STRING` 数组每个元素自动初始化为空串，赋值走深拷贝，作用域结束逐元素释放（局部和全局都管）。
- `SYMBOL` / `ERROR` / `ENTITY` 元素数组**暂不支持**。

越界行为：常量下标越界在编译期报错；变量下标越界是运行期未定义行为，**不做边界检查**。

需要边跑边增长的容器用 `SYS.LIST`，见[第 8 章](./08-stdlib.md#syslist-动态列表)。

## HANDLE 资源句柄

`HANDLE AS Kind` 是名义化的 64 位资源 token。不同 kind 即使底层布局相同也不能互相赋值或比较——这把「把窗口句柄传给文件函数」这类错误留在了编译期：

```basic
10 DIM file AS HANDLE AS FILE AS VAR
20 DIM sock AS HANDLE AS NET_STREAM AS VAR
30 SUB main AS PUBLIC AS VOID
40 file = NULL
50 sock = NULL
60 IF file = NULL THEN
70 PRINT "no file"
80 END IF
90 .ENDSUB
100 CALL main
110 END
```

规则：

- 同 kind 句柄可赋值、传参、返回，可与 `NULL` 做等值比较。
- 禁止句柄算术、位运算、解引用，禁止隐式转换到 `NUM` / `CPTR` / `PTR`。
- **句柄不会自动关闭。** 因为复制后的多个变量可能指向同一资源，编译器无从判断谁负责释放。资源模块各自提供显式关闭 API，必须手动调用。
- runtime 用槽位 + generation + kind 约束生成 token。资源关闭后，旧副本不会重新指向后来复用同一槽位的新资源。

标准库提供的 kind：

| kind | 来源模块 | 关闭函数 |
|---|---|---|
| `FILE` | `SYS.FILE` | `CLOSE` |
| `BUFFER` | `SYS.BINARY` | `CLOSE` |
| `LIST` / `STR_LIST` | `SYS.LIST` | `CLOSE` / `CLOSE_STR` |
| `MAP` / `STR_MAP` | `SYS.MAP` | `CLOSE` / `CLOSE_STR` |
| `NET_STREAM` | `SYS.NET` | `STREAM_CLOSE` |
| `TCP_LISTENER` | `SYS.NET` | `TCP_LISTENER_CLOSE` |
| `UDP_SOCKET` | `SYS.NET` | `UDP_CLOSE` |
| `WINDOW` | `SYS.GUI` | `CLOSE` |
| `WIDGET` | `SYS.GUI` | 随窗口销毁，无需显式关闭 |

进程退出时 runtime 会兜底清理，但那只是安全网，不是可以依赖的策略。
