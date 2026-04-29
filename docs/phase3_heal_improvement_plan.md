# Phase 3 自愈管线改进方案

> 基于 mini-blog 端到端失败的全链路审计，2026-04-11

---

## 一、问题复盘：mini-blog 为什么跑不起来

### 实际失败链

```
comments/__init__.py  →  import comments.handlers
  → comments/handlers.py 第10行: from db import execute_insert
    → db/__init__.py 没有 re-export execute_insert
    → ImportError ✘
```

### 三个阻断点

| # | 问题 | 所在层 | 根因 |
|---|------|--------|------|
| A | `db/__init__.py` 缺失 `execute_insert` 的 re-export | Phase2 Surgeon + Phase3 Heal | 原仓库的 barrel 就不完整；Heal 只能删/注释，不能补齐 |
| B | `notifications/__init__.py` 全部 re-export 被注释为空壳 | Phase3 Pre-heal (ImportFixer) | ImportFixer 判定 `from notifications.handlers import ...` 中的符号"不在模块导出中"（因为 handlers.py import 链报错导致 AST 解析失败），一刀注释整块 |
| C | `app.py` 未进入保留集 | Phase2 锚点/闭包 | 用户指令要求保留 app.py，但自然语言指令（非 golden_answer 精确列表）导致锚点未捕获 |

### 为什么每一层都没挡住

| 层 | 行为 | 失手原因 |
|----|------|----------|
| **Pre-heal ImportFixer** | 多轮收敛扫描 → 在第1轮发现 `db` 模块的 exports 不含 `execute_insert` → 把 `comments/handlers.py` 的 `from db import execute_insert` **保留了**（因为 `_is_name_used_in_file` 发现 execute_insert 在代码中有引用） | ✅ 这步是正确的。但 ImportFixer 在 `notifications/__init__.py` 上就出错了——因为 handlers.py import 出错导致 handlers 无法被 AST 解析 → `_scan_sub_repo()` 收集到的 exports 为空 → 所有 re-export 名都判定为"不在模块导出中" → 注释掉 |
| **Layer 1 BuildValidator** | `_check_python_references()` 只收集模块**自身的** top-level 定义作为 exports（函数/类/赋值），**不追踪 re-export 链** | execute_insert 是通过 `from db.connection import ...` re-export 的，但 `_check_python_references()` 的 module_exports 收集逻辑**漏掉了 ImportFrom 节点**——它只在 `_scan_sub_repo` 中收集了 ImportFrom，而 validator.py 的 `_check_python_references` 是独立实现的，只收集 FunctionDef/ClassDef/Assign |
| **Layer 1.5 UndefinedNameResolver** | pyflakes 在**语法合法**的文件上检测 undefined name | comments/handlers.py `from db import execute_insert` 这行在 pyflakes 看来不是 undefined name（它是一个 ImportFrom），pyflakes 不做 runtime import 检查 |
| **Layer 2.0 RuntimeValidator** | **正确检测到** `ImportError: cannot import name 'execute_insert' from 'db'` | ✅ 检测成功 |
| **Layer 2.0 RuntimeFixer** | `_fix_import_error()` → 找到 `db/__init__.py` → 调用 `_remove_symbol_from_file` 尝试移除 execute_insert → **initpy 里根本没有 execute_insert** → 改不动 → fallback: `_comment_specific_import` → 在调用者文件里注释掉 import execute_insert 行 | ❌ **核心缺陷：只有删除能力，没有补齐能力** |
| **Layer 2.5 BootValidator** | LLM 生成启动脚本 → app.py 缺失，入口不明确 | 无法暴露深层 import 链错误 |

---

## 二、根因分类

### R1：re-export 补齐能力缺失 (P0)

**当前状态**: RuntimeFixer 对 `ImportError: cannot import name 'X' from 'Y'` 的修复策略是"从 Y 中移除 X 的导出"。但如果 X 根本不在 Y 的 `__init__.py` 中（原仓库 barrel 不完整，或 Surgeon 复制时丢失），修复器直接走 fallback 注释调用者的 import，**相当于放弃了功能**。

**正确行为**: 检查 X 是否在 Y 包的**某个子模块**中有定义 → 如果有，在 Y/__init__.py 中补一行 `from Y.submodule import X`。

**影响范围**: 所有 Python 项目的包级 import 都可能触发。mini-blog 的 `db.execute_insert`、所有 `notifications.*` 都是这种情况。

