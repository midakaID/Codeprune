"""
Phase 2.6: 引用审计与清理
在 ImportFixer + CascadeCleaner 之后运行，检测并修复存活文件中对已删除模块/符号的非 import 引用。

两大能力:
1. ReferenceAuditor  — 扫描存活文件中对已删除符号的悬挂引用（函数调用、配置值、注册表映射等）
2. RegistrySync      — 规则引擎同步 __init__.py、__all__、插件注册表等固定结构
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.graph.schema import CodeGraph, CodeNode, NodeType, Language

logger = logging.getLogger(__name__)

_CODE_EXTS = {".py", ".java", ".js", ".ts", ".c", ".cpp", ".h", ".hpp"}


# ───────────────────── 数据结构 ─────────────────────

@dataclass
class ReferenceIssue:
    """一条悬挂引用"""
    file_path: Path       # 相对于 sub_repo_path
    line: int             # 1-based
    symbol: str           # 被引用的已删除符号名
    context: str          # 该行文本
    kind: str = "usage"   # usage | registry | init_export


@dataclass
class AuditAction:
    """LLM 决策的修复动作"""
    file_path: Path
    line: int
    action: str           # REMOVE | COMMENT | KEEP
    symbol: str


@dataclass
class AuditReport:
    """审计结果"""
    issues: list[ReferenceIssue] = field(default_factory=list)
    actions: list[AuditAction] = field(default_factory=list)
    fixes_applied: int = 0
    registry_fixes: int = 0


# ───────────────────── ReferenceAuditor ─────────────────────

class ReferenceAuditor:
    """扫描存活文件中对已删除模块/符号的非 import 引用"""

    def __init__(
        self,
        sub_repo_path: Path,
        graph: CodeGraph,
        excluded_modules: list[str],
        llm,
        language: Language = Language.UNKNOWN,
    ):
        self.sub_repo_path = sub_repo_path
        self.graph = graph
        self.excluded_modules = excluded_modules
        self.llm = llm
        self.language = language
        self.deleted_symbols: dict[str, str] = {}  # symbol_name → source_file

    def audit_and_fix(self) -> AuditReport:
        """完整流程：收集符号 → 扫描引用 → LLM 决策 → 应用修复"""
        report = AuditReport()

        # Step 0: 规则引擎处理 __init__.py / 注册表
        syncer = RegistrySync(self.sub_repo_path, self.excluded_modules)
        report.registry_fixes = syncer.sync()

        # Step 1: 收集已删除符号
        self._collect_deleted_symbols()
        if not self.deleted_symbols:
            logger.debug("引用审计: 无已删除符号需审计")
            return report

        logger.info(f"引用审计: 收集到 {len(self.deleted_symbols)} 个已删除符号")

        # Step 2: 扫描悬挂引用
        report.issues = self._scan_references()
        if not report.issues:
            logger.info("引用审计: 未发现悬挂引用")
            return report

        logger.info(f"引用审计: 发现 {len(report.issues)} 条悬挂引用")

        # Step 3: LLM 批量决策
        report.actions = self._llm_batch_decide(report.issues)

        # Step 4: 应用修复
        report.fixes_applied = self._apply_actions(report.actions)
        logger.info(
            f"引用审计: 应用 {report.fixes_applied} 处修复"
            f"（注册表同步 {report.registry_fixes} 处）"
        )
        return report

    def _collect_deleted_symbols(self) -> None:
        """从 CodeGraph + excluded_modules 收集所有被删除的公开符号"""
        # 过于通用的名称 — 几乎肯定会误报
        _GENERIC_NAMES = {
            "name", "run", "setup", "init", "start", "stop", "close",
            "get", "set", "add", "remove", "delete", "update", "create",
            "register", "execute", "process", "handle", "call",
            "read", "write", "load", "save", "parse", "format",
            "validate", "check", "test", "main", "help",
            "teardown", "reset", "clear", "open", "send",
            "__init__", "__str__", "__repr__", "__eq__", "__hash__",
            "__enter__", "__exit__", "__len__", "__iter__", "__next__",
            "__getitem__", "__setitem__", "__contains__",
        }

        # 方法1: 遍历 CodeGraph 中所有节点，找出文件不在子仓库中的符号
        existing_files: set[str] = set()
        for f in self.sub_repo_path.rglob("*"):
            if f.is_file():
                try:
                    existing_files.add(
                        str(f.relative_to(self.sub_repo_path)).replace("\\", "/")
                    )
                except ValueError:
                    pass

        for node in self.graph.nodes.values():
            if node.node_type not in (
                NodeType.FUNCTION, NodeType.CLASS, NodeType.INTERFACE, NodeType.ENUM
            ):
                continue

            if not node.file_path:
                continue

            file_rel = str(node.file_path).replace("\\", "/")

            # 检查文件是否在子仓库中
            if file_rel in existing_files:
                continue

            # 跳过私有符号（不太可能被外部引用）
            if node.name.startswith("_") and not node.name.startswith("__"):
                continue

            # 跳过 dunder 方法和通用名称
            if node.name in _GENERIC_NAMES:
                continue

            # 跳过太短或太通用的名称（容易误匹配）
            if len(node.name) <= 2:
                continue

            # 对函数/方法：只保留足够特殊的名称
            # 类名通常 PascalCase 有高辨识度，方法名需更严格过滤
            if node.node_type == NodeType.FUNCTION:
                # 跳过纯小写的短函数名（<8字符）— 太通用
                if node.name.islower() and len(node.name) < 8:
                    continue
                # 跳过仅含一个单词的函数名（如 validate, to_db）
                if "_" not in node.name and node.name.islower():
                    continue

            self.deleted_symbols[node.name] = file_rel

        # 方法2: 从 excluded_modules 提取模块名作为额外候选
        for ex in self.excluded_modules:
            ex_norm = ex.replace("\\", "/").rstrip("/")
            # 提取模块基名（如 "orchestrator/plugins/cleanup.py" → "cleanup"）
            base = ex_norm.split("/")[-1]
            if "." in base:
                base = base.rsplit(".", 1)[0]
            if base and len(base) > 2 and not base.startswith("_"):
                self.deleted_symbols.setdefault(base, ex_norm)

    def _scan_references(self) -> list[ReferenceIssue]:
        """扫描存活文件中的悬挂引用"""
        issues: list[ReferenceIssue] = []

        # 构建正则：所有 deleted symbol 名的 word-boundary 匹配
        # 分组处理避免正则过长
        symbol_list = sorted(self.deleted_symbols.keys(), key=len, reverse=True)
        if not symbol_list:
            return issues

        # 收集存活文件中的本地定义符号（用于排除误报）
        local_defs_by_file: dict[Path, set[str]] = {}
        for node in self.graph.nodes.values():
            if node.file_path and node.node_type in (
                NodeType.FUNCTION, NodeType.CLASS, NodeType.INTERFACE
            ):
                fp = node.file_path
                local_defs_by_file.setdefault(fp, set()).add(node.name)

        # 按批次处理（每批最多100个符号）
        batch_size = 100
        for batch_start in range(0, len(symbol_list), batch_size):
            batch = symbol_list[batch_start : batch_start + batch_size]
            # 转义+word boundary
            pattern_str = "|".join(re.escape(s) for s in batch)
            pattern = re.compile(rf"\b({pattern_str})\b")

            for code_file in sorted(self.sub_repo_path.rglob("*")):
                if not code_file.is_file() or code_file.suffix not in _CODE_EXTS:
                    continue
                try:
                    content = code_file.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue

                rel_path = code_file.relative_to(self.sub_repo_path)

                # 本文件中定义的符号 — 排除同名误报
                local_defs = local_defs_by_file.get(rel_path, set())

                for line_idx, line in enumerate(content.splitlines()):
                    stripped = line.strip()

                    # 跳过 import 行（已由 ImportFixer 处理）
                    if self._is_import_line(stripped, code_file.suffix):
                        continue

                    # 跳过注释行
                    if self._is_comment_line(stripped, code_file.suffix):
                        continue

                    # 跳过已被 CodePrune 处理的行
                    if "[CodePrune]" in stripped:
                        continue

                    # 跳过 pruned 标记行
                    if re.match(r"^(?:#|//)\s*\.\.\.\s*pruned\s+\d+\s+lines", stripped):
                        continue

                    matches = pattern.findall(line)
                    for sym in matches:
                        # 排除在当前文件中也有定义的符号名
                        if sym in local_defs:
                            continue

                        # 排除出现在函数定义/参数位置的名称
                        # def xxx(app: "App") / def on_request(self...)
                        if re.match(
                            rf"^\s*def\s+{re.escape(sym)}\s*\(", stripped
                        ):
                            continue
                        # 参数名: (app: ...) / (app, ...) / self.app
                        if re.match(
                            rf".*[\(,]\s*{re.escape(sym)}\s*[:=,\)]", stripped
                        ):
                            # 看起来像参数/变量赋值，跳过
                            continue

                        issues.append(ReferenceIssue(
                            file_path=rel_path,
                            line=line_idx + 1,
                            symbol=sym,
                            context=stripped[:200],
                        ))

        return issues

    @staticmethod
    def _is_import_line(stripped: str, suffix: str) -> bool:
        """判断是否为 import/include 行"""
        if suffix == ".py":
            return stripped.startswith(("import ", "from "))
        elif suffix == ".java":
            return stripped.startswith(("import ", "package "))
        elif suffix in (".ts", ".js"):
            return bool(
                re.match(r"^(import |export .* from |const .* = require\()", stripped)
            )
        elif suffix in (".c", ".cpp", ".h", ".hpp"):
            return stripped.startswith("#include")
        return False

    @staticmethod
    def _is_comment_line(stripped: str, suffix: str) -> bool:
        """判断是否为纯注释行"""
        if suffix == ".py":
            return stripped.startswith("#")
        elif suffix in (".java", ".ts", ".js", ".c", ".cpp", ".h", ".hpp"):
            return stripped.startswith("//") or stripped.startswith("/*")
        return False

    def _llm_batch_decide(self, issues: list[ReferenceIssue]) -> list[AuditAction]:
        """按文件分组让 LLM 批量决策修复动作"""
        from collections import defaultdict

        actions: list[AuditAction] = []

        # 按文件分组
        by_file: dict[Path, list[ReferenceIssue]] = defaultdict(list)
        for issue in issues:
            by_file[issue.file_path].append(issue)

        for file_path, file_issues in by_file.items():
            # 去重同一行同一符号
            seen = set()
            deduped = []
            for iss in file_issues:
                key = (iss.line, iss.symbol)
                if key not in seen:
                    seen.add(key)
                    deduped.append(iss)

            if not deduped:
                continue

            # 读取文件内容供 LLM 参考
            full_path = self.sub_repo_path / file_path
            try:
                file_content = full_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            # 构建 issue 描述
            issue_lines = []
            for i, iss in enumerate(deduped, 1):
                source = self.deleted_symbols.get(iss.symbol, "unknown")
                issue_lines.append(
                    f"  {i}. Line {iss.line}: `{iss.symbol}` "
                    f"(from deleted file: {source})\n"
                    f"     Code: {iss.context}"
                )

            prompt = PROMPT_AUDIT_REFERENCES.format(
                file_path=str(file_path),
                issues="\n".join(issue_lines),
                file_content=file_content[:8000],
                issue_count=len(deduped),
            )

            try:
                result = self.llm.chat_json([{"role": "user", "content": prompt}])
                decisions = result.get("decisions", [])
                for dec in decisions:
                    action_str = str(dec.get("action", "KEEP")).upper()
                    if action_str not in ("REMOVE", "COMMENT", "KEEP"):
                        action_str = "KEEP"
                    line_num = int(dec.get("line", 0))
                    symbol = str(dec.get("symbol", ""))
                    if action_str != "KEEP" and line_num > 0:
                        actions.append(AuditAction(
                            file_path=file_path,
                            line=line_num,
                            action=action_str,
                            symbol=symbol,
                        ))
            except Exception as e:
                logger.warning(f"引用审计 LLM 决策失败 ({file_path}): {e}")

        return actions

    def _apply_actions(self, actions: list[AuditAction]) -> int:
        """应用 LLM 决策的修复动作"""
        from collections import defaultdict

        # 按文件分组
        by_file: dict[Path, list[AuditAction]] = defaultdict(list)
        for act in actions:
            by_file[act.file_path].append(act)

        total_fixed = 0

        for file_path, file_actions in by_file.items():
            full_path = self.sub_repo_path / file_path
            try:
                lines = full_path.read_text(encoding="utf-8").splitlines(keepends=True)
            except (OSError, UnicodeDecodeError):
                continue

            # 按行号降序处理，避免行号偏移
            file_actions.sort(key=lambda a: a.line, reverse=True)
            modified = False

            suffix = full_path.suffix
            comment_prefix = "#" if suffix == ".py" else "//"

            for act in file_actions:
                idx = act.line - 1  # 0-based
                if idx < 0 or idx >= len(lines):
                    continue

                original = lines[idx].rstrip("\n\r")

                if act.action == "REMOVE":
                    lines[idx] = ""
                    modified = True
                    total_fixed += 1
                    logger.debug(
                        f"引用审计 REMOVE: {file_path}:{act.line} "
                        f"({act.symbol})"
                    )
                elif act.action == "COMMENT":
                    indent = len(original) - len(original.lstrip())
                    lines[idx] = (
                        " " * indent
                        + f"{comment_prefix} [CodePrune] audit: {original.strip()}\n"
                    )
                    modified = True
                    total_fixed += 1
                    logger.debug(
                        f"引用审计 COMMENT: {file_path}:{act.line} "
                        f"({act.symbol})"
                    )

            if modified:
                # 清理连续空行（REMOVE 后可能留下多个空行）
                cleaned = self._collapse_blank_lines(lines)
                try:
                    full_path.write_text("".join(cleaned), encoding="utf-8")
                except OSError:
                    pass

        return total_fixed

    @staticmethod
    def _collapse_blank_lines(lines: list[str]) -> list[str]:
        """清理连续空行，最多保留2个"""
        result = []
        blank_count = 0
        for line in lines:
            if not line.strip():
                blank_count += 1
                if blank_count <= 2:
                    result.append(line)
            else:
                blank_count = 0
                result.append(line)
        return result


# ───────────────────── RegistrySync ─────────────────────

class RegistrySync:
    """规则引擎同步 __init__.py、__all__、插件注册表"""

    def __init__(self, sub_repo_path: Path, excluded_modules: list[str]):
        self.sub_repo_path = sub_repo_path
        self.excluded_modules = excluded_modules
        self._existing_modules: Optional[set[str]] = None

    @property
    def existing_modules(self) -> set[str]:
        """子仓库中实际存在的模块名"""
        if self._existing_modules is None:
            self._existing_modules = set()
            for f in self.sub_repo_path.rglob("*"):
                if f.is_file() and f.suffix in _CODE_EXTS:
                    # 文件名去扩展名
                    self._existing_modules.add(f.stem)
                elif f.is_dir() and (f / "__init__.py").exists():
                    # Python 包名
                    self._existing_modules.add(f.name)
        return self._existing_modules

    def sync(self) -> int:
        """同步所有注册表/导出文件，返回修复条目数"""
        total = 0
        for f in sorted(self.sub_repo_path.rglob("*")):
            if not f.is_file():
                continue
            if f.suffix == ".py":
                total += self._sync_python_file(f)
            elif f.suffix in (".ts", ".js"):
                total += self._sync_js_ts_barrel(f)
        return total

    def _sync_python_file(self, file_path: Path) -> int:
        """同步 Python __init__.py 和 __all__ 定义"""
        if file_path.name not in ("__init__.py",):
            return 0

        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return 0

        lines = content.splitlines(keepends=True)
        modified = False
        fixes = 0

        new_lines = []
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # 处理 from .xxx import yyy — 检查 .xxx 对应的模块是否存在
            m = re.match(r"^from\s+\.(\w+)", stripped)
            if m:
                module_name = m.group(1)
                # 检查同目录下是否有该模块文件或子包
                parent_dir = file_path.parent
                module_exists = (
                    (parent_dir / f"{module_name}.py").is_file()
                    or (parent_dir / module_name).is_dir()
                )
                if not module_exists:
                    # 模块不存在，注释掉
                    indent = len(line) - len(line.lstrip())
                    new_lines.append(
                        " " * indent
                        + f"# [CodePrune] registry: {stripped}\n"
                    )
                    modified = True
                    fixes += 1
                    i += 1
                    continue

            # 处理 __all__ = [...] — 移除不存在的名称
            if "__all__" in stripped and "=" in stripped:
                # 尝试提取完整的 __all__ 定义（可能跨多行）
                all_text = line
                j = i + 1
                bracket_depth = all_text.count("[") - all_text.count("]")
                while bracket_depth > 0 and j < len(lines):
                    all_text += lines[j]
                    bracket_depth += lines[j].count("[") - lines[j].count("]")
                    j += 1

                # 解析 __all__ 中的名称
                names_match = re.findall(r"""['"]([\w]+)['"]""", all_text)
                if names_match:
                    surviving = [
                        n for n in names_match
                        if self._name_exists_in_scope(n, file_path.parent)
                    ]
                    if len(surviving) < len(names_match):
                        # 重写 __all__
                        indent = len(line) - len(line.lstrip())
                        if surviving:
                            items = ", ".join(f'"{n}"' for n in surviving)
                            new_lines.append(f"{' ' * indent}__all__ = [{items}]\n")
                        else:
                            new_lines.append(f"{' ' * indent}__all__ = []\n")
                        modified = True
                        fixes += len(names_match) - len(surviving)
                        i = j  # 跳过多行
                        continue

            new_lines.append(line)
            i += 1

        if modified:
            try:
                file_path.write_text("".join(new_lines), encoding="utf-8")
                rel = file_path.relative_to(self.sub_repo_path)
                logger.info(f"RegistrySync: {rel} — {fixes} 处修复")
            except OSError:
                return 0

        return fixes

    def _name_exists_in_scope(self, name: str, parent_dir: Path) -> bool:
        """检查名称对应的模块/类/函数是否在作用域内存在"""
        # 检查文件级：同目录下有 name.py 或 name/ 包
        if (parent_dir / f"{name}.py").is_file():
            return True
        if (parent_dir / name).is_dir():
            return True

        # 检查符号级：在同目录的 .py 文件中定义了该名称
        for py_file in parent_dir.glob("*.py"):
            if py_file.name == "__init__.py":
                continue
            try:
                content = py_file.read_text(encoding="utf-8")
                if re.search(
                    rf"^(?:class|def)\s+{re.escape(name)}\b", content, re.MULTILINE
                ):
                    return True
            except (OSError, UnicodeDecodeError):
                pass

        return False

    def _sync_js_ts_barrel(self, file_path: Path) -> int:
        """同步 TS/JS barrel 文件 (index.ts) 的 re-export"""
        if file_path.stem != "index":
            return 0

        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return 0

        lines = content.splitlines(keepends=True)
        modified = False
        fixes = 0
        new_lines = []

        parent_dir = file_path.parent

        for line in lines:
            stripped = line.strip()
            # export { ... } from './xxx'  或 export * from './xxx'
            m = re.search(r"""(?:from|require\()\s*['"](\.[^'"]+)['"]""", stripped)
            if m:
                rel_import = m.group(1)
                # 解析相对路径
                target = rel_import.lstrip("./")
                # 检查目标文件是否存在
                candidates = [
                    parent_dir / target,
                    parent_dir / f"{target}.ts",
                    parent_dir / f"{target}.js",
                    parent_dir / target / "index.ts",
                    parent_dir / target / "index.js",
                ]
                if not any(c.is_file() for c in candidates):
                    indent = len(line) - len(line.lstrip())
                    new_lines.append(
                        " " * indent
                        + f"// [CodePrune] registry: {stripped}\n"
                    )
                    modified = True
                    fixes += 1
                    continue

            new_lines.append(line)

        if modified:
            try:
                file_path.write_text("".join(new_lines), encoding="utf-8")
                rel = file_path.relative_to(self.sub_repo_path)
                logger.info(f"RegistrySync: {rel} — {fixes} 处修复")
            except OSError:
                return 0

        return fixes


