# Phase3 可运行性增强设计

> 目标：将子仓库从"文件选择正确 (F1=0.944)" 提升到"可独立编译/启动/运行目标功能 (可运行性 ≥80%)"
>
> 基于 9 个 benchmark 的独立运行性分析 (当前平均 ~17%)，设计 Phase3 HealEngine 的增强方案。

---

## 一、核心设计理念

**修改前后必须配合 LLM 自动尝试编译/启动验证** — 这是整个设计的中轴。Phase3 不再仅仅"修编译错误"，而是以**"子仓库能独立运行目标功能"**为终止条件，形成闭环：

```
检测问题 → LLM 规划修复 → 应用修复 → 编译/启动验证 → 检测新问题 → ...
```

---

## 二、问题 1：上下文的更新 — 什么保留、什么清理

### 2.1 当前状态

Phase 2.5 `_pre_heal_cleanup` 做了：
- Python: ImportFixer (AST精确删除不存在的 import 名称) + CascadeCleaner (注释引用行)
- 非 Python: 正则注释 `// [CodePrune] removed: ...`

**不足**：只处理了 import 语句，未处理：
- 入口点中的**函数调用/实例化** (如 `app.py` 中的 `load_plugins("cache")`)
- 注册表/配置中的**字面值引用** (如 `Config.plugins = ["auth", "cache"]`)
- `__init__.py` / `__all__` 中的**再导出** (如 `from .cache import Cache`)
- 非 import 形式的**模块引用** (如 `_BUILTIN_PLUGINS["cache"] = "..."`)

### 2.2 设计：三层上下文清理

```
Layer A: Import 清理 (现有 Phase 2.5，保持不变)
  ↓
Layer B: 引用审计 (新增)
  - 扫描所有存活文件，检测对已删除模块/符号的非 import 引用
  - 分类：函数调用、实例化、字典映射、列表元素、配置默认值
  - LLM 辅助决策：该引用是"删除"还是"替换为存活的替代品"
  ↓  
Layer C: 注册表同步 (新增)
  - 自动检测并清理：__init__.py 的 __all__ / 再导出
  - 自动检测并清理：插件注册表、路由注册、工厂映射
  - 自动检测并清理：配置 dataclass 默认值
```

### 2.3 Layer B: 引用审计 — 详细设计

**输入**：
- `excluded_files`: Phase2 中被删除的文件列表
- `excluded_symbols`: 从 excluded_files 中提取的公开符号集合（类名、函数名、常量名）
- `surviving_files`: 子仓库中所有存活文件

**过程**：
```python
def _audit_references(self, sub_repo_path: Path) -> list[ReferenceIssue]:
    """扫描存活文件中对已删除符号的引用"""
    excluded_symbols = self._collect_excluded_symbols()  # 从 CodeGraph 收集
    issues = []
    
    for file in surviving_files:
        content = file.read_text()
        for symbol in excluded_symbols:
            # 查找非 import 的引用（函数调用、实例化、字典键、列表元素等）
            occurrences = self._find_symbol_references(content, symbol)
            for occ in occurrences:
                issues.append(ReferenceIssue(
                    file=file,
                    line=occ.line,
                    symbol=symbol,
                    context_type=occ.type,  # "call" | "instantiation" | "registry" | "config" | "other"
                    surrounding_code=occ.context_lines,
                ))
    return issues
```

**LLM 决策**：对每组 issues，调用 LLM 判断修复策略：

```
Given:
- File: framework/core/config.py
- Line 30: `plugins: list[str] = field(default_factory=lambda: ["auth", "cache"])`
- "cache" references deleted plugin framework/plugins/cache.py
- Available surviving plugins: ["auth"]

Action: REMOVE the "cache" entry → `plugins: list[str] = field(default_factory=lambda: ["auth"])`
```

策略类型：
| 策略 | 描述 | 适用场景 |
|------|------|----------|
| `REMOVE` | 直接删除引用行/元素 | 列表元素、字典条目、注册表条目 |
| `REPLACE` | 替换为存活的替代品 | 有等价替代的情况 |
| `COMMENT` | 注释掉引用代码块 | 复杂的条件分支/函数体 |
| `STUB` | 生成最小桩代码 | 被依赖的接口/类型 |
| `REWRITE` | LLM 重写代码段 | 需要重构逻辑的情况 |

### 2.4 Layer C: 注册表同步 — 详细设计

**自动化规则**（不需要 LLM，纯静态分析）：

