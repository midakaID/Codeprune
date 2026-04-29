"""
Universal Error → Deterministic Action Dispatcher

语言无关的错误模式 → 确定性修复 调度框架。

设计原则:
1. 每种语言注册错误模式（regex）和对应的确定性修复 handler
2. Dispatcher 按模式匹配错误 → 执行确定性修复 → 未匹配的留给 LLM Architect
3. Handler 操作文件系统（复制/修改文件），不调用 LLM

使用方式:
    dispatcher = ErrorDispatcher(sub_repo, source_repo, graph, excluded)
    for error in errors:
        if dispatcher.try_fix(error, lang):
            fixed += 1
        else:
            # fall through to LLM Architect
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from core.graph.schema import CodeGraph, Language, NodeType

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Data Structures
# ═══════════════════════════════════════════════════════════════

@dataclass
class ErrorPattern:
    """A registered error pattern with its deterministic handler.

    Attributes:
        name:      Human-readable identifier (for logging)
        regex:     Compiled regex matching error *message* text
        handler:   (match, error, context) → bool — attempts the fix
        languages: Which languages this pattern applies to
    """
    name: str
    regex: re.Pattern
    handler: Callable[..., bool]
    languages: list[Language]


@dataclass
class DispatchContext:
    """Shared mutable context passed to every handler."""
    sub_repo: Path
    source_repo: Path
    graph: CodeGraph
    excluded: list[str] = field(default_factory=list)
    supplemented_files: set[str] = field(default_factory=set)
    # Protected includes: file_path → set of include lines
    # Tracked so they can be re-applied if LLM patches remove them
    protected_includes: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class RepairContext:
    """Records a Dispatcher repair attempt for LLM context enrichment."""
    file_path: str
    error_message: str
    pattern_name: str
    attempted_fix: str
    fix_result: str             # "success" | "failed"
    root_cause: str             # e.g. "symbol X was in pruned file Y"
    graph_evidence: str = ""    # CodeGraph 中的引用关系


# ═══════════════════════════════════════════════════════════════
# Core Dispatcher
# ═══════════════════════════════════════════════════════════════

class ErrorDispatcher:
    """Universal error → deterministic-fix dispatcher.

    Usage inside HealEngine._fix_syntax_errors():
        for error in errors:
            if self._dispatcher.try_fix(error, lang):
                continue   # fixed deterministically, no LLM needed
            # ... fall through to LLM Architect
    """

    def __init__(
        self,
        sub_repo: Path,
        source_repo: Path,
        graph: CodeGraph,
        excluded: list[str] | None = None,
    ):
        self.ctx = DispatchContext(
            sub_repo=sub_repo,
            source_repo=source_repo,
            graph=graph,
            excluded=excluded or [],
        )
        self._patterns: list[ErrorPattern] = []
        self.repair_contexts: list[RepairContext] = []
        self._register_builtin_patterns()

    @property
    def supplemented_files(self) -> set[str]:
        return self.ctx.supplemented_files

    # ── Public API ───────────────────────────────────────────────

    def can_fix(self, error: "ValidationError", lang: Language) -> bool:
        """Check if any registered pattern matches (no side effects)."""
        for pat in self._patterns:
            if lang not in pat.languages:
                continue
            if pat.regex.search(error.message):
                return True
        return False

    def try_fix(self, error: "ValidationError", lang: Language) -> bool:
        """Try every registered pattern for *lang*. Return True if fixed."""
        for pat in self._patterns:
            if lang not in pat.languages:
                continue
            m = pat.regex.search(error.message)
            if m:
                try:
                    if pat.handler(m, error, self.ctx):
                        logger.info(
                            f"[Dispatcher:{pat.name}] 确定性修复 "
                            f"{error.file_path}:{error.line}"
                        )
                        return True
                    else:
                        # Handler 匹配但修复失败 — 记录上下文供 LLM 使用
                        self._record_failed_repair(pat.name, error, m)
                except Exception as exc:
                    logger.debug(f"[Dispatcher:{pat.name}] 处理失败: {exc}")
                    self._record_failed_repair(pat.name, error, m, str(exc))
        return False

    def register(self, pattern: ErrorPattern) -> None:
        """Register an additional pattern (for extensions / tests)."""
        self._patterns.append(pattern)

    def _record_failed_repair(
        self, pattern_name: str, error: "ValidationError",
        match: re.Match, exception: str = "",
    ) -> None:
        """Record a Dispatcher repair attempt that failed, for LLM context."""
        # 从错误消息中提取缺失符号名
        symbol = match.group(1) if match.lastindex else ""
        # 查找 graph 中的定义信息
        graph_evidence = ""
        if symbol:
            for node in self.ctx.graph.nodes.values():
                if node.name == symbol:
                    graph_evidence = (
                        f"{symbol} defined in {node.file_path} "
                        f"(type: {node.node_type.value})"
                    )
                    break
        root_cause = f"symbol '{symbol}' missing" if symbol else "pattern matched but handler failed"
        if exception:
            root_cause += f" ({exception})"

        rc = RepairContext(
            file_path=str(error.file_path),
            error_message=error.message[:300],
            pattern_name=pattern_name,
            attempted_fix=f"Dispatcher:{pattern_name} tried to fix deterministically",
            fix_result="failed",
            root_cause=root_cause,
            graph_evidence=graph_evidence,
        )
        self.repair_contexts.append(rc)
        logger.debug(f"[Dispatcher:{pattern_name}] 修复失败，已记录上下文: {root_cause}")

    def get_failed_repair_context(self) -> str:
        """Format failed repair attempts as LLM-readable context string."""
        if not self.repair_contexts:
            return ""
        lines = ["\n⚠️ DISPATCHER ANALYSIS (deterministic fix was attempted but failed):"]
        for rc in self.repair_contexts:
            lines.append(f"- File: {rc.file_path}")
            lines.append(f"  Error: {rc.error_message}")
            lines.append(f"  Attempted: {rc.attempted_fix}")
            lines.append(f"  Root cause: {rc.root_cause}")
            if rc.graph_evidence:
                lines.append(f"  Graph evidence: {rc.graph_evidence}")
            lines.append("")
        return "\n".join(lines)

    def clear_repair_contexts(self) -> None:
        """Clear repair contexts after they've been consumed by LLM prompt."""
        self.repair_contexts.clear()

    def reapply_protected_includes(self) -> int:
        """Re-apply any previously added includes that LLM patches removed.

        Detects includes that were disabled (``#if 0``, commented out) and
        restores them.  Also removes LLM-generated static fallback stubs
        that conflict with the now-included headers.

        Returns the number of includes re-applied / restored.
        """
        count = 0
        for fpath_str, includes in self.ctx.protected_includes.items():
            fpath = Path(fpath_str)
            if not fpath.exists():
                continue
            try:
                content = fpath.read_text(encoding="utf-8")
            except OSError:
                continue
            lines = content.splitlines(keepends=True)
            modified = False

            for inc in includes:
                if _is_include_active(lines, inc):
                    continue  # include is live, nothing to do

                # Check if LLM wrapped it in #if 0 … #endif — unwrap it
                unwrapped = _unwrap_disabled_include(lines, inc)
                if unwrapped:
                    lines = unwrapped
                    modified = True
                    count += 1
                    logger.info(
                        f"[Dispatcher:protect] 恢复被 LLM #if 0 禁用的 {inc} → {fpath.name}"
                    )
                    continue

                # Include was fully removed — re-insert
                idx = _find_include_insertion_point(lines)
                lines.insert(idx, inc + "\n")
                modified = True
                count += 1
                logger.info(
                    f"[Dispatcher:protect] 重新添加被 LLM 移除的 {inc} → {fpath.name}"
                )

            # Remove LLM-generated static fallback stubs that duplicate
            # functions from the protected headers
            cleaned = _remove_conflicting_stubs(lines, includes, self.ctx)
            if cleaned != lines:
                lines = cleaned
                modified = True
                count += 1

            if modified:
                fpath.write_text("".join(lines), encoding="utf-8")

        return count

    def _record_protected_include(self, file_path: Path, include_line: str) -> None:
        """Record an include so it can be re-applied if LLM removes it."""
        key = str(file_path.resolve())
        self.ctx.protected_includes.setdefault(key, set()).add(include_line)

    # ── Built-in Pattern Registration ────────────────────────────

    def _register_builtin_patterns(self) -> None:
        ALL = list(Language)

        c_langs = [Language.C, Language.CPP]
        py_langs = [Language.PYTHON]
        ts_langs = [Language.TYPESCRIPT, Language.JAVASCRIPT]
        java_langs = [Language.JAVA]

        self._patterns = [
            # ── C / C++ ────────────────────────────────────────
            ErrorPattern(
                name="c_missing_header",
                regex=re.compile(
                    r"fatal error:\s*([^\s:]+):\s*No such file", re.I,
                ),
                handler=_fix_c_missing_header,
                languages=c_langs,
            ),
            ErrorPattern(
                name="c_implicit_decl",
                regex=re.compile(
                    r"implicit declaration of function\s+['\"]?(\w+)", re.I,
                ),
                handler=_fix_c_undeclared_symbol,
                languages=c_langs,
            ),
            ErrorPattern(
                name="c_unknown_type",
                regex=re.compile(
                    r"unknown type name\s+['\"]?(\w+)", re.I,
                ),
                handler=_fix_c_undeclared_symbol,
                languages=c_langs,
            ),
            ErrorPattern(
                name="c_undeclared",
                regex=re.compile(
                    r"['\"]?(\w+)['\"]?\s+undeclared", re.I,
                ),
                handler=_fix_c_undeclared_symbol,
                languages=c_langs,
            ),
            ErrorPattern(
                name="c_no_member",
                regex=re.compile(
                    r"['\"]?(\w+)['\"]?\s+has no member named\s+['\"]?(\w+)", re.I,
                ),
                handler=_fix_c_no_member,
                languages=c_langs,
            ),

            # ── Python ─────────────────────────────────────────
            ErrorPattern(
                name="py_module_not_found",
                regex=re.compile(r"No module named '([^']+)'"),
                handler=_fix_py_module_not_found,
                languages=py_langs,
            ),
            ErrorPattern(
                name="py_cannot_import",
                regex=re.compile(
                    r"cannot import name '([^']+)' from '([^']+)'"
                ),
                handler=_fix_py_import_error,
                languages=py_langs,
            ),

            # ── TypeScript / JavaScript ────────────────────────
            ErrorPattern(
                name="ts_module_not_found",
                regex=re.compile(
                    r"(?:Cannot find module|TS2307).*?'([^']+)'"
                ),
                handler=_fix_ts_module_not_found,
                languages=ts_langs,
            ),

            # ── Java ───────────────────────────────────────────
            ErrorPattern(
                name="java_cannot_find",
                regex=re.compile(
                    r"cannot find symbol.*?(?:class|variable)\s+(\w+)",
                    re.DOTALL,
                ),
                handler=_fix_java_missing_symbol,
                languages=java_langs,
            ),
        ]


