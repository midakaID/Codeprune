"""
SourceRecovery — 统一的原仓库代码恢复器

三层恢复粒度:
  1. 文件级: recover_file() — 整文件从原仓库复制
  2. 符号级: recover_symbol() — CodeGraph 精确定位 + byte_range 提取
  3. 语句级: recover_commented_lines() — 扫描 [CodePrune] audit 标记, 智能恢复

在修复流程中的位置: Phase 0.5, 在 LLM 和 Dispatcher 之前执行
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Optional

from core.graph.schema import CodeGraph, CodeNode, NodeType

logger = logging.getLogger(__name__)

# 错误消息 → 恢复策略的匹配模式
_IMPLICIT_DECL = re.compile(
    r"implicit declaration of function '(\w+)'", re.IGNORECASE
)
_UNKNOWN_TYPE = re.compile(
    r"unknown type name '(\w+)'", re.IGNORECASE
)
_UNDECLARED_ID = re.compile(
    r"(?:undeclared identifier|use of undeclared identifier) '(\w+)'", re.IGNORECASE
)
_NO_MODULE = re.compile(
    r"No module named '([^']+)'", re.IGNORECASE
)
_CANNOT_IMPORT = re.compile(
    r"cannot import name '(\w+)' from '([^']+)'", re.IGNORECASE
)
_CANNOT_FIND_SYMBOL = re.compile(
    r"cannot find symbol.*?symbol:\s+(?:method|variable|class)\s+(\w+)", re.IGNORECASE | re.DOTALL
)

# [CodePrune] audit 标记正则
_AUDIT_COMMENT = re.compile(
    r'^(\s*)(//|#)\s*\[CodePrune\]\s*audit:\s*(.+)$'
)


class SourceRecovery:
    """统一的原仓库代码恢复器"""

    def __init__(
        self,
        repo_path: Path,
        sub_repo_path: Path,
        graph: CodeGraph,
    ):
        self.repo_path = repo_path
        self.sub_repo_path = sub_repo_path
        self.graph = graph
        self._recovered_symbols: set[str] = set()  # 去重

    # ── 文件级恢复 ──

    def recover_file(self, rel_path: str) -> bool:
        """文件级：整文件从原仓库复制"""
        src = self.repo_path / rel_path
        dst = self.sub_repo_path / rel_path
        if not src.exists():
            return False
        if dst.exists():
            return False  # 已存在不覆盖
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        logger.info(f"SourceRecovery: 文件恢复 {rel_path}")
        return True

    # ── 符号级恢复 ──

    def recover_symbol(self, symbol_name: str, target_file: Path) -> bool:
        """符号级：从原仓库提取 function/class/struct/macro/typedef 的完整定义

        使用 CodeGraph byte_range 精确提取，然后追加到目标文件。
        """
        if symbol_name in self._recovered_symbols:
            return False

        # 在 CodeGraph 中查找符号定义
        node = self._find_symbol_node(symbol_name)
        if not node or not node.byte_range or not node.file_path:
            return False

        # 从原仓库文件中精确提取
        src_file = self.repo_path / node.file_path
        if not src_file.exists():
            return False

        try:
            src_content = src_file.read_text(encoding="utf-8", errors="replace")
            src_lines = src_content.splitlines(keepends=True)
        except OSError:
            return False

        br = node.byte_range
        if br.start_line < 1 or br.end_line > len(src_lines):
            return False

        snippet = "".join(src_lines[br.start_line - 1 : br.end_line])
        if not snippet.strip():
            return False

        # 检查目标文件中是否已存在该符号
        target_path = self.sub_repo_path / target_file
        if not target_path.exists():
            # 如果目标文件不存在，检查原仓库中同文件是否是符号来源
            if node.file_path == target_file:
                return self.recover_file(str(target_file))
            return False

        try:
            dst_content = target_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False

        if self._symbol_exists_in_content(symbol_name, dst_content, target_path.suffix):
            return False

        # 追加符号定义
        with open(target_path, "a", encoding="utf-8") as f:
            f.write(f"\n\n{snippet}")

        self._recovered_symbols.add(symbol_name)
        logger.info(
            f"SourceRecovery: 符号恢复 {symbol_name} "
            f"({node.file_path} L{br.start_line}-{br.end_line}) → {target_file}"
        )
        return True

    # ── 语句级恢复 ──

    def recover_commented_lines(self, file_path: Path) -> int:
        """扫描 [CodePrune] audit 注释标记，验证被注释的代码是否应该恢复

        判断标准:
        1. 被注释行引用的符号，如果在子仓库中实际存在 → 恢复
        2. 被注释行引用的符号，如果在子仓库中不存在 → 保持注释
        """
        abs_path = self.sub_repo_path / file_path
        if not abs_path.exists():
            return 0

        try:
            lines = abs_path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            return 0

        # 收集子仓库中可用的符号集合（用于判断引用是否可解析）
        available_symbols = self._collect_available_symbols()

        restored = 0
        modified = False
        for i, line in enumerate(lines):
            m = _AUDIT_COMMENT.match(line.rstrip("\n\r"))
            if not m:
                continue

            indent = m.group(1)
            original_code = m.group(3).strip()

            # 提取原始代码中引用的标识符
            referenced = self._extract_identifiers(original_code)
            # 只检查长标识符（≥4字符），短名视为局部变量/参数
            significant_refs = {s for s in referenced if len(s) >= 4}

            # 如果所有重要引用的符号都在子仓库中可用 → 恢复
            if not significant_refs or all(
                sym in available_symbols for sym in significant_refs
            ):
                lines[i] = f"{indent}{original_code}\n"
                restored += 1
                modified = True
                logger.debug(
                    f"SourceRecovery: 恢复被注释行 {file_path}:{i + 1} — {original_code[:60]}"
                )

        if modified:
            abs_path.write_text("".join(lines), encoding="utf-8")
            logger.info(
                f"SourceRecovery: {file_path} 恢复 {restored} 行 audit 注释"
            )

        return restored

    # ── 错误驱动入口 ──

    def try_recover_from_error(self, error_message: str, error_file: Path) -> bool:
        """根据编译错误消息，尝试确定性恢复

        返回 True 表示恢复成功（跳过 LLM），False 表示需要 LLM 处理。
        """
        # C: implicit declaration of function 'X'
        m = _IMPLICIT_DECL.search(error_message)
        if m:
            return self._recover_c_declaration(m.group(1), error_file)

        # C: unknown type name 'X'
        m = _UNKNOWN_TYPE.search(error_message)
        if m:
            return self._recover_c_declaration(m.group(1), error_file)

        # C: undeclared identifier 'X'
        m = _UNDECLARED_ID.search(error_message)
        if m:
            return self._recover_c_declaration(m.group(1), error_file)

        # Python: No module named 'X'
        m = _NO_MODULE.search(error_message)
        if m:
            module = m.group(1)
            rel = module.replace(".", "/")
            return (
                self.recover_file(f"{rel}.py")
                or self.recover_file(f"{rel}/__init__.py")
            )

        # Python: cannot import name 'X' from 'Y'
        m = _CANNOT_IMPORT.search(error_message)
        if m:
            symbol_name, module = m.group(1), m.group(2)
            target = Path(module.replace(".", "/") + ".py")
            return self.recover_symbol(symbol_name, target)

        # Java: cannot find symbol
        m = _CANNOT_FIND_SYMBOL.search(error_message)
        if m:
            return self.recover_symbol(m.group(1), error_file)

        return False

    # ── 私有方法 ──

    def _recover_c_declaration(self, symbol_name: str, error_file: Path) -> bool:
        """恢复 C/C++ 符号 — 先尝试在错误文件对应的头文件中查找，再全局搜索"""
        # 首先在 include 链中查找
        node = self._find_symbol_node(symbol_name)
        if not node or not node.file_path:
            return False

        target_file = node.file_path

        # 如果符号在头文件中且头文件不在子仓库中 → 恢复整个头文件
        if str(target_file).endswith((".h", ".hpp")):
            target_path = self.sub_repo_path / target_file
            if not target_path.exists():
                return self.recover_file(str(target_file))

        # 符号来源文件已存在 → 符号级恢复到来源文件
        return self.recover_symbol(symbol_name, target_file)

    def _find_symbol_node(self, symbol_name: str) -> Optional[CodeNode]:
        """在 CodeGraph 中查找符号的定义节点，优先精确匹配"""
        best: Optional[CodeNode] = None
        for node in self.graph.nodes.values():
            if not node.is_physical:
                continue
            if node.name == symbol_name:
                # 精确匹配
                if node.node_type in (NodeType.FUNCTION, NodeType.CLASS):
                    return node  # 立即返回函数/类精确匹配
                if best is None:
                    best = node
        return best

    def _symbol_exists_in_content(
        self, symbol_name: str, content: str, suffix: str,
    ) -> bool:
        """检查符号是否已存在于目标文件内容中"""
        if suffix in (".c", ".h", ".cpp", ".hpp"):
            # C/C++: 检查函数/struct/typedef/enum/macro 定义
            patterns = [
                rf'\b{re.escape(symbol_name)}\s*\(',  # function call/def
                rf'\bstruct\s+{re.escape(symbol_name)}\b',
                rf'\btypedef\b.*\b{re.escape(symbol_name)}\b',
                rf'\benum\s+{re.escape(symbol_name)}\b',
                rf'#define\s+{re.escape(symbol_name)}\b',
            ]
        elif suffix == ".py":
            patterns = [
                rf'\bdef\s+{re.escape(symbol_name)}\b',
                rf'\bclass\s+{re.escape(symbol_name)}\b',
                rf'^{re.escape(symbol_name)}\s*=',
            ]
        elif suffix == ".java":
            patterns = [
                rf'\b(?:public|private|protected)?\s*(?:static\s+)?(?:class|interface|enum)\s+{re.escape(symbol_name)}\b',
                rf'\b\w+\s+{re.escape(symbol_name)}\s*\(',
            ]
        else:
            patterns = [rf'\b{re.escape(symbol_name)}\b']

        return any(
            re.search(p, content, re.MULTILINE)
            for p in patterns
        )

    def _collect_available_symbols(self) -> set[str]:
        """收集子仓库中所有可用的符号名"""
        symbols: set[str] = set()
        for f in self.sub_repo_path.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix not in (".py", ".c", ".h", ".cpp", ".hpp", ".java", ".js", ".ts"):
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # 简单提取: 函数/类/struct/enum/typedef/#define
            for m in re.finditer(
                r'\b(?:def|class|struct|enum|typedef|interface)\s+(\w+)'
                r'|#define\s+(\w+)'
                r'|^[\w][\w\s*]+?\b(\w+)\s*\(',
                content,
                re.MULTILINE,
            ):
                name = m.group(1) or m.group(2) or m.group(3)
                if name:
                    symbols.add(name)
        # 也从 CodeGraph 节点获取（覆盖物理层定义）
        for node in self.graph.nodes.values():
            if node.is_physical and node.file_path:
                sub_path = self.sub_repo_path / node.file_path
                if sub_path.exists():
                    symbols.add(node.name)
        return symbols

    @staticmethod
    def _extract_identifiers(code: str) -> set[str]:
        """从代码行中提取所有标识符（用于判断引用是否可解析）"""
        # 排除关键字
        keywords = {
            "if", "else", "for", "while", "return", "break", "continue",
            "int", "char", "void", "float", "double", "long", "short",
            "unsigned", "signed", "const", "static", "extern", "struct",
            "enum", "typedef", "sizeof", "NULL", "nullptr", "true", "false",
            "def", "class", "import", "from", "pass", "None", "True", "False",
            "include", "define", "ifdef", "ifndef", "endif", "pragma",
        }
        identifiers = set(re.findall(r'\b([a-zA-Z_]\w+)\b', code))
        return identifiers - keywords
