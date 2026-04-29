# Phase3 可运行性增强 — 最终改进方案

> 本文是对 `phase3_runnability_design.md` 的提炼。去掉冗余展开，聚焦于**可实施的改进路线**。

> **实施状态 (2026-04-10)**：三个模块全部实现并验证通过。
> - Module A (引用审计): ✅ `core/heal/reference_audit.py`
> - Module B (启动验证): ✅ `core/heal/boot_validator.py`
> - Module C (功能验证): ✅ `core/heal/functional_validator.py`
> - Framework benchmark F1=0.938 (无回归), 9 项平均 F1=0.941

---

## 一、问题本质

| 诊断 | 数据 |
|------|------|
| F1(文件选择准确性) | 0.944 — 优秀 |
| 平均可运行性 | ~17% — 不可接受 |
| 9/9 项目共有的 #1 问题 | 存活文件中保留了对已删除模块的**非 import 引用**（函数调用、配置值、注册表映射） |

**根本矛盾**：Phase2 以文件为粒度做"选/删"决策，Phase3 以编译错误为目标做"修"决策。**没有人负责"存活文件逻辑一致性"**这个中间地带。

---

## 二、设计原则

1. **编译/启动验证驱动修复闭环** — 每次修改后必须重新验证，而不是"修完就算"
2. **不做桩除非必要** — 优先清理引用（REMOVE/COMMENT），其次从原仓库恢复，最后才打桩
3. **渐进式验证** — 编译 → 启动 → 功能，每层独立可跳过
4. **不破坏 F1** — 所有改动只影响存活文件的内部内容，不改变文件选择集

---

## 三、改进方案（共 3 个模块）

### 模块 A：引用审计与清理 (Phase 2.5 增强) ✅ IMPLEMENTED

**时机**：Phase 2.5，在现有 ImportFixer 之后、heal 循环之前。不消耗修复轮次。

