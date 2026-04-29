# CodeHeal v2 — 完整改进设计

> 设计原则：**不做 A+B 拼接**，而是从 CodePrune 的独特问题域出发，吸收开源方案中已验证的机制。

---

## 一、问题域定位

CodePrune 的自愈 (heal) 任务与 aider / SWE-agent / Agentless 解决的问题有**本质区别**：

| 维度 | aider / SWE-agent | CodePrune heal |
|------|-------------------|----------------|
| 输入 | 人写的功能需求 / GitHub issue | 编译器/运行时的**确定性错误消息** |
| 代码来源 | LLM 从零生成或大规模修改 | 原仓库代码已存在，只需**最小修补** |
| ground truth | 无（LLM 自行判断） | 有（原仓库 = ground truth） |
| 错误类型 | 开放式 | 有限集合（缺声明、缺导入、类型不匹配…） |
| 修复目标 | 功能正确 | **语义保真 + 可编译运行** |

**结论**：CodePrune 不需要变成通用 coding agent，它的 heal 任务是**约束优化问题** — 在保真度约束下，以最小修改消除编译/运行时错误。

---

## 二、当前系统的七个结构性问题

### P1. C/C++ 函数名解析错误 ✅ 已修复
`_extract_name()` 将 C 函数的返回类型误判为函数名，导致 CodeGraph 中所有 C 函数的名称、摘要、嵌入、锚点、闭包全部失真。

### P2. Patch 应用脆弱
`_apply_patch()` 使用 `SequenceMatcher.ratio() ≥ 0.78` 的编辑距离回退，存在两个问题：
1. **误匹配**：78% 相似度阈值容易匹配到错误位置
2. **竞争条件**：前一个 patch 修改文件后，后续 patch 的 `original_code` 失效

### P3. 错误上下文截断严重
- `file_content[:6000]` — 6000 字符后的代码对 LLM 不可见
- `error.message[:300]` — 复杂的模板错误被截断
- 单个错误修复看不到其他错误 → 修一个引入一个

### P4. ErrorDispatcher 缺少"为什么"
Dispatcher 的确定性修复（补 include、补 import）只做了"怎么修"，没有告诉后续 LLM "为什么缺"。当 Dispatcher 修复失败时，LLM 拿到的上下文中缺少关键信息。

### P5. Reference Audit 误删声明
`reference_audit.py` 的 `_collect_deleted_symbols()` 不知道 surgeon 的 `_pair_c_headers()` 会自动补充文件，导致将实际存活的符号判定为"已删除"，LLM 随后 COMMENT 掉了本该保留的声明。

### P6. Stub 质量低
当前 stub 生成使用 `*args, **kwargs`（Python）或空函数体（C），不感知原函数签名：
- 调用方的类型检查仍然失败
- C/C++ 的 stub 缺少正确的参数类型和返回值

### P7. Phase2 误删代码补回能力分散且粒度不足

Phase2（锚点 + 闭包）因 LLM 判断误差、图质量问题、闭包传播不完全等原因，会在 file / function / statement 三种粒度上误删代码。当前系统有三个独立的补回机制，但各有盲区：

| 组件 | 补回粒度 | 触发条件 | 盲区 |
|------|---------|---------|------|
| `HealEngine._supplement_missing()` | 文件级 | completeness LLM 判定缺失 | LLM 判断基于摘要不看代码，误报率高；只能补整文件 |
| `RuntimeFixer._try_supplement_symbol()` | 函数级 | import 错误 / undefined function | 只支持 `def`/`class` 形态，不能补 C macro/typedef/struct |
| `ReferenceAudit._apply_actions()` | 行级 | 悬挂引用扫描 | 只做 COMMENT/REMOVE，**不做补回**；且会误删正确声明(P5) |

**核心问题**：
1. **没有统一的"从原仓库恢复"策略** — 三个组件各自为政，缺少协调
2. **statement 级误删无补回** — Reference Audit 注释掉的行、LLM patch 意外删掉的语句，没有恢复机制
3. **补回无去重** — RuntimeFixer 多次补同一符号时无冲突检测
4. **C/C++ 非函数符号无法补回** — macro、typedef、struct、enum 等 Phase2 裁掉后无人处理

---

## 三、改进方案

### 3.1 SEARCH/REPLACE 编辑格式（替换当前 patch 格式）

**来源启发**：aider 的 diff edit format

**问题**：当前 `_apply_patch()` 依赖 LLM 输出 `original_code` + `fixed_code` 的 JSON，存在：
- JSON 转义地狱（`\"`, `\\n` 等）
- LLM 输出的 `original_code` 经常含微小差异（空格、换行）
- 多个 patch 间的文件状态不一致

**方案**：

将 LLM 输出格式从 JSON 切换为 **SEARCH/REPLACE block**：

```
path/to/file.c
<<<<<<< SEARCH
// exact lines from the file
int old_function(void) {
    return 0;
}
======= 
// replacement lines
int old_function(void) {
    return 42;
}
>>>>>>> REPLACE
```

**具体改动**：

1. **修改 `Prompts.FIX_SYNTAX_ERROR`**：输出格式从 JSON 改为 SEARCH/REPLACE block
2. **新增 `_parse_search_replace_blocks()`**：解析 LLM 输出中的所有 SEARCH/REPLACE block
3. **`_apply_patch()` 适配**：先尝试精确匹配 SEARCH block，再 fallback 到 `_find_context_core()` 行级匹配
4. **删除** `_find_by_edit_distance()` — 78% 阈值的编辑距离匹配不再需要

**为什么不直接用 unified diff**：
- aider 的实验数据显示 SEARCH/REPLACE 的 LLM 成功率 > unified diff
- SEARCH/REPLACE 对 LLM 认知负担更低（不需要计算行号偏移）
- CodePrune 的修复通常是 1-5 行的小改动，SEARCH/REPLACE 最适合

### 3.2 全错误感知的修复 prompt（解决截断问题）

**问题**：当前每个错误独立送入 LLM，LLM 看不到全局错误分布。

**方案**：实施 **两阶段修复协议**（改进当前 Architect 模式）：

#### 阶段 A — 全局分析（每轮一次）

