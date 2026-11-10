"""LLM Judge：按 rubric 给输出打分，并保留评分依据。

设计要点（设计文档 §7、§19.3）：
- rubric 是独立 Markdown 文件，``version`` 写入结果与人工标注，保证可追溯；
- 同一用例默认采样 3 次取中位数，降低单次随机性；
- ``mock`` 来源零密钥、确定性，仅用于演示与 CI；真实评审需显式配置
  OpenAI 兼容接口，不能把 mock 分数当成业务结论。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Protocol

import httpx
import yaml

from evalops.embeddings import Embedder, MockEmbedder, build_embedder, cosine

FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
SCORE_MIN = 1.0
SCORE_MAX = 5.0


class JudgeError(ValueError):
    """rubric 加载或 Judge 调用失败。"""


@dataclass
class Rubric:
    path: Path
    text: str
    version: str
    reference: str | None = None


@dataclass
class JudgeResult:
    score: float
    scores: list[float]
    rationale: str
    provider: str
    model: str = ""

    def to_detail(self, rubric: Rubric, pass_score: float) -> dict[str, Any]:
        return {
            "rubric": rubric.path.name,
            "rubric_version": rubric.version,
            "scores": self.scores,
            "median": self.score,
            "pass_score": pass_score,
            "rationale": self.rationale,
            "provider": self.provider,
            "model": self.model,
        }


class Judge(Protocol):
    name: str

    async def score(self, rubric: Rubric, output: str, samples: int) -> JudgeResult: ...


def load_rubric(reference: str, search_dirs: Sequence[Path]) -> Rubric:
    """按项目目录 → 用例目录 → 当前目录的顺序解析 rubric 路径。"""
    raw = Path(reference)
    candidates = (
        [raw]
        if raw.is_absolute()
        else [Path(directory) / raw for directory in search_dirs]
    )
    for candidate in candidates:
        if candidate.is_file():
            return _parse_rubric(candidate)
    tried = "、".join(str(item) for item in candidates)
    raise JudgeError(f"rubric 文件不存在: {reference}（已尝试: {tried}）")


def _parse_rubric(path: Path) -> Rubric:
    text = path.read_text(encoding="utf-8")
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    reference: str | None = None

    match = FRONT_MATTER.match(text)
    if match:
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            raise JudgeError(f"{path}: rubric front matter 解析失败: {exc}") from exc
        if not isinstance(meta, dict):
            raise JudgeError(f"{path}: rubric front matter 必须是映射")
        if meta.get("version"):
            version = str(meta["version"])
        if meta.get("reference"):
            reference = str(meta["reference"])

    return Rubric(path=path, text=text, version=version, reference=reference)


@dataclass
class MockJudge:
    """确定性演示 Judge：用输出与 rubric 参考表述的字面相似度模拟评分。"""

    embedder: Embedder = field(default_factory=MockEmbedder)
    name: str = "mock"
    model: str = "mock-judge"

    async def score(self, rubric: Rubric, output: str, samples: int) -> JudgeResult:
        if not rubric.reference:
            raise JudgeError(
                f"{rubric.path}: mock judge 需要在 front matter 中声明 reference；"
                "真实评分请配置 config.judge.provider=openai"
            )
        vectors = await self.embedder.embed([rubric.reference, output])
        similarity = max(0.0, min(1.0, cosine(vectors[0], vectors[1])))
        value = round(SCORE_MIN + (SCORE_MAX - SCORE_MIN) * similarity, 2)
        rationale = (
            f"mock 模式：与 rubric reference 的字面相似度 {similarity:.3f}，"
            "仅用于演示与 CI"
        )
        return JudgeResult(
            score=value,
            scores=[value for _ in range(samples)],
            rationale=rationale,
            provider=self.name,
            model=self.model,
        )


@dataclass
class OpenAIJudge:
    """OpenAI 兼容 chat/completions Judge；采样多次取中位数。"""

    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key: str = field(default="", repr=False)
    temperature: float = 0.0
    timeout_s: float = 30.0
    max_tokens: int = 300
    name: str = "openai"

    async def score(self, rubric: Rubric, output: str, samples: int) -> JudgeResult:
        outcomes = await asyncio.gather(
            *(self._score_once(rubric, output) for _ in range(samples))
        )
        scores = [item[0] for item in outcomes]
        value = float(median(scores))
        closest = min(outcomes, key=lambda item: abs(item[0] - value))
        return JudgeResult(
            score=value,
            scores=scores,
            rationale=closest[1],
            provider=self.name,
            model=self.model,
        )

    async def _score_once(self, rubric: Rubric, output: str) -> tuple[float, str]:
        prompt = (
            "评分标准：\n"
            f"{rubric.text}\n\n"
            "待评输出：\n"
            f"{output}\n\n"
            '只输出 JSON 对象：{"score": 1-5 的数字, "rationale": "一句话理由"}'
        )
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": "你是严格的评测员，只按 rubric 打分。"},
                {"role": "user", "content": prompt},
            ],
        }
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise JudgeError(f"Judge 请求失败: {exc}") from exc
        if response.status_code >= 400:
            raise JudgeError(f"Judge 接口返回 HTTP {response.status_code}: {response.text[:200]}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise JudgeError(f"Judge 响应格式不正确: {exc}") from exc
        return _parse_payload(str(content))


def _parse_payload(content: str) -> tuple[float, str]:
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        raise JudgeError(f"Judge 未返回 JSON: {content[:200]}")
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError as exc:
        raise JudgeError(f"Judge 返回的 JSON 无法解析: {exc}") from exc
    if not isinstance(data, dict) or "score" not in data:
        raise JudgeError("Judge 返回值缺少 score 字段")
    try:
        score = float(data["score"])
    except (TypeError, ValueError) as exc:
        raise JudgeError(f"Judge score 不是数字: {data.get('score')!r}") from exc
    if math.isnan(score):
        raise JudgeError("Judge score 不能是 NaN")
    return min(SCORE_MAX, max(SCORE_MIN, score)), str(data.get("rationale", ""))


def build_judge(config: dict[str, Any], embedder: Embedder | None = None) -> Judge:
    """按 ``config.judge.provider`` 构建 Judge，缺省为离线 mock。"""
    raw = config.get("judge") or {}
    if not isinstance(raw, dict):
        raise JudgeError("config.judge 必须是映射")
    provider = str(raw.get("provider", "mock")).lower()

    if provider in ("mock", "hash"):
        active = embedder or build_embedder(config)
        return MockJudge(embedder=active)

    if provider in ("openai", "openai_compatible"):
        api_key_env = str(raw.get("api_key_env", "OPENAI_API_KEY"))
        api_key = os.environ.get(api_key_env) or str(raw.get("api_key") or "")
        if not api_key:
            raise JudgeError(
                "judge.provider=openai 需要环境变量 "
                f"{api_key_env}（或 config.judge.api_key）"
            )
        return OpenAIJudge(
            base_url=str(raw.get("base_url", "https://api.openai.com/v1")),
            model=str(raw.get("model", "gpt-4o-mini")),
            api_key=api_key,
            temperature=float(raw.get("temperature", 0.0)),
            timeout_s=float(raw.get("timeout_s", 30)),
            max_tokens=int(raw.get("max_tokens", 300)),
        )

    raise JudgeError(f"未知 judge provider: {provider}（可选 mock / openai）")
