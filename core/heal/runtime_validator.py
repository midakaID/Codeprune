"""
Layer 2.0: Runtime Validation — 确定性 import 扫描 + 运行时错误修复循环

与 boot_validator 的关键区别:
1. 确定性: 不依赖 LLM 生成测试脚本，直接 import 所有子仓库模块
2. 精准修复: 解析运行时错误 → 定位出错文件/符号 → 确定性修复(非LLM)
3. 自主循环: import → 发现错误 → 修复 → 重新 import, 直到全部通过或达上限

错误分类与修复策略:
- ModuleNotFoundError "No module named X":
    → 检查 X 是否 out_of_scope → 注释 import
    → 检查 X 是否可从原仓库补充 → 复制文件
- ImportError "cannot import name X from Y":
    → 检查 Y 模块的 __init__.py → 移除失效的 re-export
    → 检查 X 是否在该模块的其他文件中 → 修复 import 路径
- SyntaxError:
    → 委托给 build layer 处理
- AttributeError / TypeError during import:
    → 注释/移除出错行
"""

from __future__ import annotations

import ast
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RuntimeError_:
    """运行时错误（命名避免与 builtin 冲突）"""
    error_type: str         # ModuleNotFoundError / ImportError / SyntaxError / ...
    message: str
    module_name: str = ""   # 出错的模块名
    file_path: Optional[Path] = None  # 出错的文件 (相对路径)
    line: int = 0
    symbol: str = ""        # 缺失的符号名
    source_module: str = "" # ImportError 时的来源模块


@dataclass
class RuntimeValidationResult:
    """运行时验证结果"""
    success: bool
    errors: list[RuntimeError_] = field(default_factory=list)
    modules_tested: int = 0
    modules_passed: int = 0


