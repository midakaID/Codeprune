"""
Phase3+: 子仓库产物生成 — requirements + README
在 Phase3 heal 成功后自动运行，为子仓库生成面向使用者的依赖清单和文档。
"""

from __future__ import annotations

import ast
import logging
import re
import sys
from pathlib import Path
from typing import Optional

from config import CodePruneConfig
from core.graph.schema import CodeGraph, EdgeType, NodeType
from core.llm.provider import LLMProvider
from core.llm.prompts import Prompts
from core.prune.closure import ClosureResult

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════════════════════

# Python import 名 → pip 包名 (仅列不一致的)
_IMPORT_TO_PIP: dict[str, str] = {
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "attr": "attrs",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "jose": "python-jose",
    "magic": "python-magic",
    "gi": "PyGObject",
    "usb": "pyusb",
    "serial": "pyserial",
    "wx": "wxPython",
    "Crypto": "pycryptodome",
    "lxml": "lxml",
}

# Python stdlib (3.10+ 有 sys.stdlib_module_names，低版本 fallback)
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
}

# Node.js 内置模块
_NODE_BUILTINS: set[str] = {
    "assert", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "dns", "domain",
    "events", "fs", "http", "http2", "https",
    "module", "net", "os", "path", "perf_hooks",
    "process", "querystring", "readline", "repl", "stream",
    "string_decoder", "timers", "tls", "tty", "url",
    "util", "v8", "vm", "worker_threads", "zlib",
    # Node 16+ with node: prefix handled separately
}


# ═══════════════════════════════════════════════════════════════
#  SubRepoFinalizer
# ═══════════════════════════════════════════════════════════════

