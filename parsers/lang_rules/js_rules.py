"""
JavaScript / TypeScript 语言特化规则
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from core.graph.schema import CodeGraph, Language
from parsers.lang_rules.base import LanguageRule, LangWarning


class JSRules(LanguageRule):
    """JS 和 TS 共用规则，TS 的类型导入差异在 closure/surgeon 已特化处理。"""

    @property
    def language(self) -> Language:
        return Language.JAVASCRIPT  # JS/TS 共用

    @property
    def import_line_pattern(self) -> Optional[re.Pattern]:
        return re.compile(
            r"^\s*(?:import\s|export\s.*from\s|const\s+\w+\s*=\s*require\()"
        )

    @property
    def decorator_prefixes(self) -> tuple[str, ...]:
        return ("@",)

    @property
    def constructor_names(self) -> Optional[tuple[str, ...]]:
        return ("constructor",)

    @property
    def build_config_patterns(self) -> list[str]:
        return ["package.json", "tsconfig.json", "jsconfig.json"]

    def import_header_keywords(self) -> tuple[str, ...]:
        return ("import ", "require(", "export ")

    def post_surgery_fixup(self, output_path: Path) -> list[LangWarning]:
        """
        检查 TS/JS 文件中的相对导入路径是否指向实际存在的文件。
        """
        warnings: list[LangWarning] = []
        rel_import_re = re.compile(
            r"""(?:from|require\()\s*['"](\.[^'"]+)['"]"""
        )
        extensions = ("*.js", "*.ts", "*.jsx", "*.tsx", "*.mjs", "*.cjs")

        for ext in extensions:
            for js_file in output_path.rglob(ext):
                try:
                    text = js_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue

                for lineno, line in enumerate(text.splitlines(), 1):
                    m = rel_import_re.search(line)
                    if not m:
                        continue
                    target = m.group(1)
                    parent = js_file.parent
                    # 尝试解析（带/不带扩展名）
                    candidates = [
                        parent / target,
                        parent / (target + ".ts"),
                        parent / (target + ".js"),
                        parent / (target + ".tsx"),
                        parent / (target + ".jsx"),
                        parent / target / "index.ts",
                        parent / target / "index.js",
                    ]
                    if not any(c.exists() for c in candidates):
                        warnings.append(LangWarning(
                            file_path=js_file.relative_to(output_path),
                            line=lineno,
                            message=f"相对导入 '{target}' 目标文件不存在",
                        ))
        return warnings
