# SonAlgebraic Package Format (.spkg)

版本：1

`.spkg` 是 SonAlgebraic 的多模块、自包含包格式。一个 `.spkg` 文件是一个 zip 压缩包，内含一个或多个 SonAlgebraic 模块、可选的预编译二进制、可选的原生 C 库依赖，以及描述这些内容的 `manifest.json`。

## 目录

- [设计目标](#设计目标)
- [文件布局](#文件布局)
- [manifest.json](#manifestjson)
  - [顶层字段](#顶层字段)
  - [package](#package)
  - [bundled_packages](#bundled_packages)
  - [modules](#modules)
  - [module_to_package](#module_to_package)
  - [dependency_graph](#dependency_graph)
  - [hashes](#hashes)
- [模块命名](#模块命名)
- [解包安全](#解包安全)
- [构建时选择逻辑](#构建时选择逻辑)
- [FFI 与原生库](#ffi-与原生库)
- [最小示例](#最小示例)
- [版本与兼容性](#版本与兼容性)

## 设计目标

- **自包含**：发布者把全部依赖 bundle 进一个文件，消费者不需要联网解析。
- **源码与二进制共存**：可以只发源码、只发二进制，或两者都发。
- **多 target 支持**：同一个包可以同时包含 Windows/Linux/macOS、x86_64/aarch64 的预编译产物。
- **与 `.slib` 兼容**：`.spkg` 可以视为多个 `.slib` 的聚合，旧工具链可以逐步迁移。

## 文件布局

```text
mylib.spkg (zip)
├── manifest.json
└── packages/
    ├── mylib/                         # 根包（is_root=true）
    │   ├── src/
    │   │   ├── __init__.sa            # 模块 MYLIB
    │   │   └── math.sa                # 模块 MYLIB.MATH
    │   ├── generated/
    │   │   ├── sa_user_mylib.c
    │   │   ├── sa_user_mylib.h
    │   │   ├── sa_user_mylib_math.c
    │   │   └── sa_user_mylib_math.h
    │   ├── binaries/
    │   │   ├── x86_64-windows-gnu/
    │   │   │   ├── libmylib.dll
    │   │   │   ├── libmylib.dll.a
    │   │   │   ├── libmylib_math.dll
    │   │   │   └── libmylib_math.dll.a
    │   │   └── x86_64-linux-gnu/
    │   │       ├── libmylib.so
    │   │       └── libmylib_math.so
    │   └── native/                    # 可选：包依赖的 C 原生库
    │       └── x86_64-linux-gnu/
    │           └── libcurl.so
    └── base/                          # 被 bundle 进来的依赖
        ├── src/
        │   ├── __init__.sa            # 模块 BASE
        │   └── io.sa                  # 模块 BASE.IO
        ├── generated/
        │   └── ...
        └── binaries/
            └── ...
```

## manifest.json

### 顶层字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `format` | string | 固定值 `"sonalgebraic-spkg"` |
| `version` | integer | 格式版本，当前为 `1` |
| `package` | object | 根包元数据 |
| `bundled_packages` | array | 所有被 bundle 的包，包含根包和依赖 |
| `modules` | array | 包内所有模块的详细信息 |
| `module_to_package` | object | 模块名到包名的映射 |
| `dependency_graph` | object | 包之间的依赖关系，仅作信息/校验用 |
| `hashes` | object | 关键文件的 sha256 校验值 |

### package

```json
{
  "name": "mylib",
  "version": "1.2.3",
  "description": "Standard utilities",
  "author": "LANS",
  "license": "MIT"
}
```

`sonc pack` 当前生成的就是这五个字段，未提供时 `version` 默认 `"0.1.0"`，其余为空串。

### bundled_packages

每个元素描述一个包：

```json
{
  "name": "mylib",
  "version": "1.2.3",
  "is_root": true,
  "path": "packages/mylib",
  "artifacts": {
    "source": true,
    "binary": true,
    "headers": true,
    "targets": ["x86_64-windows-gnu", "x86_64-linux-gnu"]
  },
  "native_libs": [
    {
      "name": "libcurl",
      "targets": {
        "x86_64-linux-gnu": {
          "so": "packages/mylib/native/x86_64-linux-gnu/libcurl.so"
        }
      }
    }
  ]
}
```

`artifacts` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `source` | bool | 是否包含 `.sa` 源码 |
| `binary` | bool | 是否包含至少一个 target 的预编译产物 |
| `headers` | bool | 是否包含生成的头文件（二进制包必须包含，否则无法链接） |
| `targets` | array | 已提供二进制的 target 列表 |

### modules

每个元素描述一个模块：

```json
{
  "name": "MYLIB.MATH",
  "package": "mylib",
  "source": "packages/mylib/src/math.sa",
  "header": "packages/mylib/generated/sa_user_mylib_math.h",
  "binaries": {
    "x86_64-windows-gnu": {
      "kind": "dynamic",
      "dll": "packages/mylib/binaries/x86_64-windows-gnu/libmylib_math.dll",
      "import_lib": "packages/mylib/binaries/x86_64-windows-gnu/libmylib_math.dll.a"
    },
    "x86_64-linux-gnu": {
      "kind": "dynamic",
      "so": "packages/mylib/binaries/x86_64-linux-gnu/libmylib_math.so"
    }
  }
}
```

`binaries` 按 target 组织。`kind` 可选 `static` 或 `dynamic`：

- `dynamic` 下使用 `dll`（Windows）、`so`（Linux）、`dylib`（macOS），Windows 还需 `import_lib`。
- `static` 下使用 `lib`，值为 `.a` 或 `.lib` 的路径。

> **当前实现范围。** `sonc pack` 目前只产出源码包，每个 `modules` 元素实际只有 `name` / `package` / `source` / `binaries` 四个字段，其中 `binaries` 恒为空对象、`header` 字段不生成。上面示例里的 `header` 和非空 `binaries` 属于规范预留，等二进制 artifact 落地后启用。
>
> 另外注意 `source` 的路径形式：示例写的是完整的 `packages/mylib/src/math.sa`，而 `sonc pack` 实际生成的是相对根包目录的 `src/math.sa`。两种都被接受，解析规则与 [`hashes` 的键](#hashes)一致——不以 `packages/` 开头就拼上根包名。bundle 进来的依赖包必须用完整形式。

### module_to_package

```json
{
  "MYLIB": "mylib",
  "MYLIB.MATH": "mylib",
  "BASE": "base",
  "BASE.IO": "base"
}
```

### dependency_graph

```json
{
  "mylib": ["base"]
}
```

### hashes

包内文件的 sha256 摘要，值固定是 `sha256:` 前缀的十六进制串。键有两种形式：

```json
{
  "src/math.sa": "sha256:85416de4d2...",
  "packages/base/src/io.sa": "sha256:2c26b46b68..."
}
```

- **不以 `packages/` 开头**：相对**根包目录**解析，即拼成 `packages/<根包名>/<键>`。`sonc pack` 生成的就是这种形式——它只打根包，所以省掉前缀更短。
- **以 `packages/` 开头**：直接作为解包目录下的路径。bundle 进来的依赖包必须用这种形式，否则会被错误地拼上根包名。

解析规则是纯字符串的，不看磁盘上哪个文件存在——同一个键在任何情况下都指向同一个对象，否则反查覆盖率时会出漏洞。

校验在解包时进行：

- 只接受 `sha256:` 前缀，其他算法报 `不支持的 hash 格式`。
- 键指向的文件不存在报 `.spkg hash 文件不存在`；摘要不匹配报 `.spkg hash 校验失败`。
- **反查**：每个 `modules[*].source` 都必须落在已校验的文件集合里，否则报 `.spkg 模块源码缺少 hash 声明`。

反查这条是关键——只验 manifest 自己声明的条目等于没验，省掉某个条目（甚至把 `hashes` 留空）就能让对应模块源码零校验地参与编译。所以 **`"hashes": {}` 的包无法加载**，哪怕它只有一个模块。

## 模块命名

模块名使用大写，按包路径组织：

- 包根模块：`__init__.sa` → 模块名 = 包名大写，例如 `packages/mylib/src/__init__.sa` → `MYLIB`。
- 包子模块：`src/` 下的子目录或文件，例如 `packages/mylib/src/math.sa` → `MYLIB.MATH`。
- 多版本共存：不同版本的同名依赖分别放在 `packages/base-2.0.1/`、`packages/base-3.0.0/`，`module_to_package` 指向具体版本。

## 解包安全

`.spkg` 来自第三方，解包等于让别人往你磁盘上写文件，所以有一组硬性检查。任一条不通过就直接报编译错误，不会有文件落盘——manifest 先读出来校验格式，格式对不上的东西没必要往用户磁盘上铺。

**zip 路径穿越**。拒绝绝对路径、`..` 段、冒号和反斜杠绕过。否则 `../../../../Windows/System32/x.dll` 这种条目名能让解包写到任意位置。

**Windows 保留设备名**。拒绝 `CON` / `NUL` / `PRN` / `AUX` / `COM1`–`COM9` / `LPT1`–`LPT9`，**含带扩展名的形式**（`NUL.sa` 在 Windows 上仍然解析成设备）。否则写入会打到设备而不是磁盘。

**摘要校验**。见[上文的 hashes 规则](#hashes)，含反查。

**`USELIB` 值的白名单**。`USELIB` 的值会进入 C 编译器命令行，而它可以来自第三方包的源码，所以只接受纯库名（字母数字和 `_ . + -`）以及不以 `-` 开头的库文件路径。像 `USELIB "-fplugin=./evil.so"` 这种会被当成编译器选项、在构建期加载任意插件的写法直接报错。

**解包目录隔离**。每个 `.spkg` 解到自己的目录，名字是 `<stem>-<路径摘要前12位>`。只用文件名命名的话，`a/lib.spkg` 和 `b/lib.spkg` 会解到同一个坑里互相覆盖。

需要强调的是这套机制**没有签名**。摘要清单挡得住换掉某个成员这类局部篡改和传输损坏，挡不住整份 `manifest.json` 被重写。来源不明的包之前请自行确认。

## 构建时选择逻辑

对于每个被引用的模块，编译器按以下顺序决策：

1. 如果当前 `--target` 在 `module.binaries` 中存在对应条目，优先使用二进制：
   - `kind=dynamic`：链接 import lib / .so / .dylib，运行时复制 DLL/SO/dylib 到可执行文件目录。
   - `kind=static`：链接 `.a`。
2. 如果包 `artifacts.source` 为 `true`，fallback 到源码编译。
3. 如果都没有，报错。

## FFI 与原生库

`.spkg` 内的 `native_libs` 与 SA 语法 `USELIB` 配合使用：

<!-- doctest: skip 只是 FFI 声明片段，不含 SUB main -->
```basic
10 USEC "curl/curl.h" AS CURL_H
20 USELIB "curl" AS CURL_LIB
30 DECLARE C SUB CURL_H.curl_easy_init() AS CPTR
```

- `USEC` 生成 `#include`。
- `USELIB "curl"` 先在当前 `.spkg` 的 `native_libs` 中查找名为 `curl` 的库，按当前 target 链接实际路径；如果找不到，按系统库处理（例如 `-lcurl`）。

## 最小示例

单个文件打包为根模块：

```powershell
python -m sonalgebraic pack examples/mathlib.sa -o build/mathlib.spkg
```

得到的 zip 只有两个条目：

```text
mathlib.spkg
├── manifest.json
└── packages/
    └── mathlib/
        └── src/
            └── __init__.sa
```

完整的 `manifest.json`（这是 `sonc pack` 的真实输出）：

```json
{
  "format": "sonalgebraic-spkg",
  "version": 1,
  "package": {
    "name": "mathlib",
    "version": "0.1.0",
    "description": "",
    "author": "",
    "license": ""
  },
  "bundled_packages": [
    {
      "name": "mathlib",
      "version": "0.1.0",
      "is_root": true,
      "path": "packages/mathlib",
      "artifacts": { "source": true, "binary": false, "headers": false, "targets": [] },
      "native_libs": []
    }
  ],
  "modules": [
    {
      "name": "MATHLIB",
      "package": "mathlib",
      "source": "src/__init__.sa",
      "binaries": {}
    }
  ],
  "module_to_package": { "MATHLIB": "mathlib" },
  "dependency_graph": {},
  "hashes": {
    "src/__init__.sa": "sha256:85416de4d26c08c3b447f295fb69a9957cc2c47bafd4080da7f5386178792f3f"
  }
}
```

注意 `dependency_graph` 目前恒为空对象——包间依赖关系还没实装。而 `hashes` **不能**为空，见[上文的反查规则](#hashes)。

## 版本与兼容性

- 格式版本 `1` 为当前版本，加载器只接受 `1`。
- 未来版本升级时，必须保持向后兼容或提供明确的迁移指南。
- `.spkg` 不要求消费者使用发布者相同的 SonAlgebraic 编译器版本，但二进制产物必须与消费者指定的 `--target` 匹配。
- 当前实现仍以源码包为主。规范里描述的多 target 二进制 artifact、依赖递归 bundle 和版本冲突处理还没落地。
