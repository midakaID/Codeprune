# 闭包求解 (ClosureSolver) 方案设计

> 版本: v2.0 | 日期: 2026-04-02

## 一、背景与问题

### 1.1 闭包求解在 Pipeline 中的位置

```
Phase1: CodeGraph（物理层 + 语义层图谱构建）
    ↓
Phase2: CodePrune
    ├── 锚点定位 (anchor.py)      ← 找到用户描述的功能核心代码
    ├── 闭包求解 (closure.py)     ← 本文档：从锚点扩展到最小可运行子集
    └── AST 手术 (surgeon.py)     ← 根据闭包结果提取代码
    ↓
Phase3: CodeHeal（自愈修复）
```

### 1.2 核心目标

给定一组锚点（用户描述功能对应的代码实体），求解**最小可运行闭包**——恰好包含该功能运行所需的全部代码，不多不少。

### 1.3 v1 方案的根本缺陷

v1 方案采用"**结构驱动 + 语义打补丁**"的思路：

```
锚点 → 沿硬边 BFS 无差别扩散 → 遇到问题再处理 → 闭包
```

这导致了以下系统性问题：

| 编号 | 问题 | 严重性 | 根因 |
|------|------|--------|------|
| #1 | CLASS 自动展开摧毁函数级粒度 | P0 | `_expand_class_children` 无条件展开所有 CLASS，选一个方法 → 拉入整个类 |
| #2 | import 符号传播时序导致过度拉入 | P0 | 策略 2 依赖闭包中已有节点的引用关系，但被引用者可能尚未入队 |
| #3 | 软依赖二次扩展后的新软依赖丢失 | P1 | 软候选集只在初始 BFS 中收集一次 |
| #4 | selected_summaries 在批量判断中过时 | P1 | LLM 上下文不反映已接受的新节点 |
| #5 | `_expand_hard_deps` 遗漏 type_only 降级 | P1 | 重复的 BFS 逻辑导致行为不一致 |
| #6 | Python `__init__.py` 递归包含不完整 | P1 | 只包含直接父目录，不递归向上 |
| #7 | `_auto_include_init_py` 全量扫描性能热点 | P2 | 对每个目录遍历全部节点 |
| #8 | `import *` 无 `__all__` 时过度保守 | P2 | 直接回退到整文件拉入 |
| #9 | 闭包大小预警只看节点数 | P2 | 节点数 ≠ 代码体积 |
| #10 | 批量 prompt 未纳入 Prompts 类 | Minor | 维护困难 |
| #11 | LLM 缺少"为什么是软依赖"的上下文 | Minor | 判断质量受限 |
| #12 | 别名 import 可能匹配失败 | Minor | imported_symbols 可能存储别名而非原名 |
| #13 | BFS 与 `_expand_hard_deps` 逻辑重复 | Minor | 维护时容易不一致 |
| #14 | 硬依赖一刀切，无法表达语义边界 | P0 | 所有 CALLS 边无差别传递 |
| #15 | 上帝节点引爆闭包 | P0 | BFS 无法识别基础设施节点 |
| #16 | 固定判定策略 vs 场景多样性 | P1 | 不同剪枝场景需要不同边界策略 |

### 1.4 根本矛盾

> **闭包的边界由用户的语义描述决定，而非由图的拓扑决定。**

一条 CALLS 边的"必要性"不在边本身，而在目标节点与用户意图的语义距离。同一条 `CALLS` 边在不同语境下的必要性差异极大。

---

## 二、v2 方案：语义定界 + 结构补全

### 2.1 核心范式转换

```
v1: 结构驱动 — 沿边 BFS，每条边做判断
    → 问题爆炸：每条边的判断需要综合分析大量上下文

v2: 语义驱动 — 先圈定语义范围，再处理结构缺口
    → 问题简化：大部分节点在语义层面已有结论，只有边界缺口需要精判
```

### 2.2 三步框架

