# Phase 3 增加式修补设计文档

> 终端报错驱动 + 原仓库代码上下文 → 增加式精确修补

## 1. 问题陈述

### 1.1 现状

Phase 3 的 RuntimeFixer 遇到 `ImportError: cannot import name 'X' from 'Y'` 时，当前修复策略链是：

```
_fix_import_error():
  1. 找 Y/__init__.py → _remove_symbol_from_file(X) → "删除 Y 对 X 的 re-export"
  2. fallback → _comment_specific_import() → "注释调用方对 X 的 import"
```

**两个策略都是"删除式"的**，修完后 X 的功能彻底消失。

### 1.2 mini-blog 的真实报错序列

```
# Round 1: notifications/handlers.py 导入 db 时 execute_insert 找不到
ImportError: cannot import name 'execute_insert' from 'db'
  → 根因: db/__init__.py 没有 re-export execute_insert（原仓库 bug）
  → 当前行为: 注释掉 notifications/handlers.py 的 from db import execute_insert
  → 后果: execute_insert 在文件中成为 undefined name

# Round 2: comments/handlers.py 导入 notifications 时 notify_comment_reply 找不到  
ImportError: cannot import name 'notify_comment_reply' from 'notifications'
  → 根因: Phase 2 裁掉了 notify_comment_reply 的定义（闭包未包含）
  → 当前行为: 注释掉 comments/handlers.py 的 import
  → 后果: 通知功能彻底报废
```

### 1.3 目标

在"删除式"策略之前，增加"增加式"策略：

1. **补齐 barrel re-export**：`db/__init__.py` 缺少 `execute_insert` → 在子模块中找到定义 → 补 re-export
2. **补回被裁函数定义**：`notify_comment_reply` 被裁掉 → 从原仓库提取函数定义 → 插回子仓库文件
3. **补齐 `__init__.py` re-export**：补回函数后，确保 barrel 也导出它

## 2. 架构设计

### 2.1 修复策略优先级（修改后）

```
RuntimeFixer._fix_import_error(err):
  ┌─────────────────────────────────────────────────┐
  │ ★ 新策略 A: _try_add_reexport(symbol, source)  │ ← 补 barrel
  │ ★ 新策略 B: _try_supplement_symbol(symbol, src) │ ← 补代码
  ├─────────────────────────────────────────────────┤
  │   原策略 1: _remove_symbol_from_file()          │ ← 删 barrel
  │   原策略 2: _comment_specific_import()          │ ← 删调用方  
  └─────────────────────────────────────────────────┘
```

**核心原则：先增后删。能补则补，补不了才删。**

### 2.2 策略 A: `_try_add_reexport(symbol, source)` — 补齐 barrel re-export

#### 触发条件

`ImportError: cannot import name 'X' from 'Y'`，其中 Y 是一个包（有 `__init__.py`）

#### 算法

```
输入: symbol='execute_insert', source='db'

1. 定位 source 包的 __init__.py:
   init_path = sub_repo_path / source.replace('.', '/') / '__init__.py'
   如果不存在 → 退出

2. 在子仓库的 source 包子模块中搜索 symbol 的定义:
   for py_file in (sub_repo_path / 'db').rglob('*.py'):
       # 排除 __init__.py 本身
       ast_parse(py_file) → 提取所有顶层 def/class 名
       if symbol in top_level_names:
           defining_module = 'db.connection'  # 找到了
           break

3. 如果找到:
   在 __init__.py 末尾追加:
   "from {defining_module} import {symbol}\n"
   → return True

4. 如果子仓库中找不到，在 CodeGraph 中搜索:
   for node in graph.nodes.values():
       if node.name == symbol and node.file_path 在 source 包下:
           # 函数存在于原仓库但不在子仓库 → 交给策略 B
           break
   → return False (让策略 B 处理)
```

#### 处理示例

```python
# 错误: ImportError: cannot import name 'execute_insert' from 'db'
# Step 1: init_path = sub_repo/db/__init__.py ✅ 存在
# Step 2: 扫描 sub_repo/db/connection.py → 找到 def execute_insert(...)
# Step 3: 在 db/__init__.py 追加:
#         from db.connection import execute_insert
# → 修好了 ✅
```

#### 代码骨架