# ───────────────────── Prompt 模板 ─────────────────────

PROMPT_AUDIT_REFERENCES = """You are analyzing a pruned code repository. Some files were intentionally deleted.
The surviving files still contain references to symbols from those deleted files.

For each reference below, decide the minimal fix action:
- REMOVE: delete the entire line (when the line is purely about the deleted feature)
- COMMENT: comment out the line (when the line is part of a larger structure like a dict/list)
- KEEP: leave unchanged (false positive — the symbol name is coincidental or used in a different context)

File: {file_path}

Dangling references ({issue_count} total):
{issues}

File content (for context):
```
{file_content}
```

CRITICAL RULES:
1. Be CONSERVATIVE — only REMOVE/COMMENT when you are confident the reference is to a deleted module
2. If the symbol appears in a string literal as documentation/logging, prefer KEEP
3. If the symbol is a common word that could have other meanings, prefer KEEP
4. If removing a line would break syntax (e.g., middle of a function call), prefer COMMENT
5. For dict entries like `"key": "deleted_module"`, prefer REMOVE the entire entry
6. For list elements that reference deleted items, prefer REMOVE the element

Respond in JSON:
{{"decisions": [{{"line": <line_number>, "symbol": "<symbol_name>", "action": "REMOVE|COMMENT|KEEP", "reason": "<brief reason>"}}]}}
"""
