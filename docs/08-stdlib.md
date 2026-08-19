# 标准库

11 个内置模块，`USE` 之后通过别名访问。别名可以任取，但**必须**带前缀——`USE SYS.STRING AS S` 之后只能写 `S.LENGTH(...)`，不能写 `LENGTH(...)`。

内置模块在编译期直接 lowering，不产生独立符号或头文件，也没有运行时加载开销。详见[第 7 章](./07-modules.md#两类模块)。

## 目录

- [SYS.IO 控制台输入](#sysio-控制台输入)
- [SYS.MATH 数学](#sysmath-数学)
- [SYS.STRING 字符串](#sysstring-字符串)
- [SYS.BINARY 二进制缓冲区](#sysbinary-二进制缓冲区)
- [SYS.LIST 动态列表](#syslist-动态列表)
- [SYS.MAP 关联容器](#sysmap-关联容器)
- [SYS.FILE 文件](#sysfile-文件)
- [SYS.NET 网络](#sysnet-网络)
- [SYS.DESKTOP 桌面集成](#sysdesktop-桌面集成)
- [SYS.GUI 窗口界面](#sysgui-窗口界面)
- [SYS.LINT 编译期语法糖](#syslint-编译期语法糖)
- [资源管理约定](#资源管理约定)
- [平台差异汇总](#平台差异汇总)

## SYS.IO 控制台输入

这个模块只提供一件事：读一行输入。它是**语句**而不是函数：

```
<别名>.INPUT "<提示文本>", <变量>
```

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

提示文本原样输出（不自动换行），然后读取一行赋给变量。目标变量可以是 `STRING` 或数值类型，数值会做转换。

输出用语言内置的 `PRINT`，不需要 `SYS.IO`。

## SYS.MATH 数学

常量：

| 常量 | 类型 | 值 |
|---|---|---|
| `PI` | `DOUBLE` | 3.14159265358979323846 |
| `E` | `DOUBLE` | 2.71828182845904523536 |
| `TAU` | `DOUBLE` | 6.28318530717958647692 |
| `EPSILON` | `DOUBLE` | 2.2204460492503131e-16 |
| `MAX_LONG` | `LONG` | 9223372036854775807 |
| `MIN_LONG` | `LONG` | -9223372036854775808 |

函数只有一个：

| 函数 | 签名 | 说明 |
|---|---|---|
| `POW(base, exp)` | `NUM, NUM -> DOUBLE` | 幂。任一参数是 `SYMBOL` 时返回 `SYMBOL` |

```basic
10 USE SYS.MATH AS M
20 DIM r AS NUM AS DOUBLE AS VAR
30 DIM area AS NUM AS DOUBLE AS VAR
40 SUB main AS PUBLIC AS VOID
50 r = 2.0
60 area = M.PI * M.POW(r, 2.0)
70 PRINT F"area={area} max={M.MAX_LONG}"
80 .ENDSUB
90 CALL main
100 END
```

日常幂运算直接用语言级的 `**` 运算符更方便，`M.POW` 主要用于需要显式 `DOUBLE` 语义的场合。

## SYS.STRING 字符串

常量：`NEWLINE`(`\n`)、`TAB`(`\t`)、`CR`(`\r`)、`EMPTY`(`""`)。

| 函数 | 签名 | 说明 |
|---|---|---|
| `LENGTH(s)` | `STRING -> LONG` | 字节长度 |
| `CONCAT(a, b)` | `STRING, STRING -> STRING` | 拼接 |
| `SLICE(s, start, count)` | `STRING, LONG, LONG -> STRING` | 子串，越界安全裁剪 |
| `FIND(s, sub)` | `STRING, STRING -> LONG` | 首次出现位置，找不到返回 -1 |
| `UPPER(s)` | `STRING -> STRING` | 转大写 |
| `LOWER(s)` | `STRING -> STRING` | 转小写 |
| `REPLACE(s, old, new)` | `STRING, STRING, STRING -> STRING` | 替换所有出现 |

```basic
10 USE SYS.STRING AS S
20 DIM name AS STRING AS VAR
30 SUB main AS PUBLIC AS VOID
40 name = "sonalgebraic"
50 PRINT F"len={S.LENGTH(name)} upper={S.UPPER(name)}"
60 PRINT F"pos={S.FIND(name, "algebra")} slice={S.SLICE(name, 0, 3)}"
70 PRINT S.CONCAT("Hello, ", name)
80 PRINT F"a{S.NEWLINE}b"
90 .ENDSUB
100 CALL main
110 END
```

返回新字符串的函数（`CONCAT` / `SLICE` / `UPPER` / `LOWER` / `REPLACE`）在堆上分配，由编译器自动登记释放，不需要手工管理。

## SYS.BINARY 二进制缓冲区

网络数据包不能用 `STRING` 承载——内嵌 NUL 会截断 C 字符串。`SYS.BINARY` 提供真实字节缓冲区，句柄 kind 是 `BUFFER`。

| 函数 | 签名 | 说明 |
|---|---|---|
| `NEW(length)` | `LONG -> BUFFER` | 创建零填充缓冲区 |
| `LENGTH(buffer)` | `BUFFER -> LONG` | 字节长度，失败 -1 |
| `SLICE(buffer, offset, count)` | `BUFFER, LONG, LONG -> BUFFER` | 复制切片 |
| `COPY(dst, dstOff, src, srcOff, count)` | `... -> BOOL` | 支持重叠复制 |
| `HEX_DECODE(text)` | `STRING -> BUFFER` | HEX 解码 |
| `HEX_ENCODE(buffer)` | `BUFFER -> STRING` | HEX 编码 |
| `PACK_U16_LE/BE`、`PACK_U32_LE/BE`、`PACK_U64_LE/BE` | `BUFFER, LONG, LONG -> BOOL` | 按大小端写无符号字段 |
| `UNPACK_U16_LE/BE`、`UNPACK_U32_LE/BE`、`UNPACK_U64_LE/BE` | `BUFFER, LONG -> LONG` | 按大小端读字段 |
| `CHECKSUM8(buffer, offset, count)` | `BUFFER, LONG, LONG -> LONG` | 字节和模 256 |
| `CLOSE(buffer)` | `BUFFER -> BOOL` | 显式释放 |
| `LAST_ERROR()` | `-> STRING` | 最近一次错误 |

```basic
10 USE SYS.BINARY AS B
20 DIM packet AS HANDLE AS BUFFER AS VAR
30 DIM ok AS BOOL AS VAR
40 SUB main AS PUBLIC AS VOID
50 packet = B.NEW(8)
60 ok = B.PACK_U16_BE(packet, 0, 4660)
70 ok = B.PACK_U32_LE(packet, 2, 2309737967)
80 PRINT B.HEX_ENCODE(packet)
90 PRINT F"len={B.LENGTH(packet)} sum={B.CHECKSUM8(packet, 0, 8)}"
100 ok = B.CLOSE(packet)
110 .ENDSUB
120 CALL main
130 END
```

## SYS.LIST 动态列表

定长数组覆盖不了「边跑边收集」。`SYS.LIST` 提供可变长列表，数值和字符串分成两个句柄 kind——把「字符串列表传给数值函数」这类错误留在编译期。

数值列表（`HANDLE AS LIST`，元素按 `DOUBLE` 存储）：

| 函数 | 签名 | 说明 |
|---|---|---|
| `NEW()` | `-> LIST` | 创建空列表 |
| `PUSH(list, value)` | `LIST, DOUBLE -> BOOL` | 尾部追加 |
| `POP(list)` | `LIST -> DOUBLE` | 弹出尾元素，空表返回 0 并记录错误 |
| `GET(list, index)` | `LIST, LONG -> DOUBLE` | 按下标读 |
| `SET(list, index, value)` | `LIST, LONG, DOUBLE -> BOOL` | 按下标写 |
| `INSERT(list, index, value)` | `LIST, LONG, DOUBLE -> BOOL` | 在 index 处插入（0..LENGTH） |
| `REMOVE(list, index)` | `LIST, LONG -> BOOL` | 删除指定下标 |
| `LENGTH(list)` | `LIST -> LONG` | 元素个数，失败 -1 |
| `CLEAR(list)` | `LIST -> BOOL` | 清空但保留列表 |
| `CLOSE(list)` | `LIST -> BOOL` | 显式释放 |

字符串列表（`HANDLE AS STR_LIST`）是同名函数加 `_STR` 后缀：`NEW_STR` / `PUSH_STR` / `POP_STR` / `GET_STR` / `SET_STR` / `INSERT_STR` / `REMOVE_STR` / `LENGTH_STR` / `CLEAR_STR` / `CLOSE_STR`，另有 `JOIN_STR(list, separator) -> STRING`。

`LAST_ERROR()` 返回最近一次列表操作错误。

```basic
10 USE SYS.LIST AS L
20 DIM xs AS HANDLE AS LIST AS VAR
30 DIM names AS HANDLE AS STR_LIST AS VAR
40 DIM ok AS BOOL AS VAR
50 SUB main AS PUBLIC AS VOID
60 xs = L.NEW()
70 ok = L.PUSH(xs, 10)
80 ok = L.PUSH(xs, 20)
90 PRINT F"len={L.LENGTH(xs)} first={L.GET(xs, 0)}"
100 ok = L.CLOSE(xs)
110 names = L.NEW_STR()
120 ok = L.PUSH_STR(names, "alpha")
130 ok = L.PUSH_STR(names, "beta")
140 PRINT L.JOIN_STR(names, ",")
150 ok = L.CLOSE_STR(names)
160 .ENDSUB
170 CALL main
180 END
```

数值元素统一按 `DOUBLE` 存储，约 2^53 以内的整数无损。需要精确 64 位整数序列时用 `SYS.BINARY` 的缓冲区。

## SYS.MAP 关联容器

STRING key 的哈希映射（链地址法，负载超 1 自动翻倍 rehash）。值同样分两个 kind：`HANDLE AS MAP`（值按 `DOUBLE` 存储）和 `HANDLE AS STR_MAP`。

| 函数 | 签名 | 说明 |
|---|---|---|
| `NEW()` | `-> MAP` | 创建空映射 |
| `SET(map, key, value)` | `MAP, STRING, DOUBLE -> BOOL` | 插入或覆盖 |
| `GET(map, key)` | `MAP, STRING -> DOUBLE` | 取值，缺 key 返回 0 并记录错误 |
| `HAS(map, key)` | `MAP, STRING -> BOOL` | key 是否存在 |
| `REMOVE(map, key)` | `MAP, STRING -> BOOL` | 删除 key |
| `LENGTH(map)` | `MAP -> LONG` | 键值对个数，失败 -1 |
| `KEYS(map)` | `MAP -> STR_LIST` | 所有 key 的快照列表（无序） |
| `CLEAR(map)` | `MAP -> BOOL` | 清空但保留映射 |
| `CLOSE(map)` | `MAP -> BOOL` | 显式释放 |

字符串映射是同名加 `_STR`：`NEW_STR` / `SET_STR` / `GET_STR` / `HAS_STR` / `REMOVE_STR` / `LENGTH_STR` / `KEYS_STR` / `CLEAR_STR` / `CLOSE_STR`。

```basic
10 USE SYS.MAP AS M
20 USE SYS.LIST AS L
30 DIM scores AS HANDLE AS MAP AS VAR
40 DIM keys AS HANDLE AS STR_LIST AS VAR
50 DIM ok AS BOOL AS VAR
60 SUB main AS PUBLIC AS VOID
70 scores = M.NEW()
80 ok = M.SET(scores, "alice", 99)
90 ok = M.SET(scores, "bob", 87)
100 PRINT F"alice={M.GET(scores, "alice")} has_bob={M.HAS(scores, "bob")}"
110 keys = M.KEYS(scores)
120 PRINT L.JOIN_STR(keys, ",")
130 ok = L.CLOSE_STR(keys)
140 ok = M.CLOSE(scores)
150 .ENDSUB
160 CALL main
170 END
```

`KEYS` 返回的是 `STR_LIST` 句柄，要用 `SYS.LIST` 的函数遍历，**并且用完要 `CLOSE_STR`**。也因为这层依赖，`USE SYS.MAP` 会自动连带启用 list runtime。

## SYS.FILE 文件

句柄 API（kind 是 `FILE`）：

| 函数 | 签名 | 说明 |
|---|---|---|
| `OPEN(path, mode)` | `STRING, STRING -> FILE` | mode: `READ` / `WRITE` / `APPEND` / `UPDATE` / `CREATE`，失败返回 `NULL` |
| `READ(file, count)` | `FILE, LONG -> STRING` | 从当前位置最多读 count 字节，不接受内嵌 NUL |
| `WRITE(file, text)` | `FILE, STRING -> LONG` | 写入字节数，失败 -1 |
| `SEEK(file, offset, origin)` | `FILE, LONG, STRING -> BOOL` | origin: `START` / `CURRENT` / `END` |
| `TELL(file)` | `FILE -> LONG` | 当前偏移，失败 -1 |
| `SIZE(file)` | `FILE -> LONG` | 文件大小，并恢复原偏移 |
| `CLOSE(file)` | `FILE -> BOOL` | 显式关闭 |

路径便捷 API（都接受路径字符串，不用开句柄）：

| 函数 | 签名 |
|---|---|
| `READ_TEXT(path)` | `STRING -> STRING` |
| `WRITE_TEXT(path, text)` | `STRING, STRING -> BOOL` |
| `APPEND_TEXT(path, text)` | `STRING, STRING -> BOOL` |
| `EXISTS(path)` / `IS_FILE(path)` / `IS_DIR(path)` | `STRING -> BOOL` |
| `DELETE(path)` / `MKDIR(path)` | `STRING -> BOOL` |
| `CWD()` | `-> STRING` |
| `ABSOLUTE(path)` | `STRING -> STRING` |
| `LAST_ERROR()` | `-> STRING` |

```basic
10 USE SYS.FILE AS F
20 DIM file AS HANDLE AS FILE AS VAR
30 DIM ok AS BOOL AS VAR
40 SUB main AS PUBLIC AS VOID
50 file = F.OPEN("build/demo.txt", "WRITE")
60 IF file = NULL THEN
70 PRINT F"open failed: {F.LAST_ERROR()}"
80 RETURN
90 .ENDIF
100 PRINT F.WRITE(file, "hello")
110 ok = F.CLOSE(file)
120 file = NULL
130 PRINT F.READ_TEXT("build/demo.txt")
140 ok = F.DELETE("build/demo.txt")
150 .ENDSUB
160 CALL main
170 END
```

`DELETE` 只删除普通文件或空目录，**不做递归删除**。

## SYS.NET 网络

### HTTP / HTTPS

| 函数 | 签名 | 说明 |
|---|---|---|
| `GET(url)` | `STRING -> STRING` | 阻塞 GET，默认 30 秒超时 |
| `STATUS(url)` | `STRING -> LONG` | GET 状态码，传输失败为 0 |
| `POST(url, body, contentType)` | `STRING, STRING, STRING -> STRING` | 阻塞 POST |
| `REQUEST(method, url, body, headers)` | `4×STRING -> STRING` | 通用请求 |
| `REQUEST_STATUS(method, url, body, headers)` | `4×STRING -> LONG` | 通用请求状态码 |
| `REQUEST_TIMEOUT(method, url, body, headers, timeoutMs)` | `4×STRING, LONG -> STRING` | 带毫秒超时 |
| `REQUEST_STATUS_TIMEOUT(method, url, body, headers, timeoutMs)` | `4×STRING, LONG -> LONG` | 带超时的状态码 |
| `LAST_HEADERS()` | `-> STRING` | 最近一次成功响应的原始 header 文本 |
| `LAST_ERROR()` | `-> STRING` | 最近一次传输错误，成功时为空串 |
| `LAST_CODE()` | `-> LONG` | 最近一次平台错误码 |
| `URLENCODE(value)` | `STRING -> STRING` | UTF-8 字节百分号编码 |
| `DNS(host)` | `STRING -> STRING` | 逗号分隔的数字地址 |

### TCP / TLS

| 函数 | 签名 | 说明 |
|---|---|---|
| `TCP_CONNECT(host, port, timeoutMs)` | `STRING, LONG, LONG -> NET_STREAM` | TCP 客户端 |
| `TLS_CONNECT(host, port, timeoutMs)` | `STRING, LONG, LONG -> NET_STREAM` | 校验证书和主机名的 TLS 客户端 |
| `TCP_LISTEN(host, port, backlog)` | `STRING, LONG, LONG -> TCP_LISTENER` | 监听，port 传 0 表示由系统分配 |
| `LOCAL_PORT(listener)` | `TCP_LISTENER -> LONG` | listener 实际本地端口 |
| `TCP_ACCEPT(listener, timeoutMs)` | `TCP_LISTENER, LONG -> NET_STREAM` | 接受连接，超时返回 `NULL` |
| `TCP_LISTENER_CLOSE(listener)` | `TCP_LISTENER -> BOOL` | 关闭 listener |
| `STREAM_SEND(stream, text)` | `NET_STREAM, STRING -> LONG` | 发送字符串字节 |
| `STREAM_RECV(stream, maxBytes)` | `NET_STREAM, LONG -> STRING` | 接收文本，不适合内嵌 NUL |
| `STREAM_SEND_BUFFER(stream, buffer, offset, count)` | `NET_STREAM, BUFFER, LONG, LONG -> LONG` | 二进制发送 |
| `STREAM_RECV_BUFFER(stream, maxBytes)` | `NET_STREAM, LONG -> BUFFER` | 二进制接收 |
| `STREAM_CLOSE(stream)` | `NET_STREAM -> BOOL` | 关闭；TLS 会发 close_notify |

客户端示例见 `examples/net_tls.sa`。服务端示例见 `examples/web_server.sa`——一个完整的迷你 HTTP server，`TCP_LISTEN` + `TCP_ACCEPT` 循环 + 按路径路由。

### UDP

| 函数 | 签名 | 说明 |
|---|---|---|
| `UDP_OPEN()` | `-> UDP_SOCKET` | 创建未绑定 socket |
| `UDP_BIND(socket, host, port)` | `UDP_SOCKET, STRING, LONG -> BOOL` | 绑定本地地址，port 可为 0 |
| `UDP_CONNECT(socket, host, port)` | `UDP_SOCKET, STRING, LONG -> BOOL` | 设置默认远端 |
| `UDP_LOCAL_PORT(socket)` | `UDP_SOCKET -> LONG` | 实际本地端口 |
| `UDP_SEND(socket, text)` | `UDP_SOCKET, STRING -> LONG` | 发到默认远端 |
| `UDP_SEND_TO(socket, host, port, text)` | `UDP_SOCKET, STRING, LONG, STRING -> LONG` | 发到指定地址 |
| `UDP_SEND_BUFFER(socket, buffer, offset, count)` | `... -> LONG` | 二进制发到默认远端 |
| `UDP_SEND_BUFFER_TO(socket, host, port, buffer, offset, count)` | `... -> LONG` | 二进制发到指定地址 |
| `UDP_RECV(socket, maxBytes)` | `UDP_SOCKET, LONG -> STRING` | 接收一个 datagram |
| `UDP_RECV_BUFFER(socket, maxBytes)` | `UDP_SOCKET, LONG -> BUFFER` | 二进制接收 |
| `UDP_CLOSE(socket)` | `UDP_SOCKET -> BOOL` | 关闭 |

`LAST_PEER_HOST()` / `LAST_PEER_PORT()` 返回最近一次 accept 或 recvfrom 的对端信息。

## SYS.DESKTOP 桌面集成

| 函数 | 签名 | 说明 |
|---|---|---|
| `MESSAGE(title, text)` | `STRING, STRING -> BOOL` | 原生 Unicode 信息框 |
| `OPEN(target)` | `STRING -> BOOL` | 让系统打开路径或 URL |
| `CLIPBOARD_SET(text)` | `STRING -> BOOL` | 写入 Unicode 文本剪贴板 |
| `CLIPBOARD_GET()` | `-> STRING` | 读取 Unicode 文本剪贴板 |
| `LAST_ERROR()` | `-> STRING` | 最近一次错误 |

```basic
10 USE SYS.DESKTOP AS D
20 DIM text AS STRING AS VAR
30 DIM ok AS BOOL AS VAR
40 SUB main AS PUBLIC AS VOID
50 ok = D.CLIPBOARD_SET("hello")
60 text = D.CLIPBOARD_GET()
70 PRINT text
80 .ENDSUB
90 CALL main
100 END
```

非 Windows 平台当前明确返回失败。runtime **不会**通过 `system()` 拼接用户输入执行 shell 命令。

## SYS.GUI 窗口界面

SA 没有函数指针，经典的「回调注册」式 GUI 表达不了。`SYS.GUI` 走复古轮询路线：控件创建时带一个数字 control id，`WAIT_EVENT()` 阻塞取事件并返回被点击的 id，SA 侧用 `WHILE` 或 `GOTO` 加 `IF` 分发——这同时也是 Win32 `WM_COMMAND` 的原生模式。

| 函数 | 签名 | 说明 |
|---|---|---|
| `WINDOW(title, width, height)` | `STRING, LONG, LONG -> WINDOW` | 创建并显示窗口（固定大小，客户区尺寸） |
| `BUTTON(win, id, text, x, y, w, h)` | `WINDOW, LONG, STRING, 4×LONG -> WIDGET` | 按钮，id 取 1..65535 |
| `LABEL(win, text, x, y, w, h)` | `WINDOW, STRING, 4×LONG -> WIDGET` | 静态文本 |
| `TEXTBOX(win, x, y, w, h)` | `WINDOW, 4×LONG -> WIDGET` | 单行输入框 |
| `SET_TEXT(widget, text)` | `WIDGET, STRING -> BOOL` | 写控件文本 |
| `GET_TEXT(widget)` | `WIDGET -> STRING` | 读控件文本（UTF-8） |
| `WAIT_EVENT()` | `-> LONG` | 阻塞直到事件：>0 为被点击按钮的 id，0 表示所有窗口已关闭 |
| `CLOSE(win)` | `WINDOW -> BOOL` | 关闭窗口（等价于点 X） |
| `LAST_ERROR()` | `-> STRING` | 最近一次错误 |

```basic
10 USE SYS.GUI AS G
20 DIM win AS HANDLE AS WINDOW AS VAR
30 DIM box AS HANDLE AS WIDGET AS VAR
40 DIM btn AS HANDLE AS WIDGET AS VAR
50 DIM ev AS NUM AS LONG AS VAR
60 DIM ok AS BOOL AS VAR
70 SUB main AS PUBLIC AS VOID
80 win = G.WINDOW("Demo", 300, 120)
90 box = G.TEXTBOX(win, 10, 10, 200, 24)
100 btn = G.BUTTON(win, 1, "OK", 10, 44, 60, 26)
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

- `WINDOW` 和 `WIDGET` 是两个不同的句柄 kind，把「窗口句柄传给 `SET_TEXT`」这类错误留在编译期。
- WIDGET 随窗口销毁，无需显式关闭；窗口点 X 或 `CLOSE` 之后句柄自动失效。
- 事件循环建议以 `WAIT_EVENT() = 0`（全部窗口已关闭）作为退出条件。
- 这个示例会开真窗口并进事件循环，没人点就不退出——不要在无人值守的环境里跑。

## SYS.LINT 编译期语法糖

编译期开关，不产生任何 runtime 依赖。当前只支持一个：

```basic
USE SYS.LINT AS NONE_NUMBER
SUB main AS PUBLIC AS VOID
PRINT "no line numbers required"
.ENDSUB
CALL main
END
```

- `NONE_NUMBER` 允许源码不写行号，编译器在解析前按出现顺序补上 `10, 20, 30...`。
- 源码里已有的行号会被**整体重写**，不予保留。
- 不开这个开关时，仍强制要求每一非空行以递增正整数行号开头。

## 资源管理约定

所有 `HANDLE` 都是可复制的资源 token，**不会自动关闭**。必须调用对应的关闭函数：

| kind | 关闭函数 |
|---|---|
| `FILE` | `CLOSE` |
| `BUFFER` | `CLOSE` |
| `LIST` / `STR_LIST` | `CLOSE` / `CLOSE_STR` |
| `MAP` / `STR_MAP` | `CLOSE` / `CLOSE_STR` |
| `NET_STREAM` | `STREAM_CLOSE` |
| `TCP_LISTENER` | `TCP_LISTENER_CLOSE` |
| `UDP_SOCKET` | `UDP_CLOSE` |
| `WINDOW` | `CLOSE` |
| `WIDGET` | 随窗口销毁 |

runtime 会让已关闭句柄的旧副本失效，并在进程退出时兜底清理——但那只是安全网，不是可依赖的策略。

**返回句柄的函数必须直接赋给对应类型的变量**（或从同类型 `SUB` 直接 `RETURN`），不能写成 `B.LENGTH(B.SLICE(...))` 这种匿名嵌套。匿名中间句柄没有变量承接，就没法 `CLOSE`，必然泄漏。

## 平台差异汇总

| 能力 | Windows | POSIX（Linux / macOS） |
|---|---|---|
| HTTP / HTTPS | WinHTTP | socket；HTTPS 需要 OpenSSL |
| 原始 TLS stream | 系统 Schannel | OpenSSL |
| 文件路径 | runtime 内转 UTF-16 | 直接用 UTF-8 字节路径 |
| `SYS.DESKTOP` | user32 原生实现 | 明确返回失败 + `LAST_ERROR()` |
| `SYS.GUI` | Win32（user32/gdi32） | GTK3，需要开发文件 |

POSIX 上编译 TLS 需要 OpenSSL 开发文件和 `libssl` / `libcrypto`；Windows 用系统 Schannel，不需要外部 TLS SDK。

`SYS.GUI` 在 Linux/macOS 上依赖 GTK3 开发文件（`pkg-config` 能查到 `gtk+-3.0`，如 Debian/Ubuntu 的 `libgtk-3-dev`）。装了就在编译期自动启用；没装则编译仍然成功，但所有 GUI 函数运行时返回失败并提供 `LAST_ERROR()`——和 POSIX TLS 需要 OpenSSL 的策略一致。交叉编译时不启用 GTK 后端。