```python
# Python __init__.py
# 检测 __all__ 中引用不存在的名称 → 删除
# 检测 from .xxx import yyy 中 xxx 不存在 → 删除整行

# Java ServiceLoader / Registry pattern
# 检测 Map/Dict 中 value 引用不存在的类 → 删除条目

# TypeScript index.ts re-exports
# 检测 export { X } from './xxx' 中 ./xxx 不存在 → 删除
```

---

## 三、问题 2：自动编译/启动验证

### 3.1 当前状态

`BuildValidator` 已支持：
- Python: AST 语法检查 + Pyflakes undefined names
- Java: `javac` 编译
- TypeScript: `npx tsc --noEmit`
- C/C++: `gcc`/`clang` 编译

**不足**：只检查"编译通过"，不检查"能启动" 或 "目标功能可用"。

### 3.2 设计：三级验证金字塔

```
Level 1: 编译验证 (现有，保持)
  "代码没有语法/类型错误"
  
Level 2: 启动验证 (新增)
  "入口点可以成功 import 并初始化，不会在启动时崩溃"

Level 3: 功能验证 (新增)
  "目标功能的关键路径可以无异常执行"
```

### 3.3 Level 2: 启动验证 — 详细设计

**核心思路**：生成一个**最小启动脚本**，尝试 import 入口模块并实例化核心对象。

#### 3.3.1 启动脚本生成

**输入给 LLM**：
```
Sub-repo files: [list of files]
User instruction: "保留HTTP路由和认证功能"
Entry point candidates: [app.py, main.py, __init__.py, ...]
```

**LLM 输出**：一个启动验证脚本
```python
# _codeprune_boot_test.py (自动生成，执行后删除)
import sys; sys.exit(0) if False else None

try:
    # Step 1: Import entry module
    from framework.core import App
    from framework.core.config import Config
    
    # Step 2: Instantiate core objects (no side effects)
    config = Config(plugins=["auth"])
    app = App(config)
    
    # Step 3: Verify key attributes exist
    assert hasattr(app, 'router'), "Missing router attribute"
    assert hasattr(app, 'middleware'), "Missing middleware attribute"
    
    print("BOOT_OK")
except Exception as e:
    print(f"BOOT_FAIL: {e}")
    sys.exit(1)
```

#### 3.3.2 各语言的启动验证策略

| 语言 | 启动验证方式 | 超时 |
|------|-------------|------|
| **Python** | `python _codeprune_boot_test.py` | 10s |
| **Java** | `javac + java -cp . BootTest` (生成 BootTest.java) | 30s |
| **TypeScript** | `npx ts-node _codeprune_boot_test.ts` 或 `tsc && node dist/boot_test.js` | 15s |
| **C/C++** | `make + ./a.out` (已有 main 则直接运行，或生成 boot_test.c) | 10s |

#### 3.3.3 编译指令的获取

**问题**：不同项目的编译/运行方式千差万别，如何自动推断？

**设计**：三级推断策略

```
Level 1: 项目特征文件检测 (自动)
  - Makefile → make
  - pom.xml → mvn compile
  - package.json → npm run build / npx tsc
  - setup.py / pyproject.toml → pip install -e .
  - CMakeLists.txt → cmake + make

Level 2: 原仓库线索 (自动)
  - README 中的 "Build" / "Getting Started" 段落
  - CI 配置文件 (.github/workflows, Jenkinsfile)
  - Makefile 中的 target 列表

Level 3: LLM 推断 (回退)
  - 给 LLM 子仓库文件列表 + 语言 → 生成构建命令
```

**实现**：

```python
class BuildCommandResolver:
    """推断子仓库的编译/运行命令"""
    
    def resolve(self, sub_repo_path: Path, language: Language) -> BuildCommands:
        # Level 1: 特征文件
        if (sub_repo_path / "Makefile").exists():
            return BuildCommands(compile="make", run="./a.out")
        if (sub_repo_path / "pom.xml").exists():
            return BuildCommands(compile="mvn compile", run="mvn exec:java")
        if (sub_repo_path / "package.json").exists():
            pkg = json.loads((sub_repo_path / "package.json").read_text())
            if "build" in pkg.get("scripts", {}):
                return BuildCommands(compile="npm run build", run="npm start")
        
        # Level 2: README / CI
        commands = self._extract_from_readme(sub_repo_path)
        if commands:
            return commands
            
        # Level 3: LLM fallback
        return self._llm_infer(sub_repo_path, language)
```

### 3.4 Level 3: 功能验证 — 详细设计

**核心思路**：基于 user_instruction，让 LLM 生成一个**功能烟雾测试脚本**，执行目标功能的核心路径。

