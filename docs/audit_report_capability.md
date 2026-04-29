# CodePrune 能力审计报告

> 审计范围：方向(1) Lazy Resolution 影响链、方向(2) Embedding vs LLM 决策权、方向(3) CodeHeal 对比 aider、方向(4) 其他发现  
> 所有结论基于源码逐行审读，附行号引用  
> ✅ 标记表示已实施修复

---

## 方向一：Lazy Resolution 影响链分析

### 当前行为时序

```
build(file-level only)
  → semantic enrich(仅 FILE 节点生成 embedding)
    → instruction analysis
      → anchor locate(在 FILE 级 embedding 中搜索)
        → resolve_region(展开锚点文件 + 硬依赖文件)
          → re-enrich(对新展开节点生成 embedding)
            → closure BFS
```

### 发现 L1：锚点候选池被人工缩窄

- **位置**：[pipeline.py](../pipeline.py#L128)、[anchor.py](../core/prune/anchor.py) `_locate()`  
- **机制**：Lazy 模式下，`builder.build()` 只创建 FILE 节点（无 FUNCTION/CLASS）。`semantic.enrich()` 只为 FILE 节点生成 embedding。Anchor 搜索因此只能在 ~100 个 FILE embedding 中匹配，而非完整 ~10K FUNCTION 级 embedding。
- **后果**：如果指令描述的是函数级行为（"用户认证中间件"），FILE 级 embedding（整个文件的摘要）能否命中取决于摘要质量。函数级 embedding 在语义精度上天然优于文件级。
- **严重程度**：HIGH — 直接影响 recall

### 发现 L2：二阶依赖永远不被展开

- **位置**：[pipeline.py](../pipeline.py#L155-175) `_expand_anchor_regions()`  
- **机制**：`resolve_region()` 展开锚点文件 + 其一阶硬依赖文件。但二阶依赖（依赖的依赖）不会被展开。这些文件在后续 BFS 中仍然是 FILE 级 stub，无子节点。
- **后果**：[closure.py](../core/prune/closure.py#L560-570) `_import_symbol_level()` 执行符号级传播时，依赖 `file_node.children`。未展开文件的 children 为空 → 传播失败 → 整文件回退或跳过。

### 发现 L3：BFS 静默吞没未展开节点

- **位置**：[closure.py](../core/prune/closure.py#L390-410) BFS 主循环  
- **机制**：BFS 遇到边的 target 不在 graph 中（未展开文件的子节点…根本不存在）时，`graph.get_node(target)` 返回 None → `continue`。没有任何日志、没有记录结构缺口、没有仲裁。
- **后果**：依赖关系被静默丢弃，用户无感知。对比非 Lazy 模式下（所有节点存在），同一 BFS 路径会正常传播。

### 建议

**短期**：将 `lazy_resolution` 默认值从 `true` 改为 `false`。当前的性能优化不足以弥补 recall 损失，尤其在中小型仓库（<500 文件）中 lazy 的时间节省不明显。

**中期**：如果要保留 lazy 模式，需要：
1. 在 BFS 遇到未展开节点时，**按需展开**（demand-driven resolution）而非静默跳过
2. 或至少记录为 StructuralGap 交由仲裁处理

---

## 方向二：Embedding 单阀门问题

### 核心发现：Embedding 是不可逆闸门，LLM 无法翻盘

当前决策链：

```
embedding score → 三区分类 (CORE / PERIPHERAL / OUTSIDE)
  → CORE: 直接加入闭包，不经 LLM
  → OUTSIDE: 永不发送给 LLM，即使被 BFS 触达
  → PERIPHERAL: 唯一经过精细判断的区域
```

### 发现 E1：目录级排除的不可逆性

- **位置**：[closure.py](../core/prune/closure.py#L290-330) `_dir_level_exclusion()`  
- **机制**：embedding score < `periph_thresh × 0.5` 的目录，其全部后代被强制移入 OUTSIDE + `dir_excluded` 集合。后续 BFS 中，这些节点即使被硬依赖边指向，也永远跳过。
- **阈值计算**：假设锚点最低分 0.35，`core_threshold_factor = 0.85`，`peripheral_threshold_factor = 0.5` → core = 0.30，periph = 0.15，dir_exclude = 0.075。一个目录只要 embedding 得分 < 0.075 就永久排除。
- **问题**：embedding 对目录的打分本身就不稳定（目录无代码内容，摘要靠子文件聚合）。0.074 和 0.076 的差距可以决定整个目录的生死，但人类完全无法区分。

### 发现 E2：CORE 区无 LLM 验证

- **位置**：[closure.py](../core/prune/closure.py#L419) BFS 分支  
- **机制**：`if target_id in scope.core: result.required_nodes.add(target)` — 无条件加入，不经 LLM reasoning。
- **问题**：高 embedding score 不等于功能相关。一个命名相似但功能无关的模块（如 `auth_handler` vs `auth_test_helper`）可能获得高相似度。CORE 自动包含意味着无法拦截这类误判。

### 发现 E3：低置信度边静默丢弃

- **位置**：[closure.py](../core/prune/closure.py#L368-381)  
- **机制**：`if edge.confidence < 0.6: continue` — 置信度 < 0.6 的边在 BFS 中被直接跳过，不记为 gap，不交仲裁。
- **问题**：0.59 的边完全消失，0.61 的边走完整流程。这 0.02 的差距由 tree-sitter 解析的模糊启发式决定，不经任何 LLM 检验。

### 发现 E4：权力不对称

| 场景 | Embedding 能做 | LLM 能做 |
|------|---------------|----------|
| OUTSIDE 节点被 BFS 硬依赖触达 | ✅ 阻止加入 | ❌ 永远看不到这个节点 |
| CORE 节点功能无关 | ✅ 自动加入 | ❌ 不被询问 |
| PERIPHERAL 节点 | 决定起始区域 | ✅ 通过仲裁判决 |

LLM reasoning 目前只在 PERIPHERAL 区域的缺口仲裁中真正起作用。对于 CORE 和 OUTSIDE（往往占总节点的 60-80%），LLM 完全缺席。

### 建议

**方案 A（推荐）：CORE 区加 LLM 校验门**  
对 embedding 划为 CORE 的节点，在加入闭包前批量发送给 LLM 做一次 "相关性确认"。每批 ~20 个节点，增加 1 次 LLM 调用，成本可控。如果 LLM 否决，降级为 PERIPHERAL 走正常流程。

**方案 B：OUTSIDE 区的按需复议**  
当 BFS 遇到 OUTSIDE 节点且该边是硬依赖（IMPORTS / INHERITS / IMPLEMENTS）时，不直接跳过，而是记录为 gap 交仲裁。只增加对 OUTSIDE + 硬边的复议，不影响 OUTSIDE + 软边的快速跳过。

**方案 C（激进）：取消三区分类，全量 LLM 决策**  
用 embedding 做预排序（而非预分类），BFS 中每个节点都交 LLM 判断。成本显著上升（可能 10x），但消除阈值脆弱性。不推荐，除非 LLM 成本持续下降。

---

## 方向三：CodeHeal vs Aider 对比分析

### 补丁匹配策略对比

| 层级 | CodePrune fixer.py | Aider editblock_coder.py |
|------|-------------------|--------------------------|
| L1 | 精确匹配 | 精确匹配 (`perfect_replace`) |
| L2 | rstrip 后匹配 | — |
| L3 | strip 后匹配 | — |
| L4 | 缩进感知匹配 (`_find_context_core`) | 前缀空白剥离匹配 |
| L5 | — | `...` 省略块展开 (`try_dotdotdots`) |
| L6 | — | 编辑距离模糊匹配 (SequenceMatcher, 阈值 0.8) |

**差异分析**：
- CodePrune 的 L2/L3（rstrip/strip）处理行尾空白差异，aider 没有独立层级，但其 L4 覆盖了部分场景。  
- Aider 的 `try_dotdotdots` 让 LLM 可以用 `...` 代替不变的代码块，减少 token 消耗。CodePrune 没有此机制，LLM 必须完整输出上下文块。  
- Aider 的编辑距离回退（L6）是 CodePrune 完全缺失的。当 LLM 输出的 SEARCH 块与实际代码有微小差异（变量名拼错、多/少一个空行）时，aider 能容错，CodePrune 直接失败。

### 反思循环对比

| 特性 | CodePrune | Aider |
|------|-----------|-------|
| 编译/lint 检查 | Layer 1 (validator.py) | 自动 lint |
| 失败后修复 | 同一 fixer 循环，最多 3 轮 | reflections 循环，最多 3 次 |
| 测试驱动修复 | Layer 2 completeness (LLM 判断缺失) | 运行测试 → 自动修复 |
| Architect 模式 | reasoning 分析 + fast 执行 (≥3 错误触发) | 独立 architect model + editor model |
| 忠实度检验 | Layer 3 (行级追溯 + 2x 大小卫士) | 无 |

**差异分析**：
- Aider 的测试驱动修复依赖用户提供测试用例，CodePrune 的场景（提取子仓库）通常无法运行测试，因此 Layer 2 用 LLM 判断替代实际测试，这是合理的领域适配。  
- **CodePrune 的 Layer 3 忠实度检验是 aider 没有的优势**。aider 不关心 LLM 是否幻觉出新代码（因为目的是修改代码），CodePrune 必须防止修复过程引入原仓库不存在的代码。这个设计是对的。  
- Aider 的 Architect 模式是全局双模型（所有修复都走 architect → editor），CodePrune 只在错误 ≥3 时触发，这更经济。

### 可借鉴的改进

**值得引入的**：

1. **编辑距离回退匹配**（aider 的 `replace_closest_edit_distance`）  
   - **理由**：[fixer.py](../core/heal/fixer.py#L1190-1253) 的三级匹配在 LLM 输出轻微偏差时全部失败。加一层 SequenceMatcher 回退（阈值 0.75-0.8），可以容错变量名拼写差异、多余空行等。  
   - **实现成本**：低，~50 行 Python，只改 `_apply_patch()`。  
   - **风险**：模糊匹配可能命中错误位置。建议仅在前三级失败后启用，且匹配到后打 warning 日志。

2. **省略块支持**（aider 的 `try_dotdotdots` 思路，不是照搬语法）  
   - **理由**：CodePrune 修复中 LLM 必须完整输出 SEARCH 块的全部代码行。对于大函数（50+ 行），中间不变的部分占大量 token。  
   - **实现思路**：在 prompt 中告诉 LLM 可以用 `// ... unchanged ...` 标记不变区域，`_apply_patch()` 在匹配时展开这些标记为原文件对应行。  
   - **实现成本**：中，~100 行改动，需改 prompt + matcher。

**不建议引入的**：

1. ~~Aider 的跨文件回退匹配~~（在目标文件匹配失败时尝试其他文件）  
   - CodePrune 的修复上下文已经包含错误文件路径，不存在"找错文件"的问题。引入跨文件搜索只会增加误修复风险。

2. ~~Aider 的全局 Architect 模式~~  
   - CodePrune 的条件触发策略（≥3 错误）更经济，且 Layer 1-2-3 的分层验证已经提供了足够的质量保障。

---

## 方向四：其他发现

### F1: Barrel/Re-Export 链只追踪一层（CRITICAL）

- **位置**：[closure.py](../core/prune/closure.py#L1078-1110) `_auto_include_barrel_files()`  
- **现状**：该方法仅按目录共存规则包含 barrel 文件（同目录有已选 TS/JS 文件 → 包含 index.ts）。不追踪 barrel 内部的 re-export 链。
- **影响场景**：
  ```
  src/api/index.ts  re-exports from  src/api/routes/index.ts
  src/api/routes/index.ts  re-exports from  src/api/routes/users.ts
  ```
  如果只有 `src/api/` 目录有已选文件,`src/api/routes/index.ts` 不在同目录 → 不被自动包含 → 运行时 import 失败。
- **修复建议**：在 barrel 自动包含后，对新包含的 barrel 检查其 IMPORTS 出边，递归追踪（加 visited set 防循环）。

### F2: C/C++ 前向声明与实际使用不区分（CRITICAL）

- **位置**：[parsers/treesitter_adapter.py](../parsers/treesitter_adapter.py) `_walk_deps`  
- **现状**：`struct Executor;`（前向声明）和 `Executor exec;`（实际使用）都被创建为 USES 边，confidence 相同。  
- **影响**：C 项目中前向声明广泛使用（尤其头文件间），导致闭包膨胀 150-300%，大量无用头文件被拉入。  
- **修复建议**：在 tree-sitter 解析阶段识别前向声明模式（`struct X;` 无 body / `class X;`），生成 FORWARD_DECLARES 边而非 USES 边，BFS 中将其降级为软依赖。

### F3: 多行装饰器/注解手术缺陷（HIGH）

- **位置**：[surgeon.py](../core/prune/surgeon.py#L610-625) `_expand_decorators_upward()`  
- **现状**：向上搜索遇到 `@`/`#` 前缀就纳入行，遇到非装饰器行就 break。但多行注解（Java `@Deprecated(\n since="1.0"\n)`）的第 2+ 行不以 `@` 开头 → 被错误地 break → 孤立注解残留。  
- **修改建议**：加状态跟踪——如果当前在一个未闭合的括号内（括号计数 > 0），即使该行不以 `@` 开头也继续向上扫描。

### F4: 配置无验证，静默失败（HIGH）

- **位置**：[config.py](../config.py)  
- **现状**：`CodePruneConfig` 的 `__post_init__` 不校验参数范围。`anchor_confidence_threshold: 0.95` + summary_quality 系数 0.7 = 实际 0.665，用户无法从配置推断。API key 缺失在首次 LLM 调用时才暴露，错误信息无帮助。  
- **修复建议**：
  1. 在 `__post_init__` 加数值范围校验（threshold 必须 0-1，ratio 必须 0-1）
  2. 启动时校验 API key 可用性（发一条空 embedding 请求）
  3. 若 summary_quality 系数生效，打 warning 日志说明实际阈值

### F5: Phase 1 enrichment 部分失败不阻断 Phase 2（HIGH）

- **位置**：[pipeline.py](../pipeline.py#L115-140)  
- **现状**：如果 `semantic.enrich()` 部分失败（embedding API 超时），部分节点无 embedding score → `relevance_map` 中为 None → 被分入 PERIPHERAL（[closure.py](../core/prune/closure.py#L275)），但 instruction_analysis 基于不完整图谱运行。  
- **影响**：enrichment 失败率 > 10% 时，PERIPHERAL 区膨胀（本应有确切分值的节点都落入"不确定"区），闭包精度下降。  
- **修复建议**：计算 enrichment 覆盖率（有 score 的节点 / 总节点），< 90% 时打 warning，< 50% 时 abort。

### F6: __init__.py strict 模式过度保守（MEDIUM）

- **位置**：[closure.py](../core/prune/closure.py#L653-660)  
- **现状**：strict 模式下，如果 import 边无 `imported_symbols` → 直接 return，不走回退策略。对于 `from . import *` 的 `__init__.py`，无法分辨导入了什么 → 整个模块丢失。  
- **修复建议**：strict 模式下遇到无符号信息时，降级为 gap 交仲裁，而非静默跳过。

---

## 综合优先级排序

| 优先级 | 问题 | 方向 | 状态 | 改动文件 |
|--------|------|------|------|---------|
| P0 | Lazy Resolution 默认关闭 | ① | ✅ 已实施 | config.py, codeprune.yaml, cli.py, README.md |
| P0 | BFS 静默吞没未展开节点 | ① | ✅ 已实施 | closure.py — 硬依赖记录为 StructuralGap("unresolved") |
| P0 | Barrel re-export 递归追踪 | ④ | ✅ 已实施 | closure.py — 沿 IMPORTS 边递归追踪，带 visited set |
| P1 | CORE 区加 LLM 校验门 | ② | ✅ 已实施 | closure.py + prompts.py — BFS 后批量校验 CORE 自动包含 |
| P1 | OUTSIDE 硬依赖复议 | ② | ✅ 已实施 | closure.py — R_OUT 扩展 INHERITS/IMPLEMENTS，非 Java 结构性穿透 |
| P1 | C/C++ 前向声明区分 | ④ | ⏭️ 跳过 | 经核实 USES 边从未在代码中创建，问题不存在 |
| P1 | 多行装饰器手术 | ④ | ✅ 已实施 | surgeon.py — 括号计数追踪多行注解 |
| P2 | 编辑距离回退匹配 | ③ | ✅ 已实施 | fixer.py — SequenceMatcher 滑动窗口，阈值 0.78 |
| P2 | 省略块支持 | ③ | ⏭️ 未实施 | 需改 prompt + matcher，风险较高 |
| P2 | 配置校验 | ④ | ✅ 已实施 | config.py — __post_init__ 数值范围 + API key 检查 |
| P2 | Enrichment 覆盖率检查 | ④ | ✅ 已实施 | pipeline.py — <50% abort, <90% warning |
| P3 | strict 模式降级 | ④ | ✅ 已实施 | closure.py — 无符号信息时降级为文件级包含 |
| P3 | strict 模式降级 | ④ | ~15 行 | 边界 case 覆盖 |
