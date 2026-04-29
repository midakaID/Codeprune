"""
Import 路径解析器
将各语言的 import 语句中的模块路径解析为实际文件路径
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from core.graph.schema import Language

logger = logging.getLogger(__name__)


# Python 标准库模块列表（核心部分）
PYTHON_STDLIB = {
    "__future__", "abc", "argparse", "ast", "asyncio", "atexit", "base64",
    "bisect", "builtins", "calendar", "collections", "concurrent", "configparser",
    "contextlib", "copy", "csv", "ctypes", "dataclasses", "datetime",
    "decimal", "difflib", "dis", "email", "enum", "errno", "fcntl",
    "fileinput", "fnmatch", "fractions", "ftplib", "functools", "gc",
    "getpass", "gettext", "glob", "gzip", "hashlib", "heapq", "hmac",
    "html", "http", "importlib", "inspect", "io", "ipaddress",
    "itertools", "json", "keyword", "linecache", "locale", "logging",
    "lzma", "math", "mimetypes", "multiprocessing", "numbers",
    "operator", "os", "pathlib", "pickle", "platform", "pprint",
    "profile", "pstats", "queue", "random", "re", "readline",
    "reprlib", "secrets", "select", "shelve", "shlex", "shutil",
    "signal", "site", "smtplib", "socket", "sqlite3", "ssl",
    "stat", "statistics", "string", "struct", "subprocess", "sys",
    "syslog", "tempfile", "termios", "textwrap", "threading", "time",
    "timeit", "tkinter", "token", "tokenize", "tomllib", "trace",
    "traceback", "tracemalloc", "tty", "turtle", "types", "typing",
    "unicodedata", "unittest", "urllib", "uuid", "venv", "warnings",
    "wave", "weakref", "webbrowser", "wsgiref", "xml", "xmlrpc",
    "zipfile", "zipimport", "zlib", "_thread",
}

# Java 标准库前缀
JAVA_STDLIB_PREFIXES = ("java.", "javax.", "sun.", "jdk.", "com.sun.")


class ImportResolver(ABC):
    """Import 路径解析器抽象基类"""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root

    @abstractmethod
    def resolve(self, module_path: str, source_file: Path) -> Optional[Path]:
        """
        将 import 中的模块路径解析为仓库内的文件路径（相对于 repo_root）
        返回 None 表示外部依赖（标准库/第三方）
        """
        ...

    def is_external(self, module_path: str) -> bool:
        """判断是否为外部依赖"""
        return False


class PythonImportResolver(ImportResolver):
    """Python import 解析器"""

    def resolve(self, module_path: str, source_file: Path) -> Optional[Path]:
        if self.is_external(module_path):
            return None

        # 处理相对导入
        if module_path.startswith("."):
            return self._resolve_relative(module_path, source_file)

        # 绝对导入: models.user → models/user.py 或 models/user/__init__.py
        parts = module_path.split(".")
        return self._find_module_file(parts)

    def is_external(self, module_path: str) -> bool:
        top_module = module_path.lstrip(".").split(".")[0]
        if top_module in PYTHON_STDLIB:
            return True
        # 检查仓库中是否存在对应路径
        parts = top_module.split(".")
        candidate = self.repo_root / "/".join(parts)
        return not (candidate.exists() or
                    candidate.with_suffix(".py").exists() or
                    (candidate / "__init__.py").exists())

    def _resolve_relative(self, module_path: str, source_file: Path) -> Optional[Path]:
        """解析相对导入"""
        # 计算相对层级
        dots = len(module_path) - len(module_path.lstrip("."))
        remaining = module_path.lstrip(".")

        # 从 source_file 的包目录开始向上
        current = source_file.parent
        for _ in range(dots - 1):
            current = current.parent

        if remaining:
            parts = remaining.split(".")
            return self._find_module_file(parts, base=current)
        else:
            # from . import xxx → 当前包的 __init__.py
            init_file = current / "__init__.py"
            rel = self._try_relative(init_file)
            return rel

    def _find_module_file(self, parts: list[str], base: Path = None) -> Optional[Path]:
        """查找模块对应的文件"""
        base = base or self.repo_root
        path = base / "/".join(parts)

        # 尝试 xxx.py
        py_file = path.with_suffix(".py")
        if py_file.exists():
            return self._try_relative(py_file)

        # 尝试 xxx/__init__.py (包)
        init_file = path / "__init__.py"
        if init_file.exists():
            return self._try_relative(init_file)

        # 尝试 xxx.pyi
        pyi_file = path.with_suffix(".pyi")
        if pyi_file.exists():
            return self._try_relative(pyi_file)

        return None

    def _try_relative(self, path: Path) -> Optional[Path]:
        try:
            return path.relative_to(self.repo_root)
        except ValueError:
            return None


class JavaImportResolver(ImportResolver):
    """Java import 解析器"""

    def resolve(self, module_path: str, source_file: Path) -> Optional[Path]:
        if self.is_external(module_path):
            return None

        # import com.blog.models.User → com/blog/models/User.java
        # 去掉 static 关键字
        clean = module_path.replace("static ", "").strip()
        # 去掉尾部通配符
        if clean.endswith(".*"):
            clean = clean[:-2]

        parts = clean.split(".")
        # 最后一个可能是类名，尝试整个路径
        java_path = Path("/".join(parts)).with_suffix(".java")
        full_path = self.repo_root / java_path
        if full_path.exists():
            return java_path

        # 如果最后一个是内部类/静态成员，去掉后再试
        if len(parts) > 1:
            parent_path = Path("/".join(parts[:-1])).with_suffix(".java")
            if (self.repo_root / parent_path).exists():
                return parent_path

        # 查找 src/main/java 等常见目录
        for src_dir in ("src/main/java", "src", "java"):
            candidate = self.repo_root / src_dir / java_path
            if candidate.exists():
                return Path(src_dir) / java_path

        return None

    def is_external(self, module_path: str) -> bool:
        return any(module_path.startswith(p) for p in JAVA_STDLIB_PREFIXES)


class JSImportResolver(ImportResolver):
    """JavaScript/TypeScript import 解析器"""

    def __init__(self, repo_root: Path, ts_config_paths: dict[str, str] = None):
        super().__init__(repo_root)
        self.ts_paths = ts_config_paths or {}
        self._load_ts_config()

    def _load_ts_config(self):
        """尝试从 tsconfig.json 加载路径别名"""
        if self.ts_paths:
            return
        tsconfig = self.repo_root / "tsconfig.json"
        if tsconfig.exists():
            try:
                import json
                config = json.loads(tsconfig.read_text(encoding="utf-8"))
                paths = config.get("compilerOptions", {}).get("paths", {})
                base_url = config.get("compilerOptions", {}).get("baseUrl", ".")
                for alias, targets in paths.items():
                    # "@/*" → ["src/*"] → 去掉尾部 /*
                    clean_alias = alias.rstrip("/*")
                    if targets:
                        clean_target = targets[0].rstrip("/*")
                        self.ts_paths[clean_alias] = str(Path(base_url) / clean_target)
            except Exception as e:
                logger.debug(f"解析 tsconfig.json 失败: {e}")

    def resolve(self, module_path: str, source_file: Path) -> Optional[Path]:
        if self.is_external(module_path):
            return None

        # 别名解析 (@/ → src/)
        resolved = self._resolve_alias(module_path)

        if resolved.startswith("."):
            # 相对路径
            base = (self.repo_root / source_file).parent
            target = (base / resolved).resolve()
        else:
            # 已解析的绝对路径或非外部路径
            target = (self.repo_root / resolved).resolve()

        return self._find_js_file(target)

    def is_external(self, module_path: str) -> bool:
        # 裸路径（无 ./ 或 ../）且不是已知别名 → 第三方包
        if module_path.startswith("."):
            return False
        # 检查别名
        for alias in self.ts_paths:
            if module_path.startswith(alias):
                return False
        # 检查仓库中是否存在
        if self._find_js_file((self.repo_root / module_path).resolve()):
            return False
        return True

    def _resolve_alias(self, module_path: str) -> str:
        for alias, target in self.ts_paths.items():
            if module_path.startswith(alias):
                return module_path.replace(alias, target, 1)
        return module_path

    def _find_js_file(self, target: Path) -> Optional[Path]:
        """尝试多种扩展名和 index 文件"""
        extensions = [".ts", ".tsx", ".js", ".jsx", ".mjs"]

        # 直接匹配
        if target.is_file():
            return self._try_relative(target)

        # 尝试添加扩展名
        for ext in extensions:
            candidate = target.with_suffix(ext)
            if candidate.is_file():
                return self._try_relative(candidate)

        # 尝试 index 文件
        if target.is_dir():
            for ext in extensions:
                index = target / f"index{ext}"
                if index.is_file():
                    return self._try_relative(index)

        return None

    def _try_relative(self, path: Path) -> Optional[Path]:
        try:
            return path.relative_to(self.repo_root)
        except ValueError:
            return None


class CIncludeResolver(ImportResolver):
    """C/C++ #include 解析器

    自动从 Makefile / CMakeLists.txt 提取 -I 路径，
    并加入 include/ 和 src/ 等常见默认路径。
    """

    def __init__(self, repo_root: Path, include_dirs: list[Path] = None):
        super().__init__(repo_root)
        if include_dirs is not None:
            self.include_dirs = include_dirs
        else:
            self.include_dirs = self._detect_include_dirs()

    def _detect_include_dirs(self) -> list[Path]:
        """从构建配置文件中提取 -I 路径，合并常见默认路径"""
        dirs: list[Path] = [self.repo_root]

        # 常见默认 include 目录
        for default in ("include", "src", "lib"):
            candidate = self.repo_root / default
            if candidate.is_dir():
                dirs.append(candidate)

        # 从 Makefile 提取 -I 标志
        makefile = self.repo_root / "Makefile"
        if makefile.exists():
            try:
                text = makefile.read_text(encoding="utf-8", errors="replace")
                # 匹配 -Ipath 或 -I path（含变量展开后的典型模式）
                for m in re.finditer(r'-I\s*([^\s,;]+)', text):
                    inc_path = Path(m.group(1))
                    if not inc_path.is_absolute():
                        inc_path = self.repo_root / inc_path
                    if inc_path.is_dir():
                        dirs.append(inc_path)
            except OSError:
                pass

        # 从 CMakeLists.txt 提取 include_directories / target_include_directories
        cmake = self.repo_root / "CMakeLists.txt"
        if cmake.exists():
            try:
                text = cmake.read_text(encoding="utf-8", errors="replace")
                for m in re.finditer(
                    r'(?:include_directories|target_include_directories)\s*\([^)]*?'
                    r'(?:PUBLIC|PRIVATE|INTERFACE)?\s*([^\s)]+)',
                    text, re.IGNORECASE,
                ):
                    inc_path = Path(m.group(1))
                    if not inc_path.is_absolute():
                        inc_path = self.repo_root / inc_path
                    if inc_path.is_dir():
                        dirs.append(inc_path)
            except OSError:
                pass

        # 去重保序
        seen: set[Path] = set()
        unique: list[Path] = []
        for d in dirs:
            resolved = d.resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique.append(d)
        return unique

    def resolve(self, module_path: str, source_file: Path) -> Optional[Path]:
        if self.is_external(module_path):
            return None

        # "xxx.h" → 相对于当前文件或 include_dirs
        # <xxx.h> → 系统头文件，通常已被 is_external 过滤

        # 相对于当前文件
        source_dir = (self.repo_root / source_file).parent
        relative = source_dir / module_path
        if relative.exists():
            try:
                return relative.relative_to(self.repo_root)
            except ValueError:
                pass

        # 相对于 include dirs
        for inc_dir in self.include_dirs:
            full = inc_dir if inc_dir.is_absolute() else self.repo_root / inc_dir
            candidate = full / module_path
            if candidate.exists():
                try:
                    return candidate.relative_to(self.repo_root)
                except ValueError:
                    pass

        logger.warning(
            f"C/C++ include 未解析: '{module_path}' from {source_file} "
            f"(搜索路径: {[str(d) for d in self.include_dirs]})"
        )
        return None

    def is_external(self, module_path: str) -> bool:
        # 系统头文件（无扩展名或标准库名）
        system_headers = {
            "stdio.h", "stdlib.h", "string.h", "math.h", "time.h",
            "assert.h", "ctype.h", "errno.h", "float.h", "limits.h",
            "locale.h", "setjmp.h", "signal.h", "stdarg.h", "stddef.h",
            "iostream", "string", "vector", "map", "set", "list",
            "algorithm", "memory", "functional", "utility", "tuple",
            "array", "deque", "stack", "queue", "bitset", "numeric",
            "fstream", "sstream", "iomanip", "cassert", "cmath",
            "cstdio", "cstdlib", "cstring", "ctime", "cctype",
            "thread", "mutex", "condition_variable", "future", "atomic",
        }
        basename = Path(module_path).name
        stem = Path(module_path).stem
        return basename in system_headers or stem in system_headers


# ═══════════════ 工厂 ═══════════════

def create_import_resolver(language: Language, repo_root: Path) -> ImportResolver:
    """根据语言创建 ImportResolver"""
    resolvers = {
        Language.PYTHON: PythonImportResolver,
        Language.JAVA: JavaImportResolver,
        Language.JAVASCRIPT: JSImportResolver,
        Language.TYPESCRIPT: JSImportResolver,
        Language.C: CIncludeResolver,
        Language.CPP: CIncludeResolver,
    }
    cls = resolvers.get(language)
    if cls is None:
        raise ValueError(f"不支持的语言: {language}")
    return cls(repo_root)
