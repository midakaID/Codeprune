# Phase 2+3 协同机制审查与改进方案

> 基于 mini-blog "评论系统" 端到端失败的全链路审计
> 2026-04-11

---

## 一、当前架构：数据流与责任边界

```
Phase 1 (Graph)         Phase 2 (Prune)                Phase 3 (Heal)
┌──────────────┐   ┌──────────────────────────┐   ┌──────────────────────┐
│ AST 解析      │   │ 指令分析                  │   │ Pre-heal Cleanup     │
│ 依赖边构建    │──→│ 锚点定位                  │   │  ImportFixer          │
│ 语义标注      │   │ 闭包求解 (BFS+仲裁)       │   │  CascadeCleaner      │
│ CodeGraph     │   │ AST 手术 (Surgeon)        │──→│  UndefinedNameResolver│
│               │   │                          │   │  ReferenceAuditor     │
│               │   │  输出: required_nodes     │   │                      │
│               │   │        stub_nodes         │   │ Heal Loop (≤8轮)     │
│               │   │        out_of_scope       │   │  L1  Build           │
│               │   │                          │   │  L1.5 Undefined Names │
│               │   │  产物: 子仓库目录          │   │  L2.0 Runtime        │
│               │   │        diagnostics.json   │   │  L2.5 Boot           │
│               │   │        graph.pkl          │   │  L2  Completeness    │
│               │   │                          │   │  L3  Fidelity        │
│               │   │                          │   │  L3.5 Functional     │
│               │   │                          │   │  L4  Test            │
└──────────────┘   └──────────────────────────┘   └──────────────────────┘
```

### Phase 2 → Phase 3 的隐性契约

Phase 3 的所有修复策略都基于以下**隐性假设**：

| 假设 | Phase 2 保证？ | 实际情况 |
|------|:---:|---|
| A1: 闭包中的每个函数调用，其被调用函数也在闭包中 | ❌ | `create_comment` 调用 `notify_comment_reply`，但后者不在闭包中 |
| A2: 子仓库中每条 import 语句的目标符号在子仓库中存在 | ❌ | `from db import execute_insert`，但 `db/__init__.py` 没 re-export 它 |
| A3: `__init__.py` 的 re-export 只包含子仓库中存在的符号 | ⚠️ | Surgeon 基本保证，但不追踪间接引用 |
| A4: 子仓库可通过 python -c "import X" 测试所有模块 | ❌ | 由 Phase 3 RuntimeValidator 保证 |

**Phase 3 的设计假设是 A1-A3 大体成立，它只需做"微修"。** 当 Phase 2 系统性违反 A1 时，Phase 3 的"只能删不能补"修复策略就会让情况越来越糟。

---

## 二、mini-blog 失败的精确归因

### 根因 1：闭包 BFS 漏掉了 `notify_comment_reply` 和 `notify_post_author`

**闭包 required_nodes 包含**:
- `comments\\handlers.py::create_comment` ✅ — 调用了 `notify_comment_reply()`
- `notifications\\handlers.py::create_notification` ✅
- `notifications\\handlers.py::notify_followers` ✅

**闭包 required_nodes 不包含**:
- `notifications\\handlers.py::notify_comment_reply` ❌
- `notifications\\handlers.py::notify_post_author` ❌

**为什么 BFS 没有追踪到 create_comment → notify_comment_reply 的 CALLS 边？**

两种可能:
1. **图谱缺边**: Phase 1 的 AST 解析器没有为 `create_comment` → `notify_comment_reply` 创建 CALLS 边
2. **闭包决策排除**: BFS 走到了 `notify_comment_reply`，但它被归类为 PERIPHERAL/OUTSIDE 且被仲裁为 "exclude"

从闭包诊断看：
- `notifications\\handlers.py` 作为文件被包含（是 required_nodes）
- 但文件内的 `notify_comment_reply` 和 `notify_post_author` 没被选中
- 这意味着 **Surgeon 做了符号级裁剪** — 文件保留但只保留了闭包中的函数

这说明**不是 BFS 漏掉了它们，而是 BFS 的 `_import_symbol_level()` 在处理 `from notifications import notify_comment_reply` 时没有走精确匹配路径，或走了 strict 模式导致被过滤。**

