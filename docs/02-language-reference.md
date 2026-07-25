### SonAlgebraic 关键字与保留字总览

这份列表是 SonAlgebraic 语言的参考。

#### **1. 程序结构与流程控制 (Program Structure & Flow Control)**

| 关键字 | 用途 | 示例 |
| :--- | :--- | :--- |
| **SUB** / **.ENDSUB** | 定义一个子程序（过程或函数）的代码块。 | `100 SUB _myRoutine`<br>`110 ...`<br>`120 .ENDSUB` |
| **CALL** | 调用一个子程序。 | `200 CALL _myRoutine` |
| **RETURN** | 从一个函数 (`SUB`) 中返回一个值。 | `150 RETURN result` |
| **IF** / **THEN** / **END IF** / **.ENDIF**| 执行条件逻辑判断；两种结束写法同义。 | `100 IF x > 0 THEN`<br>`110   PRINT "Positive"`<br>`120 .ENDIF` |
| **GOTO** | 无条件跳转到指定的标签。 | `GOTO ::myLabel` |
| **GOSUB** | 跳转到指定的标签并执行，直到遇到 `RETURN`。 | `GOSUB ::mySubroutine` |
| **END** | 标志着程序的执行终点。 | `999 END` |
| **::<label_name>** | 定义一个代码标签，用作 `GOTO` 或 `GOSUB` 的跳转目标。 | `::myLabel`<br>`PRINT "Here"` |

#### **2. 数据声明与类型 (Data Declaration & Types)**

| 关键字 | 用途 | 示例 |
| :--- | :--- | :--- |
| **DIM** | 声明一个或多个**变量** (VARiable)。 | `10 DIM counter AS NUM AS LONG AS VAR` |
| **CONST** | 声明一个**常量** (CONSTant)。 | `20 CONST PI AS NUM AS DOUBLE = 3.14` |
| **VAR** | 在 `DIM` 语句中，指定声明的实体是变量。 | `DIM name AS STRING AS VAR` |
| **NUM** | 声明为数值类型，可细分为 `LONG`, `DOUBLE`, `FLOAT`。 | `DIM price AS NUM AS DOUBLE` |
| **STRING** | 声明为字符串类型。 | `DIM message AS STRING` |
| **SYMBOL** | 声明为代数符号类型，用于存储数学表达式。 | `DIM expr AS SYMBOL` |
| **ERROR** | 声明为错误对象类型，用于异常处理。 | `DIM e AS ERROR AS VAR` |
| **HANDLE AS Kind** | 声明名义化原生资源句柄；不同 kind 不可混用。 | `DIM file AS HANDLE AS FILE AS VAR` |

#### **3. 子程序修饰符 (Subroutine Modifiers)**

| 关键字 | 用途 | 示例 |
| :--- | :--- | :--- |
| **PUBLIC** | 将 `SUB` 标记为公共的，可在任何地方调用。 | `SUB myApi AS PUBLIC` |
| **PRIVATE** | 将 `SUB` 标记为私有的，仅限当前文件内调用（默认）。 | `SUB _helper AS PRIVATE` |
| **VOID** | 明确指出一个 `SUB` 是过程，没有返回值。 | `SUB display AS VOID` |

#### **4. 表达式与类型转换 (Expressions & Type Casting)**

| 关键字/语法 | 用途 | 示例 |
| :--- | :--- | :--- |
| **NUMBER()** | 内置函数，将字符串转换为数值 (`NUM`) 类型。 | `myNum = NUMBER("123.45")` |
| **STRING()** | 内置函数，将任意类型的数据转换为字符串 (`STRING`)。 | `myStr = STRING(myNum)` |
| **F"..."** | F-String 语法前缀，用于创建格式化字符串。 | `PRINT F"User: {name}, Score: {score}"` |

#### **5. I/O 与系统交互 (I/O & System Interaction)**

| 关键字 | 用途 | 示例 |
| :--- | :--- | :--- |
| **USE** | 加载一个系统模块。 | `USE SYS.IO AS IO` |
| **PRINT** | 将文本或表达式的值输出到屏幕。 | `PRINT "Hello, World!"` |
| **IO.INPUT** | 从用户处获取输入（需先 `USE SYS.IO`）。 | `IO.INPUT "Enter name: ", nameVar` |
| **CLS** | 清除屏幕内容。 | `10 CLS` |
| **REM** | 标记该行剩余部分为注释 (REMark)，可独占一行，也可跟在语句之后。 | `10 REM 整行注释`<br>`20 x = 1 REM 行尾注释` |
| **SYS.BINARY** | 二进制 BUFFER、HEX、大小端打包和校验和。 | `packet = B.HEX_DECODE(hex)` |
| **SYS.NET** | HTTP/HTTPS、DNS、TCP/TLS/UDP 和 BUFFER 收发。 | `stream = N.TCP_CONNECT(host, port, 5000)` |
| **SYS.FILE** | 文件句柄、文本文件和路径操作。 | `file = F.OPEN(path, "READ")` |
| **SYS.DESKTOP** | 消息框、系统打开和文本剪贴板。 | `ok = D.CLIPBOARD_SET(text)` |

#### **6. 结构化异常处理 (Structured Error Handling)**

| 关键字 | 用途 | 示例 |
| :--- | :--- | :--- |
| **TRY** | 开始一个受监控的代码块，必须后跟一个 `CALL` 语句。 | `TRY CALL _riskyFunction ...` |
| **TRACEBACK** | `TRY` 语句的一部分，用于指定存储错误信息的容器。 | `... TRACEBACK ERROR AS errorInfo` |
| **CATCH** | 在 `TRY` 块内，定义一个用于捕获特定类型错误的分支。 | `CATCH ERR_DIV_ZERO AS e` |
| **THROW** | 主动抛出一个新的错误，或重新抛出一个已捕获的错误。 | `THROW NEW ERR_GENERIC, "Fail!"`<br>`THROW e` |
| **NEW** | 与 `THROW` 配合，用于创建一个新的 `ERROR` 对象实例。 | `THROW NEW ERR_GENERIC, "..."` |
| **.ENDTRY** | 结束一个 `TRY...CATCH` 代码块。 | `.ENDTRY` |

---

### **特别说明: 万能关键字 `AS`**

`AS` 是 SonAlgebraic 的语法签名，其在不同上下文中扮演着统一的“被视为”或“作为”的角色。

1.  **主类型声明**: `DIM name AS STRING`
2.  **子类型声明**: `DIM price AS NUM AS DOUBLE`
3.  **属性/可见性**: `SUB main AS PUBLIC`
4.  **返回类型**: `SUB getName AS STRING`
5.  **模块别名**: `USE SYS.IO AS IO`
6.  **错误容器指定**: `TRACEBACK ERROR AS trap`
7.  **错误别名捕获**: `CATCH ERR_ANY AS e`

---

### **已废弃的关键字 (Obsolete Keywords)**

以下关键字在语言的早期版本中存在，现已被更优的语法结构取代。

| 关键字 | 替代方案 |
| :--- | :--- |
| **LET** | 直接赋值，例如 `x = 10` |
| **DIM ... AS VAL** | 使用 `CONST` 关键字，例如 `CONST PI AS NUM = 3.14` |
