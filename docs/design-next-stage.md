# SonAlgebraic 下一阶段语言设计草案

> **状态：草案（DRAFT），部分已落地。** 本文是对 SA 下一阶段的整体设计梳理，不是语言参考。标了【已落地】/【部分落地】的小节已经进了编译器，正式用法以 [docs 第 1–11 章](./README.md) 为准，这里只保留设计动机和落地时改了主意的地方；其余小节仍是纯设计。哪些进了、进到哪，见[已落地的部分](#已落地的部分)。
>
> **示例约定：** 本文代码块一律用 ` ```sa ` 标注，**不用** ` ```basic `。原因很实在——`tests/test_docs_examples.py` 只收集 ` ```basic ` 块去做编译校验，草案里大半是还没实现的语法，用 `basic` 标会让 CI 直接红；已落地部分的可编译示例放在正式文档里受检，这里不重复。为聚焦语义，示例多为**片段**、省略了行号；实际代码仍受强制行号规则约束。
>
> **状态图例：**【已落地】编译器已实现，有测试 ·【部分落地】一部分进了实现，剩余见小节内说明 ·【方向已定】设计主线基本确定 ·【细节待定】方向认可、细节未敲死 ·【仅脑洞】记录在案、尚未采纳。

## 这批设计要还的账

第 6 章 [指针与 C FFI · 当前限制](./06-pointers-and-ffi.md#当前限制) 挂着几笔明账：

> C struct 字段访问、**函数回调、字符串所有权转换**都还没有，需要后续扩展。SA 没有**函数指针**，所以回调式 API（包括经典的 GUI 回调注册）暂时表达不了——`SYS.GUI` 走的是轮询式事件循环，就是这个原因。

本文的**函数指针（`PTR TO SUB`）、托管回调、所有权语义（`= / f= / m=`）、托管 HANDLE、异步模型、以及 Context Manager 上下文治理体系**，就是来平这几笔账的（所有权语义、异步模型、函数指针和 GUI 托管回调已经落地，第 6 章那两笔「没有函数指针 / 回调」的账已销）。它们不是各自独立的语法糖，而是围绕一个共同哲学长出来的七个面：

> **边界不允许隐式穿透。** 作用域、扩展链、所有权、执行上下文、异步实例，每一处都有明确的屏障；跨越屏障必须显式，且身份与权限会随之改变。

## 七大方向总览

| 方向 | 关键构造 | 解决的问题 | 进度 |
|---|---|---|---|
| [一、函数模型](#一函数模型) | `REAL`/`VIRTUAL SUB`、`PTR TO SUB`、`NEW SUB`、Lambda、托管回调、`CALLRET`、实体函数 | 函数有了正式的身份、指针、生命周期和回调出口 | `PTR TO SUB`、`NEW SUB`、`CALLRET`、GUI 托管回调 ✅（C/native）；REAL/VIRTUAL、Lambda、实体函数未动 |
| [二、扩展模型](#二扩展模型) | `EXTEND`、`BEFORE`/`AFTER`/`FINALLY`、`TAG`、`::tag` | 不改签名地向执行结构注入，且注入能力可显式转发、可截断 | 未动 |
| [三、作用域模型](#三作用域模型) | Runtime / Instance / Internal+Public Scope、Scope Barrier | 函数身份随作用域边界改变，是整套设计的地基 | 未动 |
| [四、资源模型](#四资源模型) | `= / f= / m=`、托管 HANDLE、callable ownership | 复制/借用/移动三分，作用域绑定的 RAII | `= / f= / m=`、`RETURN` 所有权、callable ownership ✅；托管 HANDLE 未动 |
| [五、异步模型](#五异步模型) | `ASYNC SUB`、`PROMISE OF`、`CALL`/`AWAIT`/`SYNC` | 真正的异步函数与三种取值方式 | ✅ C/native 核心状态机；native 特有边界见第 12 章 |
| [六、上下文治理模型（Context Manager）](#六上下文治理模型context-manager) | Handle Switcher、Scope Exchanger、`SYS.MULTIPROCESS[WORKER/FORK]` | 资源操作权交接、跨域函数能力转换、多进程并发治理 | 未动 |
| [七、开发体验](#七开发体验) | SA lint、自动行号、SA Traceback | 行号不再靠手维护，崩溃回映射到 SA 世界 | 行号 ✅；Traceback 只有编译期 |

### 已落地的部分

截至 2026-09 进了编译器的东西，正式说明都在 docs 正文里，这里只做索引：

| 条目 | 落地范围 | 正式文档 / 测试 |
|---|---|---|
| `= / f= / m=` 复制 / 借用 / 移动（[4.1](#41-复制--借用--移动--f--m已落地)） | `STRING` / `SYMBOL` / `ERROR` / 含托管字段的 `ENTITY`；C 与 native 两个后端；借用存续期间源在**编译期冻结** | [第 2 章 · 赋值：复制、借用、移动](./02-language-basics.md#赋值复制借用移动)、[第 9 章 · 借用与移动如何接进清理登记](./09-implementation-notes.md#借用与移动如何接进清理登记)、`tests/test_ownership.py` |
| `RETURN` 的所有权交接（[4.1](#41-复制--借用--移动--f--m已落地)） | 本帧局部整体搬出、搬不动的深拷贝、临时量直接接管；调用方接管或语句尾释放；两个后端 | [第 9 章 · RETURN 交出去的是一份独立所有权](./09-implementation-notes.md#return-交出去的是一份独立所有权)、`tests/test_return_ownership.py` |
| `ASYNC SUB` / `PROMISE OF <T>` / `CALL` / `AWAIT` / `SYNC`（[5.1](#51-async-sub--promise--call--await--sync已落地)） | 三种取值方式、异步网络、跨模块调用、异常清理及原错误传播；**C/native** | [第 12 章](./12-async.md)、`tests/test_coroutines.py`、`tests/test_async_errors.py`、`tests/test_async_diagnostics.py`、`tests/test_native_coroutines.py`、`tests/test_native_async_integration.py` |
| 自动行号（[7.1](#71-sa-lint-与自动行号部分落地)，部分） | `sonc fmt --renumber`、`USE SYS.LINT AS NONE_NUMBER` | [第 1 章](./01-getting-started.md) |
| `PTR TO SUB` / `@foo()` / `NEW SUB ... FROM` / `CALLRET`（[1.2](#12-函数指针-ptr-to-sub已落地)、[1.3](#13-new-sub--from-ptr生成局部-callable-实体已落地)、[1.6](#16-callret以回调替代-return-的控制流出口已落地)、[4.3](#43-callable-ownership已落地)） | 函数引用、引用计数 callable、`CALLRET` 终结语句；FFI 上对标 C 函数指针；**C/native** | [第 3 章 · 函数引用、callable 与 CALLRET](./03-subroutines.md#函数引用callable-与-callret)、[第 6 章 · 函数指针与回调](./06-pointers-and-ffi.md#函数指针与回调)、[第 9 章 · 函数引用与 callable](./09-implementation-notes.md#函数引用与-callable)、`tests/test_function_model.py`、`tests/test_native_function_model.py` |
| GUI 托管回调（[1.5](#15-托管回调部分落地)，部分） | `SYS.GUI.ON_CLICK` / `RUN`，复用现有事件队列；timer / 网络事件未动 | [第 8 章 · SYS.GUI](./08-stdlib.md#sysgui-窗口界面) |
| SA Traceback（[7.2](#72-sa-traceback部分落地)，部分） | 只有 C **编译期**报错反查 SA 行；运行期 traceback 未动 | [第 9 章 · 行号溯源注释](./09-implementation-notes.md#行号溯源注释) |

---

## 一、函数模型

SA 的函数从“只有一种 `SUB`”升级为一套有身份、有指针、有生命周期的体系。核心是**实函数与虚函数的二分**——这个二分不是修饰符标出来的，而是由[作用域边界](#三作用域模型)决定的。

### 1.1 REAL SUB 与 VIRTUAL SUB【方向已定】

**实函数（REAL SUB）** ——在当前 SA 实例内、用正常语法定义出来的非匿名函数：

```sa
SUB foo(...)
    ...
.ENDSUB
```

特点：

- 存在于实例的**内作用域**（Internal Scope）；
- 可以取函数指针；
- 可以**无条件** `EXTEND`；
- 拥有完整的 SA 函数结构，包括 `TAG` 等结构信息。

**虚函数（VIRTUAL SUB）** ——来源不是“当前实例亲自定义”的函数，包括：`USE` 导入的 SA 函数、Lambda、`USE C` / `USE LIB` 引入的函数、从函数指针生成的实体。虚函数又分两类：

| 类别 | 来源 | 能否 `EXTEND` | 能否 `TAG FOR` |
|---|---|---|---|
| **有名虚函数** | `USE` 导入的 SA 函数、签名完整的 C / Native 函数 | 满足条件后可 `EXTEND` 出新的实函数 | C/Native 无 SA 函数体结构，**不能** `TAG FOR :foo` |
| **无名虚函数** | Lambda、函数指针生成的函数实体 | 只能调用，**不能** `EXTEND` | 不能 |

一个容易混的点要钉死：

> **“有名字变量绑定” ≠ “有名虚函数身份”。** `NEW SUB foo FROM ptr` 里的 `foo` 只是个局部绑定名，它的实体仍然属于**无名**虚函数。

> 术语衔接：现有实现里 C 函数通过 [`DECLARE C SUB`](./06-pointers-and-ffi.md#c-ffi-声明) 注册、模块通过 [`USE`](./07-modules.md) 导入。本文的“有名虚函数”就是这两类导入函数在当前实例里的身份归类。

### 1.2 函数指针 `PTR TO SUB`【已落地】

正式的函数指针类型，与现有 [`PTR TO T`](./06-pointers-and-ffi.md#两种指针) 同族：

```sa
DIM funcP AS PTR TO SUB AS VAR

SUB foo()
    ...
.ENDSUB

funcP f= @foo()
```

> 记号扩展提醒：现有 `@` 只用于**变量取址**（`@x`、`@实体.字段`）。这里 `@foo()` 把 `@` 扩展到**取函数引用**，用尾随 `()` 表明“这是个函数而非变量”。取址符复用要在语法层明确区分两种语义。

**函数引用本身是只读对象**，所以：

```text
PTR TO SUB
= 可复制（copy）
= 可借用（borrow）
≠ 可移动目标（movable）
```

不允许移动：

```sa
a m= @foo()
```

理由干净利落——函数引用**不持有函数代码的所有权**，没有可转移的东西，`m=` 自然非法。这条约束和[资源模型](#四资源模型)里的所有权规则是同一套。

> 落地说明（用法见[第 3 章](./03-subroutines.md#函数引用callable-与-callret)）：
>
> - 带签名的写法是 `PTR TO SUB(x AS NUM AS LONG) AS NUM AS LONG`，裸写 `PTR TO SUB` 等于无参 `VOID`；签名比较不看参数名。调用直接写 `op(3)`，空引用调用抛 `ERR_NULL_CALL`。
> - `@` 的两种语义靠尾随 `()` 在语法层区分，`@foo()` 能取本文件 `SUB`、用户模块 `PUBLIC SUB` 和 `DECLARE C` 函数。
> - **FFI 边界上对标 C 函数指针，内部表示可以养胖。** 现在内部就是瘦指针（`SaSubFn`），出入 FFI 原样传递、不生成中转函数，`@CB.cfunc()` 拿到的是裸地址。转换只集中在 codegen 的一个点上，将来为 Lambda 换成 `{fn, env}` 胖指针时，FFI 一侧只改那一处（见[第 9 章](./09-implementation-notes.md#ffi-边界唯一的转换点)）。

### 1.3 `NEW SUB ... FROM ptr`：生成局部 callable 实体【已落地】

要把一个函数指针变成**具有 SA 局部生命周期的可调用体**：

```sa
NEW SUB fooo FROM tp
```

注意这里**不需要** `.ENDSUB`——因为它不是在定义新函数体，而是**从函数指针生成一个局部可调用实体**。这个实体属于[无名虚函数](#11-real-sub-与-virtual-sub方向已定)。

于是三层关系非常清楚：

```text
@foo()                 函数引用（只读，不拥有代码）
   │
   ▼
PTR TO SUB             保存 / 借用函数引用
   │  NEW SUB … FROM
   ▼
local callable entity  拥有 SA 局部生命周期的可调用实体
```

因为这个 callable **自己有生命周期**，所以它的所有权可以合法转移：

```sa
dest m= fooo
```

这正是“函数引用不可 `m=`，但 callable 实体可 `m=`”的分界线所在。

> 落地说明：callable 的类型写作 `SUB(参数) AS T`，C 里是引用计数的 `SaCallable*`。「局部生命周期」只是默认——它和 `STRING` 一样可以传参、返回、存进 `ENTITY` 字段和全局变量，计数归零时释放。callable 不能过 FFI，C 不会替它维护计数。REAL / VIRTUAL 二分还没落地，所以「无名虚函数」目前只体现为：callable 不能再 `@` 取址。

### 1.4 Lambda：无名虚函数【方向已定，未进首期】

局部 Lambda 归类为**无名虚函数**：可以调用、可以进入 callback / callable 系统，但**不能**拿它继续做结构化 `EXTEND`。

【细节待定】如果 Lambda 要**捕获局部变量**，得进一步处理 closure environment 与 lifetime——捕获值还是捕获引用、闭包逃逸后被捕获变量的生命周期如何延续，都还没定。这一块和[托管 HANDLE](#42-托管-handle方向已定) 的逃逸规则要一起想。

> 函数模型首期没做 Lambda，原因有两个：一是捕获语义（[未决问题 #3](#九未决问题)）没定，而没有捕获的 Lambda 和「具名 `SUB` + `@foo()`」没有区别，只是省了个名字；二是捕获一旦落地，函数引用就得从瘦指针换成 `{fn, env}`，`env` 的生命周期要跟[托管 HANDLE](#42-托管-handle方向已定) 的逃逸规则一起设计，这块现在还是空的。首期已经把 FFI 转换收拢到一处，给这次改造留好了位置。

### 1.5 托管回调【部分落地】

SA 要让 callback 不再等价于“裸 C 函数指针”。路径是：函数引用先形成 [callable 实体](#13-new-sub--from-ptr生成局部-callable-实体已落地)，再交给 runtime 托管其回调生命周期。

这会成为一批上层能力的公共地基：

- GUI event（直接填掉第 6 章说的“GUI 回调注册表达不了”这笔账，`SYS.GUI` 就能从轮询升级为真正的事件回调）；
- timer；
- 网络事件；
- FFI callback；
- async runtime。

> 落地说明：GUI 这一项已经进来了，`SYS.GUI.ON_CLICK(button, handler)` 挂回调、`RUN()` 派发（见[第 8 章](./08-stdlib.md#sysgui-窗口界面)）。按[第十节](#十和现有实现的衔接与建议落地顺序)的要求复用了现有事件队列，没有另起一套：回调和 `WAIT_EVENT` 轮询共用同一个队列，派发发生在 `RUN` 自己的栈帧里，这样 SA 异常的 `longjmp` 不会穿过 WndProc / GTK 主循环。FFI callback 走的是 `PTR TO SUB` 裸函数指针，不经过托管层（callable 不过 FFI）。timer、网络事件未动；async runtime 的事件循环和 GUI 队列还没合并。

> 生命周期补充：GUI 派发器临时保活 handler，并用异常清理帧保证正常返回、回调抛错、回调释放注册引用后抛错都归还这份引用；清理后原样重抛，不改写原错误信息。测试覆盖 `-O0` / `-O2`、重复派发的异常栈平衡和零净分配。

### 1.6 `CALLRET`：以回调替代 RETURN 的控制流出口【已落地】

`CALLRET` **不是** `return foo()`，**也不是** tail-call。它的含义是：

> **把一个 callable 当作当前函数的返回出口。**

```sa
SUB handler(retp AS PTR TO SUB(...) ...)
    NEW SUB ret FROM retp
    ...
    CALLRET ret(result)
.ENDSUB
```

执行到 `CALLRET ret(result)` 时，不再通过普通 `RETURN` 把结果交给 caller，而是**调用传进来的返回 callback、并携带结果**。

`CALLRET` 是**终结控制流**——它之后的代码不可达：

```sa
CALLRET ret(x)
PRINT "hello"        REM 不可达代码
```

> 实现衔接：现有编译器对非 `VOID SUB` 做严格的[返回路径分析](./03-subroutines.md#返回值)（TODO P0 里为此修过 `IF`/`GOTO`/`TRY-CATCH` 多处）。`CALLRET` 必须纳入这套分析——含 `CALLRET` 的路径应视为“已终结”，其后语句报不可达。
>
> 记号家族：`CALLRET` 天然是**语句级**关键字，和 [`CALL` 的定位](./03-subroutines.md#call-能出现在哪里)一致——不能嵌进表达式中间。

> 落地说明：
>
> - 终结语义、语句级、接进返回路径分析，都按上面实现了，其后同块代码报不可达。目标只能是 callable 或函数引用；具名 `SUB` 直接 `CALL` 再 `RETURN`。
> - 比原稿多了一条：在**非 `VOID`** 的 `SUB` 里，被调 callable 的返回值就是本 `SUB` 的返回值（类型必须能赋过去）；`VOID` 的 `SUB` 里返回值被丢弃，这就是原稿里「把结果交给返回 callback」的用法。这么做是因为非 `VOID` 的 `SUB` 必须有返回值，与其禁止，不如让它自然地透传。
> - 首期限制：`ASYNC SUB` 里不能用；含 `GOSUB` 的 `SUB` 里不能用（那里的返回要先过返回栈分发）。

### 1.7 实体函数（ENTITY 方法）【细节待定，未进首期】

`ENTITY` 准备允许定义自己的内部函数 / 方法，不再只能当纯数据结构。

但要讲清楚一件事：**SA 的“继承”落在 [`SUB EXTEND`](#二扩展模型) 上，而不是传统 class inheritance。** 实体函数提供的是“数据带行为”，不是类型层级。这条决定了 ENTITY 方法的设计不能滑向 OOP 类继承那一套。

> 函数模型首期没做实体函数：它和 `SUB EXTEND` 的分工（[未决问题 #4](#九未决问题)）还没划死，而扩展模型整章都没动，现在定方法语法等于替 `EXTEND` 提前拍板；另外，方法要隐式绑定 `self`，本质上是一种带捕获的 callable，应该和 Lambda 的 `{fn, env}` 表示一起设计。在这之前，「数据带行为」可以用 callable 字段凑合：`ENTITY` 里放一个 `SUB(...)` 类型的字段，调用时手动把实体传进去。

---

## 二、扩展模型

### 2.1 `EXTEND`：函数执行结构继承【方向已定】

`EXTEND` 不是 OOP 类继承，而是：

> **保持函数签名完全不变，对另一个函数的执行结构注入内容。**

不能改变：参数、参数类型、返回类型、签名、调用约定。

主要注入点：

```sa
BEFORE
    ...
.ENDBEFORE
```

```sa
AFTER
    ...
.ENDAFTER
```

```sa
AFTER AS FINALLY
    ...
.ENDAFTER
```

`FINALLY` 无论正常返回还是异常退出都参与退出流程。

> 衔接：这与第 5 章的 [`TRY`/`CATCH`/`THROW`](./05-errors-and-symbols.md) 是同一条退出路径上的东西。`AFTER AS FINALLY` 的清理时机应与现有异常退出时的局部资源释放对齐。

**多层 EXTEND 的注入遵循先进后出（LIFO）**：谁最后注入，谁更靠近当前执行层。

### 2.2 `TAG` 扩展点与“禁止隔代打祖宗”【方向已定】

可以针对**直接父函数**里的 `TAG` 注入：

```sa
TAG FOR :foo AS BEFORE
    ...
.ENDTAG
```

```sa
TAG FOR :foo AS AFTER
    ...
.ENDTAG
```

核心原则：

> **EXTEND 只能扩展直接上一层，不能穿透上一层去修改祖先函数。**

```text
A
↓ EXTEND
B
↓ EXTEND
C
↓ EXTEND
D
```

`D` 只能扩展 `C`。**不能**让 `D` 隔着 `C`、`B` 直接 `TAG FOR` `A` 里的东西。这和 [SA Scope Barrier](#32-sa-scope-barrier方向已定) 是同一套哲学：**边界不允许隐式穿透。**

### 2.3 `::foo`：TAG 显式转发【方向已定】

上一层可以主动把 TAG 继续开放给下一层：

```sa
TAG FOR :foo AS BEFORE
    ...
    ::foo
.ENDTAG
```

于是扩展能力沿链**逐级显式转发**：

```text
A :foo
   │  A 开放给 B
   ▼
B ::foo
   │  B 明确转给 C
   ▼
C ::foo
   │  C 明确转给 D
   ▼
D
```

不是“D 能直接访问 A”，而是一级一级明确转交。**任何中间层不写 `::foo`，就从此截断扩展点。** 所以 `::tag` 本质上是**显式转发扩展 capability**。

### 2.4 一处记号撞车，需要拍板

现有语法里 `::label` 已经被 [`GOSUB ::helper`](./03-subroutines.md#gosub-与标签) 占用（块内标签）。这里 `::foo` 又用作 TAG 转发。两个语境不同（跳转标签 vs 扩展点转发），但记号相同——见[未决问题](#九未决问题)。

---

## 三、作用域模型

这一层现在是整套设计的**地基**。函数是实是虚、能不能 `EXTEND`、`TAG` 打得到打不到，全由作用域边界决定。

### 3.1 三层作用域【方向已定】

最外层是 **SA Runtime Scope**（俗称全局运行时作用域）。

每个 SA 实例（`foo.sa` / `foo.slib` / `foo.spkg`）都有独立的**实例作用域**，内部再分两层：

```text
Instance Scope
├── Internal Scope   只能存放当前实例真正定义的 REAL SUB
└── Public Scope     存放通过 USE / USE C / USE LIB 导入的虚函数
```

- **内作用域（Internal Scope）**：只能存在当前实例真正定义的 [REAL SUB](#11-real-sub-与-virtual-sub方向已定)。
- **公共作用域（Public Scope）**：存放导入进来的虚函数。

> 衔接：这与第 7 章[模块系统](./07-modules.md)的 `USE` 解析、以及 [`AS PUBLIC` / `AS PRIVATE` 可见性](./03-subroutines.md#可见性)是同一套东西的两个视角。可见性讲“能不能被别人看到”，作用域讲“进来之后是什么身份”。

### 3.2 SA Scope Barrier【方向已定】

不同 SA 实例之间存在 **SA Scope Barrier**。函数一旦跨过这道屏障，身份就改变：

```text
Instance A

foo [REAL]
     │
     │ USE
     ▼
════════════ SA Scope Barrier ════════════
     ▼

Instance B

foo [VIRTUAL]
```

由此得到一条铁律：

> **跨作用域之后，调用者永远拿不到另一个实例里的“实函数本体”，只能得到它在当前实例中的虚函数表示。**

跨屏障之后仍可继续加工：

```text
A.foo [REAL]
   │ USE
   ▼
B.foo [NAMED VIRTUAL]
   │ EXTEND
   ▼
B.extFoo [REAL]
```

但 `B.extFoo` 是 **B 自己新生的实函数**，不是把 A 的实函数偷过来了。屏障保证了实例之间的实函数本体永不外泄。

---

## 四、资源模型

对象开始明确区分几种所有权关系。这一层给 SA 带来接近**作用域绑定 RAII / 线性资源管理**的能力。

### 4.1 复制 / 借用 / 移动：`= / f= / m=`【已落地】

> 用户文档：[第 2 章 · 赋值：复制、借用、移动](./02-language-basics.md#赋值复制借用移动)；实现：[第 9 章 · 借用与移动如何接进清理登记](./09-implementation-notes.md#借用与移动如何接进清理登记)。本节保留设计动机，以及落地时和原稿不一样的地方。

```text
=    copy     普通复制
f=   borrow   只读借用
m=   move     所有权转移
```

**普通复制** `a = b`：正常 copy。

**只读借用** `a f= b`：不取得 ownership；不能修改目标；生命周期依赖 `b`。

**所有权转移** `a m= b`：ownership 从 `b` 转移给 `a`，之后 `a = owner`、`b = moved / invalid`，再用 `b` 属于非法访问。

> 与现有 `AS REF` 的分工：[`AS REF`](./03-subroutines.md#引用传参-as-ref) 描述的是**参数传递方式**（调用时如何入栈），`f= / m=` 描述的是**赋值 / 绑定时的所有权关系**。两者不冲突，但语义检查要能把“借用来的值又被 `AS REF` 传出去”这类逃逸路径管起来。

**落地时定下来的：**

- 原稿写的是「`FREE b` 之后 `a` 立即失效」，悬空算用户责任。落地收紧成**编译期冻结**：借用存续期间源不能被赋值、移动、取址、传 `AS REF`，借用者本身只读——上面说的逃逸路径在语义层全部拦死。
- 覆盖 `STRING` / `SYMBOL` / `ERROR` 和含托管字段的 `ENTITY`，两个后端都支持。
- 三条限制：含 `GOTO` / `GOSUB` 的 SUB 里禁用（标签跳转让顺序分析不可靠）；借用目标必须与借用语句同块 `DIM`（借用靠摘清理登记实现，登记按块静态生成）；移动的源不能是全局、`AS REF` 参数或按值传入的 `SYMBOL` / `ERROR` 参数（本帧不持有它们）。
- `RETURN` 走同一套思路：本帧拥有的局部整体搬给调用方（源清零），搬不动的深拷贝，本语句刚算出的临时量直接接管；调用方把返回值当自己的资源接管或在语句尾释放——见[第 9 章 · RETURN 交出去的是一份独立所有权](./09-implementation-notes.md#return-交出去的是一份独立所有权)。
- [`PTR TO SUB`](#12-函数指针-ptr-to-sub已落地) 与 [callable 实体](#43-callable-ownership已落地)已并入同一套检查，见 4.3。

### 4.2 托管 HANDLE【方向已定】

普通 SA [`HANDLE`](./04-composite-types.md)（第 4 章的资源句柄）可以被提升为受 SA 生命周期管理的对象。设计方向：

- 作用域**拥有**资源；
- 不允许随意复制；
- 不能非法逃逸；
- 离开局部作用域**自动 `CLOSE`**；
- `THROW` / 异常退出也要完成清理。

这实际上就是 **SA 自己的 scope-bound RAII / 线性资源管理**。`FILE`、`SOCKET`、`BUFFER`、GUI HANDLE 等都会从这里受益。

> 实现衔接：现有编译器已经做局部 `STRING` / `SYMBOL` / `ERROR` 的[释放策略](./03-subroutines.md#返回值)（TODO P0 已落地），返回前先算返回值、再清本帧资源，返回值的所有权也已明确交给调用方（见 [4.1](#41-复制--借用--移动--f--m已落地)）。托管 HANDLE 是把这套自动清理**从内置类型扩展到用户资源**，清理时机要和 `AFTER AS FINALLY`、异常退出统一到同一条退出路径上。

### 4.3 callable ownership【已落地】

[callable 实体](#13-new-sub--from-ptr生成局部-callable-实体已落地)本身带生命周期，因此纳入同一套所有权规则：函数引用（`PTR TO SUB`）不可 `m=`，callable 实体可 `m=`。这条已在函数模型讲过，此处只是强调它和 `= / f= / m=` 是**同一个所有权系统**，而不是平行的两套。

> 落地说明：确实是同一套。callable 实体走的是 `STRING` 那条路，借用冻结、移动后源失效、`RETURN` 交出所有权都直接沿用；区别只在「复制」是加一份引用计数，不复制函数本身。函数引用写 `m=` 在语义层报错。

---

## 五、异步模型

### 5.1 `ASYNC SUB` / `PROMISE` / `CALL` / `AWAIT` / `SYNC`【已落地】

> 落地范围：C/native 核心异步模型（无栈状态机协程 + 单线程事件循环）。native 直接发射堆帧与 start/resume/cleanup LLVM IR，并共用 runtime；返回值范围及 native 的跨作用域跳转、数组和聚合 ABI 边界见[第 12 章](./12-async.md)。该章包含受检的完整示例；本节保留设计速览，省略号需要替换为实际语句。

```sa
ASYNC SUB asyncfoo(n AS NUM AS LONG) AS STRING
    ...
    RETURN s
.ENDSUB
```

`AWAIT` / `SYNC` 是**语句级关键字**，归入和 [`CALL`](./03-subroutines.md#call-能出现在哪里) 一样的家族：只能独立成句或占据整条赋值右侧，不能嵌进 `CAST`、实参、F-string 里。

异步函数调用有**三种取值方式**：

**① 先启动、后取现（Promise）**

```sa
DIM p AS PROMISE OF STRING AS VAR

p = CALL asyncfoo(1)
x = AWAIT p
```

`p = CALL asyncfoo(1)` 只启动异步实例、拿回 `PROMISE`，以后再 `AWAIT p` 取现。`PROMISE` **带结果类型**，写法 `PROMISE OF <类型>` 与 `PTR TO <类型>` 同构；一个 `PROMISE` 只能取现一次，结果按移动语义交出（不拷贝）。

**② 标准异步调用**

```sa
x = AWAIT asyncfoo(1)
```

启动后等待结果。

**③ 同步调用**

```sa
x = SYNC asyncfoo(1)
```

把异步函数按同步方式驱动到结束，当前执行流**阻塞**。这也是普通 `SUB`（比如 `main`）进入异步世界的唯一入口——`AWAIT` 只允许出现在 `ASYNC SUB` 里。

不要返回值就独立成句：`AWAIT p`、`SYNC asyncfoo(1)`。

**`PROMISE` 的本质**：它关联某个**异步函数实例对象**，`AWAIT` 根据 Promise 找回对应实例、取得返回值。

**落地时定下的边界：**

- `ASYNC SUB` 不能用独立 `CALL` 语句调用（只能 `AWAIT` / `SYNC` / `p = CALL`），也不能作为 `TRY CALL` 的目标；
- `AWAIT` 不能出现在 `TRY` 块内（挂起会破坏 setjmp 异常栈），`SYNC` 不挂起当前帧、不受此限；
- `ASYNC SUB` 不支持 `AS REF` 参数（协程帧需独占参数所有权）；
- `SYMBOL` 参数在启动时递归 clone，`ERROR` 参数复制消息并保留错误元数据，帧独立拥有副本；ENTITY 内 SYMBOL 字段也递归托管。正常完成、异常、未启动即 drop、挂起取消，以及跨挂起的 ENTITY 临时值均有零净分配回归，见[实现说明](./09-implementation-notes.md#协程参数拥有独立资源)。
- Promise 失败保留原错误类型、错误码、消息、行号和 SUB 名称，取值时先释放失败 Promise 再重抛；这是原错误位置传播，尚非完整异步调用栈。网络原语自身错误仍可为 `ERR_ASYNC`。
- 不支持的异步返回类型和 `PROMISE OF T` 内层类型在语义检查阶段报告，不再等到 C 生成阶段。
- 异步返回值目前仅支持 `NUM` / `BOOL` / `STRING` / `HANDLE` / `VOID`；支持 SYMBOL / ERROR 参数不代表支持它们作为异步返回值。`ASYNC SUB` 内仍不能使用 `NEW SUB` / `CALLRET`；
- `SYS.NET` 提供 `ACCEPT_ASYNC` / `RECV_ASYNC` / `SEND_ASYNC` / `CONNECT_ASYNC`，分别返回 `PROMISE OF NET_STREAM` / `STRING` / `NUM` / `NET_STREAM`，是真正会挂起让出的 I/O 点；
- 跨模块调用 `ASYNC SUB` 走同一套 ABI。
- 原稿里「Promise 完全不透明、返回类型靠用户猜」的脑洞版**没有采纳**，落地的就是带类型的 `PROMISE OF <T>`。

---

## 六、上下文治理模型（Context Manager）

`SA Context Manager` 是 SA Runtime 的**上下文治理层**，总职责定义为：

> **管理一个 SA 执行上下文中的资源、作用域与执行载体，以及它们跨 Context 时应该发生什么。**

其核心在于解决实体、SUB 或资源离开当前 Context 时的**权限转移、属性映射与物理执行位置**问题。

```text
SA Context Manager
│
├─ SA Handle Switcher        (资源操作权治理与死锁仲裁)
│
├─ SA Scope Exchanger        (跨 Scope Barrier 函数能力转换)
│
└─ SYS.MULTIPROCESS          (执行上下文跨进程扩展)
    │
    ├─ [WORKER]             (M:N 任务进程池)
    │
    └─ [FORK]               (进程上下文分裂)
```

### 6.1 SA Handle Switcher：资源权属调度与死锁恢复【方向已定】

Handle 在 SA 中已不再是裸资源引用，而是**带 Context ownership 的 Runtime resource**。Handle Switcher 负责 Handle 所有权和操作权在不同 Context 间的动态交接与竞争仲裁。

#### 1. 上下文流转规则
- **同步调用切换**：当 `SUB main` 持有 Handle `H` 并 `CALL SUB A`，若 `A` 申请同一个 `H`，不是新建句柄，而是状态置换：
  - 调用期：`main.H → FROZEN`，`A.H → ACTIVE`
  - 返回后：`A.H → RELEASED`，`main.H → ACTIVE`
- **异步竞争互斥**：若 `main` 派生 `ASYNC A` 与 `ASYNC B` 竞争同一底层 Handle：
  - 执行流按序进入：`A ACTIVE`，`B BLOCKED`，`main FROZEN`
  - `A` 完成释放后，`B` 唤醒激活；在下游竞争未结束前，`main` 始终维持 `FROZEN`。

#### 2. 死锁检测与自动仲裁
当出现循环等待链（如 `A owns H1 → waits H2` 且 `B owns H2 → waits H1`）时，Switcher 介入死锁仲裁：

```text
检测 deadlock cycle
       ↓
选择持有 Handle 时间最短的一方（victim）
       ↓
KILL victim
       ↓
抛出错误：ERROR: Handle Is Locked
       ↓
释放 victim 拥有的所有 Handle
       ↓
其余 Context 自动恢复运行
```

### 6.2 SA Scope Exchanger：跨域能力转换引擎【方向已定】

在单一 Class 或同一文件内（Definition Scope），内部 REAL SUB 互相调用无需流转，直接执行。

当且仅当函数跨越 [SA Scope Barrier](#32-sa-scope-barrier方向已定)（如 `USE classes[Foo] AS Foo`）时，由 Scope Exchanger 负责**能力转换**：

```text
REAL SUB (原始定义域)
       ↓
  Scope Exchanger
       ↓
目标 Scope 中的虚函数视图
```

Scope Exchanger 并非简单地 `export symbol`，而是强制重构函数在目标环境下的权限矩阵：
- 重新赋予 `REAL` / `VIRTUAL` 属性（是否属于 named virtual）；
- 判定是否允许 `EXTEND` 及其限定规则；
- 重置 `TAG` 访问权与 `::tag` 转发许可；
- 约束函数指针生成能力（`@foo()`）与 Native / C callable 的暴露边界。

### 6.3 SYS.MULTIPROCESS：进程执行上下文扩展【方向已定】

`SYS.MULTIPROCESS` 作为 Context Manager 的执行载体扩展层，解决的是：**当前 Context 的 SUB，要怎样跨越 OS 进程边界去运行？** 体系由两个独立的系统 CLASS 构成：

#### 1. `MULTIPROCESS[WORKER]`：M:N 进程任务池
WORKER 采用 **M 个 Worker 进程调度 N 个 Task** 模型，并非 1:1 的短生命周期进程映射：

```sa
USE SYS.MULTIPROCESS[WORKER] AS worker

wk = CALL worker.MAKE foo(a, b)
result = AWAIT wk
```

- **深度复用异步体系**：`worker.MAKE` 产出标准 `PROMISE`，通过统一的 `AWAIT` 消费，对上层屏蔽 Coroutine、Socket Poll 与 OS Process 的底层差异。
- **OnStart 自动化初始化**：
  - WORKER 依赖 SA 的 `OnStart` 机制：首次调用 `worker.MAKE` 若未显式初始化，触发**无参隐式调用** `worker.init()`，默认 Worker 进程数等于 **CPU 核心数**。
  - 用户可显式调用带参初始化：`CALL worker.init(N)`。
- **差异化重塑规则（二次初始化）**：
  - **扩容（`N > current`）**：原地 Resize，增量补充缺少的 Worker（如 4 核扩至 8 核，保留原 W0~W3，追加 W4~W7）。
  - **等容（`N == current`）**：保持不变。
  - **缩容（`N < current`）**：**不执行池内逐个裁减**，直接销毁旧池（Destroy old Pool）并创建新池（Create new Pool）。彻底避免在缩容时裁杀持有 Handle/Promise 上下文的复杂状态。

#### 2. `MULTIPROCESS[FORK]`：进程上下文分裂【细节待定】
作为独立于 WORKER 的模型，表示当前进程上下文的对等分裂：

```text
Current Process Context
           │
          FORK
         /          Parent  Child
```

- 明确为独立 CLASS，绝非 WORKER 的参数分支；
- 针对 Fork 之后 Handle 的所有权迁移、Scope 映射保留以及 Promise 状态克隆机制，留待后续细化。

---

## 七、开发体验

### 7.1 SA lint 与自动行号【部分落地】

对强制行号开发体验的改造：源码可以**不手写全部行号**，由 SA lint / formatter 管理；同时仍兼容人工行号。

如果人工指定的行号与自动规则**撞号**，按 SA 原规则处理：

> **重复行号非法。**

这相当于——保留“行号属于语言语义”这条根，但不再逼程序员人工维护所有编号。

> 已落地：[`sonc fmt --renumber`](./01-getting-started.md) 重排行号；`USE SYS.LINT AS NONE_NUMBER` 让编译器给无行号源码自动补号。剩下的是把「人工行号与自动行号混写时的撞号规则」规范化进语言设计——现在 `NONE_NUMBER` 模式下手写的行号会被整体覆盖，两种写法是二选一，还没有混写。

### 7.2 SA Traceback【部分落地】

运行时 crash / error 后，尽量把底层错误**重新映射回 SA 世界**：SA 调用栈、`SUB`、源代码位置 / 行号、错误代码——而不是把用户扔到 `generated.c:19428` 去考古。

> 已落地：**C 编译期**报错反查 SA 行——驱动拿生成 C 里的 `/* SA nnn: ... */` 注释定位（见[第 9 章 · 行号溯源注释](./09-implementation-notes.md#行号溯源注释)）。未动：**运行期** traceback，难点在于运行期没有编译期的静态注释可依赖，需要运行时侧的行号 / 调用栈元数据。

---

## 八、七个模型如何咬合

这批设计最有说服力的地方，是几个模型合起来能直接拼出一个 **高性能 SA Web/微服务框架**：

```text
EXTEND             →  middleware（中间件结构注入）
ASYNC / AWAIT      →  异步非阻塞 I/O
CALLRET            →  response 控制流出口
Context Manager    →  Worker 进程池任务调度 + 连接 Handle 权属流转
```

典型请求流程：

```text
框架网络层 (Worker Pool / Socket Handle Switcher)
   │  分发 Task，传递 handler 函数指针 + URL + return callback
   ▼
handler 生成局部 callable（NEW SUB FROM ptr）
   │
   ▼
执行中间件与业务逻辑（其间可 AWAIT 异步 I/O 或向 Worker 派发密集计算）
   │
   ▼
CALLRET responseCallback(response)
   │
   ▼
框架获得响应，自动清理释放局部上下文 Handle
```

而更深一层的一致性，是**同一条哲学贯穿七个模型**：

| 模型 | 屏障 / 边界 | 跨越规则 |
|---|---|---|
| 作用域 | SA Scope Barrier | 跨越即由 REAL 降格为 VIRTUAL，受 Scope Exchanger 转换 |
| 扩展 | 直接父层 | 只能扩展上一层，`::tag` 显式转发才能穿透 |
| 资源 | ownership 边界 | 借用不夺权，移动后原绑定失效 |
| 异步 | 异步实例边界 | Promise 显式持有实例，`AWAIT` 显式取现 |
| 上下文治理 | Context 边界 | Handle 显式 Freeze/Active/Release，Worker 池销毁重建 |

一句话收口：**边界不隐式穿透，穿透必须显式、且身份与权属随之改变。**

---

## 九、未决问题

按需要拍板的紧迫度排：

1. **`::` 记号撞车。** `::label` 已被 [`GOSUB`](./03-subroutines.md#gosub-与标签) 占用（块内标签），`::foo` 又要做 TAG 转发。是有意复用（都表达“这是个可被跳转/转发的具名点”），还是需要换记号？——语法层必须能区分。
2. **`USE C` / `USE LIB` vs 现有 `USEC` / `USELIB`。** 笔记里写 `USE C` / `USE LIB`（带空格），现有实现是 [`USEC` / `USELIB`](./06-pointers-and-ffi.md#c-ffi-声明)（连写）。需要统一，否则文档和实现两张皮。
3. **Lambda 闭包捕获。** 捕获值还是捕获引用？闭包逃逸后被捕获变量的生命周期如何延续？和[托管 HANDLE](#42-托管-handle方向已定) 的逃逸规则要一起定。
4. **ENTITY 方法的边界。** 实体函数与 `SUB EXTEND` 的分工要划死，防止滑向 OOP 类继承。
5. **FORK 继承与状态清理细节。** Fork 动作后 Context Manager 如何同步继承 Handle 树、Scope Exchanger 状态与 Promise 状态，需要独立规约。

**随落地拍板、不再悬着的：**

- **`PROMISE` 带不带结果类型** → 带，写法 `PROMISE OF <类型>`，与 `PTR TO <类型>` 同构；「完全不透明」版弃用（见 [5.1](#51-async-sub--promise--call--await--sync已落地)）。
- **"font Copy" 代号** → 没有沿用。正式名就叫复制 / 借用 / 移动（`= / f= / m=`），用户文档和实现里都不出现这个代号。
- **借用悬空是谁的责任** → 原稿「`FREE b` 之后 `a` 立即失效」由用户自己兜，落地改成编译期冻结源（见 [4.1](#41-复制--借用--移动--f--m已落地)）。
- **`CALLRET` 与返回路径分析的合流** → 已接入，含 `CALLRET` 的路径视为已终结；非 `VOID` 的 `SUB` 里透传被调方的返回值（见 [1.6](#16-callret以回调替代-return-的控制流出口已落地)）。
- **`PTR TO SUB` 在 FFI 上是什么** → 边界上就是 C 函数指针，原样传递；内部表示允许以后加胖，转换点只有一处（见 [1.2](#12-函数指针-ptr-to-sub已落地)）。

---

## 十、和现有实现的衔接与建议落地顺序

粗排落地顺序（沿用 TODO 的 P 级习惯），✅ 表示已进编译器：

- **P0（地基，别的都依赖它）**：[作用域模型](#三作用域模型) 与 [Scope Exchanger](#62-sa-scope-exchanger跨域能力转换引擎方向已定)（REAL/VIRTUAL 二分 + Scope Barrier + 转换规则）。它决定了函数身份，是扩展模型和函数指针的前提。
- **P1（函数与资源）**：✅ [`= / f= / m=`](#41-复制--借用--移动--f--m已落地)（连同 `RETURN` 的所有权交接）先于这条链落地了。✅ [`PTR TO SUB`](#12-函数指针-ptr-to-sub已落地) → ✅ [`NEW SUB FROM`](#13-new-sub--from-ptr生成局部-callable-实体已落地)，已接进 4.1 的所有权检查。剩下 [托管 HANDLE 与 Handle Switcher](#61-sa-handle-switcher资源权属调度与死锁恢复方向已定)。第 6 章的三笔账里，“没有函数指针 / 回调”两笔已销；“所有权转换”SA 侧有了，C FFI 的字符串所有权转换是另一回事，未动。
- **P1（扩展）**：[`EXTEND` + `BEFORE`/`AFTER`/`FINALLY`](#21-extend函数执行结构继承方向已定) → [`TAG` / `::tag`](#22-tag-扩展点与禁止隔代打祖宗方向已定)。
- **P2（并发与异步）**：✅ [`ASYNC`/`AWAIT`/`SYNC`/`PROMISE`](#五异步模型)（C/native）。顺序和原计划反了——异步先于托管回调落地，事件循环已经在 runtime 里；之后 [`CALLRET`](#16-callret以回调替代-return-的控制流出口已落地) ✅ 和 [托管回调](#15-托管回调部分落地) 的 GUI 部分 ✅ 也进来了，GUI 回调复用了现有事件队列；timer / 网络事件回调、async 事件循环与 GUI 队列合并还没做。剩下 [`SYS.MULTIPROCESS[WORKER]`](#63-sysmultiprocess进程执行上下文扩展方向已定) 与 native 的部分数组/聚合 ABI 扩展。
- **P2（体验）**：[SA lint / 自动行号](#71-sa-lint-与自动行号部分落地)（✅ 工具已有，混写规则未定）、[SA Traceback](#72-sa-traceback部分落地)（✅ 编译期，运行期未动）。可与主线并行推进。
- **P3（深度探索）**：[`SYS.MULTIPROCESS[FORK]`](#2-multiprocessfork进程上下文分裂细节待定) 规范化。
