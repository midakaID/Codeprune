# CodePrune — 项目背景 Pro

> 基于 LLM 的实体代码仓库功能级裁剪系统，从完整仓库中精确提取用户指定功能的最小可运行子仓库。

---

## 一、项目定位

CodePrune 解决的核心问题：给定一个完整代码仓库和一段自然语言功能描述，自动裁剪出**仅包含该功能所需代码**的最小可运行子仓库。

**典型场景**：一个博客后端仓库包含用户管理、文章发布、评论系统、推荐算法、后台管理等十余模块。用户只需要"评论相关功能"——CodePrune 自动识别评论模块涉及的所有类、函数、import 链、配置文件，剪除无关代码，输出一个可独立编译运行的评论功能子仓库。

**核心约束**：

| 约束 | 说明 |
|------|------|
| 多语言覆盖 | Python / Java / JavaScript / TypeScript / C / C++ 六种主流语言 |
| 单仓库场景 | 只处理单体仓库（非 monorepo 多包场景） |
| 功能级粒度 | 函数/类级别的精确裁剪，而非目录级的粗暴删除 |
| 可运行保证 | 输出的子仓库必须能独立编译/运行，不能有悬空引用和缺失依赖 |
| LLM 约束感知 | 充分利用 LLM 的语义理解能力，同时规避其幻觉和不确定性 |
| 不使用增量解析 | 设计决策：每次全量处理，不维护增量状态 |

---

## 二、三阶段流水线

```
Phase1: CodeGraph      →      Phase2: CodePrune       →      Phase3: CodeHeal
 ┌─────────────────┐    ┌──────────────────────────┐    ┌──────────────────┐
 │ 物理层: AST解析  │    │ 2.0 指令理解(grounded)   │    │ L1: 编译验证+修复│
 │ 语义层: LLM摘要  │    │ 2.1 锚点定位(三层合并)   │    │ L2: 完整性检查   │
 │ 向量层: Embedding │    │ 2.2 闭包求解(语义定界)   │    │ L3: 真实性校验   │
 │ 簇层: 功能簇聚合  │    │ 2.3 AST手术(符号级提取)  │    │                  │
 └─────────────────┘    └──────────────────────────┘    └──────────────────┘
     ~5-10 LLM calls         ~10-30 LLM calls              ~5-15 LLM calls
```

---

## 三、Phase1: CodeGraph — 代码认知

**目标**：将代码仓库转化为一个多层图谱，作为后续裁剪的全量上下文。

### 3.1 物理层 (builder.py)

递归扫描仓库 → tree-sitter 多语言 AST 解析 → 提取符号和依赖关系：

```
REPOSITORY → DIRECTORY → FILE → CLASS / FUNCTION / INTERFACE / ENUM / NAMESPACE
              (CONTAINS 边)     (IMPORTS/CALLS/INHERITS/IMPLEMENTS/USES 边)
```

- **节点类型** (NodeType): REPOSITORY / DIRECTORY / FILE / CLASS / FUNCTION / INTERFACE / ENUM / MODULE / NAMESPACE
- **边类型** (EdgeType): 分为硬依赖（CONTAINS, IMPORTS, CALLS, INHERITS, IMPLEMENTS, USES）和软依赖（SEMANTIC_RELATED, COOPERATES）
- **import 边元信息**: `metadata.imported_symbols` 精确记录被导入的符号名，`type_only` 标记 TS 纯类型导入
- **Python 特殊处理**: `__all__` 提取（存入文件节点 metadata）、`importlib.import_module()` 动态 import 生成额外边
- **Lazy Resolution**: 初始仅做文件级扫描（快），锚点定位后再对相关区域展开类/函数级解析（精）

### 3.2 语义层 (semantic.py)

LLM 驱动的语义增强，Bottom-up 顺序（FUNCTION → CLASS → FILE → DIRECTORY）：

1. **签名融入摘要**: 函数摘要 Prompt 附带参数签名信息 → 摘要中隐含类型语义，不只是"处理某事"
2. **摘要质量评估**: 检测 4 类低质量摘要（过短 / 泛化模式 / 与函数名重复）→ 标记 `summary_quality="low"` → 下游锚点定位时 confidence × 0.7 降权
3. **功能簇聚合**: CALLS 边连通分量 (2~8) → 整簇用集体摘要前缀 `[Cluster: ...]` → 避免紧密协作的小函数被分散选中
4. **签名增强 Embedding**: `qualified_name(params): summary` 作为 embedding 输入文本 → 参数类型信息直接编码到向量中