# ═══════════════════════════════════════════════════════════════
# C / C++ Handlers
# ═══════════════════════════════════════════════════════════════

_C_SYSTEM_HEADERS = frozenset({
    "stdio.h", "stdlib.h", "string.h", "stddef.h", "stdint.h",
    "stdbool.h", "limits.h", "assert.h", "math.h", "ctype.h",
    "errno.h", "float.h", "signal.h", "time.h", "stdarg.h",
    "unistd.h", "fcntl.h", "sys/types.h", "sys/stat.h",
    "inttypes.h", "locale.h", "setjmp.h", "wchar.h", "wctype.h",
})


def _fix_c_missing_header(
    m: re.Match, error: "ValidationError", ctx: DispatchContext,
) -> bool:
    """Supplement missing C/C++ header from original repo."""
    header_name = m.group(1)

    # System headers → skip (compiler must provide them)
    if header_name in _C_SYSTEM_HEADERS:
        return False

    # Search original repo for this header
    candidates = list(ctx.source_repo.rglob(header_name))
    if not candidates:
        return False

    best = _pick_best_candidate(candidates, error.file_path, ctx)
    if not best:
        return False

    # Copy header to sub_repo
    rel = best.relative_to(ctx.source_repo)
    dst = ctx.sub_repo / rel
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(best, dst)
        ctx.supplemented_files.add(str(rel))
        logger.info(f"[c_missing_header] 补充 {rel}")

    # Also copy paired .c/.cpp implementation
    _supplement_paired_source(best, ctx)

    # Clean up the file that had the error — uncomment pruned include,
    # remove synthetic stubs for types the header provides
    error_file = ctx.sub_repo / error.file_path
    if error_file.exists():
        include_path = _compute_include_path(rel, error.file_path, ctx=ctx)
        _add_include_to_file(error_file, include_path, header_src=best, ctx=ctx)

    return True


