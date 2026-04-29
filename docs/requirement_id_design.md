# RequirementID 协同推导完整保留集：设计方案

> 版本: v1.0 | 日期: 2026-04-09
> 目标：使 CodePrune 在仅有高层用户指令时，通过 requirementID 追踪实现 InstructionAnalyzer ↔ AnchorLocator ↔ ClosureSolver 三阶段精确协同，自主推导完整保留集。

---

## 一、问题定义

### 1.1 当前系统的结构性盲区

当前数据流是**扁平化**的——子功能分解的结构信息在传递过程中逐步丢失：

```
SubFeature[] ──(丢失关联)──→ AnchorResult[] ──(丢失关联)──→ ClosureResult.required_nodes
```

| 断裂位置 | 表现 | 后果 |
|----------|------|------|
| `AnchorResult` 无 `sub_feature` 引用 | 闭包不知道哪个锚点服务于哪个需求 | BFS 是"全量一锅炖"，无法做 per-requirement 完整性校验 |
| `ClosureSolver` 只有 `features_text`（拼接字符串） | 缺口仲裁不知道缺口威胁的是哪个子功能 | LLM 缺乏精准裁决上下文 |
| 无 per-requirement 覆盖度检查 | 某个子功能的依赖链完全丢失时无法察觉 | shop 的"订单→商品"依赖被 out_of_scope 误删却无告警 |

### 1.2 笼统指令下的失败案例对照

以 instruction_rewrite_report 中的实际失败为参照：

| Benchmark | 笼统指令 | 遗漏模块 | 根因 |
|-----------|---------|----------|------|
| shop | "保留订单管理的完整功能链，不需要商品CRUD" | ProductService, Product, dao/ | "不需要商品CRUD" → LLM 将 ProductService 列入 out_of_scope，但 CartService 编译依赖它 |
| orchestrator | "保留本地工作流执行能力" | core/, backends/local, config | 只锚定到 workflows/，BFS 无边追踪到 core/ |
| ticketing | "保留核心审批主链" | events/, model/, api/ | 只锚定 service/ApprovalService，不知道它依赖 EventBus 和 model |
| dashboard | "保留图表仪表盘" | chart types, user 相关 | embedding 偏移导致部分文件遗漏，无完整性校验察觉不到 |

**核心矛盾**：当指令不直接列出文件名时，系统需要自己推导"这些子功能的代码是什么"。而推导的正确性取决于：
1. 子功能分解是否准确（InstructionAnalyzer）
2. 每个子功能的锚点是否完备（AnchorLocator）
3. 从锚点出发的依赖追踪是否能覆盖完整功能链（ClosureSolver）

三者缺一则链条断裂。**requirementID 是贯穿三者的追踪线**。

---

## 二、RequirementID 方案总览

### 2.1 核心思想

为每个 `SubFeature` 分配唯一的 `req_id`，并在全pipeline传播：

```
InstructionAnalyzer → SubFeature.req_id = "R1"
    ↓ 传递
AnchorLocator → AnchorResult.req_ids = ["R1"]
    ↓ 传递
ClosureSolver → node_requirements[node_id] = {"R1", "R2"}
    ↓ 回填
SubFeature.covered_nodes, SubFeature.coverage_ratio
```

### 2.2 设计原则

1. **叠加而非替代**：requirementID 是在现有 BFS/语义定界之上的追踪层，不改变核心算法
2. **优雅降级**：analysis=None 或 req_id 分配失败时，退化回现有逻辑，零降级
3. **可审计**：最终输出中每个保留的文件都能溯源到"为什么保留"（属于哪个 requirement）
4. **闭包级约束**：per-requirement 覆盖度检查可发现"某个子功能的依赖链断裂"

---

## 三、数据结构改造

### 3.1 config.py — SubFeature 增加 req_id 和回填字段

```python
@dataclass
class SubFeature:
    """一个独立的子功能需求（LLM grounded 分析产出）"""
    req_id: str = ""                  # ← 新增: "R1", "R2" ...
    description: str = ""
    root_entities: list[str] = field(default_factory=list)
    reasoning: str = ""
    # 闭包完成后回填（审计用）
    covered_nodes: set[str] = field(default_factory=set)
    coverage_ratio: float = 0.0
```

### 3.2 anchor.py — AnchorResult 增加 req_ids

