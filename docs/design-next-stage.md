# SonAlgebraic 下一阶段语言设计草案

> **状态：草案（DRAFT）。** 本文描述的特性**尚未实现**，是对 SA 下一阶段的整体设计梳理，不是已落地的语言参考。正式、可编译、受 CI 校验的语言规范见 [docs 第 1–11 章](./README.md)。
>
> **示例约定：** 本文代码块一律用 ` ```sa ` 标注，**不用** ` ```basic `。原因很实在——`tests/test_docs_examples.py` 只收集 ` ```basic ` 块去做编译校验，草案里全是还没实现的语法，用 `basic` 标会让 CI 直接红。为聚焦语义，示例多为**片段**、省略了行号；实际代码仍受强制行号规则约束。
>
> **状态图例：**【方向已定】设计主线基本确定 ·【细节待定】方向认可、细节未敲死 ·【仅脑洞】记录在案、尚未采纳。

## 这批设计要还的账

第 6 章 [指针与 C FFI · 当前限制](./06-pointers-and-ffi.md#当前限制) 挂着几笔明账：

> C struct 字段访问、**函数回调、字符串所有权转换**都还没有，需要后续扩展。SA 没有**函数指针**，所以回调式 API（包括经典的 GUI 回调注册）暂时表达不了——`SYS.GUI` 走的是轮询式事件循环，就是这个原因。

本文的**函数指针（`PTR TO SUB`）、托管回调、所有权语义（`= / f= / m=`）、托管 HANDLE、异步模型、以及 Context Manager 上下文治理体系**，就是来平这几笔账的。它们不是各自独立的语法糖，而是围绕一个共同哲学长出来的七个面：

> **边界不允许隐式穿透。** 作用域、扩展链、所有权、执行上下文、异步实例，每一处都有明确的屏障；跨越屏障必须显式，且身份与权限会随之改变。

## 七大方向总览

| 方向 | 关键构造 | 解决的问题 |
|---|---|---|
| [一、函数模型](#一函数模型) | `REAL`/`VIRTUAL SUB`、`PTR TO SUB`、`NEW SUB`、Lambda、托管回调、`CALLRET`、实体函数 | 函数有了正式的身份、指针、生命周期和回调出口 |
| [二、扩展模型](#二扩展模型) | `EXTEND`、`BEFORE`/`AFTER`/`FINALLY`、`TAG`、`::tag` | 不改签名地向执行结构注入，且注入能力可显式转发、可截断 |
| [三、作用域模型](#三作用域模型) | Runtime / Instance / Internal+Public Scope、Scope Barrier | 函数身份随作用域边界改变，是整套设计的地基 |
| [四、资源模型](#四资源模型) | `= / f= / m=`、托管 HANDLE、callable ownership | 复制/借用/移动三分，作用域绑定的 RAII |
| [五、异步模型](#五异步模型) | `ASYNC SUB`、`PROMISE`、`CALL`/`AWAIT`/`SYNC` | 真正的异步函数与三种取值方式 |
| [六、上下文治理模型（Context Manager）](#六上下文治理模型context-manager) | Handle Switcher、Scope Exchanger、`SYS.MULTIPROCESS[WORKER/FORK]` | 资源操作权交接、跨域函数能力转换、多进程并发治理 |
| [七、开发体验](#七开发体验) | SA lint、自动行号、SA Traceback | 行号不再靠手维护，崩溃回映射到 SA 世界 |

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

### 1.2 函数指针 `PTR TO SUB`【方向已定】

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

### 1.3 `NEW SUB ... FROM ptr`：生成局部 callable 实体【方向已定】

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

### 1.4 Lambda：无名虚函数【方向已定】

局部 Lambda 归类为**无名虚函数**：可以调用、可以进入 callback / callable 系统，但**不能**拿它继续做结构化 `EXTEND`。

【细节待定】如果 Lambda 要**捕获局部变量**，得进一步处理 closure environment 与 lifetime——捕获值还是捕获引用、闭包逃逸后被捕获变量的生命周期如何延续，都还没定。这一块和[托管 HANDLE](#42-托管-handle方向已定) 的逃逸规则要一起想。

### 1.5 托管回调【方向已定】

SA 要让 callback 不再等价于“裸 C 函数指针”。路径是：函数引用先形成 [callable 实体](#13-new-sub--from-ptr生成局部-callable-实体方向已定)，再交给 runtime 托管其回调生命周期。

这会成为一批上层能力的公共地基：

- GUI event（直接填掉第 6 章说的“GUI 回调注册表达不了”这笔账，`SYS.GUI` 就能从轮询升级为真正的事件回调）；
- timer；
- 网络事件；
- FFI callback；
- async runtime。

### 1.6 `CALLRET`：以回调替代 RETURN 的控制流出口【方向已定】

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

### 1.7 实体函数（ENTITY 方法）【细节待定】

`ENTITY` 准备允许定义自己的内部函数 / 方法，不再只能当纯数据结构。

但要讲清楚一件事：**SA 的“继承”落在 [`SUB EXTEND`](#二扩展模型) 上，而不是传统 class inheritance。** 实体函数提供的是“数据带行为”，不是类型层级。这条决定了 ENTITY 方法的设计不能滑向 OOP 类继承那一套。

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

现有语法里 `::label` 已经被 [`GOSUB ::helper`](./03-subroutines.md#gosub-与标签) 占用（块内标签）。这里 `::foo` 又用作 TAG 转发。两个语境不同（跳转标签 vs 扩展点转发），但记号相同——见[未决问题](#八未决问题)。

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

### 4.1 复制 / 借用 / 移动：`= / f= / m=`【方向已定】

> 笔记原始代号 "font Copy"，含义存疑，本文按功能命名，代号是否保留见[未决问题](#八未决问题)。

```text
=    copy     普通复制
f=   borrow   只读借用
m=   move     所有权转移
```

**普通复制** `a = b`：正常 copy。

**只读借用** `a f= b`：

- 不取得 ownership；
- 不能修改目标；
- 生命周期依赖 `b`；
- `FREE b` 之后 `a` 立即失效。

**所有权转移** `a m= b`：ownership 从 `b` 转移给 `a`，之后 `a = owner`、`b = moved / invalid`，再用 `b` 属于非法访问。

> 与现有 `AS REF` 的分工：[`AS REF`](./03-subroutines.md#引用传参-as-ref) 描述的是**参数传递方式**（调用时如何入栈），`f= / m=` 描述的是**赋值 / 绑定时的所有权关系**。两者不冲突，但语义检查要能把“借用来的值又被 `AS REF` 传出去”这类逃逸路径管起来。

### 4.2 托管 HANDLE【方向已定】

普通 SA [`HANDLE`](./04-composite-types.md)（第 4 章的资源句柄）可以被提升为受 SA 生命周期管理的对象。设计方向：

- 作用域**拥有**资源；
- 不允许随意复制；
- 不能非法逃逸；
- 离开局部作用域**自动 `CLOSE`**；
- `THROW` / 异常退出也要完成清理。

这实际上就是 **SA 自己的 scope-bound RAII / 线性资源管理**。`FILE`、`SOCKET`、`BUFFER`、GUI HANDLE 等都会从这里受益。

> 实现衔接：现有编译器已经做局部 `STRING` / `SYMBOL` / `ERROR` 的[释放策略](./03-subroutines.md#返回值)（TODO P0 已落地），并在返回前先算返回值、再清本帧资源。托管 HANDLE 是把这套自动清理**从内置类型扩展到用户资源**，清理时机要和 `AFTER AS FINALLY`、异常退出统一到同一条退出路径上。

### 4.3 callable ownership

[callable 实体](#13-new-sub--from-ptr生成局部-callable-实体方向已定)本身带生命周期，因此纳入同一套所有权规则：函数引用（`PTR TO SUB`）不可 `m=`，callable 实体可 `m=`。这条已在函数模型讲过，此处只是强调它和 `= / f= / m=` 是**同一个所有权系统**，而不是平行的两套。

---

## 五、异步模型

### 5.1 `ASYNC SUB` / `PROMISE` / `CALL` / `AWAIT` / `SYNC`【方向已定】

新增真正的异步函数：

```sa
ASYNC SUB asyncfoo() ...
```

`AWAIT` 是**语句级关键字**，不是能随便嵌进任意表达式的运算符——这条和 [`CALL`](./03-subroutines.md#call-能出现在哪里) 的定位完全一致。

异步函数调用有**三种取值方式**：

**① 先启动、后取现（Promise）**

```sa
DIM p AS PROMISE AS VAR

