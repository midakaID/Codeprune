"""
Java 语言特化规则
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from core.graph.schema import CodeGraph, Language
from parsers.lang_rules.base import LanguageRule, LangWarning


class JavaRules(LanguageRule):

    @property
    def language(self) -> Language:
        return Language.JAVA

    @property
    def import_line_pattern(self) -> Optional[re.Pattern]:
        return re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)")

    @property
    def decorator_prefixes(self) -> tuple[str, ...]:
        return ("@",)

    @property
    def constructor_names(self) -> Optional[tuple[str, ...]]:
        return None  # 与类同名

    @property
    def build_config_patterns(self) -> list[str]:
        return ["pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle"]

    def import_header_keywords(self) -> tuple[str, ...]:
        return ("import ", "package ")

    def post_surgery_fixup(self, output_path: Path) -> list[LangWarning]:
        """
        检查 Java 文件的 package 声明与其目录结构是否匹配。
        例如 package com.example.service → 文件应在 com/example/service/ 下。
        """
        warnings: list[LangWarning] = []
        package_re = re.compile(r"^\s*package\s+([\w.]+)\s*;")

        for java_file in output_path.rglob("*.java"):
            rel = java_file.relative_to(output_path)
            try:
                with open(java_file, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        m = package_re.match(line)
                        if m:
                            pkg = m.group(1)
                            expected_dir = Path(pkg.replace(".", "/"))
                            actual_dir = rel.parent
                            # 容忍 src/main/java/ 等前缀
                            actual_str = str(actual_dir).replace("\\", "/")
                            expected_str = str(expected_dir).replace("\\", "/")
                            if not actual_str.endswith(expected_str):
                                warnings.append(LangWarning(
                                    file_path=rel, line=1,
                                    message=f"package '{pkg}' 与目录 '{actual_dir}' 不匹配",
                                ))
                            break
            except OSError:
                continue
        return warnings
