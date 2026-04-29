"""
Phase 2.5+: 精确 Import 修复 & 级联清理
在 heal 循环前运行，确定性修复裁剪产生的 import 断裂。

两大能力:
1. ImportFixer  — 精确修复 import 语句（只移除不存在的名称，而非注释整行）
2. CascadeCleaner — 追踪被移除的名称，注释掉引用行
"""

from __future__ import annotations

import ast
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Python stdlib (3.10+ 有 sys.stdlib_module_names)
_PYTHON_STDLIB: set[str] = getattr(sys, "stdlib_module_names", set()) | {
    "__future__", "abc", "argparse", "array", "ast", "asyncio",
    "base64", "binascii", "bisect", "builtins", "calendar",
    "cmath", "codecs", "collections", "colorsys", "concurrent",
    "configparser", "contextlib", "copy", "csv", "ctypes",
    "dataclasses", "datetime", "decimal", "difflib", "dis",
    "email", "enum", "errno", "faulthandler", "fcntl",
    "filecmp", "fnmatch", "fractions", "ftplib", "functools",
    "gc", "getpass", "gettext", "glob", "gzip",
    "hashlib", "heapq", "hmac", "html", "http",
    "importlib", "inspect", "io", "ipaddress", "itertools",
    "json", "keyword", "linecache", "locale", "logging",
    "lzma", "math", "mimetypes", "multiprocessing",
    "numbers", "operator", "os", "pathlib", "pdb",
    "pickle", "platform", "pprint", "profile", "pstats",
    "queue", "random", "re", "readline", "reprlib",
    "resource", "secrets", "select", "shelve", "shlex",
    "shutil", "signal", "site", "smtplib", "socket",
    "socketserver", "sqlite3", "ssl", "stat", "statistics",
    "string", "struct", "subprocess", "sys", "sysconfig",
    "tempfile", "termios", "textwrap", "threading", "time",
    "timeit", "tkinter", "token", "tokenize", "tomllib",
    "trace", "traceback", "tracemalloc", "tty", "turtle",
    "types", "typing", "unicodedata", "unittest", "urllib",
    "uuid", "venv", "warnings", "wave", "weakref",
    "webbrowser", "xml", "xmlrpc", "zipfile", "zipimport", "zlib",
    "_thread",
}


