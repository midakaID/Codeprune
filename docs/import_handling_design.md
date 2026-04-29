# CodePrune 缺失导入 & 未定义名称处理方案

> 基于 aider linter.py / base_coder.py 的研究, 结合 CodePrune 特有的裁剪场景设计

---

## 1. 问题域分析

### 1.1 CodePrune 独有的根因

CodePrune 不同于 aider —— aider 处理的是**LLM 生成/编辑代码后**的通用 lint；CodePrune 处理的是**已知裁剪行为引起**的引用断裂。这意味着：

| 类型 | 根因 | 举例 | 正确处理 |
|------|------|------|----------|
| **A: 排除模块导入** | `out_of_scope` 模块被排除 | `from auth.service import AuthService` | 注释/删除整行 |
| **B: 裁剪残留引用** | 符号被裁剪但引用残留 | `handler = AuthMiddleware()` | 注释/删除使用行 |
| **C: 部分提取断裂** | 文件只提取了部分符号 | `from utils import a, b, c` 但 `c` 未保留 | 从 import 行移除 `c` |
| **D: 传递性缺失** | 闭包外依赖链 | `from models import User` (User 在保留范围内但引用了 Base) | 补充依赖 或 stub |
| **E: 第三方/stdlib** | 正常依赖 | `import flask` | **保留不动** |

**aider 不区分这些类型，全部交给 LLM。CodePrune 可以（也应该）利用裁剪上下文做精确分类。**

### 1.2 当前架构的处理能力

```
Phase 2.5 _pre_heal_cleanup     → 处理类型 A（粗粒度，注释 import 行）
validator._check_python_import  → 检测类型 A/D（import 目标不存在）
validator._check_python_references → 检测类型 C（from X import Y，Y 不在 X 中）
validator._check_python_undefined_names → 检测类型 B（pyflakes，severity=warning）
fixer._try_fix_missing_import   → 修复类型 A/D（注释或补充）
fixer._try_generate_stub        → 修复类型 D（stub 占位）
```

**缺口：**
1. 类型 B（裁剪残留引用）只标记为 warning，不主动修复
2. 类型 C（部分 import 断裂）only 粗粒度注释整行，不精确移除单个名称
3. pyflakes undefined name 的 warning 不参与 build 层的 real_errors 判定
4. 没有对"注释 import 后级联产生的 undefined name"做后续清理

---

## 2. 设计方案

### 2.1 分层策略: 确定性修复优先，LLM 兜底

```
                 ┌─────────────────────────────────┐
 Phase 2.5       │  Pre-heal: 批量清理 (已有)        │  → 类型 A: 注释 out_of_scope import
                 └────────────────┬────────────────┘
                                  ▼
                 ┌─────────────────────────────────┐
 NEW Layer 0     │  Import 精确修复 (确定性)          │  → 类型 A/C: 精确修改 import 语句
                 │  Cascade 清理 (确定性)             │  → 类型 B: 清理因 import 注释导致的 undefined
                 └────────────────┬────────────────┘
                                  ▼
                 ┌─────────────────────────────────┐
 Layer 1 Build   │  编译验证 + pyflakes (已有)        │
                 └────────────────┬────────────────┘
                                  ▼
                 ┌─────────────────────────────────┐
 Layer 1.5 NEW   │  Undefined Name 处理              │  → 类型 B 残留: 裁剪上下文判定 + 自动注释
                 └────────────────┬────────────────┘
                                  ▼
                 ┌─────────────────────────────────┐
 Layer 2-4       │  完整性/真实性/测试 (已有)          │
                 └─────────────────────────────────┘
```

### 2.2 Import 精确修复器 — `ImportFixer`

**位置:** `core/heal/import_fixer.py` ✅ 已实现