```python
@dataclass
class AnchorResult:
    """锚点定位结果"""
    node_id: str
    node: CodeNode
    relevance_score: float
    confidence: float
    reason: str
    req_ids: list[str] = field(default_factory=list)   # ← 新增: 该锚点服务的需求
```

### 3.3 closure.py — ClosureResult 增加 node_requirements

```python
@dataclass
class ClosureResult:
    """闭包求解结果"""
    required_nodes: set[str] = field(default_factory=set)
    stub_nodes: set[str] = field(default_factory=set)
    excluded_edges: list[tuple[str, str]] = field(default_factory=list)
    # 审计
    soft_included: set[str] = field(default_factory=set)
    soft_excluded: set[str] = field(default_factory=set)
    structural_gaps: list[StructuralGap] = field(default_factory=list)
    relevance_map: dict[str, float | None] = field(default_factory=dict)
    # ← 新增
    node_requirements: dict[str, set[str]] = field(default_factory=dict)  # node_id → {req_ids}
```

### 3.4 StructuralGap 增加 req_id

```python
@dataclass
class StructuralGap:
    source: str
    target: str
    edge: Edge
    target_scope: str
    req_id: str = ""   # ← 新增: 产生此缺口的需求
```

---

## 四、各阶段改造详设

### 4.1 InstructionAnalyzer — 生成 req_id

在 `_parse_analysis()` 中自动分配：

```python
def _parse_analysis(self, result, candidates, user_instruction):
    # ... existing validation ...
    
    for i, sf_raw in enumerate(result.get("sub_features", []), 1):
        # ... existing root_entities validation ...
        
        sub_features.append(SubFeature(
            req_id=f"R{i}",                    # ← 自动分配
            description=sf_raw.get("description", ""),
            root_entities=valid_roots,
            reasoning=sf_raw.get("reasoning", ""),
        ))
    
    # ... rest unchanged ...
```

无 LLM 改动，纯机械分配。

### 4.2 AnchorLocator — 关联 req_id

在 `_locate_from_analysis()` 中记录来源映射：

```python
def _locate_from_analysis(self, analysis, query_emb):
    all_candidates: dict[str, float] = {}
    anchor_req_map: dict[str, set[str]] = {}   # ← 新增: node_id → req_ids

    for sf in analysis.sub_features:
        # 来源 1: LLM 直选的 root_entities
        for qname in sf.root_entities:
            node = self._find_by_qualified_name(qname)
            if node:
                all_candidates[node.id] = max(all_candidates.get(node.id, 0), 0.95)
                anchor_req_map.setdefault(node.id, set()).add(sf.req_id)

        # 来源 2: 子功能描述的 embedding 检索
        sf_emb = self.llm.embed([sf.description])[0]
        # ... existing hit logic ...
        for nid, score in hits:
            if nid not in excluded:
                all_candidates[nid] = max(all_candidates.get(nid, 0), score)
                anchor_req_map.setdefault(nid, set()).add(sf.req_id)

    # 保存映射到实例变量，供 locate() 主流程使用
    self._anchor_req_map = anchor_req_map
    
    # ... rest unchanged ...
```

在 `locate()` 的 Step 3 (LLM 验证) 之后，将 req_ids 写入 AnchorResult：

```python
# 在创建 AnchorResult 时：
anchors.append(AnchorResult(
    node_id=node_id,
    node=node,
    relevance_score=score,
    confidence=confidence,
    reason=verification.get("reason", ""),
    req_ids=list(self._anchor_req_map.get(node_id, [])),  # ← 注入 req_ids
))
```

### 4.3 ClosureSolver — Per-Requirement 标注（P1: 轻量方案）

**不改变 BFS 算法**，只在现有全量 BFS 的每个 include 决策处打 req_id 标签：

```python
def _semantic_bfs(self, anchors, scope, result, max_depth, excluded_dirs):
    # 初始化锚点的 requirement 标注
    for a in anchors:
        result.node_requirements.setdefault(a.node_id, set()).update(a.req_ids)
    
    queue = deque((a.node_id, 0) for a in anchors)
    result.required_nodes.update(a.node_id for a in anchors)
    
    while queue:
        nid, depth = queue.popleft()
        source_reqs = result.node_requirements.get(nid, set())
        
        for edge in self.graph.get_outgoing(nid):
            target = edge.target
            if target in result.required_nodes:
                # 已在闭包中，但可能需要追加 req_ids
                result.node_requirements.setdefault(target, set()).update(source_reqs)
                continue
            
            # ... existing scope-based decision logic ...
            
            if decision == "include":
                result.required_nodes.add(target)
                queue.append((target, depth + 1))
                # ← 传播 req_ids
                result.node_requirements.setdefault(target, set()).update(source_reqs)
```