更准确地说:
- `comments\\handlers.py` (CORE) → `from notifications import notify_comment_reply`
- BFS 沿 IMPORTS 边走到 `notifications\\handlers.py` (文件级)
- `_import_symbol_level()` 应该把 `notify_comment_reply` 加入 required
- 但它只加了 `create_notification` 和 `notify_followers`

**最可能的原因**: `_import_symbol_level` 的策略 1 (精确匹配) 查找的是**被 import 语句直接命名的符号**，但 import 语句是 `from notifications import ...`。根据代码，`_import_symbol_level` 处理的是 IMPORTS 边的 imported_symbols 元数据。如果图谱中 `comments/handlers.py → notifications/__init__.py` 的 IMPORTS 边的 imported_symbols 只包含了 `notify_comment_reply` 但该符号在 `notifications/__init__.py` (不是 handlers.py) 中查找... 那可能目标文件不对。

**或者更简单的解释**: `comments/handlers.py` → `notifications` 的 import 解析为指向 `notifications/__init__.py`（包级导入），而不是 `notifications/handlers.py`。那 `_import_symbol_level` 就在 `__init__.py` 的子节点中查找 `notify_comment_reply`。如果 `__init__.py` 只是一个 re-export 文件，它的 AST 子节点不包含 `notify_comment_reply` 的函数定义 → 精确匹配失败 → 走降级策略（可能拉入 CORE 子符号或全文件）。

这是一个**图谱解析 + 闭包 BFS 的联合缺陷**：barrel re-export 链没有被 BFS 穿透。

### 根因 2：Surgeon 的 __init__.py 处理不追踪 re-export 需求

Surgeon 的 `_filter_header_imports()` 只保留"闭包中有对应目标的 import"。

`db/__init__.py` 原始内容:
```python
from db.connection import get_connection, close_connection, init_tables, execute_query, execute_write
```

Surgeon 检查: `execute_insert` 是否在 `closure_names`？
- `db\\connection.py::execute_insert` 在 required_nodes 中 ✅
- 但 Surgeon 检查的是 `_filter_header_imports` 中的逐名匹配
- `db/__init__.py` 这行 import 是从原仓库复制的，它**本身就没有 execute_insert**
- Surgeon 不会"添加"新的 re-export —— 它只"保留"或"移除"已有的

所以这不是 Surgeon 的错，是**原仓库 barrel 不完整** + **Surgeon 没有补齐能力**。

### 根因 3：Phase 3 的修复策略偏"降级"

已在前文详细分析。

---

## 三、协同机制的架构缺陷

### 缺陷 D1：无"引用完整性"不变量

**现状**: Phase 2 和 Phase 3 之间没有定义明确的不变量。Phase 2 产出的子仓库可能包含引用悬垂（代码调用了被裁掉的函数），Phase 3 遇到后只能降级处理。

**应有不变量**:
> **INV-1 (引用闭合)**: 子仓库中每一条代码引用（import、函数调用、类继承），其目标要么存在于子仓库中，要么被 Phase 3 显式标记为"允许悬垂"（如 stub）。

### 缺陷 D2：符号级裁剪不追踪调用方

**现状**: Surgeon 对 `notifications/handlers.py` 做符号级裁剪时，只看 required_nodes 中有哪些函数 → 只保留 `create_notification` 和 `notify_followers`。但它**不检查**子仓库中是否有其他代码调用了被裁掉的函数。

**应有机制**:
> **INV-2 (调用覆盖)**: Surgeon 在裁剪文件 F 的函数集合时，应检查 required_nodes 中的所有文件是否调用了 F 中即将被裁掉的函数。如果有，要么保留该函数，要么在调用方标记为"需 Phase 3 修复"。

### 缺陷 D3：barrel re-export 链未被 BFS 穿透

**现状**: 当代码 `from notifications import notify_comment_reply` 时，BFS 沿 IMPORTS 边走到 `notifications/__init__.py`。但 `__init__.py` 只是 re-export 层，实际函数定义在 `notifications/handlers.py`。如果 BFS 的 `_import_symbol_level` 没有追踪到 handlers.py 中的 `notify_comment_reply`（因为它查的是 `__init__.py` 的子节点），这个函数就不会被加入闭包。

**应有机制**:
> **INV-3 (barrel 穿透)**: 闭包 BFS 在处理 `from package import symbol` 时，如果 `package/__init__.py` 通过 re-export 引入 `symbol`，应沿 re-export 链追踪到实际定义处，将定义处的函数加入 required_nodes。

