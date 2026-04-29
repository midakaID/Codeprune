# Phase3 CodeHeal 升级方案

> 基于 aider 代码纠正框架调研 + Phase3 现状分析

---

## 现状诊断

### Phase3 已有能力
- ✅ 三层验证循环（编译 → 完整性 → 真实性）
- ✅ 三级模糊补丁匹配（exact → rstrip → strip → indent-aware）
- ✅ 修复历史反馈（_fix_history 防止重复尝试）
- ✅ 死循环检测（error hash + missing hash）
- ✅ out_of_scope import 批量注释
- ✅ warning severity 过滤

### Phase3 核心痛点

| # | 痛点 | 表现 | 根因 |
|---|------|------|------|
| P1 | **错误上下文贫乏** | LLM 只收到 error.message + 全文件内容（截断 8000 字符） | 缺少错误行周围代码的高亮展示 |
| P2 | **修复策略单一** | 只有"注释 import"和"LLM 生成补丁"两条路 | 缺少 stub/mock/签名降级等中间策略 |
| P3 | **完整性检查不可靠** | LLM 返回描述性文本而非文件路径 | prompt 约束不足 + 缺少结构化校验 |
| P4 | **跨层信息断裂** | Phase2 out_of_scope 到 Phase3 的信息传递靠属性文件 | 无预处理环节清理已知问题 |
| P5 | **验证器覆盖有限** | Java 只有 javac, TS 只有 tsc, Python 无 flake8 | 无法检测逻辑级错误（未定义变量等） |
| P6 | **无测试验证** | 只验证编译，不验证功能 | 裁剪后的代码可能编译通过但逻辑断裂 |

---

## 升级方案（按优先级排序）

### Tier 1: 立即可做（改动小，收益大）

#### U1: 丰富错误上下文（Error Context Enhancement）

**来源**: aider 的 `lint_edited()` + `tree_context()` 模式

**现状**: `_generate_fix` 发送整个文件内容（截断 8000 字符）+ 纯文本 error.message
```python
prompt = Prompts.FIX_SYNTAX_ERROR.format(
    error_message=error.message,        # "cannot find symbol"
    file_content=file_content[:8000],   # 整个文件（可能截断关键部分）
    original_context=original_context,
)
```

**升级**: 精确聚焦错误行 ± 10 行 + 标记错误行
```python
def _format_error_context(self, file_path, content, error_line, window=10):
    """aider-style 错误上下文格式化"""
    lines = content.splitlines()
    start = max(0, error_line - window - 1)
    end = min(len(lines), error_line + window)
    
    context_lines = []
    for i in range(start, end):
        marker = "█ " if i == error_line - 1 else "  "
        context_lines.append(f"{marker}{i+1:4d} | {lines[i]}")
    
    return "\n".join(context_lines)
```

**预期收益**: LLM 修复准确率提升，减少无效补丁

---

#### U2: Phase 2.5 — 预处理清理（Pre-heal Cleanup）

**来源**: aider 的"编辑后立即 lint"理念（先清理已知问题再进入修复循环）

**现状**: Phase2 生成子仓库后直接进入 Phase3 验证循环。已知的 out_of_scope import 在第一轮才被检测和注释。

**升级**: 在 heal 循环开始前，增加"Phase 2.5"预处理步骤：
```python
def _pre_heal_cleanup(self, sub_repo_path: Path):
    """在 heal 循环前清理已知问题（不消耗修复轮次）"""
    excluded = self._get_out_of_scope()
    if not excluded:
        return
    
    # 1. 扫描所有代码文件，批量注释指向 excluded 的 import
    for code_file in sub_repo_path.rglob("*"):
        if code_file.suffix in ('.py', '.java', '.ts', '.js', '.c', '.h'):
            self._clean_excluded_imports(code_file, excluded)
    
    # 2. Java/TS: 扫描并注释对 excluded 类的引用声明
    #    （减少 javac "cannot find symbol" 的干扰）
```

**预期收益**: 
- 修复轮次节省 1-2 轮（不再首轮浪费在已知 import 问题上）
- 消除 blog 首轮 11 行注释 + compiler 首轮编译修复的开销

---

#### U3: 完整性检查结构化约束

**来源**: aider 的结构化 LLM 输出 + 我们的实际问题