### R2：ImportFixer 循环依赖导致 barrel 全灭 (P0)

**当前状态**: ImportFixer._scan_sub_repo() 在第1轮收集模块导出时，如果某文件的 import 链已断裂（比如 handlers.py 依赖 db → db 的 barrel 不完整 → handlers.py AST 解析看不到 import 出错但扫描拿到的 export 列表不全），那它收集到的 exports 就不完整。后续用这个不完整的 exports 来判断 `notifications/__init__.py` 的 re-export 是否有效，就会误删。

**正确行为**: `_scan_sub_repo()` 应该在 `__init__.py` 的 re-export 处做**递归追踪**——`from notifications.handlers import create_notification` 的有效性不应仅看 handlers AST 顶层定义，还应看 handlers.py 内 def create_notification 是否实际存在。当前逻辑已经部分做了（ImportFrom alias 也加入 names），但问题是 handlers.py 本身的 import 可能链式失败。

### R3：BuildValidator 的 _check_python_references 与 ImportFixer 的 exports 收集逻辑不一致 (P1)

`validator.py::_check_python_references()` 自己又收集了一遍 module_exports，但**没有包含 ImportFrom re-export**。而 ImportFixer._scan_sub_repo() 则包含了。这造成：
- ImportFixer 认为 db 模块导出了 execute_query（因为 `__init__.py` 有 `from db.connection import execute_query`）
- BuildValidator 认为 db 模块**不**导出 execute_query（因为它只看 FunctionDef/ClassDef/Assign）

应统一 exports 收集逻辑，或复用 ImportFixer 的实现。

### R4：RuntimeFixer._comment_specific_import 粒度太粗 (P1)

当 `from db import execute_query, execute_insert, execute_write` 中只有 `execute_insert` 不可 import 时，fallback 逻辑在文件中找到该行后调用 `_remove_symbol_from_file(line, symbol, ...)` → 这里是在**单行**上操作，实际上如果 import 是多行 `from X import (\n a,\n b,\n c\n)` 格式，只处理一行可能不够。

### R5：Boot/Functional Validator 覆盖度不足 (P2)

BootValidator 的 `_generate_boot_script` 由 LLM 生成，入口点检测基于文件名模式和图谱零入度。当 app.py 缺失时，LLM 可能只生成 `import db` 这种浅层测试脚本，无法暴露深层依赖问题。

---

## 三、改进方案

### 方案 M1：RuntimeFixer 增加 re-export 补齐能力 (P0)

**修改文件**: `core/heal/runtime_validator.py :: RuntimeFixer._fix_import_error()`

**原逻辑**:
```python
def _fix_import_error(self, err):
    # 1. 在 __init__.py 中移除 symbol → 如果 __init__.py 里根本没有，失败
    # 2. fallback: 注释调用者的 import
```

**新逻辑**:
```python
def _fix_import_error(self, err):
    symbol = err.symbol
    source = err.source_module
    
    # 1. 先尝试在 __init__.py 中移除（原逻辑，处理"导出了不存在的符号"情况）
    if self._try_remove_from_init(symbol, source):
        return True
    
    # 2. 【新增】检查 symbol 是否在源包的某个子模块中定义
    if self._try_add_reexport(symbol, source):
        return True
    
    # 3. fallback: 注释调用者的 import
    return self._comment_specific_import(err)
```

**新方法 `_try_add_reexport()`**:
```python
def _try_add_reexport(self, symbol: str, package: str) -> bool:
    """在 package/__init__.py 中补齐缺失的 re-export"""
    pkg_dir = self.sub_repo_path / package.replace(".", "/")
    if not pkg_dir.is_dir():
        return False
    
    init_path = pkg_dir / "__init__.py"
    if not init_path.exists():
        return False
    
    # 扫描包下所有 .py 文件，查找 symbol 的定义
    for py_file in pkg_dir.glob("*.py"):
        if py_file.name == "__init__.py":
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == symbol:
                    # 找到定义，补齐 re-export
                    submodule = py_file.stem  # 如 "connection"
                    new_line = f"from {package}.{submodule} import {symbol}\n"
                    
                    content = init_path.read_text(encoding="utf-8")
                    if new_line not in content and f"import {symbol}" not in content:
                        content += new_line
                        init_path.write_text(content, encoding="utf-8")
                        logger.info(f"Runtime fix: 补齐 {init_path.name} re-export: {symbol} from .{submodule}")
                        return True
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == symbol:
                        submodule = py_file.stem
                        new_line = f"from {package}.{submodule} import {symbol}\n"
                        content = init_path.read_text(encoding="utf-8")
                        if new_line not in content:
                            content += new_line
                            init_path.write_text(content, encoding="utf-8")
                            logger.info(f"Runtime fix: 补齐 {init_path.name} re-export: {symbol} from .{submodule}")
                            return True
    
    # 也查原仓库（部分符号可能在子仓库中对应的文件存在但符号只在原仓库）  
    return False
```