将**所有错误**（不截断消息）+ **错误所在文件的完整内容** 一次性送入 LLM：

```
你需要修复以下编译错误。这些代码从一个更大的仓库中裁剪出来，错误通常是因为被裁剪掉的依赖。

=== 所有错误 ===
[完整错误列表，不截断 message]

=== 涉及文件 ===
[每个出错文件的完整内容]

=== 原仓库参考 ===
[错误涉及的原仓库文件内容]

输出一系列 SEARCH/REPLACE block 来修复所有错误。
```

#### 阶段 B — 验证

应用所有 SEARCH/REPLACE block → 重新编译 → 如果仍有错误，进入下一轮。

**关键改动**：
1. **去掉 `file_content[:6000]` 截断** — 改用智能截取：只发送错误所在函数/类 + 其上下各 50 行
2. **去掉 `error.message[:300]` 截断** — 完整传递错误消息
3. **合并 Architect + Fix 为单步** — 当前分两次 LLM 调用（architect 分析 → 逐个 fix），改为一次调用输出所有 SEARCH/REPLACE block
4. **每轮最多处理的错误数提高到 20**（当前 `capped = errors[:10]`）

**Token 预算管理**：
- 如果涉及文件总内容超过模型上下文窗口的 60%，按错误密度排序只保留最高密度的文件
- 原仓库参考只发送与错误直接相关的部分（通过 CodeGraph 的调用关系定位）

### 3.3 签名感知的 Stub 生成

**问题**：当前 stub `def foo(*args, **kwargs): pass` 不满足类型检查。

**方案**：利用 CodeGraph 中已存在的元数据生成精确 stub：

```python
def _generate_typed_stub(self, symbol_name: str, graph: CodeGraph) -> str:
    node = graph.find_by_name(symbol_name)
    if not node or not node.source_code:
        return self._generate_generic_stub(symbol_name)  # fallback
    
    # 从原代码提取签名（只要第一行/声明）
    signature = self._extract_signature(node.source_code, node.language)
    
    # 根据语言生成最小实现
    if node.language == "python":
        return f"{signature}\n    raise NotImplementedError('Pruned by CodePrune')"
    elif node.language in ("c", "cpp"):
        return_type = self._extract_return_type(node.source_code)
        return f"{signature} {{\n    /* Pruned by CodePrune */\n    return {default_for(return_type)};\n}}"
```

**具体改动**：
1. `_try_generate_stub()` 改为先查 CodeGraph 获取签名
2. 对于 C/C++，从原仓库 .h 文件提取声明，生成匹配的空实现
3. 对于 Python，保留原函数签名（参数名、类型注解、默认值）

### 3.4 Reference Audit 的 surgeon 感知修复

**问题**：`_collect_deleted_symbols()` 不知道 surgeon 的 `_pair_c_headers()` 添加了哪些文件。

**方案**：

在 surgeon 的 `assemble()` 阶段，记录所有**自动补充**的文件到 `selection_diagnostics.json`：

```python
# surgeon.py — _pair_c_headers() 结束时
diagnostics["auto_paired_files"] = list(auto_paired)
```

在 `reference_audit.py` 的 `_collect_deleted_symbols()` 中：
```python
# 读取 auto_paired_files，将其中的符号从 deleted_symbols 中排除
auto_paired = diagnostics.get("auto_paired_files", [])
```

**具体改动**：
1. `surgeon.py`：在 `_pair_c_headers()` 中将配对结果写入 diagnostics
2. `reference_audit.py`：读取 diagnostics 中的 `auto_paired_files`，排除这些文件中的符号

### 3.5 Dispatcher 的上下文增强

**问题**：Dispatcher 修复失败时，后续 LLM 不知道"为什么这个 include 丢了"。

**方案**：Dispatcher 在修复时生成 **repair context annotation**：

```python
@dataclass
class RepairContext:
    error: ValidationError
    attempted_fix: str          # 尝试过的修复
    fix_result: str             # "success" | "failed" | "partial"  
    root_cause: str             # "symbol X was in pruned file Y"
    graph_evidence: str         # CodeGraph 中的引用关系
```

当 Dispatcher 修复失败时，将 `RepairContext` 附加到 LLM prompt 中：

```
Dispatcher 尝试过的修复（失败）：
- 错误: implicit declaration of function 'registry_register_builtin'
- 尝试: #include "registry.h"
- 失败原因: registry.h 已存在于文件中
- 图分析: registry_register_builtin 定义于 registry.c（已包含在子仓库中），声明在 registry.h 第15行
```

这样 LLM 能直接定位问题（声明被注释了）而不是瞎猜。

### 3.6 统一的原仓库代码恢复机制（SourceRecovery）

**问题**：Phase2 误删代码在 file / function / statement 三种粒度上发生，当前三个独立组件各自补回，缺少统一策略，且 statement 级误删完全没有恢复机制。

**方案**：引入 `SourceRecovery` 统一恢复层，所有"从原仓库取回代码"的操作都通过它执行。

#### 核心思想：**错误驱动 + diff 驱动的恢复，而非凭想象生成**

对于每个编译/运行时错误，在送入 LLM 之前，先尝试**确定性恢复**：

```
错误消息 → 定位缺失符号 → CodeGraph 查找原定义 → 从原仓库精确提取 → 插入子仓库
```

只有当确定性恢复失败时，才交给 LLM。

#### 3.6.1 三层恢复粒度

```python
class SourceRecovery:
    """统一的原仓库代码恢复器"""
    
    def recover_file(self, rel_path: str) -> bool:
        """文件级：整文件从原仓库复制（当前 _supplement_missing 的功能）"""
        
    def recover_symbol(self, symbol_name: str, target_file: Path) -> bool:
        """符号级：从原仓库提取 function/class/struct/macro/typedef 的完整定义
        利用 CodeGraph 的 byte_range 精确提取，支持：
        - Python: def, class
        - C/C++: function, struct, typedef, enum, #define macro
        - Java: method, class
        - TypeScript: function, class, interface, type
        """
        
    def recover_statement(self, file_path: Path, line_range: tuple[int, int]) -> bool:
        """语句级：从原仓库恢复指定行范围的代码
        用于：Reference Audit 误 COMMENT 的行、LLM patch 意外删除的代码
        对比子仓库与原仓库的 diff，将被删除的行恢复
        """
```