**现状**: LLM 返回 `missing_components: ["models.py 中仅确认到 User，缺少 Post dataclass"]`——描述性文本而非路径。

**升级**: 强化 prompt + 后处理校验
```python
# Prompt 强化
IMPORTANT OUTPUT RULES:
- missing_components MUST be file paths only (e.g. "src/utils.py")  
- Do NOT include descriptions or explanations in component names
- If a file EXISTS but you're unsure about its contents, report it as
  {"complete": true, "note": "..."} — do NOT list it as missing

# 后处理校验
def _validate_missing_components(self, missing, sub_repo_path):
    """过滤非路径格式的条目 + 已存在文件"""
    valid = []
    for comp in missing:
        # 必须像文件路径（含扩展名或目录分隔符）
        if not re.search(r'\.\w{1,4}$|/', comp):
            logger.debug(f"忽略非路径格式的完整性条目: {comp}")
            continue
        # 已存在的文件不算缺失
        if (sub_repo_path / comp).exists():
            logger.debug(f"忽略已存在文件: {comp}")
            continue
        valid.append(comp)
    return valid
```

**预期收益**: 消除 blog 的完整性死循环（LLM 返回描述性文本被过滤）

---

### Tier 2: 中期架构升级

#### U4: 反射消息重构（Reflection Pattern）

**来源**: aider 核心模式 — `reflected_message` 驱动的修复循环

**现状**: 三层验证用独立的 `if` 块串行执行，层间信息传递靠 `continue/break`

**升级**: 用统一的 `reflected_message` 驱动修复循环
```python
def heal(self, sub_repo_path: Path) -> bool:
    self._pre_heal_cleanup(sub_repo_path)  # U2
    
    reflected = None  # 初始无错误消息
    for round_num in range(1, max_rounds + 1):
        # 如果有反射消息，让 LLM 修复
        if reflected:
            patches = self._llm_fix(sub_repo_path, reflected)
            for patch in patches:
                self._apply_patch(sub_repo_path, patch)
        
        # 分层验证，第一个失败的层产生反射消息
        reflected = self._validate_all_layers(sub_repo_path)
        
        if reflected is None:
            logger.info("所有验证通过")
            return True
        
        # 死循环检测
        if self._is_loop(reflected):
            break
    
    return False

def _validate_all_layers(self, sub_repo_path):
    """按优先级验证，返回第一个失败层的错误消息（含上下文）"""
    # Layer 1: 编译
    build_errors = self._check_build(sub_repo_path)
    if build_errors:
        return self._format_build_errors(build_errors)  # aider-style 上下文
    
    # Layer 2: 完整性
    missing = self._check_completeness(sub_repo_path)
    if missing:
        return self._format_missing(missing)
    
    # Layer 3: 真实性
    hallucinations = self._check_fidelity(sub_repo_path)
    if hallucinations:
        return self._format_hallucinations(hallucinations)
    
    return None  # 全部通过
```

**预期收益**: 
- 统一的信息流，所有错误用同一格式发送给 LLM
- LLM 能看到完整的错误上下文而非分散的 handler
- 更容易扩展新的验证层

---

#### U5: 多策略修复链（Fix Strategy Chain）

**来源**: aider 的多 edit format + 我们对不同错误类型的需求

**现状**: 只有两种策略：注释 import（自动）或 LLM 补丁（单次生成）

**升级**: 策略链按优先级尝试
```
对于编译错误:
  Strategy 1: 自动修复（import 注释、类型声明注释）  ← 现有
  Strategy 2: LLM 精确补丁（original_code → fixed_code）  ← 现有
  Strategy 3: LLM Stub 生成（为缺失依赖生成 interface stub）  ← 新增
  Strategy 4: LLM 整文件重写（保留语义，修复所有错误）  ← 新增

对于完整性缺失:
  Strategy 1: 从原仓库复制  ← 现有
  Strategy 2: 从原仓库提取相关函数/类  ← 新增（符号级补充）
  Strategy 3: LLM 生成 minimal stub  ← 新增
```

