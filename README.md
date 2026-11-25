# offlinepack

离线交付包生成器：给定镜像索引和本地依赖清单，按目标架构递归收齐全部内容、逐层校验摘要，缺任何一层都给出**从入口到缺件**的完整依赖路径；构建可断点续导；预览页比较两个包的差异，并回答"同摘要内容实际复用了多少"。

纯标准库实现（Python ≥ 3.10），无第三方依赖，产物可直接在隔离环境里校验。

## 快速开始

```bash
# 1. 只解析、不落盘：看计划与缺失路径
python3 -m offlinepack plan \
  --index  examples/index.json \
  --deps   examples/deps.json \
  --target linux/amd64

# 2. 构建（可重复执行 --resume 续导）
python3 -m offlinepack build \
  --index  examples/index.json --deps examples/deps.json \
  --target linux/amd64 --out dist/app-bundle

# 3. 在隔离环境里复核（重新计算每个 blob 的 sha256）
python3 -m offlinepack verify dist/app-bundle

# 4. 和上一版比较，并生成本地预览页
python3 -m offlinepack diff dist/app-bundle-old dist/app-bundle \
  --html dist/preview.html
```

`plan` / `build` 的退出码：`0` 完整，`2` 有未解析路径（已打印路径），`1` 输入错误。

## 输入格式

### 镜像索引 `index.json`

```json
{
  "schema": "offlinepack/index@1",
  "name": "payments",
  "images": [
    {"name": "payments/app", "manifests": [
      {"digest": "sha256:…", "size": 812, "path": "manifests/app-amd64.json",
       "mediaType": "application/vnd.oci.image.manifest.v1+json",
       "platform": {"os": "linux", "architecture": "amd64"}},
      {"digest": "sha256:…", "path": "manifests/app-arm64.json",
       "platform": {"os": "linux", "architecture": "arm64"}}
    ]}
  ]
}
```

- `images[0]` 是入口镜像；`manifests[]` 按 `platform` 选择目标架构（`--target linux/amd64`，也可简写 `amd64`）。
- `path` 指向 OCI image manifest 的 JSON 文件，**或**一个 OCI layout 目录（含 `index.json` + `blobs/sha256/…`）。两种情况都支持。
- 单一 manifest 也可用 `"manifest": {…}`，或直接用 `"path": "…"`（不再按架构区分）。

manifest 的 `config` 与 `layers[]` 都会被递归收齐。若某个 layer 的 `mediaType` 是 manifest/index 类型，则按**嵌套镜像**继续展开——这就是多层镜像栈的表示方式，深度不限。

### 本地依赖清单 `deps.json`

```json
{
  "schema": "offlinepack/deps@1",
  "name": "payments deps",
  "artifacts": [
    {"digest": "sha256:…", "size": 1234, "path": "artifacts/ca.rpm"}
  ],
  "deps": {
    "sha256:<layer-digest>": [
      {"digest": "sha256:…", "path": "artifacts/ca.rpm"},
      {"digest": "sha256:…"}
    ]
  }
}
```

- `artifacts` 是本地内容登记表（按摘要索引），`deps` 把任意节点摘要挂到额外本地文件上。
- `deps` 里的条目可以省略 `path`，此时从 `artifacts` 按摘要取。
- 依赖一律按摘要钉死，因此即使图里有环，解析也能终止。

## 行为细节

**递归收齐 + 摘要校验.** 每个被引用的文件在写入包内 `blobs/sha256/<hex>` 前都会重新计算 sha256，和描述符/清单声明不符就判定为缺件并终止（不会留下半成品清单）。构建完成后再跑 `verify` 会再次全量重算摘要，并检查 `bundle.json` 自身有没有被改过。

**缺件路径.** 任何失败都记录完整链路：

```
missing-file: payments/app@linux/amd64 -> sha256:000000000000 (absent.tar.gz)  [file does not exist]
  looked for: /data/fixture/layers/absent.tar.gz
```

路径第一段是入口，最后一段是缺失的那条边；`fromDigest` 指出是谁引用了它。`plan --json` 会输出同样的结构，便于接入流水线。

**断点续导.** 构建开始前写入 `state.json` + `plan.json`（计划摘要），每完成一个 blob 追加一行 `journal.jsonl`。中断后加 `--resume` 继续：

- 计划摘要不同 → 拒绝（`StateMismatch`），防止把两个不同包的半成品拼在一起；
- 已完成 blob 直接复用，未完成的重拷；
- 拷贝先写 `<hex>.part`，校验通过才 rename，所以半截文件永远不会被当成"已完成"；
- 成功后会清除检查点，`--resume` 对已完成的同一个包是幂等的空操作；
- journal 最后一行写坏（中断时常见）会被忽略；
- `--rehash-existing` 强制重算已有内容，可发现并修复被篡改的存储。

**跨包复用.** `--reuse-from OLD_BUNDLE` 会把旧包里摘要相同的内容直接搬入新包（搬运后仍校验摘要），不需要重新从源目录读。

## 预览页

`diff --html` 生成单文件、零外部依赖的静态页，可直接在隔离环境打开。内容包括：

- **同摘要复用率**：`sharedBytes / (shared + added + removed)`，即两个包内容的并集里有多少字节可以原样复用；
- **两种条形图**：A∪B 内容按"共有 / 仅 B / 仅 A"划分；以及 B 自身是"新拷贝 / 复用（缓存+续导）"如何组装出来的；
- **逐 blob 表**：摘要、类型、大小、引用次数、从入口开始的依赖路径，可按状态/类型过滤、按摘要或路径搜索；
- 每个图都配表格视图；颜色不是唯一编码（状态同时有图标和文字）；明暗两套配色各自校验过对比度与色盲可分辨度。

页面里所有来自包的字符串都用 `textContent` 写入，payload 里的 `<`/`>`/`&` 会被转义，避免注入。

## 目录结构

```
bundle/
  bundle.json          # 清单：edges、每个 blob 的来源/引用数、汇总
  blobs/sha256/<hex>   # 内容寻址存储，OCI layout 兼容
```

构建成功后 `state.json` / `journal.jsonl` / `plan.json` 会被清除；`bundle.json` 的 `digest` 是除时间戳外全部字段的规范化 JSON 摘要。

## 开发

```bash
cd tests && python3 -m unittest discover -v
```

测试用 `tests/fixture_builder.py` 生成一套多架构、多层嵌套、含共享摘要与缺失/损坏变体的样例。

## 已知边界

- 只处理本地已存在的文件；不访问 registry（本工具面向隔离环境）。
- `--target` 目前支持 `os/arch[/variant]` 形式。
- 预览页的"复用率"是字节口径；blob 数量的口径单列在下方表格。
