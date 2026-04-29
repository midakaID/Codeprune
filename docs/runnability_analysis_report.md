# CodePrune 独立运行性分析报告

> 基于 `llm_hierarchical` scope strategy，9 个 benchmark 全量评测后的输出子仓库独立运行性分析。
> 
> 日期: 2025-07  
> F1 均值: 0.944 | 平均可运行性: ~17%

---

## 一、评分总览

| # | 项目 | 语言 | F1 | Precision | Recall | Prune% |
|---|------|------|-----|-----------|--------|--------|
| 1 | ticketing | Java | 0.977 | 0.955 | 1.000 | +8% |
| 2 | framework | Python | 0.970 | 0.941 | 1.000 | +39% |
| 3 | compiler | C | 0.968 | 0.938 | 1.000 | +38% |
| 4 | blog | Python | 0.966 | 0.933 | 1.000 | +45% |
| 5 | dashboard | TS | 0.952 | 0.909 | 1.000 | +24% |
| 6 | etl | Python | 0.941 | 0.941 | 0.941 | +28% |
| 7 | orchestrator | Python | 0.923 | 0.947 | 0.900 | +15% |
| 8 | query-engine | C | 0.914 | 0.941 | 0.889 | +12% |
| 9 | shop | Java | 0.889 | 0.923 | 0.857 | +13% |
| — | **AVERAGE** | — | **0.944** | 0.936 | 0.954 | — |

---

## 二、独立运行性汇总

| # | 项目 | 语言 | 可运行性 | 能编译? | 能启动? | 核心功能可用? |
|---|------|------|---------|---------|---------|-------------|
| 1 | blog | Python | **0%** | N/A | ❌ | ❌ |
| 2 | compiler | C | **75%** | ✅ | ✅ | ⚠️ 部分 |
| 3 | dashboard | TS | **5%** | ❌ | ❌ | ❌ |
| 4 | shop | Java | **15%** | ❌ | ❌ | ❌ |
| 5 | etl | Python | **0%** | N/A | ❌ | ❌ |
| 6 | framework | Python | **25%** | N/A | ❌ | ⚠️ 可绕过 |
| 7 | orchestrator | Python | **20%** | N/A | ❌ | ❌ |
| 8 | ticketing | Java | **15%** | ❌ | ❌ | ❌ |
| 9 | query-engine | C | **0%** | ❌ | ❌ | ❌ |

**平均可运行性: ~17%**

---

## 三、各项目详细问题

### 3.1 Blog (Python) — 0%

**阻塞原因**: app.py（入口点）import 了已删除的模块

| 问题 | 文件 | 详情 |
|------|------|------|
| ModuleNotFoundError | app.py | `from db.cache import ...`、`from auth import ...`、`from posts import ...`、`from tags import ...`、`from media import ...` 全部指向已删除模块 |
| 应删未删 | api/, notifications/email.py | 这些文件应被删除但仍存在 |
| 缺失函数 | moderation.py, reactions.py | 内部函数被删但文件保留 |

---

### 3.2 Compiler (C) — 75%

**基本可运行**: 核心解析管线可编译可执行

| 问题 | 文件 | 详情 |
|------|------|------|
| 应删未删 (5文件) | error_reporter.h/c, preprocessor.h/c, main.c | 不影响编译但违反规范 |
| 头文件截断 | optimizer.h, typechecker.h | 函数声明被 `// ... pruned N lines ...` 替代，外部无法调用 |
| 测试残留 | tests/_ft.c | 应删除 |

---

### 3.3 Dashboard (TypeScript) — 5%

**阻塞原因**: 组件 import 已删除的子组件

| 问题 | 文件 | 详情 |
|------|------|------|
| TS 编译失败 | Dashboard.ts | 导入已删除的 UserList、UserProfile 组件 |
| API/类型层 | — | 正确裁剪 |

---

### 3.4 Shop (Java) — 15%

**阻塞原因**: 核心服务被误删

| 问题 | 文件 | 详情 |
|------|------|------|
| 架构矛盾 | ProductService.java | 被删除但 OrderService/CartService 硬依赖它 |
| Java 编译失败 | OrderService, CartService | 无法解析 ProductService 类型 |

---

### 3.5 ETL (Python) — 0%

**阻塞原因**: 入口点 import 已删除模块

| 问题 | 文件 | 详情 |
|------|------|------|
| ModuleNotFoundError | main.py | `from etl.monitoring.dashboard import ...` 指向不存在模块 |
| 应删未删 | enrich.py, aggregate.py | 应被删除但仍存在 |

---

### 3.6 Framework (Python) — 25%

**阻塞原因**: 配置默认值引用已删除插件

| 问题 | 文件 | 详情 |
|------|------|------|
| Config 默认值 | core/config.py:30 | `plugins: ["auth", "cache"]` — cache 插件已删除 |
| 插件注册表 | plugins/__init__.py:24-27 | `_BUILTIN_PLUGINS` 仍映射 cache、rate_limit |
| utils 导出 | utils/__init__.py:9-14 | 尝试导入已删除的函数 (build_query_string, log_action, paginate, from_json) |

**备注**: 如果显式传 `Config(plugins=['auth'])`，核心路由功能可用。

---

### 3.7 Orchestrator (Python) — 20%

**阻塞原因**: 必需的插件文件被误删 + 应删文件保留

