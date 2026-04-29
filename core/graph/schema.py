"""
CodeGraph 核心数据结构定义
物理层 + 语义层的统一图谱 schema
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


# ───────────────────── 枚举定义 ─────────────────────

class Language(Enum):
    """支持的代码语言"""
    C = "c"
    CPP = "cpp"
    JAVA = "java"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    PYTHON = "python"
    UNKNOWN = "unknown"

    @classmethod
    def from_extension(cls, ext: str) -> "Language":
        mapping = {
            ".c": cls.C, ".h": cls.C,
            ".cpp": cls.CPP, ".cc": cls.CPP, ".cxx": cls.CPP,
            ".hpp": cls.CPP, ".hh": cls.CPP, ".hxx": cls.CPP,
            ".java": cls.JAVA,
            ".js": cls.JAVASCRIPT, ".jsx": cls.JAVASCRIPT, ".mjs": cls.JAVASCRIPT,
            ".ts": cls.TYPESCRIPT, ".tsx": cls.TYPESCRIPT,
            ".py": cls.PYTHON, ".pyi": cls.PYTHON,
        }
        return mapping.get(ext.lower(), cls.UNKNOWN)


class NodeType(Enum):
    """代码节点类型 — 分层粒度"""
    REPOSITORY = "repository"     # 仓库根
    DIRECTORY = "directory"       # 目录
    FILE = "file"                 # 文件
    CLASS = "class"               # 类/结构体
    FUNCTION = "function"         # 函数/方法
    INTERFACE = "interface"       # 接口 (Java/TS)
    ENUM = "enum"                 # 枚举
    MODULE = "module"             # 模块 (Python module / JS module)
    NAMESPACE = "namespace"       # 命名空间 (C++/TS)


class EdgeType(Enum):
    """图谱边类型"""
    # ── 物理层（静态分析可靠提取）──
    CONTAINS = "contains"           # 层级包含 (dir→file, file→class, class→method)
    IMPORTS = "imports"             # import/include 依赖
    CALLS = "calls"                 # 函数调用
    INHERITS = "inherits"           # 类继承
    IMPLEMENTS = "implements"       # 接口实现
    USES = "uses"                   # 类型引用 (参数类型、返回类型、字段类型)

    # ── 语义层（LLM 推断）──
    SEMANTIC_RELATED = "semantic_related"   # 功能语义关联
    COOPERATES = "cooperates"              # 功能协作


class EdgeCategory(Enum):
    """边的硬/软分类 — 决定闭包求解行为"""
    HARD = "hard"   # 硬依赖：必须传递闭包
    SOFT = "soft"   # 软依赖：LLM 决策是否纳入


# 硬/软依赖映射
EDGE_CATEGORY: dict[EdgeType, EdgeCategory] = {
    EdgeType.CONTAINS: EdgeCategory.HARD,
    EdgeType.IMPORTS: EdgeCategory.HARD,
    EdgeType.CALLS: EdgeCategory.HARD,
    EdgeType.INHERITS: EdgeCategory.HARD,
    EdgeType.IMPLEMENTS: EdgeCategory.HARD,
    EdgeType.USES: EdgeCategory.HARD,
    EdgeType.SEMANTIC_RELATED: EdgeCategory.SOFT,
    EdgeType.COOPERATES: EdgeCategory.SOFT,
}


# ───────────────────── 节点 ─────────────────────

@dataclass
class ByteRange:
    """AST 节点在源文件中的字节范围，用于 Phase2 手术定位"""
    start_byte: int
    end_byte: int
    start_line: int      # 1-based
    start_col: int       # 0-based
    end_line: int
    end_col: int


@dataclass
class CodeNode:
    """代码图谱节点"""
    id: str                             # 全局唯一 ID (如 "file:src/main.py::class:BlogService")
    node_type: NodeType
    name: str                           # 短名
    qualified_name: str                 # 全限定名
    file_path: Optional[Path] = None    # 所在文件路径 (相对于仓库根)
    language: Language = Language.UNKNOWN
    byte_range: Optional[ByteRange] = None  # AST 定位
    children: list[str] = field(default_factory=list)   # 子节点 ID 列表

    # ── 语义层属性（Phase1 语义阶段填充）──
    summary: Optional[str] = None       # LLM 生成的功能摘要
    embedding: Optional[list[float]] = None  # 摘要向量
    signature: Optional[str] = None     # 函数/方法签名

    # ── 元数据 ──
    metadata: dict = field(default_factory=dict)

    @property
    def is_physical(self) -> bool:
        """是否为物理层节点（有 AST 位置信息）"""
        return self.byte_range is not None

    @property
    def is_semantic_ready(self) -> bool:
        """语义信息是否已填充"""
        return self.summary is not None


# ───────────────────── 边 ─────────────────────

@dataclass
class Edge:
    """图谱边"""
    source: str           # 源节点 ID
    target: str           # 目标节点 ID
    edge_type: EdgeType
    confidence: float = 1.0   # 语义边带置信度，物理边默认 1.0
    metadata: dict = field(default_factory=dict)

    @property
    def category(self) -> EdgeCategory:
        return EDGE_CATEGORY[self.edge_type]

    @property
    def is_hard(self) -> bool:
        return self.category == EdgeCategory.HARD

    @property
    def is_soft(self) -> bool:
        return self.category == EdgeCategory.SOFT


# ───────────────────── 图谱 ─────────────────────

@dataclass
class CodeGraph:
    """统一代码图谱 — Phase1 的核心产出"""
    repo_root: Path
    nodes: dict[str, CodeNode] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)

    # ── 索引（构建时填充，加速查询）──
    _outgoing: dict[str, list[Edge]] = field(default_factory=dict, repr=False)
    _incoming: dict[str, list[Edge]] = field(default_factory=dict, repr=False)

    def add_node(self, node: CodeNode) -> None:
        self.nodes[node.id] = node

    def add_edge(self, edge: Edge) -> None:
        self.edges.append(edge)
        self._outgoing.setdefault(edge.source, []).append(edge)
        self._incoming.setdefault(edge.target, []).append(edge)

    def get_node(self, node_id: str) -> Optional[CodeNode]:
        return self.nodes.get(node_id)

    def get_outgoing(self, node_id: str, edge_type: Optional[EdgeType] = None) -> list[Edge]:
        edges = self._outgoing.get(node_id, [])
        if edge_type:
            return [e for e in edges if e.edge_type == edge_type]
        return edges

    def get_incoming(self, node_id: str, edge_type: Optional[EdgeType] = None) -> list[Edge]:
        edges = self._incoming.get(node_id, [])
        if edge_type:
            return [e for e in edges if e.edge_type == edge_type]
        return edges

    def get_hard_dependencies(self, node_id: str) -> list[Edge]:
        """获取节点的所有硬依赖出边"""
        return [e for e in self.get_outgoing(node_id) if e.is_hard]

    def get_soft_dependencies(self, node_id: str) -> list[Edge]:
        """获取节点的所有软依赖出边"""
        return [e for e in self.get_outgoing(node_id) if e.is_soft]

    def get_nodes_by_type(self, node_type: NodeType) -> list[CodeNode]:
        return [n for n in self.nodes.values() if n.node_type == node_type]

    def get_nodes_by_file(self, file_path: Path) -> list[CodeNode]:
        return [n for n in self.nodes.values() if n.file_path == file_path]

    @property
    def file_nodes(self) -> list[CodeNode]:
        return self.get_nodes_by_type(NodeType.FILE)

    @property
    def stats(self) -> dict:
        from collections import Counter
        node_counts = Counter(n.node_type.value for n in self.nodes.values())
        edge_counts = Counter(e.edge_type.value for e in self.edges)
        return {"nodes": dict(node_counts), "edges": dict(edge_counts), "total_nodes": len(self.nodes), "total_edges": len(self.edges)}

    # ── 序列化 ──

    def save(self, path: Path) -> None:
        """序列化图谱到文件 (pickle)"""
        import pickle
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(self, f, protocol=pickle.HIGHEST_PROTOCOL)

    @classmethod
    def load(cls, path: Path) -> "CodeGraph":
        """从文件反序列化图谱"""
        import pickle
        with open(path, "rb") as f:
            graph = pickle.load(f)
        if not isinstance(graph, cls):
            raise TypeError(f"期望 CodeGraph，实际 {type(graph).__name__}")
        return graph