#### 3.4.1 功能测试脚本生成

**输入给 LLM**：
```
User instruction: "保留工单提交→评论→审批→通知完整流程"
Sub-repo structure: [file list with brief descriptions]
Available classes/functions: [extracted from code]
```

**LLM 输出示例** (Java ticketing)：
```java
// _CodePruneFunctionalTest.java
public class _CodePruneFunctionalTest {
    public static void main(String[] args) {
        try {
            // 1. 创建核心服务
            var ticketRepo = new TicketRepository();
            var notifRepo = new NotificationRepository();
            var eventBus = new EventBus();
            var ticketService = new TicketService(ticketRepo, eventBus);
            var approvalService = new ApprovalService(ticketRepo, eventBus);
            var notifService = new NotificationService(notifRepo, eventBus);
            
            // 2. 提交工单
            var ticket = ticketService.submit("Bug Report", "Login fails");
            assert ticket != null : "Ticket creation failed";
            
            // 3. 添加评论
            ticketService.addComment(ticket.getId(), "Investigating...");
            
            // 4. 审批
            approvalService.approve(ticket.getId(), "admin");
            
            // 5. 验证通知
            var notifications = notifRepo.findAll();
            
            System.out.println("FUNCTIONAL_OK");
        } catch (Exception e) {
            System.out.println("FUNCTIONAL_FAIL: " + e.getMessage());
            e.printStackTrace();
            System.exit(1);
        }
    }
}
```

#### 3.4.2 自主验证目标功能的流程

```
1. LLM 分析 user_instruction → 提取核心功能路径
2. LLM 生成功能测试脚本
3. 编译并执行测试脚本
4. 如果失败：
   a. 捕获错误信息
   b. LLM 分析错误原因
   c. 分类：
      - 子仓库代码缺陷 → 进入修复循环
      - 测试脚本本身有问题 → 重新生成测试脚本
      - 需要外部依赖/数据 → 跳到桩/Mock 策略
5. 重复直到通过或达到轮次上限
```

### 3.5 环境隔离

**问题**：Phase3 验证可能影响宿主机环境。

**设计**：

```
Python: 使用 subprocess + 虚拟 virtualenv (如果可用), 否则直接 subprocess
Java: subprocess + 临时编译目录
C/C++: subprocess + 临时目录 (已有 Makefile 支持)
TypeScript: subprocess + node_modules (如果存在)
```

**安全约束**：
- 所有 subprocess 设置 `timeout`（默认 30s）
- 不执行任何用户代码中的 `exec`/`eval`
- 生成的测试脚本不涉及网络/文件系统副作用（纯内存计算）
- 验证完成后删除生成的测试文件

---

## 四、问题 3：边界打桩/Mock 策略

### 4.1 为什么需要打桩

裁剪后的子仓库在"边界"处会遇到：
- **已删除的外部依赖**：如数据库连接、HTTP 客户端、消息队列
- **已删除的内部模块**：如 `ProductService` 被删但 `OrderService` 仍引用
- **环境依赖**：如配置文件、环境变量、第三方 API

子仓库需要**在这些边界提供最小桩/Mock**，使核心功能路径可运行。

### 4.2 桩的分类

| 类型 | 描述 | 示例 |
|------|------|------|
| **Type Stub** | 仅提供类型定义，无实现 | `class ProductService: pass` |
| **Behavioral Stub** | 返回合理默认值的最小实现 | `def get_product(id): return {"id": id, "name": "stub"}` |
| **Data Stub** | 提供测试数据/fixtures | 内存数据库、JSON fixtures |
| **Interface Stub** | 实现接口但所有方法为空/抛异常 | `class StubCache implements Cache { get(): null }` |

### 4.3 设计：自动桩生成流水线

```
Step 1: 识别桩需求
  - 编译错误中的 "cannot find symbol" / "ModuleNotFoundError"
  - 启动测试中的运行时异常
  - 功能测试中的缺失依赖
  ↓
Step 2: 分类桩类型
  - LLM 分析缺失符号的角色：
    a) 核心业务逻辑（应保留但被误删）→ 从原仓库恢复
    b) 已删除功能的接口（边界依赖）→ 生成 Interface/Type Stub
    c) 外部依赖（数据库、网络等）→ 生成 Behavioral Stub
  ↓
Step 3: 生成桩代码
  - 从 CodeGraph 获取原始接口签名
  - LLM 生成最小桩实现
  - 桩文件统一放在 `_stubs/` 目录（Python: `_stubs/__init__.py`）
  ↓
Step 4: 注入桩
  - 修改 import 路径指向桩
  - 或使用依赖注入/monkey-patch
```