**预期效果**: `execute_insert` 在 `db/connection.py` 有定义 → 自动在 `db/__init__.py` 补一行 `from db.connection import execute_insert`。

---

### 方案 M2：ImportFixer 增加"被引用但未导出"的 re-export 补齐 (P0)

**修改文件**: `core/heal/import_fixer.py :: ImportFixer.fix_all()`

在 fix_all() 的多轮收敛循环之后，增加一个"被引用但未导出"的审计步骤：

```python
def fix_all(self) -> tuple[int, dict[Path, set[str]]]:
    # ... 现有的多轮修复 ...
    
    # 【新增】审计阶段：检查所有 from X import Y 的 Y 是否在 X 的导出中
    #         如果 Y 在 X 的某个子模块中有定义但 X/__init__.py 未导出，自动补齐
    reexport_fixed = self._audit_and_fix_missing_reexports()
    total_fixed += reexport_fixed
    
    return total_fixed, all_removed
```

**新方法 `_audit_and_fix_missing_reexports()`**:

```python
def _audit_and_fix_missing_reexports(self) -> int:
    """审计并补齐缺失的 re-export"""
    fixed = 0
    
    # 收集所有 from X import Y 需求
    demands: dict[str, set[str]] = {}  # package → {symbol_names needed}
    for py_file in self.sub_repo.rglob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                module = node.module
                if module in self._available_modules and "." not in module:
                    # 这是包级导入 (from db import X)
                    exports = self._get_module_exports(module)
                    if exports is not None:
                        for alias in node.names:
                            if alias.name != "*" and alias.name not in exports:
                                demands.setdefault(module, set()).add(alias.name)
    
    if not demands:
        return 0
    
    # 对每个缺失的 re-export，在子仓库的包下搜索定义
    for package, missing_symbols in demands.items():
        pkg_dir = self.sub_repo / package.replace(".", "/")
        if not pkg_dir.is_dir():
            continue
        init_path = pkg_dir / "__init__.py"
        if not init_path.exists():
            continue
        
        additions = []
        for py_file in pkg_dir.glob("*.py"):
            if py_file.name == "__init__.py":
                continue
            submod_exports = self._module_exports.get(f"{package}.{py_file.stem}", set())
            for sym in list(missing_symbols):
                if sym in submod_exports:
                    additions.append((py_file.stem, sym))
                    missing_symbols.discard(sym)
        
        if additions:
            content = init_path.read_text(encoding="utf-8")
            for submod, sym in additions:
                line = f"from {package}.{submod} import {sym}\n"
                if line not in content and f"import {sym}" not in content:
                    content += line
                    fixed += 1
            init_path.write_text(content, encoding="utf-8")
            logger.info(f"ImportFixer: 补齐 {init_path.name} re-export: {[s for _, s in additions]}")
    
    return fixed
```

---

### 方案 M3：ImportFixer barrel 注释改为符号级精确保留 (P0)

**问题**: `notifications/__init__.py` 的整个 re-export 块被注释掉，因为 ImportFixer 对 `from notifications.handlers import (create_notification, notify_comment_reply, ...)` 判定为"模块 exports 不包含这些名称" → 全部标记 remove。

**修改文件**: `core/heal/import_fixer.py :: ImportFixer._handle_import_from()`

在 `__init__.py` 文件中处理 re-export 时，使用更保守的策略——如果目标模块存在于子仓库且不是 out_of_scope，则**保留** re-export 行，即使当前扫描到的 exports 不包含该名称：