### 3.3 数据结构

**CodeNode** — 图谱中每个实体：
```
id: "file:src/auth.py::class:AuthService::function:login"
node_type / name / qualified_name / file_path / language
byte_range: (start_byte, end_byte, start_line, end_line, start_col, end_col)
children: [子节点ID列表]
summary / embedding / signature
metadata: {summary_quality, __all__, fullclass, ...}
```

**Edge** — 实体间的关系：
```
source / target / edge_type / confidence
metadata: {imported_symbols, type_only, dynamic, ...}
category: HARD (闭包必须传递) | SOFT (LLM 判决)
```

**CodeGraph** — 图谱容器：
```
nodes: {id → CodeNode}, edges: [Edge], root: str
get_hard_dependencies / get_all_outgoing / get_all_incoming / stats
```

### 3.4 语言规则钩子

Phase1 结束后调用 `lang_rules.post_build_validate(graph, file_path)` 进行语言级图谱验证：
- Python: `__all__` 与实际导出的一致性
- Java: package 声明与目录结构匹配
- JS/TS: 相对导入路径存在性

---

## 四、Phase2: CodePrune — 精确裁剪

### 4.0 指令理解 (instruction_analyzer.py)

**核心理念**：不让 LLM 盲猜代码结构，而是先从图谱中收集上下文，再让 LLM 基于真实图谱信息做 grounded reasoning。

#### 阶段 A — 上下文收集（0 次 LLM 调用）

```
用户指令 → embedding → 与图谱所有实体计算余弦相似度
                       ↓
          自适应阈值 = max_score × 0.5
                       ↓
          选出 Top 候选实体 (≤50, ≤3000 tokens)
                       ↓
          收集 DIRECTORY 节点摘要（仓库骨架）
          收集未命中实体的文件名列表（兜底）
```

为什么自适应阈值而非固定 Top-K？
- 明确指令（"保留登录功能"）→ 少数候选高度匹配 → 上下文小而精
- 宽泛指令（"保留用户相关"）→ 候选分散 → 上下文大但全

#### 阶段 B — Grounded Reasoning（1 次 LLM 调用）

将 [目录摘要] + [候选实体] + [其他文件名] + [用户指令] 送入 `UNDERSTAND_INSTRUCTION` Prompt：

```
LLM 输出 (JSON):
├── sub_features:         # 独立子功能列表
│   ├── [0].description   → "用户密码验证和JWT令牌生成"
│   ├── [0].root_entities → ["AuthService.authenticate", "JWTUtil.generate"]
│   └── [0].reasoning     → "authenticate 是验证入口，JWTUtil 生成令牌"
├── out_of_scope:         # 明确排除的目录/模块
│   └── ["tests/", "docs/", "admin/"]
└── anchor_strategy:      # 锚点策略
    └── "focused" | "distributed" | "broad"
```

**验证与兜底**：
- `_fuzzy_match_name()`: 后缀匹配 → 包含匹配 → 多匹配放弃（宁缺勿乱）
- 完整回退链：LLM 失败 / JSON 解析失败 / 无有效子功能 → 返回 None → 下游走回退路径

#### 产物

```python
@dataclass
class InstructionAnalysis:
    original: str                   # 原始指令
    sub_features: list[SubFeature]  # 分解的子功能
    out_of_scope: list[str]         # 排除目录
    anchor_strategy: str            # "focused"(1-3) / "distributed"(4-8) / "broad"(8+)
```

---

### 4.1 锚点定位 (anchor.py)

**目标**：在图谱中找到用户指令对应功能的 implementation root（实现入口）。

#### 双路径架构

```
InstructionAnalysis 存在?
  ├── YES → Path A: 分析驱动
  │   ├── 第一层: LLM root_entities → qualified_name 精确查找 → 置信度 0.95
  │   ├── 第二层: 各子功能 description → embedding 搜索 → 排除 out_of_scope
  │   └── 第三层: 指令中英文标识符 → 关键词名称匹配
  │
  └── NO  → Path B: 回退
      ├── 原始指令 embedding → 语义检索
      └── 名称/关键词检索补充
```

#### 增强的 LLM 验证

