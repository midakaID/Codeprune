"""
CodePrune 全局配置
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ───────── 指令理解结构 ─────────

@dataclass
class SubFeature:
    """一个独立的子功能需求（LLM grounded 分析产出）"""
    description: str          # 功能描述（基于图谱上下文的 grounded 描述）
    root_entities: list[str]  # LLM 从候选列表中选出的 implementation roots (qualified_name)
    reasoning: str            # 选择理由
    req_id: str = ""          # 需求追踪 ID ("R1", "R2" ...)
    covered_nodes: set = field(default_factory=set)    # 闭包后回填：该需求覆盖的节点
    coverage_ratio: float = 0.0                        # 闭包后回填：CORE 节点覆盖率


@dataclass
class InstructionAnalysis:
    """LLM 对指令的 grounded 分析结果"""
    original: str                   # 原始指令
    sub_features: list[SubFeature]  # 独立子功能列表
    out_of_scope: list[str]         # 明确不在范围内的目录/模块
    anchor_strategy: str            # "focused" | "distributed" | "broad"
    excluded_symbols: list[str] = field(default_factory=list)   # F28: 方法级排除 (如 "TicketService.rejectTicket")
    restricted_classes: list[str] = field(default_factory=list)  # F28: 仅保留“被链路使用的方法”的类


# ───────── LLM / 图谱 / 剪枝配置 ─────────

@dataclass
class ModelEndpoint:
    """单个模型端点配置"""
    model: str = "gpt-4o"
    temperature: float = 0.2
    max_tokens: int = 4096


@dataclass
class LLMConfig:
    """LLM 调用配置"""
    provider: str = "openai"                    # openai / anthropic / 第三方 (兼容 OpenAI API)
    api_key: Optional[str] = None               # 优先从环境变量读取
    api_base: Optional[str] = None              # 第三方 API 端点 (如 https://api.deepseek.com/v1)
    timeout: int = 60                           # 秒
    max_retries: int = 3
    cache_enabled: bool = True                  # 缓存 LLM 调用结果
    cache_dir: Optional[Path] = None

    # Embedding 独立端点 (可选，当主 API 不支持 embedding 时使用)
    embedding_api_base: Optional[str] = None
    embedding_api_key: Optional[str] = None

    # 双模型端点
    reasoning: ModelEndpoint = field(default_factory=lambda: ModelEndpoint(
        model="gpt-5.4", temperature=0.2, max_tokens=4096,
    ))
    fast: ModelEndpoint = field(default_factory=lambda: ModelEndpoint(
        model="gpt-5.4-mini", temperature=0.3, max_tokens=2048,
    ))


@dataclass
class GraphConfig:
    """Phase1 图谱构建配置"""
    # 粒度控制
    initial_granularity: str = "file"           # file / class / function
    lazy_resolution: bool = False               # 默认全量解析（lazy 模式有 recall 损失风险）

    # 语义层
    enable_semantic: bool = True
    enable_embedding_diagnostics: bool = False  # B3: Phase1 结束后运行 embedding 质量诊断
    summary_batch_size: int = 20                # 一次批量摘要的节点数
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    enable_embedding: bool = True               # 独立控制 embedding 生成（False 时 anchor 用 tag 匹配降级）

    # 忽略规则
    ignore_patterns: list[str] = field(default_factory=lambda: [
        "node_modules", "__pycache__", ".git", ".venv", "venv",
        "dist", "build", "target", ".idea", ".vscode",
        "*.min.js", "*.map", "*.lock",
    ])
    max_file_size_kb: int = 500                 # 超过此大小的文件跳过解析


@dataclass
class ClosurePolicy:
    """闭包求解策略参数"""
    # ── 语义定界阈值 ──
    core_threshold_factor: float = 0.75       # CORE = 最弱锚点 × 此值
    peripheral_threshold_factor: float = 0.50  # PERIPHERAL = CORE × 此值
    core_floor: float = 0.30                   # CORE 阈值保底
    peripheral_floor: float = 0.15             # PERIPHERAL 阈值保底
    anchor_percentile: float = 0.25            # 用锚点分位数替代最弱锚点
    core_corpus_percentile: float = 0.90       # 语料分布下 CORE 阈值下界
    peripheral_corpus_percentile: float = 0.75 # 语料分布下 PERIPHERAL 阈值下界
    core_margin: float = 0.06                  # 锚点参照线到 CORE 的退让边界
    peripheral_margin: float = 0.14            # 锚点参照线到 PERIPHERAL 的退让边界
    max_semantic_scope_ratio: float = 0.45     # BFS 前 CORE+PERIPHERAL 的最大占比
    threshold_tightening_step: float = 0.03    # 语义范围过宽时的自动收紧步长

    # ── 独占性 ──
    exclusivity_include_threshold: float = 0.5  # PERIPHERAL 区域独占性高于此值 → include
    exclusivity_rule_threshold: float = 0.8     # 规则层独占性高于此值 → include

    # ── 缺口仲裁 ──
    small_code_threshold: int = 20              # 行数低于此 → 直接 include
    infra_in_degree_threshold: int = 25         # 入度高于此 → 直接 stub
    prefer_stub: bool = True                    # 边界节点不确定时优先 stub
    max_gap_iterations: int = 3                 # 缺口仲裁最大迭代轮次

    # ── 闭包大小控制 ──
    max_closure_ratio: float = 0.5              # 代码行数占比硬上限（触发自动收紧）
    size_check_interval: int = 50               # 每增加 N 个节点检查一次大小

    # ── 边置信度 ──
    min_edge_confidence: float = 0.6            # CALLS/USES 边低于此值不自动 BFS 传播

    # ── 用户控制 ──
    exclude_keywords: list[str] = field(default_factory=list)


@dataclass
class PruneConfig:
    """Phase2 剪枝配置"""
    # 锚点定位
    anchor_top_k: int = 20                      # 语义检索召回数
    anchor_confidence_threshold: float = 0.6    # LLM 验证置信度阈值
    file_anchor_seed_budget: int = 3            # FILE 锚点最多展开为多少个函数 seed
    class_anchor_seed_budget: int = 4           # CLASS 锚点最多展开为多少个函数 seed
    anchor_expansion_warning_ratio: float = 2.0 # 锚点展开倍数超过该值时记录告警
    enable_selection_diagnostics: bool = True   # 输出 selection_diagnostics.json

    # Scope 分类策略
    scope_strategy: str = "llm_hierarchical"    # "llm_hierarchical" (新默认) | "embedding_threshold" (旧模式)

    # 闭包求解
    max_closure_depth: int = 50                 # 防止无限传递
    soft_dep_auto_include: bool = False         # 软依赖默认不自动加入
    closure_policy: ClosurePolicy = field(default_factory=ClosurePolicy)


@dataclass
class HealConfig:
    """Phase3 自愈配置"""
    max_heal_rounds: int = 8                    # 最大修复轮次 (含 runtime/functional 循环需更多轮次)
    enable_build_validation: bool = True        # 是否尝试编译验证
    enable_completeness_check: bool = True      # 功能完整性检查
    enable_fidelity_check: bool = True          # 真实性校验
    enable_test_validation: bool = True         # U8: 自动检测并运行测试
    diff_tolerance: float = 0.1                 # 允许的与原仓库代码差异比例
    enable_finalize: bool = True                # Phase3 后生成 requirements + README
    enable_reference_audit: bool = True         # Phase 2.6: 引用审计与清理
    enable_runtime_validation: bool = True      # Layer 2.0: 确定性 import 扫描 + 运行时修复循环
    runtime_import_timeout: int = 15            # 单模块 import 超时(秒)
    enable_boot_validation: bool = True         # Layer 2.5: 启动验证
    boot_timeout: int = 15                      # 启动验证超时(秒)
    boot_max_entry_points: int = 5              # 最多尝试的入口点数
    boot_script_max_retries: int = 2            # 启动脚本失败重试次数
    enable_functional_validation: bool = True   # Layer 3.5: 功能验证(默认开启, 支持真实运行闭环)
    functional_timeout: int = 30                # 功能验证脚本超时(秒)
    functional_script_max_retries: int = 2      # 功能脚本失败重试次数


@dataclass
class CodePruneConfig:
    """顶层配置"""
    repo_path: Path = Path(".")                 # 待剪枝仓库路径
    output_path: Path = Path("./output")        # 子仓库输出路径
    user_instruction: str = ""                  # 用户自然语言指令
    verbose: bool = False
    log_dir: Optional[Path] = None

    llm: LLMConfig = field(default_factory=LLMConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    prune: PruneConfig = field(default_factory=PruneConfig)
    heal: HealConfig = field(default_factory=HealConfig)

    # 运行时填充（Phase 2.0 产出）
    instruction_analysis: Optional[InstructionAnalysis] = field(default=None, repr=False)

    def __post_init__(self):
        self.repo_path = Path(self.repo_path).resolve()
        self.output_path = Path(self.output_path).resolve()
        if self.log_dir is None:
            self.log_dir = self.output_path / ".codeprune_logs"
        if self.llm.cache_dir is None:
            self.llm.cache_dir = self.output_path / ".codeprune_cache"
        self._validate()

    def _validate(self) -> None:
        """P2: 配置参数校验，启动时捕获无效配置"""
        import os
        errors: list[str] = []

        # API key 检查
        api_key = self.llm.api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            errors.append(
                "LLM API key 未配置: 请设置 llm.api_key 或 OPENAI_API_KEY 环境变量"
            )

        # 数值范围校验
        p = self.prune.closure_policy
        if not (0 < p.core_threshold_factor <= 1):
            errors.append(f"core_threshold_factor={p.core_threshold_factor} 应在 (0, 1] 内")
        if not (0 < p.peripheral_threshold_factor <= 1):
            errors.append(f"peripheral_threshold_factor={p.peripheral_threshold_factor} 应在 (0, 1] 内")
        for name in ("anchor_percentile", "core_corpus_percentile", "peripheral_corpus_percentile"):
            value = getattr(p, name)
            if not (0 < value <= 1):
                errors.append(f"{name}={value} 应在 (0, 1] 内")
        if p.threshold_tightening_step <= 0:
            errors.append(f"threshold_tightening_step={p.threshold_tightening_step} 应大于 0")
        if not (0 < p.max_semantic_scope_ratio <= 1):
            errors.append(f"max_semantic_scope_ratio={p.max_semantic_scope_ratio} 应在 (0, 1] 内")
        if not (0 < p.max_closure_ratio <= 1):
            errors.append(f"max_closure_ratio={p.max_closure_ratio} 应在 (0, 1] 内")
        if not (0 < self.prune.anchor_confidence_threshold <= 1):
            errors.append(
                f"anchor_confidence_threshold={self.prune.anchor_confidence_threshold} 应在 (0, 1] 内"
            )
        if self.prune.file_anchor_seed_budget <= 0:
            errors.append(
                f"file_anchor_seed_budget={self.prune.file_anchor_seed_budget} 应大于 0"
            )
        if self.prune.class_anchor_seed_budget <= 0:
            errors.append(
                f"class_anchor_seed_budget={self.prune.class_anchor_seed_budget} 应大于 0"
            )
        if self.prune.anchor_expansion_warning_ratio <= 1:
            errors.append(
                f"anchor_expansion_warning_ratio={self.prune.anchor_expansion_warning_ratio} 应大于 1"
            )
        if not (0 < self.heal.diff_tolerance <= 1):
            errors.append(f"diff_tolerance={self.heal.diff_tolerance} 应在 (0, 1] 内")

        if errors:
            raise ValueError("配置校验失败:\n  - " + "\n  - ".join(errors))

    # ── 配置序列化 ──

    def to_dict(self) -> dict:
        """导出为可序列化字典（不含运行时字段）"""
        return _to_serializable(self)

    @classmethod
    def from_dict(cls, data: dict) -> CodePruneConfig:
        """从字典创建配置（支持部分字段）"""
        d = {k: v for k, v in data.items() if k != "instruction_analysis"}
        if "llm" in d and isinstance(d["llm"], dict):
            llm_d = d["llm"].copy()
            for k in ("cache_dir",):
                if k in llm_d and llm_d[k] is not None:
                    llm_d[k] = Path(llm_d[k])
            if "reasoning" in llm_d and isinstance(llm_d["reasoning"], dict):
                llm_d["reasoning"] = ModelEndpoint(**llm_d["reasoning"])
            if "fast" in llm_d and isinstance(llm_d["fast"], dict):
                llm_d["fast"] = ModelEndpoint(**llm_d["fast"])
            d["llm"] = LLMConfig(**llm_d)
        if "graph" in d and isinstance(d["graph"], dict):
            d["graph"] = GraphConfig(**d["graph"])
        if "prune" in d and isinstance(d["prune"], dict):
            pd = d["prune"].copy()
            if "closure_policy" in pd and isinstance(pd["closure_policy"], dict):
                pd["closure_policy"] = ClosurePolicy(**pd["closure_policy"])
            d["prune"] = PruneConfig(**pd)
        if "heal" in d and isinstance(d["heal"], dict):
            d["heal"] = HealConfig(**d["heal"])
        for k in ("repo_path", "output_path", "log_dir"):
            if k in d and d[k] is not None:
                d[k] = Path(d[k])
        return cls(**d)

    def to_yaml(self, path: Path) -> None:
        """导出为 YAML 配置文件"""
        import yaml
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(self.to_dict(), f, default_flow_style=False,
                      allow_unicode=True, sort_keys=False)

    @classmethod
    def from_yaml(cls, path: Path) -> CodePruneConfig:
        """从 YAML 文件加载配置"""
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data or {})


def _to_serializable(obj: Any) -> Any:
    """递归将 dataclass 转为可序列化 dict"""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            f.name: _to_serializable(getattr(obj, f.name))
            for f in dataclasses.fields(obj)
            if f.name != "instruction_analysis"
        }
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, list):
        return [_to_serializable(v) for v in obj]
    return obj
