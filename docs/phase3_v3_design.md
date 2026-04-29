# Phase3 改进方案 V3：可预测裁剪 · 机械修复

> 基于 CodePrune V2.3 实际失败模式 + GitNexus 工程实践选择性借鉴

---

## 一、设计理念

Phase3 不可靠的根源不在修复能力不足——而在于它收到的是一个**受损且无标注的**工件。它必须用 LLM 去诊断"哪坏了、为什么坏了、怎么修"，而 LLM 修复天生不确定。

**核心判断：增强 LLM 修复是死胡同。正确方向是消除 LLM 修复的必要性。**

设计原则：
1. Phase2 的每一刀都必须"自注释"——Surgeon 裁掉什么，同步记录什么
2. 能机械修的不用 LLM——import 删除、stub 生成、`__init__` 修补全部确定性完成
3. LLM 只处理残余的真正歧义——预期 <20% 的修复需要 LLM 参与
4. 语义完整由 Phase2 负责，语法完整由机械层负责——职责不混淆

## 二、问题诊断——"不能跑"的根因链

根据 V2.3 benchmark（F1=97%）和 `fix_plan.md`，梳理"输出不能跑"的根因链：

| # | 根因 | 表现 | 发生阶段 | 当前应对 | 可靠性 |
|---|------|------|---------|---------|--------|
| R1 | 锚点覆盖不足 | 该选的文件没选（FN） | Phase2 AnchorLocator | LLM 多轮验证 | 中（LLM 非确定性） |
| R2 | 闭包断裂 | 硬依赖未被传递拉入 | Phase2 ClosureSolver | R_OUT 规则 + LLM 仲裁 | 中→高（R_OUT 已改善） |
| R3 | Surgeon 断口无标注 | import 指向已删文件、引用已删符号 | Phase2 Surgeon → Phase3 | Phase3 build 循环 + LLM 修补 | **低** |
| R4 | 完整性判断失真 | LLM 说"缺 X"但 X 已存在 / 描述性输出 | Phase3 Completeness | U3 结构化约束 | **低** |

**关键发现**：R1 和 R2 已通过 V2.1-V2.3 的修复大幅改善。**残余瓶颈集中在 R3 和 R4**。

---

## 三、方案架构

```
Phase2 改善             Phase2→Phase3 桥接        Phase2.5 (新增)       Phase3 (瘦身)
┌───────────────┐      ┌──────────────┐          ┌────────────────┐    ┌──────────────┐
│ M1: 锚点扩展  │      │              │          │                │    │              │
│ (入口点启发)  │      │              │          │                │    │ Build 验证   │
│               │      │ M3: Cut      │    ──→   │ M4: Mechanical │──→ │ UndefinedName│
│ M2: 边置信度  │      │    Manifest   │          │    Healer      │    │ Fidelity 回滚│
│ (分层权重)    │      │ (裁剪清单)   │          │ (机械修复层)   │    │ (结构化完整性│
│               │      │              │          │                │    │  替代LLM完整) │
└───────────────┘      └──────────────┘          └────────────────┘    └──────────────┘
```

五个模块按依赖关系排列：M1/M2 独立可做 → M3 依赖 Surgeon → M4 依赖 M3 → Phase3 瘦身依赖 M4。

---

## 四、模块详细设计

### 模块一 (M1)：锚点扩展——入口点评分启发式

**灵感来源**：GitNexus `process-processor.ts` 的入口点打分（call ratio + export status + name patterns）

**要解决的问题**：V2.3 Dashboard FN=2，根因是 LLM 把"保留图表功能"映射到渲染组件但漏掉了 `statsApi.ts`（数据获取层）。这是系统性问题——LLM 倾向于锚定"名字最像指令"的实体，但漏掉功能链上的数据源。

**设计**：在 AnchorLocator 的 `locate()` 末尾，对 LLM 选出的锚点做一轮**调用链扩展**：