```python
def _try_add_reexport(self, symbol: str, source: str) -> bool:
    """尝试在包的 __init__.py 中补齐缺失的 re-export。
    
    前提: source 是一个包, symbol 定义在该包的某个子模块中。
    """
    source_dir = self.sub_repo_path / source.replace(".", "/")
    init_path = source_dir / "__init__.py"
    if not init_path.exists():
        return False

    # 在包的子模块中搜索 symbol 的定义
    defining_module = self._find_symbol_in_package(source_dir, source, symbol)
    if not defining_module:
        return False

    # 检查 __init__.py 是否已经导出了该 symbol（避免重复）
    try:
        init_content = init_path.read_text(encoding="utf-8")
    except OSError:
        return False

    if f"import {symbol}" in init_content and symbol in init_content:
        return False  # 已有导出但仍报错 → 不是 re-export 问题

    # 追加 re-export
    import_line = f"from {defining_module} import {symbol}\n"
    init_path.write_text(init_content.rstrip("\n") + "\n" + import_line, encoding="utf-8")
    
    rel = init_path.relative_to(self.sub_repo_path)
    logger.info(f"Runtime fix: 补齐 {rel} 中对 '{symbol}' 的 re-export (from {defining_module})")
    return True

def _find_symbol_in_package(self, pkg_dir: Path, pkg_name: str, symbol: str) -> Optional[str]:
    """在包目录中搜索 symbol 的定义, 返回定义所在的模块全名。"""
    for py_file in sorted(pkg_dir.rglob("*.py")):
        if py_file.name == "__init__.py":
            continue
        if "__pycache__" in str(py_file):
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == symbol:
                    # 计算模块全名: db/connection.py → db.connection
                    rel = py_file.relative_to(self.sub_repo_path)
                    mod_name = str(rel.with_suffix("")).replace("\\", "/").replace("/", ".")
                    return mod_name
    return None
```

### 2.3 策略 B: `_try_supplement_symbol(symbol, source)` — 从原仓库补回被裁函数

#### 触发条件

策略 A 在子仓库中找不到 symbol 的定义（被 Phase 2 裁掉了），但 CodeGraph 记录了它在原仓库中的位置。

#### 算法

```
输入: symbol='notify_comment_reply', source='notifications'

1. 在 CodeGraph 中定位 symbol:
   for node in graph.nodes.values():
       if node.name == symbol and node.file_path 以 source 开头:
           found: file_path='notifications/handlers.py', 
                  start_line=30, end_line=39 (ByteRange)
           break
   如果找不到 → return False

2. 目标文件检查:
   target = sub_repo_path / 'notifications/handlers.py'
   如果目标文件不在子仓库 → return False
   （不从原仓库整体复制文件——只补函数）

3. 从原仓库提取函数定义:
   original_file = source_repo_path / 'notifications/handlers.py'
   lines = original_file.readlines()
   func_code = lines[start_line-1 : end_line]  # 精确提取

4. 依赖安全检查:
   解析 func_code 中引用的名称
   对每个引用的名称:
     - 如果在子仓库文件中已有 import 或定义 → OK
     - 如果需要新 import → 检查该模块是否在子仓库中可用
     - 如果有不可满足的依赖 → return False (放弃补回, 避免雪崩)

5. 插入函数定义:
   在目标文件中找到合适的插入位置:
     - 优先: 同级 pruned 注释附近 (# ... pruned N lines ...)
     - 回退: 文件末尾
   写回文件

6. 修复 __init__.py re-export (如果需要):
   检查 source/__init__.py 是否导出了该 symbol
   如果没有 → 补齐

→ return True
```

#### 处理示例

```python
# 错误: ImportError: cannot import name 'notify_comment_reply' from 'notifications'
#
# Step 1: CodeGraph 查到 → notifications/handlers.py:30-39
# Step 2: sub_repo/notifications/handlers.py 存在 ✅
# Step 3: 从原仓库读取:
#   def notify_comment_reply(target_user_id: int, replier_id: int,
#                            post_id: int, comment_id: int) -> None:
#       """通知用户：有人回复了他的评论"""
#       replier_rows = execute_query(
#           "SELECT username FROM users WHERE id = ?", (replier_id,)
#       )
#       username = replier_rows[0]["username"] if replier_rows else "某用户"
#       message = f"@{username} 回复了你的评论"
#       create_notification(target_user_id, "comment_reply", message, source_id=comment_id)
#       logger.info(f"通知已发送: comment_reply → user={target_user_id}")
#
# Step 4: 依赖检查:
#   - execute_query → db.connection 中有定义, 且 handlers.py 已 import ✅
#   - create_notification → 同文件中有定义 ✅
#   - logger → 同文件已定义 ✅
#   → 所有依赖可满足 ✅
#
# Step 5: 插入到 notifications/handlers.py 适当位置
# Step 6: 修复 notifications/__init__.py 的 re-export
# → 修好了 ✅
```