**关键**：req_id 沿依赖边传播，最终每个 required_node 都知道"我被哪些 requirement 需要"。

### 4.4 Per-Requirement 完整性校验（P2）

在 BFS 完成后、gap 仲裁之后、surgeon 之前执行：

```python
def _verify_requirement_completeness(self, analysis, result, scope):
    """检查每个 sub_feature 的功能链完整度"""
    if not analysis or not analysis.sub_features:
        return
    
    for sf in analysis.sub_features:
        # 该需求拥有的闭包节点
        req_nodes = {nid for nid, reqs in result.node_requirements.items()
                     if sf.req_id in reqs}
        
        # 该需求相关的 CORE 节点（用子功能 description 的 embedding 匹配）
        sf_emb = self.llm.embed([sf.description])[0]
        req_core = set()
        for nid in scope.core:
            node = self.graph.get_node(nid)
            if node and node.embedding is not None:
                sim = self._cosine_sim(node.embedding, sf_emb)
                if sim >= self.policy.core_floor:
                    req_core.add(nid)
        
        covered = req_core & req_nodes
        coverage = len(covered) / max(len(req_core), 1)
        
        # 回填审计信息
        sf.covered_nodes = covered
        sf.coverage_ratio = coverage
        
        logger.info(
            f"[{sf.req_id}] '{sf.description}' 覆盖率: {coverage:.0%} "
            f"({len(covered)}/{len(req_core)} CORE 节点)"
        )
        
        if coverage < 0.5 and req_core - covered:
            logger.warning(
                f"[{sf.req_id}] 覆盖率不足，触发定向补全: "
                f"缺失 {len(req_core - covered)} 个 CORE 节点"
            )
            self._targeted_recovery(sf, req_core - covered, result, scope)

def _targeted_recovery(self, sf, missing_core_nodes, result, scope):
    """针对性补全：对覆盖不足的子功能，从缺失的 CORE 节点出发反向追踪"""
    recovered = 0
    for nid in missing_core_nodes:
        node = self.graph.get_node(nid)
        if not node:
            continue
        
        # 检查：缺失节点是否被闭包内的节点 import/call？
        incoming = self.graph.get_incoming(nid)
        has_closure_caller = any(
            e.source in result.required_nodes for e in incoming
        )
        
        if has_closure_caller:
            # 被闭包内节点依赖但自身不在闭包中 → 大概率是遗漏
            result.required_nodes.add(nid)
            result.node_requirements.setdefault(nid, set()).add(sf.req_id)
            recovered += 1
        else:
            # 不被闭包内节点依赖 → 可能是独立入口，检查它是否依赖闭包内节点
            outgoing = self.graph.get_outgoing(nid)
            deps_in_closure = sum(1 for e in outgoing if e.target in result.required_nodes)
            if deps_in_closure >= 2:
                # 该节点依赖 ≥2 个闭包内节点 → 大概率属于同一功能
                result.required_nodes.add(nid)
                result.node_requirements.setdefault(nid, set()).add(sf.req_id)
                recovered += 1
    
    if recovered:
        logger.info(f"[{sf.req_id}] 定向补全恢复 {recovered} 个节点")
```

### 4.5 缺口仲裁增强（P3）

在 gap 产生时标注 req_id：

```python
# _semantic_bfs 中产生 gap 时：
structural_gaps.append(StructuralGap(
    source=nid, target=target, edge=edge,
    target_scope="peripheral",
    req_id=",".join(source_reqs) if source_reqs else "",  # ← 标注
))
```

在 LLM 仲裁 prompt 中注入 requirement 上下文：

```python
def _format_gap_for_llm(self, gap, analysis):
    """增强 gap 描述，加入 requirement 上下文"""
    base_desc = f"{source_name} → {target_name} ({edge_type})"
    
    if gap.req_id and analysis:
        req_ids = gap.req_id.split(",")
        req_descs = []
        for sf in analysis.sub_features:
            if sf.req_id in req_ids:
                req_descs.append(f"[{sf.req_id}] {sf.description}")
        if req_descs:
            base_desc += f"\n  Affects requirements: {'; '.join(req_descs)}"
    
    return base_desc
```

