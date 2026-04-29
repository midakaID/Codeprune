"""
语言规则引擎 — 抽象基类
每种语言实现一个 LanguageRule 子类，提供跨阶段的语言特化逻辑。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.graph.schema import CodeGraph, CodeNode, Language, NodeType


@dataclass
class LangWarning:
    """语言规则产生的警告"""
    file_path: Path
    line: int
    message: str
    severity: str = "warning"


class LanguageRule(ABC):
    """语言规则抽象基类"""

    @property
    @abstractmethod
    def language(self) -> Language:
        """该规则适用的语言"""
        ...

    @property
    @abstractmethod
    def import_line_pattern(self) -> Optional[re.Pattern]:
        """匹配 import 行的正则（供 surgeon 使用）"""
        ...

    @property
    @abstractmethod
    def decorator_prefixes(self) -> tuple[str, ...]:
        """装饰器/注解起始字符（供 surgeon 向上扩展）"""
        ...

    @property
    @abstractmethod
    def constructor_names(self) -> Optional[tuple[str, ...]]:
        """
        构造方法名列表。
        None = 与类同名（Java/C++），() = 无构造方法（C）
        """
        ...

    @property
    def header_source_pairs(self) -> dict[str, tuple[str, ...]]:
        """头文件↔源文件后缀映射，仅 C/C++ 使用"""
        return {}

    @property
    def build_config_patterns(self) -> list[str]:
        """该语言的构建配置文件名模式"""
        return []

    # ── Phase1 钩子 ──

    def post_build_validate(
        self, graph: CodeGraph, file_path: Path,
    ) -> list[LangWarning]:
        """
        Phase1 构建图谱后的语言级验证。
        例如：Python 检查 __all__ 与实际导出是否一致。
        默认无额外验证。
        """
        return []

    # ── Phase2 钩子 ──

    def adjust_closure(
        self, required_nodes: set[str], graph: CodeGraph,
    ) -> set[str]:
        """
        Phase2 闭包求解后的语言级调整。
        例如：Python 自动包含 __init__.py（已在 closure.py 中实现，
        此处提供扩展点供未来语言特化）。
        默认不调整。
        """
        return required_nodes

    def import_header_keywords(self) -> tuple[str, ...]:
        """文件头部关键词（用于 _detect_header_end）"""
        return ("import ",)

    # ── Phase3 钩子 ──

    def post_surgery_fixup(self, output_path: Path) -> list[LangWarning]:
        """
        Phase2 手术后的语言级 fixup。
        例如：Java 检查 package 声明与目录结构是否匹配。
        默认无 fixup。
        """
        return []

    def get_compile_command(self, sub_repo_path: Path) -> Optional[list[str]]:
        """
        返回该语言的编译验证命令。
        返回 None 表示不支持编译验证。
        """
        return None


def get_language_rules(language: Language) -> Optional[LanguageRule]:
    """工厂方法：获取指定语言的规则实例"""
    from parsers.lang_rules.python_rules import PythonRules
    from parsers.lang_rules.java_rules import JavaRules
    from parsers.lang_rules.js_rules import JSRules
    from parsers.lang_rules.c_rules import CRules

    _REGISTRY: dict[Language, type[LanguageRule]] = {
        Language.PYTHON: PythonRules,
        Language.JAVA: JavaRules,
        Language.JAVASCRIPT: JSRules,
        Language.TYPESCRIPT: JSRules,
        Language.C: CRules,
        Language.CPP: CRules,
    }
    cls = _REGISTRY.get(language)
    return cls() if cls else None