#### 代码骨架

```python
def _try_supplement_symbol(self, symbol: str, source: str) -> bool:
    """尝试从原仓库补回被裁掉的函数/类定义。
    
    前提: symbol 在子仓库中不存在, 但在 CodeGraph 中有记录。
    """
    if not self.graph:
        return False

    # Step 1: 在 CodeGraph 中定位
    from core.graph.schema import NodeType
    target_types = {NodeType.FUNCTION, NodeType.CLASS}
    found_node = None
    for node in self.graph.nodes.values():
        if (node.name == symbol 
            and node.node_type in target_types
            and node.file_path
            and str(node.file_path).replace("\\", "/").startswith(source.replace(".", "/"))):
            found_node = node
            break

    if not found_node or not found_node.byte_range:
        return False

    # Step 2: 确认目标文件在子仓库中存在
    target_file = self.sub_repo_path / found_node.file_path
    if not target_file.exists():
        return False

    # Step 3: 从原仓库提取函数定义
    original_file = self.source_repo_path / found_node.file_path
    if not original_file.exists():
        return False

    try:
        orig_lines = original_file.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return False

    br = found_node.byte_range
    func_lines = orig_lines[br.start_line - 1 : br.end_line]
    func_code = "".join(func_lines)

    # 确认函数在子仓库中确实不存在
    try:
        target_content = target_file.read_text(encoding="utf-8")
    except OSError:
        return False

    if f"def {symbol}(" in target_content or f"class {symbol}" in target_content:
        return False  # 已存在, 不需要补

    # Step 4: 依赖安全检查
    if not self._check_supplement_deps(func_code, target_file):
        logger.info(f"补充 {symbol} 放弃: 存在不可满足的依赖")
        return False

    # Step 5: 插入到目标文件
    insert_pos = self._find_insert_position(target_content, symbol, br.start_line)
    new_content = self._insert_function(target_content, func_code, insert_pos)
    target_file.write_text(new_content, encoding="utf-8")
    
    # Step 6: 修复 __init__.py re-export
    self._ensure_reexport(source, symbol, found_node.file_path)

    rel = target_file.relative_to(self.sub_repo_path)
    logger.info(f"Runtime fix: 从原仓库补回 {rel}::{symbol} ({len(func_lines)} 行)")
    return True

def _check_supplement_deps(self, func_code: str, target_file: Path) -> bool:
    """检查补回的代码中引用的名称是否在子仓库中可满足。"""
    try:
        target_content = target_file.read_text(encoding="utf-8")
    except OSError:
        return False

    # 粗粒度检查: 提取 func_code 中的标识符
    # 精确检查: 只看 func_code 中的函数调用和变量引用
    #          与 target_file 的 import + 顶层定义做交叉
    try:
        tree = ast.parse(target_content)
    except SyntaxError:
        return True  # 解析失败时保守通过

    # 收集目标文件中可用的名称
    available = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                available.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                available.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            available.add(node.name)
        elif isinstance(node, ast.ClassDef):
            available.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    available.add(target.id)

    # Python builtins
    import builtins
    available.update(dir(builtins))

    # 提取 func_code 中引用的名称（Name 节点）
    try:
        func_tree = ast.parse(func_code)
    except SyntaxError:
        return True  # 解析失败时保守通过

    referenced = set()
    for node in ast.walk(func_tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            referenced.add(node.id)

    # 排除: 函数参数名、局部变量
    for node in ast.walk(func_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in node.args.args + node.args.kwonlyargs:
                referenced.discard(arg.arg)
            # 局部赋值
            for child in ast.walk(node):
                if isinstance(child, ast.Assign):
                    for t in child.targets:
                        if isinstance(t, ast.Name):
                            referenced.discard(t.id)

    # 检查未满足的引用
    unsatisfied = referenced - available
    if unsatisfied:
        logger.debug(f"补充函数依赖检查: unsatisfied={unsatisfied}")
        # 允许少量未满足（可能是方法调用的 self.xxx 等）
        if len(unsatisfied) > 3:
            return False

    return True

def _find_insert_position(self, content: str, symbol: str, original_line: int) -> int:
    """找到在目标文件中插入函数定义的最佳位置（字符偏移量）。
    
    优先级:
    1. # ... pruned N lines ... 注释附近
    2. 文件末尾
    """
    lines = content.splitlines(keepends=True)

    # 查找 pruned 注释
    for i, line in enumerate(lines):
        if "pruned" in line.lower() and "lines" in line.lower():
            # 在 pruned 注释后插入
            offset = sum(len(l) for l in lines[:i+1])
            return offset

    # 文件末尾
    return len(content)

def _insert_function(self, content: str, func_code: str, pos: int) -> str:
    """在指定位置插入函数代码，确保有适当的空行分隔。"""
    before = content[:pos].rstrip("\n")
    after = content[pos:].lstrip("\n")
    return before + "\n\n\n" + func_code.rstrip("\n") + "\n\n\n" + after

def _ensure_reexport(self, source: str, symbol: str, file_path: Path) -> None:
    """确保包的 __init__.py 导出了指定 symbol。"""
    source_dir = self.sub_repo_path / source.replace(".", "/")
    init_path = source_dir / "__init__.py"
    if not init_path.exists():
        return

    try:
        content = init_path.read_text(encoding="utf-8")
    except OSError:
        return

    # 检查是否已导出
    if symbol in content:
        # 可能被注释了 → 检查是否在 # [CodePrune] removed 行中
        for line in content.splitlines():
            if symbol in line and "# [CodePrune]" in line:
                # 取消注释: 重新激活
                new_line = re.sub(
                    r'^(\s*)(?:pass\s*)?#\s*\[CodePrune\].*?:\s*', r'\1', line
                )
                content = content.replace(line, new_line)
                init_path.write_text(content, encoding="utf-8")
                logger.info(f"Runtime fix: 取消注释 {source}/__init__.py 中对 '{symbol}' 的导出")
                return
        return  # 已存在且未被注释

    # 需要添加 → 计算模块全名
    mod_name = str(file_path.with_suffix("")).replace("\\", "/").replace("/", ".")
    import_line = f"from {mod_name} import {symbol}\n"
    
    new_content = content.rstrip("\n") + "\n" + import_line
    init_path.write_text(new_content, encoding="utf-8")
    logger.info(f"Runtime fix: 补齐 {source}/__init__.py 对 '{symbol}' 的 re-export")
```