**预期效果**：LLM 看到 "CartService → ProductService (IMPORTS), Affects requirements: [R1] 购物车下单功能" 时，比看到 "CartService → ProductService (IMPORTS)" 的裁决准确率大幅提升。

---

## 五、交叉依赖检测（P4: 独立 per-req BFS 方案）

> P4 是架构性改变，建议在 P0-P3 验证 rentability 后再做。

### 5.1 核心思路

不是"一次 BFS 从所有锚点出发"，而是"每个 requirement 独立 BFS → 合并 + 交叉检验"：

```python
def solve(self, anchors, ...):
    # 按 req_id 分组锚点
    req_groups = {}
    for a in anchors:
        for rid in (a.req_ids or [""]):
            req_groups.setdefault(rid, []).append(a)
    
    per_req_closures: dict[str, set[str]] = {}
    all_gaps = []
    
    for req_id, req_anchors in req_groups.items():
        closure, gaps = self._semantic_bfs(req_anchors, scope, ...)
        per_req_closures[req_id] = closure
        for g in gaps:
            g.req_id = req_id
        all_gaps.extend(gaps)
    
    # 合并
    result.required_nodes = set().union(*per_req_closures.values())
    
    # 交叉依赖检测
    self._cross_requirement_check(per_req_closures, result)
```

### 5.2 交叉依赖检测

检测 requirement A 的闭包是否依赖闭包外的编译级节点：

```python
def _cross_requirement_check(self, per_req_closures, result):
    """跨 requirement 编译级依赖强制恢复"""
    for req_id, closure in per_req_closures.items():
        for nid in closure:
            for edge in self.graph.get_outgoing(nid):
                target = edge.target
                if target in result.required_nodes:
                    continue  # 已在某个 requirement 的闭包中
                
                target_node = self.graph.get_node(target)
                if not target_node:
                    continue
                
                # 编译级依赖: IMPORTS + INHERITS + type-import
                if self._is_compile_dep(edge):
                    result.required_nodes.add(target)
                    result.node_requirements.setdefault(target, set()).add(req_id)
                    logger.info(
                        f"交叉依赖恢复 [{req_id}]: {nid} → {target} "
                        f"(edge={edge.edge_type.value})"
                    )

def _is_compile_dep(self, edge):
    """判断边是否为编译级依赖"""
    if edge.edge_type == EdgeType.INHERITS:
        return True
    if edge.edge_type == EdgeType.IMPORTS:
        # 导入的符号是类型名（首字母大写）→ 编译级
        imported = edge.metadata.get("imported_symbols", [])
        if any(s and s[0].isupper() for s in imported):
            return True
        # 无符号信息 → 保守视为编译级
        if not imported:
            return True
    return False
```

### 5.3 P4 的优势与风险

| 优势 | 风险 |
|------|------|
| 每个 requirement 的闭包边界独立、清晰 | 多次 BFS 成本（N 倍节点遍历） |
| 交叉检测可精确定位"哪个需求导致哪个文件被恢复" | requirement 分组可能不均衡（1个锚点 vs 8个） |
| per-req closure 可直接生成审计报告 | 独立 BFS 的 scope 分类可能需要 per-req 调整 |

---

## 六、与现有机制的完整协同流程