```
╔══════════════════════════════════════════════════════════╗
║  Step 1: 语义定界 (Semantic Scoping)                     ║
║  → 全节点 embedding 相关度评分                            ║
║  → 划分 CORE / PERIPHERAL / OUTSIDE 三个区域             ║
╠══════════════════════════════════════════════════════════╣
║  Step 2: 语义引导 BFS (Scope-Aware Traversal)            ║
║  → CORE 区域内自由扩展                                    ║
║  → PERIPHERAL 按独占性+边类型精细控制                      ║
║  → 跨出到 OUTSIDE 产生"结构缺口"                         ║
╠══════════════════════════════════════════════════════════╣
║  Step 3: 缺口仲裁 (Gap Arbitration)                      ║
║  → 规则快筛 (Zero Cost)                                  ║
║  → LLM 三选一: include / stub / exclude                  ║
║  → 迭代扩展新 include 产生的二级缺口                      ║
╚══════════════════════════════════════════════════════════╝
```

### 2.3 与 v1 概念映射

| v1 概念 | v2 替代 | 说明 |
|---------|---------|------|
| 硬依赖/软依赖 二分法 | CORE/PERIPHERAL/OUTSIDE 三区域 | 不再按边类型分，而按目标节点的语义相关度分 |
| 按边类型判断（CALLS=硬） | 按区域 + 独占性判断 | 同一 CALLS 边，在 CORE 内直接 include，在 OUTSIDE 则为 gap |
| LLM 判断 include/exclude | LLM 判断 include/stub/exclude | 新增 stub 选项解决"不想拉入但删了编译不过" |
| 70% 节点数预警 | 代码行数实时监控 + 动态收紧 | 从事后预警变为过程中动态调节 |

---

## 三、Step 1 · 语义定界 (Semantic Scoping) 详细设计

### 3.1 Relevance Map 构建

**输入**: 
- `query_embedding`: 用户指令的 embedding 向量（由 AnchorLocator 产出）
- `graph.nodes`: 图谱中所有节点（含 embedding）

**输出**: 
- `relevance_map: dict[str, float]` — 节点 ID → 语义相关度 [0, 1]

**逻辑**:
```python
def build_relevance_map(query_embedding, graph):
    relevance = {}
    for nid, node in graph.nodes.items():
        if node.node_type in (NodeType.DIRECTORY, NodeType.REPOSITORY):
            continue  # 目录/仓库节点不参与评分
        if node.embedding is not None:
            relevance[nid] = cosine_sim(node.embedding, query_embedding)
        else:
            relevance[nid] = None  # 标记待推断
    return relevance
```

### 3.2 Embedding 缺失处理

部分节点可能无 embedding（summary 生成失败、文件过大跳过等），不能忽略——它们可能是关键代码。

**推断规则**（按优先级）:
1. **子节点上推**: FUNCTION 缺失 → 取父节点 (CLASS/FILE) 的 relevance
2. **父节点下推**: CLASS/FILE 缺失 → 取子节点的 MAX relevance
3. **无法推断**: 保守标记为 PERIPHERAL（宁可多包含，不漏掉）

```python
def fill_missing_relevance(relevance, graph):
    for nid in list(relevance):
        if relevance[nid] is not None:
            continue
        node = graph.get_node(nid)
        if not node:
            relevance[nid] = 0.0
            continue
        
        # 从子节点推断
        child_scores = [relevance[c] for c in node.children 
                       if relevance.get(c) is not None]
        if child_scores:
            relevance[nid] = max(child_scores)
            continue
        
        # 从父节点推断
        incoming = graph.get_incoming(nid, EdgeType.CONTAINS)
        if incoming:
            parent_score = relevance.get(incoming[0].source)
            if parent_score is not None:
                relevance[nid] = parent_score * 0.8  # 轻微衰减
                continue
        
        # 无法推断 → 放入 PERIPHERAL
        relevance[nid] = PERIPHERAL_THRESHOLD
```

### 3.3 自适应阈值

**核心思想**: 不硬编码阈值，以锚点的 relevance 为参照动态推导。

锚点是经过 LLM 确认的"一定相关"的节点，代表了 CORE 区域的"底线"。

```python
def compute_thresholds(relevance_map, anchors):
    anchor_scores = [relevance_map[a.node_id] 
                    for a in anchors 
                    if relevance_map.get(a.node_id) is not None]
    
    if not anchor_scores:
        return 0.40, 0.20  # 全回退
    
    min_anchor = min(anchor_scores)
    # CORE: 与最弱锚点相似度达到 75% → 大概率属于同一功能域
    core_threshold = max(min_anchor * 0.75, 0.30)
    # PERIPHERAL: CORE 的 50% → 灰色地带
    peripheral_threshold = max(core_threshold * 0.50, 0.15)
    
    return core_threshold, peripheral_threshold
```

