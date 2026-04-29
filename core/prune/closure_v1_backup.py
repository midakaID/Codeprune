"""
Phase2: CodePrune — 最小闭包求解
硬依赖传递闭包 + 软依赖 LLM 裁决
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

from config import CodePruneConfig
from core.graph.schema import CodeGraph, CodeNode, Edge, EdgeType, NodeType
from core.llm.provider import LLMProvider
from core.llm.prompts import Prompts
from core.prune.anchor import AnchorResult

logger = logging.getLogger(__name__)


@dataclass
class ClosureResult:
    """闭包求解结果"""
    required_nodes: set[str] = field(default_factory=set)
    soft_included: set[str] = field(default_factory=set)     # 经 LLM 决策纳入的软依赖
    soft_excluded: set[str] = field(default_factory=set)     # 经 LLM 决策排除的软依赖


class ClosureSolver:
    """最小可运行闭包求解器"""

    def __init__(self, config: CodePruneConfig, llm: LLMProvider, graph: CodeGraph):
        self.config = config
        self.llm = llm
        self.graph = graph

    def solve(self, anchors: list[AnchorResult], user_instruction: str) -> ClosureResult:
        """
        求解最小闭包:
        1. 从锚点出发，传递求解所有硬依赖
        2. 收集过程中遇到的软依赖候选
        3. LLM 决策软依赖是否纳入
        4. 纳入的软依赖再递归求解硬依赖
        """
        result = ClosureResult()
        soft_candidates: set[str] = set()
        max_depth = self.config.prune.max_closure_depth

        anchor_ids = {a.node_id for a in anchors}
        result.required_nodes.update(anchor_ids)

        # Step 1: 硬依赖传递闭包
        queue: deque[tuple[str, int]] = deque((nid, 0) for nid in anchor_ids)
        while queue:
            node_id, depth = queue.popleft()
            if depth >= max_depth:
                logger.warning(f"闭包深度达到上限 {max_depth}，停止传递: {node_id}")
                continue

            # 硬依赖
            for edge in self.graph.get_hard_dependencies(node_id):
                target = edge.target
                # ── import 边符号级传播 ──
                # 当 IMPORTS 边指向文件节点时，不盲目拉入整个文件，
                # 而是只拉入当前闭包中实际引用（CALLS/INHERITS/USES）的该文件内的符号
                if edge.edge_type == EdgeType.IMPORTS:
                    # TypeScript type-only import → 降级为软依赖
                    if edge.metadata.get("type_only"):
                        if target not in result.required_nodes and target not in soft_candidates:
                            soft_candidates.add(target)
                        continue
                    target_node = self.graph.get_node(target)
                    if target_node and target_node.node_type == NodeType.FILE:
                        self._import_symbol_level(
                            target, result, queue, depth, edge,
                        )
                        continue

                if target not in result.required_nodes:
                    result.required_nodes.add(target)
                    queue.append((target, depth + 1))

            # 收集软依赖候选
            for edge in self.graph.get_soft_dependencies(node_id):
                if edge.target not in result.required_nodes and edge.target not in soft_candidates:
                    soft_candidates.add(edge.target)

        logger.info(f"硬依赖闭包: {len(result.required_nodes)} 个节点, 软依赖候选: {len(soft_candidates)} 个")

        # Step 2: LLM 决策软依赖（批量处理）
        if soft_candidates:
            selected_summaries = self._build_selected_summaries(result.required_nodes)
            candidate_nodes = []
            for cid in soft_candidates:
                node = self.graph.get_node(cid)
                if node and node.summary:
                    candidate_nodes.append((cid, node))

            # 分批判断，每批最多 5 个
            batch_size = 5
            for bi in range(0, len(candidate_nodes), batch_size):
                batch = candidate_nodes[bi:bi + batch_size]
                if len(batch) == 1:
                    cid, node = batch[0]
                    include = self._judge_soft_dep(user_instruction, node, selected_summaries)
                    if include:
                        result.soft_included.add(cid)
                        result.required_nodes.add(cid)
                        self._expand_hard_deps(cid, result, max_depth)
                    else:
                        result.soft_excluded.add(cid)
                else:
                    decisions = self._judge_soft_deps_batch(
                        user_instruction, batch, selected_summaries,
                    )
                    for (cid, node), include in zip(batch, decisions):
                        if include:
                            result.soft_included.add(cid)
                            result.required_nodes.add(cid)
                            self._expand_hard_deps(cid, result, max_depth)
                        else:
                            result.soft_excluded.add(cid)

        # Step 3: 确保包含链完整 — 如果选了函数，其所属的类和文件也要包含
        self._ensure_containment_chain(result)

        # Step 3.5: 粒度升级 — 如果一个类的所有子方法都已在闭包中，标记为整类选中
        self._upgrade_full_classes(result)

        # Step 4: CLASS/INTERFACE 自动展开 — 如果整个类被选中，展开其所有方法
        self._expand_class_children(result)

        # Step 5: 闭包过大预警
        total_nodes = len(self.graph.nodes)
        closure_size = len(result.required_nodes)
        if total_nodes > 0:
            ratio = closure_size / total_nodes
            if ratio > 0.7:
                logger.warning(
                    f"⚠ 闭包过大: {closure_size}/{total_nodes} 个节点 ({ratio:.0%})，"
                    f"裁剪效果不佳，建议缩小用户指令范围"
                )

        logger.info(
            f"闭包求解完成: {len(result.required_nodes)} 个节点 "
            f"(软依赖纳入 {len(result.soft_included)}, 排除 {len(result.soft_excluded)})"
        )
        return result

    def _expand_hard_deps(self, node_id: str, result: ClosureResult, max_depth: int) -> None:
        """递归展开硬依赖（复用 import 符号级传播逻辑）"""
        queue: deque[tuple[str, int]] = deque([(node_id, 0)])
        while queue:
            nid, depth = queue.popleft()
            if depth >= max_depth:
                break
            for edge in self.graph.get_hard_dependencies(nid):
                target = edge.target
                # import 边符号级传播
                if edge.edge_type == EdgeType.IMPORTS:
                    target_node = self.graph.get_node(target)
                    if target_node and target_node.node_type == NodeType.FILE:
                        self._import_symbol_level(target, result, queue, depth, edge)
                        continue
                if target not in result.required_nodes:
                    result.required_nodes.add(target)
                    queue.append((target, depth + 1))

    def _import_symbol_level(
        self, file_node_id: str, result: ClosureResult,
        queue: deque, depth: int,
        import_edge: Edge | None = None,
    ) -> None:
        """
        import 边符号级传播：
        1. 优先利用 edge.metadata['imported_symbols'] 精确匹配
        2. 回退为检查闭包中已有节点的 CALLS/INHERITS/USES 引用
        3. 都找不到时保守拉入整个文件
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

        # 策略 0: 如果是 "from X import *"，利用目标文件的 __all__ 限制范围
        if (import_edge and import_edge.metadata.get("imported_symbols")
                and "*" in import_edge.metadata["imported_symbols"]):
            dunder_all = file_node.metadata.get("__all__")
            if dunder_all:
                # __all__ 定义了导出 → 只拉入 __all__ 中的符号
                exported_names = set(dunder_all)
                for cid, child in file_children.items():
                    if child.name in exported_names:
                        referenced.add(cid)
                if referenced:
                    for sym_id in referenced:
                        if sym_id not in result.required_nodes:
                            result.required_nodes.add(sym_id)
                            queue.append((sym_id, depth + 1))
                    logger.debug(
                        f"import * + __all__ 传播: {file_node.name} → "
                        f"拉入 {len(referenced)}/{len(file_children)} 个符号"
                    )
                    return
                # __all__ 中的名称在 file_children 中找不到 → 走正常流程

        # 策略 1: 利用 import 边元数据中的 imported_symbols
        if import_edge and import_edge.metadata.get("imported_symbols"):
            imported_names = set(import_edge.metadata["imported_symbols"])
            imported_names.discard("*")  # 去掉通配符
            for cid, child in file_children.items():
                if child.name in imported_names:
                    referenced.add(cid)

        # 策略 2: 回退 — 从闭包已有节点的 CALLS/INHERITS/USES 边查找
        if not referenced:
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
                    queue.append((sym_id, depth + 1))
            logger.debug(
                f"import 符号级传播: {file_node.name} → "
                f"仅拉入 {len(referenced)}/{len(file_children)} 个符号"
            )
        else:
            # 策略 3: 保守回退 — 拉入整个文件
            if file_node_id not in result.required_nodes:
                result.required_nodes.add(file_node_id)
                queue.append((file_node_id, depth + 1))
            logger.debug(f"import 回退: 整个文件 {file_node.name}")

    def _ensure_containment_chain(self, result: ClosureResult) -> None:
        """确保包含链完整：选中的符号 → 其所属类 → 文件 → 目录"""
        to_add = set()
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

        # Python __init__.py 自动包含：
        # 如果选中了某个 Python 目录下的文件，自动包含该目录的 __init__.py
        self._auto_include_init_py(result)

    def _auto_include_init_py(self, result: ClosureResult) -> None:
        """如果闭包包含某个包目录下的 .py 文件，自动包含该目录的 __init__.py"""
        from pathlib import Path as _Path

        # 收集闭包中所有 Python 文件所在的目录
        py_dirs: set[_Path] = set()
        for nid in list(result.required_nodes):
            node = self.graph.get_node(nid)
            if (node and node.node_type == NodeType.FILE
                    and node.file_path and str(node.file_path).endswith(".py")):
                parent_dir = _Path(node.file_path).parent
                if parent_dir != _Path("."):
                    py_dirs.add(parent_dir)

        if not py_dirs:
            return

        # 在图谱中查找这些目录下的 __init__.py 文件节点
        for dir_path in py_dirs:
            init_path = dir_path / "__init__.py"
            for node in self.graph.nodes.values():
                if (node.node_type == NodeType.FILE
                        and node.file_path == init_path
                        and node.id not in result.required_nodes):
                    result.required_nodes.add(node.id)
                    logger.debug(f"自动包含 __init__.py: {init_path}")
                    break

    def _expand_class_children(self, result: ClosureResult) -> None:
        """如果闭包包含 CLASS/INTERFACE 节点，自动展开其所有子方法/字段"""
        to_add: set[str] = set()
        for nid in list(result.required_nodes):
            node = self.graph.get_node(nid)
            if node and node.node_type in (NodeType.CLASS, NodeType.INTERFACE):
                for child_id in node.children:
                    if child_id not in result.required_nodes:
                        to_add.add(child_id)
        if to_add:
            result.required_nodes.update(to_add)
            logger.debug(f"类自动展开: +{len(to_add)} 个子节点")

    def _upgrade_full_classes(self, result: ClosureResult) -> None:
        """
        粒度升级：检测一个 CLASS 的所有 FUNCTION 子节点都在闭包中时，
        确保该 CLASS 节点也在闭包中，并在 metadata 标记"fullclass"
        供 surgeon 知道可以整类提取而不是逐函数拼接。
        """
        upgraded = 0
        for nid, node in self.graph.nodes.items():
            if node.node_type not in (NodeType.CLASS, NodeType.INTERFACE):
                continue
            if not node.children:
                continue
            # 检查是否所有子节点都在闭包中
            child_count = len(node.children)
            in_closure = sum(1 for c in node.children if c in result.required_nodes)
            if in_closure == child_count and child_count > 0:
                if nid not in result.required_nodes:
                    result.required_nodes.add(nid)
                node.metadata["fullclass"] = True
                upgraded += 1
        if upgraded:
            logger.info(f"粒度升级: {upgraded} 个类的所有子方法均在闭包中，标记为整类提取")

    def _judge_soft_dep(self, user_instruction: str, node: CodeNode, selected_summaries: str) -> bool:
        """LLM 判断软依赖是否应该纳入"""
        prompt = Prompts.JUDGE_SOFT_DEPENDENCY.format(
            user_instruction=user_instruction,
            name=node.qualified_name,
            node_type=node.node_type.value,
            summary=node.summary,
            file_path=node.file_path or "N/A",
            selected_summaries=selected_summaries,
        )
        try:
            result = self.llm.chat_json([{"role": "user", "content": prompt}])
            return result.get("include", False)
        except Exception as e:
            logger.warning(f"软依赖判断失败 [{node.id}]: {e}")
            return False

    def _judge_soft_deps_batch(
        self, user_instruction: str,
        candidates: list[tuple[str, CodeNode]],
        selected_summaries: str,
    ) -> list[bool]:
        """批量 LLM 判断多个软依赖 — 减少 API 调用次数"""
        entities_text = "\n".join(
            f"{i+1}. {node.qualified_name} ({node.node_type.value}) "
            f"in {node.file_path or 'N/A'}: {node.summary}"
            for i, (_, node) in enumerate(candidates)
        )
        prompt = (
            f'You are deciding which code entities should be included in a pruned sub-repository.\n\n'
            f'User\'s feature request: "{user_instruction}"\n\n'
            f'The following entities are soft dependencies of already-selected code:\n'
            f'{entities_text}\n\n'
            f'Already selected entities:\n{selected_summaries}\n\n'
            f'For each numbered entity, decide whether it should be included.\n'
            f'Respond in JSON: {{"decisions": [true/false, true/false, ...]}}\n'
            f'Array length MUST be {len(candidates)}.'
        )
        try:
            result = self.llm.chat_json([{"role": "user", "content": prompt}])
            decisions = result.get("decisions", [])
            if len(decisions) == len(candidates):
                return [bool(d) for d in decisions]
            # 长度不匹配 → 回退逐个判断
            logger.warning("批量判断返回长度不匹配，回退逐个判断")
        except Exception as e:
            logger.warning(f"批量软依赖判断失败: {e}")

        # 回退
        return [
            self._judge_soft_dep(user_instruction, node, selected_summaries)
            for _, node in candidates
        ]

    def _build_selected_summaries(self, node_ids: set[str]) -> str:
        """构建已选节点的摘要文本"""
        lines = []
        for nid in sorted(node_ids):
            node = self.graph.get_node(nid)
            if node and node.summary:
                lines.append(f"- {node.qualified_name}: {node.summary}")
        return "\n".join(lines[:50])  # 限制长度避免超窗口
