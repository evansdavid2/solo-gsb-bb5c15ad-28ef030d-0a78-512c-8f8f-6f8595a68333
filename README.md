# 合规规则判定 API（Python / FastAPI）

基于事实字典对命名合规规则进行 **true / false / unknown 三值逻辑** 判定的 HTTP 服务。

- 规则节点仅支持 `all`、`any`、`not`、`ref` 以及 `eq / ne / gt / gte / lt / lte` 事实比较
- 求值前先解析全部引用依赖：**缺失引用**与**循环引用**直接拒绝（422）
- 求值采用 Kleene 三值逻辑，支持短路；即使短路，也会返回**实际读取的事实、未提供字段、命中的规则路径**
- `POST /compare` 用同一 `rules`/`target` 对两组事实各求值一次，按路径对齐两侧诊断，报告新增/移除/结果翻转的命中路径与事实差异
- `POST /counterfactual` 在**不修改原事实**的前提下，搜索让目标变为确定 `true` 的**最少字段变更方案**：每个字段只能取 `domains` 给定的候选值，`unknown` 不算达成；搜索空间超过 10000 种组合时返回 422，拒绝无界计算
- 同一请求输出稳定：数组型规则按给定顺序求值，集合类结果排序，路径确定
- 依赖安装与初始化全部在镜像构建阶段完成，单条命令启动；**不依赖任何外部账号或付费服务**

---

## 1. 快速开始

```bash
docker compose up --build
```

启动后服务监听 **8000** 端口：

- 接口地址：`POST http://localhost:8000/evaluate`
- 对比接口：`POST http://localhost:8000/compare`
- 反事实搜索：`POST http://localhost:8000/counterfactual`
- 健康检查：`GET http://localhost:8000/health`
- 交互式文档：`http://localhost:8000/docs`（Swagger UI）

后台运行：`docker compose up --build -d`；停止：`docker compose down`。

### 最小示例

```bash
curl -s -X POST http://localhost:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
    "facts": {"age": 20, "country": "CN"},
    "rules": {
      "is_adult":   {"gte": ["age", 18]},
      "is_domestic": {"eq": ["country", "CN"]},
      "eligible": {"all": [{"ref": "is_adult"}, {"ref": "is_domestic"}]}
    },
    "target": "eligible"
  }'
```

响应：

```json
{
  "result": "true",
  "result_known": true,
  "used_facts": ["age", "country"],
  "missing_fields": [],
  "hit_paths": [
    {"path": "$target/all[0]/ref:is_adult", "kind": "compare", "result": "true", "field": "age", "expected": 18, "ref": "is_adult"},
    {"path": "$target/all[1]/ref:is_domestic", "kind": "compare", "result": "true", "field": "country", "expected": "CN", "ref": "is_domestic"},
    {"path": "$target", "kind": "all", "result": "true", "ref": "eligible"}
  ]
}
```

---

## 2. 请求与响应

### 请求体 `POST /evaluate`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `facts` | object | 否（默认 `{}`） | 事实字典，值为任意 JSON 标量/数组/对象 |
| `rules` | object | 否 | 命名规则映射：`{"规则名": 规则节点}` |
| `target` | object 或 string | 是 | 内联规则节点，或 `rules` 中某条规则的名称 |

### 响应体（HTTP 200）

| 字段 | 说明 |
|---|---|
| `result` | `"true"` / `"false"` / `"unknown"` |
| `result_known` | 布尔；`false` 即结果为 unknown |
| `used_facts` | 求值过程中**实际被读取过**的事实字段（字典序，稳定） |
| `missing_fields` | 被引用但 `facts` 中未提供的字段（字典序，稳定） |
| `hit_paths` | 实际命中（遍历）到的规则路径，见下 |

`hit_paths` 中每条记录：

- `path`：规则路径，根为 `$target`，后续段为 `/all[i]`、`/any[i]`、`/not`、`/ref:规则名`
- `kind`：`all` / `any` / `not` / `compare`
- `result`：该节点求值结果（三值）
- `field`、`expected`：仅比较节点存在，表示比较的事实字段与字面量
- `ref`：当该节点位于某命名规则体内时，标注规则名