### 缺陷 D4：Phase 3 无"创建代码"能力

**现状**: Phase 3 的修复工具箱只有:
- 删除 / 注释（ImportFixer / RuntimeFixer / CascadeCleaner）
- LLM 补丁（_fix_syntax_errors）
- Stub 空壳
- 从原仓库补充文件

**缺少**: 从原仓库**精确补充函数定义**的能力（不是补充整个文件，而是补充特定函数）。

---

## 四、改进方案（分层）

### 层级 A：Phase 2 闭包改进 — 消除引用悬垂

#### A1：BFS barrel 穿透 (对应 D3)

**修改文件**: `core/prune/closure.py :: _import_symbol_level()`

当 BFS 沿 IMPORTS 边到达 `__init__.py`（barrel 文件）时，如果 imported_symbols 在 barrel 的直接子节点中找不到定义，应**沿 barrel 的 re-export 链追踪**:

```python
# 策略 1.5 (新增): barrel re-export 穿透
if not referenced and file_node.name == "__init__.py":
    # 扫描 barrel 的 ImportFrom 子节点
    for edge in graph.get_outgoing(file_node_id):
        if edge.edge_type == EdgeType.IMPORTS:
            target_module = graph.get_node(edge.target)
            if target_module:
                for child in graph.get_children(target_module.id):
                    if child.name in imported_symbols:
                        referenced.add(child.id)
```

预期效果: `from notifications import notify_comment_reply` → BFS 会追踪到 `notifications/handlers.py::notify_comment_reply` 并加入 required_nodes。

#### A2：Surgeon 后置引用验证 (对应 D2)

**修改文件**: `core/prune/surgeon.py :: extract()`

在 Surgeon 完成所有文件提取后，增加一个验证步骤:

```python
def _validate_reference_closure(self, sub_repo_path, closure):
    """验证子仓库中所有代码引用的目标都存在"""
    missing = []
    for py_file in sub_repo_path.rglob("*.py"):
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    # 检查 alias.name 是否在子仓库的目标模块中可解析
                    if not self._symbol_resolvable(sub_repo_path, node.module, alias.name):
                        missing.append((py_file, node.module, alias.name))
    
    # 对缺失的符号，尝试从原仓库补充函数定义
    for file_path, module, symbol in missing:
        self._supplement_symbol(sub_repo_path, module, symbol)
```

预期效果: Surgeon 产出的子仓库在交给 Phase 3 之前就满足 INV-1。

### 层级 B：Phase 3 Heal 改进 — 增加"补齐"能力

#### B1：RuntimeFixer re-export 补齐 (即 M1)

已在前文详述。修改 `runtime_validator.py :: RuntimeFixer._fix_import_error()`，增加 `_try_add_reexport()` 方法。

#### B2：ImportFixer barrel 保守策略 (即 M3)

已在前文详述。`__init__.py` 中的 re-export 只删确定 out_of_scope 的，其他保留。

#### B3：ImportFixer 导出审计 (即 M2)

已在前文详述。fix_all() 后增加 `_audit_and_fix_missing_reexports()`。

#### B4：Phase 3 增加"函数补充"能力 (新)

**修改文件**: `core/heal/fixer.py :: _fix_runtime_errors()` + `runtime_validator.py :: RuntimeFixer`

当 RuntimeFixer 遇到"函数在子仓库中完全不存在"的情况时（如 `notify_comment_reply` 被 Surgeon 裁掉），不应只注释调用者的 import，而应**从原仓库补充该函数定义**:

```python
def _try_supplement_function(self, symbol: str, source_module: str) -> bool:
    """从原仓库补充缺失的函数定义到子仓库"""
    # 1. 在原仓库中定位函数
    source_path = self.source_repo_path / source_module.replace(".", "/")
    for py_file in [source_path / "handlers.py", source_path.with_suffix(".py")]:
        if not py_file.exists():
            continue
        tree = ast.parse(py_file.read_text())
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.FunctionDef) and node.name == symbol:
                # 2. 提取函数源码
                lines = py_file.read_text().splitlines()
                func_lines = lines[node.lineno - 1 : node.end_lineno]
                
                # 3. 追加到子仓库对应文件
                dst = self.sub_repo_path / py_file.relative_to(self.source_repo_path)
                if dst.exists():
                    dst_content = dst.read_text()
                    dst_content += "\n\n" + "\n".join(func_lines) + "\n"
                    dst.write_text(dst_content)
                    return True
    return False
```