### 4.4 各语言的桩注入策略

#### Python
```python
# _stubs/cache.py (自动生成)
class Cache:
    """Stub: 替代已删除的 db.cache 模块"""
    def get(self, key): return None
    def set(self, key, value, ttl=None): pass
    def delete(self, key): pass

# 在 app.py 的 import 处替换：
# 原: from db.cache import Cache
# 改: from _stubs.cache import Cache
```

#### Java
```java
// _stubs/ProductService.java (自动生成)
package com.shop.service;

/**
 * Stub: 替代已删除的 ProductService
 * 提供 OrderService/CartService 所需的最小接口
 */
public class ProductService {
    public Product findById(String id) {
        return new Product(id, "stub-product", 0.0);
    }
    public boolean checkStock(String id, int quantity) {
        return true;  // stub: always in stock
    }
}
```

#### C
```c
// _stubs/vector_stub.c (自动生成)
// Stub: 替代已删除的 core/vector.c

#include "vector.h"

void vector_init(PtrVector *vec) {
    vec->items = NULL;
    vec->count = 0;
    vec->capacity = 0;
}

QStatus vector_push(PtrVector *vec, void *item) {
    // Minimal: 不实际分配内存，仅满足编译
    return Q_OK;
}

void vector_free(PtrVector *vec) {
    free(vec->items);
    vec->items = NULL;
}
```

### 4.5 数据脚本生成

**场景**：某些功能需要初始数据才能运行（如 ETL 的输入数据、Blog 的测试用户）。

**设计**：

```python
class DataScriptGenerator:
    """为功能验证生成最小测试数据"""
    
    def generate(self, sub_repo_path, user_instruction, missing_data_hint):
        """
        LLM 分析：
        1. 目标功能需要什么输入数据
        2. 数据的最小结构（schema）
        3. 生成内存中的 fixture 数据（不涉及文件/数据库）
        """
        prompt = f"""
        The pruned sub-repo implements: {user_instruction}
        The functional test needs test data but the data source was pruned.
        
        Generate a minimal data fixture that:
        1. Lives in-memory (dict/list/object)
        2. Contains just enough data to exercise the core path
        3. Is injected at the test's setup phase
        """
```

### 4.6 桩的生命周期

```
Phase3 开始
  │
  ├─ 编译验证失败 → 缺失符号 → 判断是否为桩候选
  │   └─ 是 → 生成 Type Stub → 重新编译
  │
  ├─ 启动验证失败 → 运行时缺失 → 判断是否为桩候选
  │   └─ 是 → 生成 Behavioral Stub → 重新启动验证
  │
  └─ 功能验证失败 → 缺失数据/依赖 → 判断是否为桩候选
      └─ 是 → 生成 Data Stub → 重新功能验证
```

**桩的标记**：所有桩文件以注释头标记来源
```python
# [CodePrune Stub] Generated to replace pruned module: db.cache
# Original interface from: db/cache.py (lines 10-45)
```

---

## 五、头脑风暴：其它核心问题

### 5.1 LLM 上下文窗口限制

**问题**：大型仓库的存活文件可能有几十上百个，全部传给 LLM 不现实。

**解决**：
- **分层摘要**：先传文件列表 + 每文件一行摘要 → LLM 选择需要的文件 → 再传完整内容
- **错误驱动聚焦**：只传与当前错误相关的文件（通过 import 链/调用链定位）
- **增量上下文**：每轮只传新增/变更的文件差异

### 5.2 功能测试脚本的可信度

**问题**：LLM 生成的功能测试脚本本身可能有 bug，导致误报"子仓库有问题"。

**解决**：
- **测试脚本自身编译验证**：先编译测试脚本，失败则重新生成（不扣修复轮次）
- **两阶段验证**：
  1. 先在**原仓库**上运行功能测试（作为 baseline，必须通过）
  2. 再在**子仓库**上运行同一测试
- **简化测试**：功能测试只验证"不崩溃" + "关键对象存在"，不验证业务正确性
- **最多重新生成 2 次**：如果连续 3 次测试脚本编译失败，放弃功能验证

### 5.3 桩代码与原仓库代码的边界

**问题**：桩代码可能引入新的不一致（如桩的返回值类型与原接口不匹配）。

**解决**：
- **从 CodeGraph 提取精确接口签名**：确保桩的方法签名、参数类型、返回类型与原始一致
- **Fidelity 检查就位**：Layer 3 已有真实性校验框架，仅对桩文件放宽容忍度
- **桩标记机制**：`FixPatch.synthetic = True` 已存在，确保桩不被误判为幻觉代码