class RuntimeValidator:
    """确定性运行时验证器: import all modules + 精准错误解析"""

    def __init__(self, sub_repo_path: Path, timeout: int = 30):
        self.sub_repo_path = sub_repo_path
        self.timeout = timeout

    def validate(self) -> RuntimeValidationResult:
        """扫描子仓库所有 Python 模块并尝试 import。

        使用单独的 subprocess 逐模块 import, 避免 import 副作用污染。
        """
        modules = self._discover_modules()
        if not modules:
            return RuntimeValidationResult(success=True)

        errors: list[RuntimeError_] = []
        passed = 0

        for mod_name in modules:
            error = self._try_import(mod_name)
            if error:
                errors.append(error)
            else:
                passed += 1

        return RuntimeValidationResult(
            success=len(errors) == 0,
            errors=errors,
            modules_tested=len(modules),
            modules_passed=passed,
        )

    def _discover_modules(self) -> list[str]:
        """发现子仓库中所有可 import 的 Python 模块, 按依赖拓扑排序"""
        modules: list[str] = []
        for py_file in sorted(self.sub_repo_path.rglob("*.py")):
            if py_file.name.startswith("_codeprune_"):
                continue
            # 跳过 __pycache__
            if "__pycache__" in py_file.parts:
                continue
            rel = py_file.relative_to(self.sub_repo_path)
            mod = str(rel.with_suffix("")).replace("\\", "/").replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[:-9]
            modules.append(mod)

        # 拓扑排序: __init__ 优先, 深层模块在后
        def sort_key(m: str) -> tuple:
            depth = m.count(".")
            is_init = m.endswith("__init__") or "." not in m
            return (depth, 0 if is_init else 1, m)

        modules.sort(key=sort_key)
        return modules

    def _try_import(self, module_name: str) -> Optional[RuntimeError_]:
        """尝试 import 单个模块, 返回错误或 None"""
        script = (
            f"import sys; sys.path.insert(0, '.'); "
            f"import {module_name}; "
            f"print('IMPORT_OK')"
        )
        try:
            proc = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=str(self.sub_repo_path),
            )

            if proc.returncode == 0 and "IMPORT_OK" in proc.stdout:
                return None

            output = (proc.stderr or "") + (proc.stdout or "")
            return self._parse_error(output, module_name)

        except subprocess.TimeoutExpired:
            return RuntimeError_(
                error_type="TimeoutError",
                message=f"Import of {module_name} timed out",
                module_name=module_name,
            )
        except Exception as e:
            return RuntimeError_(
                error_type=type(e).__name__,
                message=str(e),
                module_name=module_name,
            )

    def _parse_error(self, output: str, module_name: str) -> RuntimeError_:
        """解析 subprocess 输出, 提取结构化错误信息"""

        # ModuleNotFoundError: No module named 'xxx'
        m = re.search(r"ModuleNotFoundError: No module named '([^']+)'", output)
        if m:
            missing = m.group(1)
            return RuntimeError_(
                error_type="ModuleNotFoundError",
                message=f"No module named '{missing}'",
                module_name=module_name,
                symbol=missing,
            )

        # ImportError: cannot import name 'xxx' from 'yyy'
        m = re.search(
            r"ImportError: cannot import name '([^']+)' from '([^']+)'",
            output,
        )
        if m:
            name, source = m.group(1), m.group(2)
            # 提取文件路径
            file_path = None
            fm = re.search(r"\(([^)]+\.py)\)", output)
            if fm:
                try:
                    fp = Path(fm.group(1))
                    file_path = fp.relative_to(self.sub_repo_path)
                except (ValueError, OSError):
                    pass
            return RuntimeError_(
                error_type="ImportError",
                message=f"cannot import name '{name}' from '{source}'",
                module_name=module_name,
                symbol=name,
                source_module=source,
                file_path=file_path,
            )

        # SyntaxError
        m = re.search(
            r"SyntaxError: (.+?)(?:\n|$)",
            output,
        )
        if m:
            # 提取文件和行号
            fm = re.search(r'File "([^"]+)", line (\d+)', output)
            file_path = None
            line = 0
            if fm:
                try:
                    fp = Path(fm.group(1))
                    file_path = fp.relative_to(self.sub_repo_path)
                except (ValueError, OSError):
                    pass
                line = int(fm.group(2))
            return RuntimeError_(
                error_type="SyntaxError",
                message=m.group(1),
                module_name=module_name,
                file_path=file_path,
                line=line,
            )

        # AttributeError
        m = re.search(r"AttributeError: (.+?)(?:\n|$)", output)
        if m:
            fm = re.search(r'File "([^"]+)", line (\d+)', output)
            file_path = None
            line = 0
            if fm:
                try:
                    fp = Path(fm.group(1))
                    file_path = fp.relative_to(self.sub_repo_path)
                except (ValueError, OSError):
                    pass
                line = int(fm.group(2))
            return RuntimeError_(
                error_type="AttributeError",
                message=m.group(1),
                module_name=module_name,
                file_path=file_path,
                line=line,
            )

        # TypeError
        m = re.search(r"TypeError: (.+?)(?:\n|$)", output)
        if m:
            fm = re.search(r'File "([^"]+)", line (\d+)', output)
            file_path = None
            line = 0
            if fm:
                try:
                    fp = Path(fm.group(1))
                    file_path = fp.relative_to(self.sub_repo_path)
                except (ValueError, OSError):
                    pass
                line = int(fm.group(2))
            return RuntimeError_(
                error_type="TypeError",
                message=m.group(1),
                module_name=module_name,
                file_path=file_path,
                line=line,
            )

        # 通用: 从 traceback 最后一行提取
        lines = output.strip().splitlines()
        last = lines[-1] if lines else output[:200]
        m2 = re.match(r"(\w+Error):\s*(.+)", last)
        if m2:
            return RuntimeError_(
                error_type=m2.group(1),
                message=m2.group(2),
                module_name=module_name,
            )

        return RuntimeError_(
            error_type="UnknownError",
            message=output[-500:] if output else "import failed with no output",
            module_name=module_name,
        )


