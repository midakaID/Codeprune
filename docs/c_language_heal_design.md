# C/C++ 语言自愈增强设计文档

## 1. 问题描述

对 mini-query-engine（C 项目）执行 CodePrune 后, 输出仓库无法通过 `gcc -fsyntax-only` 编译。
Phase 3 Heal 循环 8 轮修复未收敛（24→15→17→2→14→9 个错误, 反弹振荡）。

### 1.1 错误分类

| 错误类型 | 数量 | 示例 | 根因层 |
|----------|------|------|--------|
| **缺失文件** | 2 | `fatal error: vector.h: No such file` | Phase 2 闭包 |
| **`#include` 被剪** | 3 | `ast.h` 中 `#include "common.h"` 被裁 | Phase 2 Surgeon |
| **`#define` 守卫被剪** | 2 | `ast.h` 中 `#define QE_AST_H` 被裁 | Phase 2 Surgeon |
| **函数声明被剪** | 3+ | `ast_make/ast_free/lexer_next` implicit decl | Phase 2 Surgeon |
| **NULL 未定义** | 4 | `lexer.c: 'NULL' undeclared` | 传递性: common.h 未被 include |

### 1.2 Phase 3 现有行为

Phase 3 **已有** error→LLM→fix 循环（Architect 模式）, 但对上述错误的修复策略有缺陷:

| 策略 | 行为 | 结果 |
|------|------|------|
| Architect + LLM patch | 尝试在 .c 文件内联实现缺失类型 | 产生 synthetic 代码, fidelity < 50% |
| 内联 PtrVector | 每轮重写 struct 定义 | 与前轮冲突, 导致重复定义 |
| 添加 `#include <stddef.h>` | 修复 NULL 问题 | 部分有效, 但治标不治本 |

**核心问题**: 没有"从原仓库补回整个文件"的策略, 只能在现有文件内做 patch。

## 2. 根因链

```
用户指令: "Remove executor, storage, main"
  │
  ▼
[Phase 2.0] instruction_analyzer._sanitize_dir_exclusions()
  │ → 只解析 Python import (ast.ImportFrom), 不解析 C #include
  │ → include/core 目录没被保护
  ▼
[Phase 2.1] closure BFS pre-exclusion
  │ → vector.h/vector.c 被标记为 scope.dir_excluded
  ▼
[Phase 2.2] closure _rule_arbitrate()
  │ → F19 dir_excluded 硬阻断 (优先级 > R0 C header 保护)
  │ → vector.h 被排除, R0 永远不会执行到
  ▼
[Phase 2.3] surgeon _filter_header_imports()
  │ → #include "vector.h" 目标不在闭包 → 被丢弃
  │ → #define QE_AST_H 被丢掉 (部分提取时被算入 pruned lines)
  ▼
[Phase 3] heal Architect loop
  → gcc 报错 → LLM 尝试内联重写 → synthetic 振荡 → 8 轮后放弃
```

## 3. 修复方案

### 3.1 P0: Surgeon `#define` 守卫保护

**位置**: `core/prune/surgeon.py` → `_filter_header_imports()`

**现状**: C/C++ 的 `#pragma`, `#define`, `#ifndef`, `#ifdef`, `#endif` 已有无条件保留逻辑 (surgeon.py line ~785)。
但 `_detect_header_end()` 返回的 `header_end` 可能不包含 `#define` 行 (如果 `#ifndef` 后紧跟 `#define`, header_end 停在 `#ifndef` 行)。

**修复**: 在 `_partial_extract()` 中, 对 C/C++ 文件额外保留所有预处理行:

```python
# 在 Step 4.5 _align_preprocessor_pairs 之前添加:
# 4.4 C/C++ 预处理行全量保护 — #include, #define, #ifndef, #ifdef, #endif, #pragma
if file_node.language in (Language.C, Language.CPP):
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith('#') and any(s.startswith(d) for d in (
            '#define', '#ifndef', '#ifdef', '#endif', '#pragma',
            '#if ', '#if(', '#else', '#elif',
        )):
            keep_lines.add(i)
```

注意: `#include` 行不在此列 — 它们仍由 `_filter_header_imports` 根据闭包决定保留/丢弃。

**影响范围**: 只影响 C/C++ 文件的部分提取; Python/Java/TS 不受影响。

### 3.2 P1: instruction_analyzer C `#include` 依赖解析