class ImportFixer:
    """确定性 import 修复器 — 在 heal 循环前运行，不消耗修复轮次

    核心改进: 对 `from X import a, b, c` 精确移除不存在的名称,
    而非注释整行导致 a, b 也不可用。

    安全机制: 删除前检查名称是否仍在文件代码中被使用 (usage-aware)。
    """

    def __init__(
        self,
        sub_repo: Path,
        out_of_scope: list[str],
    ):
        self.sub_repo = sub_repo
        self.out_of_scope = out_of_scope
        # 预计算
        self._excluded_tokens = self._build_excluded_tokens()
        self._module_exports: dict[str, set[str]] = {}  # module_dotpath → exported names
        self._available_modules: set[str] = set()        # 子仓库内可导入的模块路径
        self._scan_sub_repo()

    # ── 公共 API ─────────────────────────────────────────────────

    def fix_all(self) -> tuple[int, dict[Path, set[str]]]:
        """
        扫描子仓库所有 Python 文件，精确修复每条 import 语句。

        Returns:
            (fixed_count, removed_names_by_file):
              - fixed_count: 修改的行数
              - removed_names_by_file: 每个文件被移除的导入名称集合
                （供 CascadeCleaner 使用）
        """
        total_fixed = 0
        all_removed: dict[Path, set[str]] = {}

        # 多轮收敛：下游 __init__.py 被修正后，上游 re-export 链需要再次同步
        for _pass in range(3):
            self._module_exports.clear()
            self._available_modules.clear()
            self._scan_sub_repo()

            pass_fixed = 0
            pass_removed: dict[Path, set[str]] = {}

            for py_file in sorted(self.sub_repo.rglob("*.py")):
                rel = py_file.relative_to(self.sub_repo)
                fixed, removed = self._fix_file(py_file, rel)
                if fixed:
                    pass_fixed += fixed
                if removed:
                    pass_removed.setdefault(rel, set()).update(removed)

            total_fixed += pass_fixed
            for rel, removed in pass_removed.items():
                all_removed.setdefault(rel, set()).update(removed)

            if pass_fixed == 0:
                break

        if total_fixed:
            logger.info(f"ImportFixer: 精确修复 {total_fixed} 行 import")

        return total_fixed, all_removed

    # ── 单文件修复 ───────────────────────────────────────────────

    def _fix_file(self, abs_path: Path, rel_path: Path) -> tuple[int, set[str]]:
        """修复单个文件的 import 语句。返回 (修复行数, 移除的名称集合)。"""
        try:
            source = abs_path.read_text(encoding="utf-8")
        except OSError:
            return 0, set()

        try:
            tree = ast.parse(source, filename=str(rel_path))
        except SyntaxError:
            return 0, set()

        lines = source.splitlines(keepends=True)
        removed_names: set[str] = set()
        edits: list[tuple[int, int, str | None]] = []  # (start_line_0based, end_line_0based, replacement | None)

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                edit = self._handle_import(node, rel_path)
                if edit:
                    edits.append(edit)
                    for alias in node.names:
                        name = alias.asname or alias.name
                        module_top = alias.name.split(".")[0]
                        if self._is_excluded_module(alias.name) or not self._module_exists(alias.name):
                            removed_names.add(name)

            elif isinstance(node, ast.ImportFrom):
                edit, names = self._handle_import_from(node, rel_path)
                if edit:
                    edits.append(edit)
                    removed_names.update(names)

        if not edits:
            return 0, set()

        # 从后向前应用编辑（避免行号偏移）
        edits.sort(key=lambda e: e[0], reverse=True)
        for start, end, replacement in edits:
            if replacement is None:
                # 注释掉该行
                for i in range(start, end + 1):
                    if 0 <= i < len(lines):
                        original = lines[i].rstrip()
                        if not original.lstrip().startswith("# [CodePrune]"):
                            lines[i] = f"# [CodePrune] removed: {original}\n"
            else:
                # 替换为精确修复后的行
                # 先算出需要替换的行范围
                old_range = lines[start:end + 1]
                # 保持原始缩进
                indent = ""
                for ch in (old_range[0] if old_range else ""):
                    if ch in (" ", "\t"):
                        indent += ch
                    else:
                        break
                new_line = indent + replacement + "\n"
                lines[start:end + 1] = [new_line]

        try:
            abs_path.write_text("".join(lines), encoding="utf-8")
        except OSError:
            return 0, set()

        # __all__ 同步: 从 __all__ 列表中移除已删除的导入名称
        if removed_names:
            self._sync_dunder_all(abs_path, removed_names)

        fixed_count = len(edits)
        if removed_names:
            logger.debug(f"  {rel_path}: 修复 {fixed_count} 行, 移除名称 {removed_names}")
        return fixed_count, removed_names

    # ── Import 语句处理 ──────────────────────────────────────────

    def _handle_import(
        self, node: ast.Import, rel_path: Path,
    ) -> tuple[int, int, str | None] | None:
        """处理 `import X` / `import X, Y` 语句

        Returns None 如果无需修改，否则 (start, end, replacement_or_None)
        """
        # 检查每个导入的模块
        keep = []
        remove = []
        for alias in node.names:
            module = alias.name
            top = module.split(".")[0]
            if top in _PYTHON_STDLIB:
                keep.append(alias)
            elif self._is_excluded_module(module):
                remove.append(alias)
            elif self._module_exists(module):
                keep.append(alias)
            else:
                # 第三方包 — 尝试 importlib 检测
                if self._is_third_party(top):
                    keep.append(alias)
                else:
                    remove.append(alias)

        if not remove:
            return None

        start = node.lineno - 1
        end = (node.end_lineno or node.lineno) - 1

        if not keep:
            # 全部移除 → 注释整行
            return (start, end, None)

        # 部分保留 → 重建 import 语句
        parts = []
        for alias in keep:
            if alias.asname:
                parts.append(f"{alias.name} as {alias.asname}")
            else:
                parts.append(alias.name)
        replacement = "import " + ", ".join(parts)
        return (start, end, replacement)

    def _handle_import_from(
        self, node: ast.ImportFrom, rel_path: Path,
    ) -> tuple[tuple[int, int, str | None] | None, set[str]]:
        """处理 `from X import a, b, c` 语句

        Returns (edit_or_None, removed_name_set)
        """
        removed_names: set[str] = set()

        if node.level > 0:
            # 相对导入 — 需要解析
            module_path = self._resolve_relative_import(node, rel_path)
        else:
            module_path = node.module or ""

        top = module_path.split(".")[0] if module_path else ""

        # 整个模块被排除
        if top and self._is_excluded_module(module_path):
            # 安全检查: 即使模块被排除，仍检查每个导入名是否在代码中被使用
            keep_aliases = []
            remove_aliases = []
            for alias in node.names:
                local_name = alias.asname or alias.name
                if self._is_name_used_in_file(rel_path, local_name):
                    keep_aliases.append(alias)
                    logger.debug(
                        f"  {rel_path}: 保留 '{local_name}' — 模块被排除但名称在代码中被使用"
                    )
                else:
                    remove_aliases.append(alias)
                    removed_names.add(local_name)
            if not remove_aliases:
                return None, set()
            start = node.lineno - 1
            end = (node.end_lineno or node.lineno) - 1
            if not keep_aliases:
                return (start, end, None), removed_names
            # 部分保留
            dots = "." * node.level
            parts = []
            for alias in keep_aliases:
                if alias.asname:
                    parts.append(f"{alias.name} as {alias.asname}")
                else:
                    parts.append(alias.name)
            replacement = f"from {dots}{node.module or ''} import {', '.join(parts)}"
            return (start, end, replacement), removed_names

        # 模块是 stdlib/第三方 → 保留不动
        if top in _PYTHON_STDLIB or self._is_third_party(top):
            return None, set()

        # 模块存在于子仓库 → 检查每个导入名称是否存在
        module_exports = self._get_module_exports(module_path)
        if module_exports is None:
            # 模块不存在于子仓库 → 可能是第三方
            if self._is_third_party(top):
                return None, set()
            # 模块完全不存在 → 安全检查: 仍需验证名称是否被使用
            keep_aliases = []
            remove_aliases = []
            for alias in node.names:
                local_name = alias.asname or alias.name
                if self._is_name_used_in_file(rel_path, local_name):
                    keep_aliases.append(alias)
                    logger.debug(
                        f"  {rel_path}: 保留 '{local_name}' — 模块不存在但名称在代码中被使用"
                    )
                else:
                    remove_aliases.append(alias)
                    removed_names.add(local_name)
            if not remove_aliases:
                return None, set()
            start = node.lineno - 1
            end = (node.end_lineno or node.lineno) - 1
            if not keep_aliases:
                return (start, end, None), removed_names
            dots = "." * node.level
            parts = []
            for alias in keep_aliases:
                if alias.asname:
                    parts.append(f"{alias.name} as {alias.asname}")
                else:
                    parts.append(alias.name)
            replacement = f"from {dots}{node.module or ''} import {', '.join(parts)}"
            return (start, end, replacement), removed_names

        # 模块存在 → 精确检查每个名称
        keep = []
        remove = []
        for alias in node.names:
            name = alias.name
            local_name = alias.asname or name
            if name == "*":
                # star import — 保留
                keep.append(alias)
            elif name in module_exports:
                keep.append(alias)
            else:
                # 安全检查: 名称虽不在模块导出中，但在文件代码中被引用 → 保留
                if self._is_name_used_in_file(rel_path, local_name):
                    keep.append(alias)
                    logger.debug(
                        f"  {rel_path}: 保留 '{local_name}' — 不在模块导出中但在代码中被使用"
                    )
                else:
                    remove.append(alias)
                    removed_names.add(local_name)

        if not remove:
            return None, set()

        start = node.lineno - 1
        end = (node.end_lineno or node.lineno) - 1

        if not keep:
            return (start, end, None), removed_names

        # 精确重建: from X import a, b (移除了 c)
        from_module = node.module or ""
        # 重建 level dots
        dots = "." * node.level
        parts = []
        for alias in keep:
            if alias.asname:
                parts.append(f"{alias.name} as {alias.asname}")
            else:
                parts.append(alias.name)
        names_str = ", ".join(parts)
        replacement = f"from {dots}{from_module} import {names_str}"
        return (start, end, replacement), removed_names

    # ── 子仓库扫描 ──────────────────────────────────────────────

    def _scan_sub_repo(self) -> None:
        """扫描子仓库，收集可用模块及其导出名称"""
        for py_file in self.sub_repo.rglob("*.py"):
            rel = py_file.relative_to(self.sub_repo)
            # 模块路径: db/session.py → "db.session"
            module_path = str(rel.with_suffix("")).replace("\\", "/").replace("/", ".")
            # 去掉 __init__ 后缀
            if module_path.endswith(".__init__"):
                module_path = module_path[:-9]  # len(".__init__") == 9

            self._available_modules.add(module_path)
            # 也加入顶级模块名
            top = module_path.split(".")[0]
            self._available_modules.add(top)

            # 扫描导出名称
            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source)
                names: set[str] = set()
                for child in ast.iter_child_nodes(tree):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        names.add(child.name)
                    elif isinstance(child, ast.ClassDef):
                        names.add(child.name)
                    elif isinstance(child, ast.Assign):
                        for target in child.targets:
                            if isinstance(target, ast.Name):
                                names.add(target.id)
                    elif isinstance(child, (ast.Import, ast.ImportFrom)):
                        # re-exports
                        if isinstance(child, ast.ImportFrom):
                            for alias in child.names:
                                names.add(alias.asname or alias.name)
                        else:
                            for alias in child.names:
                                names.add(alias.asname or alias.name.split(".")[-1])
                self._module_exports[module_path] = names
            except (OSError, SyntaxError):
                self._module_exports[module_path] = set()

    def _get_module_exports(self, module_path: str) -> set[str] | None:
        """获取模块的导出名称。模块不存在于子仓库时返回 None。"""
        if module_path in self._module_exports:
            return self._module_exports[module_path]
        # 尝试各种路径变体
        parts = module_path.split(".")
        for i in range(len(parts), 0, -1):
            candidate = ".".join(parts[:i])
            if candidate in self._module_exports:
                return self._module_exports[candidate]
        return None

    def _module_exists(self, module_path: str) -> bool:
        """检查模块是否存在于子仓库"""
        if module_path in self._available_modules:
            return True
        top = module_path.split(".")[0]
        return top in self._available_modules

    # ── 排除判断 ─────────────────────────────────────────────────

    def _sync_dunder_all(self, abs_path: Path, removed_names: set[str]) -> None:
        """从 __all__ 列表中移除已删除的导入名称"""
        try:
            source = abs_path.read_text(encoding="utf-8")
        except OSError:
            return

        # 匹配 __all__ = [...] 或 __all__ += [...]
        # 逐行扫描，找到包含 __all__ 的赋值块
        lines = source.splitlines(keepends=True)
        all_pattern = re.compile(r'^(\s*)__all__\s*[+]?=\s*\[')
        modified = False

        i = 0
        while i < len(lines):
            m = all_pattern.match(lines[i])
            if m:
                # 找到 __all__ 赋值的完整范围
                all_start = i
                all_end = i
                merged = lines[i]
                while "]" not in merged and all_end < len(lines) - 1:
                    all_end += 1
                    merged += lines[all_end]

                # 解析当前 __all__ 中的名称
                all_block = "".join(lines[all_start:all_end + 1])
                # 移除已删除的名称条目
                for name in removed_names:
                    # 匹配 "name" 或 'name' 及其后面的逗号和可能的空格/换行
                    all_block = re.sub(
                        rf'''["']{re.escape(name)}["']\s*,?\s*\n?''',
                        "",
                        all_block,
                    )
                # 清理多余的逗号和空行
                all_block = re.sub(r',\s*\]', '\n]', all_block)
                all_block = re.sub(r'\n\s*\n', '\n', all_block)

                new_lines = all_block.splitlines(keepends=True)
                if new_lines != lines[all_start:all_end + 1]:
                    lines[all_start:all_end + 1] = new_lines
                    modified = True
                    logger.debug(
                        f"  {abs_path.name}: __all__ 已移除 "
                        f"{removed_names & self._get_all_names(merged)}"
                    )
                i = all_start + len(new_lines)
            else:
                i += 1

        if modified:
            try:
                abs_path.write_text("".join(lines), encoding="utf-8")
            except OSError:
                pass

    @staticmethod
    def _get_all_names(all_text: str) -> set[str]:
        """从 __all__ 文本中提取所有名称"""
        return set(re.findall(r'''["'](\w+)["']''', all_text))

    def _build_excluded_tokens(self) -> set[str]:
        """从 out_of_scope 构建排除 token 集"""
        tokens: set[str] = set()
        for ex in self.out_of_scope:
            ex_norm = ex.replace("\\", "/").rstrip("/")
            tokens.add(ex_norm.replace("/", ".").removesuffix(".py"))
            parts = ex_norm.split("/")
            if len(parts) == 1:
                tokens.add(parts[0].removesuffix(".py"))
            else:
                # 不再添加顶级目录名 — 避免将同包下所有子模块错误标记为 excluded
                if "." in parts[-1]:
                    tokens.add(parts[-1].rsplit(".", 1)[0])
        return tokens

    def _is_excluded_module(self, module_path: str) -> bool:
        """检查模块是否在 out_of_scope 中（支持前缀匹配）"""
        for token in self._excluded_tokens:
            if module_path == token or module_path.startswith(token + "."):
                return True
        return False

    def _is_name_used_in_file(self, rel_path: Path, name: str) -> bool:
        """AST 级检查: 名称是否在文件的非 import 代码中被引用。

        用于在删除 import 前确认名称不再被使用（usage-aware safety check）。
        注意：仅出现在 ``__all__`` 中的名称不算真实运行时使用，
        否则会让失效的 re-export 链一直保留下来。
        """
        abs_path = self.sub_repo / rel_path
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            return True  # 保守: 解析失败时保留 import

        dunder_all_lines: set[int] = set()
        for node in ast.walk(tree):
            targets = None
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            if not targets:
                continue
            if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                start = getattr(node, "lineno", 0)
                end = getattr(node, "end_lineno", start)
                dunder_all_lines.update(range(start, end + 1))

        for node in ast.walk(tree):
            # 跳过 import 语句本身
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue

            node_line = getattr(node, "lineno", 0)
            if node_line in dunder_all_lines:
                continue

            # ast.Name: 只统计真正的读取引用, 如 new_request_id()
            if (
                isinstance(node, ast.Name)
                and node.id == name
                and isinstance(node.ctx, ast.Load)
            ):
                return True

            # ast.Attribute.value: 如 name.method()
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == name:
                    return True
        return False

    def _is_third_party(self, top_module: str) -> bool:
        """尝试判断是否为可导入的第三方包"""
        if not top_module:
            return False
        try:
            import importlib
            importlib.import_module(top_module)
            return True
        except ImportError:
            return False

    def _resolve_relative_import(self, node: ast.ImportFrom, rel_path: Path) -> str:
        """解析相对导入为绝对模块路径"""
        # 例如: from ..models import User, 在 auth/service.py 中
        # level=2, module="models" → "models" (相对于 auth 的上级)
        parts = list(rel_path.parent.parts)
        level = node.level
        if level > len(parts):
            level = len(parts)
        base_parts = parts[:len(parts) - level + 1] if level > 0 else parts
        module = node.module or ""
        if base_parts:
            return ".".join(base_parts) + ("." + module if module else "")
        return module