#### 3.6.2 符号级恢复的扩展（C/C++ 非函数符号）

当前 RuntimeFixer 只能补 `def`/`class`，新方案扩展到所有 tree-sitter 可解析的符号类型：

```python
# 利用 tree-sitter 的 node.type 精确提取
RECOVERABLE_NODE_TYPES = {
    "c": ["function_definition", "struct_specifier", "enum_specifier", 
          "type_definition", "preproc_def", "preproc_function_def"],
    "python": ["function_definition", "class_definition"],
    "java": ["method_declaration", "class_declaration", "interface_declaration"],
    "typescript": ["function_declaration", "class_declaration", 
                   "interface_declaration", "type_alias_declaration"],
}
```

#### 3.6.3 去重与冲突检测

```python
def _check_already_recovered(self, symbol: str, target: Path) -> bool:
    """检查符号是否已经存在于目标文件中（避免重复插入）"""
    content = target.read_text()
    # 精确检查：tree-sitter 解析目标文件，查找同名顶层符号
    # 不用简单的字符串匹配（避免注释/字符串中的假阳性）
```

#### 3.6.4 恢复优先级

在修复流程中的位置（插入到 Phase 0 阶段，在 LLM 和 Dispatcher 之前）：

```
编译错误 → SourceRecovery.try_recover(error) → [成功则跳过 LLM]
                                               → [失败则继续 Dispatcher → LLM]
```

具体策略：

| 错误类型 | 恢复动作 |
|---------|---------|
| `implicit declaration of function 'X'` | `recover_symbol("X", target_file)` — 从原仓库补回函数定义或声明 |
| `unknown type name 'X'` | `recover_symbol("X", target_file)` — 补回 struct/typedef/enum |
| `No module named 'X'` | `recover_file("X.py")` — 补回整个模块 |
| `cannot import name 'X' from 'Y'` | `recover_symbol("X", "Y.py")` — 在目标文件中补回符号定义 |
| `undeclared identifier 'X'` | `recover_symbol("X", target_file)` — 补回变量/常量/宏 |

#### 3.6.5 statement 级恢复（新能力）

针对 Reference Audit 和 LLM patch 造成的语句级损伤：

```python
def recover_commented_lines(self, file_path: Path) -> int:
    """扫描 [CodePrune] audit 注释标记，验证被注释的代码在当前上下文中是否应该恢复
    
    判断标准：
    1. 被注释行引用的符号，如果在子仓库中实际存在 → 恢复
    2. 被注释行引用的符号，如果在子仓库中不存在 → 保持注释
    """
    # 扫描所有 "// [CodePrune] audit:" 或 "# [CodePrune] audit:" 标记
    # 提取被注释的原始代码行
    # 检查其中引用的符号是否在子仓库中存在（通过 CodeGraph 或文件扫描）
    # 存在 → 取消注释恢复原代码
```

这直接解决了 mini-query-engine 中 `registry_register_builtin` 声明被误 COMMENT 的问题 — 因为 `registry.c` 实际存在于子仓库中，SourceRecovery 会检测到并恢复该声明。

### 3.7 编译验证的增量化

**问题**：每轮修复后全量重编译，C/C++ 大项目耗时显著。

**方案**（优先级低，作为 P2 优化）：
1. 记录上轮编译通过的文件 → 只重新编译被 patch 修改的文件及其依赖
2. 对于 Python，只对被修改的文件执行 `py_compile` + undefined name scan

---

## 四、实施优先级

| 优先级 | 项 | 改动范围 | 预期收益 |
|--------|-----|---------|---------|
| **P0** | 3.4 Reference Audit surgeon 感知 | surgeon.py + reference_audit.py | 消除 C/C++ 仓库的声明误删 |
| **P0** | 3.1 SEARCH/REPLACE 编辑格式 | prompts.py + fixer.py | 大幅提升 patch 应用成功率 |
| **P0** | 3.6 SourceRecovery 统一恢复 | 新增 source_recovery.py + fixer.py | 解决 Phase2 误删的所有粒度补回 |
| **P1** | 3.2 全错误感知修复 prompt | prompts.py + fixer.py | 减少修复轮次，避免修一个引入一个 |
| **P1** | 3.3 签名感知 Stub | fixer.py | 消除类型检查 stub 错误 |
| **P2** | 3.5 Dispatcher 上下文增强 | error_dispatcher.py + fixer.py | 提升 LLM 修复准确率 |
| **P2** | 3.7 增量编译 | validator.py | 减少验证耗时 |

---

## 五、SEARCH/REPLACE 格式详细设计

### 5.1 LLM Prompt 模板

```
You are fixing compilation errors in a pruned code repository.
The code was extracted from a larger repository. Errors are caused by pruned dependencies.

CRITICAL RULES:
1. Fix ONLY the reported errors. Make MINIMAL changes.
2. Prefer commenting out or removing problematic code over inventing new logic.
3. Use ONLY patterns from the original repository context provided.
4. Each SEARCH block must contain EXACT lines from the current file.
   Include 2-3 lines of unchanged context before and after the target lines.

=== Errors ===
{all_errors}

=== Current Files ===
{files_with_errors}

=== Original Repository Context ===
{original_context}

Output SEARCH/REPLACE blocks for all fixes:

path/to/file.ext
<<<<<<< SEARCH
exact existing lines
=======
replacement lines
>>>>>>> REPLACE
```

### 5.2 解析器

```python
import re

_SR_PATTERN = re.compile(
    r'^(.+?)\n'                     # filename
    r'<<<<<<< SEARCH\n'
    r'(.*?)'                        # search block
    r'=======\n'  
    r'(.*?)'                        # replace block
    r'>>>>>>> REPLACE',
    re.MULTILINE | re.DOTALL,
)

def parse_search_replace_blocks(text: str) -> list[FixPatch]:
    patches = []
    for m in _SR_PATTERN.finditer(text):
        patches.append(FixPatch(
            file_path=Path(m.group(1).strip()),
            original_code=m.group(2),
            fixed_code=m.group(3),
            explanation="",
        ))
    return patches
```

