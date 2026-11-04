"""Chroma 벡터 색인. chunk 텍스트 해시가 바뀐 것만 다시 임베딩한다."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import chromadb

from app.config import DATA_DIR
from app.rag.chunks import Chunk
from app.rag.embedder import TitanEmbedder


def _digest(model_id: str, chunk: Chunk) -> str:
    """텍스트와 메타데이터(경로 포함)가 바뀌면 다시 색인한다. 문서를 옮겨도 저장된 경로가 낡지 않는다."""
    meta = json.dumps(chunk.meta, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(f"{model_id}\n{meta}\n{chunk.text}".encode("utf-8")).hexdigest()


class VectorIndex:
    def __init__(self, embedder: TitanEmbedder, persist_dir: Path | None = None, collection: str = "cli_docs"):
        self.embedder = embedder
        path = Path(persist_dir or DATA_DIR / "chroma")
        path.mkdir(parents=True, exist_ok=True)
        self._col = chromadb.PersistentClient(path=str(path)).get_or_create_collection(
            name=collection, metadata={"hnsw:space": "cosine"}
        )

    def sync(self, chunks: list[Chunk]) -> dict[str, int]:
        """색인을 chunk 목록과 맞춘다. 반환: {embedded, skipped, deleted}"""
        want = {c.id: c for c in chunks}
        have = self._col.get(include=["metadatas"])
        have_hash = {i: (m or {}).get("digest") for i, m in zip(have["ids"], have["metadatas"])}

        stale = [i for i in have_hash if i not in want]
        if stale:
            self._col.delete(ids=stale)

        todo = [c for c in chunks if have_hash.get(c.id) != _digest(self.embedder.model_id, c)]
        if todo:
            vectors = self.embedder.embed_many([c.text for c in todo])
            self._col.upsert(
                ids=[c.id for c in todo],
                embeddings=vectors,
                documents=[c.text for c in todo],
                metadatas=[{**c.meta, "digest": _digest(self.embedder.model_id, c)} for c in todo],
            )
        return {"embedded": len(todo), "skipped": len(chunks) - len(todo), "deleted": len(stale)}

    def query(self, text: str, n: int = 30) -> list[tuple[str, float]]:
        """(chunk id, 코사인 거리) 가까운 순."""
        n = min(n, self._col.count())
        r = self._col.query(query_embeddings=[self.embedder.embed(text)], n_results=n)
        return list(zip(r["ids"][0], r["distances"][0]))
