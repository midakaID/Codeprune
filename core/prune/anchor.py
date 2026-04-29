"""
Phase2: CodePrune — 锚点定位
双通道搜索：语义检索 + LLM 验证
支持 InstructionAnalysis 驱动的精确锚定
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from config import CodePruneConfig, InstructionAnalysis
from core.graph.query import GraphQuery
from core.graph.schema import CodeGraph, CodeNode, EdgeType, NodeType
from core.llm.provider import LLMProvider
from core.llm.prompts import Prompts

logger = logging.getLogger(__name__)


@dataclass
class AnchorResult:
    """锚点定位结果"""
    node_id: str
    node: CodeNode
    relevance_score: float      # 语义检索得分
    confidence: float           # LLM 验证置信度
    reason: str                 # LLM 判断理由
    req_ids: list = field(default_factory=list)  # 该锚点服务的需求 ID


@dataclass
class AnchorOutput:
    """锚点定位完整输出"""
    anchors: list[AnchorResult]
    query_embedding: list[float]            # 用户指令的 embedding，供闭包求解复用
    closure_query_embedding: list[float]    # 基于 sub_features 的 embedding，用于闭包定界
    diagnostics: dict = field(default_factory=dict)


class AnchorLocator:
    """锚点定位器：在图谱中找到用户描述功能对应的核心代码实体"""

    def __init__(self, config: CodePruneConfig, llm: LLMProvider, graph: CodeGraph):
        self.config = config
        self.llm = llm
        self.graph = graph
        self.query = GraphQuery(graph)
        self._last_downgrade_stats: dict = {}

    def locate(self, user_instruction: str) -> AnchorOutput:
        """
        锚点定位主流程:
        路径 A（有 InstructionAnalysis）: 三层合并 → LLM 验证
        路径 B（fallback）: 原始 embedding 检索 → LLM 验证
        """
        logger.info(f"开始锚点定位: '{user_instruction}'")

        analysis = self.config.instruction_analysis  # Phase 2.0 产出
        diagnostics: dict = {
            "analysis_available": bool(analysis and analysis.sub_features),
            "retrieval_path": "analysis" if analysis and analysis.sub_features else "fallback",
            "warnings": [],
        }

        # Step 1: 用户指令 → embedding（如果启用）
        embedding_available = getattr(self.config.graph, 'enable_embedding', True)
        if embedding_available:
            query_emb = self.llm.embed([user_instruction])[0]
        else:
            query_emb = []

        # Step 2: 语义检索候选
        if embedding_available:
            self.query.build_embedding_index()

        if analysis and analysis.sub_features:
            # 路径 A: 基于 InstructionAnalysis 的精确锚定
            if embedding_available:
                candidates = self._locate_from_analysis(analysis, query_emb)
            else:
                candidates = self._locate_from_analysis_by_tags(analysis)
            logger.info(f"路径 A (InstructionAnalysis): {len(candidates)} 个候选")
        else:
            # 路径 B: fallback 到原始 embedding 检索 或 tag 匹配
            if embedding_available:
                candidates = self.query.semantic_search(
                    query_emb, top_k=self.config.prune.anchor_top_k,
                )
            else:
                candidates = self._tag_based_prefilter(
                    user_instruction, top_k=self.config.prune.anchor_top_k,
                )
            logger.info(f"路径 B (fallback): {len(candidates)} 个候选")
            diagnostics["warnings"].append("instruction_analysis_fallback")
        diagnostics["initial_candidate_count"] = len(candidates)

        # Step 2b: 名称/关键词辅助检索 — 合并到候选集
        keyword_candidates = self._keyword_search(user_instruction)
        existing_ids = {nid for nid, _ in candidates}
        keyword_added = 0
        for nid, score in keyword_candidates:
            if nid not in existing_ids:
                candidates.append((nid, score))
                existing_ids.add(nid)
                keyword_added += 1
        diagnostics["keyword_candidates_added"] = keyword_added
        if keyword_added:
            logger.info(f"关键词检索补充 {keyword_added} 个候选")

        # Step 2c: 硬过滤 out_of_scope — 被排除的节点不应成为锚点
        filtered = 0
        if analysis and analysis.out_of_scope:
            before_count = len(candidates)
            candidates = [
                (nid, score) for nid, score in candidates
                if not self._in_excluded_scope(self.graph.get_node(nid), analysis.out_of_scope)
            ]
            filtered = before_count - len(candidates)
            if filtered:
                logger.info(f"排除 out_of_scope 候选: {filtered} 个")
            if filtered > before_count * 0.5:
                logger.warning(
                    f"⚠ out_of_scope 过滤了超过 50% 候选 ({filtered}/{before_count})，"
                    f"请检查 out_of_scope 是否过于宽泛: {analysis.out_of_scope}"
                )
                diagnostics["warnings"].append("out_of_scope_filtered_majority_candidates")
        diagnostics["candidate_count_after_filters"] = len(candidates)
        diagnostics["out_of_scope_filtered"] = filtered

        # Step 3: LLM 验证
        anchors = []
        for node_id, score in candidates:
            node = self.graph.get_node(node_id)
            if not node or not node.summary:
                continue
            verification = self._verify_candidate(node, user_instruction)
            if verification:
                confidence = verification["confidence"]
                # 低质量摘要的节点降权 — 减少其作为锚点的可能性
                if node.metadata.get("summary_quality") == "low":
                    confidence *= 0.7
                if confidence >= self.config.prune.anchor_confidence_threshold:
                    anchors.append(AnchorResult(
                        node_id=node_id,
                        node=node,
                        relevance_score=score,
                        confidence=confidence,
                        reason=verification.get("reason", ""),
                        req_ids=list(getattr(self, '_anchor_req_map', {}).get(node_id, [])),
                    ))

        anchors.sort(key=lambda a: a.confidence, reverse=True)
        if anchors:
            logger.info(
                f"LLM 验证通过 {len(anchors)}/{len(candidates)} 个候选, "
                f"置信度 [{anchors[-1].confidence:.2f}, {anchors[0].confidence:.2f}]"
            )
        diagnostics["verified_anchor_count"] = len(anchors)
        diagnostics["verified_candidate_count"] = len(candidates)

        # Step 4a: 自适应限制 — 根据 anchor_strategy 调整上限
        _STRATEGY_MAX = {"focused": 5, "distributed": 12, "broad": 20}
        analysis = self.config.instruction_analysis
        if analysis:
            max_anchors = _STRATEGY_MAX.get(analysis.anchor_strategy, 10)
        else:
            # F25a: analysis 为空时(LLM 失败等)，根据指令复杂度估计策略
            max_anchors = self._estimate_max_anchors(user_instruction)
        diagnostics["max_anchor_budget"] = max_anchors
        if len(anchors) > max_anchors:
            logger.info(f"锚点过多 ({len(anchors)})，截取 Top-{max_anchors}")
            anchors = anchors[:max_anchors]
            diagnostics["warnings"].append("anchor_budget_trimmed")

        # Step 4a+: 指令中显式提到的文件路径 → 硬锚点 (F25b)
        anchors, explicit_added = self._ensure_explicit_file_anchors(
            anchors, candidates, user_instruction,
        )
        diagnostics["explicit_file_anchor_count"] = explicit_added

        # Step 4b: 空锚点兜底 — 放宽阈值重试 or 回退纯 embedding Top-3
        if not anchors and candidates:
            logger.warning("LLM 验证全部未通过，尝试放宽阈值兜底")
            diagnostics["warnings"].append("anchor_fallback_triggered")
            fallback_threshold = self.config.prune.anchor_confidence_threshold * 0.5
            for node_id, score in candidates[:5]:
                node = self.graph.get_node(node_id)
                if not node or not node.summary:
                    continue
                v = self._verify_candidate(node, user_instruction)
                if v and v.get("confidence", 0) >= fallback_threshold:
                    anchors.append(AnchorResult(
                        node_id=node_id, node=node,
                        relevance_score=score,
                        confidence=v["confidence"],
                        reason=f"(兜底) {v.get('reason', '')}",
                    ))
            if not anchors:
                # 最终兜底：纯 embedding Top-3，不经 LLM 验证
                for node_id, score in candidates[:3]:
                    node = self.graph.get_node(node_id)
                    if node:
                        anchors.append(AnchorResult(
                            node_id=node_id, node=node,
                            relevance_score=score, confidence=0.0,
                            reason="(embedding 兜底，无 LLM 验证)",
                        ))
                logger.warning(f"兜底: 使用 embedding Top-{len(anchors)} 作为锚点")
                diagnostics["warnings"].append("embedding_topk_fallback")

        # Step 5: FILE/CLASS 粒度降级 — 避免闭包膨胀
        if embedding_available:
            closure_query_emb = self._build_closure_query_embedding(query_emb)
        else:
            closure_query_emb = []
        anchors_before_downgrade = len(anchors)
        anchors = self._downgrade_coarse_anchors(anchors, closure_query_emb)
        anchors_after_downgrade = len(anchors)
        diagnostics["anchors_before_seed_expansion"] = anchors_before_downgrade
        diagnostics["anchors_after_seed_expansion"] = anchors_after_downgrade
        diagnostics["downgrade"] = dict(self._last_downgrade_stats)
        if anchors_before_downgrade > 0:
            expansion_ratio = anchors_after_downgrade / anchors_before_downgrade
            diagnostics["anchor_expansion_ratio"] = round(expansion_ratio, 3)
            if expansion_ratio > self.config.prune.anchor_expansion_warning_ratio:
                diagnostics["warnings"].append("anchor_expansion_ratio_high")
        diagnostics["final_anchor_count"] = len(anchors)

        logger.info(f"锚点定位完成: {len(anchors)} 个锚点")
        return AnchorOutput(
            anchors=anchors,
            query_embedding=query_emb,
            closure_query_embedding=closure_query_emb,
            diagnostics=diagnostics,
        )

    def _build_closure_query_embedding(self, fallback_embedding: list[float]) -> list[float]:
        """
        A2: 生成闭包定界专用 embedding。
        用 sub_features 的描述拼合生成，更接近代码摘要语域，消除自然语言偏移。
        """
        analysis = self.config.instruction_analysis
        if analysis and analysis.sub_features:
            closure_text = " | ".join(sf.description for sf in analysis.sub_features)
            try:
                return self.llm.embed([closure_text])[0]
            except Exception as e:
                logger.warning(f"闭包 query embedding 生成失败，回退到原始 embedding: {e}")
        return fallback_embedding

    def _downgrade_coarse_anchors(
        self, anchors: list[AnchorResult], query_embedding: list[float],
    ) -> list[AnchorResult]:
        """
        将 FILE/CLASS 级别的锚点收敛为少量函数 seed，避免大文件/大类一次性膨胀。
        F24: 对含多个顶层类的 FILE 锚点，优先保留被其他锚点文件实际导入的类。
        """
        result: list[AnchorResult] = []
        seen_ids: set[str] = set()
        stats = {
            "coarse_input_count": 0,
            "coarse_retained_count": 0,
            "file_anchor_expansions": 0,
            "class_anchor_expansions": 0,
            "seed_nodes_added": 0,
            "trimmed_descendants": 0,
        }

        # 收集所有锚点的 file node ID
        anchor_file_ids: set[str] = set()
        for a in anchors:
            if a.node.file_path:
                anchor_file_ids.add(f"file:{a.node.file_path}")

        for anchor in anchors:
            if anchor.node.node_type == NodeType.FILE:
                stats["coarse_input_count"] += 1
                selected_funcs, trimmed = self._select_file_anchor_seeds(
                    anchor, query_embedding, anchor_file_ids,
                )
                stats["trimmed_descendants"] += trimmed
                if selected_funcs:
                    stats["file_anchor_expansions"] += 1
                    stats["seed_nodes_added"] += self._append_seed_anchors(
                        result, seen_ids, selected_funcs, anchor,
                    )
                    continue

            elif anchor.node.node_type == NodeType.CLASS:
                stats["coarse_input_count"] += 1
                selected_funcs, trimmed = self._select_class_anchor_seeds(
                    anchor, query_embedding,
                )
                stats["trimmed_descendants"] += trimmed
                if selected_funcs:
                    stats["class_anchor_expansions"] += 1
                    stats["seed_nodes_added"] += self._append_seed_anchors(
                        result, seen_ids, selected_funcs, anchor,
                    )
                    continue

            # FUNCTION 锚点 或 没有子函数 → 保留原锚点
            if anchor.node_id not in seen_ids:
                seen_ids.add(anchor.node_id)
                result.append(anchor)
                if anchor.node.node_type in (NodeType.FILE, NodeType.CLASS):
                    stats["coarse_retained_count"] += 1

        self._last_downgrade_stats = stats

        return result

    def _select_file_anchor_seeds(
        self,
        anchor: AnchorResult,
        query_embedding: list[float],
        anchor_file_ids: set[str],
    ) -> tuple[list[CodeNode], int]:
        """FILE 锚点只展开少量代表性函数。"""
        descendants = self._get_descendant_functions(anchor.node)
        if not descendants:
            return [], 0

        imported_names = self._get_imported_names_from_anchors(
            anchor.node_id, anchor_file_ids,
        )
        candidates = descendants
        if imported_names:
            narrowed = [
                func for func in descendants
                if func.name in imported_names or self._get_parent_name(func) in imported_names
            ]
            if narrowed:
                candidates = narrowed

        budget = self.config.prune.file_anchor_seed_budget
        ranked = self._rank_seed_functions(
            candidates, query_embedding, anchor, imported_names,
        )
        selected = ranked[:budget]
        trimmed = max(0, len(descendants) - len(selected))
        return selected, trimmed

    def _select_class_anchor_seeds(
        self, anchor: AnchorResult, query_embedding: list[float],
    ) -> tuple[list[CodeNode], int]:
        """CLASS 锚点只展开少量代表性方法。"""
        descendants = self._get_descendant_functions(anchor.node)
        if not descendants:
            return [], 0
        budget = self.config.prune.class_anchor_seed_budget
        ranked = self._rank_seed_functions(
            descendants, query_embedding, anchor, {anchor.node.name},
        )
        selected = ranked[:budget]
        trimmed = max(0, len(descendants) - len(selected))
        return selected, trimmed

    def _rank_seed_functions(
        self,
        funcs: list[CodeNode],
        query_embedding: list[float],
        anchor: AnchorResult,
        preferred_names: set[str] | None = None,
    ) -> list[CodeNode]:
        """按语义相关度 + 结构信号排序候选 seed 函数。"""
        preferred_names = preferred_names or set()
        file_stem = ""
        if anchor.node.file_path:
            file_stem = Path(anchor.node.file_path).stem.lower()

        # 收集 feature 关键词（用于 tag overlap 评分）
        feature_keywords: set[str] = set()
        analysis = self.config.instruction_analysis
        if analysis and analysis.sub_features:
            for sf in analysis.sub_features:
                feature_keywords.update(
                    w.lower() for w in re.findall(r'[a-z]{3,}', sf.description.lower())
                )

        scored: list[tuple[float, CodeNode]] = []
        for func in funcs:
            score = anchor.confidence * 0.3 + anchor.relevance_score * 0.3
            # 优先用 embedding cosine，回退到 tag overlap
            if func.embedding is not None and query_embedding:
                score += self._cosine_sim(func.embedding, query_embedding)
            elif feature_keywords:
                func_tags = set(func.metadata.get("functional_tags", []))
                func_name_parts = set(re.findall(r'[a-z]{3,}', func.name.lower()))
                tag_overlap = len((func_tags | func_name_parts) & feature_keywords)
                score += min(0.4, tag_overlap * 0.1)
            parent_name = self._get_parent_name(func)
            if func.name in preferred_names:
                score += 1.0
            if parent_name and parent_name in preferred_names:
                score += 0.8
            if func.metadata.get("is_entry_point"):
                score += 0.15
            if file_stem and (
                func.name.lower() == file_stem
                or (parent_name and parent_name.lower() == file_stem)
            ):
                score += 0.12

            incoming_calls = len(self.graph.get_incoming(func.id, EdgeType.CALLS))
            outgoing_calls = len(self.graph.get_outgoing(func.id, EdgeType.CALLS))
            score += min(0.15, 0.02 * (incoming_calls + min(outgoing_calls, 3)))
            scored.append((score, func))

        scored.sort(key=lambda item: (-item[0], item[1].qualified_name))
        return [func for _, func in scored]

    def _append_seed_anchors(
        self,
        result: list[AnchorResult],
        seen_ids: set[str],
        funcs: list[CodeNode],
        source_anchor: AnchorResult,
    ) -> int:
        """把选中的函数 seed 追加为新的锚点结果。"""
        added = 0
        for func in funcs:
            if func.id in seen_ids:
                continue
            seen_ids.add(func.id)
            result.append(AnchorResult(
                node_id=func.id,
                node=func,
                relevance_score=source_anchor.relevance_score,
                confidence=source_anchor.confidence,
                reason=(
                    f"(seed from {source_anchor.node.node_type.value} "
                    f"{source_anchor.node.name}) {source_anchor.reason}"
                ),
                req_ids=source_anchor.req_ids,
            ))
            added += 1
        return added

    def _get_imported_names_from_anchors(
        self, target_file_id: str, anchor_file_ids: set[str],
    ) -> set[str]:
        """收集其他锚点文件对目标文件的导入符号名（排除 barrel 文件的转发导入）"""
        names: set[str] = set()
        for fid in anchor_file_ids:
            if fid == target_file_id:
                continue
            # 跳过 barrel/re-export 文件（__init__.py / index.ts）— 它们的导入是转发
            fnode = self.graph.get_node(fid)
            if fnode and fnode.name in ("__init__.py", "index.ts", "index.js", "index.tsx"):
                continue
            for edge in self.graph.get_outgoing(fid):
                if edge.edge_type == EdgeType.IMPORTS and edge.target == target_file_id:
                    syms = edge.metadata.get("imported_symbols", [])
                    names.update(syms)
        return names

    def _get_descendant_functions(self, node: CodeNode) -> list[CodeNode]:
        """获取节点下所有 FUNCTION 类型的后代节点"""
        funcs: list[CodeNode] = []
        for child_id in node.children:
            child = self.graph.get_node(child_id)
            if not child:
                continue
            if child.node_type == NodeType.FUNCTION:
                funcs.append(child)
            elif child.node_type in (NodeType.CLASS, NodeType.FILE):
                funcs.extend(self._get_descendant_functions(child))
        return funcs

    def _get_parent_name(self, node: CodeNode) -> str | None:
        """获取函数所属的父类名称（如有）。"""
        for edge in self.graph.get_incoming(node.id, EdgeType.CONTAINS):
            parent = self.graph.get_node(edge.source)
            if parent and parent.node_type in (NodeType.CLASS, NodeType.INTERFACE):
                return parent.name
        return None

    @staticmethod
    def _cosine_sim(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def _tag_based_prefilter(
        self, user_instruction: str, top_k: int = 50,
    ) -> list[tuple[str, float]]:
        """基于 functional_tags 的文本匹配预过滤（embedding 不可用时的降级路径）"""
        keywords = set(
            w.lower() for w in re.findall(r'[a-z]{3,}', user_instruction.lower())
        )
        # 从 InstructionAnalysis 补充关键词
        analysis = self.config.instruction_analysis
        if analysis and analysis.sub_features:
            for sf in analysis.sub_features:
                keywords.update(
                    w.lower() for w in re.findall(r'[a-z]{3,}', sf.description.lower())
                )

        scored: list[tuple[str, float]] = []
        for nid, node in self.graph.nodes.items():
            if node.node_type in (NodeType.DIRECTORY, NodeType.REPOSITORY):
                continue
            if not node.summary:
                continue
            tags = set(node.metadata.get("functional_tags", []))
            name_parts = set(re.findall(r'[a-z]{3,}', node.name.lower()))
            overlap = len((tags | name_parts) & keywords)
            if overlap > 0:
                scored.append((nid, overlap / max(len(keywords), 1)))

        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    def _locate_from_analysis_by_tags(
        self, analysis: InstructionAnalysis,
    ) -> list[tuple[str, float]]:
        """基于 InstructionAnalysis + tags 的锚点候选检索（embedding 不可用时的降级路径）"""
        all_candidates: dict[str, float] = {}
        self._anchor_req_map: dict[str, set[str]] = {}

        for sf in analysis.sub_features:
            # 从 root_entities 直接查找
            for root_name in sf.root_entities:
                for nid, node in self.graph.nodes.items():
                    if node.qualified_name == root_name or node.name == root_name:
                        score = max(all_candidates.get(nid, 0.0), 0.9)
                        all_candidates[nid] = score
                        self._anchor_req_map.setdefault(nid, set()).add(sf.req_id)

            # 用 tag 匹配补充候选
            keywords = set(re.findall(r'[a-z]{3,}', sf.description.lower()))
            for nid, node in self.graph.nodes.items():
                if nid in all_candidates:
                    continue
                if node.node_type in (NodeType.DIRECTORY, NodeType.REPOSITORY):
                    continue
                if not node.summary:
                    continue
                tags = set(node.metadata.get("functional_tags", []))
                name_parts = set(re.findall(r'[a-z]{3,}', node.name.lower()))
                overlap = len((tags | name_parts) & keywords)
                if overlap >= 2:
                    score = min(0.8, overlap * 0.15)
                    if nid not in all_candidates or all_candidates[nid] < score:
                        all_candidates[nid] = score
                        self._anchor_req_map.setdefault(nid, set()).add(sf.req_id)

        return sorted(all_candidates.items(), key=lambda x: -x[1])

    def _verify_candidate(self, node: CodeNode, user_instruction: str) -> dict | None:
        """LLM 验证单个候选节点（增强版：带调用关系上下文 + sub_features）"""
        analysis = self.config.instruction_analysis
        if analysis and analysis.sub_features:
            features_text = "\n".join(f"- {sf.description}" for sf in analysis.sub_features)
            exclusions_section = ""
            if analysis.out_of_scope:
                exclusions_section = (
                    "Exclusions (do NOT include): " + ", ".join(analysis.out_of_scope)
                )
        else:
            features_text = user_instruction
            exclusions_section = ""

        call_context = self._build_call_context(node)

        # B-Step1: 注入功能簇上下文
        cluster_ctx = ""
        if node.metadata.get("cluster_summary"):
            members = node.metadata.get("cluster_members", [])
            member_names = [
                self.graph.get_node(m).name
                for m in members if self.graph.get_node(m)
            ]
            if member_names:
                cluster_ctx = (
                    f"\nFunctional cluster: {node.metadata['cluster_summary']} "
                    f"(cooperates with: {', '.join(member_names)})"
                )

        prompt = Prompts.VERIFY_ANCHOR.format(
            features_text=features_text,
            exclusions_section=exclusions_section,
            name=node.qualified_name,
            node_type=node.node_type.value,
            summary=node.summary,
            file_path=node.file_path or "unknown",
            call_context=f"\n{call_context}" if call_context else "",
        )
        if cluster_ctx:
            prompt += cluster_ctx
        try:
            result = self.llm.fast_chat_json([{"role": "user", "content": prompt}])
            if result.get("relevant"):
                return result
            return None
        except Exception as e:
            logger.warning(f"锚点验证失败 [{node.id}]: {e}")
            return None

    def _build_call_context(self, node: CodeNode, max_each: int = 3) -> str:
        """从图谱中提取节点的真实调用关系"""
        lines = []

        callers = self.graph.get_incoming(node.id, EdgeType.CALLS)[:max_each]
        if callers:
            items = []
            for edge in callers:
                caller = self.graph.get_node(edge.source)
                if caller:
                    desc = f"  {caller.qualified_name}"
                    if caller.summary:
                        desc += f" — {caller.summary}"
                    items.append(desc)
            if items:
                lines.append("Called by:\n" + "\n".join(items))

        outgoing = self.graph.get_outgoing(node.id)
        callees = [e for e in outgoing if e.edge_type == EdgeType.CALLS][:max_each]
        if callees:
            items = []
            for edge in callees:
                callee = self.graph.get_node(edge.target)
                if callee:
                    desc = f"  {callee.qualified_name}"
                    if callee.summary:
                        desc += f" — {callee.summary}"
                    items.append(desc)
            if items:
                lines.append("Calls:\n" + "\n".join(items))

        return "\n".join(lines)

    def _locate_from_analysis(
        self, analysis: InstructionAnalysis, query_emb: list[float],
    ) -> list[tuple[str, float]]:
        """从 InstructionAnalysis 生成锚点候选 — 三层合并"""
        all_candidates: dict[str, float] = {}
        self._anchor_req_map: dict[str, set[str]] = {}  # node_id → req_ids

        for sf in analysis.sub_features:
            # 来源 1: LLM 直选的 root_entities — 高分直接加入
            for qname in sf.root_entities:
                node = self._find_by_qualified_name(qname)
                if node:
                    all_candidates[node.id] = max(
                        all_candidates.get(node.id, 0), 0.95,
                    )
                    self._anchor_req_map.setdefault(node.id, set()).add(sf.req_id)

            # 来源 2: 子功能描述的 embedding 检索 — 补充覆盖
            sf_emb = self.llm.embed([sf.description])[0]
            per_k = max(5, self.config.prune.anchor_top_k // len(analysis.sub_features))
            hits = self.query.semantic_search(sf_emb, top_k=per_k)
            for nid, score in hits:
                node = self.graph.get_node(nid)
                if node and not self._in_excluded_scope(node, analysis.out_of_scope):
                    all_candidates[nid] = max(all_candidates.get(nid, 0), score)
                    self._anchor_req_map.setdefault(nid, set()).add(sf.req_id)

        sorted_candidates = sorted(
            all_candidates.items(), key=lambda x: x[1], reverse=True,
        )
        return sorted_candidates[: self.config.prune.anchor_top_k]

    def _find_by_qualified_name(self, qname: str) -> CodeNode | None:
        """按 qualified_name 精确查找节点"""
        for node in self.graph.nodes.values():
            if node.qualified_name == qname:
                return node
        return None

    @staticmethod
    def _in_excluded_scope(node: CodeNode, out_of_scope: list[str]) -> bool:
        """节点是否在排除范围内（支持目录前缀 + 相对路径后缀 + 文件名匹配）"""
        if not node or not node.file_path or not out_of_scope:
            return False
        path_str = str(node.file_path).replace("\\", "/")
        for item in out_of_scope:
            item_n = item.replace("\\", "/").rstrip("/")
            if path_str == item_n:
                return True
            if item.endswith("/") and (
                path_str.startswith(item_n + "/") or path_str == item_n
            ):
                return True
            if "/" in item_n and path_str.endswith("/" + item_n):
                return True
            if "/" not in item_n and (
                path_str.endswith("/" + item_n) or path_str == item_n
            ):
                return True
        return False

    def _keyword_search(self, user_instruction: str) -> list[tuple[str, float]]:
        """
        基于名称/关键词的辅助检索通道。
        从用户指令中提取英文标识符风格的关键词，与节点 name/qualified_name 做模糊匹配。
        """
        # 提取指令中的英文单词和驼峰/蛇形标识符
        words = set(re.findall(r"[A-Za-z_]\w{2,}", user_instruction))
        # 中文指令时尝试提取关键术语对应的编程词汇不实际，主要靠 embedding
        if not words:
            return []

        # 将词汇转为小写用于匹配
        lower_words = {w.lower() for w in words}

        results: list[tuple[str, float]] = []
        for nid, node in self.graph.nodes.items():
            if node.node_type in (NodeType.REPOSITORY, NodeType.DIRECTORY):
                continue
            name_lower = node.name.lower()
            qname_lower = node.qualified_name.lower()
            # 计算匹配度
            match_count = sum(1 for w in lower_words if w in name_lower or w in qname_lower)
            if match_count > 0:
                score = match_count / len(lower_words)  # 0~1
                results.append((nid, score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:10]  # 最多补充 10 个

    # ── F25a: 指令复杂度估计 ──

    @staticmethod
    def _estimate_max_anchors(user_instruction: str) -> int:
        """
        当 InstructionAnalysis 为空（LLM 调用失败）时，
        根据指令文本复杂度粗估 max_anchors。
        统计"保留/移除"关键词数量作为功能点指标。
        """
        keep_keywords = len(re.findall(r"保留|keep|retain|include", user_instruction, re.I))
        remove_keywords = len(re.findall(r"移除|删除|remove|delete|移除", user_instruction, re.I))
        feature_count = keep_keywords + remove_keywords
        if feature_count <= 3:
            return 5   # focused
        elif feature_count <= 8:
            return 12  # distributed
        else:
            return 20  # broad

    # ── F25b: 指令中显式文件路径 → 硬锚点 ──

    def _ensure_explicit_file_anchors(
        self,
        anchors: list[AnchorResult],
        candidates: list[tuple[str, float]],
        user_instruction: str,
    ) -> tuple[list[AnchorResult], int]:
        """
        扫描指令文本中显式提到的文件名（如 retry.py、local.py）或
        裸模块关键词（如 vector、common），检查它们是否已在锚点列表中。
        如未命中则从图谱节点中补充为硬锚点。
        """
        # ── F27: 增强匹配 ──
        # 1) 带扩展名的完整文件名
        mentioned_files = set(re.findall(
            r"[\w/\\.-]+\.(?:py|java|c|h|ts|js|tsx|jsx)\b", user_instruction,
        ))
        # 2) 裸关键词：中文顿号/逗号/空格分隔的标识符（如 "保留 common、vector"）
        bare_keywords = set(re.findall(
            r"(?<=[、，,\s])([a-zA-Z_][\w]*?)(?=[、，,\s.;]|$)", user_instruction,
        ))
        # 排除常见非文件关键词
        _STOP_WORDS = {
            "if", "and", "or", "not", "the", "all", "from", "with",
            "SELECT", "FROM", "WHERE", "AST",
        }
        bare_keywords -= _STOP_WORDS
        bare_keywords -= {Path(f).stem for f in mentioned_files}  # 已有带扩展名的不重复

        # 获取 out_of_scope 用于过滤
        analysis = self.config.instruction_analysis
        out_of_scope = analysis.out_of_scope if analysis else []

        existing_ids = {a.node_id for a in anchors}
        added = 0
        candidate_scores = {nid: score for nid, score in candidates}

        # ── 处理完整文件名（精确匹配） ──
        for mentioned in mentioned_files:
            fname = mentioned.replace("\\", "/").split("/")[-1]
            for nid, node in self.graph.nodes.items():
                if node.node_type != NodeType.FILE:
                    continue
                if node.name == fname and nid not in existing_ids:
                    if self._in_excluded_scope(node, out_of_scope):
                        continue
                    score = candidate_scores.get(nid, 0.5)
                    anchors.append(AnchorResult(
                        node_id=nid, node=node,
                        relevance_score=score, confidence=0.95,
                        reason=f"(F25b 指令显式提及: {mentioned})",
                    ))
                    existing_ids.add(nid)
                    added += 1
                    # F27: 不 break — 同名不同路径的文件都添加

        # ── F27: 处理裸关键词（basename stem 匹配） ──
        for keyword in bare_keywords:
            kw_lower = keyword.lower()
            for nid, node in self.graph.nodes.items():
                if node.node_type != NodeType.FILE:
                    continue
                stem = Path(node.name).stem.lower() if node.name else ""
                if stem == kw_lower and nid not in existing_ids:
                    if self._in_excluded_scope(node, out_of_scope):
                        continue
                    score = candidate_scores.get(nid, 0.5)
                    anchors.append(AnchorResult(
                        node_id=nid, node=node,
                        relevance_score=score, confidence=0.90,
                        reason=f"(F27 指令裸关键词: {keyword})",
                    ))
                    existing_ids.add(nid)
                    added += 1

        if added:
            logger.info(f"F25b: 指令显式文件 → 补充 {added} 个显式文件锚点")
        return anchors, added