**位置**: `core/prune/instruction_analyzer.py` → `_sanitize_dir_exclusions()`

**现状**: Step 2 只解析 `.py` 文件的 `ast.ImportFrom`。

**修复**: 增加 C/C++ `#include` 解析:

```python
# Step 2b: 解析根实体 C/C++ 文件的 #include, 收集被引用的目录
import re
c_include_re = re.compile(r'^\s*#include\s+"([^"]+)"')

for rf in root_files:
    fpath = rp / rf.replace("/", "\\")
    if not fpath.exists():
        continue
    # Python
    if fpath.suffix == '.py':
        # ... existing Python import parsing ...
        pass
    # C/C++ headers and source
    elif fpath.suffix in ('.h', '.hpp', '.c', '.cpp', '.cc', '.hxx'):
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = c_include_re.match(line)
            if m:
                include_path = m.group(1)  # e.g. "vector.h", "core/common.h"
                parts = include_path.replace("\\", "/").split("/")
                if len(parts) > 1:
                    # 目录引用: "core/common.h" → "core/"
                    referenced_dirs.add(parts[0] + "/")
                # 同时搜索该头文件所在目录
                for candidate in rp.rglob(parts[-1]):
                    if candidate.is_file():
                        rel = candidate.relative_to(rp)
                        dir_prefix = str(rel.parent).replace("\\", "/")
                        if dir_prefix and dir_prefix != ".":
                            referenced_dirs.add(dir_prefix + "/")
                        break
```

**同时**: 修改 `root_files` 收集逻辑, 不再限制 `.py`:

```python
# 原: if file_part.endswith(".py"): root_files.add(file_part)
# 改: 收集所有代码文件
code_exts = {'.py', '.c', '.cpp', '.h', '.hpp', '.java', '.ts', '.js'}
if any(file_part.endswith(ext) for ext in code_exts):
    root_files.add(file_part)
```

### 3.3 P2: Phase 3 文件级补回策略

**位置**: `core/heal/fixer.py` → `_fix_syntax_errors()` or 新方法

**触发条件**: 
- 编译错误包含 `fatal error: xxx.h: No such file or directory`
- 或 `cannot open source file "xxx.h"`

**策略**:

```python
def _try_supplement_missing_file(
    self, sub_repo_path: Path, error: ValidationError,
) -> bool:
    """从原仓库补回缺失的头文件/源文件 (C/C++ 专用)"""
    # 1. 从错误消息提取缺失文件名
    m = re.search(r'fatal error:\s*(.+?):\s*No such file', error.message)
    if not m:
        m = re.search(r'cannot open source file\s*"(.+?)"', error.message)
    if not m:
        return False
    missing_file = m.group(1).strip()
    
    # 2. 在原仓库中查找该文件
    original_repo = self.config.repo_path
    candidates = list(original_repo.rglob(missing_file))
    if not candidates:
        return False
    
    # 3. 选择最佳匹配 (优先同目录结构)
    source = candidates[0]
    rel = source.relative_to(original_repo)
    
    # 4. 复制到子仓库
    dest = sub_repo_path / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    
    # 5. 如果是 .h 文件, 同时查找对应的 .c/.cpp
    if source.suffix in ('.h', '.hpp', '.hxx'):
        stem = source.stem
        for src_ext in ('.c', '.cpp', '.cc'):
            partner = source.parent / (stem + src_ext)
            if not partner.exists():
                # 也在 src/ 目录下找
                for p in original_repo.rglob(stem + src_ext):
                    partner = p
                    break
            if partner.exists():
                dest_partner = sub_repo_path / partner.relative_to(original_repo)
                dest_partner.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(partner, dest_partner)
                break
    
    self._supplemented_files.add(str(rel))
    logger.info(f"Strategy C: 从原仓库补回 {rel}")
    return True
```

**在 `_fix_syntax_errors` 中的位置**: Strategy 0 (最先执行, 在 Architect 之前):

```python
def _fix_syntax_errors(self, sub_repo_path, errors):
    # Strategy 0: 文件级补回 (C/C++ fatal error)
    fatal_errors = [e for e in errors if 'fatal error' in e.message.lower()
                    or 'No such file' in e.message]
    for fe in fatal_errors:
        if self._try_supplement_missing_file(sub_repo_path, fe):
            any_fixed = True
    
    # ... existing Strategy 1 (auto-import) → Strategy 2 (LLM) → Strategy 3 (stub)
```