```python
# anchor.py → locate() 末尾新增

def _expand_anchors_by_call_chain(self, anchors: list[str], graph: CodeGraph) -> list[str]:
    """基于调用比启发式扩展锚点覆盖"""
    expanded = set(anchors)
    for anchor_id in anchors:
        # 收集锚点直接 CALLS/IMPORTS 的目标
        for edge in graph.get_outgoing(anchor_id):
            if edge.edge_type not in (EdgeType.CALLS, EdgeType.IMPORTS):
                continue
            target = graph.get_node(edge.target)
            if not target or target.id in expanded:
                continue
            
            # 入口点评分：被多少锚点调用 / 总入度
            anchor_call_count = sum(
                1 for e in graph.get_incoming(target.id)
                if e.source in expanded and e.edge_type in (EdgeType.CALLS, EdgeType.IMPORTS)
            )
            total_in_degree = len(graph.get_incoming(target.id))
            
            # 如果被 ≥2 个锚点调用，或被锚点大量引用（exclusivity > 50%），扩展为锚点
            exclusivity = anchor_call_count / max(total_in_degree, 1)
            if anchor_call_count >= 2 or exclusivity > 0.5:
                expanded.add(target.id)
    
    return list(expanded)
```

**约束**：
- 只扩展一跳（不做递归），防止爆炸
- 只看 CALLS/IMPORTS 硬边，不看 SEMANTIC_RELATED
- 扩展的是 Phase2 的起始锚点集，不改变闭包逻辑

**预期效果**：Dashboard 场景中，`BarChart` 和 `LineChart`（已选锚点）都 IMPORTS `statsApi.ts` → `statsApi.ts` 的 anchor_call_count=2 → 自动扩展为锚点 → 闭包从 `statsApi.ts` 出发拉入 `client.ts`。

**改动文件**：`anchor.py`（+~40 行）
**风险**：低。只增加锚点，不删除 LLM 已选的锚点。最坏情况是多选了一些文件（FP 微升），但 Surgeon 会做函数级裁剪。

---

### 模块二 (M2)：边置信度分层

**灵感来源**：GitNexus `CodeRelation` 表的 `confidence: DOUBLE` 字段，下游决策按置信度区分对待。

**要解决的问题**：CodePrune 的 `Edge` 有 `confidence` 字段，但 Phase2 的 `_peripheral_decision()` 和 `_rule_arbitrate()` **几乎不使用它**——决策完全基于 `edge_type` 和 exclusivity。这意味着 tree-sitter 解析的确定性 CALLS 边和 LLM 推断的 SEMANTIC_RELATED 边被同等对待。

**设计**：

#### 2.1 Phase1 建图时赋初始置信度

```python
# graph_builder.py 中，创建 Edge 时按来源赋值

CONFIDENCE_MAP = {
    # tree-sitter 解析的结构性边 → 高置信
    (EdgeType.CONTAINS, "ast"):     1.0,
    (EdgeType.IMPORTS, "ast"):      0.95,
    (EdgeType.CALLS, "ast"):        0.90,
    (EdgeType.INHERITS, "ast"):     0.95,
    (EdgeType.IMPLEMENTS, "ast"):   0.95,
    (EdgeType.USES, "ast"):         0.85,
    # LLM/embedding 推断的语义边 → 低置信
    (EdgeType.SEMANTIC_RELATED, "embedding"): 0.50,
    (EdgeType.COOPERATES, "llm"):   0.40,
}
```

#### 2.2 Phase2 BFS 中使用置信度

```python
# closure.py → _peripheral_decision() 修改

def _peripheral_decision(self, node_id, edge, source_id):
    # 现有逻辑保持不变，但增加低置信度快速跳过
    if edge.confidence < 0.6:
        # 低置信边（SEMANTIC_RELATED/COOPERATES）在 PERIPHERAL 区不驱动扩展
        return "skip"
    
    # ... 现有的 edge_type 判断逻辑 ...
```

#### 2.3 Phase2 缺口仲裁中使用置信度