**参数选择理由**:
- `0.75`: 留 25% 余量是因为锚点有 LLM 验证加持，其他节点没有
- `0.30`: 保底阈值防止锚点 score 偏低时阈值过低把无关节点拉入 CORE
- `0.50`: PERIPHERAL 比 CORE 宽松一倍，容纳"可能相关"的节点
- `0.15`: 保底阈值防止 PERIPHERAL 范围过大

### 3.4 区域分类

```python
@dataclass
class ScopeClassification:
    core: set[str]           # 语义上属于目标功能
    peripheral: set[str]     # 可能被用到（灰色地带）
    outside: set[str]        # 语义上不属于

def classify_scope(relevance_map, core_thresh, periph_thresh):
    scope = ScopeClassification(set(), set(), set())
    for nid, score in relevance_map.items():
        if score >= core_thresh:
            scope.core.add(nid)
        elif score >= periph_thresh:
            scope.peripheral.add(nid)
        else:
            scope.outside.add(nid)
    return scope
```

---

## 四、Step 2 · 语义引导 BFS (Scope-Aware Traversal) 详细设计

### 4.1 BFS 主逻辑

从锚点出发，按目标节点所在区域决定处理策略。

```python
def semantic_bfs(anchors, scope, graph, max_depth):
    result = set(a.node_id for a in anchors)
    structural_gaps = []
    queue = deque((a.node_id, 0) for a in anchors)
    
    while queue:
        nid, depth = queue.popleft()
        if depth >= max_depth:
            continue
        
        for edge in graph.get_outgoing(nid):
            target = edge.target
            if target in result:
                continue
            target_node = graph.get_node(target)
            if not target_node:
                continue
            
            # ── 按目标区域分流 ──
            
            if target in scope.core:
                # CORE: 无条件加入，继续 BFS
                result.add(target)
                queue.append((target, depth + 1))
                
            elif target in scope.peripheral:
                # PERIPHERAL: 精细控制
                decision = _peripheral_decision(edge, target, result, graph)
                if decision == "include":
                    result.add(target)
                    queue.append((target, depth + 1))
                elif decision == "gap":
                    structural_gaps.append(StructuralGap(
                        source=nid, target=target, edge=edge,
                        target_scope="peripheral"
                    ))
                # "skip" → 忽略
                    
            else:  # OUTSIDE
                if edge.is_hard:
                    structural_gaps.append(StructuralGap(
                        source=nid, target=target, edge=edge,
                        target_scope="outside"
                    ))
                # 软依赖到 OUTSIDE → 直接忽略
    
    return result, structural_gaps
```

### 4.2 PERIPHERAL 区域决策逻辑

```python
def _peripheral_decision(edge, target_id, closure_nodes, graph):
    """
    PERIPHERAL 节点的精细决策。
    返回 "include" | "gap" | "skip"
    """
    # 结构性必含边 → include
    if edge.edge_type in (EdgeType.CONTAINS, EdgeType.INHERITS, EdgeType.IMPLEMENTS):
        return "include"
    
    # TypeScript type-only import → gap（交仲裁）
    if edge.edge_type == EdgeType.IMPORTS and edge.metadata.get("type_only"):
        return "gap"
    
    # import 边 + 目标是文件 → 符号级传播（详见 4.3）
    if edge.edge_type == EdgeType.IMPORTS:
        target_node = graph.get_node(target_id)
        if target_node and target_node.node_type == NodeType.FILE:
            return "import_propagation"  # 特殊处理
    
    # CALLS / USES → 按独占性判断
    if edge.edge_type in (EdgeType.CALLS, EdgeType.USES):
        exclusivity = _compute_exclusivity(target_id, closure_nodes, graph)
        if exclusivity > 0.5:
            return "include"  # 主要被闭包内节点使用 → 属于该功能
        else:
            return "gap"      # 共享组件 → 交仲裁
    
    # 语义边 → gap
    return "gap"
```

### 4.3 Import 符号级传播（在新框架中的定位）

原 3+1 策略在新框架下**缩小了适用范围**：

```
import 目标在 CORE     → 直接 include 整个模块（语义已确认）
import 目标在 PERIPHERAL → 符号级传播（只拉入实际用到的符号）
import 目标在 OUTSIDE   → 结构缺口，交 Step 3 仲裁
```