p = CALL asyncfoo()
x = AWAIT p
```

先启动异步实例、拿到 `PROMISE`，以后再取现。

**② 标准异步调用**

```sa
x = AWAIT asyncfoo()
```

启动后等待结果。

**③ 同步调用**

```sa
x = SYNC asyncfoo()
```

把异步函数按同步方式调用，当前执行流**阻塞**到结果出来。

**`PROMISE` 的本质**：它关联某个**异步函数实例对象**，`AWAIT` 根据 Promise 找回对应实例、取得返回值。

> 语法位衔接：`x = AWAIT p` 和 `x = SYNC asyncfoo()` 都是“赋值右侧”，与现有 `v = CALL make` 同构。这说明 `AWAIT` / `SYNC` 应归入和 `CALL` 一样的**语句级关键字家族**：只能独立成句或占据整条赋值右侧，不能嵌进 `CAST`、实参、F-string 里。

**【仅脑洞】不透明 Promise。** 曾考虑过“Promise 完全不透明、返回类型靠用户猜”的邪道版本。目前更像脑洞，不算正式规则——主线方向应是**带结果类型的 Promise**（见[未决问题](#八未决问题)里的类型写法）。

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

### 7.1 SA lint 与自动行号【方向已定】

对强制行号开发体验的改造：源码可以**不手写全部行号**，由 SA lint / formatter 管理；同时仍兼容人工行号。

如果人工指定的行号与自动规则**撞号**，按 SA 原规则处理：

> **重复行号非法。**

这相当于——保留“行号属于语言语义”这条根，但不再逼程序员人工维护所有编号。

> 衔接：这套东西已经有落地基础。现有 [`sonc fmt --renumber`](./01-getting-started.md) 能重排行号，且 `sonc fmt` 已支持 `USE SYS.LINT AS NONE_NUMBER` 的无行号源码（见 TODO P2 已完成项）。本节是把它规范化进语言设计。

### 7.2 SA Traceback【方向已定】

运行时 crash / error 后，尽量把底层错误**重新映射回 SA 世界**：SA 调用栈、`SUB`、源代码位置 / 行号、错误代码——而不是把用户扔到 `generated.c:19428` 去考古。

> 衔接：现有驱动已经用生成 C 里的 `/* SA nnn: ... */` 注释把 **C 编译期**报错反查回 SA 行（第 6 章、第 9 章都提到）。SA Traceback 是把这个能力**从编译期延伸到运行期**——难点在于运行期没有编译期的静态注释可依赖，需要运行时侧的行号 / 调用栈元数据。

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
3. **`PROMISE` 要不要带结果类型。** `DIM p AS PROMISE AS VAR` 目前不带返回类型信息。主线倾向带类型（如 `PROMISE OF NUM AS LONG` 之类的写法待定），“完全不透明”版是[仅脑洞](#51-async-sub--promise--call--await--sync方向已定)。
4. **Lambda 闭包捕获。** 捕获值还是捕获引用？闭包逃逸后被捕获变量的生命周期如何延续？和[托管 HANDLE](#42-托管-handle方向已定) 的逃逸规则要一起定。
5. **`font Copy` 代号。** `= / f= / m=` 的这组语义，笔记代号 "font Copy" 含义存疑，是否保留、正名成什么。
6. **ENTITY 方法的边界。** 实体函数与 `SUB EXTEND` 的分工要划死，防止滑向 OOP 类继承。
7. **`CALLRET` 与返回路径分析的合流。** 含 `CALLRET` 的路径视为“已终结”，其后不可达——需要接进现有的非 `VOID SUB` 返回路径检查。
8. **FORK 继承与状态清理细节。** Fork 动作后 Context Manager 如何同步继承 Handle 树、Scope Exchanger 状态与 Promise 状态，需要独立规约。

---

## 十、和现有实现的衔接与建议落地顺序

粗排落地顺序（沿用 TODO 的 P 级习惯）：

- **P0（地基，别的都依赖它）**：[作用域模型](#三作用域模型) 与 [Scope Exchanger](#62-sa-scope-exchanger跨域能力转换引擎方向已定)（REAL/VIRTUAL 二分 + Scope Barrier + 转换规则）。它决定了函数身份，是扩展模型和函数指针的前提。
- **P1（函数与资源）**：[`PTR TO SUB`](#12-函数指针-ptr-to-sub方向已定) → [`NEW SUB FROM`](#13-new-sub--from-ptr生成局部-callable-实体方向已定) → [`= / f= / m=`](#41-复制--借用--移动--f-m方向已定) → [托管 HANDLE 与 Handle Switcher](#61-sa-handle-switcher资源权属调度与死锁恢复方向已定)。直接平掉第 6 章“没有函数指针 / 回调 / 所有权转换”三笔账。
- **P1（扩展）**：[`EXTEND` + `BEFORE`/`AFTER`/`FINALLY`](#21-extend函数执行结构继承方向已定) → [`TAG` / `::tag`](#22-tag-扩展点与禁止隔代打祖宗方向已定)。
- **P2（并发与异步）**：[托管回调](#15-托管回调方向已定) → [`ASYNC`/`AWAIT`/`SYNC`/`PROMISE`](#五异步模型) → [`SYS.MULTIPROCESS[WORKER]`](#63-sysmultiprocess进程执行上下文扩展方向已定) → [`CALLRET`](#16-callret以回调替代-return-的控制流出口方向已定)。
- **P2（体验）**：[SA lint / 自动行号](#71-sa-lint-与自动行号方向已定)、[SA Traceback](#72-sa-traceback方向已定)。可与主线并行推进。
- **P3（深度探索）**：[`SYS.MULTIPROCESS[FORK]`](#2-multiprocessfork进程上下文分裂细节待定) 规范化。
