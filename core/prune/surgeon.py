"""
Phase2: CodePrune — AST 手术
根据闭包结果，从原仓库提取代码到子仓库
支持整文件复制和部分符号提取
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from config import CodePruneConfig
from core.graph.schema import CodeGraph, CodeNode, EdgeType, Language, NodeType
from core.prune.closure import ClosureResult
from parsers.lang_rules.base import get_language_rules

logger = logging.getLogger(__name__)

# fallback：当 lang_rules 未覆盖时使用
_HEADER_SOURCE_PAIRS = {
    ".h": (".c", ".cpp", ".cc", ".cxx"),
    ".hpp": (".cpp", ".cc", ".cxx"),
    ".hh": (".cc", ".cpp", ".cxx"),
    ".hxx": (".cxx", ".cpp", ".cc"),
}


class Surgeon:
    """AST 手术器：根据闭包结果提取代码"""

    def __init__(self, config: CodePruneConfig, graph: CodeGraph):
        self.config = config
        self.graph = graph
        self.repo_root = config.repo_path
        self.output_root = config.output_path
        self.auto_paired_files: list[str] = []  # _pair_c_headers 自动补充的文件路径

    def extract(self, closure: ClosureResult) -> Path:
        """
        执行代码提取，生成子仓库
        返回子仓库根目录路径
        """
        logger.info(f"开始代码提取，目标: {self.output_root}")
        self.output_root.mkdir(parents=True, exist_ok=True)

        # 按文件分组闭包节点
        file_groups = self._group_by_file(closure)

        # F26: out_of_scope 最终防线 — 排除指令明确要求移除的文件
        excluded = self._get_out_of_scope()
        if excluded:
            before = len(file_groups)
            file_groups = {
                fp: nids for fp, nids in file_groups.items()
                if not self._is_excluded(str(fp).replace("\\", "/"), excluded)
            }
            dropped = before - len(file_groups)
            if dropped:
                logger.info(f"F26: out_of_scope 排除 {dropped} 个文件")

        full_copy = 0
        partial_extract = 0

        # F28: 获取方法级排除符号 / 受限类
        analysis = self.config.instruction_analysis
        excluded_sym_set = set(analysis.excluded_symbols) if analysis else set()
        restricted_classes = set(getattr(analysis, "restricted_classes", []) or []) if analysis else set()

        for file_path, node_ids in file_groups.items():
            file_node = self._get_file_node(file_path)
            if not file_node:
                continue

            all_symbols = self._get_all_symbols_in_file(file_path)
            excluded_sym_ids = {
                s.id for s in all_symbols
                if self._symbol_excluded(s, excluded_sym_set)
            }
            all_selected = all(s.id in closure.required_nodes for s in all_symbols)

            src = self.repo_root / file_path
            dst = self.output_root / file_path
            dst.parent.mkdir(parents=True, exist_ok=True)

            # F28: 方法级排除 — 即使 all_selected，也要检查 excluded_symbols
            if all_selected and excluded_sym_set and all_symbols:
                kept = [
                    s for s in all_symbols
                    if not self._symbol_excluded(s, excluded_sym_set)
                ]
                if len(kept) < len(all_symbols):
                    # G4b: 收集所有需要排除的符号 ID（被排除方法 + 其父类节点）
                    excluded_ids = {s.id for s in all_symbols} - {s.id for s in kept}
                    parent_class_ids: set[str] = set()
                    for eid in excluded_ids:
                        incoming = self.graph.get_incoming(eid, EdgeType.CONTAINS)
                        for edge in incoming:
                            p = self.graph.get_node(edge.source)
                            if p and p.node_type == NodeType.CLASS:
                                parent_class_ids.add(p.id)
                    if parent_class_ids:
                        excluded_ids.update(parent_class_ids)
                        kept = [s for s in kept if s.id not in parent_class_ids]
                    logger.info(
                        f"F28: {file_path} 方法级排除 {len(all_symbols) - len(kept)} 个符号"
                    )
                    self._partial_extract(
                        src, dst, kept, file_node, closure,
                        excluded_sym_ids=excluded_ids,
                    )
                    partial_extract += 1
                    continue

            if all_selected or not all_symbols:
                self._copy_file(src, dst)
                full_copy += 1
            # C/C++ 头文件: 如果有任何符号被选中，整文件复制
            # 头文件中的函数原型是编译器必需的，裁剪会导致 implicit declaration 错误
            elif (file_node.language in (Language.C, Language.CPP)
                  and file_path.suffix.lower() in ('.h', '.hpp', '.hh', '.hxx')
                  and any(s.id in closure.required_nodes for s in all_symbols)):
                self._copy_file(src, dst)
                full_copy += 1
                logger.debug(f"C header 整文件保留: {file_path}")
            else:
                selected_symbols = [
                    s for s in all_symbols
                    if s.id in closure.required_nodes
                    and not self._symbol_excluded(s, excluded_sym_set)
                ]
                self._partial_extract(src, dst, selected_symbols, file_node, closure)
                partial_extract += 1

        logger.info(f"文件提取: {full_copy} 个整文件, {partial_extract} 个部分提取")

        # F28: 文本级兜底 — 对整文件复制和符号图缺失场景再做一次方法裁剪
        self._postprocess_method_level_pruning(
            file_groups, excluded_sym_set, restricted_classes,
        )

        # F26b: 清理引用已删除模块的字符串字面量
        if excluded:
            self._clean_stale_module_refs(file_groups, excluded)

        # C/C++ 头文件配对
        self._pair_c_headers(closure, file_groups)

        # 复制构建配置文件
        self._copy_build_configs()

        # 生成桩代码（stub_nodes）
        if hasattr(closure, 'stub_nodes') and closure.stub_nodes:
            self._generate_stubs(closure)

        stats = {"total_files": len(file_groups)}
        logger.info(f"代码提取完成: {stats}")
        return self.output_root

    # ── 分组 & 查询 ──

    def _group_by_file(self, closure: ClosureResult) -> dict[Path, set[str]]:
        """按文件路径分组闭包中的节点"""
        groups: dict[Path, set[str]] = {}
        for node_id in closure.required_nodes:
            node = self.graph.get_node(node_id)
            if not node or not node.file_path:
                continue
            if node.node_type in (NodeType.DIRECTORY, NodeType.REPOSITORY):
                continue
            groups.setdefault(node.file_path, set()).add(node_id)
        return groups

    def _get_out_of_scope(self) -> list[str]:
        analysis = getattr(self.config, "instruction_analysis", None)
        if analysis and hasattr(analysis, "out_of_scope"):
            return analysis.out_of_scope or []
        return []

    @staticmethod
    def _is_excluded(rel_path: str, excluded: list[str]) -> bool:
        for ex in excluded:
            ex_norm = ex.replace("\\", "/")
            if ex_norm.endswith("/"):
                if rel_path.startswith(ex_norm) or rel_path.startswith(ex_norm.rstrip("/")):
                    return True
            else:
                if rel_path == ex_norm or rel_path.endswith("/" + ex_norm):
                    return True
        return False

    @staticmethod
    def _symbol_excluded(symbol: CodeNode, excluded_sym_set: set[str]) -> bool:
        """F28: 检查符号是否在方法级排除列表中。
        excluded_sym_set 条目格式: 'ClassName.methodName'
        - 方法节点命中后应排除该方法
        - 类节点也应命中，以避免“整类整文件”把已排除方法带回去
        """
        if not excluded_sym_set or not symbol.name:
            return False
        sym_name = symbol.name or ""
        sym_qn = symbol.qualified_name or ""
        sym_id = symbol.id or ""
        file_stem = symbol.file_path.stem if symbol.file_path else ""
        for pattern in excluded_sym_set:
            parts = pattern.rsplit('.', 1)
            if len(parts) != 2:
                continue
            cls_name, meth_name = parts
            same_class = (
                cls_name == file_stem
                or cls_name == sym_name
                or cls_name in sym_qn
                or cls_name in sym_id
            )
            if not same_class:
                continue
            if symbol.node_type == NodeType.CLASS and sym_name == cls_name:
                return True
            if (
                sym_name == meth_name
                or sym_name.endswith(f'.{meth_name}')
                or f'.{meth_name}' in sym_qn
                or f'.{meth_name}' in sym_id
            ):
                return True
        return False

    def _postprocess_method_level_pruning(
        self,
        file_groups: dict[Path, set[str]],
        excluded_sym_set: set[str],
        restricted_classes: set[str],
    ) -> None:
        """F28: 对输出文件做文本级兜底裁剪。
        处理两类场景：
        1. 显式排除的 Class.method
        2. “某类只保留被链路使用的方法”
        """
        if not excluded_sym_set and not restricted_classes:
            return

        for file_path in file_groups:
            dst = self.output_root / file_path
            if not dst.exists() or not dst.is_file():
                continue

            removed = self._apply_text_exclusions_to_file(dst, file_path, excluded_sym_set)
            if removed:
                logger.info(f"F28: {file_path} 文本级移除 {removed} 个显式排除方法")

            if file_path.stem in restricted_classes:
                trimmed = self._prune_restricted_class_file(dst, file_path)
                if trimmed:
                    logger.info(f"F28: {file_path} 按“只保留使用方法”裁剪 {trimmed} 个方法")

    def _apply_text_exclusions_to_file(
        self, dst: Path, file_path: Path, excluded_sym_set: set[str],
    ) -> int:
        try:
            source_text = dst.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning(f"无法读取 {dst}: {e}")
            return 0

        pruned_text, removed = self._prune_excluded_methods_from_text(
            source_text, file_path, excluded_sym_set,
        )
        if removed and pruned_text != source_text:
            dst.write_text(pruned_text, encoding="utf-8")
        return removed

    def _prune_restricted_class_file(self, dst: Path, file_path: Path) -> int:
        """对“只保留被链路使用的方法”的类做输出后裁剪。"""
        if file_path.suffix.lower() != ".java":
            return 0
        try:
            text = dst.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning(f"无法读取 {dst}: {e}")
            return 0

        class_name = file_path.stem
        public_methods = []
        pat = re.compile(
            r'(?m)^\s*public\s+(?!class\b)(?:[\w<>\[\], ?]+\s+)?([A-Za-z_]\w*)\s*\('
        )
        for m in pat.finditer(text):
            method_name = m.group(1)
            if method_name != class_name:
                public_methods.append(method_name)

        if not public_methods:
            return 0

        to_remove = {
            f"{class_name}.{name}"
            for name in public_methods
            if not self._method_is_referenced_elsewhere(file_path, name)
        }
        if not to_remove:
            return 0

        pruned_text, removed = self._prune_excluded_methods_from_text(
            text, file_path, to_remove,
        )
        if removed and pruned_text != text:
            dst.write_text(pruned_text, encoding="utf-8")
        return removed

    def _method_is_referenced_elsewhere(self, file_path: Path, method_name: str) -> bool:
        """判断方法是否被其它保留文件或用户指令显式引用。"""
        instruction = getattr(self.config, "user_instruction", "") or ""
        if method_name in instruction:
            return True

        needle = f".{method_name}("
        for other in self.output_root.rglob("*"):
            if not other.is_file() or other == (self.output_root / file_path):
                continue
            try:
                text = other.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if needle in text:
                return True
        return False

    @staticmethod
    def _prune_excluded_methods_from_text(
        source_text: str, file_path: Path, excluded_sym_set: set[str],
    ) -> tuple[str, int]:
        """从源码文本中删除指定 Class.method 的方法定义（Java/C/TS/JS brace-style）。"""
        relevant: list[str] = []
        file_stem = file_path.stem
        for pattern in excluded_sym_set:
            parts = pattern.rsplit('.', 1)
            if len(parts) != 2:
                continue
            cls_name, meth_name = parts
            if cls_name == file_stem:
                relevant.append(meth_name)

        pruned = source_text
        removed = 0
        for meth_name in relevant:
            pruned, changed = Surgeon._remove_braced_method(pruned, meth_name)
            if changed:
                removed += 1
        return pruned, removed

    @staticmethod
    def _remove_braced_method(source_text: str, method_name: str) -> tuple[str, bool]:
        """删除 brace-style 语言中的单个方法实现。"""
        pat = re.compile(
            rf'(?ms)^[ \t]*(?:@\w+(?:\([^\n)]*\))?\s*\n[ \t]*)*'
            rf'(?:public|protected|private|static|final|synchronized|abstract|native|default|\s)+'
            rf'[^\n{{;]*\b{re.escape(method_name)}\s*\([^\n)]*\)\s*'
            rf'(?:throws[^\n{{]*)?\{{'
        )
        m = pat.search(source_text)
        if not m:
            return source_text, False

        start = m.start()
        brace_pos = source_text.find('{', m.end() - 1)
        if brace_pos < 0:
            return source_text, False

        depth = 0
        end = None
        for idx in range(brace_pos, len(source_text)):
            ch = source_text[idx]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = idx + 1
                    while end < len(source_text) and source_text[end] in '\r\n':
                        end += 1
                    break

        if end is None:
            return source_text, False
        return source_text[:start] + source_text[end:], True

    def _clean_stale_module_refs(self, file_groups: dict[Path, set[str]],
                                  excluded: list[str]) -> None:
        """F26b: 清理输出文件中引用已排除模块的字符串字面量。
        典型场景: _WORKFLOW_MODULES 元组中保留了已删除模块的点分路径。
        """
        # 将排除列表中的文件路径转换为 Python 点分模块名
        excluded_modules: set[str] = set()
        for ex in excluded:
            ex_norm = ex.replace("\\", "/")
            if ex_norm.endswith(".py"):
                mod = ex_norm[:-3].replace("/", ".")
                excluded_modules.add(mod)
        if not excluded_modules:
            return

        cleaned = 0
        for fp in file_groups:
            dst = self.output_root / fp
            if not dst.exists() or dst.suffix != ".py":
                continue
            try:
                content = dst.read_text(encoding="utf-8")
            except OSError:
                continue
            new_content = content
            for mod in excluded_modules:
                # 移除元组/列表中形如 "module.path",  或  'module.path',
                pattern = re.compile(
                    r'[ \t]*["\']' + re.escape(mod) + r'["\'][ \t]*,?[ \t]*\n?'
                )
                new_content = pattern.sub("", new_content)
            if new_content != content:
                dst.write_text(new_content, encoding="utf-8")
                cleaned += 1
        if cleaned:
            logger.info(f"F26b: 清理了 {cleaned} 个文件中的过期模块引用")

    def _get_file_node(self, file_path: Path) -> CodeNode | None:
        return self.graph.get_node(f"file:{file_path}")

    def _get_all_symbols_in_file(self, file_path: Path) -> list[CodeNode]:
        """获取文件内的所有符号级节点"""
        return [n for n in self.graph.nodes.values()
                if n.file_path == file_path
                and n.node_type not in (NodeType.FILE, NodeType.DIRECTORY, NodeType.REPOSITORY)]

    # ── 文件复制 ──

    def _copy_file(self, src: Path, dst: Path) -> None:
        try:
            shutil.copy2(src, dst)
            logger.debug(f"整文件复制: {src.name}")
        except OSError as e:
            logger.warning(f"文件复制失败 {src}: {e}")

    # ── 桩代码生成 ──

    def _generate_stubs(self, closure: ClosureResult) -> None:
        """为 stub_nodes 生成桩代码文件"""
        excluded = self._get_out_of_scope()
        # 按文件分组 stub 节点
        stub_groups: dict[Path, list[CodeNode]] = {}
        for nid in closure.stub_nodes:
            node = self.graph.get_node(nid)
            if node and node.file_path:
                if excluded and self._is_excluded(str(node.file_path).replace("\\", "/"), excluded):
                    continue
                stub_groups.setdefault(node.file_path, []).append(node)

        for file_path, nodes in stub_groups.items():
            dst = self.output_root / file_path
            if dst.exists():
                # 文件已存在（从 required_nodes 提取），追加 stub
                self._append_stubs_to_file(dst, nodes)
            else:
                # 文件不存在，创建纯 stub 文件
                dst.parent.mkdir(parents=True, exist_ok=True)
                self._create_stub_file(dst, nodes)

        if stub_groups:
            total_stubs = sum(len(v) for v in stub_groups.values())
            logger.info(f"生成桩代码: {total_stubs} 个符号, {len(stub_groups)} 个文件")

    @staticmethod
    def _comment_line(text: str, lang: Language) -> str:
        """根据语言返回正确的单行注释"""
        if lang in (Language.C, Language.CPP):
            return f"/* {text} */"
        elif lang in (Language.JAVA, Language.TYPESCRIPT, Language.JAVASCRIPT):
            return f"// {text}"
        else:
            return f"# {text}"

    def _create_stub_file(self, dst: Path, nodes: list[CodeNode]) -> None:
        """创建纯 stub 文件"""
        lang = Language.from_extension(dst.suffix)
        header = self._comment_line("Stub file generated by CodePrune", lang)
        lines = [f"{header}\n\n"]
        for node in nodes:
            lines.append(self._generate_stub_code(node, lang))
            lines.append("\n\n")
        dst.write_text("".join(lines), encoding="utf-8")

    def _append_stubs_to_file(self, dst: Path, nodes: list[CodeNode]) -> None:
        """向已存在的文件追加 stub 代码"""
        lang = Language.from_extension(dst.suffix)
        content = dst.read_text(encoding="utf-8", errors="replace")
        additions = []
        for node in nodes:
            additions.append(self._generate_stub_code(node, lang))
        if additions:
            separator = self._comment_line("── Stubs (pruned dependencies) ──", lang)
            content += f"\n\n{separator}\n\n"
            content += "\n\n".join(additions) + "\n"
            dst.write_text(content, encoding="utf-8")

    def _extract_source_signature(self, node: CodeNode) -> str | None:
        """从原仓库源文件中提取函数/方法的完整签名行"""
        if not node.file_path or not node.byte_range:
            return None
        src = self.repo_root / node.file_path
        if not src.exists():
            return None
        try:
            lines = src.read_text(encoding="utf-8", errors="replace").splitlines()
            start = node.byte_range.start_line - 1  # 0-based
            if start < 0 or start >= len(lines):
                return None
            # 收集签名行（可能跨多行，到 : 或 { 为止）
            sig_lines = []
            for i in range(start, min(start + 5, len(lines))):
                sig_lines.append(lines[i])
                stripped = lines[i].rstrip()
                if stripped.endswith((':',  '{', ');')):
                    break
            return "\n".join(sig_lines)
        except OSError:
            return None

    @staticmethod
    def _c_default_return(return_type: str) -> str:
        """为 C/C++ 返回类型生成默认返回值"""
        rt = return_type.strip().rstrip("*").strip()
        if rt == "void":
            return ""
        if "*" in return_type:
            return "return NULL;"
        if rt in ("int", "long", "short", "size_t", "ssize_t", "int32_t",
                   "int64_t", "uint32_t", "uint64_t", "unsigned"):
            return "return 0;"
        if rt in ("float", "double"):
            return "return 0.0;"
        if rt in ("bool", "_Bool"):
            return "return false;"
        if rt == "char":
            return "return '\\0';"
        return "return 0;"

    def _generate_stub_code(self, node: CodeNode, lang: Language) -> str:
        """根据节点签名和语言生成桩代码（签名感知）"""
        name = node.name
        # 尝试从原仓库提取完整签名
        full_sig = self._extract_source_signature(node)

        if node.node_type == NodeType.FUNCTION:
            if lang == Language.PYTHON:
                if full_sig:
                    # 找到函数体开始的最后一个 : （跳过类型注解中的 :）
                    # 从末尾向前查找独立的 :
                    idx = full_sig.rfind(":")
                    if idx > 0:
                        header = full_sig[:idx].rstrip()
                        if header.startswith("def ") or header.lstrip().startswith("def "):
                            return f"{header}:\n    raise NotImplementedError('pruned by CodePrune')"
                header = f"def {name}({node.signature})" if node.signature else f"def {name}(*args, **kwargs)"
                return f"{header}:\n    raise NotImplementedError('pruned by CodePrune')"
            elif lang in (Language.C, Language.CPP):
                if full_sig:
                    # 提取返回类型 + 函数名 + 参数列表
                    import re as _re
                    m = _re.match(r'([\w\s*]+?)\b' + _re.escape(name) + r'\s*(\([^)]*\))', full_sig)
                    if m:
                        ret_type = m.group(1).strip()
                        params = m.group(2)
                        ret_stmt = self._c_default_return(ret_type)
                        body = f" {ret_stmt}" if ret_stmt else ""
                        return f"/* stub */ {ret_type} {name}{params} {{{body} /* pruned */}}"
                return f"/* stub */ void {name}() {{ /* pruned */ }}"
            elif lang in (Language.JAVA,):
                if full_sig:
                    # 去掉 { 之后的部分，保留签名
                    sig_line = full_sig.split("{")[0].strip()
                    if sig_line:
                        return f"/* stub */ {sig_line} {{ throw new UnsupportedOperationException(\"pruned\"); }}"
                sig = node.signature or ""
                return f"/* stub */ {sig or f'public void {name}()'} {{ throw new UnsupportedOperationException(\"pruned\"); }}"
            elif lang in (Language.TYPESCRIPT, Language.JAVASCRIPT):
                if full_sig:
                    sig_line = full_sig.split("{")[0].strip()
                    if sig_line:
                        return f"/* stub */ {sig_line} {{ throw new Error('pruned by CodePrune'); }}"
                sig = node.signature or ""
                return f"/* stub */ {sig or f'function {name}()'} {{ throw new Error('pruned by CodePrune'); }}"
            else:
                return f"/* stub: {name} */"

        elif node.node_type == NodeType.CLASS:
            if lang == Language.PYTHON:
                return f"class {name}:\n    \"\"\"Stub: pruned by CodePrune\"\"\"\n    pass"
            elif lang in (Language.JAVA,):
                return f"/* stub */ public class {name} {{ }}"
            elif lang in (Language.TYPESCRIPT, Language.JAVASCRIPT):
                return f"/* stub */ class {name} {{ }}"
            else:
                return f"/* stub: class {name} */"

        return self._comment_line(f"stub: {name}", lang)

    # ── 部分提取（核心手术逻辑）──

    def _partial_extract(
        self, src: Path, dst: Path,
        selected: list[CodeNode], file_node: CodeNode,
        closure: ClosureResult,
        *,
        excluded_sym_ids: set[str] | None = None,
    ) -> None:
        """
        部分提取：保留文件头 + 选中符号（含装饰器）+ 类骨架（构造方法）
        import 行智能过滤：只保留闭包中有对应目标的 import
        excluded_sym_ids: F28 方法级排除的符号 ID 集合
        """
        try:
            source_text = src.read_text(encoding="utf-8", errors="replace")
            lines = source_text.splitlines(keepends=True)
        except OSError as e:
            logger.warning(f"无法读取 {src}: {e}")
            return

        if not selected or not any(s.byte_range for s in selected):
            self._copy_file(src, dst)
            return

        keep_lines: set[int] = set()

        # 1. 文件头部 — 智能 import 过滤
        header_end = self._detect_header_end(lines, file_node.language)
        self._filter_header_imports(
            keep_lines, lines, header_end, file_node, closure,
        )

        # 2. 选中符号的行范围（含装饰器向上扩展）
        for sym in selected:
            if not sym.byte_range:
                continue
            start = sym.byte_range.start_line - 1   # 0-based
            end = sym.byte_range.end_line           # exclusive
            start = self._expand_decorators_upward(lines, start, file_node.language)
            keep_lines.update(range(start, min(end, len(lines))))

        # 3. 类骨架保留
        self._include_class_skeletons(keep_lines, lines, selected, file_node, closure)

        # 4. 模块级变量保留 — 被选中符号引用的全局变量
        self._include_module_level_vars(keep_lines, lines, selected, file_node)

        # 4.1. 同文件定义保留 — 被保留代码引用的同文件类/函数
        all_symbols = self._get_all_symbols_in_file(file_node.file_path)
        # G4b: F28 排除的符号及其父类不应被引用依赖重新拉回
        if excluded_sym_ids:
            all_symbols = [s for s in all_symbols if s.id not in excluded_sym_ids]
        self._include_referenced_definitions(
            keep_lines, lines, all_symbols, selected, file_node,
        )

        # 4.2. 再次扫描模块级变量 — 覆盖 Step 4.1 新增符号引用的变量
        self._include_module_level_vars_from_kept(
            keep_lines, lines, file_node,
        )

        # 4.5. C/C++ 条件编译对齐 — 确保 #ifdef/#endif 成对保留
        if file_node.language in (Language.C, Language.CPP):
            self._align_preprocessor_pairs(keep_lines, lines)

        # 5. 组装输出 — 在不连续行之间插入裁剪标记
        sorted_lines = sorted(keep_lines)
        output_lines = []
        comment_char = "#" if file_node.language == Language.PYTHON else "//"
        for idx, line_no in enumerate(sorted_lines):
            if line_no >= len(lines):
                continue
            # 检测是否与前一行不连续（间隔 > 1 行）
            if idx > 0 and line_no - sorted_lines[idx - 1] > 1:
                # 计算被裁剪的行数
                pruned_count = line_no - sorted_lines[idx - 1] - 1
                output_lines.append(f"{comment_char} ... pruned {pruned_count} lines ...\n")
            output_lines.append(lines[line_no])
        dst.write_text("".join(output_lines), encoding="utf-8")
        logger.debug(f"部分提取: {src.name} ({len(keep_lines)}/{len(lines)} 行)")

    def _expand_decorators_upward(self, lines: list[str], start_line: int, language: Language) -> int:
        """从符号起始行向上搜索，纳入连续的装饰器/注解行（支持多行注解）"""
        rules = get_language_rules(language)
        prefixes = rules.decorator_prefixes if rules else ()
        if not prefixes:
            return start_line
        i = start_line - 1
        open_parens = 0  # P1-fix: 追踪未闭合括号（处理多行注解）
        while i >= 0:
            stripped = lines[i].strip()
            if not stripped or stripped.startswith("//") or stripped.startswith("#"):
                # 空行或注释，继续向上看
                i -= 1
                continue
            # 计算当前行的括号平衡（从下往上扫描，先数 ')' 再数 '('）
            open_parens += stripped.count(')') - stripped.count('(')
            if any(stripped.startswith(p) for p in prefixes):
                start_line = i
                open_parens = 0  # 装饰器起始行，重置括号计数
                i -= 1
                continue
            if open_parens > 0:
                # 位于多行装饰器内部（有未闭合的 '('），继续向上
                start_line = i
                i -= 1
                continue
            break
        return start_line

    def _include_class_skeletons(
        self, keep_lines: set[int], lines: list[str],
        selected: list[CodeNode], file_node: CodeNode,
        closure: ClosureResult,
    ) -> None:
        """
        如果选中了类的某些方法（但未选整个类），保留：
        - 类声明行（class Xxx: / public class Xxx {）
        - 构造方法（__init__ / constructor / 与类同名方法）
        """
        # 找到选中方法所属的类
        parent_classes: dict[str, CodeNode] = {}
        for sym in selected:
            if sym.node_type != NodeType.FUNCTION:
                continue
            # 查找父类节点
            from core.graph.schema import EdgeType
            incoming = self.graph.get_incoming(sym.id, EdgeType.CONTAINS)
            for edge in incoming:
                parent = self.graph.get_node(edge.source)
                if parent and parent.node_type == NodeType.CLASS:
                    parent_classes[parent.id] = parent

        for cls_id, cls_node in parent_classes.items():
            if cls_id in closure.required_nodes:
                # 整个类已选中，无需骨架
                continue
            if not cls_node.byte_range:
                continue

            # 保留类声明行（通常只有起始 1-2 行）
            cls_start = cls_node.byte_range.start_line - 1
            cls_start = self._expand_decorators_upward(lines, cls_start, file_node.language)
            # 类声明通常到 { 或 : 结束 — 保留前 3 行或到 { 为止
            cls_decl_end = min(cls_node.byte_range.start_line + 2, cls_node.byte_range.end_line)
            for i in range(cls_start, cls_node.byte_range.start_line):
                keep_lines.add(i)
            for i in range(cls_node.byte_range.start_line - 1, cls_decl_end):
                line_str = lines[i].rstrip() if i < len(lines) else ""
                keep_lines.add(i)
                if "{" in line_str or line_str.endswith(":"):
                    break

            # 保留类的结尾大括号行（C/Java/JS/TS）
            if file_node.language in (Language.JAVA, Language.JAVASCRIPT, Language.TYPESCRIPT, Language.CPP):
                cls_end_line = cls_node.byte_range.end_line - 1
                if cls_end_line < len(lines):
                    keep_lines.add(cls_end_line)

            # 保留构造方法
            self._include_constructor(keep_lines, lines, cls_node, file_node.language)

    def _include_constructor(
        self, keep_lines: set[int], lines: list[str],
        cls_node: CodeNode, language: Language,
    ) -> None:
        """在类骨架中保留构造方法"""
        rules = get_language_rules(language)
        ctor_names = rules.constructor_names if rules else None
        if ctor_names is not None and not ctor_names:
            return  # C 语言没有构造方法

        # 从图中找该类的子方法
        children = cls_node.children
        for child_id in children:
            child = self.graph.get_node(child_id)
            if not child or child.node_type != NodeType.FUNCTION or not child.byte_range:
                continue
            is_ctor = False
            if ctor_names is None:
                # Java/C++ — 构造方法与类同名
                is_ctor = child.name == cls_node.name
            else:
                is_ctor = child.name in ctor_names
            if is_ctor:
                start = self._expand_decorators_upward(lines, child.byte_range.start_line - 1, language)
                end = child.byte_range.end_line
                keep_lines.update(range(start, min(end, len(lines))))

    # ── 智能 import 过滤 ──

    _IMPORT_LINE_PATTERNS: dict[Language, re.Pattern] = {
        Language.PYTHON: re.compile(
            r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w., ]+))"
        ),
        Language.JAVA: re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)"),
        Language.JAVASCRIPT: re.compile(
            r"""^\s*(?:import\s+.*?from\s+['"]([^'"]+)['"]|(?:const|let|var)\s+.*?=\s*require\(['"]([^'"]+)['"]\))"""
        ),
        Language.TYPESCRIPT: re.compile(
            r"""^\s*import\s+.*?from\s+['"]([^'"]+)['"]"""
        ),
        Language.C: re.compile(r'^\s*#include\s+[<"]([^>"]+)[>"]'),
        Language.CPP: re.compile(r'^\s*#include\s+[<"]([^>"]+)[>"]'),
    }

    def _filter_header_imports(
        self, keep_lines: set[int], lines: list[str],
        header_end: int, file_node: CodeNode,
        closure: ClosureResult,
    ) -> None:
        """
        智能 import 过滤：只保留闭包中有对应目标节点的 import 行。
        非 import 行（注释、空行、package 声明、pragma 等）无条件保留。
        支持多行 import（括号续行、反斜杠续行）。
        """
        pattern = self._IMPORT_LINE_PATTERNS.get(file_node.language)
        closure_files = {
            self.graph.get_node(nid).file_path
            for nid in closure.required_nodes | closure.soft_included
            if self.graph.get_node(nid) and self.graph.get_node(nid).file_path
        }
        closure_names = {
            self.graph.get_node(nid).name
            for nid in closure.required_nodes | closure.soft_included
            if self.graph.get_node(nid)
        }

        # 先将头部行分组为逻辑 import 单元（处理多行 import）
        i = 0
        while i < header_end:
            stripped = lines[i].strip()

            # 非 import 行：空行、注释、package、pragma、include guard → 无条件保留
            if not stripped or stripped.startswith("//") or stripped.startswith("#!"):
                keep_lines.add(i)
                i += 1
                continue
            if stripped.startswith("/*") or stripped.startswith("*") or stripped.startswith("*/"):
                keep_lines.add(i)
                i += 1
                continue
            if stripped.startswith("package "):
                keep_lines.add(i)
                i += 1
                continue
            if file_node.language in (Language.C, Language.CPP):
                if any(stripped.startswith(k) for k in ("#pragma", "#define", "#ifndef", "#ifdef", "#endif")):
                    keep_lines.add(i)
                    i += 1
                    continue
            if file_node.language == Language.CPP and stripped.startswith("using "):
                keep_lines.add(i)
                i += 1
                continue

            # 检测多行 import — 收集连续行
            import_lines = [i]
            merged = stripped
            # 括号续行: from x import (
            if "(" in stripped and ")" not in stripped:
                j = i + 1
                while j < header_end:
                    import_lines.append(j)
                    merged += " " + lines[j].strip()
                    if ")" in lines[j]:
                        break
                    j += 1
            # 反斜杠续行
            elif stripped.endswith("\\"):
                j = i + 1
                while j < header_end:
                    import_lines.append(j)
                    line_s = lines[j].strip()
                    merged += " " + line_s
                    if not line_s.endswith("\\"):
                        break
                    j += 1

            # 无法解析 pattern → 保守保留
            if pattern is None:
                for li in import_lines:
                    keep_lines.add(li)
                i = import_lines[-1] + 1
                continue

            m = pattern.match(merged)
            if not m:
                for li in import_lines:
                    keep_lines.add(li)
                i = import_lines[-1] + 1
                continue

            target = next((g for g in m.groups() if g is not None), None)
            if target is None:
                for li in import_lines:
                    keep_lines.add(li)
                i = import_lines[-1] + 1
                continue

            # 判断是否需要保留 — 整组行要么全保留要么全丢弃
            # TypeScript type-only import: 更宽松地丢弃 — 只在被提取代码实际引用时保留
            is_type_import = (
                file_node.language == Language.TYPESCRIPT
                and (merged.lstrip().startswith("import type ")
                     or "{ type " in merged)
            )
            if is_type_import:
                # 只在被提取的代码行中有名称引用时保留
                extracted_text = "\n".join(
                    lines[li] for li in keep_lines if li >= header_end and li < len(lines)
                )
                import_names = re.findall(r"(?:type\s+)?(\w+)", merged.split("from")[0]) if "from" in merged else []
                need_keep = any(
                    n in extracted_text
                    for n in import_names
                    if n not in ("import", "type", "from", "{", "}")
                )
                if need_keep:
                    for li in import_lines:
                        keep_lines.add(li)
            elif self._import_target_in_closure(target, file_node.language, closure_files, closure_names):
                for li in import_lines:
                    keep_lines.add(li)

            i = import_lines[-1] + 1

    def _import_target_in_closure(
        self, target: str, language: Language,
        closure_files: set, closure_names: set,
    ) -> bool:
        """判断某个 import 目标是否存在于闭包中"""
        # 标准库 import — 保留（不在图中但必要）
        if language == Language.PYTHON:
            from parsers.import_resolver import PYTHON_STDLIB
            # 剥离相对导入前缀点号: ".models" → "models", "..utils" → "utils"
            clean_target = target.lstrip(".")
            top = clean_target.split(".")[0] if clean_target else ""
            if top in PYTHON_STDLIB:
                return True
        elif language == Language.JAVA:
            from parsers.import_resolver import JAVA_STDLIB_PREFIXES
            if any(target.startswith(p) for p in JAVA_STDLIB_PREFIXES):
                return True
            clean_target = target
        else:
            clean_target = target

        # 检查：闭包中是否有同名节点或文件路径包含 target
        parts = clean_target.replace(".", "/").replace("\\", "/")
        if parts:
            for fp in closure_files:
                if fp and parts in str(fp).replace("\\", "/"):
                    return True
        # 检查名称匹配（最后一段，如 "utils.ids" → "ids"）
        last_part = clean_target.split(".")[-1] if "." in clean_target else clean_target
        if last_part and last_part in closure_names:
            return True
        # 多 import: import a, b, c
        for name in clean_target.split(","):
            name = name.strip()
            if name in closure_names:
                return True
        return False

    # ── 模块级变量保留 ──

    def _include_module_level_vars(
        self, keep_lines: set[int], lines: list[str],
        selected: list[CodeNode], file_node: CodeNode,
    ) -> None:
        """
        保留被选中符号引用的模块级变量（顶层赋值语句）。
        扫描文件中的顶层赋值行，检查选中符号的源码中是否引用了该变量名。
        """
        if file_node.language not in (Language.PYTHON, Language.JAVASCRIPT, Language.TYPESCRIPT):
            return

        # 找到顶层赋值语句（不在任何函数/类内部的行）
        occupied_ranges: list[tuple[int, int]] = []
        for child_id in file_node.children:
            child = self.graph.get_node(child_id)
            if child and child.byte_range:
                occupied_ranges.append(
                    (child.byte_range.start_line - 1, child.byte_range.end_line)
                )

        def _is_inside_symbol(line_idx: int) -> bool:
            return any(s <= line_idx < e for s, e in occupied_ranges)

        # 收集选中符号源码中用到的标识符
        used_names: set[str] = set()
        ident_re = re.compile(r"\b([A-Za-z_]\w*)\b")
        for sym in selected:
            if not sym.byte_range:
                continue
            start = sym.byte_range.start_line - 1
            end = min(sym.byte_range.end_line, len(lines))
            for i in range(start, end):
                used_names.update(ident_re.findall(lines[i]))

        # Python 顶层赋值模式
        assign_re = re.compile(r"^([A-Za-z_]\w*)\s*(?:[:=])")

        for i, line in enumerate(lines):
            if _is_inside_symbol(i):
                continue
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                continue
            m = assign_re.match(stripped)
            if m:
                var_name = m.group(1)
                if var_name in used_names:
                    # 保留赋值行本身
                    keep_lines.add(i)
                    # 处理多行赋值（括号、花括号、方括号跨行）
                    self._include_multiline_assign(keep_lines, lines, i)

    @staticmethod
    def _include_multiline_assign(
        keep_lines: set[int], lines: list[str], start: int,
    ) -> None:
        """如果赋值行包含未闭合的括号/花括号/方括号，向下扩展直到闭合。"""
        depth = 0
        openers = "({["
        closers = ")}]"
        for i in range(start, len(lines)):
            for ch in lines[i]:
                if ch in openers:
                    depth += 1
                elif ch in closers:
                    depth -= 1
            keep_lines.add(i)
            if depth <= 0 and i > start:
                break

    def _include_module_level_vars_from_kept(
        self, keep_lines: set[int], lines: list[str],
        file_node: CodeNode,
    ) -> None:
        """
        Step 4.2: 基于所有已保留行重新扫描模块级变量。
        与 _include_module_level_vars 不同，此方法从 keep_lines 而非
        selected 符号收集标识符，以覆盖 Step 4.1 新增符号引用的变量。
        """
        if file_node.language not in (
            Language.PYTHON, Language.JAVASCRIPT, Language.TYPESCRIPT,
        ):
            return

        occupied_ranges: list[tuple[int, int]] = []
        for child_id in file_node.children:
            child = self.graph.get_node(child_id)
            if child and child.byte_range:
                occupied_ranges.append(
                    (child.byte_range.start_line - 1, child.byte_range.end_line)
                )

        def _is_inside_symbol(line_idx: int) -> bool:
            return any(s <= line_idx < e for s, e in occupied_ranges)

        # 从所有已保留行收集标识符
        ident_re = re.compile(r"\b([A-Za-z_]\w*)\b")
        used_names: set[str] = set()
        for li in keep_lines:
            if li < len(lines):
                used_names.update(ident_re.findall(lines[li]))

        assign_re = re.compile(r"^([A-Za-z_]\w*)\s*(?:[:=])")
        added = False
        for i, line in enumerate(lines):
            if i in keep_lines:
                continue
            if _is_inside_symbol(i):
                continue
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                continue
            m = assign_re.match(stripped)
            if m and m.group(1) in used_names:
                keep_lines.add(i)
                self._include_multiline_assign(keep_lines, lines, i)
                added = True
                logger.debug(
                    f"模块级变量补充保留: {m.group(1)} ({file_node.file_path})"
                )

        if added:
            logger.info("Step 4.2: 补充保留了新增符号引用的模块级变量")

    def _include_referenced_definitions(
        self,
        keep_lines: set[int],
        lines: list[str],
        all_symbols: list[CodeNode],
        selected: list[CodeNode],
        file_node: CodeNode,
    ) -> None:
        """
        同文件定义保留：如果保留代码中引用了同文件内的其他符号
        （class、dataclass、function），自动包含该符号的完整定义。
        迭代最多 2 轮处理级联引用。
        """
        selected_ids = {s.id for s in selected}
        ident_re = re.compile(r"\b([A-Za-z_]\w*)\b")

        for _ in range(2):  # 最多 2 轮级联
            # 收集保留行中的所有标识符
            used_names: set[str] = set()
            for li in keep_lines:
                if li < len(lines):
                    used_names.update(ident_re.findall(lines[li]))

            added = False
            for sym in all_symbols:
                if sym.id in selected_ids:
                    continue
                if not sym.byte_range:
                    continue
                # 符号名在保留代码中被引用 → 包含该定义
                if sym.name in used_names:
                    start = sym.byte_range.start_line - 1
                    end = sym.byte_range.end_line
                    start = self._expand_decorators_upward(
                        lines, start, file_node.language,
                    )
                    new_range = set(range(start, min(end, len(lines))))
                    if not new_range.issubset(keep_lines):
                        keep_lines.update(new_range)
                        selected_ids.add(sym.id)
                        added = True
                        logger.debug(
                            f"同文件定义保留: {sym.name} ({file_node.file_path})"
                        )

            if not added:
                break

    def _align_preprocessor_pairs(
        self, keep_lines: set[int], lines: list[str],
    ) -> None:
        """
        C/C++ 条件编译对齐：确保 #ifdef/#ifndef/#if 和 #endif 成对保留。
        如果 keep_lines 中包含某个 #ifdef 块内的代码行，
        则自动补全该块的 #ifdef + #endif（及 #else/#elif）行。
        """
        # 先构建条件编译块的层级结构
        _PP_START = re.compile(r"^\s*#\s*(ifdef|ifndef|if)\b")
        _PP_ELSE = re.compile(r"^\s*#\s*(else|elif)\b")
        _PP_END = re.compile(r"^\s*#\s*endif\b")

        # 用栈扫描，建立 start→end 映射
        stack: list[tuple[int, list[int]]] = []  # (start_line, [else_lines])
        blocks: list[tuple[int, list[int], int]] = []  # (start, elses, end)

        for i, line in enumerate(lines):
            stripped = line.strip()
            if _PP_START.match(stripped):
                stack.append((i, []))
            elif _PP_ELSE.match(stripped):
                if stack:
                    stack[-1][1].append(i)
            elif _PP_END.match(stripped):
                if stack:
                    start, elses = stack.pop()
                    blocks.append((start, elses, i))

        # 检查哪些块包含 keep_lines 中的行
        added = set()
        for start, elses, end in blocks:
            # 检查 block 内是否有被保留的代码行
            has_kept = any(
                li in keep_lines
                for li in range(start + 1, end)
                if li not in added
            )
            if has_kept:
                added.add(start)
                added.add(end)
                for e in elses:
                    added.add(e)

        keep_lines.update(added)
        if added:
            logger.debug(f"条件编译对齐: 补充 {len(added)} 行预处理指令")

    # ── 文件头检测 ──

    def _detect_header_end(self, lines: list[str], language: Language) -> int:
        """检测文件头部（import/include/package 声明区）的结束行"""
        rules = get_language_rules(language)
        keywords = rules.import_header_keywords() if rules else ("import ",)
        header_end = 0
        in_block_comment = False
        in_docstring = False
        docstring_quote: str | None = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            # 跟踪 Python 多行 docstring
            if in_docstring:
                header_end = i + 1
                if docstring_quote and docstring_quote in stripped:
                    in_docstring = False
                    docstring_quote = None
                continue
            # 跟踪块注释
            if in_block_comment:
                header_end = i + 1
                if "*/" in stripped:
                    in_block_comment = False
                continue
            if stripped.startswith("/*"):
                in_block_comment = "*/" not in stripped
                header_end = i + 1
                continue
            if not stripped or stripped.startswith("//") or stripped.startswith("#!"):
                header_end = i + 1
                continue
            # Python: # 注释和三引号 docstring
            if language == Language.PYTHON:
                if stripped.startswith("#"):
                    header_end = i + 1
                    continue
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    quote = stripped[:3]
                    rest = stripped[3:]
                    if quote in rest:
                        # 单行 docstring: """text"""
                        header_end = i + 1
                        continue
                    else:
                        # 多行 docstring 起始
                        in_docstring = True
                        docstring_quote = quote
                        header_end = i + 1
                        continue
            if any(stripped.startswith(kw) for kw in keywords):
                header_end = i + 1
                continue
            break
        return header_end

    # ── C/C++ 头文件配对 ──

    def _pair_c_headers(self, closure: ClosureResult, file_groups: dict[Path, set[str]]) -> None:
        """C/C++ 项目：如果选中了 .c/.cpp 文件，自动带上对应的 .h/.hpp 头文件；反之亦然。
        支持跨目录配对（如 include/xxx.h ↔ src/xxx.c）。
        配对的文件路径记录在 self.auto_paired_files 中，供下游 heal 使用。
        """
        rules = get_language_rules(Language.C)
        pairs = rules.header_source_pairs if rules and rules.header_source_pairs else _HEADER_SOURCE_PAIRS
        excluded = self._get_out_of_scope()

        for file_path in list(file_groups.keys()):
            suffix = file_path.suffix.lower()
            # 反向查找：如果选了源文件，找对应头文件
            for h_ext, src_exts in pairs.items():
                if suffix in src_exts:
                    header = self._find_pair_file(file_path, h_ext)
                    if header and not self._is_excluded(str(header).replace("\\", "/"), excluded):
                        dst_header = self.output_root / header
                        if not dst_header.exists():
                            dst_header.parent.mkdir(parents=True, exist_ok=True)
                            self._copy_file(self.repo_root / header, dst_header)
                            self.auto_paired_files.append(str(header).replace("\\", "/"))
                            logger.debug(f"自动配对头文件: {header}")
            # 正向查找：如果选了头文件，找对应源文件
            if suffix in pairs:
                for src_ext in pairs[suffix]:
                    source = self._find_pair_file(file_path, src_ext)
                    if source and not self._is_excluded(str(source).replace("\\", "/"), excluded):
                        dst_source = self.output_root / source
                        if not dst_source.exists():
                            dst_source.parent.mkdir(parents=True, exist_ok=True)
                            self._copy_file(self.repo_root / source, dst_source)
                            self.auto_paired_files.append(str(source).replace("\\", "/"))
                            logger.debug(f"自动配对源文件: {source}")

    def _find_pair_file(self, file_path: Path, target_ext: str) -> Path | None:
        """查找配对文件：先同目录，再跨目录（通过图中 FILE 节点匹配 stem）。"""
        # 1. 同目录
        same_dir = file_path.with_suffix(target_ext)
        if (self.repo_root / same_dir).exists():
            return same_dir
        # 2. 跨目录：遍历图中所有 FILE 节点，找 stem 相同且后缀匹配的
        stem = file_path.stem
        for node in self.graph.file_nodes:
            if node.file_path and node.file_path.stem == stem and node.file_path.suffix.lower() == target_ext:
                return node.file_path
        return None

    # ── 构建配置复制 ──

    def _copy_build_configs(self) -> None:
        """复制构建配置文件到子仓库（跳过 out_of_scope 或引用已删文件的构建文件）"""
        config_files = [
            "Makefile", "CMakeLists.txt",
            "pom.xml", "build.gradle", "build.gradle.kts",
            "package.json", "tsconfig.json", "webpack.config.js", "vite.config.ts",
            "setup.py", "setup.cfg", "pyproject.toml",
            ".gitignore", "README.md",
        ]
        # 构建文件中可能引用源文件，需验证引用是否有效
        build_files_to_validate = {"Makefile", "CMakeLists.txt"}

        out_of_scope = self._get_out_of_scope()
        # 收集子仓库实际保留的源文件路径
        extracted_files = set()
        for f in self.output_root.rglob("*"):
            if f.is_file():
                try:
                    extracted_files.add(str(f.relative_to(self.output_root)).replace("\\", "/"))
                except ValueError:
                    pass

        for name in config_files:
            if any(name == s or s.replace("\\", "/").endswith("/" + name) for s in out_of_scope):
                logger.debug(f"跳过 out_of_scope 配置: {name}")
                continue
            src = self.repo_root / name
            if src.exists():
                # 对 Makefile/CMakeLists.txt 验证引用的源文件是否都在子仓库中
                if name in build_files_to_validate:
                    if not self._validate_build_file_refs(src, extracted_files):
                        logger.info(f"跳过 {name} — 引用了已删除的源文件")
                        continue
                dst = self.output_root / name
                try:
                    shutil.copy2(src, dst)
                    logger.debug(f"复制配置: {name}")
                except OSError:
                    pass

    def _validate_build_file_refs(self, build_file: Path, extracted_files: set[str]) -> bool:
        """检查构建文件引用的源文件是否都存在于子仓库

        对 Makefile: 解析 SRCS/OBJS 变量中的 .c/.cpp/.java 文件
        对 CMakeLists.txt: 解析 add_executable/add_library 中的源文件
        """
        try:
            text = build_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True  # 读取失败时保守保留

        # 提取文件引用: 匹配 src/xxx.c, xxx.cpp 等模式
        src_pattern = re.compile(r'([\w./\\-]+\.(?:c|cpp|cc|cxx|java|py|ts|js))\b')
        referenced_files = set()
        for m in src_pattern.finditer(text):
            ref = m.group(1).replace("\\", "/")
            # 跳过注释行中的引用
            line_start = text.rfind("\n", 0, m.start()) + 1
            line = text[line_start:m.start()].strip()
            if line.startswith("#") or line.startswith("//"):
                continue
            referenced_files.add(ref)

        if not referenced_files:
            return True  # 无源文件引用时保留

        # 检查引用的源文件是否都在子仓库中
        missing = referenced_files - extracted_files
        if missing:
            logger.debug(
                f"{build_file.name} 引用了 {len(missing)} 个不在子仓库中的文件: "
                f"{list(missing)[:5]}"
            )
            return False
        return True
