"""
CodePrune CLI 入口
子命令: run (全流程) | graph (Phase1) | prune (Phase2) | heal (Phase3) | config (配置管理)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from config import CodePruneConfig, LLMConfig, ModelEndpoint, GraphConfig, PruneConfig, HealConfig
from pipeline import Pipeline

# ── 配置模板（带注释） ──

CONFIG_TEMPLATE = """\
# CodePrune 配置文件
# 使用: codeprune run --config codeprune.yaml <repo_path> <instruction>

# ── 基本配置 ──
# repo_path 和 output_path 通常通过 CLI 参数指定，也可写入配置文件
# repo_path: ./my-repo
# output_path: ./output
verbose: false

# ── LLM 配置 ──
llm:
  provider: openai              # openai / anthropic / 第三方 (兼容 OpenAI API)
  # api_key: sk-xxx             # 或设置环境变量 OPENAI_API_KEY
  # api_base: https://api.deepseek.com/v1  # 第三方 API 端点
  timeout: 60
  max_retries: 3
  cache_enabled: true

  # 推理模型 — 指令理解, 缺口仲裁, 软依赖判断, 代码修复
  reasoning:
    model: gpt-5.4
    temperature: 0.2
    max_tokens: 4096

  # 快速模型 — 函数/文件/簇摘要, 锚点验证
  fast:
    model: gpt-5.4-mini
    temperature: 0.3
    max_tokens: 2048

# ── Phase1 图谱构建 ──
graph:
  initial_granularity: file     # file / class / function
  lazy_resolution: false
  enable_semantic: true
  enable_embedding_diagnostics: false
  summary_batch_size: 20
  embedding_model: text-embedding-3-small
  embedding_dim: 1536
  max_file_size_kb: 500
  ignore_patterns:
    - node_modules
    - __pycache__
    - .git
    - .venv
    - venv
    - dist
    - build
    - target
    - .idea
    - .vscode
    - "*.min.js"
    - "*.map"
    - "*.lock"

# ── Phase2 剪枝 ──
prune:
  anchor_top_k: 20
  anchor_confidence_threshold: 0.6
  max_closure_depth: 50
  soft_dep_auto_include: false
  closure_policy:
    core_threshold_factor: 0.75
    peripheral_threshold_factor: 0.50
    core_floor: 0.30
    peripheral_floor: 0.15
    exclusivity_include_threshold: 0.5
    exclusivity_rule_threshold: 0.8
    small_code_threshold: 20
    infra_in_degree_threshold: 25
    prefer_stub: true
    max_gap_iterations: 3
    max_closure_ratio: 0.5
    size_check_interval: 50
    min_edge_confidence: 0.6

# ── Phase3 自愈 ──
heal:
  max_heal_rounds: 8
  enable_build_validation: true
  enable_completeness_check: true
  enable_fidelity_check: true
  enable_test_validation: true
  enable_runtime_validation: true
  enable_boot_validation: true
  enable_functional_validation: true
  diff_tolerance: 0.1
  enable_finalize: true              # Phase3 后生成 requirements + README