每个候选锚点经 `VERIFY_ANCHOR` Prompt 验证，新增三项上下文：
- **features_text**：从 sub_features 提取的功能描述（而非原始自然语言指令）→ 更贴近代码语义
- **exclusions_section**：out_of_scope 目录列表
- **call_context**：从图谱 CALLS 边提取该函数的 callers + callees → LLM 看到完整调用关系

#### 策略驱动

| anchor_strategy | max_anchors | 适用场景 |
|:---:|:---:|---|
| focused | 5 | 单一功能，明确入口 |
| distributed | 12 | 多模块功能，分散入口 |
| broad | 20 | 基础设施级功能，入口广泛 |

#### 兜底链

```
LLM 验证全未通过 → 阈值 × 0.5 重试 → 仍失败 → 纯 embedding Top-3（无 LLM 验证）
```

---

### 4.2 闭包求解 (closure.py)

**目标**：从锚点出发，通过图遍历确定最小可运行的代码支撑集合。

这是整个系统最复杂的模块（819 行），采用 v2 语义定界架构。

#### Step 1: 语义定界 — 三区分类

```
计算所有节点与 query_embedding 的余弦相似度 → relevance_map

自适应阈值推导:
  core_threshold     = max(min_anchor_relevance × 0.75, 0.30)
  peripheral_threshold = max(core_threshold × 0.50, 0.15)

三区分类:
  CORE       := { n | relevance[n] ≥ core_threshold }        # 明确属于目标功能
  PERIPHERAL := { n | peripheral_threshold ≤ relevance[n] < core_threshold }  # 灰色地带
  OUTSIDE    := { n | relevance[n] < peripheral_threshold }   # 明确不属于
```

优势：不只是"选/不选"二分，而是"确定 / 待定 / 排除"三分 → 支持下游细粒度判决。

#### Step 2: 语义引导 BFS

```
从锚点出发 BFS 传播:
├── 硬依赖 → CORE 区域: 自由传播，自动纳入
├── 硬依赖 → PERIPHERAL 区域: 纳入但记录待审
├── 硬依赖 → OUTSIDE 区域: 生成 StructuralGap（不直接丢弃，交缺口仲裁）
├── 硬依赖 → out_of_scope 目录: 强制降级为 StructuralGap（边界约束）
├── 软依赖: 不传播，后续批量 LLM 判决
└── TS type_only import: 降级为软依赖
```

**Import 符号级传播**（防止整文件冗余拉入）：
- Python `__all__` 模块 → 只拉入 `__all__` 中的符号
- `imported_symbols` 精确记录 → 只拉入实际导入的符号
- 回退查 CALLS/INHERITS/USES 边 → 确定实际被引用的符号
- 都无法确定 → 保守拉入整文件

#### Step 3: 缺口仲裁

```
缺口 = 闭包内节点有硬依赖指向闭包外节点

三层仲裁:
  规则快筛 ─→ LLM 三选一 ─→ 批量优化

规则快筛:
  ├── 代码 < 20 行 → 直接 include（成本可控）
  └── 入度 > 25     → 直接 stub（基础设施，保留桩即可）

LLM 三选一 (features_text 驱动):
  ├── include → 核心依赖，完整纳入
  ├── stub    → 不属于目标功能，但生成桩代码保持编译
  └── exclude → 可选调用（日志/指标/通知），安全移除

批量优化: 每 5 个缺口一次 LLM 调用
```

#### Step 4: 后处理

- 包含链保证：所有被选节点的祖先自动纳入
- Python `__init__.py` 自动递归包含
- 粒度升级：类的所有子方法都已选中 → `fullclass=True`
- CLASS 自动展开：整类选中 → 纳入所有子方法
- 闭包大小检查：超过 `max_closure_ratio` (50%) 代码行数 → 触发自动收紧

#### features_text 贯穿机制

`InstructionAnalysis.sub_features` 的 description 拼合成 `features_text`，替代原始用户指令，贯穿：
- `anchor._verify_candidate()` → LLM 验证锚点
- `closure._arbitrate_gaps()` → 缺口仲裁
- `closure._llm_judge_single_gap()` / `_llm_judge_gap_batch()` → 软依赖判决

好处：LLM 看到的是经过图谱 grounding 的功能描述，而非用户原始自然语言 → 判决更精准。

#### ClosurePolicy 参数