路径按**求值过程顺序**记录：叶子比较节点在求值时立即记录，组合节点在其子节点求值完成后记录（因此父节点排在其子节点之后）。未被遍历的节点（短路跳过）不会出现。

### 错误响应（HTTP 422）

```json
{"error": "rule_error", "detail": "cyclic rule reference detected: a -> b -> a"}
```

- `rule_error`：规则静态错误——节点结构非法、引用缺失、循环引用
- `invalid_request`：请求体不满足 schema（由 Pydantic/FastAPI 给出明细）

任何规则静态错误都在求值**之前**返回，服务不会带着坏规则执行。

---

## 3. `POST /compare`：两组事实对比求值

用**同一套 `rules`/`target`** 分别对 `baseline_facts`（基线）与 `candidate_facts`（候选）各求值一次，并按规则路径对齐两侧结果，用于回答「事实从基线变到候选后，判定与诊断发生了什么变化」。

### 请求体

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `baseline_facts` | object | 否（默认 `{}`） | 基线事实字典 |
| `candidate_facts` | object | 否（默认 `{}`） | 候选事实字典 |
| `rules` | object | 否 | 与 `/evaluate` 相同的命名规则映射 |
| `target` | object 或 string | 是 | 与 `/evaluate` 相同；两侧共用 |

静态校验（结构、引用、查环）只执行一次，在任何求值之前；任何一侧都不会带着坏规则执行。输入非法同样返回 422（`rule_error` / `invalid_request`）。两次求值互相独立，各自遵守短路、引用解析与稳定排序。

### 最小示例

```bash
curl -s -X POST http://localhost:8000/compare \
  -H 'Content-Type: application/json' \
  -d '{
    "rules": {
      "is_adult": {"gte": ["age", 18]},
      "eligible": {"all": [{"ref": "is_adult"}, {"eq": ["country", "CN"]}]}
    },
    "target": "eligible",
    "baseline_facts": {"age": 17, "country": "CN"},
    "candidate_facts": {"age": 20, "country": "CN"}
  }'
```

### 响应体（HTTP 200）

| 字段 | 说明 |
|---|---|
| `baseline` | 基线侧完整的 `/evaluate` 响应（含三值结果与全部诊断） |
| `candidate` | 候选侧完整的 `/evaluate` 响应 |
| `result_changed` | 布尔；两侧根结果 `result` 是否不同 |
| `diff` | 按路径对齐后的差异，见下 |

`diff` 的内容：

| 字段 | 说明 |
|---|---|
| `added` | 仅在**候选侧**命中的路径，值为该侧完整 hit-path 记录 |
| `removed` | 仅在**基线侧**命中的路径，值为该侧完整 hit-path 记录 |
| `result_changed` | 两侧都命中但三值 `result` 不同的路径 |
| `used_facts` | `{"added": [...], "removed": [...]}`：实际读取字段的集合差异 |
| `missing_fields` | `{"added": [...], "removed": [...]}`：未提供字段的集合差异 |

`result_changed` 中每条记录包含：

- `path`、`kind`
- `baseline_result` / `candidate_result`：`"true"` / `"false"` / `"unknown"`
- 比较节点附带 `field`、`expected`；位于命名规则体内时附带 `ref`（这些静态元数据两侧相同，只携带一份）

示例响应（上例）：

```json
{
  "baseline": {"result": "false", "result_known": true, "used_facts": ["age"], "missing_fields": [], "hit_paths": [/* ... */]},
  "candidate": {"result": "true", "result_known": true, "used_facts": ["age", "country"], "missing_fields": [], "hit_paths": [/* ... */]},
  "result_changed": true,
  "diff": {
    "added": [
      {"path": "$target/all[1]", "kind": "compare", "result": "true", "field": "country", "expected": "CN"}
    ],
    "removed": [],
    "result_changed": [
      {"path": "$target/all[0]/ref:is_adult", "kind": "compare", "baseline_result": "false", "candidate_result": "true", "field": "age", "expected": 18, "ref": "is_adult"},
      {"path": "$target", "kind": "all", "baseline_result": "false", "candidate_result": "true", "ref": "eligible"}
    ],
    "used_facts": {"added": ["country"], "removed": []},
    "missing_fields": {"added": [], "removed": []}
  }
}
```