def _fix_c_no_member(
    m: re.Match, error: "ValidationError", ctx: DispatchContext,
) -> bool:
    """Fix 'X has no member named Y' by replacing pruned struct with original."""
    type_name = m.group(1)
    member_name = m.group(2)

    # Find the header that defines this struct in the original repo
    declaring_header = _find_c_declaring_header(type_name, ctx)
    if not declaring_header:
        return False

    header_rel = declaring_header.relative_to(ctx.source_repo)
    header_dst = ctx.sub_repo / header_rel

    if header_dst.exists():
        try:
            existing = header_dst.read_text(encoding="utf-8", errors="replace")
            # Check if the member is present (not pruned/commented)
            if re.search(rf"\b{re.escape(member_name)}\b", existing):
                # Check it's not commented out
                for line in existing.splitlines():
                    stripped = line.strip()
                    if member_name in stripped and not stripped.startswith("//") and not stripped.startswith("/*"):
                        return False  # member exists and active, not our problem
            # Member is missing or commented — replace with original
            shutil.copy2(declaring_header, header_dst)
            ctx.supplemented_files.add(str(header_rel))
            logger.info(
                f"[c_no_member] 替换 pruned 头文件 {header_rel} "
                f"(缺少 {type_name}.{member_name})"
            )
            return True
        except OSError:
            pass
    else:
        header_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(declaring_header, header_dst)
        ctx.supplemented_files.add(str(header_rel))
        logger.info(f"[c_no_member] 补充头文件 {header_rel}")
        return True

    return False


