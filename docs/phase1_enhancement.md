# Phase1 产物增强设计文档

> 目标：使 Phase1 (CodeGraph 语义层) 的输出能充分支撑 Closure v2 的「语义定界 + 结构补全」三步框架

---

## 1. 问题摘要

Closure v2 对 Phase1 产物提出了更高要求，评估发现 7 个维度的问题，其中 3 个 P0 级：

| ID | 维度 | 严重度 | 核心问题 |
|----|------|--------|---------|
| A | 摘要语义质量 | **P0** | 30词摘要语义密度不足、质量门控弱、低质量摘要污染 embedding |
| B | Embedding 区分力 | **P0** | 短文本区分度有限、Cluster 前缀污染、缺少空间上下文 |
| C | CALLS 边准确性 | **P0** | 纯名称匹配产生假阳性、confidence 未被消费 |
| D | 节点覆盖率空洞 | P1 | ENUM/NAMESPACE 无摘要 |
| E | 层级聚合信息衰减 | P1 | 文件/目录级 embedding 不代表具体符号 |
| F | 语义–用户指令鸿沟 | P1 | 代码摘要 vs 自然语言分布偏移 |
| G | 缺乏行为/领域标签 | P2 | 无 utility/business 结构化标注 |

---

## 2. 设计原则

1. **同一摘要，双侧消费** — Phase1 只生成一套摘要/embedding，锚定和闭包通过不同消费策略适配
2. **层级化优先** — 闭包定界应从目录→文件→函数逐层递进，而非扁平比较所有节点
3. **四层防线** — AST类限定 → 层级化定界 → confidence 过滤 → LLM 仲裁，任何一层拦住假阳性即安全
4. **最小改动** — 优先修改消费侧 (closure.py)，Phase1 改动限于必要的质量提升

---

## 3. A 维度：摘要语义质量

### 3.1 层级化加速定界（核心改造 — 改 closure.py 消费侧）

**当前问题**：`_build_relevance_map()` 对图谱中每个非目录节点做 cosine 比较，导致：
- 大量低质量函数摘要的 embedding 参与判断，噪声大
- 计算量与节点数成正比，大仓库不经济
- 所有层级节点（目录/文件/类/函数）混在同一个 relevance_map 中，没有利用层级信息

#### 设计原则：只排除不级联

```
✗ 不按层级"提升"节点到 CORE（目录 relevance 高 → 后代自动 CORE）
  → 因为目录内可能混杂 utility 函数，CORE 级联会导致闭包爆炸且无回退机制

✓ 只按层级"排除"明显无关子树（目录 relevance 极低 → 后代强制 OUTSIDE）
  → 误排除有安全网：被依赖的 OUTSIDE 节点会产生结构缺口，经 Step 3 仲裁找回

✓ 每个有 embedding 的节点都保留自己独立的 relevance，不被父级覆盖
```

**不对称安全性推导**：
- 误排除（漏掉一个节点） → BFS 遇到硬依赖 → 产生 StructuralGap → Step 3 仲裁 include/stub → **有兜底**
- 误纳入（多加一个节点进 CORE） → BFS 自由扩展进该节点的依赖树 → 依赖的依赖继续扩展 → 闭包爆炸 → **无回退**

因此层级化只在安全方向（排除）使用。

#### 改造后的流程

```
Step 0: 扁平计算 — 对所有有 embedding 的节点做 cosine（同现有逻辑）
        结果存入 relevance_map

Step 1: 目录级快速排除
        对每个 DIRECTORY 节点:
          dir_relevance = relevance_map.get(dir_id)
          if dir_relevance is not None and dir_relevance < periph_thresh × 0.5:
              → 该目录全部后代强制标 OUTSIDE
              → 原因：远低于灰色地带，"极不可能相关"
              → 即使个别后代自己 relevance 不低也排除
                 （如果真被依赖，Step 3 缺口仲裁会捕获并裁决 include/stub）
        注意: 目录 relevance 高 → 不做任何事（不级联 CORE）

Step 2: 无 embedding 节点填充（改良版 _fill_missing_relevance）
        对 embedding 缺失的节点:
          - 优先从子节点上推 (取 MAX)
          - 再从父节点 (CLASS 或 FILE) 下推 (×0.8)
          - 都没有 → 标 peripheral_floor

Step 3: 阈值分类 — 同现有 _classify_scope (CORE / PERIPHERAL / OUTSIDE)
        此时每个节点都有 relevance 值（真实或推断）
```