### 5.3 应用顺序

同一文件的多个 SEARCH/REPLACE block **从后往前应用**（避免行号偏移影响后续匹配）：

```python
def _apply_patches_to_file(self, file_path, patches):
    content = file_path.read_text(encoding="utf-8")
    # 按匹配位置从后往前排序
    located = []
    for p in patches:
        idx = content.find(p.original_code)
        if idx >= 0:
            located.append((idx, p))
    located.sort(key=lambda x: x[0], reverse=True)
    for idx, p in located:
        content = content[:idx] + p.fixed_code + content[idx + len(p.original_code):]
    file_path.write_text(content, encoding="utf-8")
```

---

## 六、多语言架构：语言无关内核 + 语言特化规则

### 6.1 问题：Python 独占的自愈能力

当前 heal 系统对 6 种语言的支持极度不均衡：

| 能力维度 | Python | C/C++ | Java | TypeScript | JavaScript |
|---------|--------|-------|------|-----------|------------|
| ErrorDispatcher patterns | 2 | 5 | 1 | 1 | 0 |
| Import 精确修复 | ✅ ImportFixer | ❌ | ❌ | ❌ | ❌ |
| Undefined name 修复 | ✅ UndefinedNameResolver | ❌ | ❌ | ❌ | ❌ |
| 符号级运行时补回 | ✅ RuntimeFixer | ❌ | ❌ | ❌ | ❌ |
| 级联清理 | ✅ CascadeCleaner | ❌ | ❌ | ❌ | ❌ |

Python 有 5/5 维度覆盖，其他语言最多 1/5。**一种语言出 bug，所有语言都会出**，但只有 Python 有自动修复能力。

### 6.2 架构原则：LangAdapter 抽象

**核心改动**：将当前散落在 `import_fixer.py`、`error_dispatcher.py`、`runtime_validator.py` 中的语言特化逻辑抽象为统一的 `LangAdapter` 接口。

```python
class LangAdapter(ABC):
    """每种语言实现一个 adapter，提供 heal 系统所需的全部语言特化能力"""
    
    @abstractmethod
    def parse_import_error(self, error: ValidationError) -> ImportError | None:
        """解析错误消息，提取缺失的模块/符号名"""
        
    @abstractmethod  
    def fix_missing_import(self, file_path: Path, symbol: str, graph: CodeGraph) -> bool:
        """确定性修复缺失的 import/include"""
    
    @abstractmethod
    def resolve_undefined_name(self, name: str, file_path: Path, graph: CodeGraph) -> str | None:
        """将未定义名称解析为可用的 import 语句 / include 指令"""
    
    @abstractmethod
    def generate_stub(self, symbol_name: str, node: CodeNode) -> str:
        """根据原代码签名生成该语言的最小 stub"""
    
    @abstractmethod
    def extract_importable_symbols(self, file_path: Path) -> list[str]:
        """列出文件中可被外部引用的公开符号（用于级联清理）"""
    
    @abstractmethod
    def cascade_clean(self, file_path: Path, removed_symbols: set[str]) -> int:
        """级联清理：删除对已移除符号的引用（import/include/require）"""
```

### 6.3 各语言 Adapter 的具体实现

#### Python（已有能力迁移）

```python
class PythonAdapter(LangAdapter):
    """从现有 ImportFixer + UndefinedNameResolver + CascadeCleaner 迁移"""
    
    def parse_import_error(self, error):
        # 现有: "No module named 'X'" → ImportError(module="X")
        # 现有: "cannot import name 'X' from 'Y'" → ImportError(symbol="X", module="Y")
        
    def fix_missing_import(self, file_path, symbol, graph):
        # 迁移自 ImportFixer._fix_import()
        
    def resolve_undefined_name(self, name, file_path, graph):
        # 迁移自 UndefinedNameResolver._resolve_single()
        
    def generate_stub(self, symbol_name, node):
        # 保留原签名: def foo(x: int, y: str = "default") -> bool:
        #    raise NotImplementedError("Pruned by CodePrune")
        
    def cascade_clean(self, file_path, removed_symbols):
        # 迁移自 CascadeCleaner — 删除 from X import removed_name
```

#### C/C++（新增核心能力）

```python
class CAdapter(LangAdapter):
    """C/C++ 的 import = #include，symbol = 函数声明/宏/类型"""
    
    def parse_import_error(self, error):
        # "fatal error: X.h: No such file" → ImportError(header="X.h")
        # "implicit declaration of function 'X'" → ImportError(symbol="X")
        # "unknown type name 'X'" → ImportError(symbol="X", is_type=True)
        
    def fix_missing_import(self, file_path, symbol, graph):
        # 1. 在 CodeGraph 中查找 symbol 的定义文件
        # 2. 如果找到对应 .h → 添加 #include "found.h"
        # 3. 如果 .h 已 include 但声明被注释 → 恢复注释（调用 SourceRecovery）
        
    def resolve_undefined_name(self, name, file_path, graph):
        # 搜索策略：
        # 1. CodeGraph 精确匹配 → 返回 #include 路径
        # 2. 头文件全文搜索 → 匹配声明
        # 3. 原仓库 grep → 最后手段
        
    def generate_stub(self, symbol_name, node):
        # 从 node.source_code 提取签名:
        # int foo(int x, const char *y) { /* Pruned */ return 0; }
        # struct Bar { /* Pruned */ };
        # #define MACRO(x) /* Pruned */
        
    def cascade_clean(self, file_path, removed_symbols):
        # 删除对已移除函数的 #include（当该 .h 中没有其他被使用的符号时）
```

#### Java（新增核心能力）

```python
class JavaAdapter(LangAdapter):
    def parse_import_error(self, error):
        # "cannot find symbol: class X" → ImportError(symbol="X", is_type=True)
        # "cannot find symbol: method X" → ImportError(symbol="X")
        # "package X does not exist" → ImportError(package="X")
        
    def fix_missing_import(self, file_path, symbol, graph):
        # 1. CodeGraph 查找 symbol 的全限定名
        # 2. 添加 import com.example.X;
        # 3. 如果是同 package → 不需要 import，改为 SourceRecovery 补回类
        
    def generate_stub(self, symbol_name, node):
        # public class Foo { /* Pruned by CodePrune */ }
        # public interface Bar { /* Pruned by CodePrune */ }
        # 保留原始的 extends/implements 关系
```