```
┌──────────────────────────────────────────────────────────────────────┐
│  Phase 2.0: InstructionAnalyzer                                       │
│                                                                       │
│  用户指令: "保留订单管理的完整功能链，不需要商品CRUD和用户注册登录"      │
│                                                                       │
│  → SubFeature[]:                                                      │
│    R1: "购物车下单" → roots: [CartService.checkout, OrderService.create]│
│    R2: "订单状态流转" → roots: [OrderService.updateStatus]             │
│    R3: "支付退款"    → roots: [PaymentService.process, .refund]        │
│  → out_of_scope: [ProductController.java, UserController.java,        │
│                    UserService.java]                                   │
│  → anchor_strategy: "distributed"                                     │
└───────────────────┬──────────────────────────────────────────────────┘
                    │ req_id 传递
                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Phase 2.1: AnchorLocator                                             │
│                                                                       │
│  每个锚点标注 req_ids:                                                │
│    CartService.checkout      → req_ids=[R1]                           │
│    OrderService.createOrder  → req_ids=[R1, R2]                       │
│    OrderService.updateStatus → req_ids=[R2]                           │
│    PaymentService.process    → req_ids=[R3]                           │
│    PaymentService.refund     → req_ids=[R3]                           │
│                                                                       │
│  A2: closure_query_embedding 基于 R1+R2+R3 的 description             │
└───────────────────┬──────────────────────────────────────────────────┘
                    │ 带 req_ids 的锚点
                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Phase 2.2: ClosureSolver                                             │
│                                                                       │
│  Step 1: 语义定界 (不变)                                              │
│    CORE / PERIPHERAL / OUTSIDE 分类                                   │
│                                                                       │
│  Step 2: 语义引导 BFS (增强: req_id 传播)                             │
│    R1: CartService.checkout                                           │
│      → IMPORTS CartItem [R1]                                          │
│      → CALLS ProductService.getById [R1]  ← PERIPHERAL, include      │
│      → CALLS OrderService.createOrder [R1] ← CORE, include           │
│    R2: OrderService.updateStatus                                      │
│      → IMPORTS Order [R2]                                             │
│      → IMPORTS OrderStatus [R2]                                       │
│    R3: PaymentService.process                                         │
│      → IMPORTS Payment [R3]                                           │
│      → IMPORTS Order [R1,R2,R3]  ← 追加 R3                           │
│                                                                       │
│  Step 2.5: CORE 包含校验 (不变)                                       │
│                                                                       │
│  Step 3: 缺口仲裁 (增强: req_id 上下文)                               │
│    Gap: CartService → ProductService                                  │
│      LLM 看到: "Affects [R1] 购物车下单"                              │
│      → 裁决: include (CartService 编译依赖 ProductService)            │
│                                                                       │
│  Step 3.5: Per-Requirement 完整性校验 (新增)                          │
│    R1: 覆盖 4/5 CORE 节点 (80%) ✓                                    │
│    R2: 覆盖 3/3 CORE 节点 (100%) ✓                                   │
│    R3: 覆盖 3/3 CORE 节点 (100%) ✓                                   │
│                                                                       │
│  Step 4: 后处理 (不变)                                                │
│    containment chain / __init__.py / barrel / class 升级              │
│                                                                       │
│  输出:                                                                │
│    required_nodes: {CartService, OrderService, PaymentService,        │
│                     ProductService, CartItem, Order, Payment,         │
│                     OrderStatus, BaseDao, ...}                        │
│    node_requirements: {                                               │
│      CartService: {R1},                                               │
│      OrderService: {R1, R2},                                          │
│      ProductService: {R1},     ← 被交叉恢复                          │
│      Payment: {R3},                                                   │
│      Order: {R1, R2, R3},      ← 多需求共享                          │
│      ...                                                              │
│    }                                                                  │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 七、解决的失败模式映射

| 失败模式 (instruction_rewrite_report) | requirementID 如何解决 |
|---------------------------------------|----------------------|
| **shop: ProductService 被 out_of_scope 误删** | R1 的 BFS 沿 CartService→ProductService 的 IMPORTS 边传播 req_id；即使 ProductService 在 out_of_scope 中，也因"编译级依赖"被交叉检测恢复 |
| **orchestrator: core/ 整体遗漏** | R1("billing workflow") 的 BFS 从 billing.py → import core.executor → executor.py 自然追踪进入 core/，req_id 标注为 R1 |
| **ticketing: events/model/api 遗漏** | R1("提交工单") 从 TicketService → EventBus → TicketSubmittedEvent 逐层展开，per-req 完整性校验发现 R1 的事件链覆盖不足则触发补全 |
| **dashboard: chart types 遗漏** | per-req 完整性校验发现 R_charts 在 CORE 节点中覆盖率 <50% → targeted_recovery 从 CORE 中缺失的 chart type 节点反向追踪恢复 |
| **缺口仲裁方向性缺失** | gap prompt 中注入 "[R1: 购物车下单]的缺口" 而非"订单管理的完整功能链"，LLM 裁决的信息粒度提高一个量级 |
| **过度保留 (Precision 低)** | per-req 标注使得每个 required_node 都有明确归因；未被任何 req 标注的节点可被安全降级，提升 precision |

---

## 八、前置条件

| 前置 | 状态 | 说明 |
|------|------|------|
| **跨文件依赖边** (P0 from instruction_closure_design) | 当前 `initial_granularity=function` (yaml) | BFS 需要 IMPORTS/CALLS 边；如果是 `file` 粒度 + `lazy_resolution=false`，per-req BFS 仍然无边可走 |
| **prompt GROUNDING RULE 放松** (P1) | 未修改 | 当前规则阻止 LLM 推断隐式依赖，导致 out_of_scope 过宽；需要改为"功能排除 ≠ 代码排除"语义 |
| **UNDERSTAND_INSTRUCTION prompt 支持 req_id** | 无需修改 | req_id 是代码层分配，不需要 LLM 生成 |

---

## 九、实现优先级与改动量估计

| 优先级 | 内容 | 改动文件 | 预估行数 | 破坏性 |
|--------|------|----------|----------|--------|
| **P0** | `SubFeature.req_id` + `AnchorResult.req_ids` + `ClosureResult.node_requirements` | config.py, anchor.py, closure.py | ~20 行 | 纯新增字段 |
| **P1** | BFS 中 req_id 传播标注 | closure.py `_semantic_bfs()` | ~15 行 | 在现有 include 决策后追加一行标注 |
| **P2** | per-req 完整性校验 + targeted_recovery | closure.py (新方法) | ~60 行 | 新增方法，在 solve() 尾部调用 |
| **P3** | 缺口仲裁 prompt 增强 | closure.py `_arbitrate_gaps()`, prompts.py | ~30 行 | 增强现有 prompt 的上下文 |
| **P4** | 独立 per-req BFS + cross-requirement check | closure.py (重构 solve) | ~100 行 | 架构性改变，建议后置 |
| **总计 P0-P3** | | 3 个文件 | **~125 行** | **低风险** |

---

## 十、可生成的审计产物

有了 `node_requirements` 映射，可在输出中自动生成：

```markdown
## 需求追踪报告

