# 用户指令 → 完整保留集推导：协同设计

> 目标：使 CodePrune 在仅有高层用户指令（如"保留订单管理的完整功能链"）时，
> 能够自主推导出与手动指定文件等效的完整保留集。

---

## 1. 根因链

逐层追踪从用户指令到错误输出的因果链：

```
用户指令："保留订单管理的完整功能链，不需要商品CRUD和用户注册登录"
  │
  ▼ InstructionAnalyzer 
  │ ① 将 ProductService.java、Product.java、User.java 列入 out_of_scope
  │    原因：prompt 规定"Do NOT infer data models"，LLM 严格执行
  │ ② 选出 OrderService、CartService、PaymentService 等作为 root_entities
  │
  ▼ AnchorLocator
  │ ③ 根据 root_entities 查找对应 FILE 节点作为锚点
  │    （file 粒度下无 FUNCTION 节点可供精确命中）
  │
  ▼ ClosureSolver (BFS)
  │ ④ 从锚点出发，沿出边扩展闭包
  │    **但 initial_granularity=file + lazy_resolution=false ==> 图谱仅有 CONTAINS 边**
  │    文件节点之间 **没有 IMPORTS/CALLS/INHERITS 边**
  │    BFS 完全无法发现 OrderService → ProductService 的 import 依赖
  │ ⑤ ProductService 等被放入 out_of_scope，即使BFS能到达也会被预设排除
  │
  ▼ Gap Arbitration
  │ ⑥ 仅 2 个 structural gap，全部由规则判定，0 个 include
  │    （没有边 → 没有 gap → 没有仲裁机会）
  │
  ▼ CodeHeal
  │ ⑦ 检测到 "功能不完整: 缺少 BaseDao, Product, ProductService..."
  │    但 CodeHeal 只能 comment/stub/patch，无法从源仓库拉入缺失文件
  │
  ▼ 输出：6 个文件 (F1=0.571, R=0.429)
```

**三个结构性缺陷**：

| # | 缺陷 | 位置 | 影响 |
|---|------|------|------|
| **C1** | file 粒度 + lazy_resolution=false 导致图谱没有跨文件依赖边 | builder.py L39 + codeprune.yaml | ClosureSolver BFS 是盲的——无边可走 |
| **C2** | UNDERSTAND_INSTRUCTION prompt 禁止推断隐式依赖 | prompts.py L99-102 | InstructionAnalyzer 将编译必需的类型文件列入 out_of_scope |
| **C3** | CodeHeal 发现缺失文件后无法补救 | fixer.py | 最后一道防线形同虚设 |

### C1 的直接证据

所有 9 个 benchmark 的 Phase1 输出中，edges 字典仅有 `contains` 键：
```
blog:    {'edges': {'contains': 41}}     # 0 imports
shop:    {'edges': {'contains': 24}}     # 0 imports
dashboard: {'edges': {'contains': 20}}   # 0 imports
```

这不是设计意图。代码中 `_parse_all_files()` 会创建 IMPORTS/CALLS 边，但仅在 `initial_granularity in ("class", "function")` 时被调用（builder.py L39）。当前 config `initial_granularity: file` 直接跳过了全部 AST 解析。

与 lazy_resolution 的关系：
- `lazy_resolution: true` 会在 Phase2 锚定后对锚点区域做细粒度展开（pipeline.py L176-244），包括 import 解析
- `lazy_resolution: false` 时完全不展开——当前配置
- 两个开关组合导致"既不提前解析，也不延迟解析"的零解析状态

---

## 2. 为什么旧的具体指令不受影响

具体指令如：
```
1. service/OrderService.java, service/CartService.java, service/PaymentService.java 完整保留
2. model/Order.java, model/CartItem.java, model/Payment.java 完整保留
3. dao/BaseDao.java, dao/Persistable.java 完整保留
4. model/Product.java, model/User.java 保留（OrderService 依赖）
5. service/ProductService.java 保留（CartService 依赖）
```