### 对齐与排序语义

- 路径在同一棵规则树中天然唯一（根 `$target` + 下标/`ref:` 段），故按 `path` 做精确对齐
- 路径仅出现在一侧，通常意味着**短路边界移动**：例如基线在首个 `false` 子节点短路、候选继续向后求值，则后续路径出现在 `added`；反之为 `removed`
- `added` 按**候选侧**求值顺序排列，`removed` 与 `result_changed` 按**基线侧**求值顺序排列
- `used_facts` / `missing_fields` 的 `added`/`removed` 列表按字典序排列（两侧求值输出本身已排序）
- 注意：字段在事实中缺失（比较为 unknown）**不影响遍历**，因此不会单独产生 added/removed 路径——只有短路才会；缺失字段的出入体现在 `missing_fields` 差异中

---

## 4. `POST /counterfactual`：最少变更反事实搜索

在**不修改原事实**的前提下回答：「把哪些事实字段改成哪些候选值，能让目标结果变为**确定的 `true`**？最少需要改几个字段？」

### 请求体

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `facts` | object | 否（默认 `{}`） | 原始事实字典，搜索全程**只读**，不会被修改 |
| `rules` | object | 否 | 与 `/evaluate` 相同的命名规则映射 |
| `target` | object 或 string | 是 | 与 `/evaluate` 相同 |
| `domains` | object | 是 | `{"字段名": [候选值, ...]}`，值为任意 JSON；每个被变更字段只能取该数组中的值 |
| `max_changes` | 非负整数 | 是 | 单个方案允许变更的字段数上限（必须 `≥ 0`） |

### 搜索与判定语义

- **先静态校验，再搜索**：节点结构、缺失引用、循环引用与 `/evaluate`、`/compare` 完全一致，任何规则静态错误都在搜索开始前返回 422
- 方案按「把选出的字段替换为候选值」生成，对变更后的事实重新做一次完整三值求值；原 `facts` 始终不被改动
- **变更的定义**：某字段新值与其原始值的**规范 JSON**不同才算一次变更；候选值等于当前值（含「字段当前缺失」时的差异）会被自动跳过，不会伪造零效果方案
- 候选值数组内**规范 JSON 相同**的重复值只保留一个（首个出现位置）；字段的候选值数组可以为空（等价于该字段不可变更）
- 目标结果必须是**确定的 `true`** 才算达成；`false` 与 `unknown`（例如改了无关字段、字段仍缺失）都不算
- 按变更字段数从 0 开始逐层枚举：第 0 层（零变更）原结果已为 `true` 时直接返回；找到可行方案的第一层即为**全局最小变更数**，只返回该层的方案，绝不混入更大变更数的方案
- 同一层的方案按 **「变更字段名序列 + 候选值规范 JSON」** 稳定排序并去重；字段名在方案内按字典序排列
- **规范 JSON**：对象键排序、无多余空白的 JSON 文本（如 `{"b":1,"a":[1,2]}` 与 `{"a":[1,2],"b":1}` 视为同一值）；排序按该 JSON 文本的字典序（因此是字符串序而非数值序，例如候选值 `10` 排在 `1` 之前）

### 搜索空间上限（防止无界计算）

- 枚举开始前先精确统计「变更字段数不超过 `min(max_changes, domains 字段数)`」的候选组合总数（0/1 背包式 DP，含零变更这 1 种）
- 组合总数 **超过 10000** 时直接返回 422（`search_space_too_large`），不执行枚举
- 等于 10000 及以下正常执行；可行解不存在时也在有界空间内搜尽后返回

### 最小示例

```bash
curl -s -X POST http://localhost:8000/counterfactual \
  -H 'Content-Type: application/json' \
  -d '{
    "facts": {"age": 17, "country": "CN"},
    "rules": {
      "eligible": {"all": [{"gte": ["age", 18]}, {"eq": ["country", "CN"]}]}
    },
    "target": "eligible",
    "domains": {"age": [18, 30], "country": ["CN"]},
    "max_changes": 2
  }'
```

