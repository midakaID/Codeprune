"""
C / C++ 语言特化规则
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from core.graph.schema import CodeGraph, Language
from parsers.lang_rules.base import LanguageRule, LangWarning


class CRules(LanguageRule):
    """C 和 C++ 共用规则。"""

    @property
    def language(self) -> Language:
        return Language.C  # C/C++ 共用

    @property
    def import_line_pattern(self) -> Optional[re.Pattern]:
        return re.compile(r'^\s*#\s*include\s+[<"]')

    @property
    def decorator_prefixes(self) -> tuple[str, ...]:
        return ()  # C/C++ 无装饰器

    @property
    def constructor_names(self) -> Optional[tuple[str, ...]]:
        return ()  # C 无构造方法，C++ 与类同名在 surgeon 中单独处理

    @property
    def header_source_pairs(self) -> dict[str, tuple[str, ...]]:
        return {
            ".h": (".c", ".cpp", ".cc", ".cxx"),
            ".hpp": (".cpp", ".cc", ".cxx"),
            ".hxx": (".cpp", ".cc", ".cxx"),
        }

    @property
    def build_config_patterns(self) -> list[str]:
        return ["Makefile", "CMakeLists.txt", "meson.build", "configure.ac"]

    def import_header_keywords(self) -> tuple[str, ...]:
        return ("#include ",)

    def post_surgery_fixup(self, output_path: Path) -> list[LangWarning]:
        """
        检查 #include 的头文件是否存在于输出目录中（仅检查引号包含）。
        """
        warnings: list[LangWarning] = []
        include_re = re.compile(r'^\s*#\s*include\s+"([^"]+)"')
        c_exts = ("*.c", "*.cpp", "*.cc", "*.cxx", "*.h", "*.hpp", "*.hxx")

        # 收集输出目录中所有包含头文件的目录，支持 include/xxx.h 等布局
        header_dirs: set[Path] = set()
        for hext in ("*.h", "*.hpp", "*.hxx"):
            for hf in output_path.rglob(hext):
                header_dirs.add(hf.parent)

        for ext in c_exts:
            for src_file in output_path.rglob(ext):
                try:
                    text = src_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue

                for lineno, line in enumerate(text.splitlines(), 1):
                    m = include_re.match(line)
                    if not m:
                        continue
                    header = m.group(1)
                    # 搜索：当前目录 → 输出根 → 所有头文件目录
                    candidates = [
                        src_file.parent / header,
                        output_path / header,
                    ] + [d / header for d in header_dirs]
                    if not any(c.exists() for c in candidates):
                        warnings.append(LangWarning(
                            file_path=src_file.relative_to(output_path),
                            line=lineno,
                            message=f'#include "{header}" 头文件不存在',
                        ))
        return warnings

    def get_compile_command(self, sub_repo_path: Path) -> Optional[list[str]]:
        """
        如果存在 Makefile / CMakeLists.txt，返回对应编译命令。
        """
        if (sub_repo_path / "CMakeLists.txt").exists():
            build_dir = sub_repo_path / "build"
            return [
                "cmake", "-S", str(sub_repo_path),
                "-B", str(build_dir), "&&",
                "cmake", "--build", str(build_dir),
            ]
        if (sub_repo_path / "Makefile").exists():
            return ["make", "-C", str(sub_repo_path), "-j4"]
        return None
