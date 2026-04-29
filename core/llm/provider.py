"""
LLM 抽象提供者接口 + Prompt 模板管理 + 调用缓存
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from config import LLMConfig

logger = logging.getLogger(__name__)


# ───────────────────── 抽象接口 ─────────────────────

class LLMProvider(ABC):
    """LLM 提供者抽象基类"""

    def __init__(self, config: LLMConfig):
        self.config = config

    @abstractmethod
    def chat(self, messages: list[dict], **kwargs) -> str:
        """发送聊天请求，返回文本响应"""
        ...

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量文本 embedding"""
        ...

    def chat_json(self, messages: list[dict], **kwargs) -> dict:
        """聊天并解析 JSON 响应"""
        response = self.chat(messages, **kwargs)
        # 尝试从 markdown 代码块提取 JSON
        if "```json" in response:
            response = response.split("```json")[1].split("```")[0]
        elif "```" in response:
            response = response.split("```")[1].split("```")[0]
        return json.loads(response.strip())

    def fast_chat(self, messages: list[dict], **kwargs) -> str:
        """使用快速模型聊天 (摘要, 锚点验证等简单任务)"""
        ep = self.config.fast
        kwargs.setdefault("model", ep.model)
        kwargs.setdefault("temperature", ep.temperature)
        kwargs.setdefault("max_tokens", ep.max_tokens)
        return self.chat(messages, **kwargs)

    def fast_chat_json(self, messages: list[dict], **kwargs) -> dict:
        """使用快速模型聊天并解析 JSON"""
        response = self.fast_chat(messages, **kwargs)
        if "```json" in response:
            response = response.split("```json")[1].split("```")[0]
        elif "```" in response:
            response = response.split("```")[1].split("```")[0]
        return json.loads(response.strip())


# ───────────────────── OpenAI 实现 ─────────────────────

class OpenAIProvider(LLMProvider):

    _force_stream: bool = False  # 检测到代理 bug 后自动切换

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("pip install openai")
        api_key = config.api_key or os.getenv("OPENAI_API_KEY")
        self.client = OpenAI(
            api_key=api_key,
            base_url=config.api_base,
            timeout=config.timeout,
            max_retries=config.max_retries,
        )
        # 独立的 embedding 客户端（当主 API 不支持 embedding 时）
        if config.embedding_api_base:
            self.embed_client = OpenAI(
                api_key=config.embedding_api_key or api_key,
                base_url=config.embedding_api_base,
                timeout=config.timeout,
                max_retries=config.max_retries,
            )
        else:
            self.embed_client = self.client

    def _stream_chat(self, model: str, messages: list[dict],
                     temperature: float, max_tokens: int) -> str:
        """Streaming 模式调用，拼接 delta chunks"""
        stream = self.client.chat.completions.create(
            model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
            stream=True,
        )
        chunks: list[str] = []
        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                chunks.append(delta.content)
        return "".join(chunks)

    def chat(self, messages: list[dict], **kwargs) -> str:
        ep = self.config.reasoning
        model = kwargs.get("model", ep.model)
        temperature = kwargs.get("temperature", ep.temperature)
        max_tokens = kwargs.get("max_tokens", ep.max_tokens)

        # 已知代理有 bug 时直接用 streaming
        if OpenAIProvider._force_stream:
            return self._stream_chat(model, messages, temperature, max_tokens)

        # 先尝试非流式调用
        resp = self.client.chat.completions.create(
            model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
        )
        content = resp.choices[0].message.content
        if content:
            return content

        # 代理 bug 兼容：后续全部走 streaming
        logger.warning("检测到代理返回空 content，后续自动使用 streaming 模式")
        OpenAIProvider._force_stream = True
        return self._stream_chat(model, messages, temperature, max_tokens)

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self.embed_client.embeddings.create(
            model=self.config.embedding_model if hasattr(self.config, 'embedding_model') else "text-embedding-3-small",
            input=texts,
        )
        return [item.embedding for item in resp.data]


# ───────────────────── Anthropic 实现 ─────────────────────

class AnthropicProvider(LLMProvider):

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        try:
            from anthropic import Anthropic
        except ImportError:
            raise ImportError("pip install anthropic")
        api_key = config.api_key or os.getenv("ANTHROPIC_API_KEY")
        self.client = Anthropic(api_key=api_key, timeout=config.timeout, max_retries=config.max_retries)

    def chat(self, messages: list[dict], **kwargs) -> str:
        # Anthropic 需要分离 system message
        system = None
        user_msgs = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                user_msgs.append(m)
        ep = self.config.reasoning
        resp = self.client.messages.create(
            model=kwargs.get("model", ep.model),
            system=system or "",
            messages=user_msgs,
            temperature=kwargs.get("temperature", ep.temperature),
            max_tokens=kwargs.get("max_tokens", ep.max_tokens),
        )
        return resp.content[0].text

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("Anthropic 不直接提供 embedding API，请使用 OpenAI embedding 或本地模型")


# ───────────────────── 调用缓存 ─────────────────────

class LLMCache:
    """基于文件的 LLM 调用缓存"""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(self, messages: list[dict], model: str) -> str:
        content = json.dumps({"messages": messages, "model": model}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(content.encode()).hexdigest()

    def get(self, messages: list[dict], model: str) -> Optional[str]:
        path = self.cache_dir / f"{self._key(messages, model)}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("response")
        return None

    def set(self, messages: list[dict], model: str, response: str) -> None:
        path = self.cache_dir / f"{self._key(messages, model)}.json"
        data = {"messages": messages, "model": model, "response": response}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ───────────────────── 带缓存的包装器 ─────────────────────

class CachedLLMProvider(LLMProvider):
    """为任意 LLMProvider 添加缓存层"""

    def __init__(self, provider: LLMProvider, cache_dir: Path):
        super().__init__(provider.config)
        self.provider = provider
        self.cache = LLMCache(cache_dir)

    def chat(self, messages: list[dict], **kwargs) -> str:
        model = kwargs.get("model", self.config.reasoning.model)
        cached = self.cache.get(messages, model)
        if cached is not None:
            logger.debug("LLM cache hit")
            return cached
        response = self.provider.chat(messages, **kwargs)
        self.cache.set(messages, model, response)
        return response

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.provider.embed(texts)


# ───────────────────── 工厂函数 ─────────────────────

def create_llm_provider(config: LLMConfig) -> LLMProvider:
    """根据配置创建 LLM 提供者"""
    providers = {
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
    }
    cls = providers.get(config.provider)
    if cls is None:
        raise ValueError(f"不支持的 LLM provider: {config.provider}，可选: {list(providers.keys())}")
    provider = cls(config)
    if config.cache_enabled and config.cache_dir:
        provider = CachedLLMProvider(provider, config.cache_dir)
    return provider
