"""
Phase 3.5 (Layer 3.5): Functional Validation — 功能验证
在 Build/UndefinedNames/Boot/Completeness/Fidelity 全部通过后，验证核心业务路径不崩溃。

设计核心：两阶段验证
  Stage 1: 在原仓库运行功能测试脚本 → 验证脚本本身是否正确
  Stage 2: 在子仓库运行同一脚本 → 发现子仓库的真实问题

默认关闭 (enable_functional_validation = False)。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config import HealConfig
from core.graph.schema import CodeGraph, CodeNode, NodeType, Language, EdgeType
from core.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

# ── Prompt Templates ──

PROMPT_FUNC_SCRIPT = """Generate a functional smoke-test script for a pruned Python sub-repository.

Language: Python
User's extraction instruction:
{user_instruction}

Available modules in the sub-repository:
{available_modules}

Core classes and functions (from code graph):
{core_symbols}

Generate a script that:
1. Add `import sys; sys.path.insert(0, ".")` as the first line.
2. Import the core modules listed above.
3. Instantiate core objects with safe/minimal arguments (empty strings, 0, None, empty dicts).
4. Call 2-5 key business functions with mock/safe inputs.
   - NO side effects: no network calls, no file I/O, no database connections, no server.start().
   - NO infinite loops, NO blocking calls, NO stdin reads.
5. On success, print exactly "FUNC_OK" as the LAST line.
6. On any failure, print "FUNC_FAIL: <error_description>" and exit with code 1.
7. Wrap everything in try/except. The except block must catch Exception and print FUNC_FAIL.
8. Keep the script between 30-60 lines. Use only standard lib + sub-repo modules.

Output ONLY the Python script, no markdown fences, no explanation."""

PROMPT_FUNC_SCRIPT_RETRY = """The previous functional test script failed with this error:

{error_output}

Previous script:
```
{previous_script}
```

Available modules:
{available_modules}

Core symbols:
{core_symbols}