```python
class ImportFixer:
    """确定性 import 修复器 — 在 heal 循环前运行，不消耗修复轮次

    核心改进: 对 `from X import a, b, c` 精确移除不存在的名称,
    而非注释整行导致 a, b 也不可用。
    """
    def __init__(self, sub_repo: Path, out_of_scope: list[str]): ...
    def fix_all(self) -> tuple[int, dict[Path, set[str]]]: ...
```

**关键实现细节：**

1. **`_scan_sub_repo()`** — 启动时扫描子仓库所有 .py 文件，用 AST 提取每个模块的导出名称（函数、类、顶级变量、re-export）
2. **`_handle_import()`** — 处理 `import X` 语句，检测 stdlib / third-party / sub-repo / out_of_scope
3. **`_handle_import_from()`** — 处理 `from X import a, b, c`，精确检查每个名称是否在模块导出中
4. **AST 精确编辑** — 只移除不存在的 alias，保留有效的，从后向前应用避免行号偏移
5. **返回值** — `removed_names_by_file` 供 CascadeCleaner 使用

### 2.3 级联 Undefined Name 清理器 — `CascadeCleaner`

**位置:** `core/heal/import_fixer.py` ✅ 已实现

```python
class CascadeCleaner:
    """级联清理器: import 移除后，注释掉引用被移除名称的代码行"""
    def __init__(self, sub_repo: Path): ...
    def clean_all(self, removed_names_by_file: dict[Path, set[str]]) -> int: ...
```

**`_is_safe_to_comment()` 安全判断逻辑：**
- ✅ 安全注释：纯被移除名称引用行、调用语句目标是被移除名称(如 `register_routes(self)`)、赋值右值调用被移除名称(如 `auth = AuthService()`)
- ❌ 不注释：if/while/for 条件中使用、混合保留+移除名称的非调用场景
- 标记：`# [CodePrune] cascade-removed: <原行>`

### 2.4 Undefined Name 分类决策器 — `UndefinedNameResolver`

**位置:** `core/heal/import_fixer.py` ✅ 已实现

```python
class UndefinedNameClassification:
    IGNORE = "ignore"                # stdlib/builtins/typing → 不处理
    PRUNE_EXPECTED = "prune_expected" # 裁剪导致的，已级联清理
    FIXABLE = "fixable"              # 名称在子仓库其他模块中，可自动补 import
    LLM_REQUIRED = "llm_required"    # 需要 LLM 介入

class UndefinedNameResolver:
    """检测并分类 undefined names，自动补全可修复的 import"""
    def __init__(self, sub_repo, graph, removed_names=None): ...
    def resolve_all(self) -> tuple[int, list[dict]]: ...
```

**4 级分类逻辑 (`_classify()`)：**
1. Python builtins (`hasattr(builtins, name)`) → `IGNORE`
2. 常见 typing 名称 (Optional, List, Dict 等 30+) → `IGNORE`
3. 已被 ImportFixer 移除的名称 (`_removed_names`) → `PRUNE_EXPECTED`
4. 子仓库其他模块导出中存在 (`_module_exports`) → `FIXABLE`
5. CodeGraph 中有定义且文件存在 (`_graph_name_index`) → `FIXABLE`
6. 其他 → `LLM_REQUIRED`

**自动补 Import (`_auto_add_import()`)：**
- 仅无歧义时执行：`_find_unambiguous_source()` 要求恰好一个定义模块
- 先从子仓库导出查找，再查 CodeGraph
- 插入位置：最后一个 import 语句之后
- 标记：`# [CodePrune] auto-added`

**检测工具：** pyflakes（正则解析 `undefined name 'X'` 输出）

### 2.5 自动添加缺失 Import (已集成到 UndefinedNameResolver)

✅ 已实现，见 2.4 的 `_auto_add_import()` 方法。
关键安全约束：只处理无歧义（恰好一个定义模块）的情况，有歧义时留给 LLM。

### 2.6 Layer 1.5: Undefined Names 验证层

**位置:** `core/heal/fixer.py` ✅ 已实现

