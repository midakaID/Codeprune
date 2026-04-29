"""
Phase 2.5 (Layer 2.5): Boot Validation — 启动验证
在编译通过 + undefined names 解决后，验证子仓库能否最小启动。

流程:
1. 入口点评分 — 从 CodeGraph + 文件系统识别最佳入口点
2. LLM 生成启动脚本 — 不带副作用的最小 import + instantiation
3. subprocess 执行 — 检测 BOOT_OK / BOOT_FAIL
4. 错误反馈 — aider █ 标记格式供 LLM 修复层消费
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config import HealConfig
from core.graph.schema import CodeGraph, CodeNode, NodeType, Language
from core.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

# ── 入口点名字模式 ──

ENTRY_PATTERNS = [
    re.compile(r"^(main|app|cli|run|start|setup|server|init)$", re.I),
    re.compile(r"^handle[A-Z]", re.I),
    re.compile(r"Controller$", re.I),
    re.compile(r"^do[A-Z]"),
    re.compile(r"Service$"),
]

UTILITY_PATTERNS = [
    re.compile(r"^(get|set|is|has|to|from)(?:_|[A-Z])", re.I),
    re.compile(r"^_"),
    re.compile(r"Helper$", re.I),
    re.compile(r"Util(s)?$", re.I),
]

# 特征入口文件名（不含扩展名）
ENTRY_FILE_STEMS = {"main", "app", "cli", "server", "index", "run", "manage"}

# 语言到启动命令的映射
BOOT_COMMANDS = {
    Language.PYTHON: [sys.executable],
    Language.JAVA: ["java"],
    Language.TYPESCRIPT: ["npx", "ts-node"],
    Language.JAVASCRIPT: ["node"],
}


@dataclass
class EntryPoint:
    """识别到的入口点"""
    name: str
    file_path: Path          # 相对路径
    score: float             # 0.0 ~ 1.0
    is_exported: bool = False
    category: str = ""       # filesystem / codegraph / instruction


@dataclass
class BootResult:
    """启动验证结果"""
    success: bool
    boot_errors: list[str] = field(default_factory=list)
    script_content: str = ""
    execution_output: str = ""
    error_file: Optional[Path] = None
    error_line: Optional[int] = None
    error_type: str = ""     # ModuleNotFoundError / AttributeError / ...


# ── LLM Prompt ──

PROMPT_BOOT_SCRIPT = """Generate a minimal Python boot-test script for the sub-repository at: {sub_repo_path}

Available modules (from sub-repo):
{available_modules}

Top entry points (by priority):
{entry_points_desc}

Rules:
1. Import the top entry-point modules. If import fails, print "BOOT_FAIL: <error>" and exit.
2. Instantiate core objects if applicable (e.g. App(), Router()). No arguments that require external resources.
3. Do NOT perform side effects: no network calls, no file I/O, no database connections, no server.start().
4. Verify key attributes exist using hasattr() or simple assertions.
5. On success, print exactly "BOOT_OK" as the LAST line of output.
6. On any failure, print "BOOT_FAIL: <error_description>" and exit.
7. Wrap everything in try/except. The except block must catch Exception and print BOOT_FAIL.
8. Keep the script under 30 lines. Use only standard lib + sub-repo modules.
9. Add `import sys; sys.path.insert(0, ".")` as the first line.

Output ONLY the Python script, no markdown fences, no explanation."""

PROMPT_BOOT_SCRIPT_RETRY = """The previous boot script failed with this error:

{error_output}

Previous script:
```
{previous_script}
```

Available modules:
{available_modules}

