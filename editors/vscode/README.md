# SonAlgebraic VSCode 扩展

`.sa` 源码的语法高亮（tmLanguage），覆盖行号、REM 注释、单/双引号与 F-string 插值、`::label`、块结束符、函数引用/callable、异步子程序与 Promise、`AWAIT` / `SYNC`、`f=` / `m=`、模块调用及数值字面量。

## 本地安装

把 `sonalgebraic/` 目录整个复制到 VSCode 扩展目录后重启 VSCode：

```powershell
Copy-Item -Recurse editors/vscode/sonalgebraic "$env:USERPROFILE\.vscode\extensions\sonalgebraic-0.2.1"
```

Linux / macOS：

```bash
cp -r editors/vscode/sonalgebraic ~/.vscode/extensions/sonalgebraic-0.2.1
```

之后打开任意 `.sa` 文件即可。要打包成 `.vsix` 分发的话装 `vsce` 再 `vsce package`。
