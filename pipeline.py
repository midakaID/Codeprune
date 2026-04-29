"""
CodePrune Pipeline — Phase1 → Phase2 → Phase3 编排
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from config import CodePruneConfig
from core.graph.builder import GraphBuilder
from core.graph.schema import CodeGraph, EdgeType, NodeType
from core.graph.semantic import SemanticEnricher
from core.graph.query import GraphQuery
from core.llm.provider import create_llm_provider, LLMProvider
from core.prune.anchor import AnchorLocator
from core.prune.closure import ClosureSolver
from core.prune.surgeon import Surgeon
from core.prune.instruction_analyzer import InstructionAnalyzer
from core.heal.fixer import HealEngine
from parsers.lang_rules.base import get_language_rules

logger = logging.getLogger(__name__)


class Pipeline:
    """CodePrune 主流程编排"""

    ARTIFACT_DIR = ".codeprune_artifacts"
    GRAPH_FILE = "graph.pkl"

    def __init__(self, config: CodePruneConfig):
        self.config = config
        self.llm: LLMProvider | None = None
        self.graph: CodeGraph | None = None
        self._last_closure = None  # ClosureResult: Phase2 → Finalize 传递

    def _ensure_llm(self) -> None:
        if self.llm is None:
            self.llm = create_llm_provider(self.config.llm)

    def _artifact_path(self, name: str) -> Path:
        return self.config.output_path / self.ARTIFACT_DIR / name

    def run(self) -> Path:
        """
        执行完整 pipeline:
        Phase1: CodeGraph → Phase2: CodePrune → Phase3: CodeHeal
        返回子仓库输出路径
        """
        total_start = time.time()
        logger.info(f"═══════════════════════════════════════")
        logger.info(f"CodePrune Pipeline 启动")
        logger.info(f"仓库: {self.config.repo_path}")
        logger.info(f"指令: {self.config.user_instruction}")
        logger.info(f"输出: {self.config.output_path}")
        logger.info(f"═══════════════════════════════════════")

        # 初始化 LLM
        self._ensure_llm()

        # Phase 1: CodeGraph
        self.graph = self._phase1_code_graph()

        # 保存图谱产物，供后续独立运行 Phase2/3
        graph_path = self._artifact_path(self.GRAPH_FILE)
        self.graph.save(graph_path)
        logger.info(f"图谱已保存: {graph_path}")

        # Phase 2: CodePrune
        sub_repo_path = self._phase2_code_prune()

        # Phase 3: CodeHeal
        self._phase3_code_heal(sub_repo_path)

        elapsed = time.time() - total_start
        logger.info(f"═══════════════════════════════════════")
        logger.info(f"Pipeline 完成, 耗时 {elapsed:.1f}s")
        logger.info(f"子仓库输出: {sub_repo_path}")
        logger.info(f"═══════════════════════════════════════")
        return sub_repo_path

    def _phase1_code_graph(self) -> CodeGraph:
        """Phase1: 构建代码图谱"""
        logger.info("──── Phase1: CodeGraph ────")
        start = time.time()

        # Step 1.1: 物理层图谱
        builder = GraphBuilder(self.config)
        graph = builder.build()

        # Step 1.2: 语义层增强
        if self.config.graph.enable_semantic:
            enricher = SemanticEnricher(self.config, self.llm, graph)
            enricher.enrich()

            # 覆盖率检查 — 根据 scope_strategy 选择检查指标
            total_nodes = sum(
                1 for n in graph.nodes.values()
                if n.node_type not in (NodeType.DIRECTORY, NodeType.REPOSITORY)
            )

            if self.config.prune.scope_strategy == "llm_hierarchical":
                # 新策略: 检查文件级摘要覆盖率（LLM batch 只需要文件摘要）
                file_nodes = [
                    n for n in graph.nodes.values()
                    if n.node_type == NodeType.FILE
                ]
                files_with_summary = sum(1 for n in file_nodes if n.summary)
                if file_nodes:
                    file_coverage = files_with_summary / len(file_nodes)
                    if file_coverage < 0.3:
                        logger.warning(
                            f"文件级摘要覆盖率仅 {file_coverage:.0%} "
                            f"({files_with_summary}/{len(file_nodes)})，"
                            f"分层评估精度可能受影响"
                        )
                    else:
                        logger.info(
                            f"文件级摘要覆盖率 {file_coverage:.0%} "
                            f"({files_with_summary}/{len(file_nodes)})"
                        )
            else:
                # 旧策略: 检查 embedding 覆盖率
                nodes_with_embedding = sum(
                    1 for n in graph.nodes.values()
                    if n.embedding is not None
                )
                if total_nodes > 0:
                    coverage = nodes_with_embedding / total_nodes
                    if coverage < 0.5:
                        logger.error(
                            f"Embedding 覆盖率仅 {coverage:.0%} ({nodes_with_embedding}/{total_nodes})，"
                            f"语义定界将严重不准确。请检查 embedding API 配置。"
                        )
                        raise RuntimeError(
                            f"Embedding 覆盖率不足 ({coverage:.0%})，终止 pipeline"
                        )
                    elif coverage < 0.9:
                        logger.warning(
                            f"Embedding 覆盖率 {coverage:.0%} ({nodes_with_embedding}/{total_nodes})，"
                            f"部分节点将被保守归入 PERIPHERAL 区域"
                        )

            # B3: 可选 embedding 质量诊断
            if self.config.graph.enable_embedding_diagnostics:
                from core.graph.diagnostics import diagnose_embedding_quality
                diagnose_embedding_quality(graph)
        else:
            logger.info("语义层已禁用，跳过")

        logger.info(f"Phase1 完成: {graph.stats}, 耗时 {time.time() - start:.1f}s")

        # Step 1.3: 语言级图谱验证
        for lang in set(n.language for n in graph.nodes.values() if n.language):
            rules = get_language_rules(lang)
            if rules:
                for file_node in graph.nodes.values():
                    if file_node.language == lang and file_node.file_path:
                        warnings = rules.post_build_validate(graph, file_node.file_path)
                        for w in warnings:
                            logger.warning(f"[{lang.value}] {w.file_path}: {w.message}")

        return graph

    def _phase2_code_prune(self) -> Path:
        """Phase2: 剪枝"""
        logger.info("──── Phase2: CodePrune ────")
        start = time.time()

        query = GraphQuery(self.graph)

        # lazy resolution: 如果初始粒度是文件级，先做粗粒度锚定
        # 然后对锚点区域展开细粒度解析
        if self.config.graph.lazy_resolution:
            logger.info("Lazy resolution: 先进行文件级锚定...")

        # Step 2.0: 指令理解（InstructionAnalysis）
        analyzer = InstructionAnalyzer(self.config, self.llm, self.graph)
        analysis = analyzer.analyze(self.config.user_instruction)
        self.config.instruction_analysis = analysis
        if analysis:
            logger.info(
                f"指令理解: {len(analysis.sub_features)} 子功能, "
                f"策略={analysis.anchor_strategy}"
            )

        # Step 2.1: 锚点定位
        locator = AnchorLocator(self.config, self.llm, self.graph)
        anchor_output = locator.locate(self.config.user_instruction)
        anchors = anchor_output.anchors
        query_embedding = anchor_output.query_embedding
        closure_query_embedding = anchor_output.closure_query_embedding
        if not anchors:
            raise RuntimeError("未找到任何锚点，请检查指令描述或仓库内容")

        # lazy resolution: 对锚点区域做细粒度解析，然后重新降级锚点
        if self.config.graph.lazy_resolution:
            pre_count = len(self.graph.nodes)
            anchor_ids = [a.node_id for a in anchors]
            builder = GraphBuilder(self.config)
            builder.graph = self.graph
            builder.resolve_region(anchor_ids)
            logger.debug(f"锚点区域细粒度展开: {anchor_ids}")

            # 对闭包相关文件也做展开（锚点的硬依赖文件）
            dep_files: set[str] = set()
            for a in anchors:
                for edge in self.graph.get_hard_dependencies(a.node_id):
                    target_node = self.graph.get_node(edge.target)
                    if target_node and target_node.file_path:
                        dep_files.add(edge.target)
            if dep_files:
                builder.resolve_region(list(dep_files))
                logger.info(f"扩展解析锚点依赖文件: {len(dep_files)} 个")

            # F21: 消费者入口点检测 — 根目录文件若导入多个锚点模块，视为入口点锚点
            anchor_file_ids: set[str] = set()
            for a in anchors:
                if a.node.file_path:
                    anchor_file_ids.add(f"file:{a.node.file_path}")
            for did in dep_files:
                dn = self.graph.get_node(did)
                if dn and dn.file_path:
                    anchor_file_ids.add(f"file:{dn.file_path}")

            from core.prune.anchor import AnchorResult
            for nid, node in list(self.graph.nodes.items()):
                if (node.node_type == NodeType.FILE
                        and node.file_path
                        and node.file_path.parent == Path('.')
                        and nid not in anchor_file_ids):
                    if analysis and analysis.out_of_scope:
                        if locator._in_excluded_scope(node, analysis.out_of_scope):
                            continue
                    builder.resolve_file(node.file_path)
                    imports = self.graph.get_outgoing(nid, EdgeType.IMPORTS)
                    anchor_hits = sum(1 for e in imports if e.target in anchor_file_ids)
                    if anchor_hits >= 2:
                        funcs = locator._get_descendant_functions(node)
                        for func in funcs:
                            anchors.append(AnchorResult(
                                node_id=func.id, node=func,
                                relevance_score=0.5, confidence=0.6,
                                reason=f"(消费者入口点 {node.file_path})",
                            ))
                        logger.info(
                            f"消费者入口点: {node.file_path} "
                            f"({anchor_hits} 个锚点模块导入)"
                        )

            # lazy 展开后语义增强新产生的节点
            if self.config.graph.enable_semantic:
                new_nodes = [
                    nid for nid in self.graph.nodes
                    if not self.graph.nodes[nid].is_semantic_ready
                ]
                if new_nodes:
                    enricher = SemanticEnricher(self.config, self.llm, self.graph)
                    enricher.enrich(new_nodes)
                    logger.info(f"Lazy 语义增强: {len(new_nodes)} 个新节点")

            # 重新降级 FILE/CLASS 锚点到 FUNCTION（现在 children 已展开）
            anchors = locator._downgrade_coarse_anchors(
                anchors, closure_query_embedding or query_embedding,
            )
            anchor_output.anchors = anchors
            anchor_output.diagnostics.setdefault("lazy_resolution", {})
            anchor_output.diagnostics["lazy_resolution"].update({
                "node_count_before": pre_count,
                "node_count_after": len(self.graph.nodes),
                "anchor_count_before_redowngrade": len(anchor_ids),
                "anchor_count_after_redowngrade": len(anchors),
                "downgrade": dict(locator._last_downgrade_stats),
            })
            anchor_output.diagnostics["final_anchor_count"] = len(anchors)
            logger.info(
                f"Lazy resolution: {pre_count} → {len(self.graph.nodes)} 节点, "
                f"降级后锚点 {len(anchors)} 个"
            )

        # Step 2.2: 闭包求解（v2: 语义定界 + 结构补全 + 缺口仲裁）
        solver = ClosureSolver(self.config, self.llm, self.graph)
        closure = solver.solve(
            anchors, self.config.user_instruction,
            query_embedding, closure_query_embedding,
        )
        self._last_closure = closure
        logger.info(
            f"闭包结果: required={len(closure.required_nodes)}, "
            f"stub={len(closure.stub_nodes)}, "
            f"缺口={len(closure.structural_gaps)}"
        )
        self._write_selection_diagnostics(analysis, anchor_output, closure)

        # Step 2.3: AST 手术
        # 清理 output 中上次运行残留的代码文件（保留 .codeprune_* 元数据）
        self._clean_output_code_files()
        surgeon = Surgeon(self.config, self.graph)
        sub_repo_path = surgeon.extract(closure)

        # 补写 auto_paired_files 到诊断（surgeon 运行后才有数据）
        if surgeon.auto_paired_files and self.config.prune.enable_selection_diagnostics:
            diag_path = self._artifact_path("selection_diagnostics.json")
            if diag_path.exists():
                try:
                    diag = json.loads(diag_path.read_text(encoding="utf-8"))
                    diag["auto_paired_files"] = surgeon.auto_paired_files
                    diag_path.write_text(
                        json.dumps(self._json_safe(diag), ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                except (json.JSONDecodeError, OSError):
                    pass

        # Step 2.4: 语言级 fixup
        for lang in set(n.language for n in self.graph.nodes.values() if n.language):
            rules = get_language_rules(lang)
            if rules:
                warnings = rules.post_surgery_fixup(sub_repo_path)
                for w in warnings:
                    logger.warning(f"[fixup] {w.file_path}: {w.message}")

        logger.info(f"Phase2 完成, 耗时 {time.time() - start:.1f}s")
        return sub_repo_path

    def _phase3_code_heal(self, sub_repo_path: Path) -> None:
        """Phase3: 自愈"""
        logger.info("──── Phase3: CodeHeal ────")
        start = time.time()

        engine = HealEngine(self.config, self.llm, self.graph)
        success = engine.heal(sub_repo_path)

        status = "通过" if success else "部分通过（需人工检查）"
        logger.info(
            f"Phase3 完成: {status}, "
            f"修复历史 {len(engine._fix_history)} 次, "
            f"耗时 {time.time() - start:.1f}s"
        )

        # Phase3+: Finalize — 生成子仓库的 requirements + README
        if self.config.heal.enable_finalize:
            self._finalize(sub_repo_path)

    def _finalize(self, sub_repo_path: Path) -> None:
        """Phase3+: 生成子仓库产物 (requirements + README)"""
        from core.heal.finalize import SubRepoFinalizer

        logger.info("──── Finalize: 产物生成 ────")
        finalizer = SubRepoFinalizer(
            self.config, self.llm, self.graph,
            closure=self._last_closure,
        )
        artifacts = finalizer.finalize(sub_repo_path)
        generated = [k for k, v in artifacts.items() if v is not None]
        logger.info(f"产物生成完成: {', '.join(generated)}")

    def _clean_output_code_files(self) -> None:
        """清理 output 目录中上次运行残留的代码文件，保留 .codeprune_* 元数据目录。"""
        import shutil
        out = self.config.output_path
        if not out.exists():
            return
        for child in list(out.iterdir()):
            if child.name.startswith(".codeprune"):
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        logger.debug(f"已清理 output 目录残留文件: {out}")

    def _write_selection_diagnostics(self, analysis, anchor_output, closure) -> None:
        """输出 Phase2 的选择诊断，便于稳定性观察和回归比较。"""
        if not self.config.prune.enable_selection_diagnostics:
            return

        path = self._artifact_path("selection_diagnostics.json")
        payload = {
            "repo_path": str(self.config.repo_path),
            "output_path": str(self.config.output_path),
            "user_instruction": self.config.user_instruction,
            "analysis": None if analysis is None else {
                "anchor_strategy": analysis.anchor_strategy,
                "out_of_scope": analysis.out_of_scope,
                "sub_features": [
                    {
                        "req_id": sf.req_id,
                        "description": sf.description,
                        "root_entities": sf.root_entities,
                    }
                    for sf in analysis.sub_features
                ],
            },
            "anchor": {
                **anchor_output.diagnostics,
                "anchor_type_counts": self._count_node_types(
                    a.node_id for a in anchor_output.anchors
                ),
                "anchors": [
                    {
                        "node_id": a.node_id,
                        "qualified_name": a.node.qualified_name,
                        "node_type": a.node.node_type.value,
                        "confidence": round(a.confidence, 4),
                        "relevance_score": round(a.relevance_score, 4),
                        "reason": a.reason,
                        "req_ids": list(a.req_ids),
                    }
                    for a in anchor_output.anchors
                ],
            },
            "closure": {
                **closure.diagnostics,
                "required_type_counts": self._count_node_types(closure.required_nodes),
                "stub_type_counts": self._count_node_types(closure.stub_nodes),
                "required_nodes": sorted(
                    self.graph.get_node(nid).qualified_name
                    for nid in closure.required_nodes
                    if self.graph.get_node(nid) is not None
                ),
                "stub_nodes": sorted(
                    self.graph.get_node(nid).qualified_name
                    for nid in closure.stub_nodes
                    if self.graph.get_node(nid) is not None
                ),
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self._json_safe(payload), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info(f"选择诊断已保存: {path}")

    @staticmethod
    def _json_safe(value):
        """递归转为 JSON 可序列化对象。"""
        if isinstance(value, dict):
            return {str(k): Pipeline._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [Pipeline._json_safe(v) for v in value]
        if isinstance(value, set):
            return [Pipeline._json_safe(v) for v in sorted(value)]
        if isinstance(value, Path):
            return str(value)
        return value

    def _count_node_types(self, node_ids) -> dict[str, int]:
        """统计一组节点的类型分布。"""
        counts: dict[str, int] = {}
        for node_id in node_ids:
            node = self.graph.get_node(node_id) if self.graph else None
            if node is None:
                continue
            node_type = node.node_type.value
            counts[node_type] = counts.get(node_type, 0) + 1
        return counts

    # ── 独立运行接口 ──

    def run_phase1(self) -> CodeGraph:
        """独立运行 Phase1: 构建图谱并保存到 artifacts 目录"""
        self._ensure_llm()
        self.graph = self._phase1_code_graph()
        graph_path = self._artifact_path(self.GRAPH_FILE)
        self.graph.save(graph_path)
        logger.info(f"图谱已保存: {graph_path}")
        return self.graph

    def run_phase2(self, graph_path: Path | None = None) -> Path:
        """独立运行 Phase2: 加载图谱 → 裁剪 → 输出子仓库"""
        self._ensure_llm()
        if self.graph is None:
            load_path = graph_path or self._artifact_path(self.GRAPH_FILE)
            logger.info(f"加载图谱: {load_path}")
            self.graph = CodeGraph.load(load_path)
        return self._phase2_code_prune()

    def run_phase3(self, sub_repo_path: Path, graph_path: Path | None = None) -> bool:
        """独立运行 Phase3: 加载图谱 → 自愈子仓库"""
        self._ensure_llm()
        if self.graph is None:
            load_path = graph_path or self._artifact_path(self.GRAPH_FILE)
            logger.info(f"加载图谱: {load_path}")
            self.graph = CodeGraph.load(load_path)
        self._phase3_code_heal(sub_repo_path)