```python
ClosurePolicy:
  core_threshold_factor    = 0.75    # CORE = min_anchor × 0.75
  peripheral_threshold_factor = 0.50 # PERIPHERAL = CORE × 0.50
  core_floor               = 0.30   # CORE 阈值保底
  peripheral_floor         = 0.15   # PERIPHERAL 阈值保底
  
  small_code_threshold     = 20     # 小代码直接 include
  infra_in_degree_threshold = 25    # 基础设施直接 stub
  prefer_stub              = True   # 不确定时优先 stub
  max_gap_iterations       = 3      # 缝隙仲裁最大轮次
  
  max_closure_ratio        = 0.5    # 代码行数占比硬上限
  size_check_interval      = 50     # 每 50 节点检查一次
```

---

### 4.3 AST 手术 (surgeon.py)

**目标**：按闭包结果从原仓库物理提取代码到子仓库。

- 整文件在闭包 → 全量复制
- 部分提取 → 按 byte_range 精确切割 + 以下增强:
  - 装饰器向上扩展（通过 `lang_rules.decorator_prefixes`）
  - 构造方法自动包含（通过 `lang_rules.constructor_names`）
  - 类骨架保留（声明行 + 构造 + 结尾括号）
  - 智能 import 过滤（语言特定正则，TS type import 按实际引用过滤）
  - 模块级变量保留（被选中符号引用的顶层赋值）
  - C/C++ 条件编译对齐（`#ifdef`/`#endif` 栈扫描，自动补全预处理指令对）
  - 裁剪标记：不连续行间 `# ... pruned N lines ...`
- C/C++ 头文件自动配对（`.h` ↔ `.c/.cpp`）
- 构建配置文件复制（CMakeLists.txt, pom.xml 等）
- Stub 代码生成（闭包中 stub_nodes → 最小函数/类骨架）

---

## 五、Phase3: CodeHeal — 自愈修补

```
子仓库 → [编译验证 → LLM修复]×N → 完整性检查 → 真实性校验 → 可运行子仓库
```

### 5.1 Layer 1: 编译验证 + LLM 修复 (validator.py + fixer.py)

**多语言编译检查**：

| 语言 | 验证方式 | 特殊处理 |
|------|----------|----------|
| Python | `ast.parse()` + import 存在性 + AST 引用验证 | 最全面的语义级检查 |
| Java | `javac` 编译 | 构建工具感知 (Maven target/, Gradle build/)；`cannot find symbol` 降级 warning |
| JS | `node --check` | 快速语法检查 |
| TS | `tsc --noEmit` | 类型检查 |
| C/C++ | `gcc/g++ -fsyntax-only` | 仅语法，无链接 |

**LLM 修复流程**：

1. 缺失 import 前置处理：从原仓库补充缺失文件 → 仍失败则注释 import 行
2. LLM 生成修复补丁（以原仓库为 ground truth，禁止凭空生成逻辑）
3. 补丁安全验证：模糊匹配 ≥ 80%、修复代码 ≤ 原代码 × 2、必须在原仓库有出处
4. 修复拓扑排序：被更多文件 import 的优先修复（减少连锁错误）
5. 死循环检测：错误数未减少 → 停止

### 5.2 Layer 2: 功能完整性检查

LLM 对比子仓库摘要 vs 原仓库摘要 → 识别遗漏组件 → 从原仓库补充

### 5.3 Layer 3: 真实性校验

检测并删除/回退不在原仓库中的文件 → 防止 LLM 修复过程中凭空生成代码

---

## 六、语言规则引擎 (parsers/lang_rules/)

**设计目标**：统一管理跨阶段的语言特化逻辑，消除 surgeon/validator 中的硬编码字典。

```python
class LanguageRule(ABC):
    language                   # 适用语言
    import_line_pattern        # import 行正则
    decorator_prefixes         # 装饰器前缀 ("@", "#[", ...)
    constructor_names          # 构造方法名 ("__init__", "<init>", "constructor")
    header_source_pairs        # 头/源文件映射 ({".h": (".c",), ".hpp": (".cpp",)})
    build_config_patterns      # 构建配置文件 glob
    
    post_build_validate()      # Phase1 后图谱验证
    adjust_closure()           # Phase2 闭包调整
    post_surgery_fixup()       # Phase2 手术后修复
    get_compile_command()      # 编译验证命令
```

| 语言 | 规则类 | 核心特化能力 |
|------|--------|-------------|
| Python | PythonRules | `__all__` 验证、`__init__.py` 缺失检测 |
| Java | JavaRules | package 声明与目录匹配验证 |
| JS/TS | JSRules | 相对导入路径检查、index 解析 |
| C/C++ | CRules | `#include ""` 头文件检查、CMake/Make 编译命令 |

