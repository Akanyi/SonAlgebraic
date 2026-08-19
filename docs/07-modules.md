# 模块系统

`USE` 是 SonAlgebraic 唯一的模块加载方式。本章覆盖内置模块与用户模块的区别、解析顺序、导出规则和分离编译。C 头文件的引入（`USEC` / `USELIB`）属于 FFI，见[第 6 章](./06-pointers-and-ffi.md#c-ffi-声明)。

## 目录

- [USE 语法](#use-语法)
- [两类模块](#两类模块)
- [模块解析顺序](#模块解析顺序)
- [命名空间是强制的](#命名空间是强制的)
- [编写可被引用的模块](#编写可被引用的模块)
- [导出规则](#导出规则)
- [循环依赖](#循环依赖)
- [链接依赖的递归汇总](#链接依赖的递归汇总)
- [打包分发](#打包分发)

## USE 语法

```
USE <模块路径> AS <别名>
```

模块路径用点号分层。`USE` 必须写在顶层声明区，和其他语句一样带行号。

```basic
10 USE SYS.MATH AS M
20 USE SYS.STRING AS S
30 DIM area AS NUM AS DOUBLE AS VAR
40 SUB main AS PUBLIC AS VOID
50 area = M.PI * M.POW(2.0, 2.0)
60 PRINT F"area={area} tag_len={S.LENGTH("circle")}"
70 .ENDSUB
80 CALL main
90 END
```

别名可以任取。`USE SYS.MATH AS WHATEVER` 之后就用 `WHATEVER.PI`。

`USE` 本身不产生任何运行时开销——SA 禁止模块级裸代码，所以加载模块不会隐式执行任何初始化。一切逻辑只由显式的 `CALL` 触发。

## 两类模块

这是理解模块系统的关键分界，两类的编译方式**完全不同**：

| | 内置 `SYS.*` | 用户模块 |
|---|---|---|
| 来源 | 编译器内建 | 你写的 `.sa` 文件 / `.slib` / `.spkg` |
| 编译方式 | 编译期直接 lowering | 分离编译成独立的 C 文件 |
| 产物 | 无独立符号，无头文件 | `sa_user_<模块>.h` + `sa_user_<模块>.c` |
| 符号命名 | 直接落到 runtime 函数或 C 标准库 | `sa_mod_<模块>_*` 前缀 |

内置模块共 11 个：`SYS.MATH`、`SYS.IO`、`SYS.STRING`、`SYS.NET`、`SYS.FILE`、`SYS.DESKTOP`、`SYS.BINARY`、`SYS.LIST`、`SYS.MAP`、`SYS.GUI`、`SYS.LINT`。它们的 API 见[第 8 章](./08-stdlib.md)。

内置模块的调用被编译期直接翻译掉。`M.PI` 变成字面量，`M.POW(x, y)` 变成 C 的 `pow(x, y)`，`S.LENGTH(s)` 变成 runtime 的 `sa_str_length(s)`。**不会**生成 `sa_sys_math.h` 这类头文件，别名只活在编译期的符号表里。

用户模块才走真正的分离编译，生成的头文件和 `sa_mod_*` 符号是稳定接口——反向 FFI（C 程序调用 SA 编译出的动态库）依赖它。细节见[第 9 章](./09-implementation-notes.md#模块的两条不同路径)。

## 模块解析顺序

`USE STATSLIB AS ST` 按以下顺序查找：

1. **当前源码目录下的 `.sa` 文件**
   - `USE MATHLIB AS LIB` → 找 `mathlib.sa`
   - `USE DATA.MODELS AS DATA` → 优先 `data/models.sa`，回退 `data_models.sa`
2. **同名 `.slib` 包**——如 `mathlib.slib` 或 `data/models.slib`
3. **`--pkg` 传入的 `.spkg` 包内模块**——按包的 `module_to_package` 映射查找

模块名大小写不敏感，文件名按小写查找。

## 命名空间是强制的

`USE SYS.MATH AS M` 之后，该文件内**绝对不能**直接写 `POW(...)`，必须写 `M.POW(...)`。

这条规则没有例外，好处是多个模块有同名函数时永远不会冲突，读代码时也一眼能看出某个调用来自哪里。唯一不需要前缀的是语言内置的 `NUMBER()` / `STRING()` 和 `SYMBOL` 的四个代数函数。

## 编写可被引用的模块

被引用的模块和普通程序结构一样，只是**不定义 `SUB main`**，也没有顶层的 `CALL main` / `END`：

<!-- doctest: skip 模块文件不含 SUB main，单独检查会报缺少入口 -->
```basic
10 CONST SCALE AS NUM AS DOUBLE = 2.5
20 SUB twice(value AS NUM AS DOUBLE) AS PUBLIC AS NUM AS DOUBLE
30 RETURN value * SCALE
40 .ENDSUB
```

存成 `mathlib.sa`，旁边的程序就能用：

<!-- doctest: skip 依赖同目录的 mathlib.sa，单文件检查解析不到该模块 -->
```basic
10 USE MATHLIB AS LIB
20 DIM answer AS NUM AS DOUBLE AS VAR
30 SUB main AS PUBLIC AS VOID
40 answer = LIB.SCALE + LIB.twice(4.0)
50 PRINT answer
60 .ENDSUB
70 CALL main
80 END
```

这一对就是仓库里的 `examples/mathlib.sa` 和 `examples/use_user_module.sa`，可以直接跑：

```powershell
python -m sonalgebraic run examples/use_user_module.sa
```

注意 `SUB twice(...) AS PUBLIC AS NUM AS DOUBLE` 的修饰符顺序：参数列表在前，然后是可见性，最后是返回类型。

## 导出规则

模块里只有标记为 `AS PUBLIC` 的东西对外可见：

| 声明 | 导出结果 |
|---|---|
| `SUB ... AS PUBLIC` | 进模块头文件，外部可调用 |
| `CONST` | 导出为 `extern` 常量 |
| `FOR ENTITY` | 导出为 C `typedef struct` |
| `SUB ... AS PRIVATE` 或不写 | 模块私有 |
| `DIM` 全局变量 | 模块私有，**不导出** |

当前的跨模块导出是最小可用集：模块级可见性控制和更复杂的跨模块 `ENTITY` ABI 生命周期还没做。

## 循环依赖

递归加载时检测，`A -> B -> A` 会直接报出完整链路而不是栈溢出：

```text
模块循环依赖: A -> B -> A
```

## 链接依赖的递归汇总

模块内部写的 `USELIB` 会被递归汇总到最终链接命令里。这样 FFI 依赖只声明在模块中时不会被漏掉——引用方不需要知道被引用模块链接了什么库。

出于安全考虑，`USELIB` 的值会进入 C 编译器命令行，而它可能来自第三方包的源码，所以只接受纯库名（字母数字和 `_ . + -`）以及不以 `-` 开头的库文件路径。`USELIB "-fplugin=./evil.so"` 这种会被当成编译器选项、在构建期加载任意插件的写法直接报错。

## 打包分发

模块可以打包成两种格式分发：

| 格式 | 定位 | 规范 |
|---|---|---|
| `.slib` | 单模块库包，根模块 + 其递归依赖的用户模块 | [第 10 章](./10-slib-format.md) |
| `.spkg` | 多模块自包含包，可视为多个 `.slib` 的聚合 | [第 11 章](./11-spkg-format.md) |

两者都是 zip 包，内含 `manifest.json`、SA 源码副本、生成的 C 产物，以及可选的目标平台静态/动态库。打包命令见根 [README](../README.md#打包-slib) 的 CLI 章节。

两种格式解包时都会校验 `manifest.json` 里 `hashes` 声明的 sha256，并做反查——实际参与编译的每个源文件都必须被 `hashes` 覆盖，省掉条目或把 `hashes` 留空都不能绕过。`.slib` 从格式版本 3 起要求 `hashes` 必填，v1 / v2 存量包会警告跳过校验。

`.spkg` 另有一组解包安全检查：拒绝绝对路径 / `..` / 冒号 / 反斜杠绕过（zip 路径穿越），拒绝 Windows 保留设备名（`CON` / `NUL` / `COM1`，含带扩展名的 `NUL.sa`）。

> 两种包都**没有签名机制**。hash 清单挡得住换掉某个成员这类局部篡改和传输损坏，挡不住整份 manifest 被重写。引用来源不明的包之前请自行确认。