```python
# 在 _handle_import_from 中，当 rel_path 是 __init__.py 时
if rel_path.name == "__init__.py" and module_exports is not None:
    # 这是 barrel re-export — 更保守策略
    # 只移除指向 out_of_scope 的符号，其他保留
    keep = []
    remove = []
    for alias in node.names:
        name = alias.name
        local_name = alias.asname or name
        if name == "*":
            keep.append(alias)
        elif self._is_excluded_module(module_path):
            remove.append(alias)
            removed_names.add(local_name)
        else:
            # __init__.py 的 re-export: 即使当前 scan 不到也保留
            # （可能是下游 import 链尚未修复导致 scan 不完整）
            keep.append(alias)
    # ...
```

---

### 方案 M4：BuildValidator._check_python_references 统一 exports 逻辑 (P1)

**修改文件**: `core/heal/validator.py :: BuildValidator._check_python_references()`

将 module_exports 的收集逻辑与 ImportFixer._scan_sub_repo() 对齐——加入 ImportFrom re-export 名称：

```python
for node in ast.iter_child_nodes(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        names.add(node.name)
    elif isinstance(node, ast.ClassDef):
        names.add(node.name)
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    # 【新增】包含 re-export 名称
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            names.add(alias.asname or alias.name)
    elif isinstance(node, ast.Import):
        for alias in node.names:
            names.add(alias.asname or alias.name.split(".")[-1])
```

同时将 severity 从 `"warning"` 提升为 `"error"`（当前标记为 warning 会被跳过）。

---

### 方案 M5：RuntimeValidator 增加全包导入覆盖 (P2)

**修改文件**: `core/heal/runtime_validator.py :: RuntimeValidator._discover_modules()`

当前按文件发现模块并逐个 import，但如果模块很多且相互依赖，逐个 import 可能掩盖只在特定导入顺序才出现的问题。

增加一种"全量导入"模式——在逐模块 import 之后，做一次"从入口点导入所有包"的测试：

```python
def _full_import_test(self, modules: list[str]) -> Optional[RuntimeError_]:
    """在单个 subprocess 中一次性导入所有模块"""
    imports = "; ".join(f"import {m}" for m in modules)
    script = f"import sys; sys.path.insert(0, '.'); {imports}; print('ALL_OK')"
    # ... 执行并解析错误 ...
```

---

### 方案 M6：Boot Validator 增加"全包导入"降级 (P2)

当 app.py 缺失或入口点不明确时，BootValidator 不应该只生成浅层启动脚本，应降级为对所有一级包做 import 测试（复用 RuntimeValidator 的逻辑）。

---

## 四、实施优先级与预期收益

| 方案 | 优先级 | 难度 | 改动量 | 预期收益 |
|------|--------|------|--------|----------|
| **M1** RuntimeFixer re-export 补齐 | P0 | 低 | ~60行 | 直接修复 mini-blog 的核心阻断点 |
| **M2** ImportFixer 导出审计 | P0 | 中 | ~50行 | 在 Pre-heal 就补齐，避免后续 Runtime 层的被动修复 |
| **M3** ImportFixer barrel 精确保留 | P0 | 中 | ~30行 | 防止 notifications/__init__.py 全灭 |
| **M4** BuildValidator exports 统一 | P1 | 低 | ~10行 | 减少 false negative，与 ImportFixer 逻辑一致 |
| **M5** RuntimeValidator 全量导入 | P2 | 低 | ~20行 | 覆盖导入顺序敏感的错误 |
| **M6** Boot 全包导入降级 | P2 | 低 | ~20行 | 入口缺失时仍能发现深层问题 |

### 推荐实施顺序

```
M3 → M2 → M1 → M4 → M5 → M6
```

理由：M3 是最保守的防守措施（不误删），M2 在 Pre-heal 阶段主动补齐，M1 作为 Runtime 层的最后兜底。三者联合形成**"不删 → 补齐 → 兜底"**的三层防线。

---

## 五、验证计划

1. 实施 M1+M2+M3 后，**不清缓存**重跑 mini-blog：
   ```
   python cli.py run ./benchmark/mini-blog "Keep the complete comment system feature chain, including comment CRUD, spam moderation, emoji reactions, and comment-triggered notifications." -o ./benchmark/output/blog -v
   ```

2. 对输出执行端到端功能验证脚本（上次用的那个）

3. 跑 9/9 benchmark 确认无 F1 回归

4. 重点关注这几个 checkpoint：
   - `db/__init__.py` 是否包含 `execute_insert`
   - `notifications/__init__.py` 是否保留了需要的 re-export
   - `comments/handlers.py` 的 `from db import execute_insert` 是否未被注释
   - `import comments` 在子仓库 subprocess 中是否通过