def _supplement_paired_source(header: Path, ctx: DispatchContext) -> None:
    """If we supplemented a .h, also copy its .c/.cpp if it exists."""
    if header.suffix not in (".h", ".hpp"):
        return
    for ext in (".c", ".cpp", ".cc"):
        paired = header.with_suffix(ext)
        if paired.exists():
            rel = paired.relative_to(ctx.source_repo)
            paired_dst = ctx.sub_repo / rel
            if not paired_dst.exists():
                paired_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(paired, paired_dst)
                ctx.supplemented_files.add(str(rel))
                logger.info(f"[c_missing_header] 补充配对源文件 {rel}")


def _fix_c_undeclared_symbol(
    m: re.Match, error: "ValidationError", ctx: DispatchContext,
) -> bool:
    """Fix undeclared symbol by finding its declaring header and adding #include."""
    symbol = m.group(1)

    # Skip common system symbols (should be fixed via system headers)
    if symbol in (
        "NULL", "size_t", "uint8_t", "int8_t", "int16_t", "uint16_t",
        "int32_t", "uint32_t", "int64_t", "uint64_t",
        "bool", "true", "false", "FILE", "EOF", "BUFSIZ",
        "ssize_t", "ptrdiff_t", "wchar_t",
    ):
        return False

    # Find which header in the original repo declares this symbol
    declaring_header = _find_c_declaring_header(symbol, ctx)
    if not declaring_header:
        return False

    # Ensure the header is present in sub_repo (and has the declaration)
    header_rel = declaring_header.relative_to(ctx.source_repo)
    header_dst = ctx.sub_repo / header_rel
    if not header_dst.exists():
        header_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(declaring_header, header_dst)
        ctx.supplemented_files.add(str(header_rel))
        logger.info(f"[c_undeclared] 补充声明头文件 {header_rel}")
        _supplement_paired_source(declaring_header, ctx)
    else:
        # Header exists but may be pruned — check if the symbol is declared
        try:
            existing = header_dst.read_text(encoding="utf-8", errors="replace")
            if symbol not in existing:
                # Pruned header missing the declaration — replace with original
                shutil.copy2(declaring_header, header_dst)
                ctx.supplemented_files.add(str(header_rel))
                logger.info(
                    f"[c_undeclared] 替换 pruned 头文件 {header_rel} "
                    f"(缺少 {symbol} 声明)"
                )
        except OSError:
            pass

    # Add #include to the offending file
    error_file = ctx.sub_repo / error.file_path
    if not error_file.exists():
        return True  # header supplemented, that's progress

    # 防止自引用: 如果报错文件就是声明所在的头文件，不要 include 自己
    try:
        if error_file.resolve() == header_dst.resolve():
            logger.debug(f"[c_undeclared] 跳过自引用: {error.file_path} == {header_rel}")
            return True  # header 已补充/替换，不需要自 include
    except OSError:
        pass

    include_path = _compute_include_path(header_rel, error.file_path, ctx=ctx)
    return _add_include_to_file(error_file, include_path, header_src=declaring_header, ctx=ctx)