这种指令下：
1. InstructionAnalyzer 直接把所有文件名提取为 root_entities
2. AnchorLocator 用文件名完全匹配找到所有 FILE 节点
3. 所有文件都已是锚点或 anchor 直接命中 → 不需要 BFS 追踪依赖
4. ClosureSolver 只需做包含目录补全

**结论**：旧指令之所以高分，是因为它绕过了整个依赖追踪系统，直接把答案喂给了锚点定位器。

---

## 3. 改进方案

### 3.1 P0: 启用跨文件依赖边（Config + Builder）

**最小改动**：修改 `codeprune.yaml`

```yaml
graph:
  initial_granularity: function   # file → function
  # 或保持 file + 启用 lazy:
  # initial_granularity: file
  # lazy_resolution: true
```

**推荐**: `initial_granularity: function`，因为：
- 完整 AST 解析在 Phase1 完成，后续所有阶段都能受益
- 对 mini-benchmark 级别项目（15-33 文件），Phase1 增加的时间微不足道（<10s）
- InstructionAnalyzer 可以选择更精确的函数级锚点（如 `OrderService.createOrder`）
- ClosureSolver BFS 可以沿 IMPORTS/CALLS 追踪完整依赖链

**如果要保持 `file` 粒度（大仓库性能考虑）**：
- 改为 `lazy_resolution: true`
- 在 builder.py 中增加"文件级 import 预扫描"（只提取 import 语句创建 IMPORTS 边，不做完整 AST）：

```python
# builder.py — 在 _scan_filesystem() 后添加
def _scan_file_imports(self) -> None:
    """轻量级 import 扫描（file 粒度专用）
    仅提取文件间 import 关系，不做完整 AST 解析。
    """
    from parsers.import_resolver import create_import_resolver
    for node in self.graph.file_nodes:
        full_path = self.config.repo_path / node.file_path
        try:
            source = full_path.read_bytes()
        except OSError:
            continue
        lang = node.language
        adapter = TreeSitterAdapter(lang)
        deps = adapter.extract_dependencies(source, node.file_path)
        resolver = create_import_resolver(lang, self.config.repo_path)
        for dep in deps:
            resolved = self._resolve_edge_target(dep, node.file_path, resolver)
            if resolved:
                self.graph.add_edge(resolved)
```

并在 `build()` 中调用：
```python
def build(self):
    self._scan_filesystem()
    if self.config.graph.initial_granularity in ("class", "function"):
        self._parse_all_files()
    else:
        self._scan_file_imports()  # ← 新增：文件级也要有 import 边
    return self.graph
```

### 3.2 P1: InstructionAnalyzer 区分"功能排除"与"代码排除"

当前 UNDERSTAND_INSTRUCTION prompt 的问题：
```
⚠ GROUNDING RULE: Do NOT infer or add sub-features 
the user did not mention — even if they seem "obviously needed" 
(e.g. do NOT add "data models" unless the user explicitly mentions them)
```

用户说"不需要商品CRUD"时，LLM 把整个 `ProductService.java` 和 `Product.java` 列入 out_of_scope。但 OrderService 的下单逻辑依赖 `ProductService.getById()` 和 `Product` 类型。

**修改 prompt 添加语义区分**：

```
⚠ EXCLUSION SEMANTICS:
When the user says "不需要 X 功能" or "去掉 X":
- This means: do NOT preserve X's entry-point/handler/controller code
- This does NOT mean: delete all code related to X

Example: "不需要商品CRUD管理" means:
  → out_of_scope: files that are EXCLUSIVELY product management entry points 
    (e.g. ProductController.java, product_routes.py)
  → NOT out_of_scope: ProductService.java, Product.java — these may be 
    dependencies of kept features (e.g. order management uses Product model)
  
Rule: Only add a file to out_of_scope if it is EXCLUSIVELY for the excluded 
feature and NOT imported by any file you've identified as a root entity.
When unsure, lean toward NOT excluding — the dependency solver will prune 
truly unused code automatically.
```