class SubRepoFinalizer:
    """Phase3 后处理 — 生成子仓库的依赖清单和 README"""

    def __init__(
        self,
        config: CodePruneConfig,
        llm: LLMProvider,
        graph: CodeGraph,
        closure: Optional[ClosureResult] = None,
    ):
        self.config = config
        self.llm = llm
        self.graph = graph
        self.closure = closure

    # ─────────────────── 主入口 ───────────────────

    def finalize(self, sub_repo_path: Path) -> dict[str, Path | None]:
        """
        生成子仓库产物:
          1. 依赖清单 (requirements.txt / package.json / pom.xml)
          2. README.md

        返回 {"requirements": path|None, "readme": path|None}
        """
        results: dict[str, Path | None] = {}

        # 1. 依赖清单
        lang = self._detect_primary_language(sub_repo_path)
        req_path = self._generate_requirements(sub_repo_path, lang)
        results["requirements"] = req_path

        # 2. README
        readme_path = self._generate_readme(sub_repo_path, lang)
        results["readme"] = readme_path

        return results

    # ═══════════════════════════════════════════════════════════
    #  依赖清单生成
    # ═══════════════════════════════════════════════════════════

    def _generate_requirements(
        self, sub_repo_path: Path, lang: str,
    ) -> Path | None:
        """根据主语言生成依赖清单"""
        if lang == "python":
            return self._python_requirements(sub_repo_path)
        elif lang in ("javascript", "typescript"):
            return self._js_requirements(sub_repo_path)
        elif lang == "java":
            return self._java_requirements(sub_repo_path)
        else:
            logger.info(f"语言 {lang} 暂不支持依赖清单生成")
            return None

    # ── Python ──

    def _python_requirements(self, sub_repo_path: Path) -> Path:
        """AST 扫描 → stdlib/internal/external 分类 → 版本继承 → requirements.txt"""
        imports = self._scan_python_imports(sub_repo_path)
        internal = self._python_internal_modules(sub_repo_path)
        external = {m for m in imports if m not in _PYTHON_STDLIB and m not in internal}

        # import 名 → pip 包名
        pip_pkgs: dict[str, str] = {}  # pip_name → import_name
        for imp in sorted(external):
            pip_name = _IMPORT_TO_PIP.get(imp, imp)
            pip_pkgs[pip_name] = imp

        # 从原仓库 requirements.txt 继承版本约束
        versions = self._load_original_python_versions()

        # 输出
        out = sub_repo_path / "requirements.txt"
        repo_name = self.config.repo_path.name
        lines = [
            f"# Auto-generated by CodePrune",
            f"# Source: {repo_name}",
            f"# Feature: {self.config.user_instruction or 'N/A'}",
            "",
        ]
        if not pip_pkgs:
            lines.append("# (无外部依赖 — 仅使用标准库和内部模块)")
        else:
            for pkg in sorted(pip_pkgs):
                constraint = versions.get(pkg, "")
                lines.append(f"{pkg}{constraint}")

        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info(f"requirements.txt: {len(pip_pkgs)} 个外部依赖")
        return out

    def _scan_python_imports(self, sub_repo_path: Path) -> set[str]:
        """AST 扫描所有 .py 文件，提取顶层模块名"""
        modules: set[str] = set()
        for py_file in sub_repo_path.rglob("*.py"):
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        modules.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.level == 0:  # 非相对 import
                        modules.add(node.module.split(".")[0])
        return modules

    def _python_internal_modules(self, sub_repo_path: Path) -> set[str]:
        """子仓库内部模块名（.py 文件名 + 目录包名）"""
        internal: set[str] = set()
        for item in sub_repo_path.iterdir():
            if item.suffix == ".py":
                internal.add(item.stem)
            elif item.is_dir() and (item / "__init__.py").exists():
                internal.add(item.name)
        return internal

    def _load_original_python_versions(self) -> dict[str, str]:
        """从原仓库 requirements.txt 提取版本约束"""
        versions: dict[str, str] = {}
        for name in ("requirements.txt", "requirements-prod.txt", "requirements-base.txt"):
            req_file = self.config.repo_path / name
            if req_file.exists():
                for line in req_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("-"):
                        continue
                    m = re.match(r"^([A-Za-z0-9_-]+)\s*(.*)$", line)
                    if m:
                        pkg, constraint = m.group(1).lower(), m.group(2).strip()
                        # 去掉行尾注释
                        if "#" in constraint:
                            constraint = constraint[:constraint.index("#")].strip()
                        if constraint:
                            versions[pkg] = constraint
                break  # 只读取找到的第一个
        return versions

    # ── JavaScript / TypeScript ──

    def _js_requirements(self, sub_repo_path: Path) -> Path | None:
        """扫描 JS/TS 文件的 import/require → 裁剪 package.json"""
        import json

        used_pkgs = self._scan_js_imports(sub_repo_path)
        if not used_pkgs:
            logger.info("JS/TS 子仓库无外部依赖")
            return None

        # 读取原仓库 package.json
        orig_pkg = self.config.repo_path / "package.json"
        if not orig_pkg.exists():
            # 没有原始 package.json，只能列出包名
            pkg_json = {
                "name": f"{self.config.repo_path.name}-pruned",
                "version": "1.0.0",
                "description": self.config.user_instruction or "Pruned sub-repository",
                "dependencies": {p: "*" for p in sorted(used_pkgs)},
            }
        else:
            orig = json.loads(orig_pkg.read_text(encoding="utf-8"))
            all_deps = {}
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                all_deps.update(orig.get(key, {}))

            # 保留子仓库实际使用的包（保持原版本约束）
            trimmed_deps = {p: all_deps[p] for p in sorted(used_pkgs) if p in all_deps}
            # 在原 package.json 中找不到的包
            unknown = used_pkgs - set(all_deps.keys())
            for p in sorted(unknown):
                trimmed_deps[p] = "*"

            pkg_json = {
                "name": orig.get("name", self.config.repo_path.name) + "-pruned",
                "version": orig.get("version", "1.0.0"),
                "description": self.config.user_instruction or orig.get("description", ""),
                "dependencies": trimmed_deps,
            }

        out = sub_repo_path / "package.json"
        out.write_text(json.dumps(pkg_json, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        logger.info(f"package.json: {len(pkg_json['dependencies'])} 个依赖")
        return out

    def _scan_js_imports(self, sub_repo_path: Path) -> set[str]:
        """正则扫描 JS/TS 文件的 import/require 语句"""
        pkgs: set[str] = set()
        patterns = [
            re.compile(r"""(?:require|import)\s*\(\s*['"]([^'"]+)['"]\s*\)"""),  # require('x') / import('x')
            re.compile(r"""from\s+['"]([^'"]+)['"]"""),                           # import ... from 'x'
            re.compile(r"""import\s+['"]([^'"]+)['"]"""),                          # import 'x'
        ]
        for ext in ("*.js", "*.jsx", "*.ts", "*.tsx", "*.mjs", "*.cjs"):
            for f in sub_repo_path.rglob(ext):
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for pat in patterns:
                    for m in pat.finditer(content):
                        spec = m.group(1)
                        # 过滤: 相对路径、Node 内置、node: 前缀
                        if spec.startswith(".") or spec.startswith("/"):
                            continue
                        if spec.startswith("node:"):
                            continue
                        # 提取包名 (处理 @scope/pkg/sub → @scope/pkg)
                        if spec.startswith("@"):
                            parts = spec.split("/")
                            pkg_name = "/".join(parts[:2]) if len(parts) >= 2 else spec
                        else:
                            pkg_name = spec.split("/")[0]
                        if pkg_name not in _NODE_BUILTINS:
                            pkgs.add(pkg_name)
        return pkgs

    # ── Java ──

    def _java_requirements(self, sub_repo_path: Path) -> Path | None:
        """扫描 .java 文件 import → 裁剪 pom.xml"""
        used_packages = self._scan_java_imports(sub_repo_path)
        if not used_packages:
            logger.info("Java 子仓库无外部依赖")
            return None

        orig_pom = self.config.repo_path / "pom.xml"
        if not orig_pom.exists():
            logger.info("原仓库无 pom.xml，跳过 Java 依赖裁剪")
            return None

        pom_content = orig_pom.read_text(encoding="utf-8", errors="replace")

        # 解析 <dependency> 块
        dep_pattern = re.compile(
            r"<dependency>\s*"
            r"<groupId>([^<]+)</groupId>\s*"
            r"<artifactId>([^<]+)</artifactId>\s*"
            r"(?:<version>([^<]*)</version>\s*)?"
            r"(?:<scope>([^<]*)</scope>\s*)?"
            r".*?"
            r"</dependency>",
            re.DOTALL,
        )

        kept_deps: list[str] = []
        for m in dep_pattern.finditer(pom_content):
            group_id = m.group(1).strip()
            # 检查子仓库是否有 import 以此 groupId 开头的类
            if any(pkg.startswith(group_id) for pkg in used_packages):
                kept_deps.append(m.group(0))

        if not kept_deps:
            return None

        # 生成裁剪后的 pom.xml (保留原 pom 结构，替换 dependencies)
        deps_block = "\n    ".join(kept_deps)
        # 简单替换: 找到 <dependencies>...</dependencies> 区域并替换
        trimmed = re.sub(
            r"<dependencies>.*?</dependencies>",
            f"<dependencies>\n    {deps_block}\n  </dependencies>",
            pom_content,
            flags=re.DOTALL,
        )

        out = sub_repo_path / "pom.xml"
        out.write_text(trimmed, encoding="utf-8")
        logger.info(f"pom.xml: {len(kept_deps)} 个依赖保留")
        return out

    def _scan_java_imports(self, sub_repo_path: Path) -> set[str]:
        """扫描 .java 文件提取 import 的包名"""
        packages: set[str] = set()
        java_std = {"java.", "javax.", "sun.", "com.sun.", "jdk."}
        for f in sub_repo_path.rglob("*.java"):
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in re.finditer(r"^import\s+(?:static\s+)?([a-zA-Z0-9_.]+)\s*;", content, re.MULTILINE):
                fqn = m.group(1)
                # 过滤 Java 标准库
                if not any(fqn.startswith(prefix) for prefix in java_std):
                    # 取到倒数第二个 . (去掉类名)
                    pkg = fqn.rsplit(".", 1)[0] if "." in fqn else fqn
                    packages.add(pkg)
        return packages

    # ═══════════════════════════════════════════════════════════
    #  README.md 生成
    # ═══════════════════════════════════════════════════════════

    def _generate_readme(self, sub_repo_path: Path, lang: str) -> Path:
        """收集图谱信息 → 1 次 LLM 调用 → 生成 README"""
        # 收集上下文
        file_details = self._collect_file_details(sub_repo_path)
        dep_graph = self._collect_dependency_graph(sub_repo_path)
        external_deps = self._collect_external_deps(sub_repo_path)
        pruned_info = self._collect_pruned_info(sub_repo_path)

        inst_lang = self._detect_instruction_language()
        repo_name = self.config.repo_path.name

        prompt = Prompts.GENERATE_SUB_REPO_README.format(
            repo_name=repo_name,
            user_instruction=self.config.user_instruction or "(未指定)",
            file_details=file_details,
            dependency_graph=dep_graph,
            external_deps_text=external_deps or "(无外部依赖)",
            pruned_info=pruned_info or "(无裁剪信息)",
            language=inst_lang,
        )

        try:
            readme_content = self.llm.fast_chat([{"role": "user", "content": prompt}])
            # 清理: 如果 LLM 包了 ```markdown ... ``` 围栏
            readme_content = re.sub(
                r"^```(?:markdown|md)?\s*\n", "", readme_content,
            )
            readme_content = re.sub(r"\n```\s*$", "", readme_content)
        except Exception as e:
            logger.warning(f"README LLM 生成失败: {e}, 使用模板 fallback")
            readme_content = self._fallback_readme(sub_repo_path, file_details, external_deps)

        out = sub_repo_path / "README.md"
        out.write_text(readme_content.strip() + "\n", encoding="utf-8")
        logger.info("README.md 已生成")
        return out

    # ── 上下文收集 ──

    def _collect_file_details(self, sub_repo_path: Path) -> str:
        """从图谱和子仓库收集每个文件的摘要 + 公开函数签名"""
        blocks: list[str] = []
        for f in sorted(sub_repo_path.rglob("*")):
            if not f.is_file() or f.name.startswith("."):
                continue
            rel = f.relative_to(sub_repo_path)
            if str(rel).startswith(".codeprune"):
                continue

            try:
                line_count = len(f.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                line_count = 0

            # 从图谱获取摘要
            file_nid = f"file:{rel}"
            file_node = self.graph.get_node(file_nid)
            summary = file_node.summary if file_node and file_node.summary else f"{rel.name}"

            block = f"### {rel} ({line_count} 行)\n摘要: {summary}\n"

            # 收集公开函数/类签名
            if file_node and file_node.children:
                funcs: list[str] = []
                for child_id in file_node.children:
                    child = self.graph.get_node(child_id)
                    if not child:
                        continue
                    if child.node_type in (NodeType.FUNCTION, NodeType.CLASS):
                        sig = child.signature or child.name
                        desc = child.summary or ""
                        funcs.append(f"  {sig}\n    → {desc}" if desc else f"  {sig}")
                if funcs:
                    block += "\n公开函数/类:\n" + "\n".join(funcs) + "\n"

            blocks.append(block)

        return "\n".join(blocks) if blocks else "(无文件信息)"

    def _collect_dependency_graph(self, sub_repo_path: Path) -> str:
        """从图谱 IMPORTS 边提取子仓库内部的文件间依赖关系"""
        sub_files = set()
        for f in sub_repo_path.rglob("*"):
            if f.is_file() and f.suffix in (".py", ".java", ".js", ".ts", ".jsx", ".tsx"):
                sub_files.add(str(f.relative_to(sub_repo_path)))

        dep_lines: list[str] = []
        for edge in self.graph.edges:
            if edge.edge_type != EdgeType.IMPORTS:
                continue
            src_file = edge.source.removeprefix("file:")
            tgt_file = edge.target.removeprefix("file:")
            if src_file in sub_files and tgt_file in sub_files:
                symbols = edge.metadata.get("imported_symbols", [])
                sym_text = f" ({', '.join(symbols)})" if symbols else ""
                dep_lines.append(f"{src_file} → {tgt_file}{sym_text}")

        return "\n".join(sorted(dep_lines)) if dep_lines else "(无内部依赖)"

    def _collect_external_deps(self, sub_repo_path: Path) -> str:
        """读取子仓库的 requirements.txt 或 package.json"""
        req = sub_repo_path / "requirements.txt"
        if req.exists():
            content = req.read_text(encoding="utf-8", errors="replace")
            # 过滤注释和空行
            deps = [l for l in content.splitlines() if l.strip() and not l.startswith("#")]
            return "\n".join(deps) if deps else ""

        pkg_json = sub_repo_path / "package.json"
        if pkg_json.exists():
            import json
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            deps = data.get("dependencies", {})
            return "\n".join(f"{k}: {v}" for k, v in deps.items()) if deps else ""

        return ""

    def _collect_pruned_info(self, sub_repo_path: Path) -> str:
        """收集被裁剪的文件信息"""
        if not self.closure:
            return "(闭包信息不可用)"

        sub_files = set()
        for f in sub_repo_path.rglob("*"):
            if f.is_file():
                sub_files.add(str(f.relative_to(sub_repo_path)))

        # 找出图谱中有、但子仓库中不存在的文件
        pruned: list[str] = []
        for nid, node in self.graph.nodes.items():
            if node.node_type != NodeType.FILE:
                continue
            rel = str(node.file_path) if node.file_path else nid.removeprefix("file:")
            if rel not in sub_files:
                summary = node.summary or node.name
                line_count = ""
                if node.byte_range:
                    line_count = f" ({node.byte_range.end_line} 行)"
                pruned.append(f"- {rel}{line_count} — {summary}")

        if not pruned:
            return "(无文件被裁剪)"

        excluded = len(self.closure.excluded_edges) if self.closure else 0
        lines = ["已排除的文件:"] + pruned
        if excluded:
            lines.append(f"\n已切断的调用关系: {excluded} 处")
        return "\n".join(lines)

    # ── 辅助方法 ──

    def _detect_primary_language(self, sub_repo_path: Path) -> str:
        """统计子仓库中各语言文件数量，返回主语言"""
        ext_map = {
            ".py": "python", ".java": "java",
            ".js": "javascript", ".jsx": "javascript",
            ".ts": "typescript", ".tsx": "typescript",
            ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
        }
        counts: dict[str, int] = {}
        for f in sub_repo_path.rglob("*"):
            if f.is_file() and f.suffix in ext_map:
                lang = ext_map[f.suffix]
                counts[lang] = counts.get(lang, 0) + 1
        if not counts:
            return "unknown"
        return max(counts, key=counts.get)  # type: ignore

    def _detect_instruction_language(self) -> str:
        """检测用户指令语言 (中文 → 'Chinese', 否则 → 'English')"""
        inst = self.config.user_instruction or ""
        chinese_chars = sum(1 for c in inst if "\u4e00" <= c <= "\u9fff")
        return "Chinese" if chinese_chars > len(inst) * 0.2 else "English"

    def _fallback_readme(
        self, sub_repo_path: Path, file_details: str, external_deps: str,
    ) -> str:
        """LLM 失败时的模板 fallback"""
        repo_name = self.config.repo_path.name
        instruction = self.config.user_instruction or "(未指定)"
        return f"""# {repo_name} — 裁剪子仓库

> 从 `{repo_name}` 提取: {instruction}

## 文件结构

{file_details}

## 依赖

{external_deps or "(无外部依赖)"}

---
*由 CodePrune 自动生成*
"""