def _find_c_declaring_header(
    symbol: str, ctx: DispatchContext,
) -> Optional[Path]:
    """Search the original repo for a header declaring *symbol*."""
    # Strategy 1: CodeGraph lookup
    for node in ctx.graph.nodes.values():
        if node.name == symbol and node.node_type in (
            NodeType.FUNCTION, NodeType.CLASS, NodeType.INTERFACE, NodeType.ENUM,
        ):
            fp = node.file_path
            if fp and str(fp).endswith((".h", ".hpp")):
                header = ctx.source_repo / fp
                if header.exists():
                    return header

    # Strategy 2: Regex scan of headers
    esc = re.escape(symbol)
    pattern = re.compile(
        rf"""(?:
            typedef\s+.*?\b{esc}\b          |  # typedef ... Symbol
            struct\s+{esc}\b                 |  # struct Symbol
            enum\s+{esc}\b                   |  # enum Symbol
            \b\w[\w*\s]+\b{esc}\s*\(         |  # RetType Symbol(
            \#define\s+{esc}\b                  # #define Symbol
        )""",
        re.VERBOSE,
    )
    for h_file in ctx.source_repo.rglob("*.[hH]"):
        try:
            content = h_file.read_text(encoding="utf-8", errors="replace")
            if pattern.search(content):
                return h_file
        except OSError:
            continue

    for hpp_file in ctx.source_repo.rglob("*.hpp"):
        try:
            content = hpp_file.read_text(encoding="utf-8", errors="replace")
            if pattern.search(content):
                return hpp_file
        except OSError:
            continue

    return None


def _compute_include_path(
    header_rel: Path, error_file_rel: "Path",
    ctx: DispatchContext | None = None,
) -> str:
    """Compute the #include path from the error file to the header.

    Scans existing source files in the sub-repo to detect the project's
    include convention (e.g. ``#include "common.h"`` vs
    ``#include "core/common.h"``).
    """
    header_basename = header_rel.name

    # Strategy 1: Look at existing source files for the convention
    if ctx and ctx.sub_repo.exists():
        for ext_glob in ("*.c", "*.cpp", "*.h", "*.hpp"):
            for src_file in ctx.sub_repo.rglob(ext_glob):
                try:
                    content = src_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for line in content.splitlines():
                    stripped = line.strip()
                    if (
                        stripped.startswith("#include")
                        and header_basename in stripped
                    ):
                        inc_m = re.match(r'#include\s*"([^"]+)"', stripped)
                        if inc_m:
                            return inc_m.group(1)

    # Strategy 2: Standard convention — include/xxx.h → xxx.h
    header_parts = header_rel.parts
    if "include" in header_parts:
        idx = list(header_parts).index("include")
        return "/".join(header_parts[idx + 1:])

    # Same directory or relative reference
    return str(header_rel).replace("\\", "/")