"""


def setup_logging(verbose: bool, log_dir: Path | None = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "codeprune.log", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        handlers.append(fh)

    logging.basicConfig(level=level, format=fmt, handlers=handlers)


# ── 参数组构建 ──

def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("repo", type=str, help="待剪枝仓库路径")
    p.add_argument("-o", "--output", type=str, default=None, help="输出路径 (默认: <repo>/../output)")
    p.add_argument("-v", "--verbose", action="store_true", help="详细日志输出")
    p.add_argument("-c", "--config", type=str, default=None, help="YAML 配置文件路径")


def _add_llm_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--provider", type=str, default="openai", choices=["openai", "anthropic"])
    p.add_argument("--model", type=str, default="gpt-5.4", help="推理模型名 (指令理解/缺口仲裁/代码修复)")
    p.add_argument("--fast-model", type=str, default=None, help="快速模型名 (摘要/锚点验证); 默认 gpt-5.4-mini")
    p.add_argument("--api-key", type=str, default=None, help="API Key (或设置环境变量)")
    p.add_argument("--api-base", type=str, default=None, help="第三方 API 端点 (如 https://api.deepseek.com/v1)")


_AUTO_CONFIG_NAMES = ["codeprune.yaml", "codeprune.yml"]


def _build_config(args: argparse.Namespace, need_instruction: bool = False) -> CodePruneConfig:
    config_file = getattr(args, "config", None)

    # 自动发现: 当前目录下的 codeprune.yaml
    if not config_file:
        for name in _AUTO_CONFIG_NAMES:
            candidate = Path(name)
            if candidate.is_file():
                config_file = str(candidate)
                logging.getLogger(__name__).info(f"自动加载配置文件: {candidate.resolve()}")
                break

    if config_file:
        # 从 YAML 加载，CLI 参数仅覆盖基本字段
        config = CodePruneConfig.from_yaml(Path(config_file))
        if hasattr(args, "repo") and args.repo:
            config.repo_path = Path(args.repo).resolve()
        if args.output:
            config.output_path = Path(args.output).resolve()
        instruction = getattr(args, "instruction", None)
        if instruction:
            # 如果是文件路径，读取内容
            p = Path(instruction)
            if p.is_file():
                config.user_instruction = p.read_text(encoding="utf-8")
            else:
                config.user_instruction = instruction
        if args.verbose:
            config.verbose = True
        config.__post_init__()
        return config

    # 无配置文件 — 完全从 CLI 参数构建
    output_path = Path(args.output) if args.output else Path(args.repo).parent / "output"
    instruction = getattr(args, "instruction", "") or ""
    # 如果是文件路径，读取内容
    p = Path(instruction) if instruction else None
    if p and p.is_file():
        instruction = p.read_text(encoding="utf-8")

    config = CodePruneConfig(
        repo_path=Path(args.repo),
        output_path=output_path,
        user_instruction=instruction,
        verbose=args.verbose,
        llm=LLMConfig(
            provider=args.provider,
            api_key=args.api_key,
            api_base=getattr(args, "api_base", None),
            reasoning=ModelEndpoint(model=args.model),
            fast=ModelEndpoint(model=getattr(args, "fast_model", None) or args.model),
        ),
        graph=GraphConfig(
            initial_granularity=getattr(args, "granularity", "file"),
            enable_semantic=not getattr(args, "no_semantic", False),
        ),
        heal=HealConfig(
            max_heal_rounds=0 if getattr(args, "no_heal", False) else 5,
        ),
    )
    return config


# ── 子命令处理 ──

def cmd_run(args: argparse.Namespace) -> None:
    """全流程: Phase1 → Phase2 → Phase3"""
    config = _build_config(args)
    setup_logging(config.verbose, config.log_dir)
    pipeline = Pipeline(config)
    result_path = pipeline.run()
    print(f"\n✅ 子仓库已生成: {result_path}")


def cmd_graph(args: argparse.Namespace) -> None:
    """Phase1: 构建代码图谱"""
    config = _build_config(args)
    setup_logging(config.verbose, config.log_dir)
    pipeline = Pipeline(config)
    graph = pipeline.run_phase1()
    print(f"\n✅ 图谱已构建: {graph.stats}")


def cmd_prune(args: argparse.Namespace) -> None:
    """Phase2: 指令驱动裁剪 (需要先运行 graph)"""
    config = _build_config(args, need_instruction=True)
    setup_logging(config.verbose, config.log_dir)
    graph_path = Path(args.graph) if args.graph else None
    pipeline = Pipeline(config)
    sub_repo_path = pipeline.run_phase2(graph_path=graph_path)
    print(f"\n✅ 子仓库已生成: {sub_repo_path}")


def cmd_heal(args: argparse.Namespace) -> None:
    """Phase3: 自愈修补 (需要子仓库路径)"""
    config = _build_config(args)
    setup_logging(config.verbose, config.log_dir)
    sub_repo = Path(args.sub_repo)
    graph_path = Path(args.graph) if args.graph else None
    pipeline = Pipeline(config)
    pipeline.run_phase3(sub_repo, graph_path=graph_path)
    print(f"\n✅ 自愈完成: {sub_repo}")


def cmd_config(args: argparse.Namespace) -> None:
    """配置管理: init / show"""
    action = args.action

    if action == "init":
        out = Path(args.out) if args.out else Path("codeprune.yaml")
        if out.exists() and not args.force:
            print(f"⚠️  配置文件已存在: {out}  (使用 --force 覆盖)")
            sys.exit(1)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        print(f"✅ 已生成配置模板: {out}")

    elif action == "show":
        if args.file:
            config = CodePruneConfig.from_yaml(Path(args.file))
        else:
            config = CodePruneConfig()
        import yaml
        print(yaml.dump(config.to_dict(), default_flow_style=False,
                        allow_unicode=True, sort_keys=False))


def main():
    parser = argparse.ArgumentParser(
        prog="codeprune",
        description="CodePrune — 基于 LLM 的代码仓库功能剪枝系统",
    )
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # ── run: 全流程 ──
    p_run = subparsers.add_parser("run", help="执行完整 pipeline (Phase1→2→3)")
    _add_common_args(p_run)
    p_run.add_argument("instruction", type=str, help="自然语言功能描述")
    _add_llm_args(p_run)
    p_run.add_argument("--no-semantic", action="store_true", help="跳过语义层")
    p_run.add_argument("--no-heal", action="store_true", help="跳过自愈阶段")
    p_run.add_argument("--granularity", type=str, default="file", choices=["file", "class", "function"])

    # ── graph: Phase1 ──
    p_graph = subparsers.add_parser("graph", help="Phase1: 仅构建代码图谱")
    _add_common_args(p_graph)
    _add_llm_args(p_graph)
    p_graph.add_argument("--no-semantic", action="store_true", help="跳过语义层")
    p_graph.add_argument("--granularity", type=str, default="file", choices=["file", "class", "function"])

    # ── prune: Phase2 ──
    p_prune = subparsers.add_parser("prune", help="Phase2: 指令驱动裁剪 (需先运行 graph)")
    _add_common_args(p_prune)
    p_prune.add_argument("instruction", type=str, help="自然语言功能描述")
    _add_llm_args(p_prune)
    p_prune.add_argument("--graph", type=str, default=None, help="图谱文件路径 (默认: output/.codeprune_artifacts/graph.pkl)")

    # ── heal: Phase3 ──
    p_heal = subparsers.add_parser("heal", help="Phase3: 自愈修补 (需先运行 prune)")
    _add_common_args(p_heal)
    _add_llm_args(p_heal)
    p_heal.add_argument("--sub-repo", type=str, required=True, help="子仓库路径")
    p_heal.add_argument("--graph", type=str, default=None, help="图谱文件路径")

    # ── config: 配置管理 ──
    p_config = subparsers.add_parser("config", help="配置管理 (init / show)")
    config_sub = p_config.add_subparsers(dest="action", help="配置操作")
    p_config_init = config_sub.add_parser("init", help="生成默认配置模板")
    p_config_init.add_argument("-o", "--out", type=str, default=None, help="输出路径 (默认: codeprune.yaml)")
    p_config_init.add_argument("-f", "--force", action="store_true", help="覆盖已有文件")
    p_config_show = config_sub.add_parser("show", help="显示配置内容")
    p_config_show.add_argument("file", type=str, nargs="?", default=None, help="YAML 配置文件路径 (可选)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    dispatch = {
        "run": cmd_run,
        "graph": cmd_graph,
        "prune": cmd_prune,
        "heal": cmd_heal,
        "config": cmd_config,
    }

    try:
        dispatch[args.command](args)
    except Exception as e:
        logging.error(f"执行失败: {e}", exc_info="--verbose" in sys.argv or "-v" in sys.argv)
        sys.exit(1)


if __name__ == "__main__":
    main()
