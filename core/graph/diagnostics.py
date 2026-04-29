"""
Phase1 诊断工具：Embedding 质量评估
离线检测 embedding 索引的区分力，辅助调优参数
"""

from __future__ import annotations

import logging
import random
from typing import Optional

import numpy as np

from core.graph.schema import CodeGraph, CodeNode, NodeType

logger = logging.getLogger(__name__)


def _cosine_sim(a: list[float] | None, b: list[float] | None) -> float:
    """两个向量的余弦相似度"""
    if a is None or b is None:
        return 0.0
    va, vb = np.array(a), np.array(b)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    if denom < 1e-9:
        return 0.0
    return float(np.dot(va, vb) / denom)


def _get_directory(node: CodeNode) -> Optional[str]:
    """获取节点所在目录（取 file_path 的父级）"""
    if node.file_path is None:
        return None
    parts = str(node.file_path).replace("\\", "/").rsplit("/", 1)
    return parts[0] if len(parts) > 1 else ""


def diagnose_embedding_quality(
    graph: CodeGraph,
    sample_size: int = 200,
    seed: int = 42,
) -> dict:
    """
    采样检测 embedding 区分力：
    - 同目录函数 pair 的平均 cosine（intra）
    - 跨目录函数 pair 的平均 cosine（inter）
    - gap = intra - inter > 0.05 为合格

    返回 {"intra": float, "inter": float, "gap": float, "pass": bool,
           "total_embedded": int, "sampled_pairs": int}
    """
    # 收集有 embedding 的函数节点，按目录分组
    by_dir: dict[str, list[CodeNode]] = {}
    for node in graph.nodes.values():
        if node.node_type != NodeType.FUNCTION or node.embedding is None:
            continue
        d = _get_directory(node) or "__root__"
        by_dir.setdefault(d, []).append(node)

    total_embedded = sum(len(v) for v in by_dir.values())
    if total_embedded < 4:
        logger.warning(f"嵌入节点过少 ({total_embedded})，跳过诊断")
        return {
            "intra": 0.0, "inter": 0.0, "gap": 0.0, "pass": False,
            "total_embedded": total_embedded, "sampled_pairs": 0,
        }

    rng = random.Random(seed)

    # ── 采样同目录 pairs ──
    intra_sims: list[float] = []
    for nodes in by_dir.values():
        if len(nodes) < 2:
            continue
        pairs = min(sample_size // max(len(by_dir), 1), len(nodes) * (len(nodes) - 1) // 2)
        pairs = max(pairs, 1)
        for _ in range(pairs):
            a, b = rng.sample(nodes, 2)
            intra_sims.append(_cosine_sim(a.embedding, b.embedding))

    # ── 采样跨目录 pairs ──
    inter_sims: list[float] = []
    dir_keys = [k for k, v in by_dir.items() if v]
    if len(dir_keys) >= 2:
        target_pairs = max(len(intra_sims), sample_size)
        for _ in range(target_pairs):
            d1, d2 = rng.sample(dir_keys, 2)
            a = rng.choice(by_dir[d1])
            b = rng.choice(by_dir[d2])
            inter_sims.append(_cosine_sim(a.embedding, b.embedding))

    intra = float(np.mean(intra_sims)) if intra_sims else 0.0
    inter = float(np.mean(inter_sims)) if inter_sims else 0.0
    gap = intra - inter

    result = {
        "intra": round(intra, 4),
        "inter": round(inter, 4),
        "gap": round(gap, 4),
        "pass": gap > 0.05,
        "total_embedded": total_embedded,
        "sampled_pairs": len(intra_sims) + len(inter_sims),
    }

    status = "PASS" if result["pass"] else "FAIL"
    logger.info(
        f"Embedding 诊断 [{status}]: "
        f"intra={result['intra']:.4f}, inter={result['inter']:.4f}, "
        f"gap={result['gap']:.4f} (阈值 0.05), "
        f"节点={total_embedded}, 采样对={result['sampled_pairs']}"
    )
    return result