### 5.4 修复循环的收敛性

**问题**：新增的验证层（启动验证、功能验证）可能导致修复循环不收敛。

**解决**：
- **严格的轮次预算分配**：
  ```
  总轮次 = max_heal_rounds (默认 5)
  - 编译修复: 最多消耗 3 轮
  - 启动修复: 最多消耗 2 轮
  - 功能修复: 最多消耗 2 轮
  - 总计上限仍为 5 轮（可配置为 8）
  ```
- **层级降级**：如果某层修复失败 2 次，设置 `skip_layers` 并继续
- **错误计数单调递减检查**：如果错误数连续 2 轮不减少 → 放弃该层

### 5.5 多语言环境依赖

**问题**：Java 需要 JDK、TypeScript 需要 Node.js、C 需要 gcc。验证机器可能缺少某些工具链。

**解决**：
- **预检查**：Phase3 开始前检测所需工具链是否可用
  ```python
  def _check_toolchain(self, language):
      commands = {"python": "python --version", "java": "javac -version", ...}
      try:
          subprocess.run(commands[language], capture_output=True, timeout=5)
          return True
      except: 
          return False
  ```
- **优雅降级**：工具链不可用时跳过启动/功能验证，仅做静态分析
- **Docker fallback** (远期)：提供 Dockerfile 模板，在容器中验证

### 5.6 副作用隔离

**问题**：功能测试可能有副作用（写文件、连接数据库、发网络请求）。

**解决**：
- **LLM 生成约束**：Prompt 中明确要求"纯内存操作，禁止 I/O 副作用"
- **沙箱执行**：subprocess 在临时目录中运行，限制文件系统访问
- **测试后清理**：删除生成的测试文件、桩文件（如果 Fidelity 检查不需要）

### 5.7 恢复 vs 打桩的决策困境

**问题**：某些被误删的文件（如 `retry.py`、`vector.c`），应该是从原仓库恢复还是打桩？

**设计**：决策树

```
文件在 golden answer 中应保留？
  ├─ 是 → 从原仓库整文件恢复 (这是 Phase2 的 Recall 缺陷)
  │       → 但这要求 Phase3 能重新评估 Phase2 的裁剪决策
  │
  └─ 否 / 不确定 → 进入桩策略
      │
      ├─ 被 3+ 个存活文件依赖？
      │   └─ 是 → 恢复整文件（强依赖 = 裁剪边界判断失误）
      │   └─ 否 → 打桩
      │
      └─ 是核心业务逻辑 vs 辅助功能？
          └─ 核心 → 恢复
          └─ 辅助 → 打桩
```

**实现**：在 Layer 2 (Completeness) 中，LLM + CodeGraph 联合判断：
```python
# 计算被删文件的"被引用度"
dep_count = sum(
    1 for node in surviving_nodes 
    if deleted_file in node.dependencies
)

if dep_count >= 3:
    action = "RESTORE"  # 太多依赖 → 裁剪判断失误
elif is_core_business_logic(deleted_file):
    action = "RESTORE"
else:
    action = "STUB"
```

### 5.8 C/C++ 头文件截断问题

**问题**：当前 Phase2 裁剪器对 C/C++ 头文件用 `// ... pruned N lines ...` 替代被删除的函数声明，导致外部无法使用这些函数。

**解决**：
- **头文件与源文件联动**：如果 `.c` 文件保留，其对应的 `.h` 中的**所有**函数声明都应保留
- **Phase 2.5 增加头文件修复**：扫描保留的 `.c` 文件中定义的函数 → 确保对应 `.h` 中有声明
- **回退策略**：如果 `.h` 被截断太多，从原仓库恢复整个 `.h` 文件

---

## 六、其它需要改进的方向

### 6.1 Phase2 精度提升（上游改进）

| 问题 | 改进方向 | 预期收益 |
|------|----------|----------|
| 应删未删 (7/9 项目) | 强化 anchor/closure 的边界判定，对"无任何存活依赖者引用"的文件强制删除 | 减少 false negatives |
| 应保留却误删 (3/9 项目) | 增加"被引用度"指标：被 2+ 存活文件 import 的文件不应删除 | 提升 Recall |
| 方法级裁剪 | 对保留文件内的方法做语义评估（当前为整文件粒度） | 减少 dead code |

### 6.2 `_pre_heal_cleanup` 增强

当前 Phase 2.5 只处理 import 语句。增强为：