**在 `_validate_all_layers` 中的位置：**
```
Layer 1   (build)           → 编译/语法错误
Layer 1.5 (undefined_names) → ★ NEW: 残留 undefined names
Layer 2   (completeness)    → 功能完整性
Layer 3   (fidelity)        → 真实性
Layer 4   (test)            → 测试
```

**`_validate_undefined_names()` 实现：**
- 第一轮复用 `_pre_heal_cleanup` 中 `UndefinedNameResolver` 的结果（避免重复扫描）
- 后续轮重新扫描（LLM 修复可能已解决部分问题）
- 只将 `llm_required` 分类的名称作为 real_errors（warning 级别的跳过）
- 返回 `LayerResult(layer="undefined_names", ...)` 进入 `_fix_layer` 分发

### 2.7 Phase C: LLM 专用修复 — `_fix_undefined_names`

**位置:** `core/heal/fixer.py` ✅ 已实现

当 Layer 1.5 返回 `llm_required` 的 undefined names 时，进入专用修复路径：

```python
def _fix_undefined_names(self, sub_repo_path, errors) -> bool:
    """与通用 _fix_syntax_errors 的关键差异:
    1. 专用 prompt (FIX_UNDEFINED_NAMES): CodeGraph 上下文
    2. 按文件分组: 同一文件多个 undefined names 一次性修复
    3. 批量补丁: LLM 返回 fixes 数组
    """
```

**专用 Prompt (`FIX_UNDEFINED_NAMES`) 包含：**
- 每个 undefined name 的上下文行（±2 行，`█` 标记错误行）
- **CodeGraph 上下文** — 从 `graph.nodes` 查找名称的定义位置、类型、摘要
- **子仓库可用模块列表** — 帮助 LLM 判断是否可从现存模块导入
- **原仓库上下文** — `_get_original_context()` 获取
- **reflected_message** — 之前失败的修复尝试（最近 3 次）

**Prompt 修复策略引导规则：**
1. 优先注释（移除被裁剪依赖的引用）
2. 如名称明确来自已知模块（CodeGraph 上下文），添加正确 import
3. 类型注解用 `Any` + `from typing import Any` 替代
4. 不发明业务逻辑，不猜测函数行为

**辅助方法：**
- `_format_undefined_names()` — 格式化名称列表含上下文行
- `_extract_names_from_errors()` — 从错误消息提取名称
- `_get_graph_context_for_names()` — 从 CodeGraph 构建上下文信息
- `_list_available_modules()` — 列出子仓库 Python 模块

---

## 3. 实现状态

### Phase A: 确定性修复 ✅ 完成

| 编号 | 任务 | 修改文件 | 状态 |
|------|------|----------|------|
| A1 | Import 精确修复 — 只移除不存在的名称而非注释整行 | `import_fixer.py` ImportFixer | ✅ |
| A2 | 级联清理 — import 移除后清理引用行 | `import_fixer.py` CascadeCleaner | ✅ |
| A3 | `_pre_heal_cleanup` 调用 ImportFixer + CascadeCleaner | `fixer.py` | ✅ |

### Phase B: 分类决策 ✅ 完成

| 编号 | 任务 | 修改文件 | 状态 |
|------|------|----------|------|
| B1 | Undefined name 4 级分类器 | `import_fixer.py` UndefinedNameResolver | ✅ |
| B2 | 自动添加 import (基于 CodeGraph/子仓库导出无歧义查找) | `import_fixer.py` `_auto_add_import` | ✅ |
| B3 | 新增 Layer 1.5 undefined_names 验证层 | `fixer.py` `_validate_undefined_names` | ✅ |
| B4 | `_fix_layer` 分发 undefined_names | `fixer.py` | ✅ |
| B5 | `__init__.py` 导出 UndefinedNameResolver | `core/heal/__init__.py` | ✅ |

### Phase C: LLM 兜底 ✅ 完成

