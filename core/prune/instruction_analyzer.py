"""
Phase2: 指令理解 — 两阶段漏斗 + Grounded Reasoning

Phase 2.0: 在锚点定位之前运行
  阶段 A: embedding 驱动的上下文收集 (0 LLM)
  阶段 B: LLM grounded reasoning → InstructionAnalysis (1 LLM)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict

from config import CodePruneConfig, InstructionAnalysis, SubFeature
from core.graph.schema import CodeGraph, CodeNode, NodeType
from core.graph.query import GraphQuery
from core.llm.provider import LLMProvider
from core.llm.prompts import Prompts

logger = logging.getLogger(__name__)


class InstructionAnalyzer:
    """指令理解器：将用户自然语言指令 + 图谱上下文 → InstructionAnalysis"""

    def __init__(self, config: CodePruneConfig, llm: LLMProvider, graph: CodeGraph):
        self.config = config
        self.llm = llm
        self.graph = graph
        self.query = GraphQuery(graph)

    def analyze(self, user_instruction: str) -> InstructionAnalysis | None:
        """
        主入口：
        1. 收集上下文（embedding 自适应选取 + DIRECTORY 摘要）
        2. LLM grounded reasoning → InstructionAnalysis
        3. 验证 + 兜底
        失败时返回 None，调用方 fallback 到原始 locate()。
        """
        if not user_instruction.strip():
            return None

        # ── 阶段 A: 上下文收集 ──
        query_emb = self.llm.embed([user_instruction])[0]
        self.query.build_embedding_index()

        dir_summaries, dir_count = self._collect_directory_summaries()
        candidates = self._select_context_entities(query_emb)
        entity_list = self._format_entity_list(candidates)
        other_files, other_file_count = self._collect_other_files({nid for nid, _ in candidates})

        logger.info(
            f"指令理解上下文: {dir_count} 目录, "
            f"{len(candidates)} 聚焦实体, {other_file_count} 其余文件"
        )

        # ── 阶段 B: LLM Grounded Reasoning ──
        prompt = Prompts.UNDERSTAND_INSTRUCTION.format(
            user_instruction=user_instruction,
            directory_summaries=dir_summaries,
            entity_list=entity_list,
            other_files=other_files,
        )
        # F25c: 支持重试，防止瞬态超时(524)导致 analysis 丢失
        result = None
        for attempt in range(2):
            try:
                result = self.llm.chat_json([{"role": "user", "content": prompt}])
                break
            except Exception as e:
                logger.warning(f"指令理解 LLM 调用失败 (attempt {attempt+1}/2): {e}")
        if result is None:
            return None

        # ── 阶段 C: 验证 + 兜底 ──
        analysis = self._parse_analysis(result, candidates, user_instruction)
        if analysis:
            logger.info(
                f"指令理解完成: {len(analysis.sub_features)} 个子功能, "
                f"策略={analysis.anchor_strategy}, "
                f"排除={analysis.out_of_scope}"
            )
        return analysis

    # ───────── 阶段 A: 上下文收集 ─────────

    def _collect_directory_summaries(self) -> tuple[str, int]:
        """收集全部 DIRECTORY 节点的摘要"""
        lines = []
        for node in self.graph.nodes.values():
            if node.node_type == NodeType.DIRECTORY and node.summary:
                path = node.file_path or node.name
                lines.append(f"- {path}: {node.summary}")
        if not lines:
            return "(no directory summaries available)", 0
        return "\n".join(lines), len(lines)

    def _select_context_entities(
        self, query_emb: list[float], budget_tokens: int = 3000,
    ) -> list[tuple[str, float]]:
        """自适应选取上下文实体 — 信噪比优先"""
        all_scores: list[tuple[str, float]] = []
        for nid, node in self.graph.nodes.items():
            if node.node_type in (NodeType.REPOSITORY, NodeType.DIRECTORY):
                continue
            if node.embedding is not None:
                score = self._cosine_sim(node.embedding, query_emb)
                all_scores.append((nid, score))
        all_scores.sort(key=lambda x: x[1], reverse=True)

        if not all_scores:
            return []

        max_score = all_scores[0][1]
        threshold = max_score * 0.5

        selected = []
        token_count = 0
        for nid, score in all_scores:
            if score < threshold:
                break
            entry_tokens = 40  # 每个实体约 30-50 tokens
            if token_count + entry_tokens > budget_tokens:
                break
            selected.append((nid, score))
            token_count += entry_tokens
            if len(selected) >= 50:
                break
        return selected

    def _format_entity_list(self, candidates: list[tuple[str, float]]) -> str:
        """格式化候选实体列表供 LLM 阅读"""
        lines = []
        for i, (nid, score) in enumerate(candidates, 1):
            node = self.graph.nodes.get(nid)
            if not node:
                continue
            summary = node.summary or "(no summary)"
            fpath = node.file_path or "?"
            lines.append(
                f"[{i}] [{node.node_type.value.upper()}] "
                f"{node.qualified_name} — {summary} (file: {fpath})"
            )
        return "\n".join(lines) if lines else "(no candidates found)"

    def _collect_other_files(
        self, selected_nids: set[str], max_count: int = 100,
    ) -> tuple[str, int]:
        """列出未被选入聚焦区域的文件名"""
        other = []
        for node in self.graph.nodes.values():
            if node.node_type == NodeType.FILE and node.id not in selected_nids:
                other.append(str(node.file_path or node.name))
        other.sort()
        total_count = len(other)
        if len(other) > max_count:
            other = other[:max_count]
        return (", ".join(other) if other else "(none)", total_count)

    @staticmethod
    def _cosine_sim(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    # ───────── 阶段 C: 验证 + 兜底 ─────────

    def _parse_analysis(
        self,
        result: dict,
        candidates: list[tuple[str, float]],
        user_instruction: str,
    ) -> InstructionAnalysis | None:
        """解析并验证 LLM 输出"""
        if not isinstance(result, dict):
            logger.warning("指令理解输出非 dict，放弃")
            return None

        candidate_names = set()
        for nid, _ in candidates:
            node = self.graph.nodes.get(nid)
            if node:
                candidate_names.add(node.qualified_name)

        sub_features = []
        for sf in result.get("sub_features", []):
            roots = sf.get("root_entities", [])
            valid_roots = []
            for r in roots:
                if r in candidate_names:
                    valid_roots.append(r)
                else:
                    match = self._fuzzy_match_name(r, candidate_names)
                    if match:
                        valid_roots.append(match)
                    else:
                        # 文件级回退: LLM 返回 "file::func" 但 func 不存在时，尝试用 file 本身
                        if "::" in r:
                            file_part = r.rsplit("::", 1)[0]
                            file_match = self._fuzzy_match_name(file_part, candidate_names)
                            if file_match and file_match not in valid_roots:
                                valid_roots.append(file_match)
                                logger.info(f"文件级回退: {r} → {file_match}")
                                continue
                        logger.warning(f"LLM 选择了不存在的实体: {r}")

            if not valid_roots:
                logger.warning(f"子功能 '{sf.get('description', '?')}' 无有效 root，后续 embedding 回退")

            sub_features.append(SubFeature(
                description=sf.get("description", ""),
                root_entities=valid_roots,
                reasoning=sf.get("reasoning", ""),
                req_id=f"R{len(sub_features) + 1}",
            ))

        if not sub_features or all(not sf.root_entities for sf in sub_features):
            logger.warning("指令分析未产出有效结果，回退到原始锚点定位流程")
            return None

        strategy = result.get("anchor_strategy", "focused")
        if strategy not in ("focused", "distributed", "broad"):
            strategy = "focused"

        out_of_scope = result.get("out_of_scope", [])
        if not isinstance(out_of_scope, list):
            out_of_scope = []

        out_of_scope = [str(d) for d in out_of_scope]

        # 规则层兜底：从指令文本中提取显式删除目标，与 LLM 产出合并
        explicit = self._extract_explicit_deletions(user_instruction)
        if explicit:
            existing = set(out_of_scope)
            added = [e for e in explicit if e not in existing]
            out_of_scope.extend(added)
            if added:
                logger.info(f"规则层补全 out_of_scope: +{added} → {out_of_scope}")

        # F28: 提取方法级排除符号 / 受限类
        excluded_symbols = self._extract_excluded_symbols(user_instruction)
        restricted_classes = self._extract_restricted_classes(user_instruction)

        # 安全校验：如子功能引用了目录内文件，则该目录不能被整体排除
        out_of_scope = self._sanitize_dir_exclusions(
            out_of_scope, sub_features, repo_path=self.config.repo_path,
        )

        return InstructionAnalysis(
            original=user_instruction,
            sub_features=sub_features,
            out_of_scope=out_of_scope,
            anchor_strategy=strategy,
            excluded_symbols=excluded_symbols,
            restricted_classes=restricted_classes,
        )

    @staticmethod
    def _sanitize_dir_exclusions(
        out_of_scope: list[str], sub_features: list[SubFeature],
        repo_path=None,
    ) -> list[str]:
        """如果子功能的 root_entities 引用了某目录内的文件，
        或根实体文件导入了该目录内的模块，移除该目录的整体排除。"""
        import ast
        from pathlib import Path

        # Step 1: 收集子功能引用的所有路径前缀
        referenced_dirs: set[str] = set()
        root_files: set[str] = set()
        for sf in sub_features:
            for root in sf.root_entities:
                normalized = root.replace("\\", "/")
                if "/" in normalized:
                    dir_prefix = normalized.rsplit("/", 1)[0] + "/"
                    referenced_dirs.add(dir_prefix)
                # 收集根实体所在文件
                file_part = normalized.split("::")[0] if "::" in normalized else normalized
                if file_part.endswith(".py"):
                    root_files.add(file_part)

        # Step 2: 解析根实体文件的 import，收集被导入的包目录
        if repo_path:
            rp = Path(repo_path) if not isinstance(repo_path, Path) else repo_path
            for rf in root_files:
                fpath = rp / rf.replace("/", "\\")
                if not fpath.exists():
                    continue
                try:
                    tree = ast.parse(fpath.read_text(encoding="utf-8", errors="replace"))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        top_pkg = node.module.split(".")[0]
                        pkg_dir = top_pkg + "/"
                        if (rp / top_pkg).is_dir():
                            referenced_dirs.add(pkg_dir)

        # Step 3: 过滤
        removed = []
        cleaned = []
        for item in out_of_scope:
            normalized = item.replace("\\", "/")
            if normalized.endswith("/") and normalized in referenced_dirs:
                removed.append(item)
            else:
                cleaned.append(item)

        if removed:
            logger.warning(
                f"移除与子功能冲突的目录排除: {removed} "
                f"(子功能引用了这些目录中的文件或导入了这些目录的模块)"
            )
        return cleaned

    @staticmethod
    def _extract_explicit_deletions(instruction: str) -> list[str]:
        """从指令文本中提取显式删除目标（正则兜底）。
        匹配中文 删除/去掉/去除/移除 和英文 delete/remove 后跟的文件/目录名。
        """
        deletions: list[str] = []
        # 模式 1: 动词 + 文件名（带扩展名）
        #   e.g. "删除 auth.py" "去掉posts.py" "remove auth.py, posts.py"
        pat_file = re.compile(
            r'(?:删除|去掉|去除|移除|remove|delete)\s*[：:]*\s*'
            r'[「「\'"]?(\w[\w./\\-]*\.\w+)[」」\'"]?',
            re.IGNORECASE,
        )
        # 模式 2: 逗号/顿号/空格分隔的多个文件 — 在动词后搜索逗号列表
        pat_list = re.compile(
            r'(?:删除|去掉|去除|移除|remove|delete)\s*[：:]*\s*'
            r'((?:\w[\w./\\-]*\.\w+\s*[,，、]\s*)*\w[\w./\\-]*\.\w+)',
            re.IGNORECASE,
        )
        for m in pat_list.finditer(instruction):
            items = re.split(r'[,，、]\s*', m.group(1))
            deletions.extend(item.strip() for item in items if item.strip())
        # 补充单独匹配（处理 pat_list 未覆盖的情况）
        for m in pat_file.finditer(instruction):
            deletions.append(m.group(1).strip())
        # 模式 3: 动词 + 目录名（无扩展名，以 / 结尾或明确上下文）
        pat_dir = re.compile(
            r'(?:删除|去掉|去除|移除|remove|delete)\s*[：:]*\s*'
            r'[`「「\'"]?([a-zA-Z_][\w.-]*/)(?!\w)\s*[`」」\'"]?',
            re.IGNORECASE,
        )
        for m in pat_dir.finditer(instruction):
            deletions.append(m.group(1).strip())
        # 去重保序
        seen: set[str] = set()
        result: list[str] = []
        for d in deletions:
            if d not in seen:
                seen.add(d)
                result.append(d)
        return result

    @staticmethod
    def _extract_excluded_symbols(instruction: str) -> list[str]:
        """F28: 从指令文本中提取方法级排除目标。
        匹配 ClassName.methodName 模式（在删除/移除上下文中）。
        e.g. "移除 TicketService.rejectTicket" → ["TicketService.rejectTicket"]
        """
        symbols: list[str] = []
        # 策略: 扫描整条指令，提取所有 UpperCaseClass.lowerMethod 模式
        # 然后过滤掉文件扩展名
        pat = re.compile(r'\b([A-Z]\w+\.[a-z]\w+)\b')
        for m in pat.finditer(instruction):
            sym = m.group(1).strip()
            # 排除文件名（如 TicketRejectedEvent.java）
            if re.search(r'\.(java|py|c|h|ts|js|tsx|jsx)$', sym, re.I):
                continue
            symbols.append(sym)
        # 去重保序
        seen: set[str] = set()
        result: list[str] = []
        for s in symbols:
            if s not in seen:
                seen.add(s)
                result.append(s)
        if result:
            logger.info(f"F28: 提取方法级排除符号: {result}")
        return result

    @staticmethod
    def _extract_restricted_classes(instruction: str) -> list[str]:
        """F28: 提取“只保留部分方法”的类名。
        e.g. "TemplateService 只保留被通知链路使用的模板方法" → ["TemplateService"]
        """
        classes: list[str] = []
        patterns = [
            re.compile(r'`([A-Z]\w+)`\s*只保留[^\n。；;]*方法'),
            re.compile(r'\b([A-Z]\w+)\b\s*只保留[^\n。；;]*方法'),
        ]
        for pat in patterns:
            for m in pat.finditer(instruction):
                name = m.group(1).strip()
                if re.search(r'\.(java|py|c|h|ts|js|tsx|jsx)$', name, re.I):
                    continue
                classes.append(name)
        seen: set[str] = set()
        result: list[str] = []
        for c in classes:
            if c not in seen:
                seen.add(c)
                result.append(c)
        if result:
            logger.info(f"F28: 提取受限类: {result}")
        return result

    @staticmethod
    def _fuzzy_match_name(name: str, candidates: set[str]) -> str | None:
        """
        模糊匹配 LLM 给出的 qualified_name。
        尝试：后缀匹配 + 包含匹配 + 路径规范化匹配。
        多个匹配则放弃（歧义）。
        """
        name_lower = name.lower()
        # 直接后缀匹配
        suffix_matches = [c for c in candidates if c.lower().endswith(name_lower)]
        if len(suffix_matches) == 1:
            return suffix_matches[0]
        # 直接包含匹配
        contains_matches = [c for c in candidates if name_lower in c.lower()]
        if len(contains_matches) == 1:
            return contains_matches[0]

        # 路径规范化：将 Java 包名风格 (com.shop.service.CartService) 转为路径风格
        # 同时处理候选名中的路径分隔符，统一用 / 比较
        name_as_path = name_lower.replace(".", "/")
        for ext in (".java", ".py", ".ts", ".js", ".c", ".h"):
            if name_as_path.endswith(ext):
                break
        else:
            # 尝试对候选名去扩展名后匹配
            for c in candidates:
                c_norm = c.lower().replace("\\", "/")
                c_no_ext = re.sub(r'\.\w+$', '', c_norm)
                if c_no_ext.endswith(name_as_path):
                    suffix_matches.append(c)
            if len(suffix_matches) == 1:
                return suffix_matches[0]

        return None