#### TypeScript/JavaScript（新增核心能力）

```python
class TypeScriptAdapter(LangAdapter):
    def parse_import_error(self, error):
        # "Cannot find module 'X'" → ImportError(module="X")
        # "Module 'X' has no exported member 'Y'" → ImportError(symbol="Y", module="X")
        # TS2304: "Cannot find name 'X'" → ImportError(symbol="X")
        
    def fix_missing_import(self, file_path, symbol, graph):
        # 1. CodeGraph 查找 symbol 的源文件
        # 2. 添加 import { X } from './source';
        # 3. 区分 type-only import: import type { X } from './source';
        
    def generate_stub(self, symbol_name, node):
        # export function foo(x: number, y: string): boolean { 
        #   throw new Error("Pruned by CodePrune"); 
        # }
        # export interface Bar { /* Pruned */ }
        # export type Baz = any; /* Pruned */
```

### 6.4 ErrorDispatcher 的多语言扩展

当前 Pattern 覆盖严重不足。扩展每种语言的 pattern 集：

| 语言 | 当前 | 目标 | 新增 patterns |
|------|------|------|-------------|
| Python | 2 | 4 | `NameError: name 'X' is not defined`, `AttributeError: module 'X' has no attribute 'Y'` |
| C/C++ | 5 | 7 | `undefined reference to 'X'`(链接), `redefinition of 'X'`(重复定义) |
| Java | 1 | 4 | `package X does not exist`, `incompatible types`, `method X in class Y cannot be applied` |
| TypeScript | 1 | 4 | `TS2304 Cannot find name`, `TS2339 Property 'X' does not exist`, `TS2307 Cannot find module` |
| JavaScript | 0 | 2 | `ReferenceError: X is not defined`, `SyntaxError: Cannot use import` |

### 6.5 实施路径

**不需要一次全做**。按语言优先级渐进：

| 阶段 | 语言 | 内容 | 预期收益 |
|------|------|------|---------|
| **α** | C/C++ | CAdapter + SourceRecovery C 支持 | 修复 compiler/query-engine benchmark |
| **β** | Java  | JavaAdapter + 基础 import 修复 | 支持 mini-shop 等 Java 项目 |
| **γ** | TS/JS | TypeScriptAdapter | 支持 mini-dashboard 等前端项目 |
| **δ** | Python | 将现有代码迁移到 PythonAdapter | 统一架构，不引入新功能 |

Python 最后迁移是因为它已经能工作（只是架构不统一），迁移是重构不是新功能。

---

## 七、非目标（明确不做的事）

1. **不做 agent 式自主探索** — 验证步骤由 profile 或默认规则完全预定义，编译器/链接器输出确定性地指向错误位置，不需要 LLM 自主决定"下一步做什么"（详见第十章 10.7）
2. **不做多候选 patch + test 排名** — Agentless 的核心假设是"没有 ground truth"，而 CodePrune 有原仓库作为 ground truth
3. **不引入新的 LLM 调用层** — 不增加 reasoning model + editor model 双模型架构（当前单模型够用）
4. **不改变 8 层验证架构** — 当前 build → undefined_names → runtime → boot → completeness → fidelity → functional → test 的分层是合理的
5. **不做自动 rollback** — 当前的 pre_heal_snapshot 快照机制已足够
6. **不凭想象生成代码** — SourceRecovery 的核心原则是"从原仓库精确提取"，不是让 LLM 重新实现被删的功能

---

## 八、验证计划

改进实施后，按以下顺序验证：

1. **单元测试**：98/98 全部通过（不引入回归）
2. **mini-compiler**：7/7 compile test + link test pass + F1 ≥ 0.93
3. **mini-query-engine**：compile + link test pass + F1 ≥ 0.94
4. **mini-blog**：boot + functional smoke test pass + F1 ≥ 0.93
5. **mini-ticketing**：javac + SmokeTest pass + F1 ≥ 0.95
6. **全量 benchmark**：9/9 仓库的平均 F1 ≥ 0.94（历史最佳 0.941）
7. **SEARCH/REPLACE 格式特定测试**：构造 3 种典型场景（精确匹配、缩进差异、多 block 同文件）
8. **多语言验证**：每种已实现 Adapter 的语言至少跑一个 benchmark 仓库
9. **运行时验证**：C link test + Java SmokeTest + Python functional test 全部通过

---

## 九、与开源方案的关系总结

| 借鉴项 | 来源 | 在 CodePrune 中的适配 |
|--------|------|----------------------|
| SEARCH/REPLACE block 格式 | aider diff format | 替换 JSON patch 格式，保留 `_find_context_core` 行级匹配作为 fallback |
| 全错误一次性分析 | Agentless localize 理念 | 合并 Architect + Fix 为单步，但不做多候选排名 |
| 签名感知修复 | aider 的 repo-map | 利用已有 CodeGraph 的符号元数据生成精确 stub |
| ground truth 恢复优先 | CodePrune 独有 | 错误驱动的确定性恢复 — 先从原仓库精确提取，LLM 只做兜底 |
| 不做的事（tool-call 自主探索） | SWE-agent | 明确排除 — CodePrune 的错误源是确定性的 |
| 不做的事（multi-candidate ranking） | Agentless | 明确排除 — 有 ground truth 不需要排名 |
| 不做的事（凭空生成代码） | — | 明确排除 — 恢复 > 生成，原仓库就是答案 |

---

## 十、Phase3 运行时验证体系设计

### 10.1 问题陈述

**Phase3 当前根本没有做 agent 式的「编译→链接→运行→看报错→修→再运行」闭环。**

各验证层的实际能力边界：