#### 排除阈值 `periph_thresh × 0.5` 的合理性

示例计算：锚点最低分 0.7 → core_thresh = 0.7 × 0.8 = 0.56 → periph_thresh = 0.56 × 0.5 = 0.28 → **目录排除阈值 = 0.14**。只有 relevance < 0.14 的目录才被一刀切，这基本意味着与用户指令完全无关。

#### 数据结构变更

```python
@dataclass
class ScopeClassification:
    core: set[str] = field(default_factory=set)
    peripheral: set[str] = field(default_factory=set)
    outside: set[str] = field(default_factory=set)
    # 新增：记录节点被排除的原因（便于调试和审计）
    dir_excluded: set[str] = field(default_factory=set)  # 因目录级排除而标 OUTSIDE 的节点集合
```

### 3.2 Query Embedding 分化（改 anchor.py + closure.py 消费侧）

**当前问题**：锚定和闭包使用同一个 `query_embedding = llm.embed([user_instruction])`，但：
- 锚定需要精确命中具体函数 → 原始自然语言 OK
- 闭包需要区域判断 → sub_features 的 description 更接近代码摘要语域

**改造方案**：

```python
# anchor.py — AnchorOutput 增加字段
@dataclass
class AnchorOutput:
    anchors: list[AnchorResult]
    query_embedding: list[float]          # 原始，用于锚定
    closure_query_embedding: list[float]  # 新增，用于闭包定界

# anchor.py — locate() 中生成 closure_query_embedding
if analysis and analysis.sub_features:
    closure_text = " | ".join(sf.description for sf in analysis.sub_features)
    closure_query_embedding = self.llm.embed([closure_text])[0]
else:
    closure_query_embedding = query_embedding

# closure.py — solve() 使用 closure_query_embedding
def solve(self, anchors, user_instruction, query_embedding=None,
          closure_query_embedding=None):
    qe = closure_query_embedding or query_embedding or self.llm.embed([user_instruction])[0]
    # 用 qe 做定界...
```

### 3.3 质量门控增强（改 semantic.py 生产侧）

**当前问题**：`_assess_summary_quality` 只用正则 + 词数 + 名称重复判断

**增强措施**：

```python
# 在现有检查之后增加：

# 新增 R1: 语义空洞检测 — 摘要没引入任何新实词
STOPWORDS = {"the","a","an","is","are","was","were","and","or","to","in",
             "for","of","on","at","by","it","its","this","that","with","from",
             "as","be","has","have","had","do","does","did","will","would",
             "can","could","should","not","if","then","else","when","which",
             "return","returns","take","takes","get","gets","set","sets"}

summary_tokens = set(re.findall(r'[a-z]{2,}', summary.lower()))
name_tokens = set(re.findall(r'[a-z]{2,}', node.name.lower()))
novel_tokens = summary_tokens - name_tokens - STOPWORDS
if len(novel_tokens) < 2:
    return "low"  # 摘要只是函数名的同义改写
```

### 3.4 低质量摘要不生成 Embedding（改 semantic.py）

**当前**：`_build_embeddings` 不检查 `summary_quality`，低质量摘要仍生成 embedding

**改造**：

```python
# semantic.py _build_embeddings
nodes_with_summary = [
    n for n in nodes
    if n.summary and n.embedding is None
    and n.metadata.get("summary_quality") != "low"  # ← 新增
]
```

低质量节点 → `embedding = None` → 在层级化定界中由父节点决定归属 → 比低质量 embedding 误判更安全

---

## 4. B 维度：Embedding 区分力

### 4.1 Cluster 前缀去污染（改 semantic.py）

**当前**：`_summarize_cluster` 将聚合摘要拼入成员的 `summary` 前缀

```python
# 当前（污染 embedding 空间）：
m.summary = f"[Cluster: {cluster_summary}] {m.summary or ''}"
```

**改造**：

```python
# 改为存 metadata，不修改 summary
m.metadata["cluster_summary"] = cluster_summary
m.metadata["cluster_members"] = [mid for mid in cluster_ids if mid != m.id]
```

Cluster 信息在缺口仲裁 prompt 中仍可使用（从 metadata 读取），但不污染 embedding。