```python
# closure.py → _rule_arbitrate() 中，R3（高独占性→include）增加置信度门槛

# R3: 只有高置信硬边的高独占性才触发自动 include
if exclusivity > threshold and gap.max_edge_confidence > 0.8:
    return "include"
# 低置信高独占仍走 LLM 仲裁
```

**改动文件**：
- `graph_builder.py`（+10 行）
- `closure.py`（修改 ~15 行）

**风险**：低-中。需要验证置信度阈值不会误伤正常的 CALLS 边。建议先添加日志统计各置信度区间的边数量，再调参。

---

### 模块三 (M3)：裁剪清单 (CutManifest)

**灵感来源**：GitNexus `detectChanges()` 的"物理变更 → 图语义变更"映射思路。

**要解决的问题**：Surgeon 切完代码后，Phase3 不知道切了什么。它必须通过编译错误来"反向发现"断口——每个发现成本是一轮 LLM 调用。

**设计**：

```python
# 新增数据结构（可放在 schema.py 或单独文件）

@dataclass
class CutRecord:
    file_path: str              # 受影响的保留文件
    cut_type: str               # "removed_import" | "removed_symbol" | "partial_class" | "removed_file"
    removed_name: str           # 被删的符号/模块名
    referenced_by: list[str]    # 哪些保留代码引用了它
    signature: str | None       # Phase1 图中已有的签名信息（CodeNode.signature）
    language: str               # 语言（决定 stub 模板）

@dataclass
class CutManifest:
    cuts: list[CutRecord]
    included_files: set[str]    # 最终输出的文件集
    excluded_files: set[str]    # 被排除的文件集
```

**记录时机**（嵌入 Surgeon 已有流程，不新增扫描）：

| Surgeon 现有操作 | 对应 CutRecord.cut_type | 信息来源 |
|------------------|------------------------|---------|
| 注释掉 import 语句 | `removed_import` | 被注释的 import 目标模块名 |
| 通过 `_partial_extract` 排除某个函数/方法 | `removed_symbol` | 图中的 `CodeNode.signature` 字段 |
| 部分提取类（删部分方法）| `partial_class` | 被删方法列表 + 各方法签名 |
| 整个文件不在闭包输出中 | `removed_file` | 闭包排除列表 vs 输出文件集 diff |

**改动文件**：`surgeon.py`（+~80 行，在各操作点增加 `manifest.cuts.append()`）
**风险**：极低。纯增量，不修改已有逻辑。

---

### 模块四 (M4)：机械修复层 (MechanicalHealer)

**新建文件**：`core/heal/mechanical_healer.py`（~200 行）

读取 CutManifest，每种断口类型对应一个确定性修复策略：

#### 策略 1：Import 清理（`removed_import` + `removed_file`）

```python
def _fix_imports(self, sub_repo: Path, manifest: CutManifest):
    """删除/注释指向已排除模块的 import 语句"""
    for cut in manifest.cuts:
        if cut.cut_type not in ("removed_import", "removed_file"):
            continue
        for ref_file in cut.referenced_by:
            file_path = sub_repo / ref_file
            if not file_path.exists():
                continue
            content = file_path.read_text(encoding="utf-8")
            new_content = self._remove_import_statement(
                content, cut.removed_name, cut.language
            )
            if new_content != content:
                file_path.write_text(new_content, encoding="utf-8")
                self.stats["imports_fixed"] += 1
```

消除 Phase3 第一轮几乎总在做的 import 修复（blog 首轮 11 行注释、compiler 首轮 build 修复）。

#### 策略 2：签名 Stub（`removed_symbol` 有签名的情况）