| 问题 | 文件 | 详情 |
|------|------|------|
| 必需文件缺失 | plugins/retry.py, plugins/audit.py | golden answer 要求保留，但被误删。onboarding/billing workflow 依赖它们 |
| 应删未删 | workflows/cleanup.py, plugins/metrics.py, backends/archive.py | 应删除但保留，且 archive.py 内部调用已删除的 serialize_payload |
| 工作流注册表 | workflows/__init__.py:7 | 仍加载 cleanup workflow（已损坏）|
| 后端注册表 | backends/__init__.py:9 | remote 元组格式错误 |

---

### 3.8 Ticketing (Java) — 15%

**阻塞原因**: 方法级裁剪不完整导致编译错误

| 问题 | 文件 | 详情 |
|------|------|------|
| 编译错误 | TicketService.java:104-106 | `rejectTicket()` 方法引用 `TicketRejectedEvent`（已删除类）|
| 应删未删 | AgentService.java, ReportingService.java | 应删除但保留 |
| 方法残留 | TemplateService.java | `dailyDigest()`, `adminEscalationMessage()` 应删除 |
| 方法残留 | ApprovalService.java:22-27 | `reject()` 方法 (dead code) |

---

### 3.9 Query-Engine (C) — 0%

**阻塞原因**: 必需文件被误删 + 应删文件保留

| 问题 | 文件 | 详情 |
|------|------|------|
| 必需文件缺失 | include/core/vector.h, src/core/vector.c | golden answer 要求保留，但被误删 |
| 链接失败 | optimizer.c | 调用 vector_init/push/free/get，无定义 |
| 应删未删 | executor.h/c, storage.h/c | 应删除但保留 |
| 头文件改写不一致 | optimizer.h, catalog.h | 用 `void*` 或重定义替代 vector.h 引用 |

---

## 四、共同不足 (Cross-cutting Issues)

| 共同问题 | 频次 | 受影响项目 |
|----------|------|-----------|
| **入口点/注册表保留了对已删除模块的引用** | **9/9** | 全部 |
| **应删除的文件未删除** | **7/9** | blog, etl, compiler, orchestrator, ticketing, query-engine, shop |
| **符号引用悬挂** (import 删了但调用还在) | **6/9** | blog, dashboard, shop, orchestrator, ticketing, query-engine |
| **应保留的文件被误删** | **3/9** | orchestrator, query-engine, shop |
| **`__init__.py`/注册表/配置未同步** | **5/9** | framework, orchestrator, blog, etl, query-engine |

---

## 五、差异化不足 (Language/Project-Specific)

| 差异化问题 | 项目 | 详情 |
|-----------|------|------|
| C 头文件被 `// ... pruned N lines` 截断 | compiler | optimizer.h, typechecker.h 函数声明丢失 |
| C 链接符号缺失 (误删 .c 文件) | query-engine | vector.c 误删 → optimizer 链接失败 |
| Java 方法级裁剪不完整 | ticketing | rejectTicket(), dailyDigest() 等应删方法仍存在 |
| Java 架构矛盾 (服务误删) | shop | ProductService 被删但消费者硬依赖 |
| TypeScript 组件引用断裂 | dashboard | Dashboard.ts import 已删 UserList/UserProfile |
| Python 配置默认值未更新 | framework | Config.plugins 默认含已删除的 "cache" |
| C `#if 0` 包裹而非物理删除 | query-engine | executor.c 用条件编译禁用 |

---

## 六、根本原因层级分析

```
Level 1 (9/9 项目): 入口点/注册表未清理
  └─ Phase2 按文件粒度删除，不改写存活文件中的 import/include
  └─ Phase3 HealEngine 只修编译错误，不理解"业务引用完整性"

Level 2 (7/9 项目): 应删除的文件留存
  └─ Phase2 anchor/closure 判定边界不够严格
  └─ 部分文件被误保留（如 api/、cleanup.py 等）

Level 3 (3/9 项目): 应保留的文件被误删
  └─ Phase2 语义评估将间接依赖 (vector, retry/audit, ProductService) 判定为可删除
  └─ 这是 F1 评分中 Recall < 1.0 的直接原因

Level 4 (语言特有): 方法级/符号级残留
  └─ 当前系统以文件为裁剪粒度，不进行方法级别的裁剪
  └─ Java 项目尤其明显（ticketing 的 rejectTicket 等）
```

---

## 七、改进方向建议

### 短期 (提升可运行性到 60%+)
1. **入口点引用清理**: Phase3 增加"存活文件引用审计"步骤，扫描所有存活文件中 import 已删除模块的语句并移除
2. **注册表/配置同步**: 检测 `__init__.py`、config dataclass 默认值、plugin registry 中对已删除模块的引用

### 中期 (提升可运行性到 80%+)
3. **方法级裁剪**: 对保留文件内部的方法进行语义评估，删除仅服务于被裁剪功能的方法
4. **闭包精度提升**: 改进 anchor/closure 算法，减少 Level 2/3 错误

### 长期 (提升可运行性到 95%+)
5. **端到端验证**: Pipeline 完成后自动尝试编译/启动，验证输出子仓库的实际可运行性
6. **架构感知裁剪**: 理解服务间依赖关系，避免 shop 式的"删了被依赖的服务"问题
