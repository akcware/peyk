"""Embeddings: Bedrock Titan Text Embeddings v2 (1024 dims). Protocol so tests can swap a fake."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from typing import Protocol

DIMS = 1024


class Embedder(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class TitanEmbedder:
    def __init__(self, region: str = "us-east-1", model_id: str = "amazon.titan-embed-text-v2:0") -> None:
        self._region = region
        self._model_id = model_id
        self._client = None

    def _sync(self, text: str) -> list[float]:
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        body = json.dumps({"inputText": text[:8000], "dimensions": DIMS, "normalize": True})
        resp = self._client.invoke_model(modelId=self._model_id, contentType="application/json", accept="application/json", body=body)
        return json.loads(resp["body"].read())["embedding"]

    async def embed(self, text: str) -> list[float]:
        return await asyncio.to_thread(self._sync, text)


class FakeEmbedder:
    """Deterministic bag-of-words hashing embedder for tests: similar texts -> similar vectors."""

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * DIMS
        for token in text.lower().split():
            h = int(hashlib.sha256(token.encode()).hexdigest(), 16)
            vec[h % DIMS] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def to_pgvector(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
