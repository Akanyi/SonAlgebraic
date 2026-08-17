# Inno Setup 语言文件

`ChineseSimplified.isl` 不在 Inno Setup 的官方发行包里（官方只带 29 种语言，简体中文属于
第三方翻译），所以放进仓库自带，免得换台机器构建就报 "Couldn't open include file"。

- 来源：<https://github.com/kira-96/Inno-Setup-Chinese-Simplified-Translation>（Inno Setup 官网
  Third-Party Files 页面推荐的简体中文翻译）
- 入库前做了两处规范化：
  - 原文件是 UTF-8 内容却写着 `LanguageCodePage=936` 且不带 BOM，Inno 会拿 GBK 去解 UTF-8
    字节，整个向导变乱码。改成加 UTF-8 BOM + `LanguageCodePage=0`。
  - `LanguageName` 转成 `<XXXX>` 形式的 Unicode 转义，这是官方 .isl 的写法——语言选择对话框
    弹出时编码还没确定，直接放字符会显示成问号。

更新翻译时重新下载一遍，然后照上面两条再处理一次。
