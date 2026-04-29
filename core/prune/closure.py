"""
Phase2: CodePrune — 最小闭包求解 v2
语义定界 + 结构补全 + 缺口仲裁
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from config import CodePruneConfig
from core.graph.schema import CodeGraph, CodeNode, Edge, EdgeType, NodeType
from core.llm.provider import LLMProvider
from core.llm.prompts import Prompts
from core.prune.anchor import AnchorResult

logger = logging.getLogger(__name__)


# ───────────────────── 数据结构 ─────────────────────

@dataclass
class StructuralGap:
    """结构缺口：闭包内节点有硬依赖指向闭包外节点"""
    source: str           # 闭包内的节点（调用者）
    target: str           # 闭包外的节点（被调用者）
    edge: Edge            # 依赖边
    target_scope: str     # "peripheral" | "outside"
    req_id: str = ""      # 产生此缺口的需求 ID


@dataclass
class MergedGap:
    """合并后的结构缺口（同一目标去重）"""
    target: str                        # 目标节点 ID
    sources: list[tuple[str, Edge]]    # 所有调用来源 [(source_id, edge), ...]
    target_scope: str
    count: int                         # 被多少个闭包节点依赖
    req_ids: set[str] = field(default_factory=set)  # 关联的需求 ID


@dataclass
class ScopeClassification:
    """语义区域分类"""
    core: set[str] = field(default_factory=set)           # 语义上属于目标功能
    peripheral: set[str] = field(default_factory=set)     # 灰色地带
    outside: set[str] = field(default_factory=set)        # 语义上不属于
    dir_excluded: set[str] = field(default_factory=set)   # 因目录级排除而标 OUTSIDE 的节点
    dir_pattern_excluded: set[str] = field(default_factory=set)   # 因目录级 pattern (ends with /) 排除


@dataclass
class ClosureResult:
    """闭包求解结果"""
    required_nodes: set[str] = field(default_factory=set)     # 保留真实代码的节点
    stub_nodes: set[str] = field(default_factory=set)          # 需要生成桩代码的节点
    excluded_edges: list[tuple[str, str]] = field(default_factory=list)  # 被排除的调用关系 (source, target)

    # 审计 / 调试
    soft_included: set[str] = field(default_factory=set)       # 经裁决纳入的边界节点
    soft_excluded: set[str] = field(default_factory=set)       # 经裁决排除的边界节点
    structural_gaps: list[StructuralGap] = field(default_factory=list)
    relevance_map: dict[str, float | None] = field(default_factory=dict)
    node_requirements: dict[str, set[str]] = field(default_factory=dict)  # node_id → {req_ids}
    diagnostics: dict = field(default_factory=dict)


# ───────────────────── 求解器 ─────────────────────

class ClosureSolver:
    """
    最小可运行闭包求解器 v2

    三步框架:
      Step 1: 语义定界 — 全节点 embedding 相关度 → CORE / PERIPHERAL / OUTSIDE
      Step 2: 语义引导 BFS — CORE 自由扩展, PERIPHERAL 精细控制, OUTSIDE 产生缺口
      Step 3: 缺口仲裁 — 规则快筛 + LLM 三选一 (include / stub / exclude)
    """

    def __init__(self, config: CodePruneConfig, llm: LLMProvider, graph: CodeGraph):
        self.config = config
        self.llm = llm
        self.graph = graph
        self.policy = config.prune.closure_policy
        self._threshold_population_prefers_symbols = any(
            n.node_type == NodeType.FUNCTION for n in self.graph.nodes.values()
        )

    @staticmethod
    def _propagate_req_ids(
        result: ClosureResult, source_id: str, target_id: str,
    ) -> None:
        """将 source 的 req_ids 传播到 target（P1: requirement 追踪）"""
        source_reqs = result.node_requirements.get(source_id)
        if source_reqs:
            result.node_requirements.setdefault(target_id, set()).update(source_reqs)

    # ═══════════════════ 主入口 ═══════════════════

    def solve(
        self,
        anchors: list[AnchorResult],
        user_instruction: str,
        query_embedding: list[float] | None = None,
        closure_query_embedding: list[float] | None = None,
    ) -> ClosureResult:
        """
        求解最小闭包:
          1. 语义定界 — 确定 CORE/PERIPHERAL/OUTSIDE
          2. 语义引导 BFS — 生成初始闭包 + 结构缺口
          3. 缺口仲裁 — include / stub / exclude
          4. 后处理 — 包含链、`__init__.py`、粒度升级
        """
        result = ClosureResult()
        result.diagnostics["warnings"] = []
        max_depth = self.config.prune.max_closure_depth

        # 获取 InstructionAnalysis（如有）用于边界约束和 prompt 增强
        analysis = self.config.instruction_analysis
        excluded_dirs = analysis.out_of_scope if analysis else []
        features_text = self._build_features_text(user_instruction, analysis)

        # ── Step 1: 语义定界 ──
        strategy = self.config.prune.scope_strategy
        if strategy == "llm_hierarchical":
            scope, scope_diag = self._hierarchical_scope_assessment(
                features_text, excluded_dirs, anchors,
            )
            # 生成兼容的 relevance_map（数值化的分类标签）
            relevance_map: dict[str, float | None] = {}
            for nid in self.graph.nodes:
                node = self.graph.get_node(nid)
                if node and node.node_type in (NodeType.DIRECTORY, NodeType.REPOSITORY):
                    continue
                if nid in scope.core:
                    relevance_map[nid] = 1.0
                elif nid in scope.peripheral:
                    relevance_map[nid] = 0.5
                elif nid in scope.outside or nid in scope.dir_excluded:
                    relevance_map[nid] = 0.0
                else:
                    relevance_map[nid] = 0.5  # 未评估 → 保守 PERIPHERAL
            result.relevance_map = relevance_map
            result.diagnostics["thresholds"] = {
                "strategy": "llm_hierarchical",
                **scope_diag,
            }
        else:
            # 旧模式: embedding threshold
            # A2: 优先使用 closure_query_embedding（基于 sub_features，更接近代码摘要语域）
            scope_embedding = closure_query_embedding or query_embedding
            if scope_embedding is None:
                scope_embedding = self.llm.embed([user_instruction])[0]

            relevance_map = self._build_relevance_map(scope_embedding)
            self._fill_missing_relevance(relevance_map)
            core_thresh, periph_thresh, threshold_diag = self._compute_thresholds(
                relevance_map, anchors,
            )
            scope = self._classify_scope(relevance_map, core_thresh, periph_thresh)
            core_thresh, periph_thresh, scope, tighten_diag = self._tighten_scope_if_needed(
                relevance_map, scope, core_thresh, periph_thresh,
            )

            # A1: 目录级快速排除 — 极低 relevance 的目录整体标 OUTSIDE
            self._dir_level_exclusion(relevance_map, scope, periph_thresh)

            result.relevance_map = relevance_map
            result.diagnostics["thresholds"] = {
                "strategy": "embedding_threshold",
                **threshold_diag,
                **tighten_diag,
                "core_threshold": round(core_thresh, 4),
                "peripheral_threshold": round(periph_thresh, 4),
            }
            if tighten_diag.get("tighten_iterations", 0) > 0:
                result.diagnostics["warnings"].append("semantic_thresholds_tightened")
        total_relevance_nodes = max(1, len(relevance_map))
        semantic_scope_ratio = (len(scope.core) + len(scope.peripheral)) / total_relevance_nodes
        result.diagnostics["scope"] = {
            "core": len(scope.core),
            "peripheral": len(scope.peripheral),
            "outside": len(scope.outside),
            "semantic_scope_ratio": round(semantic_scope_ratio, 4),
            "dir_excluded": len(scope.dir_excluded),
        }
        if semantic_scope_ratio > self.policy.max_semantic_scope_ratio:
            result.diagnostics["warnings"].append("semantic_scope_ratio_high")
        if strategy == "llm_hierarchical":
            logger.info(
                f"语义定界 (LLM): CORE={len(scope.core)}, "
                f"PERIPHERAL={len(scope.peripheral)}, "
                f"OUTSIDE={len(scope.outside)}"
                + (f", 目录级排除 {len(scope.dir_excluded)} 节点" if scope.dir_excluded else "")
            )
        else:
            logger.info(
                f"语义定界: CORE={len(scope.core)}, "
                f"PERIPHERAL={len(scope.peripheral)}, "
                f"OUTSIDE={len(scope.outside)} "
                f"(阈值 core={core_thresh:.3f}, periph={periph_thresh:.3f})"
                + (f", 目录级排除 {len(scope.dir_excluded)} 节点" if scope.dir_excluded else "")
            )

        # ── Step 2: 语义引导 BFS ──
        structural_gaps, core_auto_included = self._semantic_bfs(
            anchors, scope, result, max_depth, excluded_dirs,
        )
        result.structural_gaps = structural_gaps
        result.diagnostics["bfs"] = {
            "anchor_count": len(anchors),
            "required_after_bfs": len(result.required_nodes),
            "structural_gap_count": len(structural_gaps),
            "core_auto_included": len(core_auto_included),
        }

        logger.info(
            f"语义 BFS: {len(result.required_nodes)} 个节点, "
            f"{len(structural_gaps)} 个结构缺口, "
            f"{len(core_auto_included)} 个 CORE 自动包含"
        )

        # ── Step 2.5: CORE 包含校验 ──
        anchor_ids = {a.node_id for a in anchors}
        verifiable_core = core_auto_included - anchor_ids
        if verifiable_core:
            rejected = self._verify_core_inclusions(
                verifiable_core, features_text, result,
            )
            if rejected:
                result.required_nodes -= rejected
                logger.info(f"CORE 校验: 移除 {len(rejected)} 个误判节点")

        # ── Step 3: 缺口仲裁 ──
        if structural_gaps:
            self._arbitrate_gaps(
                structural_gaps, result, scope, features_text, max_depth,
            )

        # ── Step 3.5: Per-Requirement 完整性校验 ──
        if analysis and analysis.sub_features:
            self._verify_requirement_completeness(analysis, result, scope)

        # ── Step 4: 后处理 ──
        self._ensure_containment_chain(result)
        self._auto_include_init_py_recursive(result)
        self._auto_include_barrel_files(result, scope)
        self._upgrade_full_classes(result)
        self._expand_class_children_if_full(result)
        self._final_size_check(result)
        result.diagnostics["final"] = {
            "required_nodes": len(result.required_nodes),
            "stub_nodes": len(result.stub_nodes),
            "excluded_edges": len(result.excluded_edges),
            "structural_gaps": len(result.structural_gaps),
        }

        logger.info(
            f"闭包求解完成: {len(result.required_nodes)} 个节点 "
            f"(stub {len(result.stub_nodes)}, 排除边 {len(result.excluded_edges)})"
        )
        return result

    # ═══════════════════ Step 1: 语义定界 ═══════════════════

    @staticmethod
    def _build_features_text(user_instruction: str, analysis) -> str:
        """构建用于 prompt 的功能描述文本"""
        if analysis and analysis.sub_features:
            return "\n".join(f"- {sf.description}" for sf in analysis.sub_features)
        return user_instruction

    @staticmethod
    def _in_excluded_scope(node: CodeNode, excluded_items: list[str]) -> bool:
        """节点是否在排除范围内（支持目录前缀 + 相对路径后缀 + 文件名匹配）"""
        if not node.file_path or not excluded_items:
            return False
        path_str = str(node.file_path).replace("\\", "/")
        for item in excluded_items:
            item_n = item.replace("\\", "/").rstrip("/")
            # 完整路径精确匹配
            if path_str == item_n:
                return True
            # 目录前缀匹配 (e.g. "api/" matches "src/api/client.ts")
            if item.endswith("/") and (
                path_str.startswith(item_n + "/") or path_str == item_n
            ):
                return True
            # 相对路径后缀匹配 (e.g. "api/client.ts" matches "src/api/client.ts")
            if "/" in item_n and path_str.endswith("/" + item_n):
                return True
            # 纯文件名匹配 — 匹配任意目录下同名文件
            # 例: "remote.py" 匹配 "orchestrator/backends/remote.py"
            if "/" not in item_n and (
                path_str == item_n or path_str.endswith("/" + item_n)
            ):
                return True
        return False

    @staticmethod
    def _in_excluded_dir(node: CodeNode, excluded_items: list[str]) -> bool:
        """兼容旧调用名，行为等同于 `_in_excluded_scope`。"""
        return ClosureSolver._in_excluded_scope(node, excluded_items)

    def _build_relevance_map(self, query_embedding: list[float]) -> dict[str, float | None]:
        """计算每个节点与用户指令的语义相关度"""
        relevance: dict[str, float | None] = {}
        for nid, node in self.graph.nodes.items():
            if node.node_type in (NodeType.DIRECTORY, NodeType.REPOSITORY):
                continue
            if node.embedding is not None:
                relevance[nid] = self._cosine_sim(node.embedding, query_embedding)
            else:
                relevance[nid] = None  # 待推断
        return relevance

    def _fill_missing_relevance(self, relevance: dict[str, float | None]) -> None:
        """
        对 embedding 缺失的节点做推断:
        1. 从子节点上推 (取 MAX)
        2. 从父节点下推 (衰减 0.8)
        3. 无法推断 → 保守标记为 PERIPHERAL
        """
        for nid in list(relevance):
            if relevance.get(nid) is not None:
                continue
            node = self.graph.get_node(nid)
            if not node:
                relevance[nid] = 0.0
                continue

            # 从子节点推断
            child_scores = [
                relevance[c] for c in node.children
                if relevance.get(c) is not None
            ]
            if child_scores:
                relevance[nid] = max(s for s in child_scores if s is not None)
                continue

            # 从父节点推断
            incoming = self.graph.get_incoming(nid, EdgeType.CONTAINS)
            if incoming:
                parent_score = relevance.get(incoming[0].source)
                if parent_score is not None:
                    relevance[nid] = parent_score * 0.8
                    continue

            # 无法推断 → 放入 PERIPHERAL
            relevance[nid] = self.policy.peripheral_floor

    def _compute_thresholds(
        self, relevance_map: dict[str, float | None], anchors: list[AnchorResult],
    ) -> tuple[float, float, dict]:
        """
        结合锚点分布和全图语义分布推导阈值:
        - anchor_ref: 锚点 relevance 分位数（默认 P25）
        - corpus_floor: 全图高分分位数，避免阈值过松
        - margin: 从 anchor_ref 回退得到 CORE / PERIPHERAL
        """
        anchor_scores = [
            relevance_map[a.node_id]
            for a in anchors
            if relevance_map.get(a.node_id) is not None
        ]
        anchor_scores = [s for s in anchor_scores if s is not None]
        corpus_scores = [
            score for nid, score in relevance_map.items()
            if score is not None and self._is_threshold_population_node(nid)
        ]
        if not corpus_scores:
            corpus_scores = [score for score in relevance_map.values() if score is not None]

        anchor_ref = self._quantile(anchor_scores, self.policy.anchor_percentile)
        if anchor_ref is None:
            anchor_ref = self.policy.core_floor

        core_corpus_floor = self._quantile(
            corpus_scores, self.policy.core_corpus_percentile,
        )
        periph_corpus_floor = self._quantile(
            corpus_scores, self.policy.peripheral_corpus_percentile,
        )
        core = max(
            self.policy.core_floor,
            anchor_ref - self.policy.core_margin,
            core_corpus_floor or 0.0,
        )
        periph = max(
            self.policy.peripheral_floor,
            anchor_ref - self.policy.peripheral_margin,
            periph_corpus_floor or 0.0,
        )
        periph = min(periph, max(self.policy.peripheral_floor, core - 0.01))
        diagnostics = {
            "anchor_score_count": len(anchor_scores),
            "anchor_reference": round(anchor_ref, 4),
            "corpus_score_count": len(corpus_scores),
            "population_mode": (
                "symbol"
                if self._threshold_population_prefers_symbols
                else "file"
            ),
            "core_corpus_floor": round(core_corpus_floor or 0.0, 4),
            "peripheral_corpus_floor": round(periph_corpus_floor or 0.0, 4),
        }
        return core, periph, diagnostics

    def _tighten_scope_if_needed(
        self,
        relevance_map: dict[str, float | None],
        scope: ScopeClassification,
        core_thresh: float,
        periph_thresh: float,
    ) -> tuple[float, float, ScopeClassification, dict]:
        """如果语义范围过宽，在 BFS 前收紧阈值。"""
        total = max(1, len(relevance_map))
        semantic_ratio = (len(scope.core) + len(scope.peripheral)) / total
        diagnostics = {
            "semantic_scope_ratio_initial": round(semantic_ratio, 4),
            "tighten_iterations": 0,
        }
        if semantic_ratio <= self.policy.max_semantic_scope_ratio:
            diagnostics["semantic_scope_ratio_final"] = round(semantic_ratio, 4)
            return core_thresh, periph_thresh, scope, diagnostics

        tightened_scope = scope
        tightened_core = core_thresh
        tightened_periph = periph_thresh
        step = self.policy.threshold_tightening_step
        for iteration in range(1, 4):
            tightened_core = min(0.99, tightened_core + step)
            tightened_periph = min(
                max(self.policy.peripheral_floor, tightened_core - 0.01),
                tightened_periph + step,
            )
            tightened_scope = self._classify_scope(
                relevance_map, tightened_core, tightened_periph,
            )
            semantic_ratio = (
                len(tightened_scope.core) + len(tightened_scope.peripheral)
            ) / total
            diagnostics["tighten_iterations"] = iteration
            if semantic_ratio <= self.policy.max_semantic_scope_ratio:
                break

        diagnostics["semantic_scope_ratio_final"] = round(semantic_ratio, 4)
        diagnostics["tightened_core_threshold"] = round(tightened_core, 4)
        diagnostics["tightened_peripheral_threshold"] = round(tightened_periph, 4)
        return tightened_core, tightened_periph, tightened_scope, diagnostics

    def _is_threshold_population_node(self, node_id: str) -> bool:
        """选取用于阈值分布估计的节点类型。"""
        node = self.graph.get_node(node_id)
        if not node:
            return False
        preferred_types = {NodeType.FUNCTION, NodeType.CLASS, NodeType.INTERFACE, NodeType.ENUM}
        if self._threshold_population_prefers_symbols:
            return node.node_type in preferred_types
        return node.node_type == NodeType.FILE

    @staticmethod
    def _quantile(values: list[float], q: float) -> float | None:
        """线性插值分位数。"""
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        pos = (len(ordered) - 1) * q
        lower = int(pos)
        upper = min(lower + 1, len(ordered) - 1)
        weight = pos - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    def _classify_scope(
        self, relevance_map: dict[str, float | None],
        core_thresh: float, periph_thresh: float,
    ) -> ScopeClassification:
        """将节点划分为 CORE / PERIPHERAL / OUTSIDE"""
        scope = ScopeClassification()
        for nid, score in relevance_map.items():
            if score is None:
                scope.peripheral.add(nid)
            elif score >= core_thresh:
                scope.core.add(nid)
            elif score >= periph_thresh:
                scope.peripheral.add(nid)
            else:
                scope.outside.add(nid)
        return scope

    # ═══════════════════ Step 1-alt: 分层语义评估 ═══════════════════

    def _hierarchical_scope_assessment(
        self,
        features_text: str,
        excluded_dirs: list[str],
        anchors: list[AnchorResult],
    ) -> tuple[ScopeClassification, dict]:
        """
        分层语义评估（替代 embedding threshold）:
          Level-1: 目录级 LLM 判定 → INCLUDE/EXCLUDE
          Level-2: 文件级 LLM 判定 → CORE/PERIPHERAL/OUTSIDE
          Level-3: 符号级继承文件分类
        """
        scope = ScopeClassification()
        diagnostics: dict = {}

        # 收集目录和文件节点
        dir_nodes = [
            n for n in self.graph.nodes.values()
            if n.node_type == NodeType.DIRECTORY
        ]
        file_nodes = [
            n for n in self.graph.nodes.values()
            if n.node_type == NodeType.FILE
        ]

        # 锚点所在的文件 ID（保护不被排除）
        anchor_file_ids: set[str] = set()
        for a in anchors:
            if a.node.file_path:
                anchor_file_ids.add(f"file:{a.node.file_path}")
            anchor_file_ids.add(a.node_id)

        # ── Level-1: 目录级评估 ──
        excluded_dir_ids: set[str] = set()
        if len(dir_nodes) > 3:
            excluded_dir_ids = self._classify_directories_batch(
                dir_nodes, features_text, excluded_dirs, anchor_file_ids,
            )
            diagnostics["dir_level"] = {
                "total_dirs": len(dir_nodes),
                "excluded_dirs": len(excluded_dir_ids),
            }
            logger.info(
                f"Level-1 目录评估: {len(excluded_dir_ids)}/{len(dir_nodes)} 个目录排除"
            )
        else:
            diagnostics["dir_level"] = {"skipped": True, "reason": "too_few_dirs"}

        # ── Level-2: 文件级评估 ──
        # 过滤掉 excluded 目录中的文件
        candidate_files = []
        for f in file_nodes:
            file_dir = self._get_parent_dir_id(f)
            if file_dir and file_dir in excluded_dir_ids:
                # 这个文件在被排除的目录中
                scope.outside.add(f.id)
                scope.dir_excluded.add(f.id)
            elif self._in_excluded_scope(f, excluded_dirs):
                scope.outside.add(f.id)
                scope.dir_excluded.add(f.id)
            else:
                candidate_files.append(f)

        file_verdicts = self._classify_files_batch(
            candidate_files, features_text, anchor_file_ids,
        )
        diagnostics["file_level"] = {
            "total_files": len(file_nodes),
            "candidate_files": len(candidate_files),
            "core_files": sum(1 for v in file_verdicts.values() if v == "CORE"),
            "peripheral_files": sum(1 for v in file_verdicts.values() if v == "PERIPHERAL"),
            "outside_files": sum(1 for v in file_verdicts.values() if v == "OUTSIDE"),
        }
        logger.info(
            f"Level-2 文件评估: "
            f"CORE={diagnostics['file_level']['core_files']}, "
            f"PERIPHERAL={diagnostics['file_level']['peripheral_files']}, "
            f"OUTSIDE={diagnostics['file_level']['outside_files']}"
        )

        # ── Level-3: 符号级继承文件分类 ──
        for f in candidate_files:
            verdict = file_verdicts.get(f.id, "PERIPHERAL")
            target_set = getattr(scope, verdict.lower())
            target_set.add(f.id)

        # 将符号节点归入所属文件的分类
        for nid, node in self.graph.nodes.items():
            if node.node_type in (NodeType.DIRECTORY, NodeType.REPOSITORY, NodeType.FILE):
                continue
            parent_file_id = self._get_file_node_id(node)
            if parent_file_id:
                if parent_file_id in scope.core:
                    scope.core.add(nid)
                elif parent_file_id in scope.peripheral:
                    scope.peripheral.add(nid)
                elif parent_file_id in scope.outside or parent_file_id in scope.dir_excluded:
                    scope.outside.add(nid)
                else:
                    scope.peripheral.add(nid)  # 未评估 → 保守
            else:
                scope.peripheral.add(nid)  # 无法找到文件 → 保守

        # 将排除目录中的所有后代也标 OUTSIDE
        for dir_id in excluded_dir_ids:
            descendants: set[str] = set()
            self._collect_descendants(dir_id, descendants)
            for desc_id in descendants:
                if desc_id not in scope.core and desc_id not in scope.peripheral:
                    scope.outside.add(desc_id)
                    scope.dir_excluded.add(desc_id)

        return scope, diagnostics

    def _classify_directories_batch(
        self,
        dir_nodes: list[CodeNode],
        features_text: str,
        excluded_dirs: list[str],
        anchor_file_ids: set[str],
    ) -> set[str]:
        """Level-1: 一次 LLM 调用，批量判定目录 INCLUDE/EXCLUDE"""
        # 构建目录条目
        dir_entries_lines = []
        dir_id_map: dict[str, str] = {}  # key_label → dir_id

        for d in dir_nodes:
            dir_path = d.qualified_name or d.name
            summary = d.summary or "(no summary)"
            tags = d.metadata.get("functional_tags", [])
            file_count = sum(
                1 for cid in d.children
                if (cn := self.graph.get_node(cid)) and cn.node_type == NodeType.FILE
            )
            tags_str = ", ".join(tags[:5]) if tags else "none"
            key = dir_path.replace("/", "_").replace("\\", "_").strip("_") or d.name
            dir_id_map[key] = d.id
            dir_entries_lines.append(
                f"- {key}: {summary} ({file_count} files) [tags: {tags_str}]"
            )

        if not dir_entries_lines:
            return set()

        out_of_scope_text = ", ".join(excluded_dirs) if excluded_dirs else "(none)"
        dir_keys = ", ".join(f'"{k}": "INCLUDE|EXCLUDE"' for k in dir_id_map)

        prompt = Prompts.CLASSIFY_DIRECTORIES.format(
            features_text=features_text,
            out_of_scope_text=out_of_scope_text,
            dir_entries="\n".join(dir_entries_lines),
            dir_keys=dir_keys,
        )

        try:
            result = self.llm.fast_chat_json([{"role": "user", "content": prompt}])
            verdicts = result.get("directories", result)

            excluded: set[str] = set()
            for key, verdict in verdicts.items():
                dir_id = dir_id_map.get(key)
                if dir_id and str(verdict).upper() == "EXCLUDE":
                    # 保护包含锚点的目录
                    dir_node = self.graph.get_node(dir_id)
                    has_anchor = False
                    if dir_node and dir_node.qualified_name:
                        has_anchor = any(
                            aid.startswith(f"file:{dir_node.qualified_name}")
                            for aid in anchor_file_ids
                        )
                    if not has_anchor:
                        excluded.add(dir_id)
            return excluded
        except Exception as e:
            logger.warning(f"目录级 LLM 评估失败，使用关键词 fallback: {e}")
            return self._keyword_fallback_dir_exclusion(
                dir_nodes, features_text, anchor_file_ids,
            )

    @staticmethod
    def _extract_exclusion_keywords(text: str) -> list[str]:
        """从用户指令中提取排除意图关键词。

        识别 "去掉 X"、"不需要 X"、"删除 X"、"排除 X" 等模式,
        提取 X 部分并分割为独立关键词。
        """
        exclusion_phrases: list[str] = []
        # 匹配 "去掉/不需要/删除/排除 ... 。/\n/end" 之间的内容
        patterns = [
            r'去掉\s*(.+?)(?:[。\n;；]|$)',
            r'不需要\s*(.+?)(?:[。\n;；]|$)',
            r'删除\s*(.+?)(?:[。\n;；]|$)',
            r'排除\s*(.+?)(?:[。\n;；]|$)',
            r'不保留\s*(.+?)(?:[。\n;；]|$)',
        ]
        for pat in patterns:
            for m in re.finditer(pat, text, re.IGNORECASE):
                exclusion_phrases.append(m.group(1).strip())

        # 将逗号/顿号/和/与 分割得到独立关键词
        keywords: list[str] = []
        for phrase in exclusion_phrases:
            parts = re.split(r'[,，、和与及]', phrase)
            for p in parts:
                p = p.strip().strip('。.；;')
                if p and len(p) >= 1:
                    keywords.append(p.lower())
        return keywords

    # 排除关键词 → 常用目录名的映射
    _KEYWORD_DIR_ALIASES: dict[str, list[str]] = {
        'orm': ['orm', 'models', 'database', 'db'],
        '任务': ['tasks', 'task', 'jobs', 'worker', 'workers'],
        '任务系统': ['tasks', 'task', 'jobs', 'worker', 'workers'],
        '缓存': ['cache', 'caching', 'redis'],
        '限流': ['rate_limit', 'ratelimit', 'throttle'],
        '插件': ['plugins', 'plugin'],
        '调度': ['scheduler', 'scheduling', 'cron'],
        '监控': ['monitor', 'monitoring', 'metrics'],
        '日志': ['logging', 'logs', 'log'],
        '测试': ['tests', 'test', '__tests__'],
        '数据库': ['db', 'database', 'migrations'],
    }

    def _keyword_fallback_dir_exclusion(
        self,
        dir_nodes: list[CodeNode],
        features_text: str,
        anchor_file_ids: set[str],
    ) -> set[str]:
        """关键词 fallback: 从指令中提取排除意图,匹配目录名"""
        keywords = self._extract_exclusion_keywords(features_text)
        if not keywords:
            logger.info("关键词 fallback: 未找到排除关键词")
            return set()

        logger.info(f"关键词 fallback: 排除关键词 = {keywords}")
        excluded: set[str] = set()

        for d in dir_nodes:
            dir_path = (d.qualified_name or d.name).replace("\\", "/").lower()
            dir_basename = dir_path.rstrip("/").rsplit("/", 1)[-1]

            # 检查锚点保护
            has_anchor = any(
                aid.startswith(f"file:{d.qualified_name}")
                for aid in anchor_file_ids
            ) if d.qualified_name else False
            if has_anchor:
                continue

            matched = False
            for kw in keywords:
                # 直接匹配: 关键词包含在目录名中
                if kw in dir_basename or dir_basename in kw:
                    matched = True
                    break
                # 别名匹配
                aliases = self._KEYWORD_DIR_ALIASES.get(kw, [])
                if dir_basename in aliases:
                    matched = True
                    break
                # 英文关键词直接匹配目录名
                if kw.isascii() and (kw == dir_basename or kw in dir_basename):
                    matched = True
                    break

            if matched:
                excluded.add(d.id)
                logger.info(f"关键词 fallback: 排除目录 {dir_path}")

        return excluded

    def _keyword_fallback_file_classification(
        self,
        file_nodes: list[CodeNode],
        features_text: str,
        anchor_file_ids: set[str],
    ) -> dict[str, str]:
        """关键词 fallback: 从指令提取意图,分类文件"""
        keywords = self._extract_exclusion_keywords(features_text)
        verdicts: dict[str, str] = {}

        for f in file_nodes:
            fid = f.id
            if fid in anchor_file_ids:
                verdicts[fid] = "CORE"
                continue

            path = str(f.file_path or f.name).replace("\\", "/").lower()
            path_parts = path.split("/")

            matched_exclude = False
            for kw in keywords:
                # 路径中包含排除关键词
                if kw.isascii() and any(kw in part for part in path_parts):
                    matched_exclude = True
                    break
                # 别名匹配
                aliases = self._KEYWORD_DIR_ALIASES.get(kw, [])
                if any(part in aliases for part in path_parts):
                    matched_exclude = True
                    break

            verdicts[fid] = "OUTSIDE" if matched_exclude else "PERIPHERAL"

        return verdicts

    def _classify_files_batch(
        self,
        file_nodes: list[CodeNode],
        features_text: str,
        anchor_file_ids: set[str],
    ) -> dict[str, str]:
        """Level-2: 批量判定文件 CORE/PERIPHERAL/OUTSIDE"""
        if not file_nodes:
            return {}

        verdicts: dict[str, str] = {}
        batch_size = 25
        all_batches_failed = True

        for i in range(0, len(file_nodes), batch_size):
            batch = file_nodes[i:i + batch_size]
            file_entries_lines = []
            idx_to_id: dict[str, str] = {}

            for j, f in enumerate(batch, 1):
                key = str(j)
                idx_to_id[key] = f.id

                summary = f.summary or "(no summary)"
                tags = f.metadata.get("functional_tags", [])
                tags_str = ", ".join(tags[:5]) if tags else ""

                # 收集 imports 信息帮助 LLM 判断结构依赖
                import_targets = []
                for edge in self.graph.get_outgoing(f.id):
                    if edge.edge_type == EdgeType.IMPORTS:
                        target = self.graph.get_node(edge.target)
                        if target:
                            import_targets.append(target.name)
                imports_str = ", ".join(import_targets[:5]) if import_targets else ""

                line = f"{j}. {f.file_path or f.name} — {summary}"
                if tags_str:
                    line += f" [tags: {tags_str}]"
                if imports_str:
                    line += f" [imports: {imports_str}]"
                file_entries_lines.append(line)

            file_keys = ", ".join(f'"{k}": "CORE|PERIPHERAL|OUTSIDE"' for k in idx_to_id)

            prompt = Prompts.CLASSIFY_FILES.format(
                features_text=features_text,
                file_entries="\n".join(file_entries_lines),
                file_keys=file_keys,
            )

            try:
                result = self.llm.fast_chat_json([{"role": "user", "content": prompt}])
                for key, verdict in result.items():
                    fid = idx_to_id.get(str(key))
                    if fid:
                        v = str(verdict).upper()
                        if v in ("CORE", "PERIPHERAL", "OUTSIDE"):
                            verdicts[fid] = v
                        else:
                            verdicts[fid] = "PERIPHERAL"
                all_batches_failed = False
            except Exception as e:
                logger.warning(f"文件级 LLM 评估 batch {i // batch_size} 失败: {e}")
                for f in batch:
                    verdicts[f.id] = "PERIPHERAL"

        # 如果全部 batch 失败，使用关键词 fallback
        if all_batches_failed and file_nodes:
            logger.info("所有文件级 LLM 评估均失败，使用关键词 fallback")
            verdicts = self._keyword_fallback_file_classification(
                file_nodes, features_text, anchor_file_ids,
            )

        # 锚点所在文件强制标为 CORE
        for fid in anchor_file_ids:
            if fid in verdicts:
                verdicts[fid] = "CORE"

        return verdicts

    def _get_parent_dir_id(self, node: CodeNode) -> str | None:
        """获取文件节点所属目录的 ID"""
        incoming = self.graph.get_incoming(node.id, EdgeType.CONTAINS)
        for edge in incoming:
            parent = self.graph.get_node(edge.source)
            if parent and parent.node_type == NodeType.DIRECTORY:
                return parent.id
        return None

    def _get_file_node_id(self, node: CodeNode) -> str | None:
        """获取符号节点所属文件的 ID"""
        if node.file_path:
            fid = f"file:{node.file_path}"
            if self.graph.get_node(fid):
                return fid
        # 回退: 从 CONTAINS 边查找
        incoming = self.graph.get_incoming(node.id, EdgeType.CONTAINS)
        for edge in incoming:
            parent = self.graph.get_node(edge.source)
            if parent:
                if parent.node_type == NodeType.FILE:
                    return parent.id
                elif parent.node_type in (NodeType.CLASS, NodeType.INTERFACE):
                    return self._get_file_node_id(parent)
        return None

    def _dir_level_exclusion(
        self, relevance_map: dict[str, float | None],
        scope: ScopeClassification, periph_thresh: float,
    ) -> None:
        """
        A1: 目录级快速排除。
        极低 relevance 的目录（< periph_thresh × 0.5）→ 全部后代强制标 OUTSIDE。
        只排除不级联 CORE — 误排除通过缺口仲裁兜底。
        """
        dir_exclude_thresh = periph_thresh * 0.5
        excluded_dirs: set[str] = set()

        for nid, node in self.graph.nodes.items():
            if node.node_type != NodeType.DIRECTORY:
                continue
            dir_rel = relevance_map.get(nid)
            if dir_rel is not None and dir_rel < dir_exclude_thresh:
                excluded_dirs.add(nid)

        if not excluded_dirs:
            return

        # 收集被排除目录的全部后代节点
        descendants: set[str] = set()
        for dir_id in excluded_dirs:
            self._collect_descendants(dir_id, descendants)

        # 将后代从 core/peripheral 移到 outside
        moved = 0
        for nid in descendants:
            if nid in scope.core:
                scope.core.discard(nid)
                scope.outside.add(nid)
                scope.dir_excluded.add(nid)
                moved += 1
            elif nid in scope.peripheral:
                scope.peripheral.discard(nid)
                scope.outside.add(nid)
                scope.dir_excluded.add(nid)
                moved += 1

        if moved:
            logger.info(
                f"目录级排除: {len(excluded_dirs)} 个目录, "
                f"{moved} 个节点降级为 OUTSIDE (阈值 {dir_exclude_thresh:.3f})"
            )

    def _collect_descendants(self, node_id: str, result: set[str]) -> None:
        """递归收集节点的全部后代"""
        for edge in self.graph.get_outgoing(node_id):
            if edge.edge_type == EdgeType.CONTAINS:
                if edge.target not in result:
                    result.add(edge.target)
                    self._collect_descendants(edge.target, result)

    # ═══════════════════ Step 2: 语义引导 BFS ═══════════════════

    def _semantic_bfs(
        self,
        anchors: list[AnchorResult],
        scope: ScopeClassification,
        result: ClosureResult,
        max_depth: int,
        excluded_dirs: list[str],
    ) -> tuple[list[StructuralGap], set[str]]:
        """
        从锚点出发 BFS，按目标节点所在语义区域决定处理策略:
        - CORE: 无条件加入（后续由 _verify_core_inclusions 复核）
        - PERIPHERAL: 精细控制（按边类型+独占性）
        - OUTSIDE: 记录为结构缺口

        Returns:
            (structural_gaps, core_auto_included)
        """
        structural_gaps: list[StructuralGap] = []
        core_auto_included: set[str] = set()  # P1: 追踪 CORE 自动包含的非锚点节点
        anchor_ids = {a.node_id for a in anchors}
        result.required_nodes.update(anchor_ids)

        # P1: 初始化锚点的 requirement 标注
        for a in anchors:
            if a.req_ids:
                result.node_requirements.setdefault(a.node_id, set()).update(a.req_ids)

        # 预设排除：将匹配 out_of_scope 的节点强制标记为 OUTSIDE
        if excluded_dirs:
            # F25: 区分目录级 pattern (ends with '/') 和文件级 pattern
            dir_patterns = [item for item in excluded_dirs if item.rstrip('\\').endswith('/')]
            pre_excluded = 0
            for nid, node in self.graph.nodes.items():
                if self._in_excluded_scope(node, excluded_dirs):
                    if nid not in anchor_ids:
                        scope.outside.add(nid)
                        scope.dir_excluded.add(nid)
                        # 标记目录级排除的节点（硬阻断，不允许 BFS 穿透）
                        if dir_patterns and self._in_excluded_scope(node, dir_patterns):
                            scope.dir_pattern_excluded.add(nid)
                        pre_excluded += 1
            if pre_excluded:
                logger.info(f"BFS 预设排除: {pre_excluded} 个节点标记为 OUTSIDE")

        queue: deque[tuple[str, int]] = deque((nid, 0) for nid in anchor_ids)
        check_interval = self.policy.size_check_interval
        _file_edges_visited: set[str] = set()  # 已遍历过文件级边的 file node
        auto_tighten_events = result.diagnostics.setdefault("auto_tighten_events", [])

        while queue:
            node_id, depth = queue.popleft()
            if depth >= max_depth:
                logger.warning(f"闭包深度达到上限 {max_depth}: {node_id}")
                continue

            # 收集当前节点自身的出边
            edges_to_walk = list(self.graph.get_outgoing(node_id))

            # F23b: FILE 节点直接在 BFS 队列中时，过滤结构性边：
            # - CONTAINS 边是文件→子符号的结构关系
            # - 同文件 CALLS 边是文件级伪边（真正调用由 function→function CALLS 处理）
            if node_id.startswith("file:"):
                _file_edges_visited.add(node_id)  # 标记已遍历，避免代理路径重复
                file_node_self = self.graph.get_node(node_id)
                file_path_self = file_node_self.file_path if file_node_self else None
                edges_to_walk = [
                    e for e in edges_to_walk
                    if e.edge_type != EdgeType.CONTAINS
                    and not (
                        e.edge_type == EdgeType.CALLS
                        and (tgt := self.graph.get_node(e.target)) is not None
                        and tgt.file_path == file_path_self
                    )
                ]

            # BFS 起点为函数/类时，需额外遍历所属文件的 IMPORTS/CALLS 边
            # 注意: 排除 CONTAINS 边 — 它们是父子结构关系，不是功能依赖
            cur_node = self.graph.get_node(node_id)
            if cur_node and cur_node.file_path:
                file_nid = f"file:{cur_node.file_path}"
                if file_nid != node_id and file_nid not in _file_edges_visited:
                    _file_edges_visited.add(file_nid)
                    for fe in self.graph.get_outgoing(file_nid):
                        if fe.edge_type == EdgeType.CONTAINS:
                            continue
                        # F18c: 排除文件到自身子函数的 CALLS 边
                        # file→child CALLS 是结构关系，真正的函数间调用
                        # 会由 function→function CALLS 边处理
                        if fe.edge_type == EdgeType.CALLS:
                            tgt = self.graph.get_node(fe.target)
                            if tgt and tgt.file_path == cur_node.file_path:
                                continue
                        edges_to_walk.append(fe)

            for edge in edges_to_walk:
                target = edge.target
                if target in result.required_nodes:
                    # F18b: FILE 节点已在 required 中，但后续 IMPORTS 边
                    # 可能携带不同的 imported_symbols，需继续收集
                    if (target.startswith("file:") and
                            edge.edge_type == EdgeType.IMPORTS):
                        self._import_symbol_level(
                            target, result, queue, depth, edge, scope,
                            strict=True,
                        )
                    continue
                target_node = self.graph.get_node(target)
                if not target_node:
                    # P0-fix: 未展开节点不再静默跳过，硬依赖记录为结构缺口交仲裁
                    if edge.is_hard:
                        structural_gaps.append(StructuralGap(
                            source=node_id, target=target,
                            edge=edge, target_scope="unresolved",
                        ))
                        logger.debug(
                            f"BFS 遇到未解析节点: {target} "
                            f"(来自 {node_id}, 边类型={edge.edge_type.name})"
                        )
                    continue

                # C3: 低置信边不自动传播 → 降级为结构缺口
                if (edge.edge_type in (EdgeType.CALLS, EdgeType.USES)
                        and edge.confidence < self.policy.min_edge_confidence):
                    if edge.is_hard:
                        structural_gaps.append(StructuralGap(
                            source=node_id, target=target,
                            edge=edge, target_scope="peripheral",
                        ))
                    continue

                # ── 按区域分流 ──
                # out_of_scope 边界约束：进入排除目录的硬依赖降级为缺口
                if excluded_dirs and self._in_excluded_scope(target_node, excluded_dirs):
                    if edge.is_hard:
                        structural_gaps.append(StructuralGap(
                            source=node_id, target=target,
                            edge=edge, target_scope="outside",
                        ))
                    continue

                if target in scope.core:
                    result.required_nodes.add(target)
                    self._propagate_req_ids(result, node_id, target)
                    core_auto_included.add(target)  # P1: 追踪 CORE 自动包含
                    # F18: FILE 节点经 IMPORTS 到达时，用符号级传播替代全量 BFS
                    # 避免大型工具文件（如 helpers.py）的全部子符号被无差别拉入
                    if (target.startswith("file:") and
                            edge.edge_type == EdgeType.IMPORTS):
                        self._import_symbol_level(
                            target, result, queue, depth, edge, scope,
                            strict=True,
                        )
                    else:
                        queue.append((target, depth + 1))

                elif target in scope.peripheral:
                    decision = self._peripheral_decision(
                        edge, target, target_node, result.required_nodes,
                    )
                    if decision == "include":
                        result.required_nodes.add(target)
                        self._propagate_req_ids(result, node_id, target)
                        queue.append((target, depth + 1))
                    elif decision == "import_propagation":
                        self._import_symbol_level(
                            target, result, queue, depth, edge, scope,
                        )
                    elif decision == "gap":
                        structural_gaps.append(StructuralGap(
                            source=node_id, target=target,
                            edge=edge, target_scope="peripheral",
                            req_id=",".join(sorted(result.node_requirements.get(node_id, []))),
                        ))
                    # "skip" → 不处理

                else:  # OUTSIDE
                    if edge.is_hard:
                        structural_gaps.append(StructuralGap(
                            source=node_id, target=target,
                            edge=edge, target_scope="outside",
                            req_id=",".join(sorted(result.node_requirements.get(node_id, []))),
                        ))
                    # 软依赖到 OUTSIDE → 忽略

            # ── 闭包大小实时监控 ──
            if len(result.required_nodes) % check_interval == 0 and len(result.required_nodes) > 0:
                ratio = self._compute_code_ratio(result.required_nodes)
                if ratio > self.policy.max_closure_ratio * 0.8:
                    # 自动收紧：将 PERIPHERAL 中尚未加入的节点降级为 OUTSIDE
                    unreached_peripheral = scope.peripheral - result.required_nodes
                    scope.outside.update(unreached_peripheral)
                    scope.peripheral -= unreached_peripheral
                    auto_tighten_events.append({
                        "required_nodes": len(result.required_nodes),
                        "code_ratio": round(ratio, 4),
                        "downgraded_peripheral_nodes": len(unreached_peripheral),
                    })
                    logger.warning(
                        f"闭包达 {ratio:.0%}，自动收紧边界 "
                        f"(降级 {len(unreached_peripheral)} 个 PERIPHERAL 节点)"
                    )

        return structural_gaps, core_auto_included

    def _peripheral_decision(
        self, edge: Edge, target_id: str, target_node: CodeNode,
        closure_nodes: set[str],
    ) -> str:
        """
        PERIPHERAL 区域的精细决策。
        返回 "include" | "import_propagation" | "gap" | "skip"
        """
        # 结构性必含边
        if edge.edge_type in (EdgeType.CONTAINS, EdgeType.INHERITS, EdgeType.IMPLEMENTS):
            return "include"

        # TypeScript type-only import → 交仲裁
        if edge.edge_type == EdgeType.IMPORTS and edge.metadata.get("type_only"):
            return "gap"

        # import 边 + 目标是文件 → 符号级传播
        if edge.edge_type == EdgeType.IMPORTS:
            if target_node.node_type == NodeType.FILE:
                return "import_propagation"
            return "include"  # import 指向具体符号 → 直接加入

        # CALLS/USES → 独占性判断
        if edge.edge_type in (EdgeType.CALLS, EdgeType.USES):
            exclusivity = self._compute_exclusivity(target_id, closure_nodes)
            if exclusivity > self.policy.exclusivity_include_threshold:
                logger.debug(
                    f"PERIPHERAL include: {target_node.qualified_name} "
                    f"独占性={exclusivity:.2f} > {self.policy.exclusivity_include_threshold}"
                )
                return "include"
            return "gap"

        # 语义边 (SEMANTIC_RELATED, COOPERATES) → gap
        if edge.is_soft:
            return "gap"

        return "gap"

    def _import_symbol_level(
        self, file_node_id: str, result: ClosureResult,
        queue: deque, depth: int,
        import_edge: Edge | None = None,
        scope: ScopeClassification | None = None,
        strict: bool = False,
    ) -> None:
        """
        import 边符号级传播（仅在 PERIPHERAL 区域启用）:
        策略 0: from X import * + __all__ → 只拉入 __all__ 中的符号
        策略 1: imported_symbols 精确匹配
        策略 2: 回退查 CALLS/INHERITS/USES 引用
        策略 3: 保守拉入整个文件
        """
        file_node = self.graph.get_node(file_node_id)
        if not file_node:
            return

        # 该文件包含的所有子符号（含类的子方法）
        file_children: dict[str, CodeNode] = {}
        for child_id in file_node.children:
            child = self.graph.get_node(child_id)
            if child:
                file_children[child_id] = child
                for grandchild_id in child.children:
                    gc = self.graph.get_node(grandchild_id)
                    if gc:
                        file_children[grandchild_id] = gc

        referenced: set[str] = set()

        # 策略 0: from X import * + __all__
        if (import_edge and import_edge.metadata.get("imported_symbols")
                and "*" in import_edge.metadata["imported_symbols"]):
            dunder_all = file_node.metadata.get("__all__")
            if dunder_all:
                exported_names = set(dunder_all)
                for cid, child in file_children.items():
                    if child.name in exported_names:
                        referenced.add(cid)
                if referenced:
                    for sym_id in referenced:
                        if sym_id not in result.required_nodes:
                            result.required_nodes.add(sym_id)
                            self._propagate_req_ids(result, file_node_id, sym_id)
                            queue.append((sym_id, depth + 1))
                    logger.debug(
                        f"import * + __all__: {file_node.name} → "
                        f"{len(referenced)}/{len(file_children)} 个符号"
                    )
                    return

        # 策略 1: imported_symbols 精确匹配
        if import_edge and import_edge.metadata.get("imported_symbols"):
            imported_names = set(import_edge.metadata["imported_symbols"])
            imported_names.discard("*")
            for cid, child in file_children.items():
                if child.name in imported_names:
                    referenced.add(cid)

            # F22: strict + barrel file (re-export) → 只保留实际被引用的符号
            # 防止 __init__.py 的转发导入无差别拉入所有符号
            if strict and referenced and import_edge.source.startswith("file:"):
                src_node = self.graph.get_node(import_edge.source)
                if src_node and src_node.name == "__init__.py":
                    actually_used: set[str] = set()
                    for existing_id in list(result.required_nodes):
                        for e in self.graph.get_outgoing(existing_id):
                            if (e.edge_type in (EdgeType.CALLS, EdgeType.INHERITS,
                                                EdgeType.IMPLEMENTS, EdgeType.USES)
                                    and e.target in referenced):
                                actually_used.add(e.target)
                    if actually_used:
                        filtered = referenced - actually_used
                        if filtered:
                            logger.info(
                                f"F22 barrel 过滤: {file_node.name} "
                                f"保留 {len(actually_used)}, 跳过 {len(filtered)}"
                            )
                        referenced = actually_used

        # 策略 2: 从闭包已有节点的 CALLS/INHERITS/USES 边查找
        if not referenced and not strict:
            for existing_id in list(result.required_nodes):
                for e in self.graph.get_outgoing(existing_id):
                    if e.edge_type in (EdgeType.CALLS, EdgeType.INHERITS,
                                       EdgeType.IMPLEMENTS, EdgeType.USES):
                        if e.target in file_children:
                            referenced.add(e.target)

        if referenced:
            for sym_id in referenced:
                if sym_id not in result.required_nodes:
                    result.required_nodes.add(sym_id)
                    self._propagate_req_ids(result, file_node_id, sym_id)
                    queue.append((sym_id, depth + 1))
            logger.debug(
                f"import 符号传播: {file_node.name} → "
                f"{len(referenced)}/{len(file_children)} 个符号"
            )
        elif strict:
            # F18: strict 模式下，无法确定具体符号时，降级为文件级包含
            # P3-fix: 不再静默 return，避免整个模块丢失
            if file_node_id not in result.required_nodes:
                result.required_nodes.add(file_node_id)
                queue.append((file_node_id, depth + 1))
            logger.debug(
                f"import 符号传播 strict 降级: {file_node.name} → 整文件"
            )
            return
        else:
            # E2: 策略 3 增强 — 先检查是否有 CORE 子符号
            if scope:
                core_children = [
                    cid for cid in file_children
                    if cid in scope.core
                ]
                if core_children:
                    for sym_id in core_children:
                        if sym_id not in result.required_nodes:
                            result.required_nodes.add(sym_id)
                            self._propagate_req_ids(result, file_node_id, sym_id)
                            queue.append((sym_id, depth + 1))
                    logger.debug(
                        f"import 回退(CORE 子符号): {file_node.name} → "
                        f"{len(core_children)}/{len(file_children)} 个符号"
                    )
                    return

            # 策略 3 最终回退 — 拉入整个文件
            if file_node_id not in result.required_nodes:
                result.required_nodes.add(file_node_id)
                queue.append((file_node_id, depth + 1))
            logger.debug(f"import 回退: 整个文件 {file_node.name}")

    # ═══════════════════ Step 3: 缺口仲裁 ═══════════════════

    def _arbitrate_gaps(
        self,
        gaps: list[StructuralGap],
        result: ClosureResult,
        scope: ScopeClassification,
        features_text: str,
        max_depth: int,
    ) -> None:
        """仲裁结构缺口，迭代处理 include 后产生的二级缺口"""
        pending = self._merge_gaps(gaps)
        max_iterations = self.policy.max_gap_iterations

        for iteration in range(max_iterations):
            if not pending:
                break

            newly_included: set[str] = set()
            batch_for_llm: list[MergedGap] = []

            # 规则快筛
            for gap in pending:
                rule_decision = self._rule_arbitrate(gap, result.required_nodes, scope)
                if rule_decision:
                    target_node = self.graph.get_node(gap.target)
                    logger.debug(
                        f"规则仲裁: {target_node.qualified_name if target_node else gap.target} "
                        f"→ {rule_decision} (被 {gap.count} 处引用)"
                    )
                    self._apply_gap_decision(gap, rule_decision, result)
                    if rule_decision == "include":
                        newly_included.add(gap.target)
                else:
                    batch_for_llm.append(gap)

            # LLM 仲裁
            if batch_for_llm:
                decisions = self._llm_batch_judge_gaps(
                    batch_for_llm, features_text, result,
                )
                for gap, decision in zip(batch_for_llm, decisions):
                    self._apply_gap_decision(gap, decision, result)
                    if decision == "include":
                        newly_included.add(gap.target)

            logger.info(
                f"缺口仲裁迭代 {iteration + 1}: "
                f"规则 {len(pending) - len(batch_for_llm)}, "
                f"LLM {len(batch_for_llm)}, "
                f"新 include {len(newly_included)}"
            )

            # 新 include 节点的出边 → 新缺口
            if not newly_included:
                break

            # 对新 include 的节点做 BFS 扩展（在语义范围内）
            new_gaps = self._expand_newly_included(
                newly_included, result, scope, max_depth,
            )
            pending = self._merge_gaps(new_gaps)

    def _merge_gaps(self, gaps: list[StructuralGap]) -> list[MergedGap]:
        """按 target 合并缺口，被依赖最多的优先判断"""
        by_target: dict[str, list[StructuralGap]] = {}
        for g in gaps:
            # 跳过已决策的目标
            by_target.setdefault(g.target, []).append(g)

        merged = []
        for target_id, group in by_target.items():
            req_ids: set[str] = set()
            for g in group:
                if g.req_id:
                    req_ids.update(r for r in g.req_id.split(",") if r)
            merged.append(MergedGap(
                target=target_id,
                sources=[(g.source, g.edge) for g in group],
                target_scope=group[0].target_scope,
                count=len(group),
                req_ids=req_ids,
            ))
        merged.sort(key=lambda m: -m.count)
        return merged

    def _rule_arbitrate(self, gap: MergedGap, closure_nodes: set[str], scope: ScopeClassification | None = None) -> str | None:
        """
        规则层快速仲裁，能决定的不送 LLM。
        返回 "include" | "stub" | "exclude" | None
        """
        target_node = self.graph.get_node(gap.target)
        if not target_node:
            return "exclude"

        # F19: dir_excluded 硬阻断
        # 指令分析器明确排除的节点一律 exclude，不允许任何规则覆盖
        # 编译依赖由 Phase 3 import_fixer 清理断裂引用 + fixer 修复
        if scope and gap.target in scope.dir_excluded:
            logger.debug(
                f"F19 硬排除: {target_node.qualified_name} "
                f"(dir_pattern={gap.target in scope.dir_pattern_excluded})"
            )
            return "exclude"

        # R_OUT: 闭包节点对 outside 目标的硬依赖 → include
        # 跨排除边界的 CALLS/IMPORTS/INHERITS/IMPLEMENTS 是结构依赖
        if gap.target_scope == "outside":
            has_hard_dep = any(
                e.edge_type in (EdgeType.CALLS, EdgeType.IMPORTS,
                                EdgeType.INHERITS, EdgeType.IMPLEMENTS)
                for _, e in gap.sources
            )
            if has_hard_dep:
                logger.debug(
                    f"R_OUT include: {target_node.qualified_name} "
                    f"— outside 但被闭包 CALLS/IMPORTS，交 surgeon 部分裁剪"
                )
                return "include"

        # R0: C/C++ 头文件保护 — 项目内头文件默认 include
        # 理由: C/C++ 头文件通常包含类型定义/宏/结构体，stub 会破坏编译。
        # 保守策略: 只要是项目内（非系统）头文件且被闭包代码引用，就 include。
        if target_node.file_path and str(target_node.file_path).endswith(('.h', '.hpp', '.hxx')):
            return "include"

        # R1: 类型定义/接口/枚举 → include（编译必需，体积小）
        if target_node.node_type in (NodeType.INTERFACE, NodeType.ENUM):
            return "include"

        # R2: 代码极小（< small_code_threshold 行）→ include
        if target_node.byte_range:
            lines = target_node.byte_range.end_line - target_node.byte_range.start_line
            if lines < self.policy.small_code_threshold:
                return "include"

        # R3: 独占性极高 → include
        exclusivity = self._compute_exclusivity(gap.target, closure_nodes)
        if exclusivity > self.policy.exclusivity_rule_threshold:
            return "include"

        # R4: 入度极高 → stub（典型基础设施）
        total_incoming = len(self.graph.get_incoming(gap.target))
        if total_incoming > self.policy.infra_in_degree_threshold:
            return "stub"

        # R5: 匹配排除关键词 → exclude
        if self._matches_exclude_patterns(target_node):
            return "exclude"

        # R6: G1 按语义类别快筛
        category = target_node.metadata.get("semantic_category")
        if category == "infrastructure":
            return "stub"
        if category == "test":
            return "exclude"

        return None

    def _llm_batch_judge_gaps(
        self,
        gaps: list[MergedGap],
        features_text: str,
        result: ClosureResult,
    ) -> list[str]:
        """批量 LLM 仲裁结构缺口"""
        decisions: list[str] = []
        batch_size = 5
        selected_summaries = self._build_selected_summaries(result.required_nodes)

        for bi in range(0, len(gaps), batch_size):
            batch = gaps[bi:bi + batch_size]
            if len(batch) == 1:
                decision = self._llm_judge_single_gap(
                    batch[0], features_text, selected_summaries,
                )
                decisions.append(decision)
            else:
                batch_decisions = self._llm_judge_gap_batch(
                    batch, features_text, selected_summaries,
                )
                decisions.extend(batch_decisions)

        return decisions

    def _llm_judge_single_gap(
        self, gap: MergedGap, features_text: str, selected_summaries: str,
    ) -> str:
        """LLM 判断单个缺口"""
        target_node = self.graph.get_node(gap.target)
        if not target_node:
            return "exclude"

        callers_context = self._build_callers_context(gap.sources)
        other_callers, other_count = self._build_other_callers_context(
            gap.target, {s for s, _ in gap.sources},
        )
        req_ctx = self._build_req_context(gap.req_ids)
        summary = (target_node.summary or "(no summary)") + req_ctx

        prompt = Prompts.JUDGE_STRUCTURAL_GAP.format(
            user_instruction=features_text,
            name=target_node.qualified_name,
            node_type=target_node.node_type.value,
            file_path=target_node.file_path or "N/A",
            summary=summary,
            callers_context=callers_context,
            other_count=other_count,
            other_callers=other_callers,
        )
        try:
            resp = self.llm.chat_json([{"role": "user", "content": prompt}])
            decision = resp.get("decision", "stub")
            if decision in ("include", "stub", "exclude"):
                return decision
            return "stub" if self.policy.prefer_stub else "exclude"
        except Exception as e:
            logger.warning(f"缺口仲裁失败 [{gap.target}]: {e}")
            return "stub" if self.policy.prefer_stub else "exclude"

    def _build_req_context(self, req_ids: set[str]) -> str:
        """根据 req_ids 查找对应 sub_feature 描述，返回简短上下文字符串"""
        if not req_ids:
            return ""
        analysis = self.config.instruction_analysis
        if not analysis or not analysis.sub_features:
            return ""
        descs = []
        for sf in analysis.sub_features:
            if sf.req_id in req_ids:
                descs.append(f"{sf.req_id}:{sf.description}")
        return f" [Affects: {'; '.join(descs)}]" if descs else ""

    def _llm_judge_gap_batch(
        self,
        gaps: list[MergedGap],
        features_text: str,
        selected_summaries: str,
    ) -> list[str]:
        """批量 LLM 仲裁多个缺口"""
        entities_text = []
        for i, gap in enumerate(gaps):
            node = self.graph.get_node(gap.target)
            if not node:
                entities_text.append(f"{i + 1}. (unknown node)")
                continue
            callers = ", ".join(
                n.qualified_name
                for s, _ in gap.sources[:3]
                if (n := self.graph.get_node(s)) is not None
            )
            req_ctx = self._build_req_context(gap.req_ids)
            entities_text.append(
                f"{i + 1}. {node.qualified_name} ({node.node_type.value}) "
                f"in {node.file_path or 'N/A'}: {node.summary or '(no summary)'} "
                f"[called by: {callers}]{req_ctx}"
            )

        prompt = Prompts.JUDGE_STRUCTURAL_GAP_BATCH.format(
            user_instruction=features_text,
            entities_text="\n".join(entities_text),
            selected_summaries=selected_summaries,
            count=len(gaps),
        )
        try:
            resp = self.llm.chat_json([{"role": "user", "content": prompt}])
            raw = resp.get("decisions", [])
            if len(raw) == len(gaps):
                return [
                    d if d in ("include", "stub", "exclude")
                    else ("stub" if self.policy.prefer_stub else "exclude")
                    for d in raw
                ]
            logger.warning("批量仲裁返回长度不匹配，回退逐个判断")
        except Exception as e:
            logger.warning(f"批量缺口仲裁失败: {e}")

        # 回退逐个
        summaries = self._build_selected_summaries(set())  # 避免重复计算
        return [
            self._llm_judge_single_gap(g, features_text, summaries)
            for g in gaps
        ]

    def _verify_core_inclusions(
        self,
        core_nodes: set[str],
        features_text: str,
        result: ClosureResult,
    ) -> set[str]:
        """
        P1: 对 BFS 中 CORE 自动包含的节点做 LLM 批量复核。
        返回应被移除的节点 ID 集合。
        """
        # 只验证有 summary 的非 FILE 节点（FILE 节点通常通过 IMPORTS 结构性到达）
        candidates = []
        for nid in core_nodes:
            node = self.graph.get_node(nid)
            if not node:
                continue
            # 跳过 FILE/DIRECTORY 节点 — 它们由 IMPORTS 结构到达，不走语义判断
            if node.node_type in (NodeType.FILE, NodeType.DIRECTORY, NodeType.REPOSITORY):
                continue
            candidates.append((nid, node))

        if not candidates:
            return set()

        rejected: set[str] = set()
        selected_summaries = self._build_selected_summaries(
            result.required_nodes - core_nodes,
        )
        batch_size = 10

        for bi in range(0, len(candidates), batch_size):
            batch = candidates[bi:bi + batch_size]
            entities_text = []
            for i, (nid, node) in enumerate(batch):
                entities_text.append(
                    f"{i + 1}. {node.qualified_name} ({node.node_type.value}) "
                    f"in {node.file_path or 'N/A'}: "
                    f"{node.summary or '(no summary)'}"
                )
            prompt = Prompts.VERIFY_CORE_INCLUSIONS.format(
                user_instruction=features_text,
                entities_text="\n".join(entities_text),
                selected_summaries=selected_summaries,
                count=len(batch),
            )
            try:
                resp = self.llm.chat_json([{"role": "user", "content": prompt}])
                raw = resp.get("decisions", [])
                if len(raw) == len(batch):
                    for j, decision in enumerate(raw):
                        if decision == "reject":
                            nid = batch[j][0]
                            node = batch[j][1]
                            rejected.add(nid)
                            logger.debug(
                                f"CORE 校验拒绝: {node.qualified_name}"
                            )
                else:
                    logger.warning(
                        f"CORE 校验返回长度不匹配: "
                        f"期望 {len(batch)}, 收到 {len(raw)}"
                    )
            except Exception as e:
                logger.warning(f"CORE 校验批次失败: {e}")

        return rejected

    def _apply_gap_decision(
        self, gap: MergedGap, decision: str, result: ClosureResult,
    ) -> None:
        """应用仲裁决策"""
        if decision == "include":
            result.required_nodes.add(gap.target)
            result.soft_included.add(gap.target)
            # P1: 传播 req_ids — 从所有 source 节点合并
            for source_id, edge in gap.sources:
                self._propagate_req_ids(result, source_id, gap.target)
        elif decision == "stub":
            result.stub_nodes.add(gap.target)
        elif decision == "exclude":
            result.soft_excluded.add(gap.target)
            for source_id, edge in gap.sources:
                result.excluded_edges.append((source_id, gap.target))

    def _expand_newly_included(
        self,
        newly_included: set[str],
        result: ClosureResult,
        scope: ScopeClassification,
        max_depth: int,
    ) -> list[StructuralGap]:
        """对新 include 的节点做 BFS 扩展，返回新产生的缺口"""
        new_gaps: list[StructuralGap] = []
        queue: deque[tuple[str, int]] = deque((nid, 0) for nid in newly_included)

        while queue:
            nid, depth = queue.popleft()
            if depth >= max_depth:
                break
            for edge in self.graph.get_outgoing(nid):
                target = edge.target
                if target in result.required_nodes or target in result.stub_nodes:
                    continue
                target_node = self.graph.get_node(target)
                if not target_node:
                    continue

                # C3: 低置信边降级为缺口
                if (edge.edge_type in (EdgeType.CALLS, EdgeType.USES)
                        and edge.confidence < self.policy.min_edge_confidence):
                    if edge.is_hard:
                        new_gaps.append(StructuralGap(
                            source=nid, target=target,
                            edge=edge, target_scope="peripheral",
                        ))
                    continue

                if target in scope.core:
                    result.required_nodes.add(target)
                    self._propagate_req_ids(result, nid, target)
                    queue.append((target, depth + 1))
                elif target in scope.peripheral:
                    decision = self._peripheral_decision(
                        edge, target, target_node, result.required_nodes,
                    )
                    if decision == "include":
                        result.required_nodes.add(target)
                        self._propagate_req_ids(result, nid, target)
                        queue.append((target, depth + 1))
                    elif decision == "import_propagation":
                        self._import_symbol_level(
                            target, result, queue, depth, edge, scope,
                        )
                    elif decision == "gap":
                        new_gaps.append(StructuralGap(
                            source=nid, target=target,
                            edge=edge, target_scope="peripheral",
                            req_id=",".join(sorted(result.node_requirements.get(nid, []))),
                        ))
                else:  # OUTSIDE
                    if edge.is_hard:
                        new_gaps.append(StructuralGap(
                            source=nid, target=target,
                            edge=edge, target_scope="outside",
                            req_id=",".join(sorted(result.node_requirements.get(nid, []))),
                        ))
        return new_gaps

    # ═══════════════ Step 3.5: Per-Requirement 完整性校验 ═══════════════

    def _verify_requirement_completeness(
        self,
        analysis,
        result: ClosureResult,
        scope: ScopeClassification,
    ) -> None:
        """检查每个 sub_feature 的功能链完整度，低覆盖时触发定向补全"""
        for sf in analysis.sub_features:
            if not sf.req_id:
                continue

            # 该需求拥有的闭包节点
            req_nodes = {
                nid for nid, reqs in result.node_requirements.items()
                if sf.req_id in reqs
            }

            # 该需求相关的 CORE 节点：与 sub_feature 描述 embedding 匹配的 CORE 节点
            # 优先使用已有的 relevance_map 避免额外 embedding 调用
            req_core: set[str] = set()
            if result.relevance_map:
                core_thresh = 0.0
                # 取该需求已有锚点的 relevance 作为参照
                anchor_scores = [
                    result.relevance_map.get(nid, 0)
                    for nid in req_nodes
                    if result.relevance_map.get(nid) is not None
                ]
                if anchor_scores:
                    core_thresh = min(s for s in anchor_scores if s is not None) * 0.6

                for nid in scope.core:
                    rel = result.relevance_map.get(nid)
                    if rel is not None and rel >= max(core_thresh, 0.2):
                        req_core.add(nid)

            if not req_core:
                continue

            covered = req_core & result.required_nodes
            coverage = len(covered) / len(req_core)

            # 回填审计信息
            sf.covered_nodes = covered
            sf.coverage_ratio = coverage

            logger.info(
                f"[{sf.req_id}] '{sf.description}' 覆盖率: {coverage:.0%} "
                f"({len(covered)}/{len(req_core)} CORE 节点)"
            )

            if coverage < 0.5 and (missing := req_core - result.required_nodes):
                logger.warning(
                    f"[{sf.req_id}] 覆盖率不足，触发定向补全: "
                    f"缺失 {len(missing)} 个 CORE 节点"
                )
                self._targeted_recovery(sf.req_id, missing, result)

    def _targeted_recovery(
        self, req_id: str, missing_nodes: set[str], result: ClosureResult,
    ) -> None:
        """针对性补全：对覆盖不足的子功能，恢复有闭包调用关系的 CORE 节点"""
        recovered = 0
        for nid in missing_nodes:
            node = self.graph.get_node(nid)
            if not node:
                continue

            # 检查：缺失节点是否被闭包内的节点 import/call？
            incoming = self.graph.get_incoming(nid)
            has_closure_caller = any(
                e.source in result.required_nodes for e in incoming
            )

            if has_closure_caller:
                result.required_nodes.add(nid)
                result.node_requirements.setdefault(nid, set()).add(req_id)
                recovered += 1
            else:
                # 反向检查：该节点是否依赖 ≥2 个闭包内节点？
                outgoing = self.graph.get_outgoing(nid)
                deps_in_closure = sum(
                    1 for e in outgoing if e.target in result.required_nodes
                )
                if deps_in_closure >= 2:
                    result.required_nodes.add(nid)
                    result.node_requirements.setdefault(nid, set()).add(req_id)
                    recovered += 1

        if recovered:
            logger.info(f"[{req_id}] 定向补全恢复 {recovered} 个节点")

    # ═══════════════════ Step 4: 后处理 ═══════════════════

    def _ensure_containment_chain(self, result: ClosureResult) -> None:
        """确保包含链完整：选中的符号 → 其所属类 → 文件 → 目录"""
        to_add: set[str] = set()
        for node_id in list(result.required_nodes):
            current = node_id
            while True:
                incoming = self.graph.get_incoming(current, EdgeType.CONTAINS)
                if not incoming:
                    break
                parent_id = incoming[0].source
                if parent_id in result.required_nodes or parent_id in to_add:
                    break
                to_add.add(parent_id)
                current = parent_id
        result.required_nodes.update(to_add)

    def _auto_include_init_py_recursive(self, result: ClosureResult) -> None:
        """
        递归向上包含所有祖先目录的 __init__.py。
        如 `a/b/c.py` 在闭包中 → 包含 `a/b/__init__.py` 和 `a/__init__.py`。
        """
        py_dirs: set[Path] = set()
        for nid in list(result.required_nodes):
            node = self.graph.get_node(nid)
            if (node and node.node_type == NodeType.FILE
                    and node.file_path and str(node.file_path).endswith(".py")):
                current = Path(node.file_path).parent
                while current != Path(".") and current != current.parent:
                    py_dirs.add(current)
                    current = current.parent

        for dir_path in py_dirs:
            init_path = dir_path / "__init__.py"
            # 通过路径索引查找（避免遍历全节点）
            init_node_id = f"file:{init_path}"
            if init_node_id in self.graph.nodes and init_node_id not in result.required_nodes:
                result.required_nodes.add(init_node_id)
                logger.debug(f"自动包含 __init__.py: {init_path}")

    def _auto_include_barrel_files(self, result: ClosureResult, scope) -> None:
        """
        自动包含 barrel 文件（如 index.ts / index.js），并递归追踪 re-export 链。
        如果某目录下已有 TS/JS 文件被闭包包含，且该目录存在 index 文件
        （典型 barrel / re-export），则自动包含它，并沿 IMPORTS 边追踪其
        re-export 的目标文件。
        """
        BARREL_NAMES = ("index.ts", "index.js", "index.tsx", "index.jsx")
        BARREL_SUFFIXES = (".ts", ".tsx", ".js", ".jsx")

        # 收集闭包中 TS/JS FILE 节点所在的目录
        dirs_with_closure_files: set[Path] = set()
        for nid in list(result.required_nodes):
            node = self.graph.get_node(nid)
            if node and node.node_type == NodeType.FILE and node.file_path:
                fp = Path(node.file_path)
                if fp.suffix in BARREL_SUFFIXES:
                    dirs_with_closure_files.add(fp.parent)

        newly_added: list[str] = []
        for dir_path in dirs_with_closure_files:
            for barrel_name in BARREL_NAMES:
                barrel_path = dir_path / barrel_name
                barrel_id = f"file:{barrel_path}"
                if barrel_id in result.required_nodes:
                    continue
                if barrel_id not in self.graph.nodes:
                    continue
                result.required_nodes.add(barrel_id)
                newly_added.append(barrel_id)
                logger.debug(f"自动包含 barrel 文件: {barrel_path}")

        # P0-fix: 递归追踪 barrel 的 IMPORTS 出边（re-export 链）
        visited: set[str] = set(result.required_nodes)
        while newly_added:
            current_batch = newly_added
            newly_added = []
            for barrel_id in current_batch:
                for edge in self.graph.get_outgoing(barrel_id):
                    if edge.edge_type != EdgeType.IMPORTS:
                        continue
                    target_id = edge.target
                    if target_id in visited:
                        continue
                    visited.add(target_id)
                    target_node = self.graph.get_node(target_id)
                    if not target_node or target_node.node_type != NodeType.FILE:
                        continue
                    if not target_node.file_path:
                        continue
                    if Path(target_node.file_path).suffix not in BARREL_SUFFIXES:
                        continue
                    result.required_nodes.add(target_id)
                    newly_added.append(target_id)
                    logger.debug(
                        f"barrel re-export 追踪: {barrel_id} → {target_node.file_path}"
                    )

    def _upgrade_full_classes(self, result: ClosureResult) -> None:
        """
        粒度升级：如果一个 CLASS 的所有 FUNCTION 子节点都在闭包中，
        标记 fullclass=True 供 surgeon 知道可以整类提取。
        """
        upgraded = 0
        for nid, node in self.graph.nodes.items():
            if node.node_type not in (NodeType.CLASS, NodeType.INTERFACE):
                continue
            # 只检查 FUNCTION 类型的子节点
            func_children = [
                c for c in node.children
                if (cn := self.graph.get_node(c)) is not None
                and cn.node_type == NodeType.FUNCTION
            ]
            if not func_children:
                continue
            if all(c in result.required_nodes for c in func_children):
                if nid not in result.required_nodes:
                    result.required_nodes.add(nid)
                node.metadata["fullclass"] = True
                upgraded += 1
        if upgraded:
            logger.info(f"粒度升级: {upgraded} 个类标记为整类提取")

    def _expand_class_children_if_full(self, result: ClosureResult) -> None:
        """只展开标记了 fullclass 的类（修正 v1 的无条件展开问题）"""
        to_add: set[str] = set()
        for nid in list(result.required_nodes):
            node = self.graph.get_node(nid)
            if node and node.metadata.get("fullclass"):
                for child_id in node.children:
                    if child_id not in result.required_nodes:
                        to_add.add(child_id)
        if to_add:
            result.required_nodes.update(to_add)
            logger.debug(f"fullclass 展开: +{len(to_add)} 个子节点")

    def _final_size_check(self, result: ClosureResult) -> None:
        """闭包大小终检（使用代码行数而非节点数）"""
        total_lines = 0
        closure_lines = 0
        # 优先使用 FUNCTION 级别统计；若无 FUNCTION 节点则回退到 FILE 级别
        count_types = {NodeType.FUNCTION}
        for nid, node in self.graph.nodes.items():
            if node.byte_range and node.node_type == NodeType.FUNCTION:
                total_lines += 1  # 先探测有无 FUNCTION 节点
                break
        if total_lines == 0:
            count_types = {NodeType.FILE}
        total_lines = 0  # 重置

        for nid, node in self.graph.nodes.items():
            if node.byte_range and node.node_type in count_types:
                lines = node.byte_range.end_line - node.byte_range.start_line
                total_lines += lines
                if nid in result.required_nodes:
                    closure_lines += lines

        if total_lines > 0:
            ratio = closure_lines / total_lines
            result.diagnostics["final_size"] = {
                "closure_lines": closure_lines,
                "total_lines": total_lines,
                "ratio": round(ratio, 4),
            }
            if ratio > 0.7:
                result.diagnostics.setdefault("warnings", []).append("final_closure_ratio_high")
                logger.warning(
                    f"⚠ 闭包过大: {closure_lines}/{total_lines} 行 ({ratio:.0%})，"
                    f"裁剪效果不佳，建议缩小指令范围"
                )

    # ═══════════════════ 工具方法 ═══════════════════

    def _compute_exclusivity(self, target_id: str, closure_nodes: set[str]) -> float:
        """
        目标节点被闭包内节点独占使用的程度。
        高独占性 → 属于该功能的专属依赖
        低独占性 → 共享基础设施
        """
        all_callers: set[str] = set()
        for edge_type in (EdgeType.CALLS, EdgeType.USES, EdgeType.INHERITS):
            for e in self.graph.get_incoming(target_id, edge_type):
                all_callers.add(e.source)
        if not all_callers:
            return 0.5
        closure_callers = all_callers & closure_nodes
        return len(closure_callers) / len(all_callers)

    def _compute_code_ratio(self, node_ids: set[str]) -> float:
        """计算闭包中代码行数占总代码行数的比例"""
        total = 0
        selected = 0
        # 优先 FUNCTION，无则回退 FILE
        count_type = NodeType.FUNCTION
        has_func = any(
            n.byte_range and n.node_type == NodeType.FUNCTION
            for n in self.graph.nodes.values()
        )
        if not has_func:
            count_type = NodeType.FILE
        for nid, node in self.graph.nodes.items():
            if node.byte_range and node.node_type == count_type:
                lines = node.byte_range.end_line - node.byte_range.start_line
                total += lines
                if nid in node_ids:
                    selected += lines
        return selected / total if total > 0 else 0.0

    def _matches_exclude_patterns(self, node: CodeNode) -> bool:
        """检查节点是否匹配用户排除关键词"""
        if not self.policy.exclude_keywords:
            return False
        name_lower = (node.qualified_name or node.name).lower()
        summary_lower = (node.summary or "").lower()
        return any(
            kw.lower() in name_lower or kw.lower() in summary_lower
            for kw in self.policy.exclude_keywords
        )

    def _header_has_definitions(self, node: CodeNode) -> bool:
        """检查头文件是否包含结构体/宏/类型定义（区分于纯声明头文件）"""
        # 方法 1: 检查子节点类型 — 有 CLASS/ENUM 子节点说明有定义
        for child_id in node.children:
            child = self.graph.get_node(child_id)
            if child and child.node_type in (NodeType.CLASS, NodeType.ENUM, NodeType.INTERFACE):
                return True
        # 方法 2: 检查摘要关键词
        summary = (node.summary or "").lower()
        definition_keywords = ("struct", "typedef", "enum", "macro", "#define", "class")
        if any(kw in summary for kw in definition_keywords):
            return True
        return False

    def _build_callers_context(self, sources: list[tuple[str, Edge]]) -> str:
        """构建调用来源的上下文描述"""
        lines = []
        for source_id, edge in sources[:5]:
            source_node = self.graph.get_node(source_id)
            if source_node:
                lines.append(
                    f"  - {source_node.qualified_name} ({edge.edge_type.value})"
                )
        return "\n".join(lines) if lines else "  (无调用来源信息)"

    def _build_other_callers_context(
        self, target_id: str, closure_sources: set[str],
    ) -> tuple[str, int]:
        """构建闭包外调用者的上下文描述"""
        all_callers: list[str] = []
        for e in self.graph.get_incoming(target_id):
            if e.source not in closure_sources:
                all_callers.append(e.source)
        other_count = len(all_callers)
        lines = []
        for caller_id in all_callers[:5]:
            caller = self.graph.get_node(caller_id)
            if caller:
                lines.append(f"  - {caller.qualified_name}")
        text = "\n".join(lines) if lines else "  (无其他调用者)"
        return text, other_count

    def _build_selected_summaries(self, node_ids: set[str]) -> str:
        """构建已选节点的摘要文本"""
        lines = []
        for nid in sorted(node_ids):
            node = self.graph.get_node(nid)
            if node and node.summary:
                lines.append(f"- {node.qualified_name}: {node.summary}")
        return "\n".join(lines[:50])

    @staticmethod
    def _cosine_sim(a: list[float], b: list[float]) -> float:
        """余弦相似度"""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