### 2.4 RuntimeFixer 构造函数变更

```python
class RuntimeFixer:
    def __init__(self, sub_repo_path: Path, source_repo_path: Path,
                 excluded_modules: list[str],
                 graph: CodeGraph = None):           # ★ 新增参数
        self.sub_repo_path = sub_repo_path
        self.source_repo_path = source_repo_path
        self.excluded = excluded_modules
        self.graph = graph                           # ★ 用于策略 B 查 CodeGraph
```

对应 `fixer.py` 中创建 `RuntimeFixer` 的位置也需更新：

```python
# fixer.py _fix_runtime_errors() 中:
fixer = RuntimeFixer(
    sub_repo_path,
    source_repo_path=Path(self.config.repo_path),
    excluded_modules=excluded,
    graph=self.graph,                                # ★ 传入 graph
)
```

### 2.5 `_fix_import_error` 修改

```python
def _fix_import_error(self, err: RuntimeError_) -> bool:
    symbol = err.symbol
    source = err.source_module
    if not symbol or not source:
        return False

    # ★ 新策略 A: 补齐 barrel re-export
    if self._try_add_reexport(symbol, source):
        return True

    # ★ 新策略 B: 从原仓库补回被裁函数
    if self._try_supplement_symbol(symbol, source):
        return True

    # 原策略 1: 定位 __init__.py, 移除失效 re-export
    ...（现有代码不变）

    # 原策略 2: 注释调用方的 import
    return self._comment_specific_import(err)
```

## 3. 安全约束

### 3.1 补回范围限制

| 约束 | 说明 |
|------|------|
| **只补到已存在的文件** | 不能把整个新文件从原仓库复制进来（那是 `_fix_module_not_found` 的职责） |
| **只补函数/类级别** | 不补模块级代码、不补 import 语句（避免引入新依赖链） |
| **依赖安全检查** | 补回的函数如果引用了 ≥4 个子仓库中不存在的名称 → 放弃 |
| **单次上限** | 对同一个文件，单轮最多补回 3 个函数（避免大量补回导致文件膨胀） |
| **不影响 F1 计算** | 补回的函数在文件级 F1 中不改变（文件已在保留集），但会让功能更完整 |