Generate a FIXED boot-test script that avoids this error.
Same rules: import entry modules, no side effects, print BOOT_OK or BOOT_FAIL.
Add `import sys; sys.path.insert(0, ".")` as the first line.
Output ONLY the Python script, no markdown fences."""


class BootValidator:
    """启动验证器: 生成并执行启动测试脚本"""

    def __init__(
        self,
        config: HealConfig,
        sub_repo_path: Path,
        language: Language,
        llm: LLMProvider,
        graph: CodeGraph,
    ):
        self.config = config
        self.sub_repo_path = sub_repo_path
        self.language = language
        self.llm = llm
        self.graph = graph

    def validate(self) -> BootResult:
        """执行启动验证。返回 BootResult。"""
        # 目前只支持 Python
        if self.language != Language.PYTHON:
            return BootResult(success=True)

        # 1. 识别入口点
        entry_points = self._identify_entry_points()
        if not entry_points:
            logger.info("Boot validation: 无入口点可验证，跳过")
            return BootResult(success=True)

        max_ep = getattr(self.config, "boot_max_entry_points", 5)
        entry_points = entry_points[:max_ep]

        # 2. 生成启动脚本
        available_modules = self._list_modules()
        ep_desc = self._format_entry_points(entry_points)

        script = self._generate_boot_script(available_modules, ep_desc)
        if not script:
            logger.warning("Boot validation: LLM 未能生成启动脚本")
            return BootResult(success=True)  # 生成失败不阻塞

        # 3. 执行
        result = self._execute_script(script)
        result.script_content = script

        # 4. 如果失败，尝试一次重试
        if not result.success:
            max_retries = getattr(self.config, "boot_script_max_retries", 2)
            for retry in range(max_retries):
                logger.info(f"Boot validation retry {retry + 1}/{max_retries}")
                script = self._regenerate_script(
                    result.execution_output, script, available_modules,
                )
                if not script:
                    break
                result = self._execute_script(script)
                result.script_content = script
                if result.success:
                    break

        if result.success:
            logger.info("Boot validation: BOOT_OK")
        else:
            logger.warning(f"Boot validation failed: {result.error_type}")

        return result

    # ── 入口点识别 ──

    def _identify_entry_points(self) -> list[EntryPoint]:
        """三层策略识别入口点，按评分降序返回"""
        entries: list[EntryPoint] = []

        # 方法 A: 特征文件
        entries.extend(self._find_filesystem_entries())

        # 方法 B: CodeGraph 评分
        entries.extend(self._find_codegraph_entries())

        # 去重（按文件路径+名称）
        seen = set()
        unique: list[EntryPoint] = []
        for ep in entries:
            key = (str(ep.file_path), ep.name)
            if key not in seen:
                seen.add(key)
                unique.append(ep)

        unique.sort(key=lambda x: x.score, reverse=True)
        return unique

    def _find_filesystem_entries(self) -> list[EntryPoint]:
        """方法 A: 通过文件名模式识别入口点"""
        entries: list[EntryPoint] = []
        for py_file in self.sub_repo_path.rglob("*.py"):
            if py_file.stem in ENTRY_FILE_STEMS:
                rel = py_file.relative_to(self.sub_repo_path)
                entries.append(EntryPoint(
                    name=py_file.stem,
                    file_path=rel,
                    score=0.9,
                    category="filesystem",
                ))
        return entries

    def _find_codegraph_entries(self) -> list[EntryPoint]:
        """方法 B: 从 CodeGraph 中按调用关系评分"""
        entries: list[EntryPoint] = []
        sub_files = {
            f.relative_to(self.sub_repo_path)
            for f in self.sub_repo_path.rglob("*.py")
            if f.is_file()
        }
        # 转换为字符串集合便于匹配
        sub_file_strs = {str(f).replace("\\", "/") for f in sub_files}

        for node in self.graph.nodes.values():
            if node.node_type != NodeType.FUNCTION:
                continue
            if not node.file_path:
                continue

            # 检查节点是否在子仓库文件中
            node_rel = str(node.file_path).replace("\\", "/")
            if not any(node_rel.endswith(sf) for sf in sub_file_strs):
                continue

            # 跳过非入口点特征
            if not node.metadata.get("is_entry_point", False):
                continue

            # 评分
            callees = len(self.graph.get_outgoing(node.id, "calls"))
            callers = len(self.graph.get_incoming(node.id, "calls"))
            base_score = callees / (callers + 1)

            # 名字模式加权
            name_mult = 1.0
            if any(p.search(node.name) for p in ENTRY_PATTERNS):
                name_mult = 1.5
            elif any(p.search(node.name) for p in UTILITY_PATTERNS):
                name_mult = 0.3

            final_score = min(base_score * name_mult, 0.95)

            # 过滤零分/低价值的 utility 入口点，避免 boot 脚本被 get_config/set_config 之类噪声带偏
            if final_score <= 0 or (
                any(p.search(node.name) for p in UTILITY_PATTERNS) and final_score < 0.15
            ):
                continue

            rel_path = Path(node_rel)
            entries.append(EntryPoint(
                name=node.name,
                file_path=rel_path,
                score=round(final_score, 3),
                is_exported=node.metadata.get("is_entry_point", False),
                category="codegraph",
            ))

        return entries

    # ── 脚本生成 ──

    def _generate_boot_script(
        self, available_modules: str, entry_points_desc: str,
    ) -> Optional[str]:
        """调用 LLM 生成启动测试脚本"""
        prompt = PROMPT_BOOT_SCRIPT.format(
            sub_repo_path=self.sub_repo_path.name,
            available_modules=available_modules,
            entry_points_desc=entry_points_desc,
        )
        try:
            resp = self.llm.chat([{"role": "user", "content": prompt}])
            script = self._clean_script(resp)
            return script if script else None
        except Exception as e:
            logger.warning(f"Boot script generation failed: {e}")
            return None

    def _regenerate_script(
        self, error_output: str, previous_script: str, available_modules: str,
    ) -> Optional[str]:
        """失败后重新生成脚本"""
        prompt = PROMPT_BOOT_SCRIPT_RETRY.format(
            error_output=error_output[:2000],
            previous_script=previous_script,
            available_modules=available_modules,
        )
        try:
            resp = self.llm.chat([{"role": "user", "content": prompt}])
            script = self._clean_script(resp)
            return script if script else None
        except Exception as e:
            logger.warning(f"Boot script regeneration failed: {e}")
            return None

    @staticmethod
    def _clean_script(raw: str) -> str:
        """去除 markdown 代码围栏等包装"""
        text = raw.strip()
        # 去除 ```python ... ```
        if text.startswith("```"):
            lines = text.split("\n")
            # 找到开头的 ``` 行
            start = 1 if lines[0].startswith("```") else 0
            # 找到结尾的 ``` 行
            end = len(lines)
            for i in range(len(lines) - 1, 0, -1):
                if lines[i].strip() == "```":
                    end = i
                    break
            text = "\n".join(lines[start:end])
        return text.strip()

    # ── 脚本执行 ──

    def _execute_script(self, script: str) -> BootResult:
        """在子仓库工作目录中执行启动脚本"""
        script_name = "_codeprune_boot_test.py"
        script_path = self.sub_repo_path / script_name

        try:
            script_path.write_text(script, encoding="utf-8")
            timeout = getattr(self.config, "boot_timeout", 15)

            proc = subprocess.run(
                [sys.executable, script_name],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.sub_repo_path),
            )
            output = (proc.stdout or "") + (proc.stderr or "")

            if "BOOT_OK" in proc.stdout:
                return BootResult(success=True, execution_output=output)

            # 解析错误
            return self._parse_failure(output)

        except subprocess.TimeoutExpired:
            return BootResult(
                success=False,
                boot_errors=["Boot script timed out"],
                error_type="TimeoutError",
                execution_output=f"Timeout after {timeout}s",
            )
        except Exception as e:
            return BootResult(
                success=False,
                boot_errors=[str(e)],
                error_type=type(e).__name__,
                execution_output=str(e),
            )
        finally:
            # 清理临时脚本
            try:
                script_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _parse_failure(self, output: str) -> BootResult:
        """从执行输出中解析错误类型和位置"""
        error_type = ""
        error_file = None
        error_line = None
        boot_errors: list[str] = []

        # 提取 BOOT_FAIL 消息
        m = re.search(r"BOOT_FAIL:\s*(.+)", output)
        if m:
            boot_errors.append(m.group(1).strip())

        # 提取 Python 异常类型
        error_patterns = [
            (r"ModuleNotFoundError:\s*No module named '([^']+)'", "ModuleNotFoundError"),
            (r"ImportError:\s*(.+)", "ImportError"),
            (r"AttributeError:\s*(.+)", "AttributeError"),
            (r"TypeError:\s*(.+)", "TypeError"),
            (r"NameError:\s*(.+)", "NameError"),
            (r"SyntaxError:\s*(.+)", "SyntaxError"),
        ]
        for pat, etype in error_patterns:
            m = re.search(pat, output)
            if m:
                error_type = etype
                if not boot_errors:
                    boot_errors.append(m.group(0))
                break

        # 提取 Traceback 中的文件和行号
        tb_pattern = re.compile(r'File "([^"]+)", line (\d+)')
        for m in tb_pattern.finditer(output):
            fpath = m.group(1)
            # 跳过标准库和 boot 脚本本身
            if "_codeprune_boot_test" in fpath or "lib" in fpath.lower():
                continue
            error_file = Path(fpath)
            error_line = int(m.group(2))

        if not boot_errors:
            boot_errors.append(output[-500:] if output else "Unknown boot error")

        return BootResult(
            success=False,
            boot_errors=boot_errors,
            execution_output=output,
            error_file=error_file,
            error_line=error_line,
            error_type=error_type,
        )

    # ── 辅助方法 ──

    def _list_modules(self) -> str:
        """列出子仓库中可用的 Python 模块"""
        modules: list[str] = []
        for py_file in sorted(self.sub_repo_path.rglob("*.py")):
            if py_file.name.startswith("_codeprune_"):
                continue
            rel = py_file.relative_to(self.sub_repo_path)
            mod = str(rel.with_suffix("")).replace("\\", "/").replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[:-9]
            modules.append(mod)
        if not modules:
            return "No Python modules found."
        return "\n".join(f"  - {m}" for m in modules[:50])

    def _format_entry_points(self, entries: list[EntryPoint]) -> str:
        """格式化入口点列表供 LLM 使用"""
        lines: list[str] = []
        for i, ep in enumerate(entries, 1):
            lines.append(
                f"  {i}. {ep.name} (score={ep.score:.2f}, file={ep.file_path}, "
                f"category={ep.category})"
            )
        return "\n".join(lines) if lines else "  (no entry points identified)"

    @staticmethod
    def format_boot_error(boot_result: BootResult, sub_repo_path: Path) -> str:
        """将 BootResult 格式化为 aider █ 标记风格的错误描述，供 LLM 修复层消费"""
        parts: list[str] = ["## Boot Test Failed\n"]

        if boot_result.error_type:
            parts.append(f"**Error Type**: `{boot_result.error_type}`")

        for err in boot_result.boot_errors[:3]:
            parts.append(f"**Error**: {err}")

        # 如果有错误位置，展示上下文
        if boot_result.error_file and boot_result.error_line:
            try:
                target = sub_repo_path / boot_result.error_file
                if not target.exists():
                    # 尝试相对路径
                    for f in sub_repo_path.rglob(boot_result.error_file.name):
                        target = f
                        break

                if target.exists():
                    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
                    line_idx = boot_result.error_line - 1
                    start = max(0, line_idx - 3)
                    end = min(len(lines), line_idx + 4)

                    parts.append(f"\n**Context** ({boot_result.error_file}):")
                    for i in range(start, end):
                        marker = "█ " if i == line_idx else "  "
                        parts.append(f"  {marker}{i + 1:4d}│ {lines[i]}")
            except Exception:
                pass

        # 建议修复方向
        suggestions = {
            "ModuleNotFoundError": "删除或注释对已删模块的 import 语句",
            "ImportError": "检查 __init__.py 是否导出了不存在的符号",
            "AttributeError": "生成缺失属性的 stub 或移除相关引用",
            "TypeError": "检查函数签名是否完整",
            "NameError": "添加缺失的 import 或定义",
        }
        if boot_result.error_type in suggestions:
            parts.append(f"\n**Suggested fix**: {suggestions[boot_result.error_type]}")

        return "\n".join(parts)