### 响应体（HTTP 200）

| 字段 | 说明 |
|---|---|
| `original` | 原始事实的完整 `/evaluate` 响应（含三值结果与全部诊断） |
| `minimum_changes` | 全局最小变更字段数（非负整数）；无可行方案时为 `null` |
| `plans` | 全部最小变更方案，按上述规范键稳定排序；无可行方案时为 `[]` |

每个方案：

- `changes`：变更列表，每项 `{"field": 字段名, "value": 候选值}`，字段名按字典序排列
- `evaluation`：变更后事实的**完整 `/evaluate` 响应**——`result` 必为 `"true"`，并携带该次求值的 `used_facts`、`missing_fields`、`hit_paths` 诊断

上例响应（年龄提到 18 或 30 即可，均只需 1 个字段；改国家无效因为年龄仍为 17）：

```json
{
  "original": {"result": "false", "result_known": true, "used_facts": ["age"], "missing_fields": [], "hit_paths": [/* ... */]},
  "minimum_changes": 1,
  "plans": [
    {
      "changes": [{"field": "age", "value": 18}],
      "evaluation": {"result": "true", "result_known": true, "used_facts": ["age", "country"], "missing_fields": [], "hit_paths": [/* ... */]}
    },
    {
      "changes": [{"field": "age", "value": 30}],
      "evaluation": {"result": "true", "result_known": true, "used_facts": ["age", "country"], "missing_fields": [], "hit_paths": [/* ... */]}
    }
  ]
}
```

原结果已经是 `true` 时：`minimum_changes` 为 `0`，`plans` 只含一个 `changes` 为空的方案（其 `evaluation` 即 `original`）。任何候选组合都不能让结果确定为 `true` 时：`minimum_changes` 为 `null`、`plans` 为 `[]`。

### 错误响应（HTTP 422）

```json
{"error": "search_space_too_large", "detail": "counterfactual search space has 10001 combinations (limit 10000); narrow 'domains' or lower 'max_changes'"}
```

- `rule_error`：规则静态错误（结构非法、引用缺失、循环引用）——先于搜索返回
- `search_space_too_large`：候选组合总数超过 10000
- `invalid_request`：请求体不满足 schema（`domains` 缺失或值不是数组、`max_changes` 缺失/为负、`target` 缺失等）

---

## 5. 规则语法

每个规则节点是一个**只含一个操作符键**的 JSON 对象：

| 节点 | 形式 | 含义 |
|---|---|---|
| 合取 | `{"all": [节点, ...]}` | 全部成立才成立 |
| 析取 | `{"any": [节点, ...]}` | 任一成立即成立 |
| 否定 | `{"not": 节点}` | 逻辑非 |
| 引用 | `{"ref": "规则名"}` | 引用 `rules` 中的命名规则 |
| 相等 | `{"eq": ["字段", 字面量]}` | `facts[字段] == 字面量` |
| 不等 | `{"ne": ["字段", 字面量]}` | `facts[字段] != 字面量` |
| 大于 | `{"gt":  ["字段", 字面量]}` |  |
| 大于等于 | `{"gte": ["字段", 字面量]}` |  |
| 小于 | `{"lt":  ["字段", 字面量]}` |  |
| 小于等于 | `{"lte": ["字段", 字面量]}` |  |

约束：

- `all` / `any` 的值必须是**数组**（顺序即求值顺序，也是短路与路径的顺序）
- 比较值必须是 `[非空字段名, JSON 字面量]` 二元数组；只支持「事实字段 vs 字面量」，不支持字段互比
- 节点不允许出现未知键，也不允许同时出现多个操作符键

### unknown 的产生

1. 比较的字段在 `facts` 中**未提供** → 该比较为 unknown，并记入 `missing_fields`
2. 字段显式为 `null`：`eq` / `ne` 正常判等；`gt/gte/lt/lte` 对 `null` 的排序无定义 → unknown
3. 大小比较两侧类型不可比（如 `"abc" > 1`）→ unknown；`eq/ne` 不受影响

