"""
Phase3: CodeHeal — LLM 修复引擎
基于编译错误，使用 LLM 生成修复补丁
约束：以原仓库代码为 ground truth，不得凭空生成逻辑
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config import CodePruneConfig
from core.graph.schema import CodeGraph
from core.heal.error_dispatcher import ErrorDispatcher
from core.heal.import_fixer import CascadeCleaner, ImportFixer, UndefinedNameResolver
from core.heal.source_recovery import SourceRecovery
from core.heal.validator import ValidationError, ValidationResult, BuildValidator
from core.llm.provider import LLMProvider
from core.llm.prompts import Prompts

logger = logging.getLogger(__name__)


def _find_context_core(lines: list[str], context: list[str], start: int) -> tuple[int, int]:
    """三级模糊匹配 (借鉴 aider/patch_coder.py find_context_core)
    Level 0: 精确行匹配
    Level 1: rstrip 匹配 (忽略行尾空白/CRLF差异)
    Level 100: strip 匹配 (忽略所有前后空白)
    Level 200: 缩进感知匹配 (忽略 leading whitespace 的绝对量, 只看 strip 后内容)
    返回: (匹配位置, fuzz级别) 或 (-1, 0) 表示未找到
    """
    if not context:
        return start, 0
    n = len(context)
    end = len(lines) - n + 1
    # Level 0: exact
    for i in range(start, end):
        if lines[i:i + n] == context:
            return i, 0
    # Level 1: rstrip
    norm_ctx = [s.rstrip() for s in context]
    for i in range(start, end):
        if [s.rstrip() for s in lines[i:i + n]] == norm_ctx:
            return i, 1
    # Level 100: strip
    norm_ctx_strip = [s.strip() for s in context]
    for i in range(start, end):
        if [s.strip() for s in lines[i:i + n]] == norm_ctx_strip:
            return i, 100
    # Level 200: 缩进感知 — strip 后匹配，但排除纯空行差异
    non_empty_ctx = [s.strip() for s in context if s.strip()]
    if non_empty_ctx:
        for i in range(start, end):
            seg_stripped = [s.strip() for s in lines[i:i + n] if s.strip()]
            if seg_stripped == non_empty_ctx:
                return i, 200
    return -1, 0


def _compute_indent_delta(file_lines: list[str], patch_lines: list[str]) -> str:
    """计算文件行与补丁行的缩进差值（借鉴 aider/editblock_coder.py）"""

    def _leading(s: str) -> str:
        return s[: len(s) - len(s.lstrip())]

    # 取第一个非空行作为参考
    file_indent = ""
    for line in file_lines:
        if line.strip():
            file_indent = _leading(line)
            break
    patch_indent = ""
    for line in patch_lines:
        if line.strip():
            patch_indent = _leading(line)
            break
    # 如果包含 tab，不做调整（避免混合缩进问题）
    if "\t" in file_indent or "\t" in patch_indent:
        return ""
    delta_len = len(file_indent) - len(patch_indent)
    if delta_len > 0:
        return " " * delta_len
    return ""


@dataclass
class FixPatch:
    """修复补丁"""
    file_path: Path
    original_code: str
    fixed_code: str
    explanation: str
    synthetic: bool = False  # True = 非原仓库代码，而是生成的 stub


import re as _re_module

_SR_PATTERN = _re_module.compile(
    r'^(\S[^\n]*?)\n'              # filename (non-empty, starts with non-whitespace)
    r'<<<<<<< SEARCH\n'
    r'(.*?)'                        # search block
    r'=======\n'
    r'(.*?)'                        # replace block
    r'>>>>>>> REPLACE',
    _re_module.MULTILINE | _re_module.DOTALL,
)


def parse_search_replace_blocks(text: str) -> list[FixPatch]:
    """解析 LLM 输出中的 SEARCH/REPLACE 块，返回 FixPatch 列表"""
    patches: list[FixPatch] = []
    for m in _SR_PATTERN.finditer(text):
        patches.append(FixPatch(
            file_path=Path(m.group(1).strip()),
            original_code=m.group(2),
            fixed_code=m.group(3),
            explanation="",
        ))
    return patches


@dataclass
class LayerResult:
    """U4: 统一的验证层结果"""
    layer: str          # "build" | "completeness" | "fidelity"
    passed: bool
    hash: str = ""      # 内容哈希，用于死循环检测
    # Layer-specific
    build_errors: list = None       # list[ValidationError] — all errors including warnings
    real_errors: list = None        # list[ValidationError] — severity != "warning"
    warning_only: bool = False
    missing: list = None            # list[str] — completeness
    hallucinations: list = None     # list[FixPatch] — fidelity


class HealEngine:
    """自愈引擎：三层验证-修复循环"""

    def __init__(self, config: CodePruneConfig, llm: LLMProvider, graph: CodeGraph):
        self.config = config
        self.llm = llm
        self.graph = graph
        self._fix_history: list[tuple[Path, str, str]] = []  # (file, error, attempted_fix)
        self._sr_match_failures: list[tuple[str, str, str]] = []  # (file_key, search_block, actual_snippet)
        self._pre_heal_snapshot: dict[Path, str] = {}  # heal前快照，用于真实性回退
        self._supplemented_files: set[str] = set()  # RuntimeFixer 增补策略修改的文件
        self._build_fixed_files: set[str] = set()  # build 层修复成功涉及的文件（免于 fidelity 回退）
        self._dispatcher: ErrorDispatcher | None = None  # 通用确定性修复调度器

    def heal(self, sub_repo_path: Path) -> bool:
        """
        U4: 反射消息驱动的自愈循环
        validate → fix → re-validate，直到全部通过或死循环/超限
        """
        max_rounds = self.config.heal.max_heal_rounds
        self._fix_history.clear()
        self._tests_copied: bool | None = None  # U8: None=未检测, True/False=结果
        self._unresolved_undefined_names: list[dict] = []  # Phase B 未解决的 undefined names

        # 快照 heal 前状态，用于真实性校验回退到外科产出而非原仓库全文件
        self._pre_heal_snapshot.clear()
        for f in sub_repo_path.rglob("*"):
            if f.is_file():
                try:
                    self._pre_heal_snapshot[f.relative_to(sub_repo_path)] = (
                        f.read_text(encoding="utf-8", errors="replace")
                    )
                except OSError:
                    pass

        logger.info(f"heal 前快照: {len(self._pre_heal_snapshot)} 个文件")

        # 创建通用确定性修复调度器
        excluded = self._get_out_of_scope()
        self._dispatcher = ErrorDispatcher(
            sub_repo=sub_repo_path,
            source_repo=Path(self.config.repo_path),
            graph=self.graph,
            excluded=excluded,
        )

        # SourceRecovery: 统一的原仓库代码恢复器
        self._source_recovery = SourceRecovery(
            repo_path=Path(self.config.repo_path),
            sub_repo_path=sub_repo_path,
            graph=self.graph,
        )

        # Phase 2.5: 预处理清理 — 在 heal 循环前批量清理已知问题（不消耗修复轮次）
        self._pre_heal_cleanup(sub_repo_path)

        # Phase 2.6: SourceRecovery 恢复被审计误注释的行
        if self._source_recovery:
            total_restored = 0
            _CODE_EXTS = {".py", ".c", ".h", ".cpp", ".hpp", ".java", ".js", ".ts"}
            for f in sub_repo_path.rglob("*"):
                if f.is_file() and f.suffix in _CODE_EXTS:
                    rel = f.relative_to(sub_repo_path)
                    total_restored += self._source_recovery.recover_commented_lines(rel)
            if total_restored:
                logger.info(
                    f"SourceRecovery: 预处理恢复 {total_restored} 行被审计误注释的代码"
                )

        prev_hashes: dict[str, str] = {}  # layer → 上轮 hash
        skip_layers: set[str] = set()      # 因死循环/不可修复而跳过的层

        for round_num in range(1, max_rounds + 1):
            logger.info(f"═══ 自愈轮次 {round_num}/{max_rounds} ═══")

            # 分层验证，获取第一个失败层的结果
            result = self._validate_all_layers(sub_repo_path, skip_layers)

            if result is None:
                logger.info("所有验证通过")
                return True

            # 死循环检测：与上轮同层 hash 相同
            if result.hash and result.hash == prev_hashes.get(result.layer):
                if result.layer == "build":
                    logger.warning("错误内容与上轮完全相同，停止修复")
                    break
                elif result.layer == "completeness":
                    logger.warning("完整性缺失列表与上轮相同（补充无效），跳过完整性检查")
                    skip_layers.add("completeness")
                    continue  # 重新验证，跳过完整性 → 进入 fidelity
                elif result.layer == "fidelity":
                    logger.warning("真实性问题与上轮相同，停止修复")
                    break
                elif result.layer == "test":
                    logger.warning("测试失败与上轮相同，跳过测试验证")
                    skip_layers.add("test")
                    continue  # U8: 测试修不动时不阻塞
                elif result.layer == "runtime":
                    logger.warning("Runtime 错误与上轮相同，跳过运行时验证")
                    skip_layers.add("runtime")
                    continue
                elif result.layer == "boot":
                    logger.warning("Boot 错误与上轮相同，跳过启动验证")
                    skip_layers.add("boot")
                    continue
                elif result.layer == "functional":
                    logger.warning("功能验证错误与上轮相同，跳过功能验证")
                    skip_layers.add("functional")
                    continue

            if result.hash:
                prev_hashes[result.layer] = result.hash

            # 尝试修复
            fixed = self._fix_layer(sub_repo_path, result)

            if not fixed:
                if result.layer == "build":
                    if result.warning_only:
                        logger.info(
                            f"编译 warning {len(result.build_errors)} 个"
                            f"（均为可忽略），视为通过"
                        )
                    else:
                        logger.warning("语法修复失败，跳过编译验证")
                    skip_layers.add("build")
                    continue  # 重新验证，跳过 build → 进入 completeness
                elif result.layer == "test":
                    logger.warning("测试修复失败，跳过测试验证")
                    skip_layers.add("test")
                    continue  # U8: 测试修复失败不阻塞
                elif result.layer == "runtime":
                    logger.warning("Runtime 修复失败，跳过运行时验证")
                    skip_layers.add("runtime")
                    continue
                elif result.layer == "boot":
                    logger.warning("Boot 修复失败，跳过启动验证")
                    skip_layers.add("boot")
                    continue
                elif result.layer == "functional":
                    logger.warning("功能验证修复失败，跳过功能验证")
                    skip_layers.add("functional")
                    continue
                # completeness/fidelity fix 失败时继续下一轮（可能部分生效）

        logger.warning(f"达到最大修复轮次 {max_rounds}，自愈结束")
        return False

    # ── U4: Unified Validation & Fix Dispatch ────────────────────────

    def _validate_all_layers(
        self, sub_repo_path: Path, skip_layers: set[str] = None,
    ) -> Optional[LayerResult]:
        """按优先级验证所有层，返回第一个失败层的结果。全部通过返回 None。"""
        skip = skip_layers or set()

        # Layer 1: 编译/语法
        if self.config.heal.enable_build_validation and "build" not in skip:
            lr = self._validate_build(sub_repo_path)
            if lr is not None:
                return lr

        # Layer 1.5: Undefined Names (Phase B) — build 通过后检测残留的 undefined names
        if "undefined_names" not in skip:
            lr = self._validate_undefined_names(sub_repo_path)
            if lr is not None:
                return lr

        # Layer 2.0: Runtime Validation — 确定性 import 扫描 (运行时错误发现+修复循环)
        if getattr(self.config.heal, "enable_runtime_validation", True) and "runtime" not in skip:
            lr = self._validate_runtime(sub_repo_path)
            if lr is not None:
                return lr

        # Layer 2.5: Boot Validation — 能否最小启动
        if getattr(self.config.heal, "enable_boot_validation", True) and "boot" not in skip:
            lr = self._validate_boot(sub_repo_path)
            if lr is not None:
                return lr

        # Layer 2: 功能完整性
        if self.config.heal.enable_completeness_check and "completeness" not in skip:
            lr = self._validate_completeness(sub_repo_path)
            if lr is not None:
                return lr

        # Layer 3: 真实性
        if self.config.heal.enable_fidelity_check and "fidelity" not in skip:
            lr = self._validate_fidelity(sub_repo_path)
            if lr is not None:
                return lr

        # Layer 3.5: Functional Validation — 核心业务路径可用性
        if getattr(self.config.heal, "enable_functional_validation", False) and "functional" not in skip:
            lr = self._validate_functional(sub_repo_path)
            if lr is not None:
                return lr

        # Layer 4: 测试 (U8) — 只在前三层全部通过后运行
        if self.config.heal.enable_test_validation and "test" not in skip:
            lr = self._validate_test(sub_repo_path)
            if lr is not None:
                return lr

        return None  # 全部通过

    def _validate_build(self, sub_repo_path: Path) -> Optional[LayerResult]:
        """Layer 1: 编译验证 → LayerResult 或 None（支持增量编译）"""
        lang = self._detect_primary_language(sub_repo_path)
        # 复用上轮的 mtime 缓存以支持增量编译
        prev_mtimes = getattr(self, '_build_clean_mtimes', None) or {}
        validator = BuildValidator(self.config.heal, sub_repo_path, lang,
                                  prev_clean_mtimes=prev_mtimes)
        result = validator.validate()
        # 保存本轮的 mtime 缓存供下轮使用
        self._build_clean_mtimes = validator.clean_mtimes

        if result.success:
            return None

        real_errors = [e for e in result.errors if e.severity != "warning"]
        warning_only = len(real_errors) == 0

        error_hash = ""
        if real_errors:
            error_text = "\n".join(sorted(e.message for e in real_errors))
            error_hash = hashlib.md5(error_text.encode()).hexdigest()
            logger.info(f"编译错误: {len(real_errors)} 个")

        return LayerResult(
            layer="build", passed=False, hash=error_hash,
            build_errors=result.errors, real_errors=real_errors,
            warning_only=warning_only,
        )

    def _validate_undefined_names(self, sub_repo_path: Path) -> Optional[LayerResult]:
        """Layer 1.5: Undefined Names 验证 — 检测并自动修复残留的 undefined names

        依次执行:
        1. UndefinedNameResolver 扫描 + 自动补 import (fixable)
        2. 剩余 llm_required 的报告为 LayerResult

        第一轮: 复用 _pre_heal_cleanup 已扫描的结果
        后续轮: 重新扫描（LLM 可能已修复部分）
        """
        if self._unresolved_undefined_names is not None and len(self._unresolved_undefined_names) > 0:
            # 复用 _pre_heal_cleanup 或上一轮的结果，避免重复扫描
            unresolved = self._unresolved_undefined_names
            self._unresolved_undefined_names = []  # 消费掉，下轮需重新扫描
        else:
            # 重新扫描（LLM 可能已修复部分 undefined names）
            resolver = UndefinedNameResolver(
                sub_repo_path, self.graph,
            )
            auto_fixed, unresolved = resolver.resolve_all()

        if not unresolved:
            return None

        # 将 unresolved 转化为 ValidationError 列表
        errors = [
            ValidationError(
                file_path=Path(item["file"]),
                line=item["line"],
                message=f"undefined name '{item['name']}' ({item['classification']})",
                severity="error" if item["classification"] == "llm_required" else "warning",
            )
            for item in unresolved
        ]

        real_errors = [e for e in errors if e.severity != "warning"]
        if not real_errors:
            return None

        error_hash = hashlib.md5(
            "\n".join(sorted(e.message for e in real_errors)).encode()
        ).hexdigest()
        logger.info(f"Undefined names: {len(real_errors)} 个需 LLM 修复")

        return LayerResult(
            layer="undefined_names", passed=False, hash=error_hash,
            build_errors=errors, real_errors=real_errors,
        )

    def _validate_runtime(self, sub_repo_path: Path) -> Optional[LayerResult]:
        """Layer 2.0: 确定性运行时验证 — import 扫描所有 Python 模块"""
        from core.graph.schema import Language
        lang = self._detect_primary_language(sub_repo_path)
        if lang != Language.PYTHON:
            return None  # 非 Python 暂不支持, 跳过

        from core.heal.runtime_validator import RuntimeValidator
        timeout = getattr(self.config.heal, "runtime_import_timeout", 15)
        validator = RuntimeValidator(sub_repo_path, timeout=timeout)
        result = validator.validate()
        self._runtime_result = result

        if result.success:
            logger.info(
                f"Runtime validation: 全部通过 "
                f"({result.modules_passed}/{result.modules_tested} modules)"
            )
            return None

        # 过滤 SyntaxError (已由 build layer 处理)
        real_errors = [e for e in result.errors if e.error_type != "SyntaxError"]
        if not real_errors:
            return None  # 只剩 SyntaxError, 留给 build 层

        # 转为 ValidationError 列表
        errors = []
        for rt_err in real_errors:
            errors.append(ValidationError(
                file_path=rt_err.file_path or Path("(runtime)"),
                line=rt_err.line,
                message=f"[runtime] {rt_err.error_type}: {rt_err.message}",
                severity="error",
            ))

        error_hash = hashlib.md5(
            "\n".join(sorted(e.message for e in errors)).encode()
        ).hexdigest()
        logger.info(
            f"Runtime validation: {len(real_errors)} 个错误 "
            f"({result.modules_passed}/{result.modules_tested} modules passed)"
        )

        return LayerResult(
            layer="runtime", passed=False, hash=error_hash,
            build_errors=errors, real_errors=errors,
        )

    def _validate_boot(self, sub_repo_path: Path) -> Optional[LayerResult]:
        """Layer 2.5: 启动验证 — 检测子仓库能否最小启动"""
        from core.heal.boot_validator import BootValidator, BootResult

        lang = self._detect_primary_language(sub_repo_path)
        validator = BootValidator(
            self.config.heal, sub_repo_path, lang, self.llm, self.graph,
        )
        result = validator.validate()
        self._boot_result = result

        if result.success:
            return None

        # 将 boot 错误转为 ValidationError 列表
        errors = []
        for err_msg in result.boot_errors:
            errors.append(ValidationError(
                file_path=result.error_file or Path("(boot)"),
                line=result.error_line or 0,
                message=f"[boot] {result.error_type}: {err_msg}",
                severity="error",
            ))

        error_hash = hashlib.md5(
            "\n".join(sorted(e.message for e in errors)).encode()
        ).hexdigest()
        logger.info(f"Boot validation 失败: {result.error_type} ({len(errors)} 个错误)")

        return LayerResult(
            layer="boot", passed=False, hash=error_hash,
            build_errors=errors, real_errors=errors,
        )

    def _validate_completeness(self, sub_repo_path: Path) -> Optional[LayerResult]:
        """Layer 2: 完整性验证 → LayerResult 或 None"""
        missing = self._check_completeness(sub_repo_path)
        if not missing:
            return None

        missing_hash = hashlib.md5(str(sorted(missing)).encode()).hexdigest()
        logger.info(f"功能不完整: 缺少 {missing}")

        return LayerResult(
            layer="completeness", passed=False, hash=missing_hash,
            missing=missing,
        )

    def _validate_fidelity(self, sub_repo_path: Path) -> Optional[LayerResult]:
        """Layer 3: 真实性验证 → LayerResult 或 None"""
        hallucinations = self._check_fidelity(sub_repo_path)
        if not hallucinations:
            return None

        h_hash = hashlib.md5(
            str(sorted(h.file_path.as_posix() for h in hallucinations)).encode()
        ).hexdigest()
        logger.info(f"发现 {len(hallucinations)} 处非原仓库代码")

        return LayerResult(
            layer="fidelity", passed=False, hash=h_hash,
            hallucinations=hallucinations,
        )

    def _validate_functional(self, sub_repo_path: Path) -> Optional[LayerResult]:
        """Layer 3.5: 功能验证 — 两阶段验证核心业务路径"""
        from core.heal.functional_validator import FunctionalValidator, FunctionalResult

        lang = self._detect_primary_language(sub_repo_path)
        source_repo_path = Path(self.config.repo_path)
        validator = FunctionalValidator(
            self.config.heal, sub_repo_path, source_repo_path,
            lang, self.llm, self.graph,
            user_instruction=getattr(self.config, "user_instruction", ""),
        )
        result = validator.validate()
        self._functional_result = result

        if result.success:
            return None

        errors = []
        for err_msg in result.errors:
            errors.append(ValidationError(
                file_path=result.error_file or Path("(functional)"),
                line=result.error_line or 0,
                message=f"[functional] {result.error_type}: {err_msg}",
                severity="error",
            ))

        error_hash = hashlib.md5(
            "\n".join(sorted(e.message for e in errors)).encode()
        ).hexdigest()
        logger.info(
            f"Functional validation 失败: {result.error_type} ({len(errors)} 个错误)"
        )

        return LayerResult(
            layer="functional", passed=False, hash=error_hash,
            build_errors=errors, real_errors=errors,
        )

    # ── U8: Test Validation ──────────────────────────────────────────

    def _validate_test(self, sub_repo_path: Path) -> Optional[LayerResult]:
        """Layer 4: 测试验证 — 检测并运行原仓库测试"""
        # 惰性复制: 只在第一次调用时把相关测试复制到子仓库
        if self._tests_copied is None:
            self._tests_copied = self._copy_relevant_tests(sub_repo_path)
            if not self._tests_copied:
                return None  # 无可用测试，视为通过

        if not self._tests_copied:
            return None

        test_errors = self._execute_tests(sub_repo_path)
        if not test_errors:
            logger.info("测试全部通过")
            return None

        error_hash = hashlib.md5(
            "\n".join(sorted(e.message for e in test_errors)).encode()
        ).hexdigest()
        logger.info(f"测试失败: {len(test_errors)} 个错误")

        return LayerResult(
            layer="test", passed=False, hash=error_hash,
            build_errors=test_errors,
            real_errors=test_errors,
        )

    def _copy_relevant_tests(self, sub_repo_path: Path) -> bool:
        """从原仓库复制与保留模块相关的测试文件到子仓库"""
        import re as _re
        import shutil

        repo = self.config.repo_path
        # 收集子仓库中已有的源文件（用于判断 test 相关性）
        sub_files: set[str] = set()
        for f in sub_repo_path.rglob("*"):
            if f.is_file() and f.suffix in (".py", ".c", ".h", ".java", ".ts", ".js"):
                sub_files.add(f.stem)
                sub_files.add(f.name)

        if not sub_files:
            return False

        # 扫描原仓库的测试文件
        test_patterns = _re.compile(
            r"^test_\w+\.\w+$|^\w+_test\.\w+$|^\w+Test\.\w+$|"
            r"^\w+\.test\.\w+$|^\w+\.spec\.\w+$",
            _re.IGNORECASE,
        )
        test_dirs = {"tests", "test", "__tests__", "spec"}

        test_files: list[Path] = []
        for f in repo.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix not in (".py", ".c", ".java", ".ts", ".js"):
                continue
            # 跳过 node_modules, .git 之类
            parts = set(f.relative_to(repo).parts)
            if parts & {"node_modules", ".git", "__pycache__", "venv", ".venv"}:
                continue
            # 匹配测试文件名 或 在测试目录下
            is_test = bool(test_patterns.match(f.name))
            is_in_test_dir = bool(parts & test_dirs)
            if is_test or is_in_test_dir:
                test_files.append(f)

        if not test_files:
            logger.debug("原仓库中未发现测试文件")
            return False

        # 过滤: 只保留与子仓库模块相关的测试
        relevant: list[Path] = []
        for tf in test_files:
            try:
                content = tf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # 检查测试是否引用了子仓库中的模块
            for name in sub_files:
                if name in content:
                    relevant.append(tf)
                    break

        if not relevant:
            logger.debug(f"发现 {len(test_files)} 个测试文件但无相关测试")
            return False

        # 复制相关测试到子仓库 (保持相对路径)
        copied = 0
        already_exist = 0
        for tf in relevant:
            rel = tf.relative_to(repo)
            dst = sub_repo_path / rel
            if dst.exists():
                already_exist += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tf, dst)
            copied += 1

        total = copied + already_exist
        if total:
            parts = []
            if copied:
                parts.append(f"复制 {copied}")
            if already_exist:
                parts.append(f"已有 {already_exist}")
            logger.info(f"U8 测试文件: {', '.join(parts)} ({', '.join(f.name for f in relevant)})")
        return total > 0

    def _execute_tests(self, sub_repo_path: Path) -> list[ValidationError]:
        """运行测试并收集失败信息。返回 ValidationError 列表 (空=全部通过)。"""
        import subprocess
        import re as _re

        lang = self._detect_primary_language(sub_repo_path)
        errors: list[ValidationError] = []

        from core.graph.schema import Language

        if lang == Language.PYTHON:
            errors = self._run_python_tests(sub_repo_path)
        elif lang == Language.C:
            errors = self._run_c_tests(sub_repo_path)
        elif lang == Language.JAVA:
            errors = self._run_java_tests(sub_repo_path)
        else:
            logger.debug(f"U8: 语言 {lang} 的测试运行暂不支持")

        return errors

    def _run_python_tests(self, sub_repo_path: Path) -> list[ValidationError]:
        """运行 Python 测试 (pytest / unittest)"""
        import subprocess
        import sys

        # 优先 pytest, 回退 unittest
        test_files = list(sub_repo_path.rglob("test_*.py")) + \
                     list(sub_repo_path.rglob("*_test.py"))
        if not test_files:
            return []

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "--tb=short", "-q"] +
                [str(f) for f in test_files],
                cwd=str(sub_repo_path),
                capture_output=True, text=True, timeout=60,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "unittest", "discover", "-s", str(sub_repo_path)],
                    cwd=str(sub_repo_path),
                    capture_output=True, text=True, timeout=60,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError):
                return []

        if result.returncode == 0:
            return []

        return self._parse_test_output(result.stdout + result.stderr, sub_repo_path)

    def _run_c_tests(self, sub_repo_path: Path) -> list[ValidationError]:
        """运行 C 测试 (make test)"""
        import subprocess

        makefile = sub_repo_path / "Makefile"
        if not makefile.exists():
            return []

        try:
            content = makefile.read_text(encoding="utf-8")
            if "test:" not in content and "test :" not in content:
                return []
        except OSError:
            return []

        try:
            result = subprocess.run(
                ["make", "test"],
                cwd=str(sub_repo_path),
                capture_output=True, text=True, timeout=60,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

        if result.returncode == 0:
            return []

        return self._parse_test_output(result.stdout + result.stderr, sub_repo_path)

    def _run_java_tests(self, sub_repo_path: Path) -> list[ValidationError]:
        """运行 Java 测试 (编译并运行测试类)"""
        import subprocess

        test_files = list(sub_repo_path.rglob("*Test.java")) + \
                     list(sub_repo_path.rglob("*Tests.java"))
        if not test_files:
            return []

        # 编译测试
        all_java = list(sub_repo_path.rglob("*.java"))
        try:
            result = subprocess.run(
                ["javac", "-cp", str(sub_repo_path)] +
                [str(f) for f in all_java],
                cwd=str(sub_repo_path),
                capture_output=True, text=True, timeout=60,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

        if result.returncode != 0:
            return self._parse_test_output(result.stderr, sub_repo_path)

        # 运行测试
        errors: list[ValidationError] = []
        for tf in test_files:
            rel = tf.relative_to(sub_repo_path)
            class_name = str(rel).replace("/", ".").replace("\\", ".").removesuffix(".java")
            try:
                run_result = subprocess.run(
                    ["java", "-cp", str(sub_repo_path), class_name],
                    cwd=str(sub_repo_path),
                    capture_output=True, text=True, timeout=30,
                )
                if run_result.returncode != 0:
                    errors.extend(
                        self._parse_test_output(
                            run_result.stdout + run_result.stderr, sub_repo_path
                        )
                    )
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

        return errors

    @staticmethod
    def _parse_test_output(output: str, sub_repo_path: Path) -> list[ValidationError]:
        """从测试输出中提取失败信息为 ValidationError"""
        import re as _re
        errors: list[ValidationError] = []
        if not output.strip():
            return errors

        # 通用: 每一行 FAIL/ERROR/FAILED
        for m in _re.finditer(
            r'(?:FAIL|ERROR|FAILED)\s*[:\[]?\s*(.+)',
            output, _re.IGNORECASE,
        ):
            msg = m.group(0).strip()[:300]
            # 尝试提取文件和行号
            file_match = _re.search(r'([\w/\\]+\.\w+):(\d+)', msg)
            fp = Path(file_match.group(1)) if file_match else Path("tests")
            line = int(file_match.group(2)) if file_match else 0
            errors.append(ValidationError(
                file_path=fp, line=line, message=msg, severity="error",
            ))

        # 如果没匹配到具体错误但 output 非空且有失败迹象
        if not errors and any(kw in output.lower() for kw in ("fail", "error", "assert")):
            # 截取最后 500 字符作为错误消息
            errors.append(ValidationError(
                file_path=Path("tests"),
                line=0,
                message=f"Test failures:\n{output[-500:]}",
                severity="error",
            ))

        return errors

    def _fix_layer(self, sub_repo_path: Path, result: LayerResult) -> bool:
        """根据 LayerResult 分发到对应的修复策略。返回 True 如果有任何修复。"""
        if result.layer == "build":
            grouped = self._group_errors_by_file(result.build_errors)
            return self._fix_syntax_errors(sub_repo_path, grouped)

        elif result.layer == "completeness":
            self._supplement_missing(sub_repo_path, result.missing)
            return True  # supplement 总是尝试修复

        elif result.layer == "fidelity":
            self._revert_hallucinations(sub_repo_path, result.hallucinations)
            return True  # revert 总是执行

        elif result.layer == "test":
            # U8: 测试失败 → 按 build error 流程修复
            grouped = self._group_errors_by_file(result.build_errors)
            return self._fix_syntax_errors(sub_repo_path, grouped)

        elif result.layer == "undefined_names":
            # Phase C: undefined names → 带 CodeGraph 上下文的专用修复
            return self._fix_undefined_names(sub_repo_path, result.build_errors)

        elif result.layer == "runtime":
            # Layer 2.0: 运行时错误 → 确定性修复 (不依赖 LLM)
            return self._fix_runtime_errors(sub_repo_path)

        elif result.layer == "boot":
            # Layer 2.5: boot 错误 → 用 boot 错误上下文 + build 修复流程
            from core.heal.boot_validator import BootValidator
            boot_result = getattr(self, "_boot_result", None)
            if boot_result:
                # 格式化 boot 错误为富文本，合并到 build_errors 修复
                formatted = BootValidator.format_boot_error(boot_result, sub_repo_path)
                logger.info(f"Boot fix context:\n{formatted[:500]}")
            grouped = self._group_errors_by_file(result.build_errors)
            return self._fix_syntax_errors(sub_repo_path, grouped)

        elif result.layer == "functional":
            # Layer 3.5: functional 错误 → 用功能错误上下文 + build 修复流程
            from core.heal.functional_validator import FunctionalValidator
            func_result = getattr(self, "_functional_result", None)
            if func_result:
                formatted = FunctionalValidator.format_functional_error(
                    func_result, sub_repo_path,
                )
                logger.info(f"Functional fix context:\n{formatted[:500]}")
            grouped = self._group_errors_by_file(result.build_errors)
            return self._fix_syntax_errors(sub_repo_path, grouped)

    # ── Phase C: Undefined Name 专用 LLM 修复 ────────────────────────

    def _fix_undefined_names(
        self, sub_repo_path: Path, errors: list[ValidationError],
    ) -> bool:
        """Phase C: 用 CodeGraph 上下文指导 LLM 修复 undefined names

        与通用 _fix_syntax_errors 的关键差异:
        1. 专用 prompt (FIX_UNDEFINED_NAMES): 提供 CodeGraph 中该名称的定义信息
        2. 按文件分组: 同一文件的多个 undefined names 一次性修复
        3. 批量补丁: LLM 返回 fixes 数组，逐一应用
        """
        # 按文件分组
        by_file: dict[str, list[ValidationError]] = {}
        for err in errors:
            key = str(err.file_path)
            by_file.setdefault(key, []).append(err)

        any_fixed = False

        for file_key, file_errors in by_file.items():
            file_path = sub_repo_path / file_key
            if not file_path.exists():
                continue

            try:
                file_content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            # 提取每个 undefined name 的详细信息
            undefined_names_detail = self._format_undefined_names(file_content, file_errors)

            # 从 CodeGraph 获取这些名称的上下文
            names = self._extract_names_from_errors(file_errors)
            graph_context = self._get_graph_context_for_names(names)

            # 子仓库可用模块列表
            available_modules = self._list_available_modules(sub_repo_path)

            # 原仓库上下文
            original_context = self._get_original_context(Path(file_key))

            # reflected_message: 之前失败的修复
            prev_attempts = [
                f"- 尝试: {fix} → 仍然报错"
                for fp, _err, fix in self._fix_history
                if str(fp) == file_key
            ]
            reflected = ""
            if prev_attempts:
                reflected = (
                    "\n\n⚠️ PREVIOUS FAILED ATTEMPTS on this file (do NOT repeat):\n"
                    + "\n".join(prev_attempts[-3:])
                )

            prompt = Prompts.FIX_UNDEFINED_NAMES.format(
                file_path=file_key,
                undefined_names_detail=undefined_names_detail,
                file_content=file_content[:6000],
                graph_context=graph_context[:4000],
                available_modules=available_modules[:2000],
                original_context=original_context[:4000],
            ) + reflected

            try:
                result = self.llm.chat_json([{"role": "user", "content": prompt}])
                fixes = result.get("fixes", [])
                if not fixes:
                    continue

                for fix_item in fixes:
                    patch = FixPatch(
                        file_path=Path(file_key),
                        original_code=fix_item.get("original_code", ""),
                        fixed_code=fix_item.get("fixed_code", ""),
                        explanation=fix_item.get("explanation", ""),
                    )
                    self._validate_patch_safety(patch)
                    if self._apply_patch(sub_repo_path, patch):
                        any_fixed = True
                        self._fix_history.append(
                            (Path(file_key), "undefined names", patch.explanation)
                        )
            except Exception as e:
                logger.warning(f"Undefined names LLM 修复失败 ({file_key}): {e}")

        return any_fixed

    # ── Layer 2.0: Runtime Error 确定性修复 ─────────────────────────

    def _fix_runtime_errors(self, sub_repo_path: Path) -> bool:
        """Layer 2.0: 使用 RuntimeFixer 确定性修复运行时 import 错误

        不依赖 LLM — 直接操作 AST/文本:
        - ImportError → 移除 __init__.py 中的失效 re-export
        - ModuleNotFoundError → 注释 import 或从原仓库补充文件
        - AttributeError → 注释出错行
        """
        from core.heal.runtime_validator import RuntimeFixer

        runtime_result = getattr(self, "_runtime_result", None)
        if not runtime_result or not runtime_result.errors:
            return False

        # 过滤 SyntaxError (已由 build layer 处理)
        fixable = [e for e in runtime_result.errors if e.error_type != "SyntaxError"]
        if not fixable:
            return False

        excluded = self._get_out_of_scope()
        fixer = RuntimeFixer(
            sub_repo_path,
            source_repo_path=Path(self.config.repo_path),
            excluded_modules=excluded,
            graph=self.graph,
        )

        fixed_count = fixer.fix(fixable)
        if fixed_count:
            logger.info(
                f"Runtime fix: 确定性修复 {fixed_count}/{len(fixable)} 个运行时错误"
            )
        # 记录增补策略修改的文件，避免 fidelity 回退
        self._supplemented_files.update(fixer._supplemented_files)
        return fixed_count > 0

    def _format_undefined_names(
        self, file_content: str, errors: list[ValidationError],
    ) -> str:
        """格式化 undefined names 列表，含上下文行"""
        lines = file_content.splitlines()
        parts: list[str] = []
        for err in errors:
            # 从错误消息中提取名称
            name = "?"
            import re
            m = re.search(r"undefined name '(\w+)'", err.message)
            if m:
                name = m.group(1)

            line_idx = (err.line or 1) - 1
            # 展示上下文: ±2 行
            start = max(0, line_idx - 2)
            end = min(len(lines), line_idx + 3)
            context_lines = []
            for i in range(start, end):
                marker = "█ " if i == line_idx else "  "
                context_lines.append(f"  {marker}{i+1:4d}| {lines[i]}")

            parts.append(
                f"- `{name}` at line {err.line}:\n" + "\n".join(context_lines)
            )
        return "\n".join(parts)

    def _extract_names_from_errors(self, errors: list[ValidationError]) -> list[str]:
        """从 error messages 中提取 undefined name 列表"""
        import re
        names = []
        for err in errors:
            m = re.search(r"undefined name '(\w+)'", err.message)
            if m:
                names.append(m.group(1))
        return names

    def _get_graph_context_for_names(self, names: list[str]) -> str:
        """从 CodeGraph 获取名称的定义信息"""
        from core.graph.schema import NodeType as NT
        parts: list[str] = []
        symbol_types = {NT.FUNCTION, NT.CLASS, NT.INTERFACE, NT.ENUM}

        for name in names:
            found = []
            for node in self.graph.nodes.values():
                if node.name == name and node.node_type in symbol_types:
                    loc = f"{node.file_path}:{node.start_line}" if node.file_path else "unknown"
                    summary = node.summary or ""
                    found.append(f"  - {node.node_type.value} `{node.name}` at {loc}")
                    if summary:
                        found[-1] += f" — {summary[:80]}"

            if found:
                parts.append(f"`{name}` found in original repo:\n" + "\n".join(found[:5]))
            else:
                parts.append(f"`{name}`: NOT found in CodeGraph (may be third-party or dynamic)")

        return "\n\n".join(parts) if parts else "No CodeGraph context available."

    def _list_available_modules(self, sub_repo_path: Path) -> str:
        """列出子仓库中可用的 Python 模块"""
        modules: list[str] = []
        for py_file in sorted(sub_repo_path.rglob("*.py")):
            rel = py_file.relative_to(sub_repo_path)
            mod = str(rel.with_suffix("")).replace("\\", "/").replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[:-9]
            modules.append(mod)
        if not modules:
            return "No Python modules found."
        return "Available modules:\n" + "\n".join(f"  - {m}" for m in modules[:50])

    # ── Phase 2.5: Pre-heal Cleanup ──────────────────────────────────

    def _pre_heal_cleanup(self, sub_repo_path: Path) -> None:
        """Phase 2.5: heal 循环前批量清理已知 out_of_scope import（不消耗修复轮次）

        Python 文件使用 ImportFixer (AST 精确修复) + CascadeCleaner (级联清理)；
        其他语言沿用 regex 注释策略。
        """
        import re as _re
        excluded = self._get_out_of_scope()
        if not excluded:
            return

        # ── Python: 精确 AST 修复 ──
        has_python = any(f.suffix == ".py" for f in sub_repo_path.rglob("*") if f.is_file())
        if has_python:
            fixer = ImportFixer(sub_repo_path, excluded)
            fixed_count, removed_names = fixer.fix_all()

            if removed_names:
                cleaner = CascadeCleaner(sub_repo_path)
                cleaner.clean_all(removed_names)

            # Phase B: 自动补全可修复的 undefined names
            resolver = UndefinedNameResolver(
                sub_repo_path, self.graph, removed_names=removed_names,
            )
            auto_fixed, unresolved = resolver.resolve_all()
            # 存储未解决的，供 Layer 1.5 使用
            self._unresolved_undefined_names = unresolved

        # ── Phase 2.6: 引用审计 — 清理对已删除模块/符号的非 import 引用 ──
        if getattr(self.config.heal, "enable_reference_audit", True):
            from core.heal.reference_audit import ReferenceAuditor
            language = self._detect_primary_language(sub_repo_path)
            auditor = ReferenceAuditor(
                sub_repo_path, self.graph, excluded, self.llm, language,
            )
            self._audit_report = auditor.audit_and_fix()
        else:
            self._audit_report = None

        # ── 非 Python: regex 注释（Java/TS/JS/C/C++）──
        excluded_tokens: set[str] = set()
        for ex in excluded:
            ex_norm = ex.replace("\\", "/").rstrip("/")
            excluded_tokens.add(ex_norm.replace("/", ".").removesuffix(".py"))
            parts = ex_norm.split("/")
            if len(parts) == 1:
                excluded_tokens.add(parts[0].removesuffix(".py"))
            else:
                # 不再添加顶级目录名 — 避免将同包下所有子模块错误标记为 excluded
                if "." in parts[-1]:
                    excluded_tokens.add(parts[-1].rsplit(".", 1)[0])

        total_cleaned = 0
        _NON_PY_EXTS = {".java", ".js", ".ts", ".c", ".cpp", ".h", ".hpp"}

        for code_file in sorted(sub_repo_path.rglob("*")):
            if not code_file.is_file() or code_file.suffix not in _NON_PY_EXTS:
                continue
            try:
                lines = code_file.read_text(encoding="utf-8").splitlines(keepends=True)
            except OSError:
                continue

            lines_to_comment: list[int] = []
            suffix = code_file.suffix

            for idx, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("// [CodePrune]"):
                    continue

                matched = False

                if suffix == ".java":
                    m = _re.match(r'^import\s+([\w.*]+)\s*;', stripped)
                    if m:
                        pkg = m.group(1)
                        parts = pkg.split(".")
                        if any(p in excluded_tokens for p in parts):
                            matched = True

                elif suffix in (".ts", ".js"):
                    m = _re.search(r'''(?:from|require\()\s*['"]([^'"]+)['"]''', stripped)
                    if m:
                        path = m.group(1).replace("\\", "/")
                        segments = path.split("/")
                        for seg in segments:
                            clean = seg.removesuffix(".js").removesuffix(".ts")
                            if clean in excluded_tokens:
                                matched = True
                                break

                elif suffix in (".c", ".cpp", ".h", ".hpp"):
                    m = _re.match(r'^#include\s+"([^"]+)"', stripped)
                    if m:
                        inc_path = m.group(1).replace("\\", "/")
                        for seg in inc_path.split("/"):
                            clean = seg.removesuffix(".h").removesuffix(".hpp").removesuffix(".c").removesuffix(".cpp")
                            if clean in excluded_tokens:
                                matched = True
                                break

                if matched:
                    lines_to_comment.append(idx)

            if lines_to_comment:
                comment_prefix = self._comment_prefix(suffix)
                for idx in lines_to_comment:
                    original = lines[idx].rstrip()
                    lines[idx] = f"{comment_prefix} [CodePrune] removed: {original}\n"
                try:
                    code_file.write_text("".join(lines), encoding="utf-8")
                    rel = code_file.relative_to(sub_repo_path)
                    total_cleaned += len(lines_to_comment)
                    logger.debug(f"预处理: 注释 {len(lines_to_comment)} 行 out_of_scope import — {rel}")
                except OSError:
                    pass

        if total_cleaned:
            logger.info(f"Phase 2.5 预处理(非Python): 共注释 {total_cleaned} 行 out_of_scope import")

    @staticmethod
    def _comment_prefix(suffix: str) -> str:
        """语言对应的行注释符"""
        if suffix in (".py",):
            return "#"
        elif suffix in (".java", ".js", ".ts", ".c", ".cpp", ".h", ".hpp"):
            return "//"
        return "#"

    @staticmethod
    def _expand_multiline_import(lines: list[str], start_idx: int) -> set[int]:
        """检测多行 import 语句，返回需要注释的所有行号（0-based）。
        如果 start_idx 行包含未闭合的 ( ，则向下扫描直到 ) 闭合。"""
        result = {start_idx}
        if start_idx < 0 or start_idx >= len(lines):
            return result
        line = lines[start_idx]
        open_count = line.count("(") - line.count(")")
        if open_count <= 0:
            return result
        idx = start_idx + 1
        while idx < len(lines) and open_count > 0:
            result.add(idx)
            open_count += lines[idx].count("(") - lines[idx].count(")")
            idx += 1
        return result

    def _detect_primary_language(self, sub_repo_path: Path) -> "Language":
        """检测子仓库的主要语言"""
        from core.graph.schema import Language
        from collections import Counter
        counter = Counter()
        for f in sub_repo_path.rglob("*"):
            if f.is_file():
                lang = Language.from_extension(f.suffix)
                if lang != Language.UNKNOWN:
                    counter[lang] += 1
        if counter:
            return counter.most_common(1)[0][0]
        return Language.UNKNOWN

    def _group_errors_by_file(self, errors: list[ValidationError]) -> list[ValidationError]:
        """按文件分组错误，import 拓扑排序：被依赖文件优先修复"""
        from collections import defaultdict
        by_file: dict[Path, list[ValidationError]] = defaultdict(list)
        for e in errors:
            by_file[e.file_path].append(e)

        grouped = []
        for file_path, file_errors in by_file.items():
            combined_msg = "\n".join(e.message for e in file_errors)
            grouped.append(ValidationError(
                file_path=file_path,
                line=file_errors[0].line,
                message=combined_msg,
            ))

        # 拓扑排序：被更多文件 import 的文件优先修复
        import_count: dict[Path, int] = defaultdict(int)
        for node in self.graph.nodes.values():
            if node.file_path:
                from core.graph.schema import EdgeType
                for edge in self.graph.get_outgoing(node.id):
                    if edge.edge_type == EdgeType.IMPORTS:
                        target = self.graph.get_node(edge.target)
                        if target and target.file_path:
                            import_count[target.file_path] += 1

        # 排序: 被依赖最多的优先 → "cannot find" 错误优先
        grouped.sort(key=lambda e: (
            -import_count.get(e.file_path, 0),
            0 if "cannot find" in e.message.lower() or "no module" in e.message.lower() else 1,
        ))
        return grouped

    def _fix_syntax_errors(self, sub_repo_path: Path, errors: list[ValidationError]) -> bool:
        """两阶段修复: Phase1 LLM先行(SR批量) → Phase2 Dispatcher殿后"""
        any_fixed = False
        capped = errors[:20]  # 每轮最多修复20个错误
        lang = self._detect_primary_language(sub_repo_path)

        # 记录 build 修复涉及的文件（用于 fidelity 豁免）
        for err in capped:
            if err.file_path:
                self._build_fixed_files.add(str(err.file_path).replace("\\", "/"))

        # ── Phase 0: 分类错误 ──────────────────────────────────────
        # 给 LLM 保留最低配额: Dispatcher 最多处理 7 个, 至少 3 个留给 LLM
        max_dispatcher = max(len(capped) - 3, 0)
        dispatcher_errors: list[ValidationError] = []
        llm_errors: list[ValidationError] = []
        for error in capped:
            if (self._dispatcher and self._dispatcher.can_fix(error, lang)
                    and len(dispatcher_errors) < max_dispatcher):
                dispatcher_errors.append(error)
            else:
                llm_errors.append(error)

        # ── Phase 0.5: SourceRecovery 确定性恢复 ─────────────────
        # 在 LLM 和 Dispatcher 之前，尝试从原仓库精确恢复缺失符号/文件
        sr_recovered: list[ValidationError] = []
        sr_remaining: list[ValidationError] = []
        if self._source_recovery:
            for error in llm_errors:
                if self._source_recovery.try_recover_from_error(
                    error.message, error.file_path,
                ):
                    sr_recovered.append(error)
                    any_fixed = True
                else:
                    sr_remaining.append(error)
            if sr_recovered:
                logger.info(
                    f"SourceRecovery: 确定性恢复 {len(sr_recovered)} 个错误"
                )
            llm_errors = sr_remaining

        # ── Phase 1: LLM 修复先行 ─────────────────────────────────
        # 先尝试确定性 import 修复，过滤掉已解决的错误
        remaining_llm_errors: list[ValidationError] = []
        for error in llm_errors:
            if self._try_fix_missing_import(sub_repo_path, error):
                any_fixed = True
            else:
                remaining_llm_errors.append(error)

        # 批量 SEARCH/REPLACE: 将剩余 LLM 错误打包为一次调用
        if remaining_llm_errors:
            sr_patches = self._generate_batch_fix_sr(sub_repo_path, remaining_llm_errors)
            if sr_patches:
                applied = self._apply_patches_reverse(sub_repo_path, sr_patches)
                if applied > 0:
                    any_fixed = True
                    logger.info(f"SR batch: 成功应用 {applied}/{len(sr_patches)} 个补丁")

            # 回退: SR 未产出补丁时，逐个 JSON 修复（兼容旧路径）
            if not sr_patches:
                logger.info("SR batch 无结果，回退到逐个 JSON 修复")
                architect_hints: dict[str, list[dict]] = {}
                if len(remaining_llm_errors) >= 3:
                    architect_hints = self._architect_analyze(
                        sub_repo_path, remaining_llm_errors
                    )
                for error in remaining_llm_errors:
                    patch = self._generate_fix(
                        sub_repo_path, error, architect_hints=architect_hints
                    )
                    if patch:
                        success = self._apply_patch(sub_repo_path, patch)
                        if success:
                            any_fixed = True
                            self._fix_history.append(
                                (error.file_path, error.message, patch.explanation)
                            )
                            continue
                    if self._try_generate_stub(sub_repo_path, error):
                        any_fixed = True

        # ── Phase 1.5: 恢复被 LLM 删除的受保护 include ────────────
        if self._dispatcher:
            restored = self._dispatcher.reapply_protected_includes()
            if restored:
                logger.info(f"Dispatcher 恢复了 {restored} 个被 LLM 移除的 include")

        # ── Phase 2: Dispatcher 确定性修复（LLM 补丁之后，不会被覆盖）──
        for error in dispatcher_errors:
            if self._dispatcher and self._dispatcher.try_fix(error, lang):
                any_fixed = True
                self._supplemented_files.update(self._dispatcher.supplemented_files)

        return any_fixed

    # ── U7: Architect Pattern ────────────────────────────────────────

    def _architect_analyze(
        self, sub_repo_path: Path, errors: list[ValidationError],
    ) -> dict[str, list[dict]]:
        """U7: reasoning model 分析所有错误，输出修复优先级和策略 hints"""
        errors_summary = []
        files_content: dict[str, str] = {}
        for i, err in enumerate(errors, 1):
            errors_summary.append(
                f"{i}. [{err.file_path}:{err.line}] {err.message[:300]}"
            )
            fkey = str(err.file_path)
            if fkey not in files_content:
                fp = sub_repo_path / err.file_path
                if fp.exists():
                    try:
                        files_content[fkey] = fp.read_text(
                            encoding="utf-8", errors="replace"
                        )[:3000]
                    except OSError:
                        pass

        files_text = ""
        for fp_, content_ in files_content.items():
            files_text += f"\n--- {fp_} ---\n```\n{content_}\n```\n"

        prompt = Prompts.ARCHITECT_ANALYZE_ERRORS.format(
            error_count=len(errors),
            errors_summary="\n".join(errors_summary),
            files_content=files_text[:8000],
        )

        try:
            result = self.llm.chat_json([{"role": "user", "content": prompt}])
            plan_items = result.get("plan", [])
            # Build lookup: file_path → list[plan_item]
            plan_map: dict[str, list[dict]] = {}
            for item in plan_items:
                key = item.get("file", "")
                plan_map.setdefault(key, []).append(item)
            logger.info(f"U7 Architect: {len(plan_items)} 个修复计划项")
            return plan_map
        except Exception as e:
            logger.warning(f"Architect 分析失败，回退到逐个修复: {e}")
            return {}

    def _try_fix_missing_import(self, sub_repo_path: Path, error: ValidationError) -> bool:
        """
        自动修复缺失的 import/module — 不经 LLM：
        1. 尝试从原仓库补充缺失文件
        2. 回退: 批量注释掉所有指向 out_of_scope 的 import 行
        """
        msg_lower = error.message.lower()
        if not any(k in msg_lower for k in ("no module named", "cannot find module",
                                             "importerror", "module not found")):
            return False

        # 提取所有缺失模块名（支持 grouped error 中包含多个模块）
        import re
        all_modules = re.findall(r"'([^']+)'", error.message)
        if not all_modules:
            return False

        excluded = self._get_out_of_scope()
        any_fixed = False

        # 收集需要注释的行号（同一文件中所有 out_of_scope import）
        lines_to_comment: set[int] = set()  # 0-based indices
        supplemented: set[str] = set()

        for module_name in dict.fromkeys(all_modules):  # 去重保序
            parts = module_name.replace(".", "/")
            candidates = [
                Path(f"{parts}.py"),
                Path(parts) / "__init__.py",
            ]
            is_excluded = False
            for rel_path in candidates:
                rel_str = str(rel_path).replace("\\", "/")
                if self._is_excluded(rel_str, excluded):
                    is_excluded = True
                    break

            if is_excluded:
                # 扫描文件，找到所有导入该模块的行
                file_path = sub_repo_path / error.file_path
                if file_path.exists():
                    try:
                        lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
                        top_module = module_name.split(".")[0]
                        for idx, line in enumerate(lines):
                            stripped = line.strip()
                            if stripped.startswith("# [CodePrune]"):
                                continue
                            if (f"import {module_name}" in stripped
                                    or f"from {module_name}" in stripped
                                    or f"import {top_module}" in stripped
                                    or f"from {top_module}" in stripped):
                                lines_to_comment.add(idx)
                    except OSError:
                        pass
                continue

            # 策略 1: 从原仓库补充文件
            for rel_path in candidates:
                if str(rel_path) in supplemented:
                    continue
                src = self.config.repo_path / rel_path
                dst = sub_repo_path / rel_path
                if src.exists() and not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    import shutil
                    shutil.copy2(src, dst)
                    logger.info(f"自动补充缺失模块: {rel_path}")
                    supplemented.add(str(rel_path))
                    any_fixed = True
                    break

        # 批量注释所有 out_of_scope import 行
        if lines_to_comment:
            file_path = sub_repo_path / error.file_path
            try:
                lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
                prefix = self._comment_prefix(file_path.suffix)
                # 展开多行 import
                expanded = set()
                for idx in sorted(lines_to_comment):
                    expanded |= self._expand_multiline_import(lines, idx)
                for idx in sorted(expanded):
                    if 0 <= idx < len(lines):
                        original_line = lines[idx]
                        if not original_line.strip().startswith(f"{prefix} [CodePrune]"):
                            lines[idx] = f"{prefix} [CodePrune] removed: {original_line.rstrip()}\n"
                file_path.write_text("".join(lines), encoding="utf-8")
                logger.info(f"批量注释 {len(expanded)} 行 out_of_scope import: {error.file_path}")
                any_fixed = True
            except OSError:
                pass

        # 兜底: 注释指定行（非 grouped error 场景）
        if not any_fixed and error.line > 0:
            file_path = sub_repo_path / error.file_path
            if file_path.exists():
                try:
                    lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
                    idx = error.line - 1
                    if 0 <= idx < len(lines):
                        prefix = self._comment_prefix(file_path.suffix)
                        all_idxs = self._expand_multiline_import(lines, idx)
                        for i in sorted(all_idxs):
                            if 0 <= i < len(lines):
                                original_line = lines[i]
                                if not original_line.strip().startswith(f"{prefix} [CodePrune]"):
                                    lines[i] = f"{prefix} [CodePrune] removed: {original_line.rstrip()}\n"
                        file_path.write_text("".join(lines), encoding="utf-8")
                        logger.info(f"注释掉缺失 import: {error.file_path}:{error.line} ({len(all_idxs)} lines)")
                        any_fixed = True
                except OSError:
                    pass

        return any_fixed

    def _get_out_of_scope(self) -> list[str]:
        """获取 instruction analysis 的排除列表"""
        analysis = getattr(self.config, "instruction_analysis", None)
        if analysis and hasattr(analysis, "out_of_scope"):
            return analysis.out_of_scope or []
        return []

    @staticmethod
    def _is_excluded(rel_path: str, excluded: list[str]) -> bool:
        """检查相对路径是否匹配 out_of_scope 中的任意项"""
        for ex in excluded:
            ex_norm = ex.replace("\\", "/")
            if ex_norm.endswith("/"):
                # 目录排除: rel_path 在该目录下
                if rel_path.startswith(ex_norm) or rel_path.startswith(ex_norm.rstrip("/")):
                    return True
            else:
                # 文件排除: 精确匹配或后缀匹配
                if rel_path == ex_norm or rel_path.endswith("/" + ex_norm):
                    return True
        return False

    # ── U5: Stub Generation Strategy ─────────────────────────────────

    def _try_generate_stub(self, sub_repo_path: Path, error: ValidationError) -> bool:
        """Strategy 3: 为缺失符号生成最小 stub 文件（仅限 out_of_scope 依赖）

        适用场景:
        - Java: "cannot find symbol" → 空 class/interface stub
        - Python: "NameError" / "ImportError" 且来自 excluded → 空 class/function stub
        - TS: "Cannot find module" → 空 export stub
        """
        import re as _re
        msg_lower = error.message.lower()
        excluded = self._get_out_of_scope()
        if not excluded:
            return False

        # 提取缺失符号和可能的来源模块
        stubs_created = 0
        file_path = sub_repo_path / error.file_path
        suffix = file_path.suffix

        if suffix == ".java":
            # Java: "cannot find symbol:   symbol: class FooService"
            for m in _re.finditer(
                r'cannot find symbol.*?(?:class|variable|method)\s+(\w+)',
                error.message, _re.IGNORECASE | _re.DOTALL,
            ):
                symbol = m.group(1)
                if self._create_java_stub(sub_repo_path, file_path, symbol, excluded):
                    stubs_created += 1

        elif suffix == ".py":
            # Python: "name 'Foo' is not defined" or "cannot import name 'Foo'"
            for m in _re.finditer(r"name '(\w+)' is not defined", error.message):
                symbol = m.group(1)
                if self._create_python_stub(sub_repo_path, file_path, symbol, excluded):
                    stubs_created += 1

        elif suffix in (".ts", ".js"):
            # TS: "Cannot find module './foo'"
            for m in _re.finditer(r"Cannot find module '([^']+)'", error.message):
                module = m.group(1)
                if self._create_ts_stub(sub_repo_path, file_path, module, excluded):
                    stubs_created += 1

        if stubs_created:
            logger.info(f"Stub 生成: 为 {error.file_path} 创建 {stubs_created} 个 stub")
            return True
        return False

    def _create_java_stub(
        self, sub_repo_path: Path, error_file: Path, symbol: str,
        excluded: list[str],
    ) -> bool:
        """为 Java 缺失类创建空 interface stub"""
        import re as _re
        # 从 error_file 的 import 语句中找到 symbol 的包路径
        try:
            content = error_file.read_text(encoding="utf-8")
        except OSError:
            return False

        for m in _re.finditer(r'^import\s+([\w.]+\.' + _re.escape(symbol) + r')\s*;',
                              content, _re.MULTILINE):
            fqn = m.group(1)
            # 检查是否属于 excluded 模块
            parts = fqn.split(".")
            if not any(p in [ex.rstrip("/") for ex in excluded] for p in parts):
                continue

            # 构建 stub 文件路径
            # com.example.service.FooService → src/main/java/com/example/service/FooService.java
            rel_path = "/".join(parts) + ".java"
            # 也检查 src/ 下
            candidates = [
                sub_repo_path / "src" / "main" / "java" / rel_path,
                sub_repo_path / "src" / rel_path,
                sub_repo_path / rel_path,
            ]
            for stub_path in candidates:
                # 确保在子仓库内
                try:
                    stub_path.resolve().relative_to(sub_repo_path.resolve())
                except ValueError:
                    continue
                if stub_path.exists():
                    continue

                package = ".".join(parts[:-1])
                # 尝试从 graph 提取类中的方法签名
                methods_stub = self._extract_java_class_methods(symbol)
                stub_content = (
                    f"package {package};\n\n"
                    f"// [CodePrune] Auto-generated stub for excluded dependency\n"
                    f"public class {symbol} {{\n{methods_stub}}}\n"
                )
                stub_path.parent.mkdir(parents=True, exist_ok=True)
                stub_path.write_text(stub_content, encoding="utf-8")
                logger.debug(f"Java stub: {stub_path.relative_to(sub_repo_path)}")
                return True
        return False

    def _create_python_stub(
        self, sub_repo_path: Path, error_file: Path, symbol: str,
        excluded: list[str],
    ) -> bool:
        """为 Python 缺失符号创建 stub 模块"""
        import re as _re
        try:
            content = error_file.read_text(encoding="utf-8")
        except OSError:
            return False

        # 查找 from X import symbol 或 import X.symbol
        for m in _re.finditer(
            rf'^(?:from\s+([\w.]+)\s+import\s+.*\b{_re.escape(symbol)}\b|'
            rf'import\s+([\w.]+\.{_re.escape(symbol)}))',
            content, _re.MULTILINE,
        ):
            module = m.group(1) or m.group(2)
            if not module:
                continue
            # 检查是否属于 excluded
            top = module.split(".")[0]
            if not any(top in ex.replace("\\", "/").rstrip("/").split("/") for ex in excluded):
                continue

            # 构建 stub 文件
            parts = module.replace(".", "/")
            stub_path = sub_repo_path / (parts + ".py")
            # 确保在子仓库内
            try:
                stub_path.resolve().relative_to(sub_repo_path.resolve())
            except ValueError:
                continue
            if stub_path.exists():
                continue

            # 从原仓库获取该符号的签名
            stub_content = self._python_stub_content(symbol, module)
            stub_path.parent.mkdir(parents=True, exist_ok=True)
            stub_path.write_text(stub_content, encoding="utf-8")
            logger.debug(f"Python stub: {stub_path.relative_to(sub_repo_path)}")
            return True
        return False

    def _python_stub_content(self, symbol: str, module: str) -> str:
        """生成 Python stub 内容 — 从原仓库提取精确签名"""
        for node in self.graph.nodes.values():
            if node.qualified_name.endswith(f".{symbol}") or node.name == symbol:
                if node.node_type.value == "class":
                    return (
                        f"# [CodePrune] Auto-generated stub for excluded dependency\n"
                        f"class {symbol}:\n    pass\n"
                    )
                elif node.node_type.value == "function":
                    # 尝试从原仓库源文件提取完整签名
                    sig_line = self._extract_function_signature(node)
                    if sig_line:
                        return (
                            f"# [CodePrune] Auto-generated stub for excluded dependency\n"
                            f"{sig_line}:\n    raise NotImplementedError('pruned by CodePrune')\n"
                        )
                    # 使用 graph 中的参数列表（仅参数部分）
                    if node.signature:
                        return (
                            f"# [CodePrune] Auto-generated stub for excluded dependency\n"
                            f"def {symbol}{node.signature}:\n    raise NotImplementedError('pruned by CodePrune')\n"
                        )
                    return (
                        f"# [CodePrune] Auto-generated stub for excluded dependency\n"
                        f"def {symbol}(*args, **kwargs):\n    raise NotImplementedError('pruned by CodePrune')\n"
                    )
        # 默认: 假设是 class
        return (
            f"# [CodePrune] Auto-generated stub for excluded dependency\n"
            f"class {symbol}:\n    pass\n"
        )

    def _extract_function_signature(self, node: CodeNode) -> str | None:
        """从原仓库源码提取 Python/Java/TS 函数的完整签名行（到 : 或 { 之前）"""
        if not node.file_path or not node.byte_range:
            return None
        src = Path(self.config.repo_path) / node.file_path
        if not src.exists():
            return None
        try:
            lines = src.read_text(encoding="utf-8", errors="replace").splitlines()
            start = node.byte_range.start_line - 1  # 0-based
            if start < 0 or start >= len(lines):
                return None
            sig_lines = []
            for i in range(start, min(start + 5, len(lines))):
                sig_lines.append(lines[i])
                stripped = lines[i].rstrip()
                if stripped.endswith((':',  '{', ');')):
                    break
            full = "\n".join(sig_lines)
            # Python: 提取到函数体 : 之前（用 rfind 避免截断类型注解中的 :）
            if full.lstrip().startswith("def "):
                idx = full.rfind(":")
                if idx > 0:
                    return full[:idx].rstrip()
            return full.rstrip()
        except OSError:
            return None

    def _extract_java_class_methods(self, class_name: str) -> str:
        """从 graph 中提取 Java 类的方法签名，生成 stub 方法"""
        methods = []
        # 查找该类下的方法节点
        for node in self.graph.nodes.values():
            if node.node_type.value != "function":
                continue
            # 匹配 ClassName.methodName 模式
            if f".{class_name}." not in node.qualified_name:
                continue
            sig = self._extract_function_signature(node)
            if sig:
                sig_line = sig.split("{")[0].strip()
                if sig_line:
                    methods.append(f"    {sig_line} {{ throw new UnsupportedOperationException(\"pruned\"); }}")
        return "\n".join(methods) + "\n" if methods else ""

    def _create_ts_stub(
        self, sub_repo_path: Path, error_file: Path, module_path: str,
        excluded: list[str],
    ) -> bool:
        """为 TypeScript 缺失模块创建空 export stub"""
        # 解析相对路径
        if module_path.startswith("."):
            base_dir = error_file.parent
            clean = module_path.removesuffix(".ts").removesuffix(".js")
            stub_path = (base_dir / clean).with_suffix(".ts")
        else:
            return False  # 非相对导入，可能是 node_modules

        # 确保在子仓库内
        try:
            stub_path.resolve().relative_to(sub_repo_path.resolve())
        except ValueError:
            return False
        if stub_path.exists():
            return False

        # 检查是否属于 excluded
        rel = stub_path.relative_to(sub_repo_path).as_posix()
        segments = rel.split("/")
        if not any(
            seg.removesuffix(".ts") in [ex.rstrip("/") for ex in excluded]
            for seg in segments
        ):
            return False

        stub_content = (
            f"// [CodePrune] Auto-generated stub for excluded dependency\n"
            f"export default {{}};\n"
        )
        stub_path.parent.mkdir(parents=True, exist_ok=True)
        stub_path.write_text(stub_content, encoding="utf-8")
        logger.debug(f"TS stub: {stub_path.relative_to(sub_repo_path)}")
        return True

    # ── U1: Error Context Enhancement ────────────────────────────────

    @staticmethod
    def _extract_error_lines(error: ValidationError) -> list[int]:
        """从 ValidationError 中提取所有涉及的行号 (1-based)"""
        import re as _re
        lines: set[int] = set()
        # 显式的 error.line
        if error.line > 0:
            lines.add(error.line)
        # 从消息文本中提取行号: "line 42", ":42:", "(42,", "Line 42"
        for m in _re.finditer(r'(?:line\s+|:)(\d+)(?:[,:\s)]|$)', error.message, _re.IGNORECASE):
            n = int(m.group(1))
            if 1 <= n <= 100000:
                lines.add(n)
        return sorted(lines)

    @staticmethod
    def _format_error_context(file_content: str, error: ValidationError, window: int = 10) -> str:
        """U1: 生成聚焦的错误上下文 — 错误行 ±window 行，█ 标记错误行"""
        lines = file_content.splitlines()
        error_lines = HealEngine._extract_error_lines(error)
        if not error_lines:
            # 无行号信息 — 返回文件前 30 行
            snippet = []
            for i, line in enumerate(lines[:30], 1):
                snippet.append(f"  {i:4d} | {line}")
            return "\n".join(snippet)

        # 收集需要显示的行范围（合并重叠区间）
        ranges: list[tuple[int, int]] = []
        for ln in error_lines:
            start = max(1, ln - window)
            end = min(len(lines), ln + window)
            if ranges and start <= ranges[-1][1] + 2:  # 合并相近区间
                ranges[-1] = (ranges[-1][0], end)
            else:
                ranges.append((start, end))

        error_set = set(error_lines)
        snippet = []
        for rng_start, rng_end in ranges:
            if snippet:
                snippet.append("     ...")
            for i in range(rng_start, rng_end + 1):
                marker = "█ " if i in error_set else "  "
                snippet.append(f"{marker}{i:4d} | {lines[i - 1]}")

        return "\n".join(snippet)

    def _generate_batch_fix_sr(
        self, sub_repo_path: Path, errors: list[ValidationError],
    ) -> list[FixPatch]:
        """批量 SEARCH/REPLACE 修复：将所有 LLM 错误打包为一次调用，返回解析后的 FixPatch 列表"""

        # ── 构建 all_errors 文本 ──
        errors_text_parts: list[str] = []
        files_content: dict[str, str] = {}
        for i, err in enumerate(errors, 1):
            errors_text_parts.append(
                f"{i}. [{err.file_path}:{err.line}] {err.message[:1000]}"
            )
            fkey = str(err.file_path)
            if fkey not in files_content:
                fp = sub_repo_path / err.file_path
                if fp.exists():
                    try:
                        files_content[fkey] = fp.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        pass

        # ── 构建 files_with_errors 文本（每个文件截断到 4000 字符）──
        files_text_parts: list[str] = []
        for fpath, content in files_content.items():
            files_text_parts.append(
                f"\n--- {fpath} ---\n```\n{content[:8000]}\n```"
            )

        # ── 构建 original_context 文本 ──
        original_parts: list[str] = []
        seen_originals: set[str] = set()
        for err in errors:
            fkey = str(err.file_path)
            if fkey in seen_originals:
                continue
            seen_originals.add(fkey)
            orig = self._get_original_context(err.file_path)
            if orig != "(original file not found)":
                original_parts.append(
                    f"\n--- {fkey} (original) ---\n```\n{orig[:8000]}\n```"
                )

        # ── reflected_history ──
        prev_attempts = [
            f"- [{fp}] 尝试: {fix} → 仍然报错"
            for fp, _err, fix in self._fix_history
        ]
        reflected = ""
        if prev_attempts:
            reflected = (
                "\n\n⚠️ PREVIOUS FAILED ATTEMPTS (do NOT repeat):\n"
                + "\n".join(prev_attempts[-5:])
            )

        # ── SR 匹配失败反馈 ──
        if self._sr_match_failures:
            parts = ["\n\n⚠️ SEARCH/REPLACE MATCH FAILURES from last round:"]
            for fkey, search_blk, actual_snip in self._sr_match_failures[-5:]:
                parts.append(
                    f"- [{fkey}] Your SEARCH block:\n```\n{search_blk}\n```\n"
                    f"  did NOT match. The actual lines in this file are:\n"
                    f"```\n{actual_snip}\n```\n"
                    f"  → Use the EXACT text from the current file."
                )
            reflected += "\n".join(parts)
            self._sr_match_failures.clear()

        # ── Dispatcher 失败修复上下文 ──
        dispatcher_context = ""
        if self._dispatcher:
            dispatcher_context = self._dispatcher.get_failed_repair_context()
            if dispatcher_context:
                self._dispatcher.clear_repair_contexts()

        prompt = Prompts.FIX_SYNTAX_ERROR_SR.format(
            error_count=len(errors),
            all_errors="\n".join(errors_text_parts),
            files_with_errors="\n".join(files_text_parts)[:24000],
            original_context="\n".join(original_parts)[:16000],
            reflected_history=reflected,
            dispatcher_context=dispatcher_context,
        )

        try:
            response = self.llm.chat(
                [{"role": "user", "content": prompt}]
            )
            patches = parse_search_replace_blocks(response)
            for p in patches:
                self._validate_patch_safety(p)
            logger.info(f"SR batch: LLM 返回 {len(patches)} 个 SEARCH/REPLACE 块")
            return patches
        except Exception as e:
            logger.warning(f"SR batch 修复生成失败: {e}")
            return []

    def _apply_patches_reverse(
        self, sub_repo_path: Path, patches: list[FixPatch],
    ) -> int:
        """按从后往前顺序应用同一文件的多个补丁，返回成功应用数"""
        from collections import defaultdict
        by_file: dict[str, list[FixPatch]] = defaultdict(list)
        for p in patches:
            by_file[str(p.file_path)].append(p)

        total_applied = 0
        for file_key, file_patches in by_file.items():
            file_path = sub_repo_path / file_key
            if not file_path.exists():
                continue
            # 安全校验
            try:
                file_path.resolve().relative_to(sub_repo_path.resolve())
            except ValueError:
                logger.warning(f"拒绝修改子仓库外文件: {file_path}")
                continue
            try:
                content = file_path.read_text(encoding="utf-8")
            except OSError:
                continue

            # 定位所有 patch 在文件中的位置
            located: list[tuple[int, FixPatch]] = []
            for p in file_patches:
                if not p.original_code:
                    continue
                idx = content.find(p.original_code)
                if idx >= 0:
                    located.append((idx, p))
                else:
                    # 回退到行级模糊匹配
                    content_lines = content.splitlines(keepends=True)
                    orig_lines = p.original_code.splitlines(keepends=True)
                    line_idx, fuzz = _find_context_core(content_lines, orig_lines, 0)
                    if line_idx >= 0:
                        # 转换行索引到字符偏移
                        char_offset = sum(len(l) for l in content_lines[:line_idx])
                        located.append((char_offset, p))
                    else:
                        logger.warning(f"SR 补丁定位失败: {file_key}")
                        # 记录匹配失败：SEARCH 块 + 文件中最相似区域
                        self._record_sr_match_failure(
                            file_key, p.original_code, content,
                        )

            # 从后往前应用
            located.sort(key=lambda x: x[0], reverse=True)
            for idx, p in located:
                # 重新定位（前面的替换可能改变了偏移）
                actual_idx = content.find(p.original_code)
                if actual_idx >= 0:
                    content = (
                        content[:actual_idx]
                        + p.fixed_code
                        + content[actual_idx + len(p.original_code):]
                    )
                    total_applied += 1
                    self._fix_history.append(
                        (Path(file_key), "", p.explanation or "SR fix")
                    )
                else:
                    # 行级模糊再试一次
                    success = self._apply_patch(sub_repo_path, p)
                    if success:
                        total_applied += 1
                        # _apply_patch 内部已写文件，重新读取
                        try:
                            content = file_path.read_text(encoding="utf-8")
                        except OSError:
                            break
                    else:
                        self._record_sr_match_failure(
                            file_key, p.original_code, content,
                        )

            if located:
                file_path.write_text(content, encoding="utf-8")

        return total_applied

    def _generate_fix(
        self, sub_repo_path: Path, error: ValidationError,
        architect_hints: dict[str, list[dict]] | None = None,
    ) -> Optional[FixPatch]:
        """LLM 生成修复补丁 (U7: 有 architect plan 时使用 fast model)"""
        file_path = sub_repo_path / error.file_path
        try:
            file_content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        # U1: 从错误消息中提取行号，生成聚焦的错误上下文
        error_context = self._format_error_context(file_content, error)

        # 从原仓库获取上下文
        original_context = self._get_original_context(error.file_path)

        # reflected_message 模式 (借鉴 aider): 提供之前对同文件的修复尝试，避免重复
        prev_attempts = [
            f"- 尝试: {fix} \u2192 仍然报错"
            for fp, _err, fix in self._fix_history
            if fp == error.file_path
        ]
        reflected = ""
        if prev_attempts:
            reflected = (
                "\n\n\u26a0\ufe0f PREVIOUS FAILED ATTEMPTS on this file (do NOT repeat):\n"
                + "\n".join(prev_attempts[-3:])
            )

        # U7: 如有 architect hint, 附加到 prompt 并使用 fast model
        hint_text = ""
        use_fast = False
        if architect_hints:
            hints = architect_hints.get(str(error.file_path), [])
            if hints:
                item = hints[0]
                strategy = item.get("strategy", "PATCH")
                hint = item.get("hint", "")
                hint_text = (
                    f"\n\nArchitect's repair plan:"
                    f"\n- Strategy: {strategy}"
                    f"\n- Hint: {hint}"
                    f"\nFollow the architect's guidance closely."
                )
                use_fast = True

        # Dispatcher 失败上下文
        dispatcher_hint = ""
        if self._dispatcher:
            ctx = self._dispatcher.get_failed_repair_context()
            if ctx:
                dispatcher_hint = ctx

        prompt = Prompts.FIX_SYNTAX_ERROR.format(
            error_message=error.message,
            file_path=error.file_path,
            error_context=error_context,
            file_content=file_content[:10000],
            original_context=original_context[:10000],
        ) + reflected + hint_text + dispatcher_hint

        try:
            if use_fast:
                result = self.llm.fast_chat_json(
                    [{"role": "user", "content": prompt}]
                )
            else:
                result = self.llm.chat_json(
                    [{"role": "user", "content": prompt}]
                )
            patch = FixPatch(
                file_path=Path(result.get("file_path", str(error.file_path))),
                original_code=result.get("original_code", ""),
                fixed_code=result.get("fixed_code", ""),
                explanation=result.get("explanation", ""),
            )
            self._validate_patch_safety(patch)
            return patch
        except Exception as e:
            # U7: fast model 失败时回退到 reasoning model
            if use_fast:
                logger.debug(f"fast model 修复失败，回退到 reasoning model: {e}")
                try:
                    result = self.llm.chat_json(
                        [{"role": "user", "content": prompt}]
                    )
                    patch = FixPatch(
                        file_path=Path(result.get("file_path", str(error.file_path))),
                        original_code=result.get("original_code", ""),
                        fixed_code=result.get("fixed_code", ""),
                        explanation=result.get("explanation", ""),
                    )
                    self._validate_patch_safety(patch)
                    return patch
                except Exception as e2:
                    logger.warning(f"修复生成失败 (回退): {e2}")
                    return None
            logger.warning(f"修复生成失败: {e}")
            return None

    def _apply_patch(self, sub_repo_path: Path, patch: FixPatch) -> bool:
        """应用修复补丁 — 三级模糊匹配 (借鉴 aider find_context_core)"""
        file_path = sub_repo_path / patch.file_path
        # 安全校验：拒绝子仓库外的文件修改（防止 LLM 输出路径逃逸）
        try:
            file_path.resolve().relative_to(sub_repo_path.resolve())
        except ValueError:
            logger.warning(f"拒绝修改子仓库外文件: {file_path}")
            return False
        try:
            content = file_path.read_text(encoding="utf-8")

            # 精确字符串匹配
            if patch.original_code and patch.original_code in content:
                new_content = content.replace(patch.original_code, patch.fixed_code, 1)
                file_path.write_text(new_content, encoding="utf-8")
                logger.debug(f"补丁应用成功 (精确): {patch.file_path} — {patch.explanation}")
                return True

            # 三级行匹配 — exact → rstrip → strip
            if patch.original_code:
                content_lines = content.splitlines(keepends=True)
                orig_lines = patch.original_code.splitlines(keepends=True)

                idx, fuzz = _find_context_core(content_lines, orig_lines, 0)
                if idx >= 0:
                    replacement = patch.fixed_code
                    if replacement and not replacement.endswith("\n"):
                        replacement += "\n"
                    replace_lines = replacement.splitlines(keepends=True)
                    # 缩进感知：fuzz >= 100 时，将 replace 块的缩进对齐到文件实际缩进
                    if fuzz >= 100 and replace_lines:
                        delta = _compute_indent_delta(
                            content_lines[idx:idx + len(orig_lines)], replace_lines,
                        )
                        if delta:
                            replace_lines = [
                                (delta + line if line.strip() else line)
                                for line in replace_lines
                            ]
                    new_lines = (
                        content_lines[:idx]
                        + replace_lines
                        + content_lines[idx + len(orig_lines):]
                    )
                    file_path.write_text("".join(new_lines), encoding="utf-8")
                    fuzz_label = {0: "exact-line", 1: "rstrip", 100: "strip", 200: "indent-aware"}
                    logger.debug(
                        f"补丁应用成功 (fuzz={fuzz_label.get(fuzz, fuzz)}): "
                        f"{patch.file_path} — {patch.explanation}"
                    )
                    return True

                # P2-fix: 编辑距离回退匹配 — 当所有精确/模糊行匹配都失败时
                if len(orig_lines) >= 3:
                    best_idx, best_ratio = self._find_by_edit_distance(
                        content_lines, orig_lines, threshold=0.78,
                    )
                    if best_idx >= 0:
                        replacement = patch.fixed_code
                        if replacement and not replacement.endswith("\n"):
                            replacement += "\n"
                        replace_lines = replacement.splitlines(keepends=True)
                        delta = _compute_indent_delta(
                            content_lines[best_idx:best_idx + len(orig_lines)],
                            replace_lines,
                        )
                        if delta:
                            replace_lines = [
                                (delta + line if line.strip() else line)
                                for line in replace_lines
                            ]
                        new_lines = (
                            content_lines[:best_idx]
                            + replace_lines
                            + content_lines[best_idx + len(orig_lines):]
                        )
                        file_path.write_text("".join(new_lines), encoding="utf-8")
                        logger.warning(
                            f"补丁应用成功 (edit-distance, ratio={best_ratio:.2f}): "
                            f"{patch.file_path} — {patch.explanation}"
                        )
                        return True

            logger.warning(f"补丁定位失败: 原始代码片段未找到 ({patch.file_path})")
            return False
        except OSError as e:
            logger.warning(f"补丁应用失败: {e}")
            return False

    def _get_original_context(self, file_path: Path) -> str:
        """从原仓库获取相关上下文"""
        original_file = self.config.repo_path / file_path
        if original_file.exists():
            try:
                return original_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        return "(original file not found)"

    @staticmethod
    def _find_by_edit_distance(
        file_lines: list[str],
        search_lines: list[str],
        threshold: float = 0.78,
    ) -> tuple[int, float]:
        """
        P2: 编辑距离回退匹配。
        在 file_lines 中滑动窗口，找到与 search_lines 最相似的位置。
        允许窗口大小在 ±10% 范围内浮动以容纳行数差异。
        Returns: (best_index, best_ratio) 或 (-1, 0.0) 表示未找到
        """
        from difflib import SequenceMatcher
        n = len(search_lines)
        if n == 0 or not file_lines:
            return -1, 0.0

        search_text = "".join(s.strip() for s in search_lines)
        best_idx = -1
        best_ratio = 0.0

        # 窗口大小范围 [n*0.9, n*1.1]
        min_window = max(1, int(n * 0.9))
        max_window = min(len(file_lines), int(n * 1.1) + 1)

        for win_size in range(min_window, max_window + 1):
            for i in range(len(file_lines) - win_size + 1):
                chunk_text = "".join(s.strip() for s in file_lines[i:i + win_size])
                ratio = SequenceMatcher(None, search_text, chunk_text).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_idx = i

        if best_ratio >= threshold:
            return best_idx, best_ratio
        return -1, 0.0

    def _record_sr_match_failure(
        self, file_key: str, search_block: str, file_content: str,
    ) -> None:
        """记录 SR 匹配失败，供下一轮反射 prompt 使用"""
        from difflib import SequenceMatcher
        search_lines = search_block.splitlines()
        file_lines = file_content.splitlines()
        n = len(search_lines)
        if n == 0 or not file_lines:
            return
        # 在文件中找最相似的区域
        search_text = "\n".join(s.strip() for s in search_lines)
        best_idx, best_ratio = 0, 0.0
        for i in range(max(1, len(file_lines) - n + 1)):
            end = min(i + n, len(file_lines))
            chunk = "\n".join(s.strip() for s in file_lines[i:end])
            ratio = SequenceMatcher(None, search_text, chunk, autojunk=False).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_idx = i
        # 提取实际代码片段 (上下各扩展 2 行)
        start = max(0, best_idx - 2)
        end = min(len(file_lines), best_idx + n + 2)
        actual = "\n".join(file_lines[start:end])
        # 截断避免 prompt 膨胀
        self._sr_match_failures.append((
            file_key,
            search_block[:500],
            actual[:500],
        ))

    def _validate_patch_safety(self, patch: FixPatch) -> None:
        """验证修复补丁的安全性，标记可疑补丁"""
        if not patch.fixed_code.strip():
            return  # 删除代码总是安全的

        orig_lines = max(len(patch.original_code.splitlines()), 1)
        fix_lines = len(patch.fixed_code.splitlines())

        # 规则: 修复代码不应超过原代码 2 倍
        if fix_lines > max(orig_lines * 2, 10):
            logger.warning(f"修复补丁过长 ({orig_lines}→{fix_lines} 行)，标记为 synthetic")
            patch.synthetic = True

        # 规则: 检查修复内容是否在原仓库中有出处
        if patch.fixed_code.strip():
            original_file = self.config.repo_path / patch.file_path
            if original_file.exists():
                try:
                    original_content = original_file.read_text(encoding="utf-8", errors="replace")
                    # 检查修复代码的非空行是否大部分能在原文件中找到
                    fix_lines_list = [l.strip() for l in patch.fixed_code.splitlines() if l.strip()]
                    if fix_lines_list:
                        found = sum(1 for l in fix_lines_list if l in original_content)
                        ratio = found / len(fix_lines_list)
                        if ratio < 0.5:
                            logger.warning(f"修复代码仅 {ratio:.0%} 能在原仓库找到出处，标记为 synthetic")
                            patch.synthetic = True
                except OSError:
                    pass

    def _check_completeness(self, sub_repo_path: Path) -> list[str]:
        """LLM 检查功能完整性"""
        # 收集子仓库文件摘要
        sub_summaries = []
        for f in sorted(sub_repo_path.rglob("*")):
            if f.is_file() and f.suffix in (".py", ".java", ".js", ".ts", ".c", ".cpp", ".h", ".hpp"):
                rel = f.relative_to(sub_repo_path)
                sub_summaries.append(f"- {rel}")

        # 收集原仓库相关摘要
        original_summaries = []
        for node in self.graph.nodes.values():
            if node.summary:
                original_summaries.append(f"- {node.qualified_name}: {node.summary}")

        # 获取排除列表，告知 LLM 这些是故意排除的
        excluded = self._get_out_of_scope()
        excluded_note = ""
        if excluded:
            excluded_note = (
                "\n\nIMPORTANT: The following components were INTENTIONALLY excluded "
                "per the user's request. Do NOT flag them as missing:\n"
                + "\n".join(f"- {e}" for e in excluded)
            )

        prompt = Prompts.CHECK_COMPLETENESS.format(
            user_instruction=self.config.user_instruction,
            sub_repo_summaries="\n".join(sub_summaries[:50]),
            original_summaries="\n".join(original_summaries[:50]),
        ) + excluded_note

        try:
            result = self.llm.chat_json([{"role": "user", "content": prompt}])
            if not result.get("complete", True):
                missing = result.get("missing_components", [])
                # 过滤掉 out_of_scope 的组件（即便 LLM 仍然误报）
                if excluded and missing:
                    filtered = []
                    for comp in missing:
                        comp_lower = comp.lower().replace("\\", "/")
                        if not any(
                            comp_lower in ex.lower().replace("\\", "/")
                            or ex.lower().replace("\\", "/").rstrip("/") in comp_lower
                            for ex in excluded
                        ):
                            filtered.append(comp)
                        else:
                            logger.info(f"完整性检查忽略 out_of_scope 组件: {comp}")
                    missing = filtered
                # U3: 结构化校验 — 过滤非路径格式的条目
                missing = self._validate_missing_components(missing, sub_repo_path)
                return missing
        except Exception as e:
            logger.warning(f"完整性检查失败: {e}")
        return []

    def _validate_missing_components(self, missing: list[str], sub_repo_path: Path) -> list[str]:
        """U3: 结构化校验 — 过滤 LLM 返回的非路径格式条目 + 已存在文件"""
        import re as _re
        if not missing:
            return missing
        valid = []
        for comp in missing:
            comp = comp.strip()
            if not comp:
                continue
            # 规则 1: 必须像文件路径（含扩展名 或 目录分隔符 或 纯目录名如 "auth/"）
            looks_like_path = bool(
                _re.search(r'\.\w{1,5}$', comp)    # 含扩展名: utils.py, App.tsx
                or '/' in comp                       # 含目录分隔符: src/utils
                or comp.endswith('/')                # 目录: auth/
            )
            if not looks_like_path:
                # 特殊通融: 如果匹配到原仓库中的某个符号名（如 "User", "paginate"），仍允许
                found_in_graph = any(
                    comp.lower() in n.qualified_name.lower()
                    for n in self.graph.nodes.values()
                )
                if not found_in_graph:
                    logger.info(f"完整性校验: 忽略非路径格式条目 → {comp!r}")
                    continue
            # 规则 2: 文件路径格式的条目 — 如果子仓库中已存在，不算缺失
            candidate = sub_repo_path / comp.rstrip("/")
            if candidate.exists():
                logger.debug(f"完整性校验: 忽略已存在 → {comp}")
                continue
            valid.append(comp)
        if len(valid) < len(missing):
            logger.info(
                f"完整性校验: {len(missing)} → {len(valid)} 条 "
                f"(过滤 {len(missing) - len(valid)} 条无效条目)"
            )
        return valid

    def _supplement_missing(self, sub_repo_path: Path, missing: list[str]) -> None:
        """U5: 从原仓库补充遗漏的组件 — 优先符号级提取，回退整文件复制"""
        import shutil
        excluded = self._get_out_of_scope()
        for component_name in missing:
            matched_files: set[Path] = set()
            matched_symbols: list[tuple[Path, str, int, int]] = []  # (file, name, start, end)

            for node in self.graph.nodes.values():
                if component_name.lower() in node.qualified_name.lower():
                    if node.file_path:
                        matched_files.add(node.file_path)
                        # 收集符号位置信息（如果有）
                        if hasattr(node, 'start_line') and node.start_line and \
                           hasattr(node, 'end_line') and node.end_line:
                            matched_symbols.append((
                                node.file_path, node.name,
                                node.start_line, node.end_line,
                            ))

            for file_path in matched_files:
                rel_str = str(file_path).replace("\\", "/")
                if self._is_excluded(rel_str, excluded):
                    logger.info(f"跳过 out_of_scope 补充: {file_path}")
                    continue

                src = self.config.repo_path / file_path
                dst = sub_repo_path / file_path
                if not src.exists():
                    continue

                if not dst.exists():
                    # 文件不存在 → 整文件复制
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    logger.info(f"补充遗漏: {file_path}")
                else:
                    # 文件已存在 → 尝试符号级追加
                    symbols_for_file = [
                        (name, start, end)
                        for fp, name, start, end in matched_symbols
                        if fp == file_path
                    ]
                    if symbols_for_file:
                        self._append_symbols(src, dst, symbols_for_file)
                break  # 每个 component 只补充第一个匹配文件

    def _append_symbols(
        self, src: Path, dst: Path,
        symbols: list[tuple[str, int, int]],
    ) -> None:
        """U5: 从原文件提取指定符号并追加到目标文件（避免整文件覆盖）"""
        try:
            src_lines = src.read_text(encoding="utf-8").splitlines(keepends=True)
            dst_content = dst.read_text(encoding="utf-8")
        except OSError:
            return

        appended = []
        for name, start, end in symbols:
            # 检查目标文件中是否已包含该符号
            if f"def {name}" in dst_content or f"class {name}" in dst_content:
                continue

            # 提取符号代码段
            if 1 <= start <= len(src_lines) and end <= len(src_lines):
                snippet = "".join(src_lines[start - 1 : end])
                appended.append(snippet)
                logger.info(f"符号级补充: {name} (L{start}-{end}) → {dst.name}")

        if appended:
            with open(dst, "a", encoding="utf-8") as f:
                f.write("\n\n")
                f.write("\n\n".join(appended))

    def _check_fidelity(self, sub_repo_path: Path) -> list[FixPatch]:
        """对比子仓库与原仓库，检测 LLM 生成的非原仓库代码
        增强：不仅检测新增文件，还检测已有文件中的行级不可追溯修改
        注意：跳过 RuntimeFixer 增补策略修改的文件（非 LLM 幻觉）
        """
        _CODE_EXTS = {".py", ".java", ".js", ".ts", ".c", ".cpp", ".h", ".hpp"}
        _STRUCTURAL = ("}", "{", ")", "(", "]", "[", "pass", "return", "break",
                       "continue", "else:", "try:", "except:", "finally:")
        hallucinations = []
        for f in sub_repo_path.rglob("*"):
            if not f.is_file() or f.suffix not in _CODE_EXTS:
                continue
            rel = f.relative_to(sub_repo_path)

            # 跳过被增补策略修改的文件（确定性代码图引导，非 LLM 幻觉）
            if str(rel) in self._supplemented_files:
                continue

            # 跳过 build 层修复涉及的文件 — 这些文件被修改是为了修复编译错误
            # 回退它们会导致编译错误死灰复燃，浪费后续修复轮次
            rel_posix = str(rel).replace("\\", "/")
            if rel_posix in self._build_fixed_files:
                continue

            original = self.config.repo_path / rel

            if not original.exists():
                hallucinations.append(FixPatch(
                    file_path=rel, original_code="", fixed_code="",
                    explanation="文件不存在于原仓库", synthetic=True,
                ))
                continue

            # 行级追溯: sub-repo 中的每一行是否能在原文件中找到
            try:
                orig_content = original.read_text(encoding="utf-8", errors="replace")
                sub_content = f.read_text(encoding="utf-8", errors="replace")

                significant = [
                    l.strip() for l in sub_content.splitlines()
                    if l.strip()
                    and l.strip() not in _STRUCTURAL
                    and not l.strip().startswith(("#", "//", "/*", "*"))
                    and len(l.strip()) > 5
                ]
                if not significant:
                    continue

                untraceable = sum(1 for l in significant if l not in orig_content)
                ratio = untraceable / len(significant)
                if ratio > self.config.heal.diff_tolerance:
                    hallucinations.append(FixPatch(
                        file_path=rel, original_code="", fixed_code="",
                        explanation=f"{untraceable}/{len(significant)} 行无法追溯到原仓库 ({ratio:.0%})",
                        synthetic=True,
                    ))
            except OSError:
                pass
        return hallucinations

    def _revert_hallucinations(self, sub_repo_path: Path, hallucinations: list[FixPatch]) -> None:
        """回退非原仓库代码 — 优先恢复到 heal 前快照（外科产出），而非原仓库全文件"""
        for h in hallucinations:
            if not h.synthetic:
                continue
            file_path = sub_repo_path / h.file_path

            # 优先恢复到 heal 前快照（保留外科裁剪结果）
            snapshot = self._pre_heal_snapshot.get(h.file_path)
            if snapshot is not None:
                file_path.write_text(snapshot, encoding="utf-8")
                logger.info(f"回退到 heal 前版本: {h.file_path}")
                continue

            # 快照中不存在 → 文件是 heal 期间新增的
            original = self.config.repo_path / h.file_path
            if not original.exists():
                if file_path.exists():
                    file_path.unlink()
                    logger.info(f"删除合成文件: {h.file_path}")
            else:
                try:
                    import shutil
                    shutil.copy2(original, file_path)
                    logger.info(f"回退到原仓库版本: {h.file_path}")
                except OSError as e:
                    logger.warning(f"回退失败 {h.file_path}: {e}")