def _add_include_to_file(
    file_path: Path, include_path: str, header_src: Path | None = None,
    ctx: DispatchContext | None = None,
) -> bool:
    """Add ``#include "include_path"`` to file, cleaning up synthetic stubs.

    When a real #include is added, also:
    1. Uncomment any ``/* #include "X" */`` for the same header
    2. Remove synthetic ``typedef`` stubs that the header now provides
    3. Remove ``/* pruned dependency */`` comments for the same header

    If *ctx* is provided, the include is registered as "protected" so it
    can be re-applied if a later LLM patch removes it.
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError:
        return False

    include_line = f'#include "{include_path}"'
    header_basename = Path(include_path).name  # e.g. "common.h"

    # Phase 1: Uncomment pruned #include for the same header
    lines = content.splitlines(keepends=True)
    modified = False
    already_active = include_line in content

    for i, line in enumerate(lines):
        stripped = line.strip()
        # Uncomment: /* #include "core/common.h" */ → #include "core/common.h"
        if (
            stripped.startswith("/*")
            and "#include" in stripped
            and (include_path in stripped or header_basename in stripped)
        ):
            # Extract the original #include
            m = re.search(r'(#include\s*[<"][^>"]+[>"])', stripped)
            if m:
                lines[i] = m.group(1) + "\n"
                already_active = True
                modified = True
                logger.info(
                    f"[c_cleanup] 恢复注释的 include: {m.group(1)} "
                    f"in {file_path.name}"
                )

    # Phase 2: Remove synthetic typedef stubs if we have the real header
    if header_src and header_src.exists():
        try:
            header_content = header_src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            header_content = ""

        if header_content:
            # Collect type names defined in the real header
            real_types: set[str] = set()
            for tm in re.finditer(
                r"(?:typedef\s+(?:struct|enum|union)\s+\w+\s*\{[^}]*\}\s*(\w+)"
                r"|typedef\s+\w[\w\s*]+\b(\w+)\s*;)",
                header_content, re.DOTALL,
            ):
                name = tm.group(1) or tm.group(2)
                if name:
                    real_types.add(name)

            # Remove synthetic stubs for types the header provides
            for i, line in enumerate(lines):
                stripped = line.strip()
                # Match "typedef int QStatus;" or "typedef int QStatus; /* synthetic */"
                m = re.match(
                    r"(?:/\*.*?\*/\s*)?typedef\s+\w+\s+(\w+)\s*;", stripped,
                )
                if m and m.group(1) in real_types:
                    lines[i] = f"/* [CodePrune] removed synthetic stub: {stripped} */\n"
                    modified = True
                    logger.info(
                        f"[c_cleanup] 移除 synthetic typedef '{m.group(1)}' "
                        f"(real definition in {header_basename})"
                    )

    # Phase 3: Remove "pruned dependency" comments for this header
    for i, line in enumerate(lines):
        stripped = line.strip()
        if (
            stripped.startswith("/*")
            and "pruned" in stripped.lower()
            and (header_basename.removesuffix(".h") in stripped.lower()
                 or include_path in stripped)
        ):
            lines[i] = ""
            modified = True

        # Also remove "X was pruned; ..." explanation lines
        if (
            stripped.startswith("/*")
            and "was pruned" in stripped.lower()
            and header_basename.removesuffix(".h") in stripped.lower()
        ):
            # Multi-line comment: consume until */
            if "*/" not in stripped:
                lines[i] = ""
                j = i + 1
                while j < len(lines) and "*/" not in lines[j]:
                    lines[j] = ""
                    j += 1
                if j < len(lines):
                    lines[j] = ""
            else:
                lines[i] = ""
            modified = True

    # Phase 4: Add the #include if not already present
    if not already_active:
        idx = _find_include_insertion_point(lines)
        lines.insert(idx, include_line + "\n")
        modified = True
        logger.info(f"[c_undeclared] 添加 {include_line} → {file_path.name}")

    if modified:
        file_path.write_text("".join(lines), encoding="utf-8")

    # Record as protected so LLM can't remove it permanently
    if ctx is not None:
        key = str(file_path.resolve())
        ctx.protected_includes.setdefault(key, set()).add(include_line)

    return modified


# ═══════════════════════════════════════════════════════════════
# Protected Include Helpers
# ═══════════════════════════════════════════════════════════════

def _is_include_active(lines: list[str], inc: str) -> bool:
    """Return True if *inc* appears on an active (non-disabled) line.

    An include is "active" only if it appears on a line that is:
    - NOT inside an ``#if 0`` … ``#endif`` block
    - NOT commented out with ``//`` or ``/* … */``
    """
    # Track #if 0 nesting depth
    if_zero_depth = 0
    for line in lines:
        stripped = line.strip()

        # Track #if 0 / #endif pairs
        if re.match(r"#\s*if\s+0\b", stripped):
            if_zero_depth += 1
            continue
        if if_zero_depth > 0:
            if re.match(r"#\s*endif\b", stripped):
                if_zero_depth -= 1
            continue

        # Outside #if 0: check if this line contains the include
        if inc not in stripped:
            continue
        # Skip commented lines
        if stripped.startswith("//"):
            continue
        if stripped.startswith("/*") and "*/" in stripped:
            continue
        # Active include found
        return True
    return False


def _unwrap_disabled_include(
    lines: list[str], inc: str,
) -> list[str] | None:
    """If *inc* is wrapped in ``#if 0 … #endif``, remove the wrapper.

    Returns the modified *lines* list, or ``None`` if not found.
    Handles patterns like::

        #if 0
        #include "foo.h"
        #endif

    Also handles optional comment lines between ``#if 0`` and ``#endif``.
    """
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not re.match(r"#\s*if\s+0\b", stripped):
            continue

        # Scan forward for the include line and the matching #endif
        block_start = i
        include_idx: int | None = None
        j = i + 1
        while j < len(lines):
            s = lines[j].strip()
            if re.match(r"#\s*endif\b", s):
                # Found end of block
                if include_idx is not None:
                    # Unwrap: remove #if 0 and #endif, keep include
                    result = lines[:]
                    result[block_start] = ""  # remove #if 0
                    result[j] = ""  # remove #endif
                    # Remove comment lines between #if 0 and the include
                    for k in range(block_start + 1, include_idx):
                        ks = result[k].strip()
                        if ks.startswith("//") or ks.startswith("/*") or ks == "":
                            result[k] = ""
                    return result
                break
            if inc in s:
                include_idx = j
            # Nested #if inside #if 0 — skip this block
            if re.match(r"#\s*if\b", s):
                break
            j += 1

    return None


def _remove_conflicting_stubs(
    lines: list[str],
    includes: set[str],
    ctx: DispatchContext,
) -> list[str]:
    """Remove LLM-generated ``static`` function stubs that conflict with
    functions declared in protected headers.

    LLM sometimes generates ``static void foo() { ... }`` as a fallback
    when it thinks a header was pruned.  If we have the real header via
    a protected include, these stubs cause "conflicting types" errors.
    """
    # Collect function names declared in the protected headers
    header_funcs: set[str] = set()
    for inc in includes:
        m = re.match(r'#include\s*"([^"]+)"', inc)
        if not m:
            continue
        header_rel = m.group(1)

        # Search in source repo
        for base in (ctx.source_repo, ctx.sub_repo):
            candidates = list(base.rglob(Path(header_rel).name))
            for cand in candidates:
                if str(cand.relative_to(base)).replace("\\", "/").endswith(
                    header_rel.replace("\\", "/")
                ):
                    try:
                        hdr = cand.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    # Extract function names from declarations
                    for fm in re.finditer(
                        r"(?:^|\n)\s*(?:extern\s+)?(?:[\w*\s]+?)\b(\w+)\s*\(",
                        hdr,
                    ):
                        name = fm.group(1)
                        # Skip common non-function keywords
                        if name not in ("if", "for", "while", "switch", "return",
                                        "sizeof", "typedef", "define", "ifdef"):
                            header_funcs.add(name)
                    break

    if not header_funcs:
        return lines

    # Scan for static stubs matching header function names
    result = lines[:]
    i = 0
    while i < len(result):
        stripped = result[i].strip()
        # Match: static <type> <name>( ... ) { ... }
        m = re.match(r"static\s+[\w*\s]+?\b(\w+)\s*\(", stripped)
        if m and m.group(1) in header_funcs:
            func_name = m.group(1)
            # Remove the entire function body
            if "{" in stripped:
                # Inline or starts on this line
                brace_depth = stripped.count("{") - stripped.count("}")
                result[i] = f"/* [CodePrune] removed conflicting static stub: {func_name} */\n"
                j = i + 1
                while j < len(result) and brace_depth > 0:
                    brace_depth += result[j].count("{") - result[j].count("}")
                    result[j] = ""
                    j += 1
                i = j
                logger.info(f"[Dispatcher:protect] 移除冲突 static stub: {func_name}")
                continue
            else:
                # Prototype-only: static void foo(void);
                result[i] = f"/* [CodePrune] removed conflicting static stub: {func_name} */\n"
                logger.info(f"[Dispatcher:protect] 移除冲突 static stub 声明: {func_name}")
        i += 1

    return result


def _find_include_insertion_point(lines: list[str]) -> int:
    """Find the best line index to insert a new ``#include``."""
    last_include = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#include"):
            last_include = i
    if last_include >= 0:
        return last_include + 1

    # No existing includes — after header guard #define
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#define") and "_H" in stripped.upper():
            return i + 1

    # Last resort: very top
    return 0


