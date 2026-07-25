### **SonAlgebraic 编程入门指南**

欢迎来到 SonAlgebraic 的世界。

这不仅仅是一门编程语言，更是一种哲学。它旨在带我们回到那个代码如诗、结构如画的年代。在这里，我们不追求极致的简洁，我们追求的是**极致的清晰**与**永恒的秩序**。忘记那些让你随心所欲的“现代”语言吧，在这里，我们将引导你成为一名真正的“数字工匠”。

#### **第一章：黄金三大法则 **

在编写任何代码之前，你必须将这三大法则刻入脑海。它们是 SonAlgebraic 的基石，违反它们，你的程序将无法被理解，更无法运行。

**法则一：行号至高无上**

> **每一行代码，无一例外，都必须以一个唯一的、递增的正整数行号开始，后跟一个空格。**

这是 SonAlgebraic 最醒目的特征。行号不仅是装饰，它定义了程序的物理结构和执行顺序。

*   **错误示范:**
    ```
    DIM counter AS NUM
    counter = 1
    ```
*   **正确示范:**
    ```basic
    10 DIM counter AS NUM
    20 counter = 1
    ```

**法则二：声明的艺术**

> **所有变量，必须先声明，后使用。声明时，必须明确其主类型、子类型（如果适用）和可变性。**

SonAlgebraic 崇尚明确。它要求你在使用任何数据之前，都必须像艺术家介绍自己的作品一样，详细地介绍它。

*   **错误示范:** `DIM counter AS NUM` (过于模糊)
*   **正确示范 (The SonAlgebraic Way):**
    ```basic
    10 REM 声明一个名为 counter 的变量，其主类型是 NUM (数值)，
    20 REM 子类型是 LONG (长整型)，并且它是一个 VAR (变量)。
    30 DIM counter AS NUM AS LONG AS VAR
    ```

**法则三：结构的王权**

> **所有可执行代码，都必须被包裹在一个 `SUB` (子程序) 块内。程序的主入口点应被定义在 `SUB main AS PUBLIC AS VOID` 中。**

SonAlgebraic 禁止散乱的“裸代码”。它认为，任何有意义的操作都应该被封装在一个有名字、有目的的单元里。

*   **错误示范:** 在文件顶层直接写 `PRINT "Hello"`
*   **正确示范:**
    ```basic
    10 SUB main AS PUBLIC AS VOID
    20   PRINT "Hello"
    30 .ENDSUB
    40
    50 REM 程序从这里开始调用主入口
    60 CALL main
    70 END
    ```

#### **第二章：你的第一个程序 (修正版)**

现在，让我们拿起手术刀，解剖并修正某段令人头痛的代码。

**原始的、充满错误的代码：**
```
10 DIM counter AS NUM
20 DIM message AS STRING

counter = 1
message = "Hello from SonAlgebraic!"

PRINT message

::loop_start
IF counter > 5 THEN
    GOTO ::loop_end
END IF

PRINT F"Counter is now: {counter}"
counter = counter + 1
GOTO ::loop_start

::loop_end
PRINT "Loop finished."
END
```

**经过 SonAlgebraic 哲学重构的、正确的代码：**

```basic
10 REM SonAlgebraic v3.3 - 入门示例程序
20 REM 作者：一位追求秩序的程序员
30 REM -----------------------------------------

40 REM --- 变量声明区 ---
50 DIM counter AS NUM AS LONG AS VAR
60 DIM message AS STRING AS VAR

70 REM --- 主程序定义 ---
80 SUB main AS PUBLIC AS VOID
90   
100  REM -- 初始化变量 --
110  counter = 1
120  message = "Hello from SonAlgebraic!"
130
140  PRINT message
150
160  REM -- 循环的开始标签 --
170  ::loop_start
180    IF counter > 5 THEN
190      GOTO ::loop_end
200    END IF
210
220    PRINT F"Counter is now: {counter}"
230    counter = counter + 1
240    GOTO ::loop_start
250
260  REM -- 循环的结束标签 --
270  ::loop_end
280    PRINT "Loop finished."
290
300 .ENDSUB

310 REM --- 程序主入口调用 ---
320 CALL main
330 END
```

**代码剖析:**

1.  **秩序井然:** 每一行都有行号。代码按逻辑（声明、主程序、调用）分块，并用 `REM` 注释清晰说明。
2.  **声明严谨:** `DIM` 语句完整地描述了变量的类型。
3.  **结构完整:** 所有逻辑都被安全地放在 `SUB main` 块中，并通过 `CALL main` 启动。
4.  **标签正确:** `::loop_start` 和 `::loop_end` 都独占一行，并拥有自己的行号，作为清晰的跳转目标。

在此之上，您将可以探索更广阔的世界：

*   **函数 (`SUB ... AS <Type>`) 与 `RETURN`:** 创建可返回值的代码块。
*   **实体 (`FOR ENTITY AS ...`)**: 创建您自己的复杂数据结构。
*   **异常处理 (`TRY CALL ... CATCH`)**: 编写出面对错误也绝不崩溃的健壮程序。