```
Phase 2.5 增强:
  1. Import 清理 (现有)
  2. 引用审计 + 清理 (新增 Layer B)
  3. 注册表同步 (新增 Layer C)
  4. 头文件完整性修复 (新增，C/C++ 专用)
```

### 6.3 验证结果的诊断输出

在 `selection_diagnostics.json` 中增加 Phase3 的验证诊断：

```json
{
  "phase3_diagnostics": {
    "compile_validation": {"passed": true, "rounds": 2, "initial_errors": 15, "final_errors": 0},
    "boot_validation": {"passed": true, "script_generated": true, "attempts": 1},
    "functional_validation": {"passed": false, "error": "FUNCTIONAL_FAIL: NullPointerException"},
    "stubs_generated": [
      {"file": "_stubs/cache.py", "type": "behavioral", "reason": "pruned module db.cache"},
    ],
    "references_cleaned": 12,
    "registry_entries_synced": 4,
  }
}
```

### 6.4 Completeness Layer 实现

当前 `_check_completeness` 虽有框架但效果一般。增强：

```
现有: LLM 对比子仓库 vs 原仓库摘要 → 输出缺失组件列表
增强: 
  1. 编译/启动/功能测试中发现的缺失 → 直接作为 completeness 输入
  2. 引用审计中发现的断裂引用 → 如果源文件在原仓库中存在且不在 excluded 中 → 标记为缺失
  3. CodeGraph 依赖分析 → 被 3+ 存活文件引用的被删文件 → 自动恢复
```

### 6.5 测试验证增强 (U8)

当前 `_copy_relevant_tests` 通过名称匹配相关测试。增强：

```
1. 测试依赖分析：解析测试 import → 只复制不依赖被删模块的测试
2. 测试桩注入：对依赖被删模块的测试自动注入 Mock
3. 选择性运行：标记测试为 "full"(需要所有模块) vs "unit"(只需要单模块)
```

---

## 七、整体架构变更

### 7.1 HealEngine 新层级结构

```
heal()
  │
  ├─ Phase 2.5: 预清理 (现有 + 增强)
  │   ├─ Layer A: Import 清理 (现有)
  │   ├─ Layer B: 引用审计 (新增)
  │   ├─ Layer C: 注册表同步 (新增)
  │   └─ Layer D: 头文件修复 (新增, C/C++)
  │
  ├─ 验证-修复循环 (增强)
  │   ├─ Layer 1: Build 验证 (现有)
  │   ├─ Layer 1.5: Undefined Names (现有)
  │   ├─ Layer 2: Completeness (增强)
  │   ├─ Layer 2.5: Boot 验证 (新增) ★
  │   ├─ Layer 3: Fidelity (现有)
  │   ├─ Layer 3.5: Functional 验证 (新增) ★
  │   └─ Layer 4: Test (现有 U8)
  │
  └─ Finalize (现有)
      ├─ requirements/pom/package.json
      └─ README
```

### 7.2 新增配置项

```python
@dataclass
class HealConfig:
    # ... 现有配置 ...
    
    # 新增
    enable_boot_validation: bool = True         # Layer 2.5: 启动验证
    enable_functional_validation: bool = True   # Layer 3.5: 功能验证
    enable_reference_audit: bool = True         # Phase 2.5 Layer B
    enable_registry_sync: bool = True           # Phase 2.5 Layer C
    enable_stub_generation: bool = True         # 自动桩生成
    boot_timeout: int = 10                      # 启动验证超时(秒)
    functional_timeout: int = 30                # 功能验证超时(秒)
    max_test_script_retries: int = 2           # 测试脚本重新生成次数
```

### 7.3 新增文件

```
core/heal/
  ├─ fixer.py          (现有，增强 Layer 2.5 / 3.5)
  ├─ validator.py      (现有，增强 boot/functional 验证)
  ├─ import_fixer.py   (现有)
  ├─ finalize.py       (现有)
  ├─ reference_audit.py  (新增 - Layer B 引用审计)
  ├─ registry_sync.py    (新增 - Layer C 注册表同步)  
  ├─ stub_generator.py   (新增 - 桩生成)
  └─ boot_validator.py   (新增 - 启动/功能验证)
```

---

## 八、实施优先级