### 4.2 增加空间上下文（改 semantic.py）

**当前** embedding 文本：`validate(user, pwd): Validates user credentials`

**改造**：增加模块路径前缀

```python
# 改造 _build_embeddings 中的 text 构建
for n in batch:
    text = n.summary
    if n.node_type == NodeType.FUNCTION and n.signature:
        module = ""
        if n.file_path:
            # auth/service.py → auth.service
            module = str(n.file_path).replace("\\", "/").rsplit(".", 1)[0].replace("/", ".") + "."
        text = f"{module}{n.name}{n.signature}: {text}"
    elif n.node_type in (NodeType.CLASS, NodeType.INTERFACE):
        if n.file_path:
            text = f"{n.file_path}: {text}"
    texts.append(text)
```

效果：`auth.service.validate(user, pwd): Validates user credentials` vs `utils.helpers.validate(data): Validates input data format` → 路径信息拉开 embedding 距离

### 4.3 Embedding 分布健康检测（新增诊断工具）

在 `core/graph/` 下新增 `diagnostics.py`，提供离线诊断能力：

```python
def diagnose_embedding_quality(graph: CodeGraph) -> dict:
    """
    采样检测 embedding 区分力：
    - 同目录函数 pair 的平均 cosine（intra）
    - 跨目录函数 pair 的平均 cosine（inter）
    - gap = intra - inter > 0.05 为合格
    返回 {"intra": float, "inter": float, "gap": float, "pass": bool}
    """
```

此方法在 pipeline.py 的 Phase1 结束后可选调用，输出诊断报告。

---

## 5. C 维度：CALLS 边准确性

### 5.1 类限定调用解析（改 treesitter_adapter.py + builder.py）

**当前调用提取**：`call_expression` → 提取函数名 → `call:func_name`

**改造**：在 `extract_dependencies` 中增加类限定信息

```
调用形式                     提取为                    含义
─────────────────────────────────────────────────────────
method()                  → call:method               裸调用
self.method()             → call:EnclosingClass.method 类内自调用（需从 AST 上下文获取）
this.method()             → call:EnclosingClass.method 同上（JS/TS/Java）
obj.method()              → call:obj.method            对象调用（obj ≈ 类名的近似）
ClassName.static_method() → call:ClassName.static      静态调用
super().method()          → call:ParentClass.method    父类调用
```

**Tree-sitter AST 提取增强**（treesitter_adapter.py）：

```python
def _extract_call_info(self, call_node, enclosing_class: str | None) -> str:
    """提取调用目标，带类限定"""
    if call_node.type == "call_expression":
        func = call_node.child_by_field_name("function")
        if func and func.type == "attribute":
            obj = func.child_by_field_name("object")
            attr = func.child_by_field_name("attribute")
            obj_text = obj.text.decode() if obj else ""
            attr_text = attr.text.decode() if attr else ""
            
            if obj_text in ("self", "this"):
                qualifier = enclosing_class or "?"
                return f"call:{qualifier}.{attr_text}"
            elif obj_text == "super()":
                return f"call:super.{attr_text}"
            else:
                return f"call:{obj_text}.{attr_text}"
        elif func and func.type == "identifier":
            return f"call:{func.text.decode()}"
    return None
```

**Builder 侧解析增强**（builder.py `_resolve_edge_target`）：