| Layer | 实际做了什么 | 缺失了什么 |
|-------|------------|-----------|
| Build | `ast.parse` / `gcc -fsyntax-only` / `javac` / `tsc --noEmit` | ❌ C/C++ 不链接（undefined reference 检测不到）；Java 不运行 |
| UndefinedNames | pyflakes 静态扫描 | ❌ 纯静态，不实际执行任何 import |
| Runtime | 逐模块 `import X`（仅 Python） | ❌ 只验证 import 成功，不执行任何函数；仅 Python |
| Boot | LLM 生成脚本 → subprocess → BOOT_OK | ❌ **仅 Python**；脚本由 LLM 猜；不验证业务逻辑 |
| Functional | LLM 生成 smoke test → 两阶段执行 | ❌ **仅 Python**；默认关闭；LLM 脚本质量不稳定 |

**结果**：C 项目的头文件函数声明被 pruned 后，`-fsyntax-only` 只能发现隐式声明警告，但 linker 阶段的 `undefined reference to 'foo'` 完全发现不了。Java 项目 `javac` 通过后，运行时 `ClassNotFoundException` 完全不知道。

**核心矛盾**：heal 循环修到编译通过就停了，但「编译通过 ≠ 能运行」。

### 10.2 设计目标

在不破坏现有 8 层验证框架的前提下，增加真正的「链接 + 运行」验证能力：

1. **C/C++**：`-fsyntax-only` → 链接为 `.o` + 可选链接测试 → 运行测试程序
2. **Java**：`javac` → `java -cp . MainClass` 或 JUnit 命令
3. **Python**：保持现有 + 强化 functional validation
4. **TS/JS**：`tsc --noEmit` → `node entry.js` 或 `npx ts-node entry.ts`
5. **对 benchmark**：预定义运行配置，避免 LLM 猜测入口点和编译参数

### 10.3 Benchmark 运行配置 — `benchmark_profile.yaml`

每个 benchmark 目录下放一个 `benchmark_profile.yaml`，声明式地告诉 Phase3 **怎么编译、怎么链接、怎么运行、怎么验证**。

#### 10.3.1 完整 Schema

```yaml
# benchmark_profile.yaml — Benchmark 运行验证配置
# 此文件随 benchmark 仓库版本管理，Phase3 在 heal 循环中读取并执行

# ── 编译配置 ──
compile:
  language: c | cpp | java | python | typescript | javascript
  
  # C/C++ 专用
  compiler: gcc                           # 可选覆盖（默认从 codeprune.yaml 全局配置读取）
  flags: ["-Wall", "-Wextra"]             # 额外编译选项
  include_dirs: ["include", "include/core", "include/query"]  # -I 路径（相对子仓库根）
  sources: ["src/**/*.c"]                 # glob 模式
  exclude_sources: ["src/main.c"]         # 从 sources 中排除

  # Java 专用
  source_path: src/main/java              # javac -sourcepath
  classpath: []                           # 外部 jar（无外部依赖时留空）
  
  # TS 专用
  tsconfig: tsconfig.json                 # 可选自定义 tsconfig 路径

# ── 链接配置（C/C++ 专用）──
link:
  enabled: true                           # 是否做链接测试
  type: library | executable              # library: 只 .o 检查; executable: 链接为可执行文件
  
  # type=library 时: 编译所有 .o 不链接 main, 验证无 undefined reference
  # type=executable 时: 链接为可执行文件
  entry_object: null                      # 可选: 指定 main 所在的 .o (如 "src/main.o")
  link_flags: []                          # 额外链接选项 (如 "-lm", "-lpthread")
  
  # 链接测试 main（可选）: 一个极简 .c 文件, 调用关键函数验证符号可见
  test_main: tests/link_test.c            # 预写好的 main, 只做 #include + 函数调用
  expected_symbols:                       # 预期必须可链接的符号列表
    - lexer_init
    - parser_parse
    - optimizer_run

# ── 启动验证 ──
boot:
  enabled: true
  
  # 方式一: 预定义命令（优先级最高，不依赖 LLM）
  command: ["python", "-c", "from app import create_app; app = create_app(); print('BOOT_OK')"]
  
  # 方式二: 预写好的启动脚本
  script: tests/boot_test.py              # 脚本内打印 BOOT_OK / BOOT_FAIL
  
  # 方式三: 自动检测入口点（现有逻辑, 最低优先级）
  auto_detect: true
  
  timeout: 15                             # 超时秒数
  success_marker: "BOOT_OK"              # stdout 中的成功标记

# ── 功能验证 ──
functional:
  enabled: true
  
  # 预写好的功能测试脚本（不依赖 LLM 生成）
  script: tests/smoke_test.py             # 或 tests/SmokeTest.java
  
  # 预期输出
  success_marker: "FUNC_OK"
  timeout: 30
  
  # 环境变量（避免测试连真实数据库等）
  env:
    DATABASE_URL: "sqlite://:memory:"
    SECRET_KEY: "test-secret"
    DEBUG: "true"

# ── 安全约束 ──
safety:
  network: deny                           # deny | allow_localhost | allow_all
  filesystem: sub_repo_only               # sub_repo_only | tempdir | allow_all
  max_memory_mb: 512
  max_cpu_seconds: 60
```

#### 10.3.2 各 Benchmark 的具体配置

**mini-query-engine (C, library)**

```yaml
compile:
  language: c
  include_dirs: ["include", "include/core", "include/query"]
  sources: ["src/**/*.c"]
  exclude_sources: ["src/main.c"]

link:
  enabled: true
  type: library
  test_main: tests/link_test.c
  expected_symbols: [lexer_init, lexer_next, parser_init, parser_parse_select,
                     catalog_init, catalog_register_table, optimizer_init, optimizer_run,
                     registry_register_builtin]

boot:
  enabled: false      # 纯库项目, 无入口点

functional:
  enabled: true
  script: tests/functional_test.c    # 预写好的 C 测试 main
  timeout: 10
```

**mini-compiler (C, library)**

```yaml
compile:
  language: c
  include_dirs: ["include"]
  sources: ["src/**/*.c"]
  exclude_sources: ["src/main.c"]

link:
  enabled: true
  type: library
  expected_symbols: [lexer_init, lexer_next_token, parser_init, parser_parse,
                     symtab_init, symtab_define, symtab_lookup,
                     optimizer_init, optimizer_run]

functional:
  enabled: true
  script: tests/functional_test.c
  timeout: 10
```