### 3.3 P2: ClosureSolver 硬依赖强制包含

即使 P0+P1 修复后，仍存在 prompt 不稳定和 embedding 偏差的风险。需要在 ClosureSolver 中增加硬保障：

**在 BFS 之后、gap arbitration 之前，添加 import 完备性检查**：

```python
def _ensure_import_completeness(self, required_nodes: set[str]) -> set[str]:
    """确保 required_nodes 中所有文件的 import 目标都在集合内。
    如果缺失且目标不在硬排除列表中，自动拉入。"""
    added = set()
    for nid in list(required_nodes):
        for edge in self.graph.get_outgoing(nid, EdgeType.IMPORTS):
            target = edge.target
            if target in required_nodes:
                continue
            if target in self._hard_excluded:
                # 在 out_of_scope 中 —— 仍然检查：是否是编译级依赖？
                target_node = self.graph.get_node(target)
                if target_node and self._is_compile_time_dep(edge, target_node):
                    required_nodes.add(target)
                    added.add(target)
                    logger.info(f"硬依赖恢复: {nid} → {target} (编译级 import)")
            else:
                required_nodes.add(target)
                added.add(target)
    return added

def _is_compile_time_dep(self, edge: Edge, target: CodeNode) -> bool:
    """判断是否为编译级依赖（类型/继承/接口实现）"""
    if edge.edge_type == EdgeType.INHERITS:
        return True
    # import 的符号是类型名（首字母大写）
    imported = edge.metadata.get("imported_symbols", [])
    return any(s[0].isupper() for s in imported if s)
```

### 3.4 P3: CodeHeal 从源仓库补全缺失文件

当前 CodeHeal 发现"功能不完整"时只能记录日志。添加自动补全能力：

```python
# fixer.py — completeness_check 发现缺失文件后
def _recover_missing_files(self, missing_files: list[str]) -> int:
    """从源仓库拷贝缺失的依赖文件到输出目录"""
    recovered = 0
    for rel_path in missing_files:
        src = self.config.repo_path / rel_path
        dst = self.output_path / rel_path
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            recovered += 1
            logger.info(f"补全: {rel_path}")
    return recovered
```

---

## 4. 优先级和预期效果

| 改进 | 复杂度 | 预期 F1 提升 | 原理 |
|------|--------|-------------|------|
| **P0**: function 粒度 | 改 1 行 yaml | +0.15~0.25 | BFS 可追踪依赖链 |
| **P1**: prompt 语义区分 | 改 prompts.py 1处 | +0.05~0.10 | 减少误排除 |
| **P2**: import 完备性 | closure.py 内新增 ~30行 | +0.05~0.10 | 硬依赖兜底 |
| **P3**: CodeHeal 补全 | fixer.py 内新增 ~15行 | +0.02~0.05 | 最后防线 |

**P0 是决定性的**。没有依赖边，其余改进全部无法发挥作用。

### 组合预期

| 场景 | 当前 F1 | P0 后 | P0+P1 后 | P0+P1+P2 后 |
|------|---------|-------|----------|-------------|
| blog | 0.966 | 0.966 | 0.966 | 0.966 |
| compiler | 0.938 | 0.970 | 0.970 | 1.000 |
| dashboard | 0.500 | 0.700 | 0.750 | 0.800 |
| shop | 0.571 | 0.750 | 0.850 | 0.950 |
| etl | 0.759 | 0.800 | 0.820 | 0.850 |
| **avg (5 valid)** | **0.747** | **0.837** | **0.871** | **0.913** |

---

## 5. 协同工作流设计（P0+P1+P2 集成后）

