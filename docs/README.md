# CodePrune

给一个完整的代码仓库，再给一句话说你想要什么功能，CodePrune 把这个功能涉及的代码精确地抠出来，交给你一个能独立编译运行的子仓库。

支持 Python、Java、JavaScript、TypeScript、C、C++。

---

## 目录

- [问题与思路](#问题与思路)
- [系统概览](#系统概览)
- [安装与运行](#安装与运行)
- [Phase 1: CodeGraph — 建图](#phase-1-codegraph--建图)
- [Phase 2: CodePrune — 裁剪](#phase-2-codeprune--裁剪)
- [Phase 3: CodeHeal — 修复](#phase-3-codeheal--修复)
- [CLI 与配置](#cli-与配置)
- [Benchmark](#benchmark)
- [项目结构](#项目结构)
- [当前问题与提升方向](#当前问题与提升方向)
- [依赖](#依赖)

---

## 问题与思路

假设你有一个博客后端——用户、文章、评论、标签、通知、媒体管理散在几十个文件里——你只要**评论功能**相关的代码。手动挑会漏依赖，按目录整删又太粗。

真实用户通常只能清楚地说出**自己要什么功能**，却很难提前穷举**自己不要什么**。因此 CodePrune 的目标不是要求用户写出完整删除清单，而是让系统结合**指令 + 仓库上下文**自己判断：哪些代码应完整保留、哪些代码只需打桩、哪些代码可以安全裁掉。

对于位于目标功能上下游、但本身不属于目标域的模块，系统优先在 `INCLUDE / STUB / EXCLUDE` 之间权衡——能用最小接口桩维持独立编译运行，就不把整个模块拖进来，避免闭包爆炸。

CodePrune 做的事：

1. 用 tree-sitter 解析仓库 AST，建出函数/类/文件级别的依赖图，用 LLM 生成语义摘要和 embedding
2. 理解用户的自然语言指令，在依赖图中定位功能入口（锚点），沿 import/调用链收集最小闭包，碰到闭包外的依赖逐个做 include/stub/exclude 仲裁，最后用 AST 手术逐文件提取保留的符号
3. 对裁剪产物做多层验证修复：编译检查 → import 扫描 → 启动验证 → 完整性 → 真实性 → 功能验证 → 测试，直到子仓库能独立运行

不是目录级的粗暴删除，是函数/类级别的精确提取。

---

## 系统概览

```
Phase 1: CodeGraph          Phase 2: CodePrune            Phase 3: CodeHeal
┌──────────────────┐    ┌───────────────────────────┐    ┌──────────────────────────┐
│ builder.py       │    │ instruction_analyzer.py    │    │ import_fixer.py          │
│  文件系统扫描     │    │  指令 → 子功能 + 排除项    │    │  断裂 import 清理         │
│  tree-sitter AST │    │ anchor.py                  │    │ reference_audit.py       │
│                  │    │  语义检索 + LLM 确认锚点   │    │  悬挂引用审计             │
│ semantic.py      │    │ closure.py                 │    │ source_recovery.py       │
│  LLM 摘要       │    │  BFS 传播 + 缺口仲裁      │    │  原仓库代码恢复           │
│  embedding 索引  │    │ surgeon.py                  │    │ validator.py             │
│  质量评估        │    │  AST 手术 + import 过滤    │    │  多语言编译检查           │
│                  │    │                            │    │ runtime_validator.py     │
│                  │    │                            │    │  确定性 import 扫描       │
│                  │    │                            │    │ error_dispatcher.py      │
│                  │    │                            │    │  通用错误调度             │
│                  │    │                            │    │ boot_validator.py        │
│                  │    │                            │    │  启动验证                │
│                  │    │                            │    │ functional_validator.py  │
│                  │    │                            │    │  功能验证                │
│                  │    │                            │    │ fixer.py                 │
│                  │    │                            │    │  LLM 补丁 + 桩          │
│                  │    │                            │    │ finalize.py              │
│                  │    │                            │    │  README/deps 生成        │
└──────────────────┘    └───────────────────────────┘    └──────────────────────────┘
   ~5-10 LLM 调用            ~10-30 LLM 调用                  ~5-15 LLM 调用
```

三个阶段由 `pipeline.py` 串联。也可以通过 CLI 单独运行某个阶段（`cli.py graph` / `prune` / `heal`）。

---

## 安装与运行

```bash
pip install tree-sitter tree-sitter-languages openai anthropic numpy pyyaml

# 全流程：建图 → 裁剪 → 修复
python cli.py run ./my_project "保留用户登录和权限验证功能" -o ./output -v

# 用配置文件
python cli.py config init           # 生成 codeprune.yaml 模板
python cli.py run ./my_project "保留登录功能" -c codeprune.yaml -o ./output
```

输出目录就是一个可以直接编译运行的子仓库。

需要 Python 3.11+。需要能调用 OpenAI 兼容 API（用于摘要生成、锚点验证、缺口仲裁、代码修复）和 embedding API。

---

## Phase 1: CodeGraph — 建图

把代码仓库变成一张图：节点是函数/类/文件/目录，边是 import/调用/继承等依赖关系。

### 物理层 — `core/graph/builder.py`（334 行）

`GraphBuilder.build()` 做三件事：

1. **扫描文件系统**：遍历目录，跳过 `node_modules`、`__pycache__`、`.git` 等（可配置），为每个目录和源文件创建图节点，建立 `CONTAINS` 层级边
2. **tree-sitter AST 解析**：对每个源文件跑对应语言的 tree-sitter parser，提取函数、类、接口、枚举等符号节点，记录每个符号在源文件中的字节范围（`ByteRange`，精确到行列），后续手术阶段靠这个范围定位代码
3. **依赖边提取**：从 AST 中识别 import/include 语句（`IMPORTS` 边）、函数调用（`CALLS` 边）、类继承（`INHERITS` 边）、类型引用（`USES` 边）。每条 `IMPORTS` 边在 `metadata.imported_symbols` 里记录了具体导入了哪些符号名——闭包求解靠这个做符号级精确传播

六种语言的 AST 差异由 `parsers/treesitter_adapter.py`（615 行）统一：Python 的 `__all__` 列表提取、`importlib.import_module()` 动态导入识别、TypeScript 的 `import type` 纯类型导入标记、C/C++ 的 `#include` 路径解析，全在这一层处理。

图谱的数据结构定义在 `core/graph/schema.py`（185 行）：

- **`CodeNode`**：节点 ID 格式为 `file:src/auth.py::class:AuthService::function:login`，携带节点类型、语言、文件路径、字节范围、摘要、embedding、签名等属性
- **`Edge`**：分硬依赖（`CONTAINS` / `IMPORTS` / `CALLS` / `INHERITS` / `IMPLEMENTS` / `USES`）和软依赖（`SEMANTIC_RELATED` / `COOPERATES`），每条边有置信度和元信息
- **`CodeGraph`**：图容器，提供按节点 ID 查找、按类型过滤、获取某节点所有入边/出边等查询方法

### 语义层 — `core/graph/semantic.py`（528 行）

`SemanticEnricher.enrich()` 在物理层图谱上叠加语义信息：

**摘要生成**：自底向上——先给函数生成 1~2 句摘要，再聚合成类摘要，再聚合成文件摘要，最后目录摘要。摘要 prompt 里融入函数签名（参数名和类型），让摘要不只是"处理某事"而是能体现参数语义。

**质量评估**：四类低质量摘要标记 `summary_quality="low"` 并在后续降权（置信度 × 0.7）：过短的、包含泛化词的、和函数名重复的、空泛的。低质量摘要可选触发重试——附上调用者/被调用者上下文让 LLM 重新生成。

**功能簇聚合**：通过 `CALLS` 边找连通分量（限制 2~8 个节点），对紧密协作的函数组生成一条聚合摘要（前缀 `[Cluster: ...]`），让后续检索能把一组协作函数当整体看。

**Embedding**：把 `qualified_name(params): summary` 拼成文本，跑 embedding 模型（默认 `text-embedding-3-small`，1536 维）得到向量。参数类型信息编码到了向量里。

**入口点标记**：零入度 + 顶层函数，或命名模式匹配（`main` / `run` / `start` / `setup`），标记 `is_entry_point=True`。

### 语言规则钩子 — `parsers/lang_rules/`（401 行）

建图结束后调 `lang_rules.post_build_validate()`：Python 检查 `__all__` 一致性，Java 检查 package 声明和目录结构，JS/TS 检查相对导入路径，C/C++ 检查头文件目录。

---

## Phase 2: CodePrune — 裁剪

系统最重的阶段。把用户的一句话变成「需要保留的代码集合」，再用 AST 手术落地。分四步。

### 2.0 指令理解 — `core/prune/instruction_analyzer.py`（402 行）

把自然语言指令拆解成结构化的裁剪策略。

**阶段 A — 上下文收集（不调 LLM）**

把用户指令做 embedding，和图谱中所有节点的向量做余弦相似度比对。用自适应阈值（最高分 × 0.5）而非固定 Top-K 选出候选。同时整理目录节点的摘要和未入选的文件名列表。

**阶段 B — Grounded Reasoning（一次 LLM 调用）**

候选实体列表 + 目录摘要 + 用户指令一起喂给 LLM，返回 JSON：

```
sub_features:         # 拆解出的子功能列表
  [0].description     → "评论 CRUD 和垃圾评论审核"
  [0].root_entities   → ["comments.handlers.create_comment", "comments.moderation.check_spam"]
out_of_scope:         # 明确排除的目录/模块
anchor_strategy:      # "focused" | "distributed" | "broad"
excluded_symbols:     # 方法级排除
restricted_classes:   # 部分提取的类
```

关键约束（GROUNDING RULE）：**每个子功能必须对应用户指令中明确提到的内容**。LLM 不许自行推断间接依赖——那是闭包求解器的事。

### 2.1 锚点定位 — `core/prune/anchor.py`（746 行）

在图谱中找到用户想要的功能对应哪些函数/类。

**有指令分析时**：

1. `root_entities` 精确查找（置信度 0.95）
2. 子功能 description embedding 检索
3. 指令中英文标识符关键词匹配
4. 三层合并，逐个 LLM 确认（prompt 带调用者/被调用者列表）

**无指令分析时**：指令 embedding 检索 + LLM 确认 → 降阈值重试 → 纯 Top-3 兜底。

**策略**：`focused` 5 / `distributed` 12 / `broad` 20 个。

**受限 seed 展开**：FILE 最多 3 个函数 seed、CLASS 最多 4 个方法 seed。

**消费者入口点**：根目录 .py 文件 import ≥ 2 锚点模块 → 自动加入。

### 2.2 闭包求解 — `core/prune/closure.py`（1935 行）

从锚点出发收集最小可运行代码集合。系统最复杂模块。

**语义定界**：embedding 相似度把节点分 CORE / PERIPHERAL / OUTSIDE 三区。阈值结合锚点分位数 + 全图分位数推导。BFS 前做范围预检：CORE+PERIPHERAL > 45% 就自动收紧。

**BFS 传播**：硬依赖自由传播，OUTSIDE 记为结构缺口。import 符号级精度（只拉 `imported_symbols` 中的符号）。barrel 文件只保留被引用的导出。

**缺口仲裁**：规则快筛 → LLM 三选一（INCLUDE/STUB/EXCLUDE）。每 5 个批量调用，最多 3 轮。

**后处理**：包含链完整性、粒度升级、闭包大小硬限 50%。

### 2.x 稳定性观测

生成 `selection_diagnostics.json`，包含指令理解结果、锚点验证、阈值计算、闭包规模、缺口仲裁数据。

### 2.3 AST 手术 — `core/prune/surgeon.py`（1237 行）

- 全文件 / 部分提取（byte_range 精确切割）
- import 过滤 + 装饰器保留 + 类骨架 + 模块级变量 + C/C++ 条件编译 + 头文件配对
- 方法级排除 + 裁剪标记 + 构建配置复制

---

## Phase 3: CodeHeal — 修复

裁剪必然破坏引用关系。目标：让子仓库能过编译、能启动、能运行。

### 预处理（不消耗修复轮次）

**Import 清理** — `import_fixer.py`（930 行）：AST 精确移除不存在的 import（支持单符号粒度），级联注释引用行，多轮收敛处理 re-export 链。

**未定义名解析**：`UndefinedNameResolver` 自动补全标准库缺失 import。

**引用审计** — `reference_audit.py`（562 行）：扫描非 import 悬挂引用（函数调用、配置值、注册表映射），LLM 决策 REMOVE/COMMENT/KEEP。

**源码恢复** — `source_recovery.py`（295 行）：统一的原仓库代码恢复器，三层恢复粒度（文件级/符号级/语句级）。在 LLM 和 Dispatcher 之前执行，扫描 `[CodePrune] audit` 标记智能恢复被审计误注释的代码。

### 修复循环 — `fixer.py`（2315 行）

最多 8 轮验证-修复循环：

```
Layer 1   — Build:          编译验证 (Python/Java/TS/JS/C/C++)
Layer 1.5 — UndefinedNames: pyflakes 未定义名
Layer 2.0 — Runtime:        确定性 import 扫描 + RuntimeFixer
Layer 2.5 — Boot:           入口点启动验证
Layer 3   — Completeness:   功能完整性
Layer 3.5 — Fidelity:       真实性校验 (快照对比)
Layer 4   — Functional:     两阶段功能验证
Layer 5   — Test:           测试集成
```

**Build 修复**：三级策略（自动 import 补充 → LLM 补丁 → 桩代码）。≥ 3 错误启动 Architect 模式（reasoning 分析 + fast 执行）。

**ErrorDispatcher**（982 行）：通用 error→deterministic action 调度，不调 LLM。C 语言 5 种模式、Python/TS/Java 各 2 种。Protected Includes 机制保护 Dispatcher 添加的 include 不被 LLM 误删。

**Runtime Validation**（828 行）：逐模块 subprocess import → 解析 4 类错误 → RuntimeFixer 确定性修复。支持 re-export 自动补齐和从原仓库补回被剪函数。

**Boot Validation**（424 行）：入口点发现 → LLM 生成 boot 脚本 → subprocess 验证 → 失败重试 → 优雅降级。

**Functional Validation**（324 行）：Stage 1 原仓库验证脚本正确性 → Stage 2 子仓库验证功能。目前仅 Python。

**死循环检测**：同层错误 hash 对比 → skip 该层进入下一层。

### Finalize — `finalize.py`（499 行）

自动生成子仓库 `requirements.txt`（内置 PIL→Pillow 等映射）和 `README.md`。

---

## CLI 与配置

### 基本用法

```bash
python cli.py run ./my-blog "保留评论功能" -o ./output/blog -v
python cli.py config init              # 生成配置模板
python cli.py config show              # 显示当前配置
```

### 配置文件结构

```yaml
llm:
  provider: openai
  reasoning:
    model: gpt-5.4
    temperature: 0.2
    max_tokens: 16384
  fast:
    model: gpt-5.4-mini
    temperature: 0.3
    max_tokens: 8192
  embedding_api_base: https://...     # 独立 embedding 端点

graph:
  initial_granularity: function       # file / class / function
  enable_semantic: true
  enable_embedding: true

prune:
  scope_strategy: llm_hierarchical    # llm_hierarchical | embedding_threshold
  closure_policy:
    max_semantic_scope_ratio: 0.45
    max_closure_ratio: 0.5

heal:
  max_heal_rounds: 8
  enable_runtime_validation: true
  enable_boot_validation: true
  enable_functional_validation: true
  enable_finalize: true
```

双模型分工：`reasoning`（指令理解、缺口仲裁、代码修复）和 `fast`（摘要、锚点验证）。缓存按模型隔离。

---

## Benchmark

9 个手写基准项目，覆盖 Python / Java / C / TypeScript：

| 项目 | 语言 | 裁剪场景 |
|------|------|---------|
| mini-blog | Python | 评论系统（审核、反应、通知） |
| mini-compiler | C | 前端 + 优化，去 codegen/runtime |
| mini-dashboard | TypeScript | 图表展示，去用户管理 |
| mini-shop | Java | 订单链（下单、取消、支付退款） |
| mini-etl | Python | 清洗 + 验证，仅 CSV/JSON |
| mini-framework | Python | HTTP 链（路由、中间件、认证），去 ORM/任务 |
| mini-orchestrator | Python | 本地执行 + onboarding/billing |
| mini-ticketing | Java | 工单审批链（提交→评论→审批→通知） |
| mini-query-engine | C | 查询前端（解析+规划+优化） |
## 项目结构

```
D:\CodePrune/
├── cli.py                          # CLI 入口
├── pipeline.py                     # Phase1→2→3 编排
├── config.py                       # 配置定义
├── codeprune.yaml                  # 运行配置
│
├── core/
│   ├── graph/                      # Phase1: 图谱构建 (1223 行)
│   │   ├── builder.py              #   文件扫描 + AST
│   │   ├── schema.py               #   数据结构
│   │   ├── semantic.py             #   LLM 摘要 + embedding
│   │   ├── query.py                #   查询工具
│   │   └── diagnostics.py          #   embedding 诊断
│   │
│   ├─ prune/                      # Phase2: 裁剪 (4323 行)
│   │   ├── instruction_analyzer.py #   指令理解
│   │   ├── anchor.py               #   锚点定位
│   │   ├── closure.py              #   闭包求解 (最复杂)
│   │   └── surgeon.py              #   AST 手术
│   │
│   ├─ heal/                       # Phase3: 修复 (7708 行)
│   │   ├─ fixer.py                #   HealEngine 主循环
│   │   ├─ import_fixer.py         #   import 精确清理
│   │   ├─ error_dispatcher.py     #   通用错误调度
│   │   ├─ runtime_validator.py    #   runtime 扫描修复
│   │   ├─ reference_audit.py      #   悬挂引用审计
│   │   ├─ source_recovery.py      #   原仓库代码恢复
│   │   ├── validator.py            #   多语言编译检查
│   │   ├── boot_validator.py       #   启动验证
│   │   ├── functional_validator.py #   功能验证
│   │   └── finalize.py             #   产物生成
│   │
│   └─ llm/                        # LLM 层 (537 行)
│       ├─ prompts.py              #   prompt 模板
│       └─ provider.py             #   双模型 + 缓存
│
├─ parsers/                        # AST 适配层 (1354 行)
│   ├── treesitter_adapter.py       #   6 语言统一
│   ├── import_resolver.py          #   import 路径
│   └── lang_rules/                 #   语言规则引擎
│
└─ docs/                           # 设计文档
```

---

## 当前问题与提升方向

1. **CALLS 边级别**：builder.py 创建 file→function CALLS 边，功能簇需要 function→function
2. **Lazy Resolution 不成熟**：默认关闭
3. **闭包保守倾向**：Precision 是瓶颈，偏向多保留
4. **AST 手术语言覆盖不均**：Python/TS 好（~50%），Java/C++ 低（~5%）
5. **Functional Validation 语言限制**：仅 Python

---

## 依赖

核心依赖（无 requirements.txt，手动安装）：

```
tree-sitter>=0.21.0,<0.22
tree-sitter-languages>=1.10.0
openai>=1.0.0
anthropic>=0.25.0
numpy>=1.24.0
pyyaml>=6.0
pyflakes>=3.0              # UndefinedNames 检测
pytest>=7.0                # dev
```
