"""Cross-Encoderによる検索候補の再順位付け（Reranker）

ベクトル検索・キーワード検索は「質問」と「文書」を別々に数値化して比較するため、
細かな関連性の判定は苦手。Cross-Encoderは質問と候補文を1組で読み込んで関連度を出すため、
上位候補（例: 20件）をこれで並べ直し、LLMへ渡す件数（例: 5件）を絞り込むとノイズが減る。
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any, Sequence

from rag_app.domain.errors import DependencyUnavailable
from rag_app.domain.models import RetrievedChunk


# sentence-transformersのCrossEncoderで候補を関連度順に並べ直すアダプター
class CrossEncoderReranker:
    def __init__(self, model_name: str, batch_size: int = 16, model: Any | None = None):
        self.model_name = model_name
        self.batch_size = batch_size
        # 複数の質問リクエストが同時に来ても推論を直列化する
        self._lock = threading.Lock()
        if model is None:
            try:
                from sentence_transformers import CrossEncoder

                model = CrossEncoder(model_name)
            except Exception as exc:
                raise DependencyUnavailable(
                    "Rerankerモデルを読み込めません。RERANKER_MODEL とネットワーク接続を確認してください。"
                ) from exc
        self._model = model

    def rerank(self, query: str, candidates: Sequence[RetrievedChunk]) -> list[RetrievedChunk]:
        if not candidates:
            return []
        pairs = [(query, candidate.chunk.text) for candidate in candidates]
        try:
            with self._lock:
                scores = self._model.predict(pairs, batch_size=self.batch_size, show_progress_bar=False)
        except Exception as exc:
            raise DependencyUnavailable("Rerankerによる再順位付けに失敗しました。") from exc

        # コサイン類似度は出典表示用に残し、並び順だけをRerankerの評価値で決める
        scored = [
            replace(candidate, rerank_score=float(score))
            for candidate, score in zip(candidates, scores)
        ]
        return sorted(scored, key=lambda candidate: candidate.rerank_score, reverse=True)