### 3.2 死循环防护

补回函数可能引入新的 import 错误（补回的函数依赖其他缺失的函数），导致补→报错→再补的循环。防护措施：

- `_try_supplement_symbol` 的依赖检查拒绝深度依赖链
- RuntimeValidator 的死循环检测（hash 不变 → skip）仍然有效
- 补回操作记录到 `_supplemented_symbols: set[str]`，同一 symbol 不重复补

## 4. ImportFixer 保守策略（P1）

独立于 RuntimeFixer 的改进，在 `_pre_heal_cleanup` 阶段防止误伤：

### 当前问题

`notifications/__init__.py` 的多行 import 块：
```python
from notifications.handlers import (
    create_notification, notify_comment_reply, notify_post_author,
    notify_followers, get_notifications, mark_read, mark_all_read,
    get_unread_count, delete_old_notifications,
)
```

ImportFixer 扫描 `notifications/handlers.py` 后发现 7 个符号不存在，于是注释掉整个块——连有效的 `create_notification` 和 `notify_followers` 也一起注释了。

### 修复方案

`import_fixer.py` 的 `_fix_file()` 方法：对 `__init__.py` 中的 `from X import (a, b, c)` 块，改为**精确移除不存在的符号，保留存在的符号**：

```python
# 当前行为（错误）:
# 如果块中任何符号不存在 → 注释整个块

# 修改后:
# 精确移除不存在的符号，保留存在的
# from notifications.handlers import (
#     create_notification,        ← 保留 ✅
# #   notify_comment_reply,       ← 移除（不存在）
# #   notify_post_author,         ← 移除
#     notify_followers,           ← 保留 ✅
# #   get_notifications,          ← 移除
# #   ...
# )
```

这需要修改 `import_fixer.py` 中对多行 import 块的处理逻辑，从"整块操作"改为"逐符号操作"。

## 5. 策略执行流模拟（mini-blog 完整路径）

```
═══ Pre-heal Cleanup ═══

ImportFixer 扫描 notifications/__init__.py:
  原始: from notifications.handlers import (
          create_notification, notify_comment_reply, ...共 9 个)
  
  P1 保守策略: 
    create_notification → handlers.py 中有 → 保留 ✅
    notify_followers    → handlers.py 中有 → 保留 ✅
    其他 7 个           → handlers.py 中无 → 移除
  
  结果: from notifications.handlers import (
          create_notification, notify_followers,
        )
  
  → create_notification 和 notify_followers 的 re-export 保住了 ✅

CascadeCleaner 跳过（无级联影响）

UndefinedNameResolver 发现:
  - notify_comment_reply 在 comments/handlers.py 中被引用但 undefined
  → 分类为 llm_required（CodeGraph 有记录但子仓库文件中不存在）

═══ Heal 循环 Round 1 ═══

Layer 1 (Build): AST 解析通过 ✅
Layer 1.5 (Undefined Names): 
  notify_comment_reply, notify_post_author → llm_required
  → 交给 LLM 修复... 但这里 LLM 可能只能注释调用行

Layer 2.0 (Runtime): 
  import db → OK ✅
  import notifications → OK ✅ (create_notification + notify_followers 导出正常)
  import notifications.handlers → 
    from db import execute_query, execute_insert, execute_write
    → ImportError: cannot import name 'execute_insert' from 'db'

→ RuntimeFixer._fix_import_error():
  ★ 策略 A: _try_add_reexport('execute_insert', 'db')
    → 扫描 db/connection.py → 找到 def execute_insert() ✅
    → 在 db/__init__.py 追加: from db.connection import execute_insert
    → return True ✅

═══ Heal 循环 Round 2 ═══

Layer 2.0 (Runtime - 重新验证):
  import notifications.handlers → OK ✅ (execute_insert 现在可以从 db 导入了)
  import comments → comments/__init__.py → import comments.handlers →
    from notifications import notify_comment_reply
    → ImportError: cannot import name 'notify_comment_reply' from 'notifications'

→ RuntimeFixer._fix_import_error():
  ★ 策略 A: _try_add_reexport('notify_comment_reply', 'notifications')
    → 扫描 notifications/handlers.py → 没有 def notify_comment_reply ✘
    → return False
  
  ★ 策略 B: _try_supplement_symbol('notify_comment_reply', 'notifications')
    → CodeGraph: notifications/handlers.py:30-39 有定义 ✅
    → 目标文件在子仓库中存在 ✅
    → 从原仓库提取函数 (10行):
        def notify_comment_reply(...):
            replier_rows = execute_query(...)
            ...
            create_notification(...)
    → 依赖检查:
        execute_query → 子仓库 db.connection 有 ✅
        create_notification → 同文件有 ✅
        logger → 同文件有 ✅
    → 插入到 notifications/handlers.py
    → 修复 notifications/__init__.py re-export
    → return True ✅

═══ Heal 循环 Round 3 ═══

Layer 2.0 (Runtime): 
  类似地修复 notify_post_author
  → 全部 import 通过 ✅

Layer 2.5 (Boot): 通过 ✅
Layer 3.5 (Functional): 通过 ✅

═══ 自愈成功 ═══
```