**Fidelity 豁免**: 复用已有的 `_supplemented_files` 追踪, `_check_fidelity` 跳过。

### 3.4 P3: Surgeon `#include` 闭包外保留增强

**位置**: `core/prune/surgeon.py` → `_filter_header_imports()`

**现状**: C `#include "xxx.h"` 如果 xxx.h 不在闭包 → 行被丢弃。

**优化**: 对于 **同项目的** C/C++ `#include`, 即使目标不在闭包, 也注释而非删除:

```python
# 在 _filter_header_imports 的最后判断中:
if file_node.language in (Language.C, Language.CPP):
    # C include 目标不在闭包 → 注释而非删除 (留给 Phase 3 补回)
    for li in import_lines:
        lines[li] = f"// [CodePrune] {lines[li].rstrip()}\n"
        keep_lines.add(li)
```

这样 Phase 3 gcc 编译时会跳过注释行, 但一旦补回了缺失文件, 可以恢复注释。

## 4. 优先级与预期效果

| 修复 | 优先级 | 预期 |
|------|--------|------|
| P0 `#define` 守卫保护 | 高 | 消除 include guard 断裂 (2 个错误) |
| P1 C `#include` 依赖解析 | 高 | 阻止 vector.h/vector.c 被排除 (根因修复) |
| P2 文件级补回策略 | 高 | 兜底修复: fatal error → copy from original |
| P3 `#include` 注释保留 | 中 | 改善 surgeon 输出质量, 减轻 Phase 3 压力 |

**全部实施后预期**: mini-query-engine 输出仓库应能通过 `gcc -fsyntax-only` 编译。

### 4.1 当前状态 (2026-04-11)

**实测发现**: Phase 3 Architect LLM 循环经过 8 轮 (15 次 LLM 调用) 后**实际收敛成功**:
- `gcc -fsyntax-only` 全部 8 个源文件: **exit code 0, 零错误**
- `gcc -c` 编译出 8 个 .o 文件: 全部成功

**但有代价**:
- 8 轮 heal (接近 max_heal_rounds=8 极限), 耗时 91.6s
- 生成 synthetic 代码 (fidelity 33%-50%), 不如原仓库代码可靠
- 错误数振荡 (24→15→17→2→14→9→?→0), 过程不稳定

**P0-P3 修复的价值**: 不是"从不通过→通过", 而是**提高修复效率和代码质量**:
- P1 实施后: Phase 2 直接保留 vector.h/vector.c → Phase 3 无需修复
- P0/P3: Surgeon 输出更干净 → Phase 3 只需处理少量残留错误
- 预期效果: 8 轮→1-2 轮, synthetic 0%, 耗时从 91.6s 降到 ~10s

## 5. 与现有 Python 策略的对照

| 维度 | Python (已实现) | C/C++ (本设计) |
|------|-----------------|----------------|
| Phase 2 目录保护 | Fix A: 解析 `ast.ImportFrom` | P1: 解析 `#include "xxx"` |
| Phase 3 符号补回 | Strategy B: 从原仓库补回函数 | P2: 从原仓库补回整个文件 |
| Phase 3 import 恢复 | Fix B: 恢复被注释的 import | P3: 恢复被注释的 #include |
| Surgeon 保护 | Import 行按闭包过滤 | P0: `#define` 守卫 + P3: `#include` 注释 |

## 6. 测试计划

### 6.1 单元测试

```python
# tests/test_c_heal.py
def test_sanitize_dir_exclusions_c_include():
    """C #include 触发目录保护"""
    # 模拟 root_entities 包含 .h 文件, 其中 #include "vector.h"
    # 验证 vector 所在目录不被排除

def test_supplement_missing_file():
    """Phase 3 文件级补回"""
    # 模拟 gcc 报 fatal error: vector.h: No such file
    # 验证从原仓库复制回 vector.h + vector.c

def test_define_guard_preserved():
    """#define 守卫不被裁切"""
    # C 头文件部分提取后, #ifndef/#define 对仍完整
```

### 6.2 E2E 集成测试

```bash
python cli.py run benchmark/mini-query-engine \
  "Keep frontend parsing and optimization. Remove executor, storage, main." \
  -o benchmark/output/query-engine -v

# 验证:
cd benchmark/output/query-engine
gcc -std=c11 -Wall -Wextra -Iinclude/core -Iinclude/query \
    -fsyntax-only src/core/*.c src/query/*.c
# 预期: 0 errors
```