```python
STUB_TEMPLATES = {
    "python": "def {name}{params}:\n    raise NotImplementedError('{name} was pruned')\n",
    "java":   "{return_type} {name}{params} {{ throw new UnsupportedOperationException(); }}\n",
    "typescript": "export function {name}{params}: never {{ throw new Error('pruned'); }}\n",
    "c":      "{return_type} {name}{params}; /* pruned stub */\n",
}

def _generate_stubs(self, sub_repo: Path, manifest: CutManifest):
    """为有签名的被删符号生成最小 stub"""
    for cut in manifest.cuts:
        if cut.cut_type != "removed_symbol" or not cut.signature:
            if cut.cut_type == "removed_symbol" and not cut.signature:
                self.needs_llm.append(cut)  # 无签名，留给 Phase3
            continue
        
        template = STUB_TEMPLATES.get(cut.language)
        if not template:
            self.needs_llm.append(cut)
            continue
        
        stub_code = self._render_stub(template, cut)
        for ref_file in cut.referenced_by:
            self._insert_stub(sub_repo / ref_file, stub_code, cut.language)
            self.stats["stubs_generated"] += 1
```

**关键约束**：
- Stub 只用于编译通过，不保证运行正确（`raise NotImplementedError`）
- 签名来自 Phase1 图中 `CodeNode.signature`（已有数据）
- 无签名的 cut → 标记 `needs_llm=True`，留给 Phase3

#### 策略 3：模块级修补

```python
def _fix_module_structure(self, sub_repo: Path, manifest: CutManifest):
    """修补 Python __init__.py / __all__ 等模块结构"""
    for cut in manifest.cuts:
        if cut.cut_type == "removed_file" and cut.language == "python":
            # __init__.py 中移除指向已删模块的 re-export
            init_py = sub_repo / Path(cut.removed_name).parent / "__init__.py"
            if init_py.exists():
                self._clean_init_exports(init_py, Path(cut.removed_name).stem)
        
        if cut.cut_type == "partial_class":
            # 为被删方法插入 stub method
            for ref_file in cut.referenced_by:
                self._insert_class_method_stubs(sub_repo / ref_file, cut)
```

#### 策略 4：统计与透传

```python
def heal(self, sub_repo: Path, manifest: CutManifest) -> MechanicalHealResult:
    """机械修复入口"""
    self._fix_imports(sub_repo, manifest)
    self._generate_stubs(sub_repo, manifest)
    self._fix_module_structure(sub_repo, manifest)
    self._fix_c_headers(sub_repo, manifest)
    
    return MechanicalHealResult(
        stats=self.stats,
        needs_llm=self.needs_llm,       # 无签名的 cut，需要 Phase3 LLM 处理
        cuts_total=len(manifest.cuts),
        cuts_resolved=self.stats["imports_fixed"] + self.stats["stubs_generated"],
    )
```

**改动文件**：新建 `core/heal/mechanical_healer.py`（~200 行）
**风险**：低。纯确定性逻辑，不修改已有文件。

---

### 模块五 (M5)：Phase3 瘦身

**改动文件**：`fixer.py`（修改 ~50 行）

#### 5.1 替换 LLM 完整性层 → 结构化完整性检查

```python
def _structural_completeness_check(self, sub_repo: Path, manifest: CutManifest) -> list[str]:
    """基于 CutManifest 的结构化完整性检查（替代 LLM 完整性层）"""
    issues = []
    for cut in manifest.cuts:
        if cut.cut_type == "removed_symbol" and not cut.signature:
            # 被删符号没有签名 → stub 不完整
            issues.append(f"MISSING_STUB: {cut.file_path}:{cut.removed_name}")
        if cut.cut_type == "removed_file":
            # 被删文件仍被 import → 检查 import 是否已清理
            for ref in cut.referenced_by:
                if self._still_imports(sub_repo / ref, cut.removed_name):
                    issues.append(f"STALE_IMPORT: {ref} → {cut.removed_name}")
    return issues
```

这个检查是**确定性的**——不会产生幻觉，不会返回描述性文本。

#### 5.2 UndefinedNames 层增强——注入 CutManifest 上下文

