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
  "license": "MIT",
  "homepage": "https://github.com/..."
}
```

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

```json
{
  "packages/mylib/src/math.sa": "sha256:abc...",
  "packages/base/src/io.sa": "sha256:def..."
}
```

## 模块命名

模块名使用大写，按包路径组织：

- 包根模块：`__init__.sa` → 模块名 = 包名大写，例如 `packages/mylib/src/__init__.sa` → `MYLIB`。
- 包子模块：`src/` 下的子目录或文件，例如 `packages/mylib/src/math.sa` → `MYLIB.MATH`。
- 多版本共存：不同版本的同名依赖分别放在 `packages/base-2.0.1/`、`packages/base-3.0.0/`，`module_to_package` 指向具体版本。

## 构建时选择逻辑

对于每个被引用的模块，编译器按以下顺序决策：

1. 如果当前 `--target` 在 `module.binaries` 中存在对应条目，优先使用二进制：
   - `kind=dynamic`：链接 import lib / .so / .dylib，运行时复制 DLL/SO/dylib 到可执行文件目录。
   - `kind=static`：链接 `.a`。
2. 如果包 `artifacts.source` 为 `true`，fallback 到源码编译。
3. 如果都没有，报错。

## FFI 与原生库

`.spkg` 内的 `native_libs` 与 SA 语法 `USELIB` 配合使用：

```basic
10 USEC "curl/curl.h" AS CURL_H
20 USELIB "curl" AS CURL_LIB
30 DECLARE C SUB CURL_H.curl_easy_init() AS CPTR
```

- `USEC` 生成 `#include`。
- `USELIB "curl"` 先在当前 `.spkg` 的 `native_libs` 中查找名为 `curl` 的库，按当前 target 链接实际路径；如果找不到，按系统库处理（例如 `-lcurl`）。

## 最小示例

单个文件打包为根模块：

```text
mathlib.spkg
├── manifest.json
└── packages/
    └── mathlib/
        └── src/
            └── __init__.sa
```

`manifest.json` 关键内容：

```json
{
  "format": "sonalgebraic-spkg",
  "version": 1,
  "package": { "name": "mathlib", "version": "1.0.0" },
  "bundled_packages": [
    {
      "name": "mathlib",
      "version": "1.0.0",
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
  "hashes": {}
}
```

## 版本与兼容性

- 格式版本 `1` 为当前版本。
- 未来版本升级时，必须保持向后兼容或提供明确的迁移指南。
- `.spkg` 不要求消费者使用发布者相同的 SonAlgebraic 编译器版本，但二进制产物必须与消费者指定的 `--target` 匹配。
