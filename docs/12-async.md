# 12. 异步子程序与 Promise

SonAlgebraic 用 `ASYNC SUB`、`PROMISE OF T`、`AWAIT` 和 `SYNC` 表达异步任务。**C 和 native 后端**均支持核心异步模型：native 直接生成 LLVM IR 的堆帧和恢复状态机，两端共用 Promise 与事件循环运行时。它使用单线程、协作式调度，不会为每个任务创建线程，也不会把纯计算自动变成多核并行。

本章每个 `basic` 代码块都是独立完整程序，包含入口调用和 `END`，由 `tests/test_docs_examples.py` 自动执行解析和语义检查。该测试不执行网络请求，也不验证运行时输出。

仓库可运行示例：[纯计算 Promise 入门](../examples/async/basics.sa)、[异步回显服务端](../examples/async/echo_server.sa)；运行条件见[示例导航](../examples/README.md)。

## 12.1 三种获取结果的方式

| 写法 | 含义 |
|---|---|
| `p = CALL work()` | 创建任务并取得 `PROMISE OF T`，暂不取结果 |
| `value = AWAIT work()` 或 `value = AWAIT p` | 挂起当前异步子程序，让事件循环推进其他任务；完成后消费结果 |
| `value = SYNC work()` 或 `value = SYNC p` | 在当前调用点驱动事件循环直到目标完成，再消费结果 |

`CALL` 得到的是任务凭据，不是最终的 `T`。用户协程创建后进入就绪队列，函数体由事件循环推进；仅创建 Promise 不保证函数体已经执行。

```basic
10 ASYNC SUB double(n AS NUM AS LONG) AS NUM AS LONG
20 RETURN n * 2
30 .ENDSUB
40 ASYNC SUB via_await() AS NUM AS LONG
50 DIM value AS NUM AS LONG AS VAR
60 value = AWAIT double(10)
70 RETURN value
80 .ENDSUB
90 SUB main AS PUBLIC AS VOID
100 DIM pending AS PROMISE OF NUM AS LONG AS VAR
110 DIM first AS NUM AS LONG AS VAR
120 DIM second AS NUM AS LONG AS VAR
130 DIM third AS NUM AS LONG AS VAR
140 pending = CALL double(20)
150 first = SYNC pending
160 second = SYNC double(30)
170 third = SYNC via_await()
180 PRINT F"{first} {second} {third}"
190 .ENDSUB
200 CALL main
210 END
```

三个结果依次为 `40`、`60`、`20`。`SYNC` 等待目标时仍会推进其他就绪任务，并非只执行这个目标。

### 类型与位置限制

- 异步结果只支持非数组 `NUM`、`BOOL`、`STRING`、带 kind 的 `HANDLE` 和 `VOID`。`NUM` 仍按普通语法指定数值子类型，例如 `NUM AS LONG`。
- `ASYNC SUB` 返回 `SYMBOL`、`ERROR`、`ENTITY`、`CPTR`、`PTR`、`SUB`、嵌套 `PROMISE` 等类型，会在 `check` 语义检查阶段报错。`PROMISE OF T` 的 `T` 有相同限制。
- 这些是**结果限制**，不是参数白名单。`SYMBOL`、`ERROR` 等按值参数不因此被拒绝；异步参数不能用 `AS REF`。合法 `PROMISE OF T` 参数以及普通同步 `SUB` 返回 `PROMISE OF T` 仍可声明，但须遵守下文的单消费者使用约定。
- `AWAIT` 只能出现在 `ASYNC SUB` 中，当前不能放进 `TRY/CATCH` 结构。`SYNC` 可用于普通子程序，异步子程序内部优先使用 `AWAIT`。
- `AWAIT` / `SYNC` 是语句级取值形式，不能藏在 `1 + AWAIT work()` 这样的表达式中；先把结果赋给变量，再参与运算。
- 独立 `CALL work()` 不能启动用户异步子程序，`TRY CALL work()` 也不能直接指向异步子程序。必须取得 Promise，或使用 `AWAIT` / `SYNC`。
- 异步子程序不能取普通函数引用，也暂不支持在内部使用 `NEW SUB`。

无结果任务仍产生 `PROMISE OF VOID`，用独立的 `AWAIT` / `SYNC` 等待：

```basic
10 ASYNC SUB tick() AS VOID
20 PRINT "完成一次任务"
30 RETURN
40 .ENDSUB
50 ASYNC SUB run() AS VOID
60 AWAIT tick()
70 RETURN
80 .ENDSUB
90 SUB main AS PUBLIC AS VOID
100 SYNC run()
110 .ENDSUB
120 CALL main
130 END
```

独立取值语句也可等待有结果任务并丢弃结果，但资源句柄结果应接住并显式关闭，不能把“忽略值”当成“关闭资源”。

## 12.2 先并发启动，再按顺序等待

两个任务都先创建，再逐个 `AWAIT`。等待第一个时，第二个也有机会推进；等待顺序不等于完成顺序。

