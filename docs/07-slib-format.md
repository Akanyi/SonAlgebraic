# SonAlgebraic Library Format (.slib)

版本：2（向后兼容版本 1）

`.slib` 是 SonAlgebraic 的单模块库包格式。一个 `.slib` 文件是一个 zip 压缩包，内含一个根模块（及其递归依赖的用户模块）的 SA 源码副本、生成的 C 源码与头文件、可选的目标平台静态库或动态库，以及描述这些内容的 `manifest.json`。

与多模块、面向分发的 [`.spkg`](./06-spkg-format.md) 相比，`.slib` 更轻量，定位是「单个模块编译产物的打包单元」。`.spkg` 在概念上可以视为多个 `.slib` 的聚合。

## 目录

- [设计目标](#设计目标)
- [三种打包形态](#三种打包形态)
- [文件布局](#文件布局)
- [manifest.json](#manifestjson)
  - [顶层字段](#顶层字段)
  - [units](#units)
  - [archives](#archives)
- [文件命名规则](#文件命名规则)
- [加载与链接逻辑](#加载与链接逻辑)
- [打包命令](#打包命令)
- [版本与兼容性](#版本与兼容性)

## 设计目标

- **单模块聚焦**：一个 `.slib` 对应一个根模块；根模块通过 `USE` 引用的其他用户模块会一并打进同一个包。
- **源码与二进制可选**：可以只发源码、发静态库，或发动态库。
- **自带 C 产物**：源码形态也预先生成好 C 源码和头文件，引用方无需重新跑代码生成即可参与本机 C 编译链接。
- **跨平台二进制**：静态/动态形态按 `--target` 归一化命名，可交叉编译后打入对应 target 目录。

## 三种打包形态

`manifest.json` 的 `kind` 字段标识打包形态，三者互斥：

| kind | 触发方式 | 包内额外产物 | 引用方行为 |
|---|---|---|---|
| `source` | 默认 | 无 | 解包后编译包内 `c/*.c` 一起链接 |
| `static` | `--binary` | `lib/<target>/*.a` | 命中 target 时链接 `.a`，跳过根模块 `.c` 编译 |
| `dynamic` | `--dynamic` | `lib/<target>/` 下 DLL/SO/dylib（Windows 另含 import lib） | 命中 target 时链接动态库，运行时需库与 exe 同目录 |

`--binary` 和 `--dynamic` 不能同时使用。

## 文件布局

```text
statslib.slib (zip)
├── manifest.json
├── sa_runtime.h                       # runtime 头文件副本
├── sources/
│   ├── sa_user_statslib.sa            # 根模块 STATSLIB 源码副本
│   └── sa_user_statslib_util.sa       # 依赖子模块 STATSLIB.UTIL（如有）
├── c/
│   ├── sa_user_statslib.c             # 生成的模块 C 源码
│   └── sa_user_statslib_util.c
├── include/
│   ├── sa_user_statslib.h             # 生成的模块头文件
│   └── sa_user_statslib_util.h
└── lib/                               # 仅 static / dynamic 形态存在
    └── x86_64-windows-gnu/
        ├── libsa_user_statslib_x86_64_windows_gnu.a        # static
        │                                                   # 或 dynamic:
        ├── sa_user_statslib.dll
        └── libsa_user_statslib_x86_64_windows_gnu.dll.a    # Windows import lib
```

`source` 形态没有 `lib/` 目录。二进制形态的 `lib/<target>/` 当前只打根模块对应的库，非根的依赖子模块仍以 `c/` 源码参与链接。

## manifest.json

### 顶层字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `format` | string | 固定值 `"sonalgebraic-slib"` |
| `version` | integer | 格式版本，当前为 `2`；加载时兼容 `1` |
| `root_module` | string | 根模块名（大写），如 `"STATSLIB"` |
| `kind` | string | `"source"` / `"static"` / `"dynamic"` |
| `target` | string \| null | 二进制形态的归一化 target；`source` 形态为 `null` |
| `units` | array | 包内所有模块单元的清单 |
| `archives` | object | target → 二进制条目映射；`source` 形态为空对象 |

### units

`units` 数组每个元素描述一个模块单元，路径均为 zip 内相对路径：

```json
{
  "module": "STATSLIB",
  "source_entry": "sources/sa_user_statslib.sa",
  "c_entry": "c/sa_user_statslib.c",
  "h_entry": "include/sa_user_statslib.h"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `module` | string | 模块名（大写） |
| `source_entry` | string | SA 源码在包内的路径 |
| `c_entry` | string | 生成的 C 源码路径 |
| `h_entry` | string | 生成的头文件路径 |

### archives

`archives` 按 target 组织，仅二进制形态存在。`kind` 与顶层 `kind` 一致。

静态库（`kind=static`）：

```json
{
  "x86_64-windows-gnu": {
    "kind": "static",
    "entry": "lib/x86_64-windows-gnu/libsa_user_statslib_x86_64_windows_gnu.a"
  }
}
```

动态库（`kind=dynamic`）：

```json
{
  "x86_64-windows-gnu": {
    "kind": "dynamic",
    "dll": "lib/x86_64-windows-gnu/sa_user_statslib.dll",
    "import_lib": "lib/x86_64-windows-gnu/libsa_user_statslib_x86_64_windows_gnu.dll.a"
  }
}
```

- `dynamic` 在 Linux/macOS 下用 `dll` 字段承载 `.so` / `.dylib`（字段名固定为 `dll`），且无 `import_lib`。
- Windows 动态库必须同时含 `dll` 与 `import_lib`：链接传 import lib，运行时加载 `.dll`。

## 文件命名规则

命名由模块名经 `module_c_name`（小写、`.` 换 `_`）和归一化 target（小写、`-` 换 `_`）推导：

| 产物 | 规则 | 示例（模块 `STATSLIB`，target `x86_64-windows-gnu`） |
|---|---|---|
| 源码副本 | `<module_c_name>.sa` | `sa_user_statslib.sa` |
| C 源码 | `sa_user_<module_c_name>.c` | `sa_user_statslib.c` |
| 头文件 | `sa_user_<module_c_name>.h` | `sa_user_statslib.h` |
| 静态库 | `libsa_user_<module_c_name>_<target_>.a` | `libsa_user_statslib_x86_64_windows_gnu.a` |
| 动态库(Win) | `sa_user_<module_c_name>.dll` | `sa_user_statslib.dll` |
| 动态库(Linux) | `libsa_user_<module_c_name>.so` | `libsa_user_statslib.so` |
| 动态库(macOS) | `libsa_user_<module_c_name>.dylib` | `libsa_user_statslib.dylib` |
| import lib(Win) | `libsa_user_<module_c_name>_<target_>.dll.a` | `libsa_user_statslib_x86_64_windows_gnu.dll.a` |

target 归一化：未指定时取本机（如 Windows → `x86_64-windows-gnu`，Linux → `x86_64-linux-gnu`，macOS → `x86_64-macos`）。

## 加载与链接逻辑

引用方在 `USE` 解析时找到 `.slib`，按以下顺序处理：

1. 读取 `manifest.json`，校验 `format` 与 `version`（仅接受 `1` / `2`）。
2. 逐个解包 `units`：写出源码副本和头文件；解析源码收集导出符号（`PUBLIC SUB` / `CONST` / `ENTITY`）。
3. 对**根模块**，若 `archives` 命中当前 `--target`：
   - 跳过根模块 `c_entry` 的解包与编译，改为解包对应静态/动态库。
   - `static`：链接 `.a`。
   - `dynamic`：解包 DLL/SO/dylib（Windows 额外解包 import lib 并以其作为链接库），运行时由 `compiler` 把动态库复制到 exe 输出目录。
4. 对**非根的依赖单元**，始终解包 `c_entry` 以源码形式参与链接。
5. 模块内部声明的 `USELIB` 会被记录到 `link_libs`，递归汇总进最终链接命令。

target 未命中或为 `source` 形态时，全部模块走 C 源码编译路径。

## 打包命令

```powershell
# 源码包
python -m sonalgebraic slib examples/statslib.sa -o examples/statslib.slib

# 静态库
python -m sonalgebraic slib examples/statslib.sa -o build/statslib_binary.slib --binary

# 动态库（Windows DLL + import lib / Linux .so / macOS .dylib）
python -m sonalgebraic slib examples/statslib.sa -o build/statslib_dynamic.slib --dynamic

# 交叉编译（需 zig）
python -m sonalgebraic slib examples/statslib.sa -o build/statslib_linux.slib --binary --target x86_64-linux-gnu
```

引用方无需特殊参数，把 `.slib` 与引用它的 `.sa` 放在 `USE` 能解析到的目录即可（参见[模块系统](./05-module-system.md)）。

## 版本与兼容性

- 当前格式版本为 `2`，加载器同时接受版本 `1`。
- `.slib` 不是 ABI 固定的预编译静态库分发格式：`source` 形态最终仍由消费者本机 C 编译器链接。
- 二进制形态（`static` / `dynamic`）的库必须与消费者指定的 `--target` 匹配，否则会 fallback 到源码（若包内有源码）或报错。
- 非本机 `--target` 的二进制打包依赖 `zig`。