### R1: 购物车下单
覆盖率: 90% (9/10 CORE 节点)
保留文件: CartService.java, OrderService.java, ProductService.java(恢复), 
          CartItem.java, Order.java, Product.java(恢复)
注意: ProductService.java 原在 out_of_scope 中，因 CartService 编译依赖被恢复

### R2: 订单状态流转
覆盖率: 100% (3/3 CORE 节点)
保留文件: OrderService.java, Order.java, OrderStatus.java

### R3: 支付退款
覆盖率: 100% (3/3 CORE 节点) 
保留文件: PaymentService.java, Payment.java, Order.java

### 共享节点
Order.java: R1, R2, R3 (核心数据模型，被所有需求共享)
BaseDao.java: R1, R2, R3 (持久化基础设施)
```

此报告可供用户快速审核"系统是否正确理解了我的需求"。

---

## 十一、与 EXCLUSION SEMANTICS 的协同

instruction_closure_design.md 中提出的 P1 (prompt 语义区分)：

```
用户说"不需要商品CRUD" → 排除 ProductController (入口) 
                        → 不排除 ProductService (被其他功能依赖)
```

与 requirementID 的协同关系：

1. **P1 减少误排除**：prompt 改进后，out_of_scope 不再包含 ProductService → BFS 自然能到达
2. **requirementID 作为兜底**：即使 P1 未完全修复（prompt 不稳定），交叉依赖检测仍能发现 R1→ProductService 的编译级依赖并恢复
3. **双重保障**：P1 + requirementID 形成"源头控制 + 兜底恢复"的纵深防线

---

## 十二、风险评估

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| LLM 子功能分解质量低 | 中 | per-req 标注不准确 | analysis=None 时全量退化；per-req 只是追踪标签不影响 BFS 决策 |
| per-req 完整性校验过度恢复 | 低 | Precision 下降 | coverage < 0.5 阈值保守；targeted_recovery 仅恢复有闭包调用关系的节点 |
| 多 requirement 共享节点的 req_id 污染 | 低 | 审计不精确 | 正常行为——Order.java 同时属于 R1/R2/R3 是正确的 |
| per-req embedding 计算成本 | 低 | 完整性校验时额外 N 次 embed | N = sub_features 数量（通常 2-5），embed 是 batch 低成本操作 |
