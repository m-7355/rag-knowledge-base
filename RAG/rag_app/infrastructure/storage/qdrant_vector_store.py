"""既存構成を破壊しない検証を備えたQdrantアダプター（検索用DB）

同期Workerスレッドの書込みと質問処理スレッドの検索が同時に起きるため、
Qdrant Localクライアントへの操作は1回ずつロックで直列化する。
ロックは操作単位なので、同期の途中でも検索は待たずに実行できる。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Sequence

from rag_app.domain.errors import CollectionMismatchError, DependencyUnavailable
from rag_app.domain.models import RetrievedChunk, TextChunk


# Qdrant LocalまたはServerを共通契約で扱うベクトルストア
class QdrantVectorStore:
    def __init__(self, settings, client=None):
        self._settings = settings
        self._lock = threading.RLock()
        if client is None:
            try:
                from qdrant_client import QdrantClient

                # URL設定時はServer、それ以外はローカル永続化を選択
                if settings.qdrant_url:
                    client = QdrantClient(url=settings.qdrant_url)
                else:
                    settings.qdrant_path.parent.mkdir(parents=True, exist_ok=True)
                    client = QdrantClient(path=str(settings.qdrant_path))
            except Exception as exc:
                if not settings.qdrant_url and "already accessed by another instance" in str(exc).casefold():
                    raise DependencyUnavailable(
                        "Qdrant Localは別のプロセスで使用中です。"
                        "起動中のWebアプリまたは索引CLIを終了してから再実行してください。"
                    ) from exc
                raise DependencyUnavailable("Qdrantを初期化できません。") from exc
        self._client = client

    def ensure_collection(self, dimension: int, model_name: str) -> None:
        # 既存コレクションを削除せずベクトル形式だけを検証
        try:
            from qdrant_client.http import models

            with self._lock:
                exists = self._client.collection_exists(self._settings.collection_name)
                if not exists:
                    self._client.create_collection(
                        collection_name=self._settings.collection_name,
                        vectors_config=models.VectorParams(
                            size=dimension,
                            distance=models.Distance.COSINE,
                        ),
                    )
                    return
                info = self._client.get_collection(self._settings.collection_name)

            vectors = info.config.params.vectors
            if isinstance(vectors, dict):
                raise CollectionMismatchError(
                    "既存Qdrantコレクションが名前付きベクトル構成です。自動変更しません。"
                )
            existing_dimension = int(vectors.size)
            distance = getattr(vectors.distance, "value", str(vectors.distance))
            if existing_dimension != dimension or str(distance).casefold() != "cosine":
                raise CollectionMismatchError(
                    "既存Qdrantコレクションの設定が不一致です "
                    f"(dimension={existing_dimension}, distance={distance}; "
                    f"required dimension={dimension}, distance=Cosine)。"
                )
        except CollectionMismatchError:
            raise
        except Exception as exc:
            raise DependencyUnavailable("Qdrantコレクションを確認できません。") from exc

    def upsert(
        self,
        chunks: Sequence[TextChunk],
        vectors: Sequence[Sequence[float]],
        searchable: bool = True,
    ) -> None:
        # チャンクとベクトルの件数確認後に検索用ペイロードを保存
        if len(chunks) != len(vectors):
            raise ValueError("chunksとvectorsの件数が一致しません。")
        if not chunks:
            return

        try:
            from qdrant_client.http import models

            indexed_at = datetime.now(timezone.utc).isoformat()
            # 出典表示と索引状態の照合に使うメタデータをベクトルと一緒に保存
            points = [
                models.PointStruct(
                    id=chunk.point_id,
                    vector=[float(value) for value in vector],
                    payload={
                        "source_id": chunk.source_id,
                        "relative_path": chunk.relative_path,
                        "file_name": chunk.file_name,
                        "file_hash": chunk.file_hash,
                        "page_number": chunk.page_number,
                        "chunk_index": chunk.chunk_index,
                        "text": chunk.text,
                        "embedding_model": self._settings.embedding_model,
                        "indexed_at": indexed_at,
                        # 新版の準備中ポイントは検索結果に混ざらないよう保留状態で保存
                        "index_state": "ready" if searchable else "pending",
                    },
                )
                for chunk, vector in zip(chunks, vectors)
            ]
            with self._lock:
                self._client.upsert(
                    collection_name=self._settings.collection_name,
                    points=points,
                    wait=True,
                )
        except Exception as exc:
            raise DependencyUnavailable("Qdrantへの索引保存に失敗しました。") from exc

    def activate_points(self, point_ids: Sequence[str]) -> None:
        # SQLiteのready状態へ切り替えた後に、新版ポイントを検索可能にする
        if not point_ids:
            return
        try:
            from qdrant_client.http import models

            with self._lock:
                self._client.set_payload(
                    collection_name=self._settings.collection_name,
                    payload={"index_state": "ready"},
                    points=models.PointIdsList(points=list(point_ids)),
                    wait=True,
                )
        except Exception as exc:
            raise DependencyUnavailable("新しいQdrantポイントを検索可能にできません。") from exc

    def search(
        self,
        vector: Sequence[float],
        limit: int,
        point_ids: Sequence[str] | None = None,
    ) -> list[RetrievedChunk]:
        # 類似度検索と不正ペイロードの個別除外
        try:
            query_filter = None
            from qdrant_client.http import models

            conditions = [
                models.FieldCondition(
                    key="index_state",
                    match=models.MatchValue(value="pending"),
                )
            ]
            must_not = [conditions[0]]
            if point_ids is not None:
                # キーワード検索で見つかったポイントだけに絞ってコサイン類似度を求める
                query_filter = models.Filter(
                    must=[models.HasIdCondition(has_id=list(point_ids))],
                    must_not=must_not,
                )
            else:
                query_filter = models.Filter(must_not=must_not)
            with self._lock:
                response = self._client.query_points(
                    collection_name=self._settings.collection_name,
                    query=[float(value) for value in vector],
                    query_filter=query_filter,
                    limit=limit,
                    with_payload=True,
                )
        except Exception as exc:
            raise DependencyUnavailable("Qdrant検索に失敗しました。") from exc

        results: list[RetrievedChunk] = []
        for point in response.points:
            payload = point.payload or {}
            try:
                chunk = self._chunk_from_payload(str(point.id), payload)
                results.append(RetrievedChunk(chunk=chunk, score=float(point.score)))
            except (KeyError, TypeError, ValueError):
                continue
        return results

    def list_by_source(self, source_id: str) -> list[TextChunk]:
        # Qdrantのページングを使ったソース単位の全件取得
        try:
            from qdrant_client.http import models

            source_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="source_id",
                        match=models.MatchValue(value=source_id),
                    )
                ]
            )
            chunks: list[TextChunk] = []
            offset = None
            while True:
                with self._lock:
                    points, next_offset = self._client.scroll(
                        collection_name=self._settings.collection_name,
                        scroll_filter=source_filter,
                        limit=256,
                        offset=offset,
                        with_payload=True,
                        with_vectors=False,
                    )
                for point in points:
                    try:
                        chunks.append(self._chunk_from_payload(str(point.id), point.payload or {}))
                    except (KeyError, TypeError, ValueError):
                        continue
                if next_offset is None:
                    break
                offset = next_offset
            return chunks
        except Exception as exc:
            raise DependencyUnavailable("Qdrantの索引ポイントを列挙できません。") from exc

    def delete_points(self, point_ids: Sequence[str]) -> None:
        # 更新・削除されたファイルの不要なポイントをID指定で削除
        if not point_ids:
            return
        try:
            from qdrant_client.http import models

            with self._lock:
                self._client.delete(
                    collection_name=self._settings.collection_name,
                    points_selector=models.PointIdsList(points=list(point_ids)),
                    wait=True,
                )
        except Exception as exc:
            raise DependencyUnavailable("Qdrantの索引ポイントを削除できません。") from exc

    def health(self) -> str:
        # コレクションを参照できるかどうかで稼働状態を判定
        try:
            with self._lock:
                self._client.get_collection(self._settings.collection_name)
            return "ok"
        except Exception:
            return "unavailable"

    def rebuild_collection(self, dimension: int, model_name: str) -> None:
        # CLIの明示確認後に行う既存コレクションの破棄と再作成
        try:
            with self._lock:
                if self._client.collection_exists(self._settings.collection_name):
                    self._client.delete_collection(self._settings.collection_name)
                self.ensure_collection(dimension, model_name)
        except Exception as exc:
            if isinstance(exc, DependencyUnavailable):
                raise
            raise DependencyUnavailable("Qdrantコレクションを再構築できません。") from exc

    def close(self) -> None:
        # ローカル保存先のロックを解放するためのクライアント終了
        close = getattr(self._client, "close", None)
        if close is not None:
            with self._lock:
                close()

    @staticmethod
    def _chunk_from_payload(point_id: str, payload: dict) -> TextChunk:
        # Qdrantのペイロードから検索結果のチャンクを復元（欠損時は例外で除外）
        return TextChunk(
            point_id=point_id,
            source_id=str(payload["source_id"]),
            relative_path=str(payload["relative_path"]),
            file_name=str(payload["file_name"]),
            file_hash=str(payload["file_hash"]),
            page_number=int(payload["page_number"]),
            chunk_index=int(payload["chunk_index"]),
            text=str(payload["text"]),
        )