| 优先级 | 任务 | 预期可运行性提升 | 复杂度 |
|--------|------|-----------------|--------|
| **P0** | Layer B 引用审计 + 清理 | +25% (解决 9/9 入口点问题) | 中 |
| **P0** | Layer C 注册表同步 | +10% (解决 5/9 注册表问题) | 低 |
| **P1** | Layer 2.5 启动验证 | +15% (发现可运行性问题) | 中 |
| **P1** | 桩生成框架 | +15% (解决边界依赖) | 高 |
| **P2** | Layer 3.5 功能验证 | +10% (验证核心路径) | 高 |
| **P2** | 头文件修复 (C/C++) | +5% (C项目专用) | 低 |
| **P3** | Phase2 精度提升 | +10% (上游减少错误) | 高 |
| **P3** | 测试验证增强 | +5% (间接提升信心) | 中 |

**预期：P0+P1 完成后，可运行性从 ~17% 提升到 ~65%。全部完成后 ≥80%。**

---

## 九、从 aider / GitNexus 借鉴的成熟组件

> 原则：不做简单的 A+B 拼接，只借鉴**经过验证的模式和成熟实现**来服务我们的目标。

### 9.1 从 aider 借鉴

#### A. 反射消息循环 (Reflection Loop) — `base_coder.py`

aider 的核心修复循环非常成熟：
```python
while message:
    self.reflected_message = None
    list(self.send_message(message))      # LLM 生成修复
    if not self.reflected_message: break   # 没有新错误 → 完成
    if self.num_reflections >= self.max_reflections:
        break                              # 达到上限 → 停止
    self.num_reflections += 1
    message = self.reflected_message       # 错误反馈 → 下一轮
```

**我们可以借鉴的**：当前 HealEngine 的 heal 循环已经类似这个模式，但 aider 把错误信息直接作为下一轮 LLM 的 user message（`reflected_message`），而不是重新构造 prompt。这使得 LLM 拥有完整的修复历史上下文。

**具体应用**：在我们的启动/功能验证循环中，将验证失败的 stderr 直接作为 reflected_message 传回 LLM，保留修复历史。

#### B. Lint 输出 + TreeContext — `linter.py`

aider 的错误反馈格式非常精练：
```
# Fix any errors below, if possible.

## Running: flake8 --select=E9,F821,F823 file.py
file.py:30:5: F821 undefined name 'Cache'

## See relevant line below marked with █.
file.py:
  28│ from framework.core import App
  29│ 
█ 30│     cache = Cache()
  31│     cache.set("key", "value")
```

**关键设计**：
- `TreeContext` 使用 tree-sitter 生成带行号 + 错误标记（█）的代码上下文
- 只展示错误周围 ±3 行，不是整个文件
- flake8 只选择 fatal 级错误：`E9,F821,F823,F831,F406,F407,F701,F702,F704,F706`

**具体应用**：增强我们的 `_validate_build` 输出格式，在传给 LLM 的错误上下文中采用同样的 `█` 标记 + ±3 行上下文，替代当前的全文件传输。

#### C. 编译验证三层链 — `Linter.py_lint()`

```python
def py_lint(self, fname, rel_fname, code):
    basic_res = basic_lint(rel_fname, code)      # tree-sitter 语法
    compile_res = lint_python_compile(fname, code) # Python compile()
    flake_res = self.flake8_lint(rel_fname)        # flake8 undefined names
```

**具体应用**：我们的 Python 验证已经类似（AST + Pyflakes），但可以补充 tree-sitter 的 `traverse_tree()` 检测 `ERROR` 节点类型，这比 `ast.parse` 更宽容（能定位更多语法错误位置）。

#### D. 命令执行 + 输出反馈模板 — `prompts.py`

```
I ran this command:
{command}

And got this output:
{output}
```

**具体应用**：在启动验证/功能验证中，用同样的模板格式把 subprocess 的 stdout/stderr 反馈给 LLM。简单但有效。

### 9.2 从 GitNexus 借鉴

#### A. 入口点评分系统 — `entry-point-scoring.ts`

GitNexus 的入口点评分是多维的：
```
score = baseScore × exportMultiplier × nameMultiplier × frameworkMultiplier

baseScore = calleeCount / (callerCount + 1)  // 调用多、被调用少 = 入口点
exportMultiplier = isExported ? 2.0 : 1.0
nameMultiplier = matchesEntryPattern ? 1.5 : (matchesUtilityPattern ? 0.3 : 1.0)
frameworkMultiplier = frameworkDetected ? hint.multiplier : 1.0
```