class CascadeCleaner:
    """级联清理器: import 移除后，注释掉引用被移除名称的代码行

    策略:
    - 简单引用行（赋值、调用、属性访问）→ 注释
    - 复杂引用（控制流条件、混合使用）→ 跳过，留给 LLM
    """

    def __init__(self, sub_repo: Path):
        self.sub_repo = sub_repo

    def clean_all(self, removed_names_by_file: dict[Path, set[str]]) -> int:
        """
        对每个文件，注释掉引用被移除名称的简单代码行。

        Args:
            removed_names_by_file: ImportFixer 返回的 {rel_path → removed_names}

        Returns:
            注释行总数
        """
        total = 0
        for rel_path, names in removed_names_by_file.items():
            if not names:
                continue
            abs_path = self.sub_repo / rel_path
            cleaned = self._clean_file(abs_path, names)
            total += cleaned

        if total:
            logger.info(f"CascadeCleaner: 级联注释 {total} 行引用")
        return total

    def _clean_file(self, abs_path: Path, removed_names: set[str]) -> int:
        """清理单个文件中引用 removed_names 的行"""
        try:
            source = abs_path.read_text(encoding="utf-8")
        except OSError:
            return 0

        try:
            tree = ast.parse(source)
        except SyntaxError:
            return 0

        lines = source.splitlines(keepends=True)
        # 收集需要注释的行号 (0-based)
        lines_to_comment: set[int] = set()

        # 收集所有引用 removed_names 的位置
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in removed_names:
                line_idx = node.lineno - 1
                if self._is_safe_to_comment(tree, node, removed_names):
                    lines_to_comment.add(line_idx)

        if not lines_to_comment:
            return 0

        # 排除 import 行本身（已由 ImportFixer 处理）和已注释行
        final_lines: list[int] = []
        for idx in sorted(lines_to_comment):
            if 0 <= idx < len(lines):
                stripped = lines[idx].strip()
                if stripped.startswith(("import ", "from ", "# [CodePrune]")):
                    continue
                final_lines.append(idx)

        if not final_lines:
            return 0

        for idx in final_lines:
            original = lines[idx].rstrip()
            lines[idx] = f"# [CodePrune] cascade-removed: {original}\n"

        try:
            abs_path.write_text("".join(lines), encoding="utf-8")
        except OSError:
            return 0

        return len(final_lines)

    def _is_safe_to_comment(
        self, tree: ast.AST, name_node: ast.Name, removed_names: set[str],
    ) -> bool:
        """判断引用行是否可以安全注释（不破坏其他逻辑）

        安全条件 (满足任一即可):
        1. 该行所有 Name 引用都在 removed_names 中（纯被移除引用行）
        2. 该行是一个调用语句，且调用目标是被移除的名称
           例如: `register_routes(self)` → register_routes 被移除 → 安全注释
        3. 该行是一个赋值语句，值是被移除名称的调用
           例如: `result = get_data()` → get_data 被移除 → 安全注释
           （但仅当 result 不被其他保留代码引用时）

        不安全:
        - 在 if/while/for 的条件部分
        - 该行混合使用保留名称和被移除名称，但非调用场景
        """
        target_line = name_node.lineno

        # 在 if/while/for 的条件中 → 不安全
        for node in ast.walk(tree):
            if isinstance(node, (ast.If, ast.While)):
                if hasattr(node, 'test') and node.test:
                    test_lines = set(range(
                        node.test.lineno,
                        (node.test.end_lineno or node.test.lineno) + 1,
                    ))
                    if target_line in test_lines:
                        return False
            elif isinstance(node, ast.For):
                if hasattr(node, 'iter') and node.iter:
                    iter_lines = set(range(
                        node.iter.lineno,
                        (node.iter.end_lineno or node.iter.lineno) + 1,
                    ))
                    if target_line in iter_lines:
                        return False

        # 查找包含该行的语句节点
        for node in ast.walk(tree):
            if not isinstance(node, ast.stmt):
                continue
            if node.lineno != target_line:
                continue

            # Case: Expr(value=Call(func=Name(id=removed)))
            # 例如: register_routes(self)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                func = node.value.func
                if isinstance(func, ast.Name) and func.id in removed_names:
                    return True
                # method call on removed name: removed_obj.method()
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    if func.value.id in removed_names:
                        return True

            # Case: Assign(targets=[Name], value=Call(func=Name(id=removed)))
            # 例如: result = get_data(x)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                func = node.value.func
                if isinstance(func, ast.Name) and func.id in removed_names:
                    return True

        # Fallback: 如果该行所有 Name 都在 removed_names → 安全
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.lineno == target_line:
                if node.id not in removed_names:
                    return False
        return True