class RuntimeFixer:
    """确定性运行时错误修复器 — 不依赖 LLM"""

    def __init__(self, sub_repo_path: Path, source_repo_path: Path,
                 excluded_modules: list[str], graph=None):
        self.sub_repo_path = sub_repo_path
        self.source_repo_path = source_repo_path
        self.excluded = excluded_modules
        self.graph = graph  # CodeGraph — 用于从原仓库定位被裁函数
        self._supplemented_symbols: set[str] = set()  # 防重复补回
        self._supplemented_files: set[str] = set()    # 被增补策略修改的文件（相对路径）
        self._added_reexports: set[tuple[str, str]] = set()  # 本轮已补齐的 (symbol, source) re-export

    def fix(self, errors: list[RuntimeError_]) -> int:
        """修复运行时错误列表。返回成功修复的数量。"""
        fixed = 0
        seen: set[tuple[str, str, str, str, int]] = set()
        for err in errors:
            key = (
                err.error_type,
                err.symbol or err.module_name,
                err.source_module,
                str(err.file_path or ""),
                err.line,
            )
            if key in seen:
                continue
            seen.add(key)
            if self._fix_one(err):
                fixed += 1
        return fixed

    @staticmethod
    def _neutralize_line(line: str, reason: str = "removed (runtime)") -> str:
        """将一行替换为语法安全的 no-op，避免留下空 if/else/type-checking block。"""
        match = re.match(r'^(\s*)', line)
        indent = match.group(1) if match else ""
        stripped = line.strip()
        if not stripped:
            return line
        return f"{indent}pass  # [CodePrune] {reason}: {stripped}\n"

    def _fix_one(self, err: RuntimeError_) -> bool:
        """尝试修复单个运行时错误"""
        if err.error_type == "ModuleNotFoundError":
            return self._fix_module_not_found(err)
        elif err.error_type == "ImportError":
            return self._fix_import_error(err)
        elif err.error_type == "SyntaxError":
            return False  # SyntaxError 由 build layer 处理
        elif err.error_type in ("AttributeError", "TypeError"):
            return self._fix_attribute_error(err)
        return False

    def _fix_module_not_found(self, err: RuntimeError_) -> bool:
        """修复 ModuleNotFoundError — 注释 import 或从原仓库补充"""
        missing = err.symbol  # 缺失的模块名
        if not missing:
            return False

        # 判断是否是 out_of_scope 模块
        top_module = missing.split(".")[0]
        is_excluded = any(
            top_module == ex.replace("\\", "/").rstrip("/").split("/")[0]
            for ex in self.excluded
        )

        if is_excluded:
            # 扫描所有文件, 注释掉所有导入该模块的行
            return self._comment_imports_of(missing)

        # 尝试从原仓库补充
        parts = missing.replace(".", "/")
        candidates = [
            Path(f"{parts}.py"),
            Path(parts) / "__init__.py",
        ]
        import shutil
        for rel_path in candidates:
            src = self.source_repo_path / rel_path
            dst = self.sub_repo_path / rel_path
            if src.exists() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                logger.info(f"Runtime fix: 从原仓库补充 {rel_path}")
                self._supplemented_files.add(str(rel_path))
                # 恢复被注释的该模块 import（可能被 pre-heal ImportFixer 移除）
                self._restore_commented_imports(missing)
                return True

        # 如果同一轮前面的错误已经把模块补齐了，则视为已修复，避免重复 fallback 破坏代码
        if any((self.sub_repo_path / rel_path).exists() for rel_path in candidates):
            self._restore_commented_imports(missing)
            return True

        # 无法补充 → 注释精确匹配该模块的 import
        return self._comment_imports_of(missing)

    def _fix_import_error(self, err: RuntimeError_) -> bool:
        """修复 ImportError "cannot import name X from Y"

        策略优先级: 先增后删
        A. 补齐 barrel re-export (子仓库内已有定义)
        B. 从原仓库补回被裁函数定义
        C. (原) 移除 __init__.py 中失效 re-export
        D. (原) 注释调用方 import
        """
        symbol = err.symbol
        source = err.source_module
        if not symbol or not source:
            return False

        # ★ 策略 A: 补齐 barrel re-export
        if self._try_add_reexport(symbol, source):
            return True

        # ★ 策略 B: 从原仓库补回被裁函数
        if self._try_supplement_symbol(symbol, source):
            return True

        # 策略 C: 定位 __init__.py, 移除失效 re-export
        source_path = source.replace(".", "/")
        init_candidates = [
            self.sub_repo_path / source_path / "__init__.py",
            self.sub_repo_path / (source_path + ".py"),
        ]

        for init_path in init_candidates:
            if not init_path.exists():
                continue

            try:
                content = init_path.read_text(encoding="utf-8")
            except OSError:
                continue

            new_content = self._remove_symbol_from_file(content, symbol, init_path.name)
            if new_content != content:
                init_path.write_text(new_content, encoding="utf-8")
                rel = init_path.relative_to(self.sub_repo_path)
                logger.info(f"Runtime fix: 移除 {rel} 中对 '{symbol}' 的导出")
                return True

        # 如果 __init__.py 修复不了, 注释掉触发 import 的文件中的对应 import 行
        return self._comment_specific_import(err)

    def _remove_symbol_from_file(self, content: str, symbol: str,
                                  filename: str) -> str:
        """从文件中移除对指定 symbol 的 import/re-export"""
        lines = content.splitlines(keepends=True)
        new_lines: list[str] = []
        i = 0
        modified = False

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # 跳过已处理的行
            if stripped.startswith("# [CodePrune]"):
                new_lines.append(line)
                i += 1
                continue

            # 处理 "from .xxx import aaa, bbb, ccc" 或 "from xxx import ..."
            m = re.match(r'^(\s*)(from\s+\S+\s+import\s+)', stripped)
            if m and symbol in stripped:
                # 收集多行 import (处理括号续行)
                import_block = line
                block_start = i
                if "(" in line and ")" not in line:
                    i += 1
                    while i < len(lines) and ")" not in lines[i]:
                        import_block += lines[i]
                        i += 1
                    if i < len(lines):
                        import_block += lines[i]

                # 使用正则从 import 列表中移除符号
                # 处理 "from X import (a, b, c)" 和 "from X import a, b, c"
                new_block = self._remove_name_from_import(import_block, symbol)
                if new_block is None:
                    # 整行移除 (只有这一个 import name)
                    indent = re.match(r'^(\s*)', line).group(1)
                    new_lines.append(
                        self._neutralize_line(line, "removed")
                    )
                    # 跳过后续续行
                    while block_start < i:
                        block_start += 1
                    modified = True
                elif new_block != import_block:
                    new_lines.append(new_block)
                    # 跳过续行 (已合并到 new_block)
                    modified = True
                else:
                    new_lines.append(line)
                i += 1
                continue

            # 处理 __all__ 列表中的符号
            if "__all__" in stripped and symbol in stripped:
                new_line = self._remove_from_all(line, symbol)
                if new_line != line:
                    new_lines.append(new_line)
                    modified = True
                    i += 1
                    continue

            new_lines.append(line)
            i += 1

        return "".join(new_lines) if modified else content

    @staticmethod
    def _remove_name_from_import(import_text: str, name: str) -> Optional[str]:
        """从 import 语句中移除指定名称。

        返回修改后的文本, 如果移除后 import 列表为空则返回 None。
        """
        # 提取 import 列表部分
        m = re.match(r'^(\s*from\s+\S+\s+import\s+)\(?(.*?)\)?\s*$',
                     import_text, re.DOTALL)
        if not m:
            return import_text  # 无法解析, 不修改

        prefix = m.group(1)
        names_text = m.group(2)

        # 分割名称列表
        names = [n.strip().rstrip(",") for n in re.split(r'[,\n]', names_text)
                 if n.strip() and n.strip() != ","]

        # 移除目标名称 (含 as 别名)
        filtered = [n for n in names
                    if not re.match(rf'^{re.escape(name)}(\s+as\s+\w+)?$', n.strip())]

        if not filtered:
            return None  # 全部移除

        if len(filtered) == len(names):
            return import_text  # 未找到, 不修改

        # 重建 import 语句
        if len(filtered) <= 3:
            return f"{prefix}{', '.join(filtered)}\n"
        else:
            items = ",\n    ".join(filtered)
            return f"{prefix}(\n    {items},\n)\n"

    @staticmethod
    def _remove_from_all(line: str, name: str) -> str:
        """从 __all__ 列表中移除指定名称"""
        # 匹配 'name' 或 "name" 加可选逗号和空格
        patterns = [
            rf"""['"]{ re.escape(name) }['"],?\s*""",
            rf""",?\s*['"]{ re.escape(name) }['"]""",
        ]
        new_line = line
        for pat in patterns:
            new_line = re.sub(pat, "", new_line, count=1)
            if new_line != line:
                break
        return new_line

    def _fix_attribute_error(self, err: RuntimeError_) -> bool:
        """修复 AttributeError / TypeError — 注释出错行"""
        if not err.file_path or not err.line:
            return False

        file_path = self.sub_repo_path / err.file_path
        if not file_path.exists():
            return False

        try:
            lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
            idx = err.line - 1
            if 0 <= idx < len(lines):
                original = lines[idx].rstrip()
                if not original.strip().startswith("# [CodePrune]"):
                    lines[idx] = self._neutralize_line(lines[idx])
                    file_path.write_text("".join(lines), encoding="utf-8")
                    rel = file_path.relative_to(self.sub_repo_path)
                    logger.info(f"Runtime fix: 注释 {rel}:{err.line}")
                    return True
        except OSError:
            pass
        return False

    # ── 增加式修补策略 ────────────────────────────────────────────────

    def _try_add_reexport(self, symbol: str, source: str) -> bool:
        """策略 A: 在包的 __init__.py 中补齐缺失的 re-export。

        当子仓库中某个包的子模块定义了 symbol，但 __init__.py 忘了导出时，
        补一行 ``from pkg.submodule import symbol``。
        """
        source_dir = self.sub_repo_path / source.replace(".", "/")
        init_path = source_dir / "__init__.py"
        if not init_path.exists():
            return False

        # 在包的子模块中搜索 symbol 的定义
        defining_module = self._find_symbol_in_package(source_dir, source, symbol)
        if not defining_module:
            return False

        try:
            init_content = init_path.read_text(encoding="utf-8")
        except OSError:
            return False

        # 如果 __init__ 已经有该 symbol 的 import（可能被注释了）→ 尝试取消注释
        if self._try_uncomment_reexport(init_path, init_content, symbol):
            return True

        # 检查是否已有有效导出（未被注释）
        if re.search(rf'\bimport\b.*\b{re.escape(symbol)}\b', init_content):
            # 如果是本轮已修复的同类错误（不同 consumer 触发），视为已修复
            if (symbol, source) in self._added_reexports:
                return True
            # 已导出但仍然报错 → 不是 re-export 问题
            return False

        # 追加 re-export
        import_line = f"from {defining_module} import {symbol}\n"
        new_content = init_content.rstrip("\n") + "\n" + import_line
        init_path.write_text(new_content, encoding="utf-8")

        rel = init_path.relative_to(self.sub_repo_path)
        self._supplemented_files.add(str(rel))
        self._added_reexports.add((symbol, source))
        logger.info(f"Runtime fix [策略A]: 补齐 {rel} 中 '{symbol}' 的 re-export (from {defining_module})")
        return True

    def _find_symbol_in_package(self, pkg_dir: Path, pkg_name: str, symbol: str) -> str | None:
        """在包目录的子模块中搜索 symbol 的顶层定义，返回定义所在的模块全名。"""
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
                        rel = py_file.relative_to(self.sub_repo_path)
                        return str(rel.with_suffix("")).replace("\\", "/").replace("/", ".")
        return None

    def _try_uncomment_reexport(self, init_path: Path, content: str, symbol: str) -> bool:
        """如果 re-export 被 [CodePrune] 注释了，取消注释恢复它。"""
        lines = content.splitlines(keepends=True)
        changed = False
        for i, line in enumerate(lines):
            if symbol in line and "# [CodePrune]" in line:
                # 恢复被注释的行: "pass  # [CodePrune] removed: from X import Y" → "from X import Y"
                m = re.search(r'#\s*\[CodePrune\].*?:\s*(.+)$', line.rstrip())
                if m:
                    original = m.group(1).strip()
                    if symbol in original and ("import" in original):
                        indent = re.match(r'^(\s*)', line).group(1)
                        lines[i] = f"{indent}{original}\n"
                        changed = True
        if changed:
            init_path.write_text("".join(lines), encoding="utf-8")
            rel = init_path.relative_to(self.sub_repo_path)
            self._supplemented_files.add(str(rel))
            logger.info(f"Runtime fix [策略A]: 恢复 {rel} 中被注释的 '{symbol}' re-export")
        return changed

    def _try_supplement_symbol(self, symbol: str, source: str) -> bool:
        """策略 B: 从原仓库补回被 Phase 2 裁掉的函数/类定义。

        通过 CodeGraph 定位函数在原仓库中的位置，提取代码并插入子仓库。
        包含依赖安全检查——如果补回的代码依赖过多不可用符号则放弃。
        """
        if not self.graph or symbol in self._supplemented_symbols:
            return False

        from core.graph.schema import NodeType

        # Step 1: 在 CodeGraph 中定位 symbol
        target_types = {NodeType.FUNCTION, NodeType.CLASS}
        found_node = None
        source_prefix = source.replace(".", "/")

        for node in self.graph.nodes.values():
            if (node.name == symbol
                    and node.node_type in target_types
                    and node.file_path
                    and str(node.file_path).replace("\\", "/").startswith(source_prefix)):
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
        func_lines = orig_lines[br.start_line - 1: br.end_line]
        func_code = "".join(func_lines)

        # 确认函数在子仓库中确实不存在
        try:
            target_content = target_file.read_text(encoding="utf-8")
        except OSError:
            return False

        if f"def {symbol}(" in target_content or f"class {symbol}" in target_content:
            return False  # 已存在

        # Step 4: 依赖安全检查
        if not self._check_supplement_deps(func_code, target_file):
            logger.info(f"Runtime fix [策略B]: 补充 '{symbol}' 放弃 — 存在不可满足的依赖")
            return False

        # Step 5: 插入函数定义到目标文件
        insert_pos = self._find_supplement_insert_pos(target_content)
        new_content = (
            target_content[:insert_pos].rstrip("\n")
            + "\n\n\n"
            + func_code.rstrip("\n")
            + "\n"
            + target_content[insert_pos:].lstrip("\n")
        )
        target_file.write_text(new_content, encoding="utf-8")
        self._supplemented_symbols.add(symbol)
        self._supplemented_files.add(str(target_file.relative_to(self.sub_repo_path)))

        # Step 6: 确保 __init__.py 也导出该 symbol
        self._ensure_barrel_reexport(source, symbol, found_node.file_path)

        rel = target_file.relative_to(self.sub_repo_path)
        logger.info(
            f"Runtime fix [策略B]: 从原仓库补回 {rel}::{symbol} ({len(func_lines)} 行)"
        )
        return True

    def _check_supplement_deps(self, func_code: str, target_file: Path) -> bool:
        """检查补回的代码中引用的名称是否在子仓库中可满足。"""
        try:
            target_content = target_file.read_text(encoding="utf-8")
            target_tree = ast.parse(target_content)
        except (OSError, SyntaxError):
            return True  # 无法检查时保守通过

        # 收集目标文件中已可用的名称
        available: set[str] = set()
        for node in ast.walk(target_tree):
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
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        available.add(t.id)

        import builtins
        available.update(dir(builtins))

        # 提取 func_code 中引用的名称
        try:
            func_tree = ast.parse(func_code)
        except SyntaxError:
            return True

        referenced: set[str] = set()
        for node in ast.walk(func_tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                referenced.add(node.id)

        # 排除函数自身定义的参数和局部变量
        for node in ast.walk(func_tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in node.args.args + node.args.kwonlyargs:
                    referenced.discard(arg.arg)
                if node.args.vararg:
                    referenced.discard(node.args.vararg.arg)
                if node.args.kwarg:
                    referenced.discard(node.args.kwarg.arg)
                for child in ast.walk(node):
                    if isinstance(child, ast.Assign):
                        for t in child.targets:
                            if isinstance(t, ast.Name):
                                referenced.discard(t.id)

        unsatisfied = referenced - available
        if len(unsatisfied) > 3:
            logger.debug(f"补充依赖检查: unsatisfied={unsatisfied}")
            return False
        return True

    def _find_supplement_insert_pos(self, content: str) -> int:
        """找到在文件中插入补回函数的最佳位置（字符偏移量）。

        优先在 '# ... pruned N lines ...' 注释附近；回退到文件末尾。
        """
        lines = content.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if re.search(r'#\s*\.\.\.\s*pruned\s+\d+\s+lines', line, re.IGNORECASE):
                return sum(len(l) for l in lines[:i + 1])
        return len(content)

    def _ensure_barrel_reexport(self, source: str, symbol: str, file_path: Path) -> None:
        """确保包的 __init__.py 导出了指定 symbol。"""
        source_dir = self.sub_repo_path / source.replace(".", "/")
        init_path = source_dir / "__init__.py"
        if not init_path.exists():
            return

        try:
            content = init_path.read_text(encoding="utf-8")
        except OSError:
            return

        # 先尝试取消注释
        if self._try_uncomment_reexport(init_path, content, symbol):
            return

        # 检查是否已导出
        if re.search(rf'\b{re.escape(symbol)}\b', content):
            # 检查不是在注释中
            for line in content.splitlines():
                if symbol in line and not line.strip().startswith("#"):
                    return  # 已有有效导出

        # 构建模块全名并追加
        mod_name = str(file_path.with_suffix("")).replace("\\", "/").replace("/", ".")
        import_line = f"from {mod_name} import {symbol}\n"
        new_content = content.rstrip("\n") + "\n" + import_line
        init_path.write_text(new_content, encoding="utf-8")
        rel = init_path.relative_to(self.sub_repo_path)
        self._supplemented_files.add(str(rel))
        logger.info(f"Runtime fix [策略B]: 补齐 {rel} 对 '{symbol}' 的 re-export")

    def _comment_imports_of(self, module_name: str) -> bool:
        """注释子仓库中所有精确导入指定模块的行。"""
        any_fixed = False
        import_pattern = re.compile(
            rf"^(?:from\s+{re.escape(module_name)}(?:\.[\w\.]+)?\s+import\b|"
            rf"import\s+{re.escape(module_name)}(?:\b|\s+as\b|\.[\w\.]+))"
        )

        for py_file in self.sub_repo_path.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue
            try:
                lines = py_file.read_text(encoding="utf-8").splitlines(keepends=True)
            except OSError:
                continue

            changed = False
            for idx, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("# [CodePrune]"):
                    continue
                if import_pattern.match(stripped):
                    lines[idx] = self._neutralize_line(lines[idx])
                    changed = True

            if changed:
                try:
                    py_file.write_text("".join(lines), encoding="utf-8")
                    rel = py_file.relative_to(self.sub_repo_path)
                    logger.info(f"Runtime fix: 注释 {rel} 中对 '{module_name}' 的 import")
                    any_fixed = True
                except OSError:
                    pass

        return any_fixed

    def _restore_commented_imports(self, module_name: str) -> None:
        """恢复被 [CodePrune] 注释掉的模块 import。
        当模块已从原仓库补充回来后调用，将
        ``pass  # [CodePrune] removed: from db import execute_query, ...``
        恢复为原始 import 语句。
        """
        for py_file in self.sub_repo_path.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue
            try:
                lines = py_file.read_text(encoding="utf-8").splitlines(keepends=True)
            except OSError:
                continue

            changed = False
            for idx, line in enumerate(lines):
                if "# [CodePrune]" not in line:
                    continue
                # 匹配: pass  # [CodePrune] removed: from db import ...
                m = re.search(r'#\s*\[CodePrune\].*?:\s*(.+)$', line.rstrip())
                if not m:
                    continue
                original = m.group(1).strip()
                # 检查 import 是否指向被恢复的模块
                if not re.search(
                    rf'\bfrom\s+{re.escape(module_name)}(?:\.[\w.]+)?\s+import\b'
                    rf'|^import\s+{re.escape(module_name)}\b',
                    original,
                ):
                    continue
                indent = re.match(r'^(\s*)', line).group(1)
                lines[idx] = f"{indent}{original}\n"
                changed = True

            if changed:
                try:
                    py_file.write_text("".join(lines), encoding="utf-8")
                    rel = py_file.relative_to(self.sub_repo_path)
                    self._supplemented_files.add(str(rel))
                    logger.info(f"Runtime fix: 恢复 {rel} 中被注释的 '{module_name}' import")
                except OSError:
                    pass

    def _comment_specific_import(self, err: RuntimeError_) -> bool:
        """注释引发 ImportError 的具体 import 行"""
        symbol = err.symbol
        source = err.source_module
        if not symbol:
            return False

        any_fixed = False
        for py_file in self.sub_repo_path.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue
            try:
                content = py_file.read_text(encoding="utf-8")
            except OSError:
                continue

            # 查找包含 from source import ... symbol ... 的行
            pattern = rf'^(\s*)(from\s+{re.escape(source)}\s+import\s+.*)$'
            changed = False
            lines = content.splitlines(keepends=True)

            for idx, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("# [CodePrune]"):
                    continue
                if re.match(pattern, stripped) and symbol in stripped:
                    new_line = self._remove_symbol_from_file(
                        line, symbol, py_file.name,
                    )
                    if new_line != line:
                        lines[idx] = new_line
                        changed = True

            if changed:
                try:
                    py_file.write_text("".join(lines), encoding="utf-8")
                    rel = py_file.relative_to(self.sub_repo_path)
                    logger.info(
                        f"Runtime fix: 移除 {rel} 中对 '{symbol}' 的 import"
                    )
                    any_fixed = True
                except OSError:
                    pass

        return any_fixed