**mini-blog (Python, application)**

```yaml
compile:
  language: python

boot:
  enabled: true
  command: ["python", "-c",
    "import sys; sys.path.insert(0,'.'); from config import get_config; from models import Comment, User; from comments.handlers import create_comment; from comments.moderation import check_spam; print('BOOT_OK')"]
  timeout: 10

functional:
  enabled: true
  script: tests/smoke_test.py
  timeout: 15
  env:
    DATABASE_URL: "sqlite://:memory:"
```

**mini-shop (Java, application)**

```yaml
compile:
  language: java
  source_path: src/main/java

boot:
  enabled: true
  script: tests/BootTest.java
  timeout: 15

functional:
  enabled: true
  script: tests/SmokeTest.java
  timeout: 20
```

**mini-ticketing (Java, application)**

```yaml
compile:
  language: java
  source_path: src/main/java

boot:
  enabled: true
  script: tests/BootTest.java
  timeout: 15

functional:
  enabled: true
  script: tests/SmokeTest.java
  timeout: 20

safety:
  network: deny
```

**mini-dashboard (TypeScript)**

```yaml
compile:
  language: typescript
  tsconfig: tsconfig.json

boot:
  enabled: false      # 前端项目, 无服务端入口

functional:
  enabled: true
  script: tests/smoke_test.ts
  timeout: 15
```

**mini-etl / mini-framework / mini-orchestrator (Python)**

```yaml
compile:
  language: python

boot:
  enabled: true
  auto_detect: true     # 使用现有入口点检测逻辑
  timeout: 10

functional:
  enabled: true
  script: tests/smoke_test.py
  timeout: 15
```

#### 10.3.3 非 Benchmark 模式（通用仓库）

普通用户不写 `benchmark_profile.yaml`。此时 Phase3 行为：

1. **Build**：与现有完全一致（`-fsyntax-only` / `ast.parse` / `javac` / `tsc`）
2. **Link**：**新增** C/C++ 自动链接测试 — 编译所有 `.o`，不链接 `main`，检查 undefined reference
3. **Boot**：保持现有 LLM 脚本生成逻辑（Python-only）
4. **Functional**：保持现有 LLM 脚本生成逻辑（Python-only，默认关闭）

即：没有 profile 文件时退化为当前行为，只有 C/C++ 增加自动链接检查。

### 10.4 执行引擎 — `RuntimeValidator` 改造

#### 10.4.1 新增 `ProfileExecutor` 类

```
core/heal/
├── profile_executor.py       # 新增: benchmark_profile.yaml 解析和执行
├── validator.py              # 改造: BuildValidator 增加 link 阶段
├── boot_validator.py         # 改造: 支持 profile 预定义命令
├── functional_validator.py   # 改造: 支持 profile 预定义脚本
└── fixer.py                  # 改造: heal 循环集成 ProfileExecutor
```

**ProfileExecutor 职责：**

```python
class ProfileExecutor:
    """解析并执行 benchmark_profile.yaml 中定义的验证步骤"""
    
    def __init__(self, sub_repo_path: Path, profile_path: Path | None = None):
        self.profile = self._load_profile(profile_path)  # None if not found
    
    def has_profile(self) -> bool: ...
    
    def get_compile_config(self) -> CompileConfig | None: ...
    def get_link_config(self) -> LinkConfig | None: ...
    def get_boot_config(self) -> BootConfig | None: ...
    def get_functional_config(self) -> FunctionalConfig | None: ...
    
    def execute_link_test(self) -> LinkResult: ...
    def execute_boot(self) -> BootResult: ...
    def execute_functional(self) -> FunctionalResult: ...
```

#### 10.4.2 链接验证（C/C++ 核心新增）

当前 `gcc -fsyntax-only` 只做语法检查。新增链接阶段：

```
阶段 1: gcc -fsyntax-only src/*.c          → 语法错误 (现有)
阶段 2: gcc -c src/*.c -o obj/*.o          → 编译为目标文件
阶段 3: gcc obj/*.o -o /dev/null           → 链接检查 (新增)
         或 gcc obj/*.o test_main.c -o test → 链接测试 main
```

**无 profile 时的自动链接**：
- 编译所有 `.c` → `.o`
- 尝试链接所有 `.o`（不含 `main.c` 如果有多个入口）
- 报告 `undefined reference to 'xxx'` 错误
- 这些错误回馈给 heal 循环的 SR batch 修复

**有 profile 时**：
- 使用 profile 指定的 include_dirs、sources、exclude_sources
- 如果 `link.test_main` 指定了测试 main，编译并链接它
- 检查 `expected_symbols` 是否全部可链接

#### 10.4.3 Heal 循环集成

```
                    ┌──────────────────────────────────┐
                    │         Heal 循环 (8 轮)          │
                    │                                    │
                    │   Layer 1: Build (syntax)          │
                    │     ↓                              │
                    │   Layer 1.2: Link (新增, C/C++)    │  ← 链接错误也进入修复循环
                    │     ↓                              │
                    │   Layer 1.5: UndefinedNames         │
                    │     ↓                              │
                    │   Layer 2: Runtime                  │
                    │     ↓                              │
                    │   Layer 2.5: Boot                  │  ← profile 优先, LLM 兜底
                    │     ↓                              │
                    │   Layer 3: Completeness             │
                    │     ↓                              │
                    │   Layer 3.5: Fidelity               │
                    │     ↓                              │
                    │   Layer 4: Functional               │  ← profile 脚本优先, LLM 兜底
                    │     ↓                              │
                    │   Layer 5: Test                     │
                    └──────────────────────────────────┘
```

**关键设计决策**：

1. **Link 错误进入 heal 修复循环** — `undefined reference to 'foo'` 由 ErrorDispatcher 或 SR batch 修复（SourceRecovery 从原仓库补回 foo 的实现）
2. **Boot/Functional 的 profile 脚本优先于 LLM 生成** — 有 profile 就不调 LLM
3. **Boot/Functional 失败反馈格式统一** — 无论脚本来源，失败输出都转为 `ValidationError` 回馈给修复循环

