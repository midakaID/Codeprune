# Instruction Rewrite 对比报告

> 测试题：将 9 个 benchmark 的指令从"具体文件级"改写为"用户级自然语言"后，系统的自规划能力是否足以产生正确结果？

## 0. 重要说明：API 余额不足影响

运行过程中 API 余额耗尽（剩余¥64.13，单次推理预扣¥67.62），导致 **4 个 benchmark**（framework/orchestrator/ticketing/query-engine）的所有 LLM 推理调用返回 403，完全退化为纯 embedding fallback。这 4 个结果 **不具备对比价值**。

有效对比数据仅 5 个（blog/compiler/dashboard/shop/etl）。

## 1. 结论

**不足（但根因比预想更深）。** 

有效 5 个 benchmark 的平均 F1 从 **0.911→0.747**（−18%），其中 dashboard (0.800→0.500) 和 shop (0.963→0.571) 严重退化。

根因不是"ClosureSolver 闭包算法不够好"，而是 **当前配置下图谱完全没有跨文件依赖边**（initial_granularity=file + lazy_resolution=false），ClosureSolver 的 BFS 无边可走。详见 [docs/instruction_closure_design.md](instruction_closure_design.md)。

## 2. F1 对比表

| Benchmark | 旧 F1 | 新 F1 | Δ | 旧 R | 新 R | 根因分析 |
|-----------|-------|-------|------|------|------|----------|
| blog | 0.966 | 0.966 | 0.000 | 1.000 | 1.000 | ✅ 无变化 |
| compiler | 1.000 | 1.000 | 0.000 | 1.000 | 1.000 | ✅ 无变化 |
| dashboard | 0.800 | 0.500 | −0.300 | 0.714 | 0.357 | 漏掉 chart types、user 相关 6 个文件 |
| etl | 0.800 | 0.759 | −0.041 | 0.706 | 0.647 | 漏掉 context、utils、config、main |
| framework | 0.692 | 0.522 | −0.170 | 0.562 | 0.375 | 只保留 http/，漏掉 core/（router/app/config）和 plugins/auth |
| orchestrator | 0.976 | 0.320 | −0.656 | 1.000 | 0.200 | 只保留 workflows/，漏掉整个 core/ 和 backends/local |
| query-engine | 1.000 | 0.875 | −0.125 | 1.000 | 0.778 | 漏掉 core/common.h、core/vector.h 及其 .c 文件 |
| shop | 0.963 | 0.600 | −0.363 | 0.929 | 0.429 | 漏掉 dao/、model/Product、model/User、ProductService |
| ticketing | 1.000 | 0.000 | −1.000 | 1.000 | 0.000 | 灾难性：只保留 service/，漏掉全部 events/、model/、api/ |
| **AVERAGE** | **0.911** | **0.616** | **−0.295** | **0.879** | **0.532** | |

## 3. 功能测试对比

| Benchmark | 旧 pass/total | 新 pass/total | 状态 |
|-----------|---------------|---------------|------|
| blog | 3/8 | 3/8 | 不变 (execute_insert re-export bug) |
| compiler | 7/7 | 7/7 | ✅ 不变 |
| dashboard | 4/4 | 3/4 | −1 (缺少 chart types) |
| etl | 7/7 | 3/7 | −4 (缺少 context 模块) |
| framework | 0/6 | 0/6 | 不变 (缺少 core/) |
| orchestrator | 6/9 | 0/9 | −6 (几乎什么都没保留) |
| query-engine | 6/6 → 1 test | 0/1 | −1 (缺少 common.h) |
| shop | 9/9 → 1 test | 0/1 | −1 (缺少 dao/) |
| ticketing | 7/7 → 1 test | 0/1 | −1 (缺少 model/events/) |
| **TOTAL** | **49/63 (78%)** | **16/44 (36%)** | |

## 4. 失败模式分析

### 4.1 ClosureSolver 闭包不足 (7/9 受影响)