预期效果: 即使 Phase 2 裁剪过度，Phase 3 也能从原仓库按需补充函数。

### 层级 C：架构级改进 — 建立跨 Phase 不变量

#### C1：引用闭合检查点

在 Phase 2 → Phase 3 交接时，增加一个**闭合检查点**:

```python
# pipeline.py
def _handoff_check(self, sub_repo_path: Path, closure: ClosureResult) -> list[str]:
    """检查子仓库是否满足引用闭合不变量"""
    violations = []
    for py_file in sub_repo_path.rglob("*.py"):
        # ... 扫描所有 import 和函数调用 ...
        # 对每个引用，检查目标是否在子仓库中可解析
        # 不可解析的记录为 violation
    return violations

# 如果有 violations，在进入 Phase 3 前尝试补充
# 这比在 Phase 3 的 heal 循环中处理更高效
```

#### C2：闭包诊断增强

在 `selection_diagnostics.json` 中增加:
- `reference_violations`: 哪些引用在子仓库中不可解析
- `barrel_gaps`: 哪些 barrel re-export 缺失
- `pruned_but_referenced`: 哪些函数被裁掉但仍被调用

---

## 五、改进优先级矩阵

| 方案 | 层级 | 解决的缺陷 | 复杂度 | 影响范围 | 推荐优先级 |
|------|------|-----------|--------|----------|:---:|
| **A1** BFS barrel 穿透 | Phase 2 | D3 闭包漏选 | 中 | 所有 Python 包级 import | **P0** |
| **B2** ImportFixer barrel 保守 | Phase 3 | D4 误删有效 export | 低 | 所有 __init__.py | **P0** |
| **B1** RuntimeFixer re-export 补齐 | Phase 3 | D4 不能补齐 barrel | 低 | barrel 不完整场景 | **P0** |
| **A2** Surgeon 后置引用验证 | Phase 2 | D2 引用悬垂 | 中 | 所有符号级裁剪 | **P1** |
| **B3** ImportFixer 导出审计 | Phase 3 | D4 主动补齐 | 中 | 所有包 import | **P1** |
| **B4** Phase 3 函数补充 | Phase 3 | D4 函数缺失 | 中 | 裁剪过度场景 | **P1** |
| **C1** 引用闭合检查点 | Pipeline | D1 无不变量 | 低 | 全局 | **P1** |
| **C2** 闭包诊断增强 | Pipeline | 可观测性 | 低 | 调试 | **P2** |

---

## 六、推荐实施路径

```
阶段 1（最小可行修复 — 修 Phase 2 根因）:
  A1: closure.py BFS barrel 穿透
  └→ 验证: mini-blog 闭包含 notify_comment_reply

阶段 2（Phase 3 防御加固）:
  B2: ImportFixer barrel 保守策略
  B1: RuntimeFixer re-export 补齐
  └→ 验证: mini-blog E2E 功能通过

阶段 3（系统性加固）:
  A2: Surgeon 后置引用验证
  B3: ImportFixer 导出审计
  B4: Phase 3 函数补充
  C1: 引用闭合检查点
  └→ 验证: 9/9 benchmark 无回归 + F1 ≥ 0.94
```

**理由**: A1 是根因修复（让 Phase 2 产出的闭包就是完整的），B1+B2 是安全网（即使 Phase 2 有遗漏，Phase 3 也能补救），A2+B3+B4+C1 是长期加固。

---

## 七、关于 barrel 问题的本质认知

**barrel (`__init__.py` re-export) 是 Python 包系统中最脆弱的一环**:
1. 它是一个手动维护的"公共 API 契约"，经常与实际代码不同步
2. 跨文件依赖链天然需要穿透 barrel 才能追踪到实际定义
3. 符号级裁剪必须同时处理 barrel 层和实现层

CodePrune 当前的 Phase 1 图谱已经正确建立了 IMPORTS 边，但 Phase 2 闭包在**穿透 barrel** 时有盲区，Phase 3 在**修复 barrel** 时有缺陷。修复这两个环节后，barrel 场景应该能从根本上解决。