```
用户指令: "保留订单管理的完整功能链，不需要商品CRUD和用户注册登录"
  │
  ▼ Phase1: CodeGraph (initial_granularity=function)
  │ 完整 AST 解析 → FILE/CLASS/FUNCTION 节点
  │ 完整依赖提取 → IMPORTS/CALLS/INHERITS 边
  │ 语义增强 → summary + embedding
  │
  ▼ Phase2.0: InstructionAnalyzer (P1 改进后)
  │ sub_features:
  │   1. "购物车到订单" → root: CartService.checkout, OrderService.createOrder
  │   2. "订单状态流转" → root: OrderService.updateStatus
  │   3. "支付退款"    → root: PaymentService.processPayment, PaymentService.refund
  │ out_of_scope:
  │   - controller/ProductController.java  ← 仅排除入口，不排除 ProductService
  │   - controller/UserController.java
  │   - service/UserService.java
  │ anchor_strategy: "distributed"
  │
  ▼ Phase2.1: AnchorLocator
  │ Layer 1: qualified name lookup → CartService.checkout, OrderService.createOrder, ...
  │ Layer 2: sub-feature embedding search → 补充 model/Order, model/Payment
  │ Layer 3: LLM verification → 通过 8/12 候选
  │ → 8 个 FUNCTION 级锚点
  │
  ▼ Phase2.2: ClosureSolver
  │ 语义定界: CORE (OrderService, CartService, PaymentService, Order, Payment, CartItem)
  │            PERIPHERAL (ProductService, BaseDao, Product, User, Validator)
  │            OUTSIDE (UserController, AuthService, ...)
  │
  │ BFS 从锚点出发:
  │   CartService.checkout → CALLS → ProductService.getById → PERIPHERAL → include!
  │   CartService.checkout → CALLS → OrderService.createOrder → CORE → include!
  │   OrderService.createOrder → IMPORTS → Order.java → CORE → include!
  │   OrderService.createOrder → IMPORTS → Product.java → PERIPHERAL → 检查...
  │     ProductService 是 ORDER 链的硬依赖 → include
  │
  │ Import 完备性检查 (P2):
  │   CartItem.java → import Product → Product not in required → 自动补入
  │   OrderService → import BaseDao → BaseDao not in required → 自动补入
  │
  ▼ 输出: 13 个文件 → F1 ≈ 0.95
```

---

## 6. 实现计划

### 第一步：P0 — 解锁依赖边（改 config）
1. `codeprune.yaml`: `initial_granularity: file` → `function`
2. 重跑 dashboard + shop 验证效果
3. 如果 Phase1 对大仓库太慢，改用 `file` + `lazy_resolution: true`

### 第二步：P1 — prompt 修正
1. `core/llm/prompts.py`: 修改 UNDERSTAND_INSTRUCTION 的 GROUNDING RULE
2. 重跑 shop 验证 out_of_scope 不再包含 ProductService

### 第三步：P2 — import 完备性兜底
1. `core/prune/closure.py`: 在 BFS 完成后添加 `_ensure_import_completeness()`  
2. 全量重跑 9 个 benchmark
3. 与旧具体指令跑分对比

### 第四步：P3（可选）
1. `core/heal/fixer.py`: 添加 `_recover_missing_files()`
2. 仅在 completeness_check 失败时触发

---

## 7. 风险评估

| 风险 | 影响 | 缓解 |
|------|------|------|
| function 粒度增加 Phase1 耗时 | 大仓库 (1000+ 文件) 可能增加 30-60s | 提供 file+lazy 回退路径 |
| P1 prompt 改动导致过度保留 | Precision 可能下降 | P2 的 import 检查作为精确兜底 |
| P2 import 传递闭包膨胀 | 可能拉入整个 utils/ | 限制传递深度(max 2级)，结合语义分数过滤 |
| API 费用增加 | function 粒度下更多 LLM 摘要调用 | 利用 cache_enabled、batch_size 控制 |