# ═══════════════════════════════════════════════════════════════
#  Phase B: Undefined Name 分类 & 自动补 Import
# ═══════════════════════════════════════════════════════════════

class UndefinedNameClassification:
    """undefined name 的分类结果"""
    IGNORE = "ignore"                # stdlib/builtins → 不需要处理
    PRUNE_EXPECTED = "prune_expected" # 裁剪导致的，已级联清理
    FIXABLE = "fixable"              # 名称在子仓库其他模块中，可自动补 import
    LLM_REQUIRED = "llm_required"    # 需要 LLM 介入


class UndefinedNameResolver:
    """Phase B: 检测并分类 undefined names，自动补全可修复的 import

    在 Phase A (ImportFixer + CascadeCleaner) 之后运行。
    处理残留的 undefined name:
    - 从 CodeGraph 查找名称定义位置
    - 无歧义时自动添加 import
    - 有歧义或无法定位时标记为 llm_required
    """

    def __init__(
        self,
        sub_repo: Path,
        graph: "CodeGraph",
        removed_names: dict[Path, set[str]] | None = None,
    ):
        self.sub_repo = sub_repo
        self.graph = graph
        self._removed_names: set[str] = set()
        if removed_names:
            for names in removed_names.values():
                self._removed_names.update(names)

        # 缓存: 子仓库内模块 → 导出名称
        self._module_exports: dict[str, set[str]] = {}
        self._scan_exports()

        # 缓存: CodeGraph 中 name → [(module_path, node)]
        self._graph_name_index: dict[str, list[tuple[str, "CodeNode"]]] = {}
        self._index_graph()

    # ── 公共 API ─────────────────────────────────────────────────

    def resolve_all(self) -> tuple[int, list[dict]]:
        """
        扫描子仓库所有 Python 文件，检测 undefined names 并尝试自动修复。

        Returns:
            (auto_fixed_count, unresolved_errors):
              - auto_fixed_count: 自动添加的 import 数量
              - unresolved_errors: 无法自动修复的 [{file, line, name, classification}]
        """
        auto_fixed = 0
        unresolved: list[dict] = []

        for py_file in sorted(self.sub_repo.rglob("*.py")):
            rel = py_file.relative_to(self.sub_repo)
            undefined = self._detect_undefined_names(py_file, rel)

            for name, lineno in undefined:
                cls = self._classify(name, rel)

                if cls == UndefinedNameClassification.IGNORE:
                    continue
                elif cls == UndefinedNameClassification.PRUNE_EXPECTED:
                    continue
                elif cls == UndefinedNameClassification.FIXABLE:
                    if self._auto_add_import(py_file, rel, name):
                        auto_fixed += 1
                        continue
                # FIXABLE 但添加失败，或 LLM_REQUIRED
                unresolved.append({
                    "file": str(rel),
                    "line": lineno,
                    "name": name,
                    "classification": cls,
                })

        if auto_fixed:
            logger.info(f"UndefinedNameResolver: 自动补全 {auto_fixed} 个 import")
        if unresolved:
            logger.info(
                f"UndefinedNameResolver: {len(unresolved)} 个 undefined name 需 LLM 介入"
            )

        return auto_fixed, unresolved

    # ── 检测 ─────────────────────────────────────────────────────

    def _detect_undefined_names(
        self, abs_path: Path, rel_path: Path,
    ) -> list[tuple[str, int]]:
        """使用 pyflakes 检测文件中的 undefined names。返回 [(name, lineno)]。"""
        try:
            from pyflakes.api import check as pyflakes_check
        except ImportError:
            return []

        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

        try:
            ast.parse(source)
        except SyntaxError:
            return []

        import io
        stream = io.StringIO()
        try:
            pyflakes_check(source, str(rel_path), stream)
        except Exception:
            return []

        output = stream.getvalue()
        if not output:
            return []

        results: list[tuple[str, int]] = []
        for line in output.splitlines():
            m = re.match(r'.+:(\d+):\d+\s+.*undefined name\s+[\'"](\w+)[\'"]', line)
            if m:
                lineno = int(m.group(1))
                name = m.group(2)
                # 跳过已注释行
                src_lines = source.splitlines()
                if 0 < lineno <= len(src_lines):
                    src_line = src_lines[lineno - 1].strip()
                    if src_line.startswith("#") or "[CodePrune]" in src_line:
                        continue
                results.append((name, lineno))

        return results

    # ── 分类 ─────────────────────────────────────────────────────

    def _classify(self, name: str, file_path: Path) -> str:
        """对 undefined name 进行 4 级分类"""
        import builtins as _builtins

        # 1. Python builtins → ignore
        if hasattr(_builtins, name):
            return UndefinedNameClassification.IGNORE

        # 2. 常见 typing 名称 → ignore
        _TYPING_NAMES = {
            "Optional", "List", "Dict", "Set", "Tuple", "Any",
            "Union", "Callable", "Type", "TypeVar", "Generic",
            "Protocol", "ClassVar", "Final", "Literal",
            "Annotated", "Self", "Never", "TypeAlias",
            "Sequence", "Mapping", "Iterable", "Iterator",
            "Generator", "Coroutine", "Awaitable", "AsyncIterator",
        }
        if name in _TYPING_NAMES:
            return UndefinedNameClassification.IGNORE

        # 3. 裁剪导致的已知移除 — 但仍需检查是否在代码中被使用
        #    如果名称被 ImportFixer 移除但 CascadeCleaner 无法清理引用行，
        #    说明该名称仍被需要，应尝试修复而非跳过
        if name in self._removed_names:
            # 检查名称是否仍在代码中被使用（排除注释行/import 行）
            abs_path = self.sub_repo / file_path
            if self._is_name_still_used(abs_path, name):
                # 名称仍在使用 → 尝试从其他模块修复
                logger.debug(
                    f"  {file_path}: '{name}' 是裁剪移除的名称但仍在代码中被使用，尝试修复"
                )
                # 继续走 fixable 检查流程（不 return PRUNE_EXPECTED）
            else:
                return UndefinedNameClassification.PRUNE_EXPECTED

        # 4. 名称在子仓库其他模块的导出中 → fixable
        for module, exports in self._module_exports.items():
            file_module = str(file_path.with_suffix("")).replace("\\", "/").replace("/", ".")
            if file_module.endswith(".__init__"):
                file_module = file_module[:-9]
            # 不自动 import 自己的模块
            if module == file_module:
                continue
            if name in exports:
                return UndefinedNameClassification.FIXABLE

        # 5. 名称在 CodeGraph 中有定义 → fixable (如果子仓库文件存在)
        if name in self._graph_name_index:
            candidates = self._graph_name_index[name]
            for module_path, node in candidates:
                # 检查定义文件是否存在于子仓库
                if node.file_path:
                    sub_file = self.sub_repo / node.file_path
                    if sub_file.exists():
                        return UndefinedNameClassification.FIXABLE

        # 6. 其他 → llm_required
        return UndefinedNameClassification.LLM_REQUIRED

    # ── 自动补 Import ────────────────────────────────────────────

    def _is_name_still_used(self, abs_path: Path, name: str) -> bool:
        """检查名称是否仍在文件的非 import / 非注释代码中被引用"""
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            return False

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, ast.Name) and node.id == name:
                # 确认不在注释行（AST 节点不会出现在注释中，所以这里天然排除）
                return True
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == name:
                    return True
        return False

    def _auto_add_import(self, abs_path: Path, rel_path: Path, name: str) -> bool:
        """在文件头添加 import 语句。仅无歧义时执行。"""
        # 查找定义位置: 优先子仓库导出，再查 CodeGraph
        target_module = self._find_unambiguous_source(name, rel_path)
        if not target_module:
            return False

        # 读取文件
        try:
            source = abs_path.read_text(encoding="utf-8")
        except OSError:
            return False

        # 检查是否已有相同 import
        if f"from {target_module} import" in source and name in source.split(f"from {target_module} import")[1].split("\n")[0]:
            return False

        # 找到插入位置: 最后一个 import 语句之后
        lines = source.splitlines(keepends=True)
        insert_idx = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and not stripped.startswith("# [CodePrune]"):
                insert_idx = i + 1
            elif stripped.startswith("# [CodePrune]") and ("import" in stripped or "from" in stripped):
                insert_idx = i + 1

        # 插入 import 语句
        import_line = f"from {target_module} import {name}  # [CodePrune] auto-added\n"
        lines.insert(insert_idx, import_line)

        try:
            abs_path.write_text("".join(lines), encoding="utf-8")
            logger.debug(f"  自动补 import: {rel_path} ← from {target_module} import {name}")
            return True
        except OSError:
            return False

    def _find_unambiguous_source(self, name: str, file_path: Path) -> str | None:
        """查找 name 的唯一定义模块。有歧义时返回 None。"""
        file_module = str(file_path.with_suffix("")).replace("\\", "/").replace("/", ".")
        if file_module.endswith(".__init__"):
            file_module = file_module[:-9]

        # 1. 从子仓库导出中查找
        found_modules: list[str] = []
        for module, exports in self._module_exports.items():
            if module == file_module:
                continue
            if name in exports:
                found_modules.append(module)

        if len(found_modules) == 1:
            return found_modules[0]

        # 2. 从 CodeGraph 中查找 (如果子仓库导出有歧义或未找到)
        if not found_modules and name in self._graph_name_index:
            graph_modules: list[str] = []
            for module_path, node in self._graph_name_index[name]:
                if node.file_path:
                    sub_file = self.sub_repo / node.file_path
                    if sub_file.exists():
                        mod = str(node.file_path.with_suffix("")).replace("\\", "/").replace("/", ".")
                        if mod.endswith(".__init__"):
                            mod = mod[:-9]
                        if mod != file_module:
                            graph_modules.append(mod)
            if len(graph_modules) == 1:
                return graph_modules[0]

        # 歧义或未找到
        return None

    # ── 扫描 / 索引 ─────────────────────────────────────────────

    def _scan_exports(self) -> None:
        """扫描子仓库模块导出"""
        for py_file in self.sub_repo.rglob("*.py"):
            rel = py_file.relative_to(self.sub_repo)
            module_path = str(rel.with_suffix("")).replace("\\", "/").replace("/", ".")
            if module_path.endswith(".__init__"):
                module_path = module_path[:-9]

            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source)
                names: set[str] = set()
                for child in ast.iter_child_nodes(tree):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        names.add(child.name)
                    elif isinstance(child, ast.ClassDef):
                        names.add(child.name)
                    elif isinstance(child, ast.Assign):
                        for target in child.targets:
                            if isinstance(target, ast.Name):
                                names.add(target.id)
                self._module_exports[module_path] = names
            except (OSError, SyntaxError):
                pass

    def _index_graph(self) -> None:
        """从 CodeGraph 构建 name → [(module, node)] 索引"""
        from core.graph.schema import NodeType as NT

        symbol_types = {NT.FUNCTION, NT.CLASS, NT.INTERFACE, NT.ENUM}
        for node in self.graph.nodes.values():
            if node.node_type in symbol_types and node.file_path:
                module = str(node.file_path.with_suffix("")).replace("\\", "/").replace("/", ".")
                if module.endswith(".__init__"):
                    module = module[:-9]
                self._graph_name_index.setdefault(node.name, []).append((module, node))
