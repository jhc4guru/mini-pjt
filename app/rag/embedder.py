"""Bedrock Titan 임베딩(LangChain `BedrockEmbeddings`, D-53).

주의: 여기로 들어오는 텍스트는 외부 모델 경계를 넘는다. 질의는 마스킹된 텍스트만 허용한다
(검사는 HybridRetriever.search가 한다). 오류 메시지에는 계정 정보가 섞일 수 있어 길이를 잘라서 올린다.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from langchain_aws import BedrockEmbeddings

from app import config


class EmbeddingError(RuntimeError):
    pass


class TitanEmbedder:
    def __init__(self, model_id: str | None = None, region: str | None = None, dimensions: int = 1024, *, client=None):
        self.model_id = model_id or config.embed_model_id()
        self.dimensions = dimensions
        self._emb = BedrockEmbeddings(
            client=client, model_id=self.model_id, region_name=region or config.region(), dimensions=dimensions, normalize=True,
        )
        self._cache: dict[str, list[float]] = {}

    def _embed(self, text: str) -> list[float]:
        try:
            return list(self._emb.embed_query(text))
        except Exception as e:  # noqa: BLE001
            raise EmbeddingError(f"{type(e).__name__}: {str(e)[:80]}") from None

    def embed(self, text: str) -> list[float]:
        if text not in self._cache:
            self._cache[text] = self._embed(text)
        return self._cache[text]

    def embed_many(self, texts: list[str], workers: int = 8) -> list[list[float]]:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(self.embed, texts))
