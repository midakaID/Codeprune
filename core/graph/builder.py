"""
Phase1: CodeGraph — 物理层图谱构建
通过 tree-sitter 解析源代码，构建文件/类/函数节点和依赖边
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from config import CodePruneConfig
from core.graph.schema import (
    ByteRange, CodeGraph, CodeNode, Edge, EdgeType, Language, NodeType,
)

logger = logging.getLogger(__name__)


class GraphBuilder:
    """物理层图谱构建器"""

    def __init__(self, config: CodePruneConfig):
        self.config = config
        self.graph = CodeGraph(repo_root=config.repo_path)
        self._file_count = 0
        self._skip_count = 0

    def build(self) -> CodeGraph:
        """构建完整物理图谱"""
        logger.info(f"开始构建物理图谱: {self.config.repo_path}")

        # Step 1: 遍历文件系统，创建目录和文件节点
        self._scan_filesystem()

        # Step 2: 对每个代码文件进行 AST 解析，提取符号和依赖
        if self.config.graph.initial_granularity in ("class", "function"):
            self._parse_all_files()
        # lazy_resolution 模式下，跳过全量细粒度解析

        logger.info(f"物理图谱构建完成: {self.graph.stats}")
        return self.graph

    def resolve_file(self, file_path: Path) -> None:
        """对单个文件做细粒度解析（lazy resolution 用）
        file_path 可以是相对路径或绝对路径
        """
        if file_path.is_absolute():
            full_path = file_path
        else:
            full_path = self.config.repo_path / file_path
        lang = Language.from_extension(full_path.suffix)
        if lang == Language.UNKNOWN:
            return
        if full_path.exists():
            self._parse_file(full_path, lang)

    def resolve_region(self, node_ids: list[str]) -> None:
        """对指定节点集合所在的文件做细粒度展开（Phase2 锚定后调用）"""
        files_to_resolve = set()
        for nid in node_ids:
            node = self.graph.get_node(nid)
            if node and node.file_path:
                files_to_resolve.add(node.file_path)
        for fp in files_to_resolve:
            self.resolve_file(fp)

    # ── 内部方法 ──

    def _scan_filesystem(self) -> None:
        """遍历文件系统构建目录/文件节点"""
        from fnmatch import fnmatch

        root = self.config.repo_path
        ignore = self.config.graph.ignore_patterns
        max_size_bytes = self.config.graph.max_file_size_kb * 1024

        # 仓库根节点
        root_node = CodeNode(
            id="repo:root",
            node_type=NodeType.REPOSITORY,
            name=root.name,
            qualified_name=root.name,
            file_path=Path("."),
        )
        self.graph.add_node(root_node)

        for item in sorted(root.rglob("*")):
            rel = item.relative_to(root)

            # 忽略检查
            if any(fnmatch(str(rel), pat) or any(fnmatch(part, pat) for part in rel.parts) for pat in ignore):
                continue

            if item.is_dir():
                dir_node = CodeNode(
                    id=f"dir:{rel}",
                    node_type=NodeType.DIRECTORY,
                    name=item.name,
                    qualified_name=str(rel),
                    file_path=rel,
                )
                self.graph.add_node(dir_node)
                # 目录包含边
                parent_id = f"dir:{rel.parent}" if str(rel.parent) != "." else "repo:root"
                self.graph.add_edge(Edge(source=parent_id, target=dir_node.id, edge_type=EdgeType.CONTAINS))

            elif item.is_file():
                lang = Language.from_extension(item.suffix)
                if lang == Language.UNKNOWN:
                    continue
                if item.stat().st_size > max_size_bytes:
                    self._skip_count += 1
                    logger.debug(f"跳过大文件: {rel}")
                    continue

                file_node = CodeNode(
                    id=f"file:{rel}",
                    node_type=NodeType.FILE,
                    name=item.name,
                    qualified_name=str(rel),
                    file_path=rel,
                    language=lang,
                )
                self.graph.add_node(file_node)
                parent_id = f"dir:{rel.parent}" if str(rel.parent) != "." else "repo:root"
                self.graph.add_edge(Edge(source=parent_id, target=file_node.id, edge_type=EdgeType.CONTAINS))
                self._file_count += 1

        logger.info(f"文件扫描完成: {self._file_count} 个代码文件, 跳过 {self._skip_count} 个")

    def _parse_all_files(self) -> None:
        """全量 AST 解析（非 lazy 模式）"""
        for node in self.graph.file_nodes:
            full_path = self.config.repo_path / node.file_path
            self._parse_file(full_path, node.language)

    def _parse_file(self, file_path: Path, language: Language) -> None:
        """AST 解析单个文件，提取符号节点和依赖边"""
        from parsers.treesitter_adapter import TreeSitterAdapter
        from parsers.import_resolver import create_import_resolver

        rel = file_path.relative_to(self.config.repo_path)
        try:
            source = file_path.read_bytes()
        except OSError as e:
            logger.warning(f"无法读取 {rel}: {e}")
            return

        adapter = TreeSitterAdapter(language)
        symbols = adapter.extract_symbols(source, rel)

        file_node_id = f"file:{rel}"
        for sym in symbols:
            self.graph.add_node(sym.node)
            # 文件→符号包含边
            parent_id = sym.parent_id or file_node_id
            self.graph.add_edge(Edge(source=parent_id, target=sym.node.id, edge_type=EdgeType.CONTAINS))
            # 更新父节点的 children 列表
            parent_node = self.graph.get_node(parent_id)
            if parent_node and sym.node.id not in parent_node.children:
                parent_node.children.append(sym.node.id)

        # 提取依赖边 (import/call/inherit)
        deps = adapter.extract_dependencies(source, rel)
        resolver = create_import_resolver(language, self.config.repo_path)

        # Python 特化: __all__ 和动态 import
        if language == Language.PYTHON:
            dunder_all = adapter.extract_dunder_all(source)
            if dunder_all is not None:
                file_node = self.graph.get_node(file_node_id)
                if file_node:
                    file_node.metadata["__all__"] = dunder_all
            dynamic_imports = adapter.extract_dynamic_imports(source, rel)
            deps.extend(dynamic_imports)

        for dep in deps:
            resolved = self._resolve_edge_target(dep, rel, resolver)
            if resolved:
                self.graph.add_edge(resolved)

        # Java 同包隐式引用检测：同包类无需 import 即可使用
        if language == Language.JAVA:
            self._detect_java_same_package_refs(file_node_id, rel, source)

    def _detect_java_same_package_refs(
        self, file_node_id: str, rel: Path, source: bytes
    ) -> None:
        """检测 Java 同包文件间的隐式引用，生成 IMPORTS 边。

        Java 同包类无需 import 语句即可直接使用，但 import_resolver 只能发现
        显式 import。此方法扫描同目录 .java 文件的类名是否出现在源码中，
        发现即创建 IMPORTS 边以补全依赖图。
        """
        import re as _re

        pkg_dir = rel.parent
        source_text = source.decode("utf-8", errors="replace")
        self_stem = rel.stem

        for nid, node in self.graph.nodes.items():
            if not nid.startswith("file:"):
                continue
            if nid == file_node_id:
                continue
            fp = node.file_path
            if fp is None or fp.parent != pkg_dir:
                continue
            if fp.suffix != ".java":
                continue

            class_name = fp.stem  # e.g. "ProductService"
            if class_name == self_stem:
                continue

            # 检查源码中是否引用了该类名（单词边界匹配）
            if _re.search(rf"\b{_re.escape(class_name)}\b", source_text):
                # 避免重复边
                existing = self.graph.get_outgoing(file_node_id, EdgeType.IMPORTS)
                if any(e.target == nid for e in existing):
                    continue
                self.graph.add_edge(Edge(
                    source=file_node_id,
                    target=nid,
                    edge_type=EdgeType.IMPORTS,
                    metadata={"same_package": True},
                ))
                logger.debug(f"Java 同包引用: {rel} → {fp}")

    def _resolve_edge_target(self, edge: Edge, source_file: Path,
                             resolver) -> Optional[Edge]:
        """将原始依赖边的目标解析为图谱中实际存在的节点"""
        target = edge.target

        if edge.edge_type == EdgeType.IMPORTS:
            # module:xxx → 解析为实际文件
            module_path = target.removeprefix("module:")
            resolved_path = resolver.resolve(module_path, source_file)
            if resolved_path is None:
                return None  # 外部依赖，忽略
            target_id = f"file:{resolved_path}"
            if target_id in self.graph.nodes:
                return Edge(source=edge.source, target=target_id,
                            edge_type=EdgeType.IMPORTS, metadata=edge.metadata)
            return None

        elif edge.edge_type == EdgeType.CALLS:
            # C1: 类限定调用解析
            call_name = target.removeprefix("call:")

            if "." in call_name:
                qualifier, method = call_name.rsplit(".", 1)

                if qualifier == "?":
                    # self.method() 但不知道封闭类 → 限定在同文件
                    candidates = [n for n in self.graph.nodes.values()
                                  if n.name == method and n.node_type == NodeType.FUNCTION
                                  and n.file_path == source_file]
                    if not candidates:
                        candidates = self._global_name_search(method)
                    confidence = 0.85 if len(candidates) == 1 else 0.5

                elif qualifier == "super":
                    # 父类调用 → 沿继承链匹配方法
                    candidates = self._resolve_super_call(method, source_file)
                    confidence = 0.8 if len(candidates) == 1 else 0.5

                else:
                    # 有限定符 → 优先匹配「父节点名 == qualifier」的方法
                    candidates = [
                        n for n in self.graph.nodes.values()
                        if n.name == method and n.node_type == NodeType.FUNCTION
                        and self._get_parent_name(n) == qualifier
                    ]
                    if not candidates:
                        # 退化：qualifier 可能是变量名不是类名
                        candidates = self._global_name_search(method)
                    confidence = 0.95 if len(candidates) == 1 else 0.6
            else:
                # 裸调用 → 同文件优先
                same_file = [n for n in self.graph.nodes.values()
                             if n.name == call_name and n.node_type == NodeType.FUNCTION
                             and n.file_path == source_file]
                if same_file:
                    candidates = same_file
                    confidence = 0.9
                else:
                    candidates = self._global_name_search(call_name)
                    confidence = 0.85 if len(candidates) == 1 else 0.5

            # 多候选裁剪：超过 3 个同名 → 按距离只取最近的
            if len(candidates) > 3:
                candidates.sort(
                    key=lambda c: self._file_distance(source_file, c.file_path)
                )
                candidates = candidates[:1]
                confidence = min(confidence, 0.6)

            for c in candidates:
                self.graph.add_edge(Edge(
                    source=edge.source, target=c.id,
                    edge_type=EdgeType.CALLS, confidence=confidence,
                ))
            return None  # 已在循环中添加

        elif edge.edge_type in (EdgeType.INHERITS, EdgeType.IMPLEMENTS):
            # class_ref:ClassName → 查找同名类
            ref_prefix = "class_ref:" if edge.edge_type == EdgeType.INHERITS else "interface_ref:"
            ref_name = target.removeprefix(ref_prefix)
            candidates = [n for n in self.graph.nodes.values()
                          if n.name == ref_name and n.node_type in (NodeType.CLASS, NodeType.INTERFACE)]
            if candidates:
                return Edge(source=edge.source, target=candidates[0].id,
                            edge_type=edge.edge_type)

        return None

    def _get_parent_name(self, node: CodeNode) -> Optional[str]:
        """获取节点所属类的名称"""
        incoming = self.graph.get_incoming(node.id, EdgeType.CONTAINS)
        if incoming:
            parent = self.graph.get_node(incoming[0].source)
            if parent and parent.node_type in (NodeType.CLASS, NodeType.INTERFACE):
                return parent.name
        return None

    def _file_distance(self, file_a: Path, file_b: Optional[Path]) -> int:
        """两个文件路径的距离（共同前缀越长距离越短）"""
        if file_b is None:
            return 999
        parts_a = file_a.parts
        parts_b = file_b.parts
        common = 0
        for a, b in zip(parts_a, parts_b):
            if a == b:
                common += 1
            else:
                break
        return len(parts_a) + len(parts_b) - 2 * common

    def _global_name_search(self, name: str) -> list[CodeNode]:
        """全局按名称搜索函数节点"""
        return [n for n in self.graph.nodes.values()
                if n.name == name and n.node_type == NodeType.FUNCTION]

    def _resolve_super_call(self, method: str, source_file: Path) -> list[CodeNode]:
        """
        沿继承链查找 super().method() 的目标。
        从同文件的类出发，找 INHERITS 边指向的父类，在父类子节点中匹配方法。
        """
        # 找同文件的类节点
        classes_in_file = [
            n for n in self.graph.nodes.values()
            if n.node_type in (NodeType.CLASS, NodeType.INTERFACE)
            and n.file_path == source_file
        ]

        candidates: list[CodeNode] = []
        visited: set[str] = set()

        for cls in classes_in_file:
            # 沿继承链向上查找（最多 5 层防止循环）
            queue = [cls.id]
            for _ in range(5):
                if not queue:
                    break
                next_queue: list[str] = []
                for cid in queue:
                    if cid in visited:
                        continue
                    visited.add(cid)
                    for edge in self.graph.get_outgoing(cid):
                        if edge.edge_type in (EdgeType.INHERITS, EdgeType.IMPLEMENTS):
                            parent = self.graph.get_node(edge.target)
                            if parent:
                                # 在父类子节点中找目标方法
                                for child_id in parent.children:
                                    child = self.graph.get_node(child_id)
                                    if child and child.name == method and child.node_type == NodeType.FUNCTION:
                                        candidates.append(child)
                                next_queue.append(edge.target)
                queue = next_queue
                if candidates:
                    break  # 找到最近父类的匹配即停

        # 未找到 → 退化为全局搜索
        if not candidates:
            candidates = self._global_name_search(method)

        return candidates
