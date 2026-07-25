# 语言特性扩展

这一章汇总在基础语法之上新增的现代语言特性，按主题组织。所有特性都遵循 SonAlgebraic 的强制行号、声明先行、`SUB` 包裹执行逻辑的核心规则。

## 目录

- [条件分支 ELSE / ELSE IF](#条件分支-else--else-if)
- [布尔与 NULL](#布尔与-null)
- [数值字面量](#数值字面量)
- [数组](#数组)
- [循环 FOR / WHILE](#循环-for--while)
- [位运算](#位运算)
- [字符串操作 SYS.STRING](#字符串操作-sysstring)
- [内置常量](#内置常量)
- [枚举 ENUM](#枚举-enum)
- [SYMBOL 代数](#symbol-代数)
- [原生句柄 HANDLE](#原生句柄-handle)
- [二进制 SYS.BINARY](#二进制-sysbinary)
- [动态列表 SYS.LIST](#动态列表-syslist)
- [关联容器 SYS.MAP](#关联容器-sysmap)
- [网络 SYS.NET](#网络-sysnet)
- [文件 SYS.FILE](#文件-sysfile)
- [桌面 SYS.DESKTOP](#桌面-sysdesktop)
- [窗口 GUI SYS.GUI](#窗口-gui-sysgui)
- [语法糖 SYS.LINT](#语法糖-syslint)

## 条件分支 ELSE / ELSE IF

`IF` 现在支持 `ELSE IF` 链和最终 `ELSE`。结束符可写 `END IF` 或 `.ENDIF`，两者完全同义：

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
```

返回路径分析已改进：当 `IF` 同时有 then、所有 `ELSE IF` 和 `ELSE` 分支、且每个分支都保证 `RETURN` 时，整条 `IF` 被视为"必定返回"。上面的 `grade` 没有末尾兜底 `RETURN` 也能通过检查。没有 `ELSE` 分支的 `IF` 仍无法保证返回。

底层生成时 `ELSE IF` 链展开成嵌套 `if-else`，以便每个分支条件的临时变量能正确求值。

## 布尔与 NULL

- `BOOL` 类型，字面量 `TRUE` / `FALSE`，底层映射到 C `int`。
- 比较运算（`< <= > >= = == != <>`）和逻辑运算（`AND` / `OR` / `NOT`）的结果类型是 `BOOL`。
- `BOOL` 与数值可互相赋值（比较结果赋给 `LONG`、整数当条件都合法）。
- `NULL` 字面量，可赋给任意指针（`PTR TO T`）、`CPTR` 或 `HANDLE AS Kind`，也可与它们做等值比较。

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
```

## 数值字面量

- 十六进制：`0xFF`、`0x1A2B`。
- 科学计数法：`1.5e3`、`2E-5`。
- 下划线分隔符：`1_000_000`（生成 C 时自动去掉）。

形态决定类型：含小数点或 `e` 的是 `DOUBLE`，十六进制和纯整数是 `LONG`。

## 数组

方括号定长数组，元素类型支持值类型（`NUM` / `BOOL` / `HANDLE` / `CPTR` / `PTR`）和 `STRING`：

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
```

- 长度必须是正整数字面量。
- 下标必须是整数表达式。
- 值类型数组映射到 C 定长数组 `type name[N]`，零初始化。
- `STRING` 数组：每个元素自动 `sa_strdup("")` 初始化、赋值走 `sa_set_string` 深拷贝、作用域结束逐元素释放（局部和全局都管理）。
- `SYMBOL` / `ERROR` / `ENTITY` 元素数组暂不支持，后续补充。

## 循环 FOR / WHILE

```basic
10 DIM i AS NUM AS LONG AS VAR
20 SUB main AS PUBLIC AS VOID
30 REM FOR：循环变量必须是已声明的数值变量
40 FOR i = 0 TO 10 STEP 2
50 PRINT i
60 .ENDFOR
70 REM 负步长倒序
80 FOR i = 10 TO 0 STEP -5
90 PRINT i
100 .ENDFOR
110 REM WHILE
120 i = 3
130 WHILE i > 0
140 i = i - 1
150 .ENDWHILE
160 .ENDSUB
```

- `FOR var = start TO end [STEP step]` / `.ENDFOR`。边界和步长在进入循环前求值一次（BASIC 语义）。步长可正可负，循环条件自动处理方向。
- `WHILE cond` / `.ENDWHILE`。条件每次迭代重新求值。

## 位运算

由于 `^` 已是解引用、`&` 已是取址，位运算用关键字：

| 关键字 | C 运算符 | 说明 |
|---|---|---|
| `BAND` | `&` | 按位与 |
| `BOR` | `\|` | 按位或 |
| `BXOR` | `^` | 按位异或 |
| `BNOT` | `~` | 按位取反（一元前缀） |
| `SHL` | `<<` | 左移 |
| `SHR` | `>>` | 右移 |

优先级（高到低）：`SHL`/`SHR` > `BAND` > `BXOR` > `BOR`，均高于比较运算、低于算术运算。

```basic
10 flags = 0x01 BOR 0x04
20 IF flags BAND 0x04 THEN
30 PRINT "bit set"
40 END IF
50 shifted = 1 SHL 8
```

## 字符串操作 SYS.STRING

```basic
10 USE SYS.STRING AS STR
```

| 函数 | 签名 | 说明 |
|---|---|---|
| `STR.LENGTH(s)` | `STRING -> NUM LONG` | 字符串长度 |
| `STR.CONCAT(a, b)` | `STRING, STRING -> STRING` | 拼接 |
| `STR.SLICE(s, start, count)` | `STRING, NUM, NUM -> STRING` | 子串（越界安全裁剪） |
| `STR.FIND(s, sub)` | `STRING, STRING -> NUM LONG` | 查找首次出现位置，找不到返回 -1 |
| `STR.UPPER(s)` | `STRING -> STRING` | 转大写 |
| `STR.LOWER(s)` | `STRING -> STRING` | 转小写 |
| `STR.REPLACE(s, old, new)` | `STRING, STRING, STRING -> STRING` | 替换所有出现的子串 |

返回新字符串的函数（CONCAT/SLICE/UPPER/LOWER/REPLACE）在堆上分配，由编译器自动登记释放。

## 内置常量

通过内置模块别名访问：

`SYS.MATH` 数学/数值常量：

| 常量 | 值 |
|---|---|
| `PI` | 3.14159265358979323846 |
| `E` | 2.71828182845904523536 |
| `TAU` | 6.28318530717958647692 |
| `EPSILON` | 2.2204460492503131e-16 |
| `MAX_LONG` | 9223372036854775807 |
| `MIN_LONG` | -9223372036854775808 |

`SYS.STRING` 字符常量：`NEWLINE`(`\n`)、`TAB`(`\t`)、`CR`(`\r`)、`EMPTY`(`""`)。

```basic
10 USE SYS.MATH AS M
20 USE SYS.STRING AS S
30 area = M.PI * radius * radius
40 PRINT F"line1{S.NEWLINE}line2"
```

布尔/空值字面量 `TRUE` / `FALSE` / `NULL` 是语言关键字，无需 `USE`。

## 枚举 ENUM

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
```

- 成员按出现顺序从 0 自增（`RED`=0、`GREEN`=1、`BLUE`=2）。
- 成员是 `LONG` 常量，通过 `EnumName.MEMBER` 访问。
- 编译期直接替换为整数字面量。

## SYMBOL 代数

`SYMBOL` 捕获表达式树，并支持完整的代数操作。幂运算使用 `**`，`^` 保留给指针解引用：

| 函数 | 签名 | 说明 |
|---|---|---|
| `DERIV(sym, "var")` | `SYMBOL, 字符串字面量 -> SYMBOL` | 对变量求符号导数（和差/乘积/商法则、幂规则） |
| `SIMPLIFY(sym)` | `SYMBOL -> SYMBOL` | 代数化简（常量折叠、单位元/零元规则、幂单位规则） |
| `SUBST(sym, "var", value)` | `SYMBOL, 字符串字面量, NUM -> SYMBOL` | 把自由变量代入数值 |
| `EVAL(sym)` | `SYMBOL -> DOUBLE` | 数值求值（应先 SUBST 消除自由变量） |

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
```

输出：

```text
f = ((x ** 3) + (2 * x))
f' = ((3 * (x ** 2)) + 2)
f(3) = 33
```

底层 runtime 提供 `sa_symbol_clone` / `sa_symbol_eval` / `sa_symbol_subst` / `sa_symbol_deriv` / `sa_symbol_simplify`，符号树由编译器自动管理生命周期。`DERIV` 当前支持 `+ - * / **`，其中 `u ** v` 会按 `u ** v * (v' * LOG(u) + v * u' / u)` 生成链式求导树。超越函数直接写入 `SYMBOL` 表达式的语法仍留待后续扩展。

## 原生句柄 HANDLE

`HANDLE AS Kind` 是名义化的 64 位资源 token。不同 kind 即使底层布局相同也不能赋值或比较：

```basic
10 DIM file AS HANDLE AS FILE AS VAR
20 DIM socket AS HANDLE AS SOCKET AS VAR
30 file = NULL
```

- 同 kind 句柄可赋值、传参、返回，并可与 `NULL` 做等值比较。
- 禁止句柄算术、位运算、解引用，以及隐式转换到 `NUM` / `CPTR` / `PTR`。
- 句柄不自动关闭，因为复制后的多个变量可能指向同一资源。资源模块负责提供显式关闭 API。
- 文件 runtime 使用槽位、generation 和 kind 约束生成 token。关闭后，旧副本不会重新指向后来复用同一槽位的新文件。

## 二进制 SYS.BINARY

网络数据包不能用 `STRING` 承载，因为内嵌 NUL 会截断 C 字符串。`SYS.BINARY` 提供真实字节 BUFFER：

```basic
10 USE SYS.BINARY AS B
20 DIM packet AS HANDLE AS BUFFER AS VAR
30 packet = B.NEW(8)
40 ok = B.PACK_U16_BE(packet, 0, 4660)
50 ok = B.PACK_U32_LE(packet, 2, 2309737967)
60 PRINT B.HEX_ENCODE(packet)
70 ok = B.CLOSE(packet)
```

| 函数 | 返回 | 说明 |
|---|---|---|
| `NEW(length)` | `HANDLE AS BUFFER` | 创建零填充 BUFFER |
| `LENGTH(buffer)` | `LONG` | 字节长度，失败为 -1 |
| `SLICE(buffer, offset, count)` | `HANDLE AS BUFFER` | 复制切片 |
| `COPY(target, targetOffset, source, sourceOffset, count)` | `BOOL` | 支持重叠复制 |
| `HEX_DECODE(text)` / `HEX_ENCODE(buffer)` | BUFFER / STRING | HEX 编解码 |
| `PACK_U16/U32/U64_LE/BE` | `BOOL` | 按大小端写无符号字段 |
| `UNPACK_U16/U32/U64_LE/BE` | `LONG` | 按大小端读取字段 |
| `CHECKSUM8(buffer, offset, count)` | `LONG` | 字节和模 256 |
| `CLOSE(buffer)` | `BOOL` | 显式释放 BUFFER |
| `LAST_ERROR()` | `STRING` | 最近一次二进制操作错误 |

BUFFER 是显式资源。返回 BUFFER 的函数必须直接赋给 `HANDLE AS BUFFER` 变量（或从同类型 SUB 直接 RETURN），不能写成 `B.LENGTH(B.SLICE(...))` 这种匿名嵌套，否则没有句柄可供 CLOSE。

## 动态列表 SYS.LIST

定长数组 `DIM xs[N]` 覆盖不了"边跑边收集"的场景。`SYS.LIST` 提供可变长列表，数值和字符串分成两个 HANDLE kind，把"字符串列表传给数值函数"这类错误留在编译期：

```basic
10 USE SYS.LIST AS L
20 DIM xs AS HANDLE AS LIST AS VAR
30 DIM names AS HANDLE AS STR_LIST AS VAR
40 DIM ok AS BOOL AS VAR
50 SUB main AS PUBLIC AS VOID
60 xs = L.NEW()
70 ok = L.PUSH(xs, 10)
80 PRINT L.GET(xs, 0)
90 ok = L.CLOSE(xs)
100 names = L.NEW_STR()
110 ok = L.PUSH_STR(names, "alpha")
120 ok = L.PUSH_STR(names, "beta")
130 PRINT L.JOIN_STR(names, ",")
140 ok = L.CLOSE_STR(names)
150 .ENDSUB
160 CALL main
170 END
```

数值列表（`HANDLE AS LIST`，元素按 `DOUBLE` 存储）：

| 函数 | 返回 | 说明 |
|---|---|---|
| `NEW()` | `HANDLE AS LIST` | 创建空列表 |
| `PUSH(list, value)` | `BOOL` | 尾部追加 |
| `POP(list)` | `DOUBLE` | 弹出尾元素，空表返回 0 并记录错误 |
| `GET(list, index)` / `SET(list, index, value)` | `DOUBLE` / `BOOL` | 按下标读写 |
| `INSERT(list, index, value)` | `BOOL` | 在 index 处插入（0..LENGTH） |
| `REMOVE(list, index)` | `BOOL` | 删除指定下标元素 |
| `LENGTH(list)` | `LONG` | 元素个数，失败为 -1 |
| `CLEAR(list)` | `BOOL` | 清空但保留列表 |
| `CLOSE(list)` | `BOOL` | 显式释放 |

字符串列表（`HANDLE AS STR_LIST`）：同名函数带 `_STR` 后缀（`NEW_STR` / `PUSH_STR` / `POP_STR` / `GET_STR` / `SET_STR` / `INSERT_STR` / `REMOVE_STR` / `LENGTH_STR` / `CLEAR_STR` / `CLOSE_STR`），另有 `JOIN_STR(list, separator)` 返回拼接结果。`LAST_ERROR()` 返回最近一次列表操作错误。

注意事项：

- 数值元素统一按 `DOUBLE` 存储，约 2^53 以内的整数无损；需要精确 64 位整数序列时用 `SYS.BINARY` 的 BUFFER。
- 与 BUFFER 相同，返回列表句柄的函数必须直接赋给对应 `HANDLE AS LIST` / `STR_LIST` 变量并显式 CLOSE；进程退出时 runtime 会兜底清理。

## 关联容器 SYS.MAP

`SYS.MAP` 提供 STRING key 的哈希映射（链地址，负载超 1 自动翻倍 rehash）。值分数值和字符串两个 kind：`HANDLE AS MAP`（值按 `DOUBLE` 存储）和 `HANDLE AS STR_MAP`：

```basic
10 USE SYS.MAP AS M
20 USE SYS.LIST AS L
30 DIM scores AS HANDLE AS MAP AS VAR
40 DIM keys AS HANDLE AS STR_LIST AS VAR
50 DIM ok AS BOOL AS VAR
60 SUB main AS PUBLIC AS VOID
70 scores = M.NEW()
80 ok = M.SET(scores, "alice", 99)
90 PRINT M.GET(scores, "alice")
100 keys = M.KEYS(scores)
110 PRINT L.JOIN_STR(keys, ",")
120 ok = L.CLOSE_STR(keys)
130 ok = M.CLOSE(scores)
140 .ENDSUB
150 CALL main
160 END
```

数值映射（`HANDLE AS MAP`）：

| 函数 | 返回 | 说明 |
|---|---|---|
| `NEW()` | `HANDLE AS MAP` | 创建空映射 |
| `SET(map, key, value)` | `BOOL` | 插入或覆盖 |
| `GET(map, key)` | `DOUBLE` | 取值，缺 key 返回 0 并记录错误 |
| `HAS(map, key)` | `BOOL` | key 是否存在 |
| `REMOVE(map, key)` | `BOOL` | 删除 key |
| `LENGTH(map)` | `LONG` | 键值对个数，失败为 -1 |
| `KEYS(map)` | `HANDLE AS STR_LIST` | 所有 key 的快照列表（无序），用 `SYS.LIST` 遍历，用完记得 CLOSE_STR |
| `CLEAR(map)` | `BOOL` | 清空但保留映射 |
| `CLOSE(map)` | `BOOL` | 显式释放 |

字符串映射（`HANDLE AS STR_MAP`）：同名函数带 `_STR` 后缀（`NEW_STR` / `SET_STR` / `GET_STR` / `HAS_STR` / `REMOVE_STR` / `LENGTH_STR` / `KEYS_STR` / `CLEAR_STR` / `CLOSE_STR`）。`LAST_ERROR()` 返回最近一次映射操作错误。

`USE SYS.MAP` 会自动连带启用 list runtime，因为 `KEYS` 产出 STR_LIST 句柄。

## 网络 SYS.NET

```basic
10 USE SYS.NET AS N
20 body = N.REQUEST_TIMEOUT("GET", "https://example.com", "", "Accept: text/plain\r\n", 5000)
30 PRINT N.LAST_HEADERS()
40 PRINT N.LAST_ERROR()
```

| 函数 | 返回 | 说明 |
|---|---|---|
| `GET(url)` | `STRING` | 阻塞 GET，默认 30 秒超时 |
| `STATUS(url)` | `LONG` | GET 状态码，传输失败为 0 |
| `POST(url, body, contentType)` | `STRING` | 阻塞 POST |
| `REQUEST(method, url, body, headers)` | `STRING` | 通用请求 |
| `REQUEST_STATUS(...)` | `LONG` | 通用请求状态码 |
| `REQUEST_TIMEOUT(..., timeoutMs)` | `STRING` | 带毫秒超时的通用请求 |
| `REQUEST_STATUS_TIMEOUT(..., timeoutMs)` | `LONG` | 带超时的状态码请求 |
| `LAST_HEADERS()` | `STRING` | 最近一次成功响应的原始 header 文本 |
| `LAST_ERROR()` | `STRING` | 最近一次传输错误；成功时为空串 |
| `LAST_CODE()` | `LONG` | 最近一次平台错误码 |
| `URLENCODE(value)` | `STRING` | UTF-8 字节百分号编码 |
| `DNS(host)` | `STRING` | 逗号分隔的数字地址 |

TCP / TLS：

| 函数 | 返回 | 说明 |
|---|---|---|
| `TCP_CONNECT(host, port, timeoutMs)` | `HANDLE AS NET_STREAM` | TCP client |
| `TLS_CONNECT(host, port, timeoutMs)` | `HANDLE AS NET_STREAM` | 校验证书和主机名的 TLS client |
| `TCP_LISTEN(host, port, backlog)` | `HANDLE AS TCP_LISTENER` | TCP listener，port 可为 0 |
| `LOCAL_PORT(listener)` | `LONG` | listener 实际本地端口 |
| `TCP_ACCEPT(listener, timeoutMs)` | `HANDLE AS NET_STREAM` | 接受连接 |
| `TCP_LISTENER_CLOSE(listener)` | `BOOL` | 关闭 listener |
| `STREAM_SEND(stream, text)` | `LONG` | 发送 STRING 字节 |
| `STREAM_RECV(stream, maxBytes)` | `STRING` | 接收文本；不适合内嵌 NUL |
| `STREAM_SEND_BUFFER(stream, buffer, offset, count)` | `LONG` | 二进制发送 |
| `STREAM_RECV_BUFFER(stream, maxBytes)` | `HANDLE AS BUFFER` | 二进制接收 |
| `STREAM_CLOSE(stream)` | `BOOL` | 关闭 TCP/TLS stream；TLS 会发送 close_notify |

UDP：

| 函数 | 返回 | 说明 |
|---|---|---|
| `UDP_OPEN()` | `HANDLE AS UDP_SOCKET` | 创建未绑定 UDP 句柄 |
| `UDP_BIND(socket, host, port)` | `BOOL` | 绑定本地地址；port 可为 0 |
| `UDP_CONNECT(socket, host, port)` | `BOOL` | 设置默认远端 |
| `UDP_LOCAL_PORT(socket)` | `LONG` | 实际本地端口 |
| `UDP_SEND` / `UDP_SEND_TO` | `LONG` | 发送文本 datagram |
| `UDP_SEND_BUFFER` / `UDP_SEND_BUFFER_TO` | `LONG` | 发送二进制 datagram |
| `UDP_RECV` / `UDP_RECV_BUFFER` | STRING / BUFFER | 接收一个 datagram |
| `LAST_PEER_HOST()` / `LAST_PEER_PORT()` | STRING / LONG | 最近 accept/recvfrom 的对端 |
| `UDP_CLOSE(socket)` | `BOOL` | 关闭 UDP socket |

Windows HTTP/HTTPS 使用 WinHTTP，原始 TLS stream 使用系统 Schannel；POSIX HTTP 使用 socket，HTTPS/TLS 使用 OpenSSL。POSIX 构建 TLS 需要 OpenSSL 开发文件和 `libssl` / `libcrypto`。所有网络句柄都必须显式关闭；进程退出清理仅是兜底。

## 文件 SYS.FILE

```basic
10 USE SYS.FILE AS F
20 DIM file AS HANDLE AS FILE AS VAR
30 file = F.OPEN("data.txt", "WRITE")
40 PRINT F.WRITE(file, "hello")
50 PRINT F.CLOSE(file)
60 file = NULL
70 PRINT F.READ_TEXT("data.txt")
```

句柄 API：

| 函数 | 返回 | 说明 |
|---|---|---|
| `OPEN(path, mode)` | `HANDLE AS FILE` | mode: READ/WRITE/APPEND/UPDATE/CREATE，失败为 NULL |
| `READ(file, count)` | `STRING` | 从当前位置最多读取 count 字节；不接受内嵌 NUL |
| `WRITE(file, text)` | `LONG` | 写入字节数，失败为 -1 |
| `SEEK(file, offset, origin)` | `BOOL` | origin: START/CURRENT/END |
| `TELL(file)` | `LONG` | 当前偏移，失败为 -1 |
| `SIZE(file)` | `LONG` | 文件大小并恢复原偏移 |
| `CLOSE(file)` | `BOOL` | 显式关闭句柄 |

便捷 API：`READ_TEXT`、`WRITE_TEXT`、`APPEND_TEXT`、`EXISTS`、`IS_FILE`、`IS_DIR`、`DELETE`、`MKDIR`、`CWD`、`ABSOLUTE`、`LAST_ERROR`。

Windows 路径在 runtime 内从 SA UTF-8 转成 UTF-16；POSIX 直接使用 UTF-8 字节路径。`DELETE` 只删除普通文件或空目录，不做递归删除。

## 桌面 SYS.DESKTOP

```basic
10 USE SYS.DESKTOP AS D
20 ok = D.CLIPBOARD_SET("hello")
30 text = D.CLIPBOARD_GET()
40 ok = D.MESSAGE("SonAlgebraic", text)
50 ok = D.OPEN("https://example.com")
```

| 函数 | 返回 | 说明 |
|---|---|---|
| `MESSAGE(title, text)` | `BOOL` | Windows 原生 Unicode 信息框 |
| `OPEN(target)` | `BOOL` | 让系统打开路径或 URL |
| `CLIPBOARD_SET(text)` | `BOOL` | 写入 Unicode 文本剪贴板 |
| `CLIPBOARD_GET()` | `STRING` | 读取 Unicode 文本剪贴板 |
| `LAST_ERROR()` | `STRING` | 最近一次桌面操作错误 |

非 Windows 平台当前明确返回失败；runtime 不会通过 `system()` 拼接用户输入执行 shell 命令。

## 窗口 GUI SYS.GUI

SA 没有函数指针，经典的"回调注册"式 GUI 表达不了。`SYS.GUI` 走复古轮询路线：控件创建时带数字 control id，`WAIT_EVENT()` 阻塞取事件返回被点击的 id，SA 侧用 `WHILE`/`GOTO` + `IF` 分发——这同时也是 Win32 `WM_COMMAND` 的原生模式。

```basic
10 USE SYS.GUI AS G
20 DIM win AS HANDLE AS WINDOW AS VAR
30 DIM box AS HANDLE AS WIDGET AS VAR
40 DIM w AS HANDLE AS WIDGET AS VAR
50 DIM ev AS NUM AS LONG AS VAR
60 DIM ok AS BOOL AS VAR
70 SUB main AS PUBLIC AS VOID
80 win = G.WINDOW("Demo", 300, 120)
90 box = G.TEXTBOX(win, 10, 10, 200, 24)
100 w = G.BUTTON(win, 1, "OK", 10, 44, 60, 26)
110 ::loop
120 ev = G.WAIT_EVENT()
130 IF ev = 1 THEN
140 PRINT G.GET_TEXT(box)
150 ok = G.CLOSE(win)
160 END IF
170 IF ev > 0 THEN
180 GOTO ::loop
190 END IF
200 .ENDSUB
210 CALL main
220 END
```

| 函数 | 返回 | 说明 |
|---|---|---|
| `WINDOW(title, width, height)` | `HANDLE AS WINDOW` | 创建并显示窗口（固定大小，客户区尺寸） |
| `BUTTON(win, id, text, x, y, w, h)` | `HANDLE AS WIDGET` | 按钮，id 取 1..65535，点击时作为事件返回 |
| `LABEL(win, text, x, y, w, h)` | `HANDLE AS WIDGET` | 静态文本 |
| `TEXTBOX(win, x, y, w, h)` | `HANDLE AS WIDGET` | 单行输入框 |
| `SET_TEXT(widget, text)` / `GET_TEXT(widget)` | `BOOL` / `STRING` | 读写控件文本（UTF-8） |
| `WAIT_EVENT()` | `LONG` | 阻塞直到事件：>0 为被点击按钮的 id，0 表示所有窗口已关闭 |
| `CLOSE(win)` | `BOOL` | 关闭窗口（点 X 等价） |
| `LAST_ERROR()` | `STRING` | 最近一次 GUI 操作错误 |

说明：

- 仅 Windows 有真实实现（user32/gdi32，Unicode，控件用系统默认 GUI 字体）；其他平台所有函数返回失败并提供 `LAST_ERROR()`。
- `WINDOW`/`WIDGET` 是两个不同的 HANDLE kind，把"把窗口句柄传给 SET_TEXT"这类错误留在编译期。
- WIDGET 随窗口销毁，无需显式关闭；窗口点 X 或 `CLOSE` 后句柄自动失效。
- 事件循环建议以 `WAIT_EVENT() = 0`（全部窗口关闭）作为退出条件。

## 语法糖 SYS.LINT

`SYS.LINT` 用于编译期语法糖开关。当前只支持：

```basic
USE SYS.LINT AS NONE_NUMBER
SUB main AS PUBLIC AS VOID
PRINT "no line numbers required"
.ENDSUB
CALL main
END
```

- `NONE_NUMBER`：允许源码不写行号。编译器会在解析前为所有非空行自动补上 `10, 20, 30...`。
- 若源码里已有行号，开启 `NONE_NUMBER` 后会按出现顺序整体重写，不再保留旧行号。
- 未写 `USE SYS.LINT AS NONE_NUMBER` 时，仍强制要求每一非空行都以递增正整数行号开头。
- `SYS.LINT` 只影响编译前预处理，不会生成 runtime 依赖。