```python
def _resolve_edge_target(self, edge, source_file, resolver):
    if edge.edge_type == EdgeType.CALLS:
        call_name = edge.target.removeprefix("call:")

        if "." in call_name:
            qualifier, method = call_name.rsplit(".", 1)
            
            if qualifier == "?":
                # self.method() 但不知道类 → 限定在同文件
                candidates = [n for n in self.graph.nodes.values()
                              if n.name == method and n.node_type == NodeType.FUNCTION
                              and n.file_path == source_file]
                if not candidates:
                    candidates = self._global_name_search(method)
                confidence = 0.85 if len(candidates) == 1 else 0.5
                
            elif qualifier == "super":
                # 父类调用 → 需要找继承链
                candidates = self._resolve_super_call(method, source_file)
                confidence = 0.8 if candidates else 0.0
                
            else:
                # 有限定符 → 优先匹配「父节点名 == qualifier」
                candidates = [
                    n for n in self.graph.nodes.values()
                    if n.name == method and n.node_type == NodeType.FUNCTION
                    and self._get_parent_name(n) == qualifier
                ]
                if not candidates:
                    # 退化：qualifier 可能是变量名
                    candidates = self._global_name_search(method)
                confidence = 0.95 if len(candidates) == 1 else 0.6
        else:
            # 裸调用 → 同文件优先
            same_file = [n for n in self.graph.nodes.values()
                         if n.name == call_name and n.node_type == NodeType.FUNCTION
                         and n.file_path == source_file]
            if same_file:
                candidates, confidence = same_file, 0.9
            else:
                candidates = self._global_name_search(call_name)
                confidence = 0.85 if len(candidates) == 1 else 0.5

        # 多候选处理：超过 3 个同名 → 只取最近的 1 个
        if len(candidates) > 3:
            candidates.sort(key=lambda c: self._file_distance(source_file, c.file_path))
            candidates = candidates[:1]
            confidence = min(confidence, 0.6)

        # 建边
        for c in candidates:
            self.graph.add_edge(Edge(
                source=edge.source, target=c.id,
                edge_type=EdgeType.CALLS,
                confidence=confidence,
            ))
        return None  # 已在循环中直接添加

def _get_parent_name(self, node: CodeNode) -> str | None:
    """获取节点所属类的名称"""
    incoming = self.graph.get_incoming(node.id, EdgeType.CONTAINS)
    if incoming:
        parent = self.graph.get_node(incoming[0].source)
        if parent and parent.node_type in (NodeType.CLASS, NodeType.INTERFACE):
            return parent.name
    return None

def _file_distance(self, file_a: Path, file_b: Path | None) -> int:
    """两个文件路径的距离（共同前缀越长距离越短）"""
    if file_b is None:
        return 999
    parts_a = file_a.parts
    parts_b = file_b.parts
    common = 0
    for a, b in zip(parts_a, parts_b):
        if a == b:
            common += 1
        else:
            break
    return len(parts_a) + len(parts_b) - 2 * common

def _global_name_search(self, name: str) -> list[CodeNode]:
    """全局按名称搜索函数节点"""
    return [n for n in self.graph.nodes.values()
            if n.name == name and n.node_type == NodeType.FUNCTION]
```

### 5.2 Closure v2 消费 edge.confidence（改 closure.py）

**ClosurePolicy 增加参数**：

```python
@dataclass
class ClosurePolicy:
    # ... 现有字段 ...
    min_edge_confidence: float = 0.6   # CALLS/USES 边低于此值不自动传播
```

**closure.py `_semantic_bfs` 增加前置过滤**：

```python
# 在"按区域分流"之前增加：
if (edge.edge_type in (EdgeType.CALLS, EdgeType.USES)
        and edge.confidence < self.policy.min_edge_confidence):
    # 低置信边不自动传播 → 降级为结构缺口
    if edge.is_hard:
        structural_gaps.append(StructuralGap(
            source=node_id, target=target,
            edge=edge, target_scope="peripheral",
        ))
    continue
```

---

## 6. P1/P2 维度（后续迭代）

### 6.1 D: ENUM/NAMESPACE 摘要

在 `SemanticEnricher.enrich()` 的 `layers` 列表中加入 `NodeType.ENUM` 和 `NodeType.NAMESPACE`：

```python
layers = [
    NodeType.FUNCTION,
    NodeType.CLASS,
    NodeType.INTERFACE,
    NodeType.ENUM,        # 新增
    NodeType.NAMESPACE,   # 新增
    NodeType.FILE,
    NodeType.DIRECTORY,
]
```

ENUM 摘要 prompt：简单列出成员名 + "Summarize this enum's purpose in ONE sentence"

### 6.2 E: import 回退不用文件级 relevance

在 `_import_symbol_level` 策略 3 回退时，增加子符号级别的 relevance 检查：

```python
# 策略 3 回退前，先检查子符号 relevance
if scope:
    core_children = [c for c in file_children if c in scope.core]
    if core_children:
        # 有 CORE 子符号 → 只拉入 CORE 的
        for sym_id in core_children:
            if sym_id not in result.required_nodes:
                result.required_nodes.add(sym_id)
                queue.append((sym_id, depth + 1))
        return
```

### 6.3 F: 由 A.2 的 closure_query_embedding 解决

已纳入 3.2 节方案。