## 6. 与现有机制的交互

### 6.1 与 `_fix_module_not_found` 的分工

| 场景 | 处理方 |
|------|--------|
| 整个模块文件缺失 | `_fix_module_not_found`（从原仓库复制整文件） |
| 文件存在但缺函数 | `_try_supplement_symbol`（从原仓库补回函数） |
| 文件存在但 barrel 缺导出 | `_try_add_reexport`（补 re-export） |
| out_of_scope 模块 | `_comment_imports_of`（注释 import） |

### 6.2 与 `_fix_undefined_names` 的关系

Layer 1.5 的 UndefinedNameResolver 也能检测到 `notify_comment_reply` 未定义，并尝试 LLM 修复。但：

1. UndefinedNameResolver 只能加 import 或让 LLM patch，不能补函数体
2. RuntimeFixer 的发现时机更早（import 链断裂直接报错）
3. 建议：保持两者独立，RuntimeFixer 先修 → undefined names 减少 → LLM 压力减轻

### 6.3 与 F1 评分的关系

- **文件级 F1 不变**：补回函数不改变文件保留集
- **功能完整度提升**：原本"文件存在但函数被裁"的半残状态得到修复
- **理论上可能微降 Precision**：补回了一些被裁掉的函数，但由于这些函数在指令中明确要求保留（如 `notify_comment_reply` 用于通知功能链），实际上是正确的

## 7. 完整改动清单

| 优先级 | 文件 | 改动 | 行数 |
|--------|------|------|------|
| **P0** | `core/heal/runtime_validator.py` | 增加 `_try_add_reexport()` | ~35 行 |
| **P0** | `core/heal/runtime_validator.py` | 增加 `_find_symbol_in_package()` | ~20 行 |
| **P0** | `core/heal/runtime_validator.py` | 增加 `_try_supplement_symbol()` | ~80 行 |
| **P0** | `core/heal/runtime_validator.py` | 增加 `_check_supplement_deps()` | ~50 行 |
| **P0** | `core/heal/runtime_validator.py` | 增加 `_find_insert_position()` `_insert_function()` `_ensure_reexport()` | ~40 行 |
| **P0** | `core/heal/runtime_validator.py` | 修改 `__init__` (接受 graph 参数) | ~3 行 |
| **P0** | `core/heal/runtime_validator.py` | 修改 `_fix_import_error` (插入新策略) | ~8 行 |
| **P0** | `core/heal/fixer.py` | 传递 `graph` 给 RuntimeFixer | ~2 行 |
| **P1** | `core/heal/import_fixer.py` | barrel 保守策略 (逐符号操作) | ~30 行 |
| **P2** | `core/heal/fixer.py` | UndefinedNameResolver 补回能力 | ~40 行 |

**总改动: ~310 行，集中在 1-2 个文件**

## 8. 验证计划

1. **单元测试**: 为 `_try_add_reexport` 和 `_try_supplement_symbol` 编写测试
2. **mini-blog E2E**: 运行完整 pipeline 并验证评论功能链可运行
3. **9 项 Benchmark 回归**: 确认 F1 无回归
4. **边界情况**:
   - 循环依赖: A 补回的函数依赖 B 补回的函数
   - 嵌套包: `a.b.c.__init__.py` 的 re-export 链
   - 被注释的 re-export: `# [CodePrune] removed: from X import Y`
   - 同名函数: 两个模块有同名函数时的消歧

## 9. 回滚方案

所有改动集中在 RuntimeFixer 的新方法中，与现有策略完全正交。如果出现问题：

- 删除 `_try_add_reexport` 和 `_try_supplement_symbol` 方法
- 恢复 `_fix_import_error` 到原始版本
- 系统回退到"删除式"修复，行为与当前完全一致