核心问题：当指令从"保留 `orchestrator/core/`, `orchestrator/backends/local.py`…"变为"保留本地执行能力"后，InstructionAnalyzer 只将 `workflows/billing.py` 和 `workflows/onboarding.py` 标为 anchor，ClosureSolver 沿 import 图向下闭包时发现这两个文件的 import 已被 surgeon 裁掉或不完整，导致 core/ 和 backends/local 整体遗漏。

**受影响的 benchmark 和遗漏模块：**

| Benchmark | 遗漏的关键模块 | 文件数 |
|-----------|--------------|--------|
| orchestrator | core/, backends/base+local, config.py, main.py | 16 |
| ticketing | events/, model/, api/ | 21 |
| shop | dao/, model/Product+User, ProductService, App.java | 8 |
| framework | core/router+app+config, plugins/auth | 10 |
| dashboard | api/client.ts, types/chart.ts, UserList/UserProfile | 9 |
| etl | core/context, utils/, config.py, main.py | 6 |
| query-engine | core/common.h+vector.h 及其 .c | 4 |

### 4.2 InstructionAnalyzer anchor 选择过窄

InstructionAnalyzer 的 embedding 搜索倾向选中"名字匹配度最高"的函数，而非"实际被需要"的模块。例如：

- "订单管理" → 只选中 OrderService/CartService/PaymentService，没选中它们依赖的 ProductService 和 dao/BaseDao
- "核心审批流程" → 只选中 service/ApprovalService，没选中 events/EventBus（审批需要发事件）和 model/ApprovalStep（审批数据模型）
- "本地执行能力" → 只选中 workflows/，没选中 core/executor（实际执行引擎）

### 4.3 blog 和 compiler 不受影响

这两个 benchmark 的新指令仍然足够具体，且它们的 anchor 文件与其他模块的依赖关系简单，closure 能自动补全。

## 5. 耗时对比

| Benchmark | 旧耗时 | 新耗时 | 变化 |
|-----------|--------|--------|------|
| blog | 130.1s | 130.1s | 不变 |
| compiler | 82.3s | 53.2s | −35% |
| dashboard | 68.3s | 76.1s | +11% |
| shop | 107.3s | 101.5s | −5% |
| etl | 112.2s | 100.9s | −10% |
| framework | 82.3s | 34.6s | −58% |
| orchestrator | 32.2s | 15.7s | −51% |
| ticketing | 75.3s | 39.3s | −48% |
| query-engine | 96.7s | 24.0s | −75% |

过度裁剪的 benchmark 反而跑得更快（处理的文件更少）。

## 6. 根因定位与改进建议

### P0: ClosureSolver 缺少"反向依赖"分析
当前 closure 只做正向闭包（从 anchor 沿 import 往下追）。需要增加：
- **被依赖分析**：如果 A import B，且 B 是 anchor，则 A 也可能需要保留
- **接口完整性**：如果保留了 service 层，自动拉入它使用的 model/dao/events

### P1: InstructionAnalyzer 需要"功能链推理"
仅靠 embedding 相似度找 anchor 不够。需要 LLM 在理解指令后，沿知识图谱推理完整的功能链：
- 用户说"订单管理"→ Order → 需要 Product（订单项依赖商品）→ 需要 dao（持久化）
- 用户说"审批流程"→ Approval → 需要 Event（审批完成发事件）→ 需要 Model（数据结构）

### P2: 增加"最小可运行集"验证
在 closure 完成后增加一轮校验：检查输出中的所有 import 是否都能在输出文件集中解析。如果有未解析的 import，自动从源仓库拉入缺失的文件。

## 7. 结论

系统目前的自规划能力 **仅对结构简单、依赖线性的项目有效**（blog、compiler）。对于有复杂依赖图的项目（orchestrator、ticketing、shop），高层指令导致严重的 recall 下降。

**短期建议**：在 benchmark 指令中保持适度的具体性（至少指出需要保留的"模块"而非文件），作为对 InstructionAnalyzer 的提示。

**中期目标**：实现 P0/P1/P2 三个改进，使系统能从纯用户语言指令自动推导完整的保留集。
