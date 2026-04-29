"""
tree-sitter 统一多语言适配器
封装 tree-sitter 的 AST 解析，提供统一的符号提取和依赖分析接口
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.graph.schema import ByteRange, CodeNode, Edge, EdgeType, Language, NodeType

logger = logging.getLogger(__name__)


@dataclass
class SymbolInfo:
    """从 AST 提取的符号信息"""
    node: CodeNode
    parent_id: Optional[str] = None


class TreeSitterAdapter:
    """tree-sitter 适配器：多语言统一 AST 解析"""

    TS_LANG_MAP = {
        Language.PYTHON: "python",
        Language.JAVA: "java",
        Language.JAVASCRIPT: "javascript",
        Language.TYPESCRIPT: "typescript",
        Language.C: "c",
        Language.CPP: "cpp",
    }

    # 各语言的 AST 节点类型 → CodeNode 类型
    SYMBOL_TYPE_MAP: dict[Language, dict[str, NodeType]] = {
        Language.PYTHON: {
            "function_definition": NodeType.FUNCTION,
            "class_definition": NodeType.CLASS,
        },
        Language.JAVA: {
            "method_declaration": NodeType.FUNCTION,
            "constructor_declaration": NodeType.FUNCTION,
            "class_declaration": NodeType.CLASS,
            "interface_declaration": NodeType.INTERFACE,
            "enum_declaration": NodeType.ENUM,
        },
        Language.JAVASCRIPT: {
            "function_declaration": NodeType.FUNCTION,
            "method_definition": NodeType.FUNCTION,
            "class_declaration": NodeType.CLASS,
            "arrow_function": NodeType.FUNCTION,
            "generator_function_declaration": NodeType.FUNCTION,
        },
        Language.TYPESCRIPT: {
            "function_declaration": NodeType.FUNCTION,
            "method_definition": NodeType.FUNCTION,
            "class_declaration": NodeType.CLASS,
            "interface_declaration": NodeType.INTERFACE,
            "enum_declaration": NodeType.ENUM,
            "type_alias_declaration": NodeType.CLASS,
            "arrow_function": NodeType.FUNCTION,
        },
        Language.C: {
            "function_definition": NodeType.FUNCTION,
            "struct_specifier": NodeType.CLASS,
            "enum_specifier": NodeType.ENUM,
            "type_definition": NodeType.CLASS,
        },
        Language.CPP: {
            "function_definition": NodeType.FUNCTION,
            "class_specifier": NodeType.CLASS,
            "struct_specifier": NodeType.CLASS,
            "namespace_definition": NodeType.NAMESPACE,
            "enum_specifier": NodeType.ENUM,
            "type_definition": NodeType.CLASS,
        },
    }

    # 各语言的 import 节点类型
    IMPORT_NODE_TYPES: dict[Language, set[str]] = {
        Language.PYTHON: {"import_statement", "import_from_statement"},
        Language.JAVA: {"import_declaration"},
        Language.JAVASCRIPT: {"import_statement", "export_statement"},
        Language.TYPESCRIPT: {"import_statement", "export_statement"},
        Language.C: {"preproc_include"},
        Language.CPP: {"preproc_include"},
    }

    # 各语言的调用表达式节点类型
    CALL_NODE_TYPES = {"call_expression", "call", "method_invocation", "function_call_expression"}

    # 各语言的继承节点查找策略
    INHERIT_QUERY: dict[Language, str] = {
        Language.PYTHON: "argument_list",      # class A(Base):  → bases 在 argument_list 中
        Language.JAVA: "superclass",            # extends 子节点
        Language.JAVASCRIPT: "class_heritage",
        Language.TYPESCRIPT: "class_heritage",
        Language.CPP: "base_class_clause",
    }

    def __init__(self, language: Language):
        self.language = language
        self._parser = None
        self._symbol_map = self.SYMBOL_TYPE_MAP.get(language, {})
        self._import_types = self.IMPORT_NODE_TYPES.get(language, set())

    # 独立语言包模块映射
    _TS_LANG_MODULES = {
        "python": "tree_sitter_python",
        "java": "tree_sitter_java",
        "javascript": "tree_sitter_javascript",
        "typescript": "tree_sitter_typescript",
        "c": "tree_sitter_c",
        "cpp": "tree_sitter_cpp",
    }

    def _ensure_parser(self):
        """懒加载 tree-sitter parser"""
        if self._parser is not None:
            return
        lang_name = self.TS_LANG_MAP.get(self.language)
        if not lang_name:
            raise ValueError(f"不支持的语言: {self.language}")
        try:
            from tree_sitter import Language as TSLanguage, Parser
            mod_name = self._TS_LANG_MODULES.get(lang_name)
            if not mod_name:
                raise ValueError(f"不支持的语言: {lang_name}")
            import importlib
            mod = importlib.import_module(mod_name)
            if lang_name == "typescript":
                lang_func = getattr(mod, "language_typescript", None) or mod.language
            else:
                lang_func = mod.language

            # 兼容不同 tree-sitter / tree-sitter-<lang> 版本：
            # - 有的语言包返回可直接传给 Language(...) 的对象
            # - 有的返回 PyCapsule，需要先提取底层指针
            raw_lang = lang_func()
            try:
                ts_lang = TSLanguage(raw_lang)
            except TypeError:
                try:
                    ts_lang = TSLanguage(raw_lang, lang_name)
                except (TypeError, ValueError):
                    import ctypes

                    get_ptr = ctypes.pythonapi.PyCapsule_GetPointer
                    get_ptr.argtypes = [ctypes.py_object, ctypes.c_char_p]
                    get_ptr.restype = ctypes.c_void_p
                    ptr = get_ptr(raw_lang, b"tree_sitter.Language")
                    ts_lang = TSLanguage(ptr, lang_name)

            self._parser = Parser()
            if hasattr(self._parser, "set_language"):
                self._parser.set_language(ts_lang)
            else:
                self._parser.language = ts_lang
        except ImportError as e:
            raise ImportError(f"pip install tree-sitter tree-sitter-{lang_name}: {e}")

    def parse(self, source: bytes):
        """解析源码返回 AST root"""
        self._ensure_parser()
        return self._parser.parse(source)

    # ═══════════════ 符号提取 ═══════════════

    def extract_symbols(self, source: bytes, file_path: Path) -> list[SymbolInfo]:
        """从源码中提取所有顶级和嵌套的符号节点"""
        tree = self.parse(source)
        symbols: list[SymbolInfo] = []
        file_id = f"file:{file_path}"
        self._walk_symbols(tree.root_node, source, file_path, file_id, symbols, depth=0)
        return symbols

    def _walk_symbols(self, node, source: bytes, file_path: Path,
                      parent_id: str, symbols: list[SymbolInfo], depth: int) -> None:
        """递归遍历 AST 提取符号"""
        if depth > 20:  # 防止极端嵌套
            return

        sym = self._try_extract_symbol(node, source, file_path, parent_id)
        current_parent = sym.node.id if sym else parent_id
        if sym:
            symbols.append(sym)

        for child in node.children:
            self._walk_symbols(child, source, file_path, current_parent, symbols, depth + 1)

    def _try_extract_symbol(self, node, source: bytes, file_path: Path,
                            parent_id: str) -> Optional[SymbolInfo]:
        """尝试将 AST 节点转换为 CodeNode"""
        if node.type not in self._symbol_map:
            return None

        code_type = self._symbol_map[node.type]
        name = self._extract_name(node, source)

        # 匿名箭头函数：从外层 variable_declarator 取名
        if not name and node.type == "arrow_function":
            name = self._extract_arrow_function_name(node, source)
        if not name:
            return None

        # 装饰器/注解扩展 byte_range
        start_byte, end_byte = self._adjust_range_for_decorators(node)
        start_line = self._byte_to_line(source, start_byte)
        end_line = node.end_point[0] + 1

        byte_range = ByteRange(
            start_byte=start_byte,
            end_byte=end_byte,
            start_line=start_line,
            start_col=node.start_point[1],
            end_line=end_line,
            end_col=node.end_point[1],
        )

        # F23: 类方法加入父类前缀，避免同名方法 ID 碰撞
        if parent_id.startswith("class:") and "::" in parent_id:
            parent_class_name = parent_id.split("::", 1)[1]
            qualified_name = f"{file_path}::{parent_class_name}.{name}"
        else:
            qualified_name = f"{file_path}::{name}"
        node_id = f"{code_type.value}:{qualified_name}"

        # 提取函数签名
        signature = None
        if code_type == NodeType.FUNCTION:
            signature = self._extract_signature(node, source)

        code_node = CodeNode(
            id=node_id,
            node_type=code_type,
            name=name,
            qualified_name=qualified_name,
            file_path=file_path,
            language=self.language,
            byte_range=byte_range,
            signature=signature,
        )

        return SymbolInfo(node=code_node, parent_id=parent_id)

    def _extract_name(self, node, source: bytes) -> Optional[str]:
        """从 AST 节点提取名称"""
        # G4: 优先使用 tree-sitter field name，避免 Java method 的 return type
        #     （type_identifier）被误判为方法名
        field_name_node = node.child_by_field_name("name")
        if field_name_node:
            return self._node_text(field_name_node, source)
        # G4b: C/C++ function_definition 的函数名在 declarator 字段中
        #      (function_definition → function_declarator → identifier)
        #      不能从 children 中取第一个 type_identifier，那是返回类型
        declarator_node = node.child_by_field_name("declarator")
        if declarator_node:
            name = self._extract_name_from_declarator(declarator_node, source)
            if name:
                return name
        name_types = {"identifier", "name", "type_identifier", "property_identifier"}
        for child in node.children:
            if child.type in name_types:
                return self._node_text(child, source)
            # Java constructor: declarator child
            if child.type == "identifier" and node.type == "constructor_declaration":
                return self._node_text(child, source)
        return None

    @staticmethod
    def _extract_name_from_declarator(declarator_node, source: bytes) -> Optional[str]:
        """从 C/C++ declarator 链中提取函数/变量名。
        function_declarator → identifier (field=declarator) 就是函数名。"""
        # 直接在 declarator 的 children 中找 identifier
        for child in declarator_node.children:
            if child.type == "identifier":
                return source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
        # 嵌套 declarator（如 pointer_declarator → identifier）
        nested = declarator_node.child_by_field_name("declarator")
        if nested:
            if nested.type == "identifier":
                return source[nested.start_byte:nested.end_byte].decode("utf-8", errors="replace")
            # 再递归一层
            for child in nested.children:
                if child.type == "identifier":
                    return source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
        return None

    def _extract_arrow_function_name(self, node, source: bytes) -> Optional[str]:
        """从外层 variable_declarator 获取箭头函数的名称"""
        parent = node.parent
        if parent and parent.type == "variable_declarator":
            for child in parent.children:
                if child.type in ("identifier", "name"):
                    return self._node_text(child, source)
        return None

    def _extract_signature(self, node, source: bytes) -> Optional[str]:
        """提取函数签名（参数列表部分）"""
        for child in node.children:
            if child.type in ("parameters", "formal_parameters", "parameter_list"):
                return self._node_text(child, source)
        return None

    def _adjust_range_for_decorators(self, node) -> tuple[int, int]:
        """扩展 byte_range 以包含装饰器/注解/文档注释"""
        start = node.start_byte
        prev = node.prev_named_sibling
        while prev and prev.type in ("decorator", "annotation", "comment", "block_comment",
                                      "expression_statement"):
            # expression_statement: Python 的 docstring 可能出现在函数开头
            if prev.type == "expression_statement":
                break  # docstring 在函数内部，不应向外扩展
            start = prev.start_byte
            prev = prev.prev_named_sibling
        return start, node.end_byte

    def _byte_to_line(self, source: bytes, byte_offset: int) -> int:
        """字节偏移 → 行号 (1-based)"""
        return source[:byte_offset].count(b"\n") + 1

    # ═══════════════ 依赖提取 ═══════════════

    def extract_dependencies(self, source: bytes, file_path: Path) -> list[Edge]:
        """提取文件的所有依赖边"""
        tree = self.parse(source)
        deps: list[Edge] = []
        file_id = f"file:{file_path}"
        self._walk_deps(tree.root_node, source, file_path, file_id, deps)
        return deps

    def _walk_deps(self, node, source: bytes, file_path: Path,
                   file_id: str, deps: list[Edge],
                   enclosing_class: str | None = None) -> None:
        """递归遍历 AST 提取依赖，跟踪封闭类上下文"""
        # import 边
        if node.type in self._import_types:
            edge = self._extract_import_edge(node, source, file_id)
            if edge:
                deps.append(edge)

        # 调用边（C1：带类限定）
        if node.type in self.CALL_NODE_TYPES:
            call_target = self._extract_call_target(node, source, enclosing_class)
            if call_target:
                deps.append(Edge(
                    source=file_id,
                    target=f"call:{call_target}",
                    edge_type=EdgeType.CALLS,
                    confidence=0.8,
                ))

        # 继承边
        if node.type in ("class_definition", "class_declaration", "class_specifier"):
            inherit_edges = self._extract_inheritance(node, source, file_path)
            deps.extend(inherit_edges)

        # 更新封闭类上下文
        next_class = enclosing_class
        if node.type in ("class_definition", "class_declaration", "class_specifier"):
            extracted = self._extract_name(node, source)
            if extracted:
                next_class = extracted

        for child in node.children:
            self._walk_deps(child, source, file_path, file_id, deps, next_class)

    def _extract_import_edge(self, node, source: bytes, file_id: str) -> Optional[Edge]:
        """提取 import 边"""
        import_text = self._node_text(node, source)
        if not import_text:
            return None

        # 提取模块路径
        module_path = self._parse_import_module(node, source)
        if not module_path:
            return None

        # 提取具体导入的符号名
        imported_symbols = self._parse_imported_symbols(node, source)

        metadata = {"raw": import_text}
        if imported_symbols:
            metadata["imported_symbols"] = imported_symbols

        # JS/TS: 检测 re-export ("export { X } from 'Y'")
        if self.language in (Language.JAVASCRIPT, Language.TYPESCRIPT):
            stripped = import_text.lstrip()
            if stripped.startswith("export "):
                metadata["re_export"] = True

        # TypeScript: 检测 type-only import/export ("import type { X }" / "export type { X } from")
        if self.language == Language.TYPESCRIPT:
            stripped = import_text.lstrip()
            if stripped.startswith("import type ") or stripped.startswith("export type "):
                metadata["type_only"] = True
            elif "{ type " in import_text:
                # 部分 type import — 标记哪些是 type-only
                type_symbols = []
                for part in re.findall(r"\btype\s+(\w+)", import_text):
                    type_symbols.append(part)
                if type_symbols:
                    metadata["type_only_symbols"] = type_symbols

        return Edge(
            source=file_id,
            target=f"module:{module_path}",
            edge_type=EdgeType.IMPORTS,
            metadata=metadata,
        )

    def _parse_import_module(self, node, source: bytes) -> Optional[str]:
        """解析 import 节点中的模块路径"""
        if self.language == Language.PYTHON:
            return self._parse_python_import(node, source)
        elif self.language == Language.JAVA:
            return self._parse_java_import(node, source)
        elif self.language in (Language.JAVASCRIPT, Language.TYPESCRIPT):
            return self._parse_js_import(node, source)
        elif self.language in (Language.C, Language.CPP):
            return self._parse_c_include(node, source)
        return None

    def _parse_python_import(self, node, source: bytes) -> Optional[str]:
        """Python: from X import Y / import X"""
        for child in node.children:
            if child.type == "dotted_name":
                return self._node_text(child, source)
            if child.type == "relative_import":
                # from . import xxx → 相对导入
                module = self._node_text(child, source)
                return module
        return None

    def _parse_java_import(self, node, source: bytes) -> Optional[str]:
        """Java: import com.example.Class"""
        for child in node.children:
            if child.type == "scoped_identifier":
                return self._node_text(child, source)
        return None

    def _parse_js_import(self, node, source: bytes) -> Optional[str]:
        """JS/TS: import { X } from 'Y' / import X from 'Y'"""
        for child in node.children:
            if child.type == "string" or child.type == "string_literal":
                text = self._node_text(child, source)
                # 去掉引号
                return text.strip("'\"")
        # 查找 source 子节点里的 string
        source_node = self._find_child_by_type(node, "source")
        if source_node:
            for child in source_node.children:
                if child.type in ("string", "string_literal"):
                    return self._node_text(child, source).strip("'\"")
        return None

    def _parse_c_include(self, node, source: bytes) -> Optional[str]:
        """C/C++: #include <xxx> / #include "xxx" """
        for child in node.children:
            if child.type in ("string_literal", "system_lib_string"):
                text = self._node_text(child, source)
                return text.strip('<>"')
        return None

    def _parse_imported_symbols(self, node, source: bytes) -> list[str]:
        """提取 import 语句中具体导入的符号名列表"""
        symbols: list[str] = []
        if self.language == Language.PYTHON:
            # from X import A, B, C  → 收集 A, B, C
            # import X → symbols 为空（整模块导入）
            for child in node.children:
                if child.type == "import_from_statement":
                    # 递归处理
                    return self._parse_imported_symbols(child, source)
                if child.type == "dotted_name" and child.parent and child.parent.type != "module_name":
                    # import_from_statement 中 from 后面的不算，name 后面的才算
                    pass
                if child.type in ("import_list", "aliased_import"):
                    for sub in child.children:
                        if sub.type == "dotted_name":
                            symbols.append(self._node_text(sub, source))
                        elif sub.type == "aliased_import":
                            # from X import A as B → 取 A
                            for sc in sub.children:
                                if sc.type == "dotted_name":
                                    symbols.append(self._node_text(sc, source))
                                    break
            # 如果没找到 import_list，直接找顶层 name 子节点
            if not symbols:
                found_from = False
                for child in node.children:
                    if child.type == "dotted_name":
                        if found_from:
                            symbols.append(self._node_text(child, source))
                        else:
                            found_from = True  # 第一个 dotted_name 是模块名

        elif self.language == Language.JAVA:
            # import com.example.ClassName → 取最后一部分
            raw = self._node_text(node, source)
            if raw:
                parts = raw.rstrip(";").split(".")
                last = parts[-1].strip()
                if last != "*":
                    symbols.append(last)

        elif self.language in (Language.JAVASCRIPT, Language.TYPESCRIPT):
            # import { A, B } from 'module' / import Default from 'module'
            # export { A, B } from 'module' (re-export)
            for child in node.children:
                if child.type in ("import_clause", "export_clause"):
                    for sub in child.children:
                        if sub.type in ("named_imports", "named_exports"):
                            for spec in sub.children:
                                if spec.type in ("import_specifier", "export_specifier"):
                                    name_node = spec.children[0] if spec.children else None
                                    if name_node:
                                        symbols.append(self._node_text(name_node, source))
                        elif sub.type == "identifier":
                            symbols.append(self._node_text(sub, source))
                elif child.type == "identifier":
                    symbols.append(self._node_text(child, source))

        return symbols

    def _extract_call_target(self, node, source: bytes,
                              enclosing_class: str | None = None) -> Optional[str]:
        """
        C1: 提取调用目标，带类限定信息。
        返回格式:
          - "func"              裸调用
          - "ClassName.method"  self/this 调用（已知封闭类）
          - "?.method"          self/this 调用（未知封闭类）
          - "obj.method"        对象调用（obj ≈ 类名近似）
        """
        if not node.children:
            return None
        callee = node.children[0]

        if callee.type == "identifier":
            return self._node_text(callee, source)

        if callee.type in ("member_expression", "attribute", "field_expression"):
            obj_text = None
            method_text = None
            for child in callee.children:
                if child.type in ("identifier", "name") and child == callee.children[0]:
                    obj_text = self._node_text(child, source)
                elif child.type in ("property_identifier", "identifier", "field_identifier"):
                    if child != callee.children[0]:
                        method_text = self._node_text(child, source)

            if method_text:
                if obj_text in ("self", "this"):
                    qualifier = enclosing_class or "?"
                    return f"{qualifier}.{method_text}"
                elif obj_text == "super":
                    return f"super.{method_text}"
                elif obj_text:
                    return f"{obj_text}.{method_text}"
                return method_text

        return None

    def _extract_call_name(self, node, source: bytes) -> Optional[str]:
        """提取调用表达式中的函数名（向后兼容）"""
        return self._extract_call_target(node, source)

    def _extract_inheritance(self, node, source: bytes, file_path: Path) -> list[Edge]:
        """提取类继承边"""
        edges = []
        class_name = self._extract_name(node, source)
        if not class_name:
            return edges

        class_id = f"class:{file_path}::{class_name}"

        if self.language == Language.PYTHON:
            # class A(Base, Mixin): → argument_list 里的各个 identifier
            for child in node.children:
                if child.type == "argument_list":
                    for arg in child.children:
                        if arg.type == "identifier":
                            base_name = self._node_text(arg, source)
                            edges.append(Edge(
                                source=class_id,
                                target=f"class_ref:{base_name}",
                                edge_type=EdgeType.INHERITS,
                            ))

        elif self.language in (Language.JAVA, Language.JAVASCRIPT, Language.TYPESCRIPT):
            for child in node.children:
                if child.type in ("superclass", "class_heritage", "extends_type"):
                    for sub in child.children:
                        if sub.type in ("identifier", "type_identifier"):
                            base_name = self._node_text(sub, source)
                            edges.append(Edge(
                                source=class_id,
                                target=f"class_ref:{base_name}",
                                edge_type=EdgeType.INHERITS,
                            ))
                elif child.type == "super_interfaces":
                    for sub in child.children:
                        if sub.type in ("identifier", "type_identifier", "type_list"):
                            intf_name = self._node_text(sub, source)
                            edges.append(Edge(
                                source=class_id,
                                target=f"interface_ref:{intf_name}",
                                edge_type=EdgeType.IMPLEMENTS,
                            ))

        elif self.language == Language.CPP:
            for child in node.children:
                if child.type == "base_class_clause":
                    for sub in child.children:
                        if sub.type == "type_identifier":
                            base_name = self._node_text(sub, source)
                            edges.append(Edge(
                                source=class_id,
                                target=f"class_ref:{base_name}",
                                edge_type=EdgeType.INHERITS,
                            ))

        return edges

    # ═══════════════ 工具方法 ═══════════════

    @staticmethod
    def _node_text(node, source: bytes) -> str:
        """获取 AST 节点对应的源码文本"""
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    @staticmethod
    def _find_child_by_type(node, child_type: str):
        """查找指定类型的子节点"""
        for child in node.children:
            if child.type == child_type:
                return child
        return None

    def extract_dunder_all(self, source: bytes) -> list[str] | None:
        """
        提取 Python 文件中 __all__ 定义的导出列表。
        返回 None 表示未定义 __all__。
        """
        if self.language != Language.PYTHON:
            return None
        tree = self.parse(source)
        for node in tree.root_node.children:
            if node.type != "expression_statement":
                continue
            child = node.children[0] if node.children else None
            if not child or child.type != "assignment":
                continue
            # __all__ = [...]
            lhs = child.children[0] if child.children else None
            if lhs and lhs.type == "identifier" and self._node_text(lhs, source) == "__all__":
                # 从右侧列表中提取字符串
                rhs = child.children[-1] if len(child.children) >= 2 else None
                if rhs and rhs.type == "list":
                    names = []
                    for elem in rhs.children:
                        if elem.type == "string":
                            text = self._node_text(elem, source).strip("'\"")
                            if text:
                                names.append(text)
                    return names
        return None

    def extract_dynamic_imports(self, source: bytes, file_path: Path) -> list[Edge]:
        """
        提取 Python 动态 import: importlib.import_module('xxx')
        返回 IMPORTS 边，metadata 标记 dynamic=True。
        """
        if self.language != Language.PYTHON:
            return []
        edges = []
        file_id = f"file:{file_path}"
        text = source.decode("utf-8", errors="replace")
        # 匹配 importlib.import_module("xxx") 或 import_module("xxx")
        for m in re.finditer(
            r'(?:importlib\.)?import_module\(\s*["\']([^"\']+)["\']\s*\)', text
        ):
            module_path = m.group(1)
            edges.append(Edge(
                source=file_id,
                target=f"module:{module_path}",
                edge_type=EdgeType.IMPORTS,
                metadata={"raw": m.group(0), "dynamic": True},
            ))
        return edges