配合语言特定的命名模式：
```python
ENTRY_POINT_PATTERNS = {
    '*': [r'^(main|init|start|run|setup|configure)$', r'^handle[A-Z]', r'Controller$'],
    'python': [r'^(get|post|put|delete)_', r'^view_', r'^app$'],
    'java': [r'^do[A-Z]', r'Service$', r'^create[A-Z]'],
    'c': [r'^main$', r'^init_', r'^start_'],
}

UTILITY_PATTERNS = [  # 反向模式 — 这些不是入口点
    r'^(get|set|is|has)[A-Z]', r'^_', r'^(format|parse|validate)',
    r'^(log|debug|error)', r'Helper$', r'Util$',
]
```

**具体应用**：在启动验证中，用这套评分系统识别子仓库的入口点，自动决定启动验证脚本应该 import 和实例化哪些对象。不需要 LLM 猜测。

#### B. 执行流追踪 (Process Detection) — `process-processor.ts`

```
1. 找入口点（无内部调用者的函数）
2. 从入口点 BFS 沿 CALLS 边前进
3. 去重 + 取最长路径
4. 输出: ProcessNode { trace: [nodeId1, nodeId2, ...], stepCount, communities }
```

**具体应用**：在功能验证中，从 CodeGraph 的 CALLS 关系 BFS 追踪目标功能的执行流。如果追踪路径中有节点对应的文件被删除 → 这就是断裂点，需要修复或打桩。

**与我们 CodeGraph 的对接**：
```python
# 我们的 CodeGraph 已有 edges/dependencies，可以直接做 BFS：
def trace_execution_flow(graph, entry_node_id, max_depth=10):
    """从入口点 BFS 追踪调用链，返回 [node_id, ...]"""
    visited = set()
    queue = [(entry_node_id, 0)]
    trace = []
    while queue:
        node_id, depth = queue.pop(0)
        if node_id in visited or depth > max_depth:
            continue
        visited.add(node_id)
        trace.append(node_id)
        for dep in graph.get_dependencies(node_id):
            if dep.dep_type == "calls":
                queue.append((dep.target_id, depth + 1))
    return trace
```

#### C. 社区凝聚度验证 — `community-processor.ts`

GitNexus 用 Leiden 算法将代码聚为社区（功能模块），计算凝聚度。

**具体应用思路**：裁剪后验证子仓库的"功能完整性" — 如果裁剪切断了社区内部的大量 CALLS 边，说明裁剪破坏了功能模块的内聚性。可以作为 Completeness Layer 的补充信号。

#### D. 测试文件识别 — `entry-point-scoring.ts: isTestFile()`

完整的多语言测试文件路径模式：
```python
TEST_FILE_PATTERNS = [
    '.test.', '.spec.', '__tests__/', '__mocks__/',
    '/test/', '/tests/', '/testing/',
    '_test.py', '/test_',           # Python
    '_test.go',                      # Go
    '/src/test/',                    # Java Maven
    'tests.swift', 'uitests/',       # Swift
    '.tests/',                       # C#
]
```

**具体应用**：增强我们 `_copy_relevant_tests` 的测试文件检测，现有实现偏简单。

### 9.3 不借鉴的部分（避免过度拼接）

| 组件 | 来源 | 不采用原因 |
|------|------|-----------|
| aider 的 SEARCH/REPLACE 编辑格式 | aider/coders/editblock_prompts.py | 我们已有三级模糊匹配的 `_find_context_core`，更适合批量修复 |
| GitNexus 的 KuzuDB 图存储 | gitnexus/mcp/core/kuzu-adapter.ts | 我们的 CodeGraph 已是内存中的，引入 DB 层增加复杂度 |
| GitNexus 的 MCP 工具框架 | gitnexus/mcp/tools.ts | 我们不需要 MCP 接口，直接 Python API 调用 |
| aider 的 RepoMap PageRank | aider/repomap.py | 我们已有 LLM hierarchical 语义评估，不需要 PageRank |
| GitNexus 的 Worker 线程池 | gitnexus/workers/ | Python 的 GIL 限制了线程池收益，当前性能瓶颈在 API 调用 |

---

## 十、风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| LLM 生成的测试脚本不可靠 | 误报/漏报 | 先在原仓库验证 baseline；最多重试 2 次 |
| 桩代码引入新 bug | 子仓库行为偏离原始 | Fidelity 检查 + 桩标记 + synthetic flag |
| 修复循环不收敛 | 超时/资源浪费 | 严格轮次预算 + 错误数单调递减检查 |
| 工具链不可用 | 无法执行验证 | 预检查 + 优雅降级（回退到静态分析） |
| 上下文窗口溢出 | LLM 无法处理 | 分层摘要 + 错误驱动聚焦 |
| 副作用执行 | 宿主机被影响 | 沙箱 subprocess + 纯内存约束 |