**只有 PERIPHERAL 区域的 import 才需要符号级传播**，这解决了 v1 的时序问题——在 CORE 区域内不需要精确到符号，直接包含即可。

符号级传播策略保持不变：
- 策略 0: `from X import *` + `__all__` → 只拉入 `__all__` 中的符号
- 策略 1: `imported_symbols` 元数据精确匹配
- 策略 2: 回退查 CALLS/INHERITS/USES 引用
- 策略 3: 保守拉入整个文件（但现在只在 PERIPHERAL 触发，影响有限）

### 4.4 独占性计算

**独占性**：目标节点被闭包内节点"独占使用"的程度。

```python
def _compute_exclusivity(target_id, closure_nodes, graph):
    """
    = 闭包内调用者数 / 仓库中全部调用者数
    
    独占性高 → 这个节点实质上是目标功能的专属依赖
    独占性低 → 共享基础设施 / 跨功能组件
    """
    all_callers = set()
    for edge_type in (EdgeType.CALLS, EdgeType.USES, EdgeType.INHERITS):
        for e in graph.get_incoming(target_id, edge_type):
            all_callers.add(e.source)
    
    if not all_callers:
        return 0.5  # 无调用者 → 中性
    
    closure_callers = all_callers & closure_nodes
    return len(closure_callers) / len(all_callers)
```

### 4.5 闭包大小实时监控

BFS 过程中实时检查闭包代码体积占比，超过阈值自动收紧边界：

```python
# 嵌入 BFS 主循环
if len(result) % check_interval == 0:
    ratio = _compute_code_ratio(result, graph)
    if ratio > policy.max_closure_ratio * 0.8:
        # 将 PERIPHERAL 中尚未加入的节点降级为 OUTSIDE
        scope.outside.update(scope.peripheral - result)
        scope.peripheral &= result
        logger.warning(f"闭包达 {ratio:.0%}，自动收紧边界")
```

**计算方式**: 代码行数比例（非节点数），从 `ByteRange` 计算。

---

## 五、Step 3 · 缺口仲裁 (Gap Arbitration) 详细设计

### 5.1 数据结构

```python
@dataclass
class StructuralGap:
    source: str           # 闭包内的节点（调用者）
    target: str           # 闭包外的节点（被调用者）
    edge: Edge            # 依赖边
    target_scope: str     # "peripheral" | "outside"

@dataclass
class MergedGap:
    target: str                        # 目标节点 ID
    sources: list[tuple[str, Edge]]    # 所有调用来源 [(source_id, edge), ...]
    target_scope: str
    count: int                         # 被多少个闭包节点依赖
```

### 5.2 缺口去重与合并

多个闭包内节点可能对同一外部节点产生缺口，合并后 LLM 可以看到完整的调用上下文。

```python
def merge_gaps(gaps):
    by_target = {}
    for g in gaps:
        by_target.setdefault(g.target, []).append(g)
    
    merged = []
    for target_id, group in by_target.items():
        merged.append(MergedGap(
            target=target_id,
            sources=[(g.source, g.edge) for g in group],
            target_scope=group[0].target_scope,
            count=len(group),
        ))
    # 被依赖最多的优先判断
    merged.sort(key=lambda m: -m.count)
    return merged
```

### 5.3 规则层快速仲裁

能用规则决定的不送 LLM：

```python
def rule_arbitrate(gap, graph, policy):
    """
    返回 "include" | "stub" | "exclude" | None(需LLM)
    """
    target_node = graph.get_node(gap.target)
    if not target_node:
        return "exclude"
    
    # R1: 类型定义/接口/枚举 → include（编译必需，体积小）
    if target_node.node_type in (NodeType.INTERFACE, NodeType.ENUM):
        return "include"
    
    # R2: 代码极小（< 20 行）→ include（stub 的开销比直接包含更大）
    if target_node.byte_range:
        lines = target_node.byte_range.end_line - target_node.byte_range.start_line
        if lines < 20:
            return "include"
    
    # R3: 独占性极高（> 0.8）→ include（实质是该功能的代码）
    exclusivity = _compute_exclusivity(gap.target, ...)
    if exclusivity > 0.8:
        return "include"
    
    # R4: 入度极高（> 25）→ stub（典型基础设施）
    total_incoming = len(graph.get_incoming(gap.target))
    if total_incoming > 25:
        return "stub"
    
    # R5: 匹配用户排除关键词 → exclude
    if _matches_exclude_patterns(target_node, policy.exclude_keywords):
        return "exclude"
    
    return None  # 需要 LLM
```