```python
# 现有 Phase C prompt（模糊）：
# "foo is undefined in bar.py. Here's the file content..."

# 新 prompt（精确）：
# "foo is undefined in bar.py.
#  Context: It was removed because closure excluded utils.py.
#  Original signature: def foo(x: int, y: str) -> bool
#  Options: 1) Add needed import  2) Generate stub with above signature  3) Remove reference"
```

#### 5.3 保留并强化的层

| 层 | 保留？ | 变化 |
|----|--------|------|
| **Build** | ✅ 保留 | 预期错误数大幅减少（M4 已处理 70%+） |
| **UndefinedNames** | ✅ 保留 | 增加 CutManifest 上下文（5.2） |
| **Completeness** | ⚠️ 替换 | LLM 判断 → 结构化检查（5.1） |
| **Fidelity** | ✅ 保留 | 不变（检测 LLM 幻觉并回滚） |
| **Test** | ✅ 保留 | 不变（U8 已有方案） |

---

## 五、这个方案不做什么

| 被排除的方向 | 来源 | 为什么不做 |
|-------------|------|-----------|
| Leiden 社区检测 | GitNexus | 为知识浏览设计，CodePrune 的 embedding 定界更适合裁剪场景 |
| 执行流追踪 (Process) | GitNexus | CALLS 边已够，无需更高阶的工作流抽象 |
| 混合搜索 (BM25 + 向量) | GitNexus | CodePrune 的输入是完整指令文本，embedding cosine 足够 |
| LLM 修复策略链 (U5) | phase3_upgrade_plan | 方向应是减少 LLM 参与，不是给 LLM 更多策略 |
| Reflection Pattern (U4) | phase3_upgrade_plan | 架构美化，信息质量不变。CutManifest 才改变信息质量 |
| Phase3 补充遗漏文件 | 旧提案 | 模糊 Phase2/Phase3 职责边界，正确做法是 Phase2 选对 |

---

## 六、实施路径与依赖

```
Phase 1: 基础设施 (M2 + M3)                           [~2天]
  ├─ M2: graph_builder.py 赋初始置信度 (+10行)
  ├─ M2: closure.py 置信度门控 (+15行) 
  ├─ M3: surgeon.py 增加 CutRecord 记录逻辑 (+80行)
  ├─ pipeline.py 传递 manifest 对象 (+5行)
  └─ 验证: 跑 benchmark, 确认 manifest 内容正确 + 置信度日志

Phase 2: 核心能力 (M1 + M4)                           [~3天]
  ├─ M1: anchor.py 锚点扩展后处理 (+40行)
  ├─ M4: 新建 mechanical_healer.py (~200行)
  ├─ pipeline.py 集成 MechanicalHealer (+10行)
  └─ 验证: 统计 Phase3 build 错误数下降幅度

Phase 3: 收尾 (M5)                                    [~1天]
  ├─ M5: fixer.py 替换 LLM completeness → 结构化检查 (~-30行)
  ├─ M5: fixer.py UndefinedNames 注入 CutManifest (+20行)
  └─ 验证: 全量 benchmark + 编译通过率统计
```

**依赖图**：
```
M2 (边置信度)    M1 (锚点扩展)
       │                │
       ↓                ↓
  闭包更精确       覆盖更完整
       │                │
       └───── M3 ───────┘
              │ (CutManifest)
              ↓
             M4 (MechanicalHealer)
              │
              ↓
             M5 (Phase3 瘦身)
```

M1 和 M2 独立可做、互不依赖。M3 是 M4 的前提。M5 在 M4 之后。

---

## 七、预期效果

| 指标 | V2.3 当前 | 预期改善 | 关键改善来源 |
|------|----------|---------|------------|
| 文件级 F1 | 97% | **98-99%** | M1 锚点扩展提高 recall |
| Phase3 LLM 轮次 | ~3-5 轮 | **0-2 轮** | M4 消除可预测修复 |
| Phase3 零轮通过率 | ~0% | **>50%** | M4 直接通过编译 |
| 修复幻觉风险 | 每轮 LLM 都可能引入 | **仅残余轮次** | LLM 参与减少 ~70% |
| 编译通过率 | 未统计 | **新指标: 目标 >90%** | M4 + M5 |
| 闭包精度 | -- | **微改善** | M2 低置信边不驱动扩展 |