注意：字段提供了 `null` 不算「未提供」，因此不会出现在 `missing_fields` 中。

---

## 6. 三值逻辑真值表（Kleene）

**NOT**

| A | not A |
|---|---|
| true | false |
| false | true |
| unknown | unknown |

**AND（all）**

| A ∧ B | true | false | unknown |
|---|---|---|---|
| **true** | true | false | unknown |
| **false** | false | false | **false** |
| **unknown** | unknown | false | unknown |

**OR（any）**

| A ∨ B | true | false | unknown |
|---|---|---|---|
| **true** | true | true | **true** |
| **false** | true | false | unknown |
| **unknown** | true | unknown | unknown |

要点：`false ∧ unknown = false`、`true ∨ unknown = true`——确定值优先，unknown 只在没有确定结论时传播。

### 短路与可观测性

- `all` 从左到右：遇到第一个 `false` 即停止（其后的兄弟节点不求值、不读事实）
- `any` 从左到右：遇到第一个 `true` 即停止
- 短路只是**停止遍历后续节点**；已经求值的节点结果、读取的事实、缺失字段照常完整返回
- 因此 `used_facts` 反映「真正被读取过」的字段；被短路跳过的字段既不在 `used_facts` 里，也不在 `missing_fields` 里

### 稳定性保证

- `all`/`any` 严格按数组下标顺序求值，路径使用下标（`all[0]`、`any[2]`）而非字典键迭代
- `used_facts`、`missing_fields` 按字典序输出
- `hit_paths` 顺序由求值过程确定，同请求重复调用结果一致
- 容器内置 `PYTHONHASHSEED=0`，避免哈希随机化影响

---

## 7. 处理流程

1. **结构校验**：递归校验 `rules` 全部节点与 `target`
2. **引用解析**：收集所有 `ref`（含字符串形式 target），任何被引用但未定义的规则名 → 422
3. **循环检测**：在命名规则依赖图上做确定性 DFS，发现环（含自环）→ 422，错误信息给出环路，如 `a -> b -> a`
4. **三值求值**：只有以上全部通过才执行，同时收集诊断信息

循环检测覆盖整个 `rules` 图（含当前 target 不可达的规则），尽早暴露坏配置。

`/counterfactual` 复用以上 1–3 作为搜索前置条件；之后按候选组合数 DP 预算搜索空间（超过 10000 → 422），再从 0 个变更起逐层枚举最小变更方案（见第 4 节）。

---

## 8. 本地开发（不使用 Docker）

需要 Python 3.11+：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

uvicorn app.main:app --reload --port 8000   # 启动
pytest -q                                    # 运行测试
```

测试覆盖：六个比较符、Kleene 真值表、短路诊断、缺失引用/自环/直接环/间接环、DAG 菱形复用、结构非法节点、端到端 HTTP 行为，`/compare` 的路径对齐（新增/移除/结果翻转）、短路边界移动、事实与缺失字段集合差异和 422 拒绝，以及 `/counterfactual` 的最小变更搜索（零变更、unknown 不算达成、无可行方案、候选去重与规范排序、原事实不变、搜索上限边界与 422 拒绝、静态校验优先）（共 86 个用例）。

---

## 9. 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 路由（/evaluate、/compare、/counterfactual）、请求模型、错误处理
│   └── engine.py      # 规则语法、依赖解析/查环、三值求值/诊断、双次求值对齐、反事实最少变更搜索
├── tests/
│   ├── test_engine.py
│   └── test_api.py
├── Dockerfile         # 依赖与代码在构建期入镜像，含 HEALTHCHECK
├── docker-compose.yml # 单服务，映射 8000:8000
├── requirements.txt
├── requirements-dev.txt
└── README.md
```

## 10. 镜像与部署说明

- 基础镜像：`python:3.11-slim`；以非 root 用户运行
- `pip install` 与代码拷贝均在构建期完成，容器启动只运行 `uvicorn`，无启动期下载、无数据库、无消息队列、无外部账号或付费服务
- 容器内置 `/health` 健康检查；`docker compose ps` 可查看健康状态
