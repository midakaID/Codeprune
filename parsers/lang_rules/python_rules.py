"""
Python 语言特化规则
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from core.graph.schema import CodeGraph, CodeNode, Language, NodeType
from parsers.lang_rules.base import LanguageRule, LangWarning


class PythonRules(LanguageRule):

    @property
    def language(self) -> Language:
        return Language.PYTHON

    @property
    def import_line_pattern(self) -> Optional[re.Pattern]:
        return re.compile(
            r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w., ]+))"
        )

    @property
    def decorator_prefixes(self) -> tuple[str, ...]:
        return ("@",)

    @property
    def constructor_names(self) -> Optional[tuple[str, ...]]:
        return ("__init__",)

    @property
    def build_config_patterns(self) -> list[str]:
        return ["setup.py", "setup.cfg", "pyproject.toml", "requirements.txt"]

    def import_header_keywords(self) -> tuple[str, ...]:
        return ("import ", "from ")

    def post_build_validate(
        self, graph: CodeGraph, file_path: Path,
    ) -> list[LangWarning]:
        """检查 __all__ 与实际导出是否一致"""
        warnings: list[LangWarning] = []
        file_id = f"file:{file_path}"
        node = graph.get_node(file_id)
        if not node:
            return warnings

        dunder_all = node.metadata.get("__all__")
        if dunder_all is None:
            return warnings

        # 收集文件中实际定义的顶层名称
        defined_names: set[str] = set()
        for child_id in node.children:
            child = graph.get_node(child_id)
            if child:
                defined_names.add(child.name)

        for name in dunder_all:
            if name not in defined_names:
                warnings.append(LangWarning(
                    file_path=file_path, line=0,
                    message=f"__all__ 中的 '{name}' 未在文件中定义",
                ))
        return warnings

    def adjust_closure(
        self, required_nodes: set[str], graph: CodeGraph,
    ) -> set[str]:
        """确保 __init__.py 被包含（补充 closure.py 中的逻辑）"""
        # 已在 closure.py 的 _auto_include_init_py 中实现
        # 此处保留扩展点
        return required_nodes

    def post_surgery_fixup(self, output_path: Path) -> list[LangWarning]:
        """检查裁剪后的 __init__.py 是否存在包目录中"""
        warnings: list[LangWarning] = []
        for py_dir in output_path.rglob("*"):
            if not py_dir.is_dir():
                continue
            py_files = list(py_dir.glob("*.py"))
            if py_files and not (py_dir / "__init__.py").exists():
                # 检查是否是一个包目录（父目录有 __init__.py 或是根目录）
                parent_has_init = (py_dir.parent / "__init__.py").exists()
                if parent_has_init:
                    warnings.append(LangWarning(
                        file_path=py_dir.relative_to(output_path),
                        line=0,
                        message=f"包目录缺少 __init__.py",
                    ))
        return warnings
