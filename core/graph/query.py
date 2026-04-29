"""
图谱查询接口
提供 Phase2/3 使用的图谱查询方法
"""

from __future__ import annotations

import logging
import numpy as np
from typing import Optional

from core.graph.schema import CodeGraph, CodeNode, EdgeType, NodeType

logger = logging.getLogger(__name__)


class GraphQuery:
    """图谱查询工具"""

    def __init__(self, graph: CodeGraph):
        self.graph = graph
        self._embedding_index: Optional[np.ndarray] = None
        self._embedding_node_ids: list[str] = []

    def build_embedding_index(self) -> None:
        """构建向量检索索引"""
        nodes_with_emb = [(nid, n) for nid, n in self.graph.nodes.items() if n.embedding is not None]
        if not nodes_with_emb:
            logger.warning("没有可用的 embedding，无法构建索引")
            return
        self._embedding_node_ids = [nid for nid, _ in nodes_with_emb]
        self._embedding_index = np.array([n.embedding for _, n in nodes_with_emb], dtype=np.float32)
        # L2 归一化用于余弦相似度
        norms = np.linalg.norm(self._embedding_index, axis=1, keepdims=True)
        norms[norms == 0] = 1
        self._embedding_index = self._embedding_index / norms
        logger.info(f"Embedding 索引构建完成: {len(self._embedding_node_ids)} 个向量")

    def semantic_search(self, query_embedding: list[float], top_k: int = 20) -> list[tuple[str, float]]:
        """语义相似度检索，返回 (node_id, score) 列表"""
        if self._embedding_index is None:
            self.build_embedding_index()
        if self._embedding_index is None or len(self._embedding_index) == 0:
            return []
        q = np.array(query_embedding, dtype=np.float32)
        q = q / (np.linalg.norm(q) or 1)
        scores = self._embedding_index @ q
        top_indices = np.argsort(scores)[::-1][:top_k]
        return [(self._embedding_node_ids[i], float(scores[i])) for i in top_indices]

    def get_transitive_dependencies(self, node_id: str, hard_only: bool = True, max_depth: int = 50) -> set[str]:
        """获取传递依赖闭包"""
        visited = set()
        queue = [node_id]
        depth = 0
        while queue and depth < max_depth:
            next_queue = []
            for nid in queue:
                if nid in visited:
                    continue
                visited.add(nid)
                edges = self.graph.get_hard_dependencies(nid) if hard_only else self.graph.get_outgoing(nid)
                for e in edges:
                    if e.target not in visited:
                        next_queue.append(e.target)
            queue = next_queue
            depth += 1
        return visited

    def get_file_for_node(self, node_id: str) -> Optional[CodeNode]:
        """找到节点所属的文件节点"""
        node = self.graph.get_node(node_id)
        if not node:
            return None
        if node.node_type == NodeType.FILE:
            return node
        # 沿包含链向上找文件
        for edge in self.graph.get_incoming(node_id, EdgeType.CONTAINS):
            parent = self.graph.get_node(edge.source)
            if parent and parent.node_type == NodeType.FILE:
                return parent
            if parent:
                return self.get_file_for_node(parent.id)
        return None

    def get_all_symbols_in_file(self, file_path) -> list[CodeNode]:
        """获取文件中的所有符号节点"""
        file_nodes = [n for n in self.graph.nodes.values()
                      if n.file_path == file_path and n.node_type not in (NodeType.FILE, NodeType.DIRECTORY, NodeType.REPOSITORY)]
        return file_nodes
