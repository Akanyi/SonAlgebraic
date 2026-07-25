# SonAlgebraic 文档

SonAlgebraic 的语言文档，按学习顺序分章节编排。工具链使用、CLI 命令和特性概览请看仓库根目录的 [README](../README.md)。

## 章节目录

1. [入门指南](./01-getting-started.md) — 黄金三大法则、第一个程序和代码剖析。
2. [语言参考](./02-language-reference.md) — 关键字、保留字、类型和 `AS` 的统一语义。
3. [参数与实体](./03-params-and-entities.md) — 传参语法（含 `AS REF`）和 `ENTITY` 结构体。
4. [进阶语义](./04-advanced-semantics.md) — `SYMBOL`、`ERROR`、`TRY/CATCH/THROW`、`GOSUB` 和非 `VOID` 返回值函数。
5. [模块系统](./05-module-system.md) — `USE` 模块加载、命名空间抹平和模块导出。
6. [.spkg 格式规范](./06-spkg-format.md) — 多模块包的 zip 结构、manifest 和 artifact 约定。
7. [.slib 格式规范](./07-slib-format.md) — 单模块库包的 zip 结构、三种打包形态和命名规则。
8. [语言特性扩展](./08-language-extensions.md) — ELSE/循环/数组/BOOL/NULL/位运算/字符串操作/枚举/SYMBOL 代数等现代特性。

> 包格式上，`.slib` 是单模块库包（源码/静态库/动态库三态），`.spkg` 是面向分发的多模块自包含包，可视为多个 `.slib` 的聚合。

## 阅读建议

- 第一次接触先读第 1、2 章建立语法直觉。
- 写实际程序时按需查第 3、4 章的语义细节。
- 拆分模块或发布库时再看第 5、6、7 章。
