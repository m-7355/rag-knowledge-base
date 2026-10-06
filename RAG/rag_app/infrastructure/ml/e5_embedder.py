"""多言語E5モデルを使うSentence Transformersアダプター

同期Worker（文書のEmbedding）と質問処理（質問のEmbedding）が同じモデルを共有する。
推論はバッチ単位でロックするため、大きな文書の同期中でも質問はバッチの切れ目で割り込める。
"""

from __future__ import annotations

import threading
from typing import Any, Sequence

from rag_app.domain.errors import DependencyUnavailable


# E5の入力形式とベクトル次元を保証するEmbeddingアダプター
class SentenceTransformerE5Embedder:
    def __init__(self, model_name: str, batch_size: int = 32, model: Any | None = None):
        self.model_name = model_name
        self.batch_size = batch_size
        self._lock = threading.Lock()
        if model is None:
            try:
                from sentence_transformers import SentenceTransformer

                model = SentenceTransformer(model_name)
            except Exception as exc:
                raise DependencyUnavailable(
                    "Embeddingモデルを読み込めません。モデル名、保存済みモデル、ネットワーク接続を確認してください。"
                ) from exc
        self._model = model
        try:
            get_dimension = getattr(self._model, "get_embedding_dimension", None)
            if get_dimension is None:
                get_dimension = self._model.get_sentence_embedding_dimension
            dimension = get_dimension()
        except Exception as exc:
            raise DependencyUnavailable("Embeddingモデルの次元を取得できません。") from exc
        if not dimension:
            raise DependencyUnavailable("Embeddingモデルの次元を取得できません。")
        self._dimension = int(dimension)

    def dimension(self) -> int:
        # Qdrantコレクションの次元検証に使うベクトル長
        return self._dimension

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        # 文書本文用のpassage接頭辞
        return self._encode([f"passage: {text}" for text in texts])

    def embed_query(self, text: str) -> list[float]:
        # 質問検索用のquery接頭辞
        return self._encode([f"query: {text}"])[0]

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list = []
        try:
            # バッチごとにロックを取り直し、他スレッドの質問を長時間待たせない
            for start in range(0, len(texts), self.batch_size):
                with self._lock:
                    vectors.extend(
                        self._model.encode(
                            texts[start : start + self.batch_size],
                            batch_size=self.batch_size,
                            normalize_embeddings=True,
                            convert_to_numpy=True,
                            show_progress_bar=False,
                        )
                    )
        except Exception as exc:
            raise DependencyUnavailable("Embeddingの生成に失敗しました。") from exc

        # モデル出力の件数と次元を契約に照合
        if len(vectors) != len(texts):
            raise DependencyUnavailable("Embeddingモデルが不正な件数のベクトルを返しました。")
        result = [[float(value) for value in vector] for vector in vectors]
        if any(len(vector) != self._dimension for vector in result):
            raise DependencyUnavailable("Embeddingモデルが不正な次元のベクトルを返しました。")
        return result