---

## 七、回退链设计

CodePrune 在每个关键环节都有完整的降级策略，确保不会因单点失败导致整体中断：

```
指令理解:
  LLM 失败 / JSON 解析失败 / 无有效子功能
  → InstructionAnalysis = None
  → 锚点走 Path B（回退 embedding 检索）

锚点定位:
  Path A 三层合并候选不足 / LLM 验证全未通过
  → 阈值 × 0.5 重试
  → 纯 embedding Top-3 兜底
  → 无任何锚点 → RuntimeError（终止）

闭包求解:
  节点无 embedding → 从子节点推论(MAX) / 父节点推论(×0.8) / peripheral_floor
  缺口 LLM 判决失败 → 规则层快筛 → 保守 exclude
  闭包过大 → 自动收紧

自愈修补:
  编译失败 → 从原仓库补文件 → LLM 补丁 → 补丁不合格则跳过
  死循环 → 停止修复
```

---

## 八、关键设计决策

| 决策 | 做法 | 理由 |
|------|------|------|
| **指令理解用 grounded reasoning** | 先收集图谱上下文，再让 LLM 从候选中选择 | 比让 LLM 盲猜代码结构可靠得多 |
| **自适应阈值而非固定 Top-K** | `max_score × 0.5` 动态阈值 | 指令明确时上下文精，宽泛时上下文全 |
| **三区语义定界** | CORE / PERIPHERAL / OUTSIDE | 比 include/exclude 二分更精细 |
| **features_text 贯穿全流程** | 从 sub_features 提取的 grounded 描述替代原始指令 | LLM 判决更精准 |
| **缝隙仲裁三选一** | include / stub / exclude 而非 include/exclude | stub 是重要的中间态 |
| **Lazy Resolution** | 先文件级再按需展开 | 初期速度快，避免无用细粒度解析 |
| **签名融入 embedding** | 参数类型编入向量 | 同名不同功能的符号可区分 |
| **功能簇聚合** | CALLS 连通分量整体摘要 | 避免紧密协作函数被分散选中 |
| **TS type import 降级** | type_only 标记 → 软依赖 | 类型导入不影响运行时 |
| **不支持增量** | 每次全量处理 | 简化架构，避免状态维护 |

---

## 九、技术栈

| 组件 | 技术选型 | 说明 |
|------|----------|------|
| AST 解析 | tree-sitter 0.21.x + tree-sitter-languages 1.10.x | 0.23.x API 不兼容，锁 0.21 |
| LLM 调用 | OpenAI / Anthropic API | provider.py 抽象层统一接口 |
| 向量运算 | numpy | embedding 余弦相似度 |
| 运行环境 | Python 3.11+ | conda 环境 |

---

## 十、代码规模

```
~5,500+ 行 Python 代码，32 个源文件
65 个单元测试（34 指令理解 + 31 语言规则）

核心模块行数:
  closure.py              819 行  (闭包求解，最复杂)
  surgeon.py              619 行  (AST 手术)
  treesitter_adapter.py   506 行  (多语言 AST 适配)
  fixer.py                374 行  (LLM 修复引擎)
  validator.py            333 行  (多语言编译验证)
  anchor.py               289 行  (锚点定位)
  import_resolver.py      277 行  (多语言 import 解析)
  semantic.py             241 行  (语义增强)
  instruction_analyzer.py 200 行  (指令理解)
  builder.py              193 行  (物理层图谱构建)
  schema.py               169 行  (核心数据结构)
  pipeline.py             155 行  (三阶段编排)
  prompts.py              150 行  (8 个 Prompt 模板)
  provider.py             144 行  (LLM 抽象层)
  config.py               113 行  (6 个配置 dataclass)
```

---

## 十一、已知限制

1. **tree-sitter 版本锁定**: 0.21.x，无法升级到 0.23.x
2. **C/C++ 宏展开**: `#define` 体内符号依赖分析有限（条件编译指令对齐已支持）
3. **LLM 幻觉**: 三层验证+原仓库 ground truth 约束部分缓解，无法完全消除
4. **动态特性**: Python `exec()` / JS `eval()` / 反射调用无法分析
5. **第三方依赖**: Maven/Gradle/npm 依赖无法自动补充
6. **LLM 成本**: 大仓库全量语义增强 + 软依赖判决需要大量 API 调用
