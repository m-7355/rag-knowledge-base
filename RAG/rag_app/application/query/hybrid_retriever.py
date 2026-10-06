"""ベクトル検索とキーワード検索を融合するHybrid Retrieval

    質問 ─┬─ Query Embedding → Qdrant（意味の近さ）   ─┐
          └─ キーワード抽出   → FTS5/BM25（固有文字列）─┴→ RRFで統合 → 候補リスト

RRF（Reciprocal Rank Fusion）はスコアの尺度が異なる検索結果を「順位」だけで統合する手法で、
どちらか一方で上位に来た候補を取りこぼしにくい。
"""

from __future__ import annotations

import logging

from rag_app.domain.models import RetrievedChunk
from rag_app.ports.interfaces import EmbeddingProvider, KeywordIndex, VectorStore

logger = logging.getLogger(__name__)

# RRFの順位平滑化定数（一般的な既定値）
_RRF_K = 60


# 質問から関連チャンク候補を集めて融合順に返す検索器
class HybridRetriever:
    def __init__(
        self,
        embedder: EmbeddingProvider,
        vector_store: VectorStore,
        keyword_index: KeywordIndex | None,
        candidate_limit: int,
    ):
        self._embedder = embedder
        self._vector_store = vector_store
        self._keyword_index = keyword_index
        self._candidate_limit = candidate_limit

    def retrieve(self, question: str) -> list[RetrievedChunk]:
        query_vector = self._embedder.embed_query(question)
        vector_hits = self._vector_store.search(query_vector, self._candidate_limit)
        keyword_ids = self._keyword_search(question)
        if not keyword_ids:
            return vector_hits

        # キーワード検索だけで見つかった候補にもコサイン類似度を付け、関連度しきい値を共通に使えるようにする
        known = {hit.chunk.point_id: hit for hit in vector_hits}
        missing_ids = [point_id for point_id in keyword_ids if point_id not in known]
        if missing_ids:
            for hit in self._vector_store.search(query_vector, len(missing_ids), point_ids=missing_ids):
                known[hit.chunk.point_id] = hit

        fused: dict[str, float] = {}
        for ranking in ([hit.chunk.point_id for hit in vector_hits], keyword_ids):
            for rank, point_id in enumerate(ranking, start=1):
                if point_id in known:
                    fused[point_id] = fused.get(point_id, 0.0) + 1.0 / (_RRF_K + rank)
        ordered = sorted(fused, key=lambda point_id: fused[point_id], reverse=True)
        return [known[point_id] for point_id in ordered[: self._candidate_limit]]

    def _keyword_search(self, question: str) -> list[str]:
        if self._keyword_index is None:
            return []
        try:
            return self._keyword_index.search(question, self._candidate_limit)
        except Exception:
            # キーワード索引は補助経路のため、失敗時はベクトル検索だけで回答を続ける
            logger.warning("キーワード検索に失敗しました。", exc_info=True)
            return []