Generate a FIXED functional test script that avoids this error.
Same rules: import core modules, call key functions with safe inputs, no side effects.
Print "FUNC_OK" on success or "FUNC_FAIL: <error>" on failure.
Add `import sys; sys.path.insert(0, ".")` as the first line.
Output ONLY the Python script, no markdown fences."""


@dataclass
class FunctionalResult:
    """功能验证结果"""
    success: bool
    errors: list[str] = field(default_factory=list)
    script_content: str = ""
    execution_output: str = ""
    error_file: Optional[Path] = None
    error_line: Optional[int] = None
    error_type: str = ""


class FunctionalValidator:
    """功能验证器: 两阶段验证核心业务路径"""

    def __init__(
        self,
        config: HealConfig,
        sub_repo_path: Path,
        source_repo_path: Path,
        language: Language,
        llm: LLMProvider,
        graph: CodeGraph,
        user_instruction: str = "",
    ):
        self.config = config
        self.sub_repo_path = sub_repo_path
        self.source_repo_path = source_repo_path
        self.language = language
        self.llm = llm
        self.graph = graph
        self.user_instruction = user_instruction

    def validate(self) -> FunctionalResult:
        """两阶段功能验证。返回 FunctionalResult。"""
        if self.language != Language.PYTHON:
            return FunctionalResult(success=True)

        available_modules = self._list_modules(self.sub_repo_path)
        core_symbols = self._extract_core_symbols()

        if not core_symbols.strip():
            logger.info("Functional validation: 无核心符号可验证，跳过")
            return FunctionalResult(success=True)

        # Stage 1: 生成脚本并在原仓库验证
        script = self._generate_script(available_modules, core_symbols)
        if not script:
            logger.warning("Functional validation: LLM 未能生成测试脚本")
            return FunctionalResult(success=True)

        max_retries = getattr(self.config, "functional_script_max_retries", 2)

        # 先在原仓库跑，确认脚本正确
        source_result = self._execute_script(script, self.source_repo_path)
        if not source_result.success:
            # 脚本本身有问题，尝试重新生成
            for retry in range(max_retries):
                logger.info(
                    f"Functional script failed on source repo, "
                    f"regenerating ({retry + 1}/{max_retries})"
                )
                script = self._regenerate_script(
                    source_result.execution_output, script,
                    available_modules, core_symbols,
                )
                if not script:
                    break
                source_result = self._execute_script(script, self.source_repo_path)
                if source_result.success:
                    break

            if not source_result.success:
                logger.warning(
                    "Functional validation: 脚本在原仓库也失败，放弃功能验证"
                )
                return FunctionalResult(success=True)

        # Stage 2: 在子仓库跑同一脚本
        assert script is not None
        sub_result = self._execute_script(script, self.sub_repo_path)
        sub_result.script_content = script

        if sub_result.success:
            logger.info("Functional validation: FUNC_OK")
        else:
            logger.warning(f"Functional validation failed: {sub_result.error_type}")

        return sub_result

    # ── 核心符号提取 ──

    def _extract_core_symbols(self) -> str:
        """从 CodeGraph 中提取子仓库内的核心类和函数"""
        sub_files = set()
        for f in self.sub_repo_path.rglob("*.py"):
            if f.is_file() and not f.name.startswith("_codeprune_"):
                rel = str(f.relative_to(self.sub_repo_path)).replace("\\", "/")
                sub_files.add(rel)

        classes: list[str] = []
        functions: list[str] = []

        for node in self.graph.nodes.values():
            if not node.file_path:
                continue
            node_rel = str(node.file_path).replace("\\", "/")
            if not any(node_rel.endswith(sf) for sf in sub_files):
                continue

            if node.node_type == NodeType.CLASS:
                classes.append(f"  - class {node.name} ({node_rel})")
            elif node.node_type == NodeType.FUNCTION:
                # 只列出公开的、非工具函数
                if node.name.startswith("_"):
                    continue
                # 优先列出入口点和被多处调用的函数
                callers = len(self.graph.get_incoming(node.id, EdgeType.CALLS))
                if callers >= 1 or node.metadata.get("is_entry_point", False):
                    functions.append(
                        f"  - {node.name}() ({node_rel}, callers={callers})"
                    )

        lines: list[str] = []
        if classes:
            lines.append("Classes:")
            lines.extend(classes[:20])
        if functions:
            lines.append("Functions:")
            lines.extend(sorted(functions, key=lambda x: x)[:30])

        return "\n".join(lines)

    # ── 脚本生成 ──

    def _generate_script(
        self, available_modules: str, core_symbols: str,
    ) -> Optional[str]:
        """调用 LLM 生成功能测试脚本"""
        prompt = PROMPT_FUNC_SCRIPT.format(
            user_instruction=self.user_instruction[:2000],
            available_modules=available_modules,
            core_symbols=core_symbols[:3000],
        )
        try:
            resp = self.llm.chat([{"role": "user", "content": prompt}])
            return self._clean_script(resp)
        except Exception as e:
            logger.warning(f"Functional script generation failed: {e}")
            return None

    def _regenerate_script(
        self,
        error_output: str,
        previous_script: str,
        available_modules: str,
        core_symbols: str,
    ) -> Optional[str]:
        """失败后重新生成脚本"""
        prompt = PROMPT_FUNC_SCRIPT_RETRY.format(
            error_output=error_output[:2000],
            previous_script=previous_script,
            available_modules=available_modules,
            core_symbols=core_symbols[:3000],
        )
        try:
            resp = self.llm.chat([{"role": "user", "content": prompt}])
            return self._clean_script(resp)
        except Exception as e:
            logger.warning(f"Functional script regeneration failed: {e}")
            return None

    @staticmethod
    def _clean_script(raw: str) -> Optional[str]:
        """去除 markdown 代码围栏等包装"""
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            start = 1 if lines[0].startswith("```") else 0
            end = len(lines)
            for i in range(len(lines) - 1, 0, -1):
                if lines[i].strip() == "```":
                    end = i
                    break
            text = "\n".join(lines[start:end])
        text = text.strip()
        return text if text else None

    # ── 脚本执行 ──

    def _execute_script(self, script: str, target_path: Path) -> FunctionalResult:
        """在指定目录中执行功能测试脚本"""
        script_name = "_codeprune_func_test.py"
        script_path = target_path / script_name
        timeout = getattr(self.config, "functional_timeout", 30)

        try:
            script_path.write_text(script, encoding="utf-8")

            proc = subprocess.run(
                [sys.executable, script_name],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(target_path),
            )
            output = (proc.stdout or "") + (proc.stderr or "")

            if "FUNC_OK" in proc.stdout:
                return FunctionalResult(success=True, execution_output=output)

            # 解析失败
            result = FunctionalResult(
                success=False,
                execution_output=output,
            )
            self._parse_failure(result, output)
            return result

        except subprocess.TimeoutExpired:
            return FunctionalResult(
                success=False,
                errors=["Functional test timed out"],
                error_type="TimeoutError",
                execution_output=f"Script timed out after {timeout}s",
            )
        except Exception as e:
            return FunctionalResult(
                success=False,
                errors=[str(e)],
                error_type=type(e).__name__,
            )
        finally:
            try:
                script_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _parse_failure(result: FunctionalResult, output: str) -> None:
        """从 traceback 中提取错误信息"""
        import re

        # 提取错误类型
        err_type_match = re.search(
            r"(\w+Error|\w+Exception):\s*(.+)", output, re.MULTILINE,
        )
        if err_type_match:
            result.error_type = err_type_match.group(1)
            result.errors = [err_type_match.group(2).strip()]
        elif "FUNC_FAIL:" in output:
            fail_match = re.search(r"FUNC_FAIL:\s*(.+)", output)
            if fail_match:
                result.errors = [fail_match.group(1).strip()]
                result.error_type = "FuncFail"
        else:
            result.errors = [output[-500:] if output else "Unknown failure"]

        # 提取文件和行号
        file_match = re.search(
            r'File "([^"]+)", line (\d+)', output, re.MULTILINE,
        )
        if file_match:
            result.error_file = Path(file_match.group(1))
            result.error_line = int(file_match.group(2))

    # ── 辅助 ──

    @staticmethod
    def _list_modules(repo_path: Path) -> str:
        """列出仓库中可用的 Python 模块"""
        modules: list[str] = []
        for py_file in sorted(repo_path.rglob("*.py")):
            if py_file.name.startswith("_codeprune_"):
                continue
            rel = py_file.relative_to(repo_path)
            mod = str(rel.with_suffix("")).replace("\\", "/").replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[:-9]
            modules.append(mod)
        if not modules:
            return "No Python modules found."
        return "\n".join(f"  - {m}" for m in modules[:50])

    @staticmethod
    def format_functional_error(
        result: FunctionalResult, sub_repo_path: Path,
    ) -> str:
        """格式化功能验证错误为 aider █ 标记风格"""
        parts: list[str] = ["## Functional Test Failed\n"]

        if result.error_type:
            parts.append(f"**Error**: {result.error_type}: {'; '.join(result.errors)}")

        if result.error_file:
            try:
                rel = result.error_file.relative_to(sub_repo_path)
            except ValueError:
                rel = result.error_file
            parts.append(f"**File**: {rel}")
            if result.error_line:
                parts.append(f"**Line**: {result.error_line}")

        if result.execution_output:
            trimmed = result.execution_output[-1000:]
            parts.append(f"\n**Output**:\n```\n{trimmed}\n```")

        if result.script_content:
            parts.append(f"\n**Test Script**:\n```python\n{result.script_content}\n```")

        return "\n".join(parts)