### 5.4 LLM 缺口仲裁

**Prompt 设计**: 提供完整上下文（来源、目标、其他调用者），三选一。

```
JUDGE_STRUCTURAL_GAP = """
你正在剪枝一个代码仓库，目标是提取用户描述的功能到独立子仓库。

用户描述: "{user_instruction}"

以下代码实体处于已选代码的边界之外，但被已选代码直接调用:

  实体: {name} ({node_type})
  文件: {file_path}
  功能: {summary}
  
  在已选代码中被以下节点调用:
{callers_context}

  该实体在仓库中还被以下 {other_count} 个其它节点使用（仅列前5）:
{other_callers}

请选择最合适的处理方式:
- "include": 这是目标功能的核心依赖，必须整体保留
- "stub": 这不属于目标功能，但调用关系存在，应生成桩代码保证编译通过
- "exclude": 这是可选调用（如日志、监控、通知），可以安全移除调用代码

回复 JSON: {"decision": "include|stub|exclude", "reason": "..."}
"""
```

### 5.5 迭代扩展

include 的节点可能引入新的外部依赖（二级缺口），需要迭代：

```python
def arbitrate_all_gaps(merged_gaps, result, graph, ...):
    pending = list(merged_gaps)
    max_iterations = 3  # 防止无限迭代
    
    for iteration in range(max_iterations):
        if not pending:
            break
        
        newly_included = set()
        # 规则快筛 + LLM 仲裁
        for gap in pending:
            decision = rule_arbitrate(gap, ...) or llm_judge(gap, ...)
            apply_decision(gap, decision, result)
            if decision == "include":
                newly_included.add(gap.target)
        
        # 新 include 节点的出边 → 新缺口
        if not newly_included:
            break
        new_gaps = []
        for nid in newly_included:
            for edge in graph.get_outgoing(nid):
                if edge.target not in result.required_nodes \
                   and edge.target not in result.stub_nodes \
                   and edge.is_hard:
                    new_gaps.append(StructuralGap(...))
        pending = merge_and_dedup(new_gaps)
```

---

## 六、Step 4 · 后处理

### 6.1 包含链完整性

与 v1 相同：选中的符号自动包含其所属类和文件。

### 6.2 `__init__.py` 递归包含（修正）

v1 只包含直接父目录的 `__init__.py`，v2 递归向上包含所有祖先目录的 `__init__.py`。

```python
def _auto_include_init_py_recursive(result, graph):
    py_dirs = set()
    for nid in list(result.required_nodes):
        node = graph.get_node(nid)
        if node and node.node_type == NodeType.FILE and str(node.file_path).endswith(".py"):
            # 递归向上收集所有祖先目录
            current = Path(node.file_path).parent
            while current != Path(".") and current != current.parent:
                py_dirs.add(current)
                current = current.parent
    
    for dir_path in py_dirs:
        init_path = dir_path / "__init__.py"
        # 通过路径索引查找（避免遍历全节点）
        init_node_id = f"file:{init_path}"
        if init_node_id in graph.nodes and init_node_id not in result.required_nodes:
            result.required_nodes.add(init_node_id)
```

### 6.3 粒度升级 + CLASS 条件展开（修正）

v1 的 `_expand_class_children` 无条件展开所有 CLASS 节点，导致函数级粒度失效。

v2 修正：**只展开 `fullclass=True` 的类**。

```python
def _upgrade_full_classes(result, graph):
    """如果一个 CLASS 的所有 FUNCTION 子节点都在闭包中，标记 fullclass"""
    for nid, node in graph.nodes.items():
        if node.node_type not in (NodeType.CLASS, NodeType.INTERFACE):
            continue
        func_children = [c for c in node.children 
                        if graph.get_node(c) and graph.get_node(c).node_type == NodeType.FUNCTION]
        if func_children and all(c in result.required_nodes for c in func_children):
            node.metadata["fullclass"] = True

def _expand_class_children_if_full(result, graph):
    """只展开标记了 fullclass 的类"""
    to_add = set()
    for nid in list(result.required_nodes):
        node = graph.get_node(nid)
        if node and node.metadata.get("fullclass"):
            for child_id in node.children:
                if child_id not in result.required_nodes:
                    to_add.add(child_id)
    result.required_nodes.update(to_add)
```

