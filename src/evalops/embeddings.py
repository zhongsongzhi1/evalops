"""embedding 来源：``semantic`` 断言用。

显式声明来源（设计文档 §19.3）：
- ``mock``：字符 n-gram 哈希向量，零密钥、确定性，用于演示与 CI；
  它衡量的是字面重合度，不代表真实语义；
- ``openai``：OpenAI 兼容的 ``/embeddings`` 接口，需要 API Key，作为可选真实路径。
"""

from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


class EmbeddingError(ValueError):
    """embedding 配置或调用失败。"""


class Embedder(Protocol):
    name: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _grams(text: str) -> list[str]:
    normalized = "".join(ch.lower() for ch in text if not ch.isspace())
    if not normalized:
        return []
    if len(normalized) < 2:
        return [normalized]
    grams: list[str] = []
    for size in (2, 3):
        grams.extend(
            normalized[index : index + size]
            for index in range(len(normalized) - size + 1)
        )
    return grams


@dataclass
class MockEmbedder:
    """确定性哈希向量：同样的文本永远得到同样的向量。"""

    dim: int = 512
    name: str = "mock"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for gram in _grams(text):
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            sign = 1.0 if value & 1 else -1.0
            vector[(value >> 1) % self.dim] += sign
        norm = math.sqrt(sum(item * item for item in vector))
        if norm == 0:
            return vector
        return [item / norm for item in vector]


@dataclass
class OpenAIEmbedder:
    base_url: str = "https://api.openai.com/v1"
    model: str = "text-embedding-3-small"
    api_key: str = field(default="", repr=False)
    timeout_s: float = 10.0
    name: str = "openai"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        url = f"{self.base_url.rstrip('/')}/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {"model": self.model, "input": texts}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embedding 请求失败: {exc}") from exc
        if response.status_code >= 400:
            raise EmbeddingError(
                f"embedding 接口返回 HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            data = response.json()["data"]
            ordered = sorted(data, key=lambda item: int(item["index"]))
            return [[float(value) for value in item["embedding"]] for item in ordered]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError(f"embedding 响应格式不正确: {exc}") from exc


def build_embedder(config: Mapping[str, Any]) -> Embedder:
    """按 ``config.embedding.provider`` 构建 embedder，缺省为离线 mock。"""
    raw = config.get("embedding") or {}
    if not isinstance(raw, Mapping):
        raise EmbeddingError("config.embedding 必须是映射")
    provider = str(raw.get("provider", "mock")).lower()

    if provider in ("mock", "hash"):
        dim = int(raw.get("dim", 512))
        if dim <= 0:
            raise EmbeddingError("embedding.dim 必须为正整数")
        return MockEmbedder(dim=dim)

    if provider == "openai":
        api_key_env = str(raw.get("api_key_env", "OPENAI_API_KEY"))
        api_key = os.environ.get(api_key_env) or str(raw.get("api_key") or "")
        if not api_key:
            raise EmbeddingError(
                "embedding.provider=openai 需要环境变量 "
                f"{api_key_env}（或 config.embedding.api_key）"
            )
        return OpenAIEmbedder(
            base_url=str(raw.get("base_url", "https://api.openai.com/v1")),
            model=str(raw.get("model", "text-embedding-3-small")),
            api_key=api_key,
            timeout_s=float(raw.get("timeout_s", 10)),
        )

    raise EmbeddingError(f"未知 embedding provider: {provider}（可选 mock / openai）")


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) != len(right):
        raise EmbeddingError(
            f"向量维度不一致: {len(left)} vs {len(right)}；请确认使用同一个 embedding 来源"
        )
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)