```basic
10 ASYNC SUB work(n AS NUM AS LONG) AS NUM AS LONG
20 RETURN n * 2
30 .ENDSUB
40 ASYNC SUB join() AS NUM AS LONG
50 DIM left AS PROMISE OF NUM AS LONG AS VAR
60 DIM right AS PROMISE OF NUM AS LONG AS VAR
70 DIM a AS NUM AS LONG AS VAR
80 DIM b AS NUM AS LONG AS VAR
90 left = CALL work(10)
100 right = CALL work(20)
110 a = AWAIT left
120 b = AWAIT right
130 RETURN a + b
140 .ENDSUB
150 SUB main AS PUBLIC AS VOID
160 DIM total AS NUM AS LONG AS VAR
170 total = SYNC join()
180 PRINT total
190 .ENDSUB
200 CALL main
210 END
```

结果为 `60`。这个小例子展示启动和等待的结构；真正的收益主要来自 I/O 等待重叠。连续执行 `a = AWAIT work(10)`、`b = AWAIT work(20)` 则要等第一个完成才创建第二个。长时间计算或同步阻塞调用不会自动让出执行权，会阻塞同一事件循环中的其他任务。

## 12.3 网络 I/O

`USE SYS.NET AS N` 提供以下异步操作，返回值可直接交给 `AWAIT` / `SYNC`，也可先存入对应 Promise 变量。内置调用使用 `N.xxx(...)`，不需要用户子程序的 `CALL` 前缀。

| 调用 | Promise 结果 |
|---|---|
| `N.CONNECT_ASYNC(host, port)` | `HANDLE AS NET_STREAM` |
| `N.ACCEPT_ASYNC(listener)` | `HANDLE AS NET_STREAM` |
| `N.RECV_ASYNC(stream, max_bytes)` | `STRING` |
| `N.SEND_ASYNC(stream, text)` | `NUM AS LONG`，发送字节数 |

下面是完整客户端。运行前需在本机 `9000` 端口准备一个 TCP 回显服务；没有服务时连接会失败。

```basic
10 USE SYS.NET AS N
20 ASYNC SUB fetch() AS STRING
30 DIM stream AS HANDLE AS NET_STREAM AS VAR
40 DIM sent AS NUM AS LONG AS VAR
50 DIM reply AS STRING AS VAR
60 DIM closed AS BOOL AS VAR
70 stream = AWAIT N.CONNECT_ASYNC("127.0.0.1", 9000)
80 sent = AWAIT N.SEND_ASYNC(stream, "ping")
90 reply = AWAIT N.RECV_ASYNC(stream, 1024)
100 closed = N.STREAM_CLOSE(stream)
110 RETURN reply
120 .ENDSUB
130 SUB main AS PUBLIC AS VOID
140 DIM reply AS STRING AS VAR
150 reply = SYNC fetch()
160 PRINT reply
170 .ENDSUB
180 CALL main
190 END
```

网络边界需要明确：

- TCP 是字节流。一次 `RECV_ASYNC` 最多接收指定字节数，不保证收到完整应用消息；空串可表示对端正常关闭。正式协议需要自己处理消息边界和多次读取。
- `SEND_ASYNC` 会推进发送直到本次字符串发完或出错，返回发送字节数；它不保证对端应用已经处理数据。
- `CONNECT_ASYNC` 会在创建时发起连接，主机名解析仍可能同步阻塞；不能把整个调用理解为绝不阻塞。
- 本例仅展示成功路径的显式关闭。若连接后收发失败，需在同步错误处理边界保留并关闭已取得的句柄，不能依赖 Promise 清理代替 `STREAM_CLOSE`。
- 服务端先用 `TCP_LISTEN` 取得 listener，再 `AWAIT N.ACCEPT_ASYNC(listener)`；listener 用 `TCP_LISTENER_CLOSE` 显式关闭。待完成操作仍使用原有 stream/listener，不要提前关闭它们。

## 12.4 错误处理：在同步边界捕获

异步子程序抛出的错误会使 Promise 失败。`AWAIT` 会把失败继续传播给当前异步任务；`SYNC` 会在同步边界重新抛出。要捕获它，用普通 `SUB` 包住 `SYNC`，再 `TRY CALL` 这个普通子程序。

```basic
10 ASYNC SUB fail() AS VOID
20 THROW NEW ERR_SAMPLE, "异步任务失败"
30 .ENDSUB
40 SUB wait_for_task() AS VOID
50 SYNC fail()
60 .ENDSUB
70 SUB main AS PUBLIC AS VOID
80 DIM trap AS ERROR AS VAR
90 TRY CALL wait_for_task() TRACEBACK ERROR AS trap
100 CATCH ERR_SAMPLE AS caught
110 PRINT caught
120 .ENDTRY
130 .ENDSUB
140 CALL main
150 END
```

