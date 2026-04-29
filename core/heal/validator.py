"""
Phase3: CodeHeal — 编译/语法验证器
负责检测子仓库的编译错误
"""

from __future__ import annotations

import ast
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from config import HealConfig
from core.graph.schema import Language

logger = logging.getLogger(__name__)


def _find_filenames_and_linenums(text: str, fnames: list[str]) -> dict[str, set[int]]:
    """从编译器输出中提取 文件名:行号 对 (借鉴 aider/linter.py)
    用于从非结构化编译器输出中兜底提取错误位置信息
    """
    if not fnames:
        return {}
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(f) for f in fnames) + r"):(\d+)\b"
    )
    result: dict[str, set[int]] = {}
    for m in pattern.finditer(text):
        fname = m.group(1)
        if fname not in result:
            result[fname] = set()
        result[fname].add(int(m.group(2)))
    return result


@dataclass
class ValidationError:
    """验证错误"""
    file_path: Path
    line: int
    message: str
    severity: str = "error"  # error / warning


@dataclass
class ValidationResult:
    """验证结果"""
    success: bool
    errors: list[ValidationError] = field(default_factory=list)
    raw_output: str = ""


class BuildValidator:
    """构建验证器：尝试编译子仓库，收集错误"""

    def __init__(self, config: HealConfig, sub_repo_path: Path, language: Language,
                 prev_clean_mtimes: dict[str, float] | None = None):
        self.config = config
        self.sub_repo_path = sub_repo_path
        self.language = language
        # 增量编译: 上轮无错误文件的 {rel_path: mtime}
        self._prev_clean_mtimes: dict[str, float] = prev_clean_mtimes or {}
        # 本轮编译后更新的无错误文件 mtime（供下轮使用）
        self.clean_mtimes: dict[str, float] = {}

    def validate(self) -> ValidationResult:
        """执行构建验证"""
        validators = {
            Language.PYTHON: self._validate_python,
            Language.JAVA: self._validate_java,
            Language.JAVASCRIPT: self._validate_js,
            Language.TYPESCRIPT: self._validate_ts,
            Language.C: self._validate_c,
            Language.CPP: self._validate_cpp,
        }
        validator = validators.get(self.language)
        if not validator:
            logger.warning(f"未实现 {self.language.value} 的验证器")
            return ValidationResult(success=True)
        result = validator()

        # 附加: Python AST 引用验证 — 检查子仓库中未定义的名称
        if self.language == Language.PYTHON:
            ref_errors = self._check_python_references()
            if ref_errors:
                result.errors.extend(ref_errors)
                result.success = False
            # U6: pyflakes 未定义名称检测
            pyflakes_errors = self._check_python_undefined_names()
            if pyflakes_errors:
                result.errors.extend(pyflakes_errors)
                result.success = False

        return result

    def _check_python_references(self) -> list[ValidationError]:
        """
        检查 Python 子仓库中的引用完整性：
        扫描所有 from X import Y 语句的 Y，验证目标模块中是否定义了 Y。
        """
        errors: list[ValidationError] = []
        # 首先收集每个模块导出的名称
        module_exports: dict[str, set[str]] = {}  # module_name → {exported_names}
        for py_file in self.sub_repo_path.rglob("*.py"):
            rel = py_file.relative_to(self.sub_repo_path)
            module_name = str(rel.with_suffix("")).replace("\\", ".").replace("/", ".")
            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source)
                names: set[str] = set()
                for node in ast.iter_child_nodes(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        names.add(node.name)
                    elif isinstance(node, ast.ClassDef):
                        names.add(node.name)
                    elif isinstance(node, ast.Assign):
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                names.add(target.id)
                module_exports[module_name] = names
            except (OSError, SyntaxError):
                continue

        # 检查 from X import Y 的 Y 是否在 X 中定义
        for py_file in self.sub_repo_path.rglob("*.py"):
            rel = py_file.relative_to(self.sub_repo_path)
            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                        target_module = node.module.replace(".", "/")
                        # 找到对应的模块
                        for mod_name, exports in module_exports.items():
                            if mod_name.replace(".", "/") == target_module:
                                for alias in node.names:
                                    if alias.name != "*" and alias.name not in exports:
                                        errors.append(ValidationError(
                                            file_path=rel, line=node.lineno,
                                            message=f"ImportError: cannot import name '{alias.name}' from '{node.module}'",
                                            severity="warning",
                                        ))
                                break
            except (OSError, SyntaxError):
                continue

        return errors

    def _check_python_undefined_names(self) -> list[ValidationError]:
        """U6: 使用 pyflakes 检测 Python 文件中的未定义名称

        仅报告 UndefinedName 类型的错误（裁剪可能删除了被引用的定义）。
        注意: 不再跳过 [CodePrune] 行 — undefined name 无论来源都应被报告。
        """
        try:
            from pyflakes.api import check as pyflakes_check
            from pyflakes.messages import UndefinedName
        except ImportError:
            logger.debug("pyflakes 未安装，跳过未定义名称检测")
            return []

        errors: list[ValidationError] = []
        for py_file in self.sub_repo_path.rglob("*.py"):
            rel = py_file.relative_to(self.sub_repo_path)
            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            # pyflakes 需要可解析的源码
            try:
                ast.parse(source)
            except SyntaxError:
                continue  # 语法错误由 _validate_python 处理

            # 使用 pyflakes 检查
            import io
            warning_stream = io.StringIO()
            try:
                pyflakes_check(source, str(rel), warning_stream)
            except Exception:
                continue

            output = warning_stream.getvalue()
            if not output:
                continue

            # 解析 pyflakes 输出: "filename:lineno:colno undefined name 'X'"
            src_lines = source.splitlines()
            for line in output.splitlines():
                m = re.match(r'.+:(\d+):\d+\s+(.+)', line)
                if not m:
                    continue
                lineno = int(m.group(1))
                msg = m.group(2)

                # 只关注 UndefinedName
                if "undefined name" not in msg.lower():
                    continue

                # 只跳过引用行本身是注释行的情况（如 # [CodePrune] removed 行自身）
                # 不跳过引用被删导入名称的代码行
                if 0 < lineno <= len(src_lines):
                    src_line = src_lines[lineno - 1].strip()
                    if src_line.startswith("#"):
                        continue  # 注释行中的 undefined name 不是真正的引用

                errors.append(ValidationError(
                    file_path=rel, line=lineno,
                    message=msg,
                    severity="error",  # undefined name 视为 error，不再是 warning
                ))

        return errors

    def _validate_python(self) -> ValidationResult:
        """Python: AST 语法检查 + import 存在性验证"""
        errors = []
        for py_file in self.sub_repo_path.rglob("*.py"):
            rel = py_file.relative_to(self.sub_repo_path)
            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
                # AST 语法检查（无需 subprocess）
                try:
                    tree = ast.parse(source, filename=str(rel))
                except SyntaxError as e:
                    errors.append(ValidationError(
                        file_path=rel, line=e.lineno or 0,
                        message=f"SyntaxError: {e.msg}",
                    ))
                    continue
                # import 存在性检查
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            self._check_python_import(
                                alias.name, rel, node.lineno, errors,
                            )
                    elif isinstance(node, ast.ImportFrom):
                        if node.module and node.level == 0:
                            self._check_python_import(
                                node.module, rel, node.lineno, errors,
                            )
            except OSError as e:
                errors.append(ValidationError(file_path=rel, line=0, message=str(e)))
        return ValidationResult(success=len(errors) == 0, errors=errors)

    def _check_python_import(
        self, module_name: str, source_file: Path,
        line: int, errors: list[ValidationError],
    ) -> None:
        """检查 Python import 目标是否存在于子仓库或标准库"""
        from parsers.import_resolver import PYTHON_STDLIB
        top = module_name.split(".")[0]
        # 标准库 → OK
        if top in PYTHON_STDLIB:
            return
        # 子仓库内文件 → OK
        parts = module_name.replace(".", "/")
        for candidate in (
            self.sub_repo_path / f"{parts}.py",
            self.sub_repo_path / parts / "__init__.py",
        ):
            if candidate.exists():
                return
        # 尝试检查顶层模块
        top_path = self.sub_repo_path / f"{top}.py"
        top_pkg = self.sub_repo_path / top / "__init__.py"
        if top_path.exists() or top_pkg.exists():
            return
        # 第三方包 — 检查是否可导入（宽松处理，不报错）
        try:
            import importlib
            importlib.import_module(top)
            return
        except ImportError:
            pass
        errors.append(ValidationError(
            file_path=source_file, line=line,
            message=f"ImportError: No module named '{module_name}' — "
                    f"not in sub-repo, stdlib, or installed packages",
            severity="warning",
        ))

    def _validate_ts(self) -> ValidationResult:
        """TypeScript: tsc --noEmit"""
        try:
            result = subprocess.run(
                ["npx", "tsc", "--noEmit", "--pretty", "false"],
                capture_output=True, text=True, timeout=120,
                cwd=self.sub_repo_path,
            )
            errors = self._parse_tsc_output(result.stdout + result.stderr)
            return ValidationResult(success=result.returncode == 0, errors=errors, raw_output=result.stdout)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return ValidationResult(success=False, errors=[ValidationError(Path("."), 0, str(e))])

    def _validate_js(self) -> ValidationResult:
        """JavaScript: 基本语法检查（node --check）"""
        errors = []
        for js_file in self.sub_repo_path.rglob("*.js"):
            rel = js_file.relative_to(self.sub_repo_path)
            try:
                result = subprocess.run(
                    ["node", "--check", str(js_file)],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    errors.append(ValidationError(file_path=rel, line=0, message=result.stderr.strip()))
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                errors.append(ValidationError(file_path=rel, line=0, message=str(e)))
        return ValidationResult(success=len(errors) == 0, errors=errors)

    def _validate_java(self) -> ValidationResult:
        """Java: javac 编译检查 + 构建工具感知 classpath"""
        java_files = list(self.sub_repo_path.rglob("*.java"))
        if not java_files:
            return ValidationResult(success=True)
        try:
            build_dir = tempfile.mkdtemp(prefix="codeprune_build_")
            javac_args = ["javac", "-d", build_dir]

            # 检测构建工具并推断 classpath
            classpath = self._detect_java_classpath()
            if classpath:
                javac_args.extend(["-cp", classpath])

            # 检测源目录（src/main/java 等）
            source_path = self._detect_java_source_path()
            if source_path:
                javac_args.extend(["-sourcepath", str(source_path)])

            javac_args.extend([str(f) for f in java_files])
            result = subprocess.run(
                javac_args,
                capture_output=True, text=True, timeout=120,
                cwd=self.sub_repo_path,
            )
            errors = self._parse_javac_output(result.stderr)

            # 对于缺少第三方依赖导致的 "cannot find symbol" 错误标记为 warning
            for err in errors:
                if "cannot find symbol" in err.message:
                    err.severity = "warning"

            return ValidationResult(success=result.returncode == 0, errors=errors, raw_output=result.stderr)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return ValidationResult(success=False, errors=[ValidationError(Path("."), 0, str(e))])

    def _detect_java_classpath(self) -> str:
        """检测 Maven/Gradle 项目的依赖目录，组装 classpath"""
        classpath_parts = []

        # Maven: target/dependency/*.jar + target/classes
        target_dep = self.sub_repo_path / "target" / "dependency"
        if target_dep.is_dir():
            for jar in target_dep.glob("*.jar"):
                classpath_parts.append(str(jar))
        target_classes = self.sub_repo_path / "target" / "classes"
        if target_classes.is_dir():
            classpath_parts.append(str(target_classes))

        # Gradle: build/libs/*.jar + build/classes
        build_libs = self.sub_repo_path / "build" / "libs"
        if build_libs.is_dir():
            for jar in build_libs.glob("*.jar"):
                classpath_parts.append(str(jar))
        build_classes = self.sub_repo_path / "build" / "classes" / "java" / "main"
        if build_classes.is_dir():
            classpath_parts.append(str(build_classes))

        # 添加源码根路径
        for src_dir in ("src/main/java", "src", "."):
            candidate = self.sub_repo_path / src_dir
            if candidate.is_dir():
                classpath_parts.append(str(candidate))
                break

        if classpath_parts:
            sep = ";" if __import__("os").name == "nt" else ":"
            return sep.join(classpath_parts)
        return ""

    def _detect_java_source_path(self) -> Path | None:
        """检测 Java 源码根目录"""
        for candidate in ("src/main/java", "src", "."):
            path = self.sub_repo_path / candidate
            if path.is_dir() and list(path.rglob("*.java")):
                return path
        return None

    def _validate_c(self) -> ValidationResult:
        return self._validate_c_cpp("gcc")

    def _validate_cpp(self) -> ValidationResult:
        return self._validate_c_cpp("g++")

    def _validate_c_cpp(self, compiler: str) -> ValidationResult:
        """C/C++: 编译检查（增量: 跳过未修改且上轮无错误的文件）"""
        ext = "*.c" if compiler == "gcc" else "*.cpp"
        src_files = list(self.sub_repo_path.rglob(ext))
        if not src_files:
            return ValidationResult(success=True)

        # 增量: 检查头文件是否有变化（头文件变则全量重编）
        header_changed = False
        for hext in ("*.h", "*.hpp", "*.hxx"):
            for hf in self.sub_repo_path.rglob(hext):
                rel = str(hf.relative_to(self.sub_repo_path))
                try:
                    mt = hf.stat().st_mtime
                except OSError:
                    continue
                prev_mt = self._prev_clean_mtimes.get(rel)
                if prev_mt is None or mt != prev_mt:
                    header_changed = True
                    break
            if header_changed:
                break

        # 筛选需要编译的文件
        if self._prev_clean_mtimes and not header_changed:
            files_to_check: list[Path] = []
            skipped: list[Path] = []
            for f in src_files:
                rel = str(f.relative_to(self.sub_repo_path))
                try:
                    mt = f.stat().st_mtime
                except OSError:
                    files_to_check.append(f)
                    continue
                prev_mt = self._prev_clean_mtimes.get(rel)
                if prev_mt is not None and mt == prev_mt:
                    skipped.append(f)
                else:
                    files_to_check.append(f)
            if skipped:
                logger.debug(
                    f"增量编译: 跳过 {len(skipped)} 个未修改文件, "
                    f"检查 {len(files_to_check)} 个"
                )
            if not files_to_check:
                # 所有文件未变化 → 沿用上轮结果（无错误）
                self._update_clean_mtimes(src_files)
                return ValidationResult(success=True)
        else:
            files_to_check = src_files

        # 收集所有包含头文件的目录作为 -I 选项
        header_dirs: set[Path] = set()
        for hext in ("*.h", "*.hpp", "*.hxx"):
            for hf in self.sub_repo_path.rglob(hext):
                header_dirs.add(hf.parent)
        # Also add parent "include/" directories so both
        # #include "common.h" and #include "core/common.h" resolve
        for d in list(header_dirs):
            if d.name != "include":
                for parent in d.parents:
                    if parent.name == "include" and parent != self.sub_repo_path:
                        header_dirs.add(parent)
                        break
        include_flags = []
        for d in header_dirs:
            include_flags += ["-I", str(d)]
        try:
            result = subprocess.run(
                [compiler, "-fsyntax-only"] + include_flags + [str(f) for f in files_to_check],
                capture_output=True, text=True, timeout=120,
                cwd=self.sub_repo_path,
            )
            errors = self._parse_gcc_output(result.stderr)
            # 更新无错误文件的 mtime 缓存
            # 注意: gcc 可能输出绝对路径，需要统一为相对路径
            error_files: set[str] = set()
            for e in errors:
                try:
                    rel = Path(e.file_path).relative_to(self.sub_repo_path)
                    error_files.add(str(rel))
                except ValueError:
                    error_files.add(str(e.file_path))
            self._update_clean_mtimes(
                [f for f in files_to_check
                 if str(f.relative_to(self.sub_repo_path)) not in error_files],
                # 保留跳过文件的缓存
                preserve_prev=not header_changed,
            )
            return ValidationResult(success=result.returncode == 0, errors=errors, raw_output=result.stderr)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return ValidationResult(success=False, errors=[ValidationError(Path("."), 0, str(e))])

    def _update_clean_mtimes(
        self, clean_files: list[Path], preserve_prev: bool = False,
    ) -> None:
        """更新已通过编译（无错误）的文件 mtime 缓存"""
        if preserve_prev:
            self.clean_mtimes.update(self._prev_clean_mtimes)
        for f in clean_files:
            rel = str(f.relative_to(self.sub_repo_path))
            try:
                self.clean_mtimes[rel] = f.stat().st_mtime
            except OSError:
                pass
        # 也缓存头文件
        for hext in ("*.h", "*.hpp", "*.hxx"):
            for hf in self.sub_repo_path.rglob(hext):
                rel = str(hf.relative_to(self.sub_repo_path))
                if rel not in self.clean_mtimes:
                    try:
                        self.clean_mtimes[rel] = hf.stat().st_mtime
                    except OSError:
                        pass

    # ── 输出解析 ──

    # TypeScript: src/file.ts(10,5): error TS2304: Cannot find name 'xxx'.
    _TSC_ERROR_RE = re.compile(r"^(.+?)\((\d+),(\d+)\):\s+error\s+(TS\d+):\s+(.+)$")

    def _parse_tsc_output(self, output: str) -> list[ValidationError]:
        errors = []
        for line in output.strip().splitlines():
            m = self._TSC_ERROR_RE.match(line.strip())
            if m:
                errors.append(ValidationError(
                    file_path=Path(m.group(1)),
                    line=int(m.group(2)),
                    message=f"{m.group(4)}: {m.group(5)}",
                ))
            elif "error TS" in line:
                # 兑底: 从未匹配行中提取文件名:行号
                finfo = _find_filenames_and_linenums(
                    line, [str(f.relative_to(self.sub_repo_path))
                           for f in self.sub_repo_path.rglob("*.ts")]
                )
                if finfo:
                    fname, linenums = next(iter(finfo.items()))
                    errors.append(ValidationError(
                        file_path=Path(fname), line=min(linenums), message=line.strip(),
                    ))
                else:
                    errors.append(ValidationError(file_path=Path("."), line=0, message=line.strip()))
        return errors

    # Java: File.java:10: error: cannot find symbol
    _JAVAC_ERROR_RE = re.compile(r"^(.+?):(\d+):\s+error:\s+(.+)$")

    def _parse_javac_output(self, output: str) -> list[ValidationError]:
        errors = []
        for line in output.strip().splitlines():
            m = self._JAVAC_ERROR_RE.match(line.strip())
            if m:
                file_path = Path(m.group(1))
                errors.append(ValidationError(
                    file_path=file_path,
                    line=int(m.group(2)),
                    message=m.group(3),
                ))
            elif ": error:" in line:
                finfo = _find_filenames_and_linenums(
                    line, [str(f.relative_to(self.sub_repo_path))
                           for f in self.sub_repo_path.rglob("*.java")]
                )
                if finfo:
                    fname, linenums = next(iter(finfo.items()))
                    errors.append(ValidationError(
                        file_path=Path(fname), line=min(linenums), message=line.strip(),
                    ))
                else:
                    errors.append(ValidationError(file_path=Path("."), line=0, message=line.strip()))
        return errors

    # GCC/G++: file.c:10:5: error: undeclared identifier 'xxx'
    _GCC_ERROR_RE = re.compile(r"^(.+?):(\d+):(\d+):\s+(?:fatal\s+)?error:\s+(.+)$")

    def _parse_gcc_output(self, output: str) -> list[ValidationError]:
        errors = []
        for line in output.strip().splitlines():
            m = self._GCC_ERROR_RE.match(line.strip())
            if m:
                errors.append(ValidationError(
                    file_path=Path(m.group(1)),
                    line=int(m.group(2)),
                    message=m.group(4),
                ))
            elif ": error:" in line or ": fatal error:" in line:
                finfo = _find_filenames_and_linenums(
                    line, [str(f.relative_to(self.sub_repo_path))
                           for f in self.sub_repo_path.rglob("*.c")
                           ] + [str(f.relative_to(self.sub_repo_path))
                                for f in self.sub_repo_path.rglob("*.cpp")]
                )
                if finfo:
                    fname, linenums = next(iter(finfo.items()))
                    errors.append(ValidationError(
                        file_path=Path(fname), line=min(linenums), message=line.strip(),
                    ))
                else:
                    errors.append(ValidationError(file_path=Path("."), line=0, message=line.strip()))
        return errors