---

## 八、验证策略

### 回归测试

全部 4 个 benchmark（Dashboard/Shop/Compiler/Blog）× 3 粒度 = 12 组，确认 F1 不下降。

### 新增指标

1. **编译通过率**：Phase2 输出直接编译通过的比例（不经 Phase3）
2. **M4 解决率**：`cuts_resolved / cuts_total`——机械层解决了多少比例的断口
3. **Phase3 零轮率**：M4 处理后、Phase3 验证即通过的比例
4. **LLM 调用量**：Phase3 的 LLM API 调用次数对比基线

### 增量验证

| 完成阶段 | 验证方式 |
|---------|---------|
| M2 | 跑 blog-s1.3，检查 SEMANTIC_RELATED 边是否被降权，闭包是否更精简 |
| M3 | 跑 compiler-s1.2，检查 CutManifest 是否正确记录了所有 removed_import / removed_symbol |
| M1 | 跑 dashboard-s1.1，检查 statsApi.ts 是否通过扩展被选为锚点 |
| M4 | 跑全量 4 benchmark，统计编译通过率提升 |
| M5 | 全量验证 + 对比 Phase3 LLM 调用次数 |

---

## 九、与现有规划的关系

| 现有规划 | 本方案态度 | 理由 |
|---------|-----------|------|
| fix_plan Group A (安全修复) | ✅ 前置要求 | A1/A2 是基础 bug fix，必须先完成 |
| fix_plan Group B (指令理解) | ✅ 兼容 | B1/B2 改善 out_of_scope，与 M1 互补（一个改 LLM 理解，一个改图启发扩展） |
| fix_plan Group C (Closure 修正) | ✅ 兼容 | C1/C2 是 bug fix，M2 是机制增强，不冲突 |
| fix_plan Group D (反向退化) | ✅ 兼容 | D1 依赖 B，与本方案无交叉 |
| phase3_upgrade U1 (错误上下文) | ⚠️ 降优先级 | M5 用 CutManifest 注入确定性上下文, 比 aider-style 行高亮更精确 |
| phase3_upgrade U2 (预清理) | ✅ 被 M4 覆盖 | M4 是 U2 的超集（U2 只清 import，M4 还生成 stub + 修补模块结构） |
| phase3_upgrade U3 (结构化完整性) | ✅ 被 M5 覆盖 | M5.1 用 CutManifest 做结构化检查，比 U3 的 prompt 约束更可靠 |
| phase3_upgrade U4 (Reflection) | ❌ 不采纳 | 架构重构，收益/风险比不划算 |
| phase3_upgrade U5 (策略链) | ❌ 不采纳 | 方向错误——应减少 LLM 参与，不是给 LLM 更多策略 |
| phase3_upgrade U6 (pyflakes) | ✅ 已实现 | V2.3 已有 UndefinedNames 层 |
| phase3_upgrade U8 (测试验证) | ✅ 正交 | 可独立实施，不与本方案冲突 |

---

## 十、总结

本方案的核心杠杆点是 **CutManifest**——一个成本极低（~80 行嵌入 Surgeon）但效果极大的改动。它把"Phase3 盲人摸象"变成"Phase3 按清单修复"，使得：

1. **M4 机械修复** 成为可能（有清单才知道修什么）
2. **M5 完整性检查** 变得确定性（有清单才能做结构化校验）
3. **Phase3 UndefinedNames** 精度提升（有清单才能注入精确上下文）

配合 **M1 锚点扩展** 和 **M2 边置信度**，两端发力：Phase2 产出更完整更精确的闭包，Phase3 用更少且更确定的修复达到可编译状态。

**总改动量**：~350 行新增代码 + ~45 行修改 + ~30 行删除。