### 6.4 闭包大小终检

使用代码行数（非节点数）计算占比：

```python
def _final_size_check(result, graph):
    total_lines = 0
    closure_lines = 0
    for nid, node in graph.nodes.items():
        if node.byte_range:
            lines = node.byte_range.end_line - node.byte_range.start_line
            total_lines += lines
            if nid in result.required_nodes:
                closure_lines += lines
    
    if total_lines > 0:
        ratio = closure_lines / total_lines
        if ratio > 0.7:
            logger.warning(
                f"⚠ 闭包过大: {closure_lines}/{total_lines} 行 ({ratio:.0%})，"
                f"裁剪效果不佳，建议缩小指令范围"
            )
```

---

## 七、数据结构变更

### 7.1 ClosureResult（扩展）

```python
@dataclass
class ClosureResult:
    required_nodes: set[str] = field(default_factory=set)     # 保留真实代码的节点
    stub_nodes: set[str] = field(default_factory=set)          # 需要生成桩代码的节点（新增）
    excluded_edges: list[tuple[str, str]] = field(default_factory=list)  # 被排除的调用关系（新增）
    
    # 审计/调试信息
    soft_included: set[str] = field(default_factory=set)       # 保留兼容：经裁决纳入的边界节点
    soft_excluded: set[str] = field(default_factory=set)       # 保留兼容：经裁决排除的边界节点
    structural_gaps: list = field(default_factory=list)         # 所有结构缺口记录（新增）
    relevance_map: dict[str, float] = field(default_factory=dict)  # 语义相关度（新增）
```

### 7.2 ClosurePolicy（新增，嵌入 PruneConfig）

```python
@dataclass
class ClosurePolicy:
    """闭包求解策略参数"""
    
    # ── 阈值控制 ──
    core_threshold_factor: float = 0.75       # CORE = 最弱锚点 × 此值
    peripheral_threshold_factor: float = 0.50  # PERIPHERAL = CORE × 此值
    core_floor: float = 0.30                   # CORE 阈值保底
    peripheral_floor: float = 0.15             # PERIPHERAL 阈值保底
    
    # ── 独占性 ──
    exclusivity_include_threshold: float = 0.5  # PERIPHERAL 区域独占性高于此值 → include
    exclusivity_rule_threshold: float = 0.8     # 规则层 独占性高于此值 → include
    
    # ── 缺口仲裁 ──
    small_code_threshold: int = 20              # 行数低于此 → 直接 include
    infra_in_degree_threshold: int = 25         # 入度高于此 → 直接 stub
    prefer_stub: bool = True                    # 边界节点不确定时优先 stub
    max_gap_iterations: int = 3                 # 缺口仲裁最大迭代轮次
    
    # ── 闭包大小控制 ──
    max_closure_ratio: float = 0.5              # 代码行数占比硬上限（触发自动收紧）
    size_check_interval: int = 50               # 每增加 N 个节点检查一次大小
    
    # ── 用户控制 ──
    exclude_keywords: list[str] = field(default_factory=list)  # 排除关键词
```

---

## 八、协同开发清单

### 8.1 anchor.py 改动

**目标**: 输出 `query_embedding`，供闭包求解复用，避免重复 embed 调用。

```python
# AnchorLocator.locate() 返回值扩展
@dataclass
class AnchorOutput:
    anchors: list[AnchorResult]
    query_embedding: list[float]    # 新增
```

### 8.2 pipeline.py 改动

**目标**: 传递 `query_embedding` 给闭包求解。

```python
# _phase2_code_prune 修改
anchor_output = locator.locate(self.config.user_instruction)
anchors = anchor_output.anchors
query_embedding = anchor_output.query_embedding

solver = ClosureSolver(self.config, self.llm, self.graph)
closure = solver.solve(anchors, self.config.user_instruction, query_embedding)
```

### 8.3 surgeon.py 改动

**目标**: 支持 `stub_nodes` — 为桩节点生成空壳代码。