def _pick_best_candidate(
    candidates: list[Path],
    error_file_rel: "Path",
    ctx: DispatchContext,
) -> Optional[Path]:
    """Pick the best matching file from *candidates* in the original repo."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    error_parent = Path(error_file_rel).parent

    # Prefer same parent directory
    for c in candidates:
        c_rel = c.relative_to(ctx.source_repo)
        if c_rel.parent == error_parent:
            return c

    # Prefer include/ directory
    for c in candidates:
        if "include" in c.parts:
            return c

    return candidates[0]


# ═══════════════════════════════════════════════════════════════
# Python Handlers
# ═══════════════════════════════════════════════════════════════

def _fix_py_module_not_found(
    m: re.Match, error: "ValidationError", ctx: DispatchContext,
) -> bool:
    """Supplement missing Python module from original repo."""
    module_name = m.group(1)
    parts = module_name.replace(".", "/")
    candidates = [
        Path(f"{parts}.py"),
        Path(parts) / "__init__.py",
    ]

    # Excluded → comment out the import
    top_module = module_name.split(".")[0]
    is_excluded = any(
        top_module == ex.replace("\\", "/").rstrip("/").split("/")[0]
        for ex in ctx.excluded
    )
    if is_excluded:
        return _comment_import_in_file(
            ctx.sub_repo / error.file_path, module_name, "#",
        )

    # Supplement from original repo
    for rel_path in candidates:
        src = ctx.source_repo / rel_path
        dst = ctx.sub_repo / rel_path
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            ctx.supplemented_files.add(str(rel_path))
            logger.info(f"[py_module] 补充 {rel_path}")
            return True

    # Already exists — probably fixed in a previous round
    if any((ctx.sub_repo / rp).exists() for rp in candidates):
        return True

    return False


def _fix_py_import_error(
    m: re.Match, error: "ValidationError", ctx: DispatchContext,
) -> bool:
    """Fix Python ``ImportError: cannot import name X from Y``.

    Strategy: find X's definition inside the package and add a re-export
    line to ``__init__.py``.
    """
    symbol = m.group(1)
    source = m.group(2)

    source_dir = ctx.sub_repo / source.replace(".", "/")
    if not source_dir.is_dir():
        return False

    # Search package for symbol definition
    for py_file in source_dir.rglob("*.py"):
        if py_file.name == "__init__.py":
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
        except OSError:
            continue

        if re.search(rf"(?:def|class)\s+{re.escape(symbol)}\b", content):
            init_path = source_dir / "__init__.py"
            if not init_path.exists():
                continue
            try:
                init_content = init_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if symbol in init_content:
                continue  # already exported — not a re-export issue

            rel_module = py_file.stem
            import_line = f"from {source}.{rel_module} import {symbol}\n"
            new_content = init_content.rstrip("\n") + "\n" + import_line
            init_path.write_text(new_content, encoding="utf-8")
            rel = init_path.relative_to(ctx.sub_repo)
            ctx.supplemented_files.add(str(rel))
            logger.info(
                f"[py_import] 补齐 {rel} 中 '{symbol}' 的 re-export"
            )
            return True

    return False


# ═══════════════════════════════════════════════════════════════
# TypeScript / JavaScript Handlers
# ═══════════════════════════════════════════════════════════════

def _fix_ts_module_not_found(
    m: re.Match, error: "ValidationError", ctx: DispatchContext,
) -> bool:
    """Supplement missing TS/JS module from original repo."""
    module_path = m.group(1)
    if not module_path.startswith("."):
        return False  # absolute / node_modules — skip

    error_file = ctx.sub_repo / error.file_path
    src_dir = ctx.source_repo / Path(error.file_path).parent
    base_dir = error_file.parent
    clean = module_path.removesuffix(".ts").removesuffix(".js")

    for ext in (".ts", ".tsx", ".js", ".jsx"):
        src = src_dir / (clean + ext)
        if not src.exists():
            # Try without leading ./
            src = ctx.source_repo / Path(error.file_path).parent / (clean.lstrip("./") + ext)
        if not src.exists():
            continue
        dst = base_dir / (Path(clean).name + ext)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            rel = dst.relative_to(ctx.sub_repo)
            ctx.supplemented_files.add(str(rel))
            logger.info(f"[ts_module] 补充 {rel}")
            return True

    return False


# ═══════════════════════════════════════════════════════════════
# Java Handlers
# ═══════════════════════════════════════════════════════════════

def _fix_java_missing_symbol(
    m: re.Match, error: "ValidationError", ctx: DispatchContext,
) -> bool:
    """Supplement missing Java class file from original repo."""
    symbol = m.group(1)

    for java_file in ctx.source_repo.rglob(f"{symbol}.java"):
        rel = java_file.relative_to(ctx.source_repo)
        dst = ctx.sub_repo / rel
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(java_file, dst)
            ctx.supplemented_files.add(str(rel))
            logger.info(f"[java] 补充 {rel}")
            return True

    return False


# ═══════════════════════════════════════════════════════════════
# Shared Utilities
# ═══════════════════════════════════════════════════════════════

def _comment_import_in_file(
    file_path: Path, module_name: str, comment_char: str,
) -> bool:
    """Comment out all imports of *module_name* in *file_path*."""
    if not file_path.exists():
        return False
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return False

    top = module_name.split(".")[0]
    modified = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{comment_char} [CodePrune]"):
            continue
        if any(kw in stripped for kw in (
            f"import {module_name}",
            f"from {module_name}",
            f"import {top}",
            f"from {top}",
        )):
            lines[i] = f"{comment_char} [CodePrune] removed: {stripped}\n"
            modified = True

    if modified:
        file_path.write_text("".join(lines), encoding="utf-8")
    return modified