| 编号 | 任务 | 修改文件 | 状态 |
|------|------|----------|------|
| C1 | 专用 `FIX_UNDEFINED_NAMES` prompt (含 CodeGraph 上下文) | `core/llm/prompts.py` | ✅ |
| C2 | `_fix_undefined_names` 专用修复方法 (按文件分组 + 批量补丁) | `fixer.py` | ✅ |
| C3 | CodeGraph 上下文构建辅助方法 (4 个) | `fixer.py` | ✅ |
| C4 | reflected_message 支持 undefined name 修复 | `fixer.py` | ✅ |

---

## 4. 与 Aider 的关键差异

| 方面 | Aider | CodePrune (本方案) |
|------|-------|-------------------|
| 检测工具 | flake8 F821 | pyflakes + AST import 分析 |
| 分类能力 | 无 (全部交给 LLM) | 4 级分类 (ignore/prune_expected/fixable/llm_required) |
| 自动修复 | 无 (纯 LLM) | 确定性修复优先 (CodeGraph 驱动) |
| Import 处理 | LLM 添加 | 精确 AST 操作 (移除无效名称/自动补 import) |
| 级联处理 | 无 | 自动追踪移除名称的引用 |
| LLM 使用 | 所有错误 | 仅 "llm_required" 类 |
| 裁剪上下文 | 无 (通用编辑器) | 充分利用 out_of_scope / CodeGraph |

**核心理念: Aider 的 "全交给 LLM" 策略适合通用编辑，但 CodePrune 拥有裁剪上下文和 CodeGraph，应该用确定性修复覆盖 80%+ 的情况，只把真正模糊的交给 LLM。**

---

## 5. 完整数据流

```
_pre_heal_cleanup()
  ├── ImportFixer.fix_all()           → 精确修复 import 语句 (AST)
  │     返回 removed_names_by_file
  ├── CascadeCleaner.clean_all()      → 注释引用被移除名称的代码行
  └── UndefinedNameResolver.resolve_all()
        ├── pyflakes 检测 undefined names
        ├── _classify() 4 级分类
        ├── _auto_add_import() 修复 fixable
        └── 存储 unresolved → self._unresolved_undefined_names
               │
               ▼
heal 循环 (最多 max_rounds 轮):
  _validate_all_layers()
    ├── Layer 1: build           → 编译/语法
    ├── Layer 1.5: undefined_names → _validate_undefined_names()
    │     ├── 第一轮: 复用 _pre_heal_cleanup 结果
    │     └── 后续轮: 重新扫描 (LLM 可能已修复部分)
    ├── Layer 2: completeness    → 功能完整性
    ├── Layer 3: fidelity        → 真实性
    └── Layer 4: test            → 测试
               │
               ▼ (如果 Layer 1.5 返回 errors)
  _fix_layer("undefined_names")
    └── _fix_undefined_names()
          ├── 按文件分组 errors
          ├── _format_undefined_names()    → 带上下文行的名称列表
          ├── _get_graph_context_for_names() → CodeGraph 定义信息
          ├── _list_available_modules()     → 子仓库模块列表
          ├── Prompts.FIX_UNDEFINED_NAMES   → 专用 prompt
          └── LLM → fixes[] → _apply_patch() 逐一应用
```

## 6. 修改文件清单

| 文件 | 变更类型 | 内容 |
|------|----------|------|
| `core/heal/import_fixer.py` | **新建** | ImportFixer, CascadeCleaner, UndefinedNameResolver (3 个类, ~850 行) |
| `core/heal/fixer.py` | **修改** | _pre_heal_cleanup 重写, Layer 1.5, _fix_undefined_names + 4 辅助方法 |
| `core/heal/__init__.py` | **修改** | 导出 ImportFixer, CascadeCleaner, UndefinedNameResolver |
| `core/llm/prompts.py` | **修改** | 新增 FIX_UNDEFINED_NAMES prompt |
| `docs/import_handling_design.md` | **新建** | 本设计文档 |
