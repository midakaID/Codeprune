"""
Phase1: CodeGraph — 语义层
LLM 驱动的代码摘要生成和 embedding 索引构建
支持功能簇聚合摘要：将互相调用的小函数簇合并生成聚合摘要
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import Optional

from config import CodePruneConfig
from core.graph.schema import CodeGraph, CodeNode, EdgeType, NodeType
from core.llm.provider import LLMProvider
from core.llm.prompts import Prompts

logger = logging.getLogger(__name__)

# 低质量摘要特征词 — 匹配到则标记为 low-quality
_GENERIC_PATTERNS = re.compile(
    r"^(this\s+)?(function|method|class|module)\s+"
    r"(is\s+a\s+)?(helper|utility|wrapper|simple|basic|generic|general)"
    r"|does\s+something|performs?\s+(an?\s+)?(action|operation|task)"
    r"|not\s+sure|unknown\s+purpose",
    re.IGNORECASE,
)

# 停用词 — 用于语义空洞检测
_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "and", "or", "to", "in",
    "for", "of", "on", "at", "by", "it", "its", "this", "that", "with", "from",
    "as", "be", "has", "have", "had", "do", "does", "did", "will", "would",
    "can", "could", "should", "not", "if", "then", "else", "when", "which",
    "return", "returns", "take", "takes", "get", "gets", "set", "sets",
})


class SemanticEnricher:
    """语义层增强器：为图谱节点添加 LLM 摘要和 embedding"""

    def __init__(self, config: CodePruneConfig, llm: LLMProvider, graph: CodeGraph):
        self.config = config
        self.llm = llm
        self.graph = graph

    def enrich(self, node_ids: Optional[list[str]] = None) -> None:
        """
        对指定节点（或全部节点）进行语义增强
        采用 bottom-up 策略：function → class → file → module
        """
        targets = [self.graph.get_node(nid) for nid in node_ids] if node_ids else list(self.graph.nodes.values())
        targets = [n for n in targets if n is not None]

        # C: 入口点标记（零入度 + 命名模式检测）
        self._mark_entry_points(targets)

        layers = [
            NodeType.FUNCTION,
            NodeType.CLASS,
            NodeType.INTERFACE,
            NodeType.ENUM,        # D1: 枚举摘要
            NodeType.NAMESPACE,   # D1: 命名空间摘要
            NodeType.FILE,
            NodeType.DIRECTORY,
        ]

        for layer in layers:
            layer_nodes = [n for n in targets if n.node_type == layer and not n.is_semantic_ready]
            if not layer_nodes:
                continue
            logger.info(f"语义增强 {layer.value} 层: {len(layer_nodes)} 个节点")
            self._summarize_batch(layer_nodes, layer)

            # 在 FUNCTION 层摘要完成后，进行功能簇聚合摘要
            if layer == NodeType.FUNCTION:
                self._enrich_by_call_cluster(targets)

            # 非 FUNCTION 层：从子节点聚合 functional_tags
            if layer != NodeType.FUNCTION:
                self._aggregate_tags(layer_nodes)

        # 构建 embedding 索引（可选，降级为仅 anchor 检索使用）
        if getattr(self.config.graph, 'enable_embedding', True):
            self._build_embeddings(targets)
        else:
            logger.info("Embedding 已禁用，跳过 embedding 生成")

    def _summarize_batch(self, nodes: list[CodeNode], layer: NodeType) -> None:
        """批量生成摘要"""
        batch_size = self.config.graph.summary_batch_size

        for i in range(0, len(nodes), batch_size):
            batch = nodes[i:i + batch_size]
            for node in batch:
                summary = self._summarize_node(node, layer)
                if summary:
                    quality = self._assess_summary_quality(summary, node)
                    node.summary = summary
                    if quality == "low":
                        node.metadata["summary_quality"] = "low"
                        logger.debug(f"低质量摘要 [{node.name}]: {summary[:60]}...")

        # A+D: FUNCTION 层完成首轮后，对 low 质量节点带上下文重试
        if layer == NodeType.FUNCTION:
            low_nodes = [n for n in nodes if n.metadata.get("summary_quality") == "low"]
            if low_nodes:
                logger.info(f"低质量摘要重试: {len(low_nodes)} 个函数")
                self._retry_low_quality(low_nodes)

    def _summarize_node(self, node: CodeNode, layer: NodeType) -> Optional[str]:
        """为单个节点生成摘要"""
        try:
            if layer == NodeType.FUNCTION:
                code = self._read_node_source(node)
                if not code:
                    return None
                # 如果有签名信息，补充到 prompt 中帮助 LLM 理解
                sig_hint = ""
                if node.signature:
                    sig_hint = f"\nSignature: {node.name}{node.signature}"
                # C: 入口点增强提示
                entry_hint = ""
                if node.metadata.get("is_entry_point"):
                    entry_hint = (
                        "\n⚡ This is a SYSTEM ENTRY POINT (top-level, no internal callers). "
                        "Focus on what external interface or user action it exposes."
                    )
                prompt = Prompts.SUMMARIZE_FUNCTION.format(
                    name=node.name, language=node.language.value, code=code,
                ) + sig_hint + entry_hint
                messages = [{"role": "user", "content": prompt}]
                raw = self.llm.fast_chat(messages)
                return self._parse_function_summary(raw, node)
            elif layer in (NodeType.CLASS, NodeType.INTERFACE):
                method_summaries = self._collect_children_summaries(node)
                prompt = Prompts.SUMMARIZE_CLASS.format(
                    name=node.name, language=node.language.value, method_summaries=method_summaries
                )
            elif layer in (NodeType.ENUM, NodeType.NAMESPACE):
                # D1: ENUM/NAMESPACE 复用 CLASS prompt
                member_summaries = self._collect_children_summaries(node)
                prompt = Prompts.SUMMARIZE_CLASS.format(
                    name=node.name, language=node.language.value,
                    method_summaries=member_summaries or f"(members of {layer.value} {node.name})",
                )
            elif layer == NodeType.FILE:
                contents = self._collect_children_summaries(node)
                prompt = Prompts.SUMMARIZE_FILE.format(
                    file_path=node.file_path, language=node.language.value, contents_summary=contents
                )
            elif layer == NodeType.DIRECTORY:
                file_summaries = self._collect_children_summaries(node)
                prompt = Prompts.SUMMARIZE_MODULE.format(
                    module_path=node.qualified_name, file_summaries=file_summaries
                )
            else:
                return None

            messages = [{"role": "user", "content": prompt}]
            return self.llm.fast_chat(messages)

        except Exception as e:
            logger.warning(f"摘要生成失败 [{node.id}]: {e}")
            return None

    def _parse_function_summary(self, raw: Optional[str], node: CodeNode) -> Optional[str]:
        """G1: 解析 FUNCTION 的结构化 JSON 响应，提取 summary、category 和 tags"""
        if not raw:
            return None
        try:
            data = json.loads(raw)
            summary = data.get("summary", "").strip()
            category = data.get("category", "").strip().lower()
            if category in ("business", "utility", "infrastructure", "config", "test"):
                node.metadata["semantic_category"] = category
            # 提取功能标签
            tags = data.get("tags", [])
            if isinstance(tags, list):
                clean_tags = [t.strip().lower() for t in tags if isinstance(t, str) and t.strip()]
                if clean_tags:
                    node.metadata["functional_tags"] = clean_tags[:5]
            return summary or None
        except (json.JSONDecodeError, AttributeError):
            # LLM 未返回有效 JSON — 视为纯文本摘要
            logger.debug(f"JSON 解析失败，回退纯文本: {node.qualified_name}")
            return raw.strip() or None

    def _assess_summary_quality(self, summary: str, node: CodeNode) -> str:
        """
        评估摘要质量:
        - "high": 正常质量
        - "low": 过于泛化或过短
        低质量摘要仍然保留，但标记 metadata 以供锚点/闭包阶段降权
        """
        words = summary.strip().split()

        # 太短（少于 4 个词 → 几乎无语义信息）
        if len(words) < 4:
            return "low"

        # 匹配泛化模式
        if _GENERIC_PATTERNS.search(summary):
            return "low"

        # 摘要与函数名高度重复（只是重述名称）
        if node.name and len(words) <= 6:
            name_parts = set(re.split(r"[_\s]", node.name.lower()))
            summary_parts = set(w.lower().strip(".,;:") for w in words)
            overlap = name_parts & summary_parts
            if len(overlap) >= len(name_parts) * 0.8 and len(name_parts) >= 2:
                return "low"

        # 语义空洞检测 — 摘要没引入任何新实词（只是函数名的同义改写）
        summary_tokens = set(re.findall(r'[a-z]{2,}', summary.lower()))
        name_tokens = set(re.findall(r'[a-z]{2,}', (node.name or '').lower()))
        novel_tokens = summary_tokens - name_tokens - _STOPWORDS
        if len(novel_tokens) < 2:
            return "low"

        return "high"

    def _build_embeddings(self, nodes: list[CodeNode]) -> None:
        """为有摘要的节点生成 embedding，跳过低质量摘要，融入签名+路径增强语义区分"""
        nodes_with_summary = [
            n for n in nodes
            if n.summary and n.embedding is None
            and n.metadata.get("summary_quality") != "low"  # A4: 低质量不生成 embedding
        ]
        if not nodes_with_summary:
            return

        skipped = sum(
            1 for n in nodes
            if n.summary and n.embedding is None
            and n.metadata.get("summary_quality") == "low"
        )
        if skipped:
            logger.info(f"跳过 {skipped} 个低质量摘要节点的 embedding 生成")

        logger.info(f"生成 embedding: {len(nodes_with_summary)} 个节点")
        batch_size = 100
        for i in range(0, len(nodes_with_summary), batch_size):
            batch = nodes_with_summary[i:i + batch_size]
            texts = []
            for n in batch:
                text = n.summary
                if n.node_type == NodeType.FUNCTION and n.signature:
                    # B2: 增加模块路径前缀，增强空间区分力
                    module = ""
                    if n.file_path:
                        module = str(n.file_path).replace("\\", "/").rsplit(".", 1)[0].replace("/", ".") + "."
                    # C: 入口点在 embedding 空间加前缀
                    entry_tag = "[ENTRY] " if n.metadata.get("is_entry_point") else ""
                    text = f"{entry_tag}{module}{n.name}{n.signature}: {text}"
                elif n.node_type in (NodeType.CLASS, NodeType.INTERFACE):
                    # B2: 类也加路径上下文
                    if n.file_path:
                        text = f"{n.file_path}: {text}"
                texts.append(text)
            try:
                embeddings = self.llm.embed(texts)
                for node, emb in zip(batch, embeddings):
                    node.embedding = emb
            except Exception as e:
                logger.warning(f"Embedding 生成失败: {e}")

    def _aggregate_tags(self, nodes: list[CodeNode]) -> None:
        """从子节点聚合 functional_tags 到父节点（CLASS/FILE/DIRECTORY 层）"""
        for node in nodes:
            if node.metadata.get("functional_tags"):
                continue  # 已有自己的 tags
            tag_counter: dict[str, int] = defaultdict(int)
            for child_id in node.children:
                child = self.graph.get_node(child_id)
                if child:
                    for tag in child.metadata.get("functional_tags", []):
                        tag_counter[tag] += 1
            if tag_counter:
                top_tags = sorted(tag_counter.keys(), key=lambda t: tag_counter[t], reverse=True)[:5]
                node.metadata["functional_tags"] = top_tags

    def _enrich_by_call_cluster(self, targets: list[CodeNode]) -> None:
        """
        功能簇聚合摘要：
        1. 用 CALLS 边构建函数级调用子图
        2. 找连通分量 (忽略方向)
        3. B-Step2: 对大连通分量 (>4) 用 LLM 做语义拆分
        4. 对大小 2~8 的最终簇生成聚合摘要
        """
        func_nodes = [n for n in targets if n.node_type == NodeType.FUNCTION]
        if len(func_nodes) < 2:
            return

        func_ids = {n.id for n in func_nodes}
        # 构建无向邻接表
        adj: dict[str, set[str]] = defaultdict(set)
        for nid in func_ids:
            for edge in self.graph.get_outgoing(nid):
                if edge.edge_type == EdgeType.CALLS and edge.target in func_ids:
                    adj[nid].add(edge.target)
                    adj[edge.target].add(nid)

        # BFS 找连通分量
        visited: set[str] = set()
        raw_clusters: list[list[str]] = []
        for nid in func_ids:
            if nid in visited:
                continue
            component: list[str] = []
            queue = [nid]
            visited.add(nid)
            while queue:
                curr = queue.pop()
                component.append(curr)
                for neighbor in adj.get(curr, ()):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)
            if len(component) >= 2:
                raw_clusters.append(component)

        if not raw_clusters:
            return

        # B-Step2: 对大簇 (>4) 用 LLM 语义拆分
        final_clusters: list[list[str]] = []
        for cluster in raw_clusters:
            if len(cluster) <= 4:
                final_clusters.append(cluster)
            elif len(cluster) <= 8:
                # 中等簇: 尝试 LLM split，失败则保留整体
                sub = self._llm_split_cluster(cluster)
                final_clusters.extend(sub)
            else:
                # 大簇: 必须拆分
                sub = self._llm_split_cluster(cluster)
                # 只保留合理大小的子簇
                final_clusters.extend(c for c in sub if len(c) <= 8)

        clusters = [c for c in final_clusters if 2 <= len(c) <= 8]
        if not clusters:
            return

        logger.info(f"功能簇聚合: {len(clusters)} 个簇")
        for cluster in clusters:
            self._summarize_cluster(cluster)

    def _llm_split_cluster(self, cluster_ids: list[str]) -> list[list[str]]:
        """B-Step2: 对大连通分量用 LLM 按功能语义拆分为子簇"""
        members = [(nid, self.graph.get_node(nid)) for nid in cluster_ids]
        members = [(nid, n) for nid, n in members if n is not None]
        if len(members) < 2:
            return [cluster_ids]

        member_info = "\n".join(
            f"- {nid}: {n.name} — {n.summary or '(no summary)'}"
            for nid, n in members
        )
        prompt = (
            "These functions are connected by call relationships but may belong to DIFFERENT features. "
            "Group them into functional sub-clusters based on their PURPOSE.\n"
            "Each sub-cluster should represent ONE coherent feature/responsibility.\n"
            "If all functions genuinely belong together, return a single group.\n\n"
            f"Functions:\n{member_info}\n\n"
            'Respond in JSON: {"clusters": [["id1", "id2"], ["id3", "id4"]]}\n'
            "Use the exact IDs from the list above."
        )
        try:
            result = self.llm.fast_chat_json([{"role": "user", "content": prompt}])
            raw_clusters = result.get("clusters", [])
            if not raw_clusters or not isinstance(raw_clusters, list):
                return [cluster_ids]

            # 验证返回的 ID 合法性
            valid_ids = {nid for nid, _ in members}
            validated = []
            for sub in raw_clusters:
                if isinstance(sub, list):
                    clean = [sid for sid in sub if sid in valid_ids]
                    if clean:
                        validated.append(clean)
            return validated if validated else [cluster_ids]
        except Exception as e:
            logger.debug(f"LLM 簇拆分失败，保留整体: {e}")
            return [cluster_ids]

    def _summarize_cluster(self, cluster_ids: list[str]) -> None:
        """为一个功能簇生成聚合摘要并注入到成员节点"""
        members = []
        for nid in cluster_ids:
            node = self.graph.get_node(nid)
            if node:
                members.append(node)
        if len(members) < 2:
            return

        member_info = "\n".join(
            f"- {m.name}: {m.summary or '(no summary)'}" for m in members
        )
        prompt = (
            "You are a code analyst. These functions form a tightly-coupled functional cluster "
            "(they call each other frequently). Summarize what this cluster does as a WHOLE "
            "in ONE concise sentence (max 40 words). Focus on the cluster's collective purpose.\n\n"
            f"Functions in cluster:\n{member_info}\n\n"
            "Respond with ONLY the summary sentence."
        )
        try:
            messages = [{"role": "user", "content": prompt}]
            cluster_summary = self.llm.fast_chat(messages)
            if cluster_summary:
                # B1: 存 metadata 而不是拼入 summary，避免污染 embedding 空间
                for m in members:
                    m.metadata["cluster_summary"] = cluster_summary
                    m.metadata["cluster_members"] = [
                        mid for mid in cluster_ids if mid != m.id
                    ]
                logger.debug(f"簇摘要 ({len(members)} 个函数): {cluster_summary[:60]}...")
        except Exception as e:
            logger.warning(f"功能簇摘要失败: {e}")

    # ── A+D: 调用上下文收集 & 低质量摘要重试 ──

    def _gather_call_context(self, node: CodeNode, max_callers: int = 3, max_callees: int = 3) -> str:
        """收集函数的调用者和被调用者上下文（用于增强 LLM 摘要）"""
        lines = []

        # 调用者（谁调用了我？）
        callers = []
        for edge in self.graph.get_incoming(node.id, EdgeType.CALLS):
            caller = self.graph.get_node(edge.source)
            if caller and caller.node_type == NodeType.FUNCTION:
                callers.append(caller)
        if callers:
            callers = callers[:max_callers]
            lines.append("Called by: " + ", ".join(
                f"{c.name}({c.summary[:50] if c.summary else '?'})" for c in callers
            ))

        # 被调用者（我调用了谁？）
        callees = []
        for edge in self.graph.get_outgoing(node.id):
            if edge.edge_type == EdgeType.CALLS:
                callee = self.graph.get_node(edge.target)
                if callee and callee.node_type == NodeType.FUNCTION:
                    callees.append(callee)
        if callees:
            callees = callees[:max_callees]
            lines.append("Calls: " + ", ".join(
                f"{c.name}({c.summary[:50] if c.summary else '?'})" for c in callees
            ))

        return "\n".join(lines)

    def _gather_file_context(self, node: CodeNode, max_siblings: int = 5) -> str:
        """收集同文件兄弟函数的摘要作为上下文"""
        if not node.file_path:
            return ""
        siblings = [
            n for n in self.graph.nodes.values()
            if n.file_path == node.file_path
            and n.node_type == NodeType.FUNCTION
            and n.id != node.id
            and n.summary
        ][:max_siblings]
        return "\n".join(f"- {s.name}: {s.summary}" for s in siblings)

    def _retry_low_quality(self, nodes: list[CodeNode]) -> None:
        """D: 对低质量摘要节点使用增强上下文重试一次"""
        rescued = 0
        for node in nodes:
            code = self._read_node_source(node)
            if not code:
                continue

            # A: 收集调用链上下文（此时其他函数的首轮摘要已可用）
            call_ctx = self._gather_call_context(node)
            file_ctx = self._gather_file_context(node)

            prompt = Prompts.SUMMARIZE_FUNCTION_RETRY.format(
                name=node.name,
                language=node.language.value,
                code=code,
                call_context=call_ctx or "(no call relationships found)",
                file_context=file_ctx or "(no sibling functions)",
                previous_summary=node.summary or "(empty)",
            )
            # 入口点增强
            if node.metadata.get("is_entry_point"):
                prompt += (
                    "\n⚡ This is a SYSTEM ENTRY POINT. "
                    "Emphasize the external interface it exposes."
                )
            try:
                messages = [{"role": "user", "content": prompt}]
                raw = self.llm.fast_chat(messages)
                new_summary = self._parse_function_summary(raw, node)

                if new_summary:
                    new_quality = self._assess_summary_quality(new_summary, node)
                    if new_quality == "high":
                        node.summary = new_summary
                        node.metadata.pop("summary_quality", None)
                        rescued += 1
                        logger.debug(f"摘要提升 [{node.name}]: {new_summary[:60]}...")
                    # else: 仍然 low，保留原摘要
            except Exception as e:
                logger.debug(f"低质量摘要重试失败 [{node.name}]: {e}")

        logger.info(f"低质量摘要抢救: {rescued}/{len(nodes)} 个提升为 high")

    # ── C: 入口点检测 ──

    _ENTRY_NAME_PATTERN = re.compile(
        r"^(main|run|start|serve|app|execute|launch|cli"
        r"|handle_|on_|do_|cmd_|command_|route_|endpoint_"
        r"|api_|view_|action_|dispatch_|process_request)",
        re.IGNORECASE,
    )

    def _mark_entry_points(self, targets: list[CodeNode]) -> None:
        """
        标记入口点函数（零入度 + 命名模式 + 顶层位置）。
        入口点在 embedding 和摘要 prompt 中获增强处理。
        """
        func_nodes = [n for n in targets if n.node_type == NodeType.FUNCTION]
        if not func_nodes:
            return

        func_id_set = {n.id for n in func_nodes}

        # 统计 CALLS 入度（只统计函数→函数的调用）
        in_degree: dict[str, int] = {n.id: 0 for n in func_nodes}
        for n in func_nodes:
            for edge in self.graph.get_outgoing(n.id):
                if edge.edge_type == EdgeType.CALLS and edge.target in func_id_set:
                    in_degree[edge.target] = in_degree.get(edge.target, 0) + 1

        # 检测顶层函数（非类方法）
        top_level_ids: set[str] = set()
        for n in func_nodes:
            parent_is_class = False
            for edge in self.graph.get_incoming(n.id, EdgeType.CONTAINS):
                parent = self.graph.get_node(edge.source)
                if parent and parent.node_type in (NodeType.CLASS, NodeType.INTERFACE):
                    parent_is_class = True
                    break
            if not parent_is_class:
                top_level_ids.add(n.id)

        entry_count = 0
        for n in func_nodes:
            is_entry = False
            # 条件A: 零入度 + 顶层（非类方法）
            if in_degree.get(n.id, 0) == 0 and n.id in top_level_ids:
                is_entry = True
            # 条件B: 命名模式匹配（不论入度，入口函数名高信号）
            if self._ENTRY_NAME_PATTERN.match(n.name):
                is_entry = True

            if is_entry:
                n.metadata["is_entry_point"] = True
                entry_count += 1

        if entry_count:
            logger.info(f"标记 {entry_count} 个入口点函数")

    def _read_node_source(self, node: CodeNode) -> Optional[str]:
        """读取节点对应的源代码"""
        if not node.file_path or not node.byte_range:
            return None
        full_path = self.graph.repo_root / node.file_path
        try:
            source = full_path.read_bytes()
            return source[node.byte_range.start_byte:node.byte_range.end_byte].decode("utf-8", errors="replace")
        except OSError:
            return None

    def _collect_children_summaries(self, node: CodeNode) -> str:
        """收集子节点的摘要，用于聚合"""
        lines = []
        for child_id in node.children:
            child = self.graph.get_node(child_id)
            if child and child.summary:
                lines.append(f"- {child.name}: {child.summary}")
        if not lines:
            # fallback: 从包含边查找子节点
            for edge in self.graph.get_outgoing(node.id):
                if edge.edge_type.value == "contains":
                    child = self.graph.get_node(edge.target)
                    if child and child.summary:
                        lines.append(f"- {child.name}: {child.summary}")
        return "\n".join(lines) if lines else "(no summaries available)"