```python
def extract(self, closure: ClosureResult) -> Path:
    # ... 原有逻辑处理 required_nodes ...
    
    # 新增: 为 stub_nodes 生成桩代码
    stub_groups = self._group_by_file_stubs(closure.stub_nodes)
    for file_path, stub_node_ids in stub_groups.items():
        self._generate_stub_file(file_path, stub_node_ids)
```

不同语言的桩代码形式:

| 语言 | 函数桩 | 类桩 |
|------|--------|------|
| Python | `def foo(...): raise NotImplementedError("pruned")` | 类声明 + `pass` 方法 |
| Java/TS | 签名 + `throw new UnsupportedOperationException()` | 类/接口声明 + 空方法 |
| C/C++ | 声明 + `return 默认值;` | 头文件保留声明，源文件空实现 |
| JS | `function foo() { /* pruned */ }` | 类声明 + 空方法 |

### 8.4 prompts.py 改动

**目标**: 新增 `JUDGE_STRUCTURAL_GAP` prompt。

详见 §5.4 的 prompt 设计。

### 8.5 fixer.py + validator.py 改动

**目标**: stub 感知。

- `_check_fidelity`: 跳过 stub 文件/函数（它们不是原仓库代码但也不是幻觉）
- `BuildValidator.validate`: stub 导致的 `NotImplementedError` 不算错误
- `_check_completeness`: stub 节点不算功能缺失

### 8.6 config.py 改动

**目标**: 将 `ClosurePolicy` 嵌入 `PruneConfig`。

```python
@dataclass
class PruneConfig:
    # ... 原有字段 ...
    closure_policy: ClosurePolicy = field(default_factory=ClosurePolicy)  # 新增
```

---

## 九、风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| embedding 质量不足导致语义定界不准 | 错误地将核心代码放入 OUTSIDE | 自适应阈值以锚点为锚 + PERIPHERAL 作为缓冲区 + 缺口仲裁兜底 |
| 全节点 embedding 计算成本高 | Phase1 耗时增加 | Phase1 已经做了全节点 summary+embedding，无额外开销 |
| stub 代码与原代码类型不匹配 | 编译错误 | stub 保留原始签名 + Phase3 Heal 处理类型问题 |
| 独占性计算在稀疏图中不可靠 | 错误的 include/gap 决策 | 低置信时升级为 LLM 判断 |
| 阈值自适应在极端分布下失效 | CORE 过大或过小 | 设置 floor/ceiling 限制 + 闭包大小实时监控 |

---

## 十、各问题对照检查

| v1 问题 | v2 如何解决 | 状态 |
|---------|------------|------|
| #1 CLASS 无条件展开 | `_expand_class_children_if_full` 条件化 | ✅ 解决 |
| #2 import 时序问题 | CORE 区域不需符号传播，PERIPHERAL 滞后问题影响缩小 | ✅ 解决 |
| #3 软依赖二次扩展丢失 | 缺口仲裁迭代扩展（§5.5） | ✅ 解决 |
| #4 selected_summaries 过时 | 缺口合并后一次性仲裁，无分批上下文不一致问题 | ✅ 解决 |
| #5 type_only 降级不一致 | 统一走 `_peripheral_decision` 处理 | ✅ 解决 |
| #6 init.py 递归不完整 | 递归向上包含 + 路径索引查找 | ✅ 解决 |
| #7 auto_include 性能 | 路径索引替代全量扫描 | ✅ 解决 |
| #8 import * 无 __all__ | 缩小到 PERIPHERAL 才触发，影响有限 | ✅ 缓解 |
| #9 预警用节点数 | 改为代码行数 | ✅ 解决 |
| #10 批量 prompt 不统一 | 统一到 `Prompts.JUDGE_STRUCTURAL_GAP` | ✅ 解决 |
| #11 LLM 缺少上下文 | 缺口仲裁 prompt 含来源 + 其他调用者 | ✅ 解决 |
| #12 别名匹配 | 符号传播仅在 PERIPHERAL 触发，风险降低 | ⚠ 缓解 |
| #13 BFS 逻辑重复 | 统一 BFS 入口，无 `_expand_hard_deps` | ✅ 解决 |
| #14 硬依赖一刀切 | 语义区域决定传递行为，非边类型 | ✅ 解决 |
| #15 上帝节点 | 高入度自然落入 OUTSIDE → stub | ✅ 解决 |
| #16 固定判定策略 | ClosurePolicy 参数化 | ✅ 解决 |