### 10.5 预写测试脚本规范

#### 10.5.1 C 链接测试 (link_test.c)

每个 C benchmark 附带一个 `tests/link_test.c`，只要能编译链接就算通过：

```c
/* tests/link_test.c — 链接可达性验证, 不执行任何逻辑 */
#include "query/lexer.h"
#include "query/parser.h"
#include "query/optimizer.h"

/* 取地址验证符号可链接, 永远不执行 */
static void _link_check(void) {
    (void)lexer_init;
    (void)lexer_next;
    (void)parser_init;
    (void)parser_parse_select;
    (void)optimizer_init;
    (void)optimizer_run;
}

int main(void) { return 0; }
```

#### 10.5.2 C 功能测试 (functional_test.c)

```c
/* tests/functional_test.c — 最小功能验证 */
#include <stdio.h>
#include "query/lexer.h"
#include "query/parser.h"

int main(void) {
    Lexer lex;
    lexer_init(&lex, "SELECT a FROM t");
    Token tok = lexer_next(&lex);
    if (tok.kind != TOK_SELECT) { printf("FUNC_FAIL: lexer\n"); return 1; }
    
    Parser p;
    parser_init(&p, "SELECT x FROM users");
    AstNode *ast = parser_parse_select(&p);
    if (!ast) { printf("FUNC_FAIL: parser\n"); return 1; }
    ast_free(ast);
    
    printf("FUNC_OK\n");
    return 0;
}
```

#### 10.5.3 Python smoke test

```python
# tests/smoke_test.py — 最小功能验证
import sys; sys.path.insert(0, ".")
try:
    from comments.handlers import create_comment, get_comments_for_post
    from comments.moderation import check_spam
    from notifications.handlers import create_notification
    from models import Comment, User, Notification
    
    u = User(id=1, username="test", email="t@t.com", password_hash="x")
    assert u.username == "test"
    
    c = Comment(id=1, post_id=1, author_id=1, content="hello")
    assert c.content == "hello"
    
    print("FUNC_OK")
except Exception as e:
    print(f"FUNC_FAIL: {e}")
    sys.exit(1)
```

#### 10.5.4 Java smoke test

```java
// tests/SmokeTest.java
public class SmokeTest {
    public static void main(String[] args) {
        try {
            // 直接实例化核心对象
            com.ticketing.support.IdGenerator idGen = new com.ticketing.support.IdGenerator();
            assert idGen.nextId("T").startsWith("T-");
            
            com.ticketing.events.EventBus bus = new com.ticketing.events.EventBus();
            assert bus != null;
            
            System.out.println("FUNC_OK");
        } catch (Exception e) {
            System.out.println("FUNC_FAIL: " + e.getMessage());
            System.exit(1);
        }
    }
}
```

### 10.6 安全隔离

运行用户代码的安全约束：

| 约束 | Python | C/Java/TS | 实现方式 |
|------|--------|-----------|---------|
| **超时** | `subprocess.run(timeout=T)` | 同左 | 已有 |
| **网络** | 不做进程级隔离 | 同左 | profile 中 `safety.network` 只是声明，由测试脚本自律 |
| **文件系统** | cwd 限定为 sub_repo | 同左 | `subprocess.run(cwd=sub_repo)` |
| **内存** | 不做 cgroup 限制 | 同左 | 依赖 OS 默认限制 |
| **stdin** | `stdin=DEVNULL` | 同左 | 防止阻塞 |

**安全非目标**：不做沙箱、不做容器化。benchmark 仓库和测试脚本由我们自己维护，可信。对外部用户提交的仓库，Phase3 只做到编译验证（现有层级），不执行未知代码。

### 10.7 与「非目标」的关系澄清

第七章写了「不做 agent 式自主探索」。运行时验证体系**不违反这条原则**：

| agent 式探索 | 运行时验证体系 |
|-------------|--------------|
| LLM 自主决定"接下来做什么" | 验证步骤由 profile 或默认规则**完全预定义** |
| tool-call 循环,轮次不确定 | 固定在 8 层验证框架内,轮次有界 |
| 探索代码库寻找修复点 | 编译器/链接器/运行时输出**确定性地**指向错误位置 |
| 凭想象生成测试 | 测试脚本**预写好**,不依赖 LLM 生成 |

核心区别：agent 模式是「LLM 驱动循环」,我们是「编译器驱动循环 + 确定性验证步骤」。

### 10.8 实施路径

| 阶段 | 内容 | 改动范围 | 优先级 |
|------|------|---------|--------|
| **α** | C/C++ 链接验证 | `validator.py` 增加 `-c` + `ld` 阶段 | P0 |
| **β** | `benchmark_profile.yaml` Schema + Parser | 新增 `profile_executor.py` | P0 |
| **γ** | 预写 9 个 benchmark 的 profile + 测试脚本 | `benchmark/*/benchmark_profile.yaml` + `tests/` | P0 |
| **δ** | Boot/Functional 集成 profile 脚本 | `boot_validator.py` + `functional_validator.py` | P1 |
| **ε** | Link 错误进入 heal 修复循环 | `fixer.py` 的验证层增加 link | P1 |
| **ζ** | Java runtime 验证（`java -cp . Test`）| `validator.py` | P2 |
| **η** | TS/JS runtime 验证（`node entry.js`）| `validator.py` | P2 |

### 10.9 预期收益

| 指标 | 当前 | 预期 |
|------|------|------|
| C/C++ undefined reference 检测 | ❌ 不可见 | ✅ 链接阶段发现并修复 |
| C/C++ 功能正确性验证 | ❌ 无 | ✅ 预写测试 + 链接测试覆盖关键符号 |
| Java 运行时验证 | ❌ javac 后就停 | ✅ SmokeTest.java 验证核心实例化 |
| Python functional validation | ⚠ LLM 脚本不稳定 | ✅ 预写脚本 100% 确定性 |
| benchmark 验证可重现性 | ❌ 每次 LLM 生成不同脚本 | ✅ profile 定义的验证完全确定性 |
| 非 benchmark 用户体验 | 不变 | 不变（C/C++ 自动 link 除外）|