### 6.4 G: 结构化摘要输出（中期）

修改 `SUMMARIZE_FUNCTION` prompt，要求结构化输出：

```
Respond in JSON: {"summary": "...", "category": "business|utility|infrastructure|config|test"}
```

存入 `node.metadata["semantic_category"]`，供 `_rule_arbitrate` 使用：

```python
# R6: 按语义类别快筛
category = target_node.metadata.get("semantic_category")
if category == "infrastructure":
    return "stub"
if category == "test":
    return "exclude"
```

---

## 7. 四层防线协同示例

**场景**：仓库有 `utils/validator.py::validate(data)` 和 `auth/service.py::validate(user, pwd)`。用户请求提取认证功能。

| 防线 | 机制 | 效果 |
|------|------|------|
| L1: AST 类限定 (C1) | `self.validate()` → `call:AuthService.validate` | 只匹配 auth 的，不连 utils 的 |
| L2: 目录级快速排除 (A.1) | `utils/` 目录 relevance=0.08 < 0.14 → 全部后代强制 OUTSIDE | 即使有假边指向 utils，也产生缺口而非自由扩展 |
| L3: Confidence 过滤 (C3) | 假边 confidence=0.5 < 0.6 | 不自动 BFS 传播，降级为缺口 |
| L4: LLM 仲裁 (已有) | "validate(data) validates input format" | LLM 判定 stub 或 exclude |

任何一层拦住即安全。四层全部失效的概率极低。

**注意**：L2 使用的是保守排除策略（只排除不级联 CORE），即使 `auth/` 目录 relevance 很高，其中的 utility 函数仍需各自凭 embedding 独立判断。

---

## 8. 文件改动清单

### Phase 1.1 ✅ 已完成

| 文件 | 改动 | 状态 |
|------|------|------|
| `core/graph/semantic.py` | B1 cluster 去污染 + A4 低质量不生成 embedding + A3 质量门控增强 + B2 空间上下文 | ✅ |
| `core/prune/closure.py` | A1 目录级快速排除 + C3 消费 confidence | ✅ |
| `config.py` | ClosurePolicy 加 `min_edge_confidence` | ✅ |

### Phase 1.2 ✅ 已完成

| 文件 | 改动 | 状态 |
|------|------|------|
| `parsers/treesitter_adapter.py` | C1 类限定调用提取 (`_extract_call_target` + `enclosing_class` tracking) | ✅ |
| `core/graph/builder.py` | C1 类限定解析 + 多候选裁剪 (`_get_parent_name`, `_file_distance`, `_global_name_search`) | ✅ |
| `core/prune/anchor.py` | A2/F1 `closure_query_embedding` 生成 | ✅ |
| `pipeline.py` | 传递 `closure_query_embedding` | ✅ |
| `core/prune/closure.py` | 接受 `closure_query_embedding` 参数 | ✅ |

### Phase 1.3 ✅ 已完成

| 文件 | 改动 | 状态 |
|------|------|------|
| `core/graph/diagnostics.py` | B3 embedding 诊断工具（新文件，`diagnose_embedding_quality`） | ✅ |
| `core/llm/prompts.py` | G1 结构化摘要 prompt（JSON: summary + category） | ✅ |
| `core/graph/semantic.py` | G1 `_parse_function_summary` 解析 + D1 ENUM/NAMESPACE 支持 | ✅ |
| `core/prune/closure.py` | E2 import 回退增强（CORE 子符号优先）+ G1 R6 category 快筛 | ✅ |

---

## 9. 风险评估

| 风险 | 影响 | 缓解 |
|------|------|------|
| 目录级排除阈值过严（periph_thresh×0.5 排除了有个别重要函数的目录） | 遗漏关键依赖 | 缺口仲裁 Step 3 会捕获被依赖的 OUTSIDE 节点，交 LLM 裁决 include/stub |
| 类限定提取对动态语言 (Python/JS) 效果有限 | 仍有假阳性 | L2-L4 兜底，不依赖单层 |
| Embedding 路径前缀改变后，已缓存的 embedding 需重建 | 增量更新场景 | 加 metadata["embedding_version"] 标记，不匹配时重生成 |
| 低质量不生成 embedding 导致部分节点永远由父节点代理 | 少数场景不准确 | 仅影响 PERIPHERAL 区域的函数级判断，父节点判断通常更可靠 |