具体来说新增 **Stub 策略**:
```python
def _generate_stub(self, sub_repo_path, missing_symbol, error):
    """为缺失的外部依赖生成最小 stub（接口/类型声明）"""
    prompt = f"""
    The following code references '{missing_symbol}' which is not available.
    Generate a MINIMAL stub that satisfies the type/interface contract.
    Only include method signatures and typing, no implementation.
    
    Error: {error.message}
    Referencing code:
    {self._format_error_context(...)}
    """
    # LLM 生成 stub → 写入 _stubs/ 目录
```

**预期收益**: 
- Java "cannot find symbol" 可通过 stub 解决（而非仅 warning 忽略）
- 更好地处理部分依赖缺失的场景

---

#### U6: 增强验证器（Multi-Layer Linting）

**来源**: aider 的 `py_lint` 三层检测（compile + tree-sitter + flake8）

**现状**: Python 只有 AST parse + import 检查；Java 只有 javac

**升级**:
```python
class BuildValidator:
    def _validate_python(self):
        errors = []
        for py_file in ...:
            # Layer 1: AST 语法（现有）
            tree = ast.parse(source)
            
            # Layer 2: import 存在性（现有）
            
            # Layer 3: 未定义名称检查（新增）
            # 使用 pyflakes 或 tree-sitter 检测引用了被裁剪函数的代码
            undefined = self._check_undefined_names(py_file, tree)
            for name, line in undefined:
                errors.append(ValidationError(
                    file_path=rel, line=line,
                    message=f"Undefined name '{name}' (possibly removed during pruning)",
                    severity="warning",
                ))
```

**预期收益**: 能在验证阶段发现更多裁剪导致的问题

---

### Tier 3: 长期愿景

#### U7: 双模型协调（Architect Pattern）

**来源**: aider 的 `ArchitectCoder` — 强模型规划 + 快模型实现

**映射到 Phase3**:
- **推理模型**（gpt-5.4）：分析所有验证错误，输出修复**计划**
- **快速模型**（gpt-5.4-mini）：根据计划生成具体补丁

```
Round flow:
  1. Validator → 收集所有错误
  2. gpt-5.4 → "分析这些错误的关联关系，输出修复优先级和策略"
  3. gpt-5.4-mini → 按计划逐个生成补丁
  4. Apply & re-validate
```

**预期收益**: 减少 gpt-5.4 调用次数，降低成本和延迟

#### U8: 测试验证集成

**来源**: aider 的 `auto_test` + `cmd_test` 机制

**升级**: 如果原仓库有测试，自动检测并在裁剪后运行相关测试

```python
def _check_tests(self, sub_repo_path):
    """检测并运行相关测试"""
    # 1. 在原仓库中找到测试文件
    # 2. 过滤出与保留文件相关的测试
    # 3. 复制到子仓库
    # 4. 运行并收集失败信息
```

---

## 实施优先级矩阵

```
              高收益
              │
      U2(预处理)│ U4(反射模式)
      U3(完整性)│ U5(策略链)
    ────────────┼────────────
      U1(上下文)│ U7(双模型)
      U6(验证器)│ U8(测试)
              │
              低收益
    低改动量 ──── 高改动量
```

**推荐实施顺序**:
1. **U2 + U3** → 预处理 + 完整性约束（改动小，直接解决现有 benchmark 的问题）
2. **U1** → 错误上下文（提升 LLM 补丁质量）
3. **U4** → 反射消息重构（架构层面的改进）
4. **U5** → 多策略修复链（扩展修复能力）
5. **U6 → U7 → U8** → 长期迭代

---

## 快速参考：Aider vs Phase3 关键对比

| 维度 | Aider | Phase3 现状 | 差距 |
|------|-------|------------|------|
| 反馈机制 | reflected_message (统一) | 分层 if/continue/break | 信息流碎片化 |
| 错误上下文 | 行号 + █标记 + 前后代码 | 纯 error.message + 全文件 | Phase3 远不够聚焦 |
| 修复策略 | whole/patch/udiff/editblock | 注释 import + LLM 补丁 | Phase3 策略太少 |
| 补丁应用 | 3级模糊匹配 | 4级模糊匹配(含 indent-aware) | **Phase3 已领先** |
| 循环控制 | max_reflections=3 | max_rounds=5 + hash检测 | **Phase3 更丰富** |
| 预处理 | 无 | 无 | 双方都缺 |
| 测试集成 | auto_test | 无 | Phase3 缺失 |
| 真实性校验 | 无 | _check_fidelity | **Phase3 独有** |
