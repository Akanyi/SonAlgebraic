# SonAlgebraic 文档

语言文档，按主题分章。工具链安装、CLI 命令、特性概览和当前限制请看仓库根目录的 [README](../README.md)。

## 章节

| # | 章节 | 内容 |
|---|---|---|
| 1 | [入门](./01-getting-started.md) | 第一个能跑的程序，三条硬规则，逐行剖析 |
| 2 | [语言基础](./02-language-basics.md) | 行号、`DIM` / `CONST`、类型系统、字面量、F-string、运算符、控制流、关键字总表 |
| 3 | [子程序](./03-subroutines.md) | `SUB`、参数、`AS REF`、返回值、可见性、`GOSUB` 与标签 |
| 4 | [复合类型](./04-composite-types.md) | `ENTITY`、`ENUM`、定长数组、`HANDLE` 资源句柄 |
| 5 | [错误处理与符号代数](./05-errors-and-symbols.md) | `ERROR`、`TRY` / `CATCH` / `THROW`、`SYMBOL` 求导与化简 |
| 6 | [指针与 C FFI](./06-pointers-and-ffi.md) | `CPTR`、`PTR TO T`、`@`、`^`、`CAST`、`USEC` / `USELIB` / `DECLARE C` |
| 7 | [模块系统](./07-modules.md) | `USE`、内置模块与用户模块的区别、解析顺序、导出规则 |
| 8 | [标准库](./08-stdlib.md) | 11 个 `SYS.*` 模块的完整 API |
| 9 | [实现说明](./09-implementation-notes.md) | 生成的 C 长什么样：命名映射、资源清理、异常、`GOSUB` |
| 10 | [.slib 格式规范](./10-slib-format.md) | 单模块库包：zip 布局、manifest、三种打包形态 |
| 11 | [.spkg 格式规范](./11-spkg-format.md) | 多模块自包含包：manifest、解包安全、构建时选择 |

## 怎么读

**第一次接触**：第 1 章跑通 hello，然后第 2 章建立语法直觉。这两章读完就能写东西了。

**写程序时按需查**：数据结构翻第 3、4 章，异常和符号代数翻第 5 章，标准库 API 翻第 8 章。

**跨语言边界**：调 C 函数或需要指针，看第 6 章。它也是 `CAST` 的唯一出处。

**拆分和发布**：第 7 章讲模块，第 10、11 章是两种包格式的规范。`.slib` 是单模块库包（源码 / 静态库 / 动态库三态），`.spkg` 是面向分发的多模块自包含包，可以视为多个 `.slib` 的聚合。

**想知道底下发生了什么**：第 9 章。排查 C 编译阶段的报错、给编译器提交改动，或者写要和 SA 产物链接的 C 代码时用得上。

## 关于文档里的示例

所有 ` ```basic ` 代码块都是**完整可编译**的程序，可以直接复制运行。`tests/test_docs_examples.py` 会把它们逐个过一遍语义检查，改语法忘了同步文档时 CI 会红。

少数刻意演示错误写法、或本身是模块片段（不含 `SUB main`）的代码块，上方带 `<!-- doctest: skip 原因 -->` 标记——这行 HTML 注释在 GitHub 渲染时不可见。