**解决的问题** (9/9 项目 #1 问题)：
- `app.py` 调用 `load_plugins("cache")` — cache 已删除
- `Config.plugins = ["auth", "cache"]` — cache 默认值
- `_BUILTIN_PLUGINS["cache"] = "framework.plugins.cache"` — 注册表映射
- `__init__.py` 中 `from .cleanup import run_cleanup` — 模块已删

#### 分两步

**Step 1: 静态引用扫描**（无 LLM，纯分析）

```python
class ReferenceAuditor:
    """扫描存活文件中对已删除模块/符号的非 import 引用"""

    def __init__(self, sub_repo_path, graph, excluded_files):
        # 从 CodeGraph + excluded_files 收集所有被删除的公开符号
        self.deleted_symbols = self._collect_deleted_symbols(graph, excluded_files)
    
    def audit(self) -> list[ReferenceIssue]:
        """返回所有悬挂引用"""
        issues = []
        for file in sub_repo_path.rglob("*"):
            if not is_code_file(file): continue
            content = file.read_text()
            for symbol in self.deleted_symbols:
                # 跳过 import 行（已被 ImportFixer 处理）
                # 检查函数调用、实例化、字符串引用、字典值等
                for match in find_symbol_usage(content, symbol, skip_imports=True):
                    issues.append(ReferenceIssue(file, match.line, symbol, match.context))
        return issues
```

**数据来源**：`self.deleted_symbols` 从两个渠道收集：
1. `excluded_files`（Phase2 已知的被排除文件）→ 提取文件中的 class/function/constant 名
2. CodeGraph 中存在但不在子仓库中的节点 → 提取 node.name

**Step 2: LLM 批量修复决策**

将 issues 按文件分组，一次性让 LLM 决策整个文件的修复方案：

```
Prompt (per file):
  File: framework/plugins/__init__.py
  
  The following references point to deleted modules/symbols:
  1. Line 24: `"cache": ("framework.plugins.cache", "CachePlugin")` → "cache" plugin was deleted
  2. Line 25: `"rate_limit": ("framework.plugins.rate_limit", "RateLimitPlugin")` → "rate_limit" was deleted
  
  Surviving plugins: ["auth"]
  
  For each reference, choose one action:
  - REMOVE: delete the line/element
  - COMMENT: comment out the line
  - KEEP: leave unchanged (false positive)
  
  Return JSON: [{line: 24, action: "REMOVE"}, {line: 25, action: "REMOVE"}]
```

**关键设计选择**：
- 不生成复杂的 REWRITE/STUB — 复杂修复留给 heal 循环
- 批量决策（每文件一次 LLM 调用），不逐引用调用
- 策略只有 REMOVE / COMMENT / KEEP — 足够处理 90% 的引用问题

#### 特殊处理：`__init__.py` / 注册表模式

这类文件有固定结构，可以用规则引擎（无 LLM）直接修复：

```python
class RegistrySync:
    """自动同步 __init__.py、__all__、插件注册表"""
    
    PATTERNS = {
        # Python __all__ = ["Cache", "Auth"] → 删除不存在的名称
        r'__all__\s*=\s*\[([^\]]+)\]': '_fix_all_list',
        # Python from .xxx import yyy → 检查 .xxx 是否存在
        r'from\s+\.(\w+)\s+import': '_fix_relative_import',
        # 通用字典映射 "key": ("module.path", "Class")
        r'["\'](\w+)["\']\s*:\s*\(': '_fix_dict_mapping',
    }
    
    def sync(self, file: Path, existing_modules: set[str]) -> int:
        """返回修复的条目数"""
```

### 模块 B：启动验证层 (Layer 2.5) ✅ IMPLEMENTED

**时机**：heal 循环内，在 Build 验证 (Layer 1) 通过后、Completeness 检查 (Layer 2) 之前。

**解决的问题**：编译通过 ≠ 能启动。很多运行时错误只在 import/实例化时才暴露。

#### 工作流

```
1. 识别入口点
   ├─ 方法 A: 特征文件 (app.py, main.py, index.ts, Main.java, main.c)
   ├─ 方法 B: 入口点评分 (借鉴 GitNexus 的 callRatio × export × namePattern)
   └─ 方法 C: user_instruction 中提到的模块/类

2. LLM 生成启动脚本
   输入:
     - 子仓库文件列表 + 入口点候选
     - user_instruction
     - 语言
   输出:
     - 一个最小脚本，尝试 import 入口模块 + 实例化核心对象
     - 预期输出: "BOOT_OK" 或 "BOOT_FAIL: {error}"

3. 执行启动脚本
   - subprocess, timeout=15s, cwd=sub_repo_path
   - 捕获 stdout + stderr

4. 结果处理
   ├─ BOOT_OK → 通过，进入下一层
   ├─ BOOT_FAIL → 提取错误信息
   │   ├─ ModuleNotFoundError → 回到引用审计或桩生成
   │   ├─ AttributeError → LLM 修复存活文件
   │   ├─ TypeError → LLM 修复参数/签名
   │   └─ 其它 → 传给 LLM 的反馈模板（借鉴 aider）:
   │       "I ran: python _codeprune_boot_test.py
   │        And got: {stderr}"
   └─ 脚本本身编译失败 → 重新生成（不扣修复轮次，最多 2 次）
```

#### 入口点评分 (移植 GitNexus 逻辑到 Python)

```python
ENTRY_PATTERNS = {
    '*': [r'^(main|init|start|run|setup)$', r'^handle[A-Z]', r'Controller$'],
    'python': [r'^app$', r'^(get|post|put|delete)_', r'^view_'],
    'java': [r'^do[A-Z]', r'Service$', r'^main$'],
    'c': [r'^main$', r'^init_', r'^start_'],
    'typescript': [r'^use[A-Z]'],
}

UTILITY_PATTERNS = [r'^(get|set|is|has)[A-Z]', r'^_', r'Helper$', r'Util$']

def score_entry_point(name, language, callee_count, caller_count, is_exported):
    base = callee_count / (caller_count + 1)
    export_mult = 2.0 if is_exported else 1.0
    name_mult = 1.5 if matches_any(name, ENTRY_PATTERNS.get(language, []) + ENTRY_PATTERNS['*']) \
                else 0.3 if matches_any(name, UTILITY_PATTERNS) else 1.0
    return base * export_mult * name_mult
```

#### 启动脚本 Prompt 模板

```
You are generating a minimal boot test script for a pruned code repository.

Language: {language}
Entry point candidates: {entry_points}
User instruction: {user_instruction}
Available files: {file_list}

Generate a script that:
1. Imports the main entry module(s)
2. Instantiates core objects (NO side effects: no network, no file I/O, no database)
3. Verifies key attributes/methods exist with hasattr/assert
4. Prints "BOOT_OK" on success, "BOOT_FAIL: {error}" on failure

Rules:
- MUST use try/except to catch ALL exceptions
- MUST print exactly "BOOT_OK" or "BOOT_FAIL: ..." as the last line
- NO infinite loops, NO blocking calls, NO external dependencies
- Keep it under 30 lines
```

### 模块 C：功能烟雾验证层 (Layer 3.5) ✅ IMPLEMENTED

**时机**：heal 循环内，Fidelity 检查 (Layer 3) 之后、Test 验证 (Layer 4) 之前。

**解决的问题**：编译通过 + 启动通过 ≠ 目标功能可用。需要验证核心业务路径不出异常。

#### 与启动验证的区别

| | 启动验证 (Layer 2.5) | 功能验证 (Layer 3.5) |
|---|---|---|
| **验证目标** | import 不崩溃 | 核心路径不崩溃 |
| **脚本复杂度** | ~15 行 | ~30-60 行 |
| **需要的上下文** | 文件列表 + 入口点 | user_instruction + 类/函数签名 |
| **可信度保障** | 简单，误报少 | 需要先在原仓库验证 baseline |
| **失败时的修复** | 回到引用审计/桩 | 需 LLM 分析 + 修复循环 |

#### 可信度保障：两阶段验证

```
Stage 1: 在原仓库运行功能测试脚本
  ├─ 通过 → 脚本可信，进入 Stage 2
  └─ 失败 → 脚本本身有问题，重新生成（最多 2 次）

Stage 2: 在子仓库运行同一脚本
  ├─ 通过 → 功能验证通过
  └─ 失败 → 真实的子仓库问题，进入修复循环
```

#### 轮次预算

功能验证是最"昂贵"的层（LLM 调用多、执行时间长），需要严格预算：

```
功能验证总预算: 2 轮
  - 轮 1: 生成脚本 + 原仓库验证 + 子仓库执行 + 如失败则 LLM 修复
  - 轮 2: 重新验证 + 如失败则放弃
  
如果功能验证连续 2 轮不改善 → skip_layers.add("functional") → 继续
```

#### 是否默认启用

**建议**：默认 `enable_functional_validation = False`。原因：
1. 功能验证依赖 LLM 生成高质量测试脚本，可信度不如编译/启动验证
2. 每次额外 2+ 次 LLM 调用 + subprocess 执行，显著增加耗时
3. 启动验证 (Layer 2.5) 已能发现 80% 的运行时问题

用户可通过配置开启：`heal.enable_functional_validation = true`

---

## 四、执行流追踪辅助（借鉴 GitNexus）

**应用场景**：辅助模块 A 的引用审计和模块 C 的功能验证。

从 CodeGraph 做 BFS 追踪入口点的调用链：

```python
def trace_execution_flow(graph, entry_node_id, max_depth=10):
    """追踪入口点的调用链，识别断裂点"""
    visited = set()
    queue = [(entry_node_id, 0)]
    trace = []
    broken_links = []  # 调用链中断的位置
    
    while queue:
        node_id, depth = queue.pop(0)
        if node_id in visited or depth > max_depth:
            continue
        visited.add(node_id)
        node = graph.get_node(node_id)
        if node is None:
            broken_links.append(node_id)  # 节点对应的文件被删除
            continue
        trace.append(node_id)
        for edge in graph.get_outgoing_edges(node_id):
            if edge.edge_type in ("calls", "imports"):
                queue.append((edge.target_id, depth + 1))
    
    return trace, broken_links
```

`broken_links` 就是需要修复或打桩的精确位置，反馈给引用审计或桩生成。

---

## 五、桩策略精简

**原则**：桩是最后手段，不是首选。

```
决策优先级:
  1. 清理引用 (REMOVE/COMMENT) — 如果引用只是残留，删掉即可
  2. 从原仓库恢复文件 — 如果被 3+ 存活文件依赖，说明是 Phase2 误删
  3. 生成桩 — 只对边界依赖（已删除功能的接口）打桩
```

**桩生成触发条件**（严格）：
1. 编译错误中 "cannot find symbol" / "ModuleNotFoundError"
2. 且该符号属于 `excluded_files`（故意删除的，不是误删的）
3. 且引用审计未能通过 REMOVE/COMMENT 解决

**桩的形式**：现有 `_try_generate_stub` 已实现 Java/Python/TS 的桩生成框架，保持现有实现，不扩展。

---

## 六、反馈格式增强（借鉴 aider）

当前 HealEngine 的错误反馈给 LLM 时传的是 `error.message`。增强为 aider 的结构化格式：

```python
def format_error_for_llm(self, error: ValidationError, file_content: str) -> str:
    """生成带上下文标记的错误反馈（借鉴 aider 的 TreeContext 格式）"""
    lines = file_content.splitlines()
    error_line = error.line - 1  # 0-indexed
    
    # ±3 行上下文
    start = max(0, error_line - 3)
    end = min(len(lines), error_line + 4)
    
    context = ""
    for i in range(start, end):
        marker = "█" if i == error_line else " "
        context += f"{marker} {i+1:4d}│ {lines[i]}\n"
    
    return (
        f"## Error in {error.file_path}\n\n"
        f"{error.message}\n\n"
        f"## See relevant line below marked with █.\n\n"
        f"```\n{context}```"
    )
```

对于启动/功能验证的 subprocess 执行结果：

```python
def format_run_result_for_llm(self, command: str, output: str) -> str:
    """借鉴 aider 的 run_output 模板"""
    return f"I ran this command:\n\n{command}\n\nAnd got this output:\n\n{output}"
```

---

## 七、整体架构变更

### 新的 Phase3 流水线

```
heal()
  │
  ├─ Phase 2.5: 预清理 (增强)
  │   ├─ Layer A: Import 清理 (现有 ImportFixer + CascadeCleaner)
  │   ├─ Layer B: 引用审计 + LLM 清理 (新增 ReferenceAuditor) ★
  │   └─ Layer C: 注册表同步 (新增 RegistrySync) ★
  │
  └─ 验证-修复循环 (增强)
      ├─ Layer 1: Build 验证 (现有)
      ├─ Layer 1.5: Undefined Names (现有)
      ├─ Layer 2: Completeness (现有，增强为使用执行流断裂检测)
      ├─ Layer 2.5: Boot 验证 (新增) ★
      ├─ Layer 3: Fidelity (现有)
      ├─ Layer 3.5: Functional 验证 (新增，默认关闭) ★
      └─ Layer 4: Test (现有 U8)
```

### 新增/修改文件

```
core/heal/
  ├─ fixer.py           (修改: 增加 Layer 2.5 / 3.5 集成) ✅
  ├─ validator.py        (不变)
  ├─ import_fixer.py     (不变)
  ├─ finalize.py         (不变)
  ├─ reference_audit.py  (新增: ReferenceAuditor + RegistrySync) ✅
  ├─ boot_validator.py   (新增: 启动脚本生成/执行/入口点评分) ✅
  └─ functional_validator.py (新增: 两阶段功能验证) ✅
```

### 新增配置

```python
@dataclass
class HealConfig:
    # ... 现有 ...
    enable_reference_audit: bool = True         # Phase 2.5 Layer B
    enable_registry_sync: bool = True           # Phase 2.5 Layer C
    enable_boot_validation: bool = True         # Layer 2.5
    enable_functional_validation: bool = False  # Layer 3.5 (默认关闭)
    boot_timeout: int = 15                      # 启动验证超时(秒)
    functional_timeout: int = 30                # 功能验证超时(秒)
```

### 新增 Prompt

```python
class Prompts:
    # ... 现有 ...
    
    AUDIT_REFERENCES = """..."""       # 引用审计 LLM 决策
    GENERATE_BOOT_TEST = """..."""     # 启动验证脚本生成
    GENERATE_FUNCTIONAL_TEST = """..."""  # 功能验证脚本生成
```

---

## 八、实施路线

### Phase 1: 引用审计 (P0，最高优先) ✅ DONE

**预期效果**：解决 9/9 项目的 #1 问题，可运行性 17% → ~50%

> **实施结果**: `core/heal/reference_audit.py` (~450 行), Framework benchmark 验证 4 audit actions, F1 无回归

| 步骤 | 内容 | 工作量 |
|------|------|--------|
| 1.1 | 实现 `ReferenceAuditor._collect_deleted_symbols()` — 从 CodeGraph 收集 | 小 |
| 1.2 | 实现 `ReferenceAuditor.audit()` — 多语言符号引用扫描 | 中 |
| 1.3 | 实现 `RegistrySync.sync()` — 规则引擎处理 `__init__.py`/注册表 | 中 |
| 1.4 | 实现审计结果 → LLM 批量决策 (REMOVE/COMMENT/KEEP) | 中 |
| 1.5 | 集成到 `_pre_heal_cleanup()` 中 | 小 |
| 1.6 | 在 framework benchmark 上验证 | 小 |

### Phase 2: 启动验证 (P1) ✅ DONE

**预期效果**：发现并修复运行时问题，可运行性 ~50% → ~70%

> **实施结果**: `core/heal/boot_validator.py` (~340 行), Framework benchmark Boot 触发并正确降级, F1=0.938

| 步骤 | 内容 | 工作量 |
|------|------|--------|
| 2.1 | 移植入口点评分系统 (GitNexus → Python) | 小 |
| 2.2 | 实现启动脚本 LLM 生成 + Prompt | 中 |
| 2.3 | 实现 `BootValidator.validate()` — subprocess 执行 + 结果解析 | 中 |
| 2.4 | 集成到 heal 循环的 `_validate_all_layers()` | 小 |
| 2.5 | 增强错误反馈格式 (aider 的 █ 标记格式) | 小 |
| 2.6 | 在 4+ benchmark 上验证 | 中 |

### Phase 3: 功能验证 (P2，可选) ✅ DONE

**预期效果**：验证核心业务路径，可运行性 ~70% → ~80%+

> **实施结果**: `core/heal/functional_validator.py` (~300 行), 两阶段验证工作正常, 默认关闭, F1 无回归

| 步骤 | 内容 | 工作量 |
|------|------|--------|
| 3.1 | 实现功能测试脚本 LLM 生成 + 两阶段验证 | 高 |
| 3.2 | 实现执行流追踪辅助 (BFS 断裂检测) | 中 |
| 3.3 | 集成到 heal 循环 | 中 |
| 3.4 | 全量 benchmark 验证 | 高 |

---

## 九、已知风险

| 风险 | 缓解 |
|------|------|
| 引用审计误报 (把正常引用标记为悬挂) | LLM 决策中加 KEEP 选项；只审计 deleted_symbols 中的名称 |
| 启动测试脚本 LLM 生成质量不稳定 | 最多重试 2 次；模板化 Prompt 降低自由度 |
| 修复循环增加 API 调用量/耗时 | Layer 2.5/3.5 各限 2 轮；可通过配置关闭 |
| 跨语言工具链缺失 | 预检查 + 优雅降级（跳过验证层） |
| 桩代码引入行为偏差 | 桩是最后手段；标记 synthetic；Fidelity 检查 |