错误跨 Promise 传播应保留原错误类型、错误码、消息以及已有的来源行号和子程序名，而不是一律改成 `ERR_ASYNC`。这里按原始 `ERR_SAMPLE` 匹配。已有来源信息不等于完整异步调用栈；不要据此假设已经提供完整堆栈追踪。网络运行时自身产生的异步错误可以是 `ERR_ASYNC`。

失败取值先复制原错误、释放失败 Promise，再重抛。协程正常完成、失败和取消均回收帧拥有的资源；已释放或已移交的字段清零，避免重复释放。跨挂起的托管临时值也进入帧清理登记，包括含 SYMBOL 的 ENTITY 临时值。`tests/test_async_errors.py` 和 `tests/test_native_coroutines.py` 验证多层传播、重复失败后继续调度、原错误位置及净分配归零。

## 12.5 单次消费、丢弃和取消

Promise 按**单消费者**使用：一个任务的结果只交给一个接收者。成功 `AWAIT` / `SYNC` 后，该结果已被取走并释放 Promise；不要再次等待同一个任务，也不要让多个协程同时等待同一个 Promise。

这目前是使用约定和运行时边界，**不是完整的静态线性类型检查**。普通 `q = p` 仍能通过语义检查，后端会复制句柄，不会自动清空 `p` 或建立共享所有权。一个别名消费或清理后，另一个就可能失效。覆盖尚未消费的 `p` 也不会先释放旧任务，因此不能用重新赋值来表达取消。请保持一个 Promise 一个持有者，消费后再复用变量。

局部 Promise 随资源清理而释放：子程序返回或协程终结时会释放仍持有的任务，native 也在声明所在块的正常出口清理它。应在拥有者离开作用域前显式等待需要完成的任务。释放未完成的任务会取消它并清理帧；若它正在等待子任务，会解除等待关系并级联释放子 Promise。已完成但未消费的字符串等 Promise 自有存储会被回收。

```basic
10 ASYNC SUB work() AS VOID
20 PRINT "任务体"
30 RETURN
40 .ENDSUB
50 SUB start_then_drop() AS VOID
60 DIM pending AS PROMISE OF VOID AS VAR
70 pending = CALL work()
80 RETURN
90 .ENDSUB
100 SUB main AS PUBLIC AS VOID
110 CALL start_then_drop()
120 PRINT "已丢弃未执行的任务"
130 .ENDSUB
140 CALL main
150 END
```

这里没有进入事件循环，局部 Promise 在普通子程序返回时已被释放，因此不会打印“任务体”。这不是后台持续运行的任务，也不是“忘了等，程序退出前会自动替你等完”。当前没有用户级 `CANCEL` 语句。

取消不会撤销已经发生的副作用，不会执行尚未到达的用户语句，也不能当成 `FINALLY`。挂起的连接操作若尚未交出 stream，取消会关闭其内部连接 socket；取消 accept/recv/send 不会顺便关闭调用方持有的 listener/stream。成功取得的 `HANDLE` 仍需显式关闭，已完成但未取走的句柄结果也不能假设会自动关闭底层资源。

因此，对必须完成的工作应显式等待；对必须关闭的资源应设计明确的同步错误处理与关闭路径。不要把 Promise 丢弃当成通用资源管理器。

## 12.6 native 后端的实现与边界

通过 `build` / `run` 的 `--backend native` 选择 native 后端。生成的每个异步子程序包含三部分：

- `_start`：分配零初始化堆帧、快照参数，创建并调度 Promise。
- `_resume`：通过状态 `switch` 进入起点或上次 `AWAIT` 的恢复点；挂起前弹出异常垫并返回调度器。
- `_cleanup`：释放帧拥有的局部值、参数和临时资源；完成、失败、取消共用这条回收路径。

`IF` / `FOR` / `WHILE` 可跨 `AWAIT`，循环上界、步长、赋值目标地址及 GOSUB 返回栈保存在堆帧中。参数快照包含 STRING、SYMBOL、ERROR 与托管 ENTITY；取错误按值实参仍受现有前端类型检查约束。Windows x64 clang 已通过真实可执行文件和 `-O0` / `-O2` 清理探针验证。

当前 native 特有边界：

- `GOTO` / `GOSUB` 仅支持同一结构化作用域内跳转；跨 IF/FOR/WHILE/TRY 作用域会报错。
- 异步数组按值参数、PROMISE 数组，以及协程帧中包含 PROMISE 或托管数组字段的 ENTITY 暂不支持。
- 跨 C 模块调用异步子程序已接入 `_start` ABI；跨该边界的 ENTITY / ERROR 按值参数暂不支持。用户模块仍沿用现有 C／库产物链接路径，主程序协程由手写 IR 生成。
- 异常跳转仍沿用 native 现有的 Windows clang ABI；其他目标的异常 ABI 适配不属于本次覆盖范围。

`tests/test_native_async_integration.py` 验证跨模块 SYNC/AWAIT、BOOL/STRING 参数、模块错误传播，以及同一事件循环下的本机 TCP connect/accept/recv/send。
