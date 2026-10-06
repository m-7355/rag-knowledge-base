"""同期元ごとの差分同期（取得 → Parse → Chunk → Embedding → 検索索引更新）の調整

1ファイルごとの判定:
    版が前回と同じ          → 本文を取得せずスキップ（unchanged）
    版は違うが内容が同じ    → 取得してSHA-256を比べ、版だけ更新（unchanged）
    内容が違う              → 抽出 → 分割 → Embedding → 旧チャンクを置換（indexed）
    同期元から消えた        → Qdrant・キーワード索引・同期管理DBから削除（removed）

全データを再Embeddingせず、変更のあったファイルだけを処理する。
Embeddingまでの重い処理中は旧版をreadyのまま検索可能にし、
検索対象から外れる時間を書込みの瞬間だけに抑える。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from rag_app.domain.errors import DocumentProcessingError
from rag_app.domain.models import (
    DocumentRef,
    IngestionItemResult,
    IngestionReport,
    PdfFile,
    SyncStateEntry,
    TextChunk,
)
from rag_app.ports.interfaces import (
    DocumentSource,
    EmbeddingProvider,
    KeywordIndex,
    PdfTextExtractor,
    SyncStateRepository,
    TextChunker,
    VectorStore,
)

logger = logging.getLogger(__name__)

# 進捗通知: (処理済み件数, 全件数, 処理中のファイルパス)
ProgressCallback = Callable[[int, int, str], None]


# 同期元の走査から検索索引と同期状態の更新までを担うサービス
class DocumentIngestionService:
    def __init__(
        self,
        extractor: PdfTextExtractor,
        chunker: TextChunker,
        embedder: EmbeddingProvider,
        vector_store: VectorStore,
        sync_state: SyncStateRepository,
        embedding_model: str,
        keyword_index: KeywordIndex | None = None,
    ):
        self._extractor = extractor
        self._chunker = chunker
        self._embedder = embedder
        self._vector_store = vector_store
        self._state = sync_state
        self._embedding_model = embedding_model
        self._keyword_index = keyword_index

    def sync_source(
        self,
        source: DocumentSource,
        force: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> IngestionReport:
        # 変更がなく再試行すべき失敗もなければ、走査自体を省略
        if not force and not source.has_changes() and not self._has_retryable_failures(source):
            return IngestionReport([], checked_only=True)

        # 本文は取得せず、現在存在する文書のメタデータだけを列挙
        documents = source.list_documents()
        present_sources = {document.source_id for document in documents}
        # 同期管理DBに残っているが同期元から消えた文書（削除同期の対象）
        missing = [
            entry
            for entry in self._state.list_all()
            if source.owns(entry.source_id) and entry.source_id not in present_sources
        ]
        total = len(documents) + len(missing)
        results: list[IngestionItemResult] = []

        # 1ファイルの失敗で他のファイルの処理を止めない個別実行
        for index, document in enumerate(documents):
            self._notify(on_progress, index, total, document.relative_path)
            try:
                results.append(self._sync_one(source, document))
            except Exception:
                logger.exception("文書の同期に失敗しました: %s", document.relative_path)
                results.append(
                    IngestionItemResult(
                        relative_path=document.relative_path,
                        status="failed",
                        message="索引処理に失敗しました。状態を確認して再実行してください。",
                    )
                )

        for offset, entry in enumerate(missing):
            self._notify(on_progress, len(documents) + offset, total, entry.relative_path)
            results.append(self._remove_missing(entry))

        self._notify(on_progress, total, total, "")
        # 失敗が残る場合は基準点を進めず、次の定期実行で再試行する
        if not any(item.status == "failed" for item in results):
            source.commit_checkpoint()
        return IngestionReport(results)

    def _sync_one(self, source: DocumentSource, document: DocumentRef) -> IngestionItemResult:
        previous = self._state.get(document.source_id)

        # 版が前回と同じなら本文の取得もハッシュ計算も行わない
        if self._is_current(previous, document):
            self._ensure_keyword_index(previous)
            if not self._delete_stale_points(previous):
                return IngestionItemResult(
                    document.relative_path,
                    "failed",
                    message="更新前の索引を整理できませんでした。再同期してください。",
                )
            return IngestionItemResult(
                document.relative_path, "unchanged", previous.chunk_count, "変更なし"
            )
        # 画像PDFなど、同じ版を何度処理しても失敗する文書は更新されるまで再処理しない
        if self._is_permanent_failure(previous, document):
            return IngestionItemResult(
                document.relative_path,
                "failed",
                message=DocumentProcessingError.message_for(previous.failure_reason),
            )

        try:
            pdf = source.fetch(document)
        except Exception as exc:
            code = exc.code if isinstance(exc, DocumentProcessingError) else "download_failed"
            self._record_attempt_failure(
                previous,
                self._new_entry(document, previous.file_hash if previous else ""),
                code,
            )
            return IngestionItemResult(
                document.relative_path, "failed", message=DocumentProcessingError.message_for(code)
            )

        try:
            return self._index(pdf, document, previous)
        finally:
            # 一時ファイルなど、取得時に作ったものを必ず片付ける
            source.release(pdf)

    def _index(
        self,
        pdf: PdfFile,
        document: DocumentRef,
        previous: SyncStateEntry | None,
    ) -> IngestionItemResult:
        try:
            file_hash = self._hash_file(pdf.absolute_path)
        except OSError:
            self._record_attempt_failure(
                previous,
                self._new_entry(document, previous.file_hash if previous else ""),
                "unreadable_file",
            )
            return IngestionItemResult(
                pdf.relative_path, "failed", message="PDFファイルを読み込めません。"
            )

        entry = self._new_entry(document, file_hash)

        # 版は変わったが内容が同じ場合（Driveのメタデータ更新など）は再Embeddingしない
        if (
            previous is not None
            and previous.status == "ready"
            and previous.file_hash == file_hash
            and previous.embedding_model == self._embedding_model
            and previous.relative_path == document.relative_path
        ):
            self._state.mark_ready(replace(entry, chunk_count=previous.chunk_count))
            self._ensure_keyword_index(previous)
            return IngestionItemResult(
                pdf.relative_path, "unchanged", previous.chunk_count, "変更なし"
            )

        try:
            # Parse → Chunk → Embedding（この間も旧版はreadyのまま検索できる）
            pages = self._extractor.extract(pdf, file_hash)
            chunks: list[TextChunk] = []
            for page in pages:
                chunks.extend(self._chunker.split(page))
            if not chunks:
                raise DocumentProcessingError("no_text")

            vectors = self._embedder.embed_passages([chunk.text for chunk in chunks])
            if len(vectors) != len(chunks):
                raise RuntimeError("Embedding件数がチャンク数と一致しません。")
            # 抽出中のPDF変更を検出して古い内容の登録を防止
            if self._hash_file(pdf.absolute_path) != file_hash:
                raise DocumentProcessingError("document_changed")

            # 新版ポイントはpendingで登録し、旧ready版だけが検索される状態を保つ
            self._vector_store.upsert(chunks, vectors, searchable=False)
            if self._keyword_index is not None:
                self._keyword_index.upsert_chunks(chunks, searchable=False)
        except DocumentProcessingError as exc:
            self._record_attempt_failure(previous, entry, exc.code)
            return IngestionItemResult(pdf.relative_path, "failed", message=str(exc))
        except Exception:
            logger.exception("索引処理に失敗しました: %s", pdf.relative_path)
            self._record_attempt_failure(previous, entry, "index_failed")
            return IngestionItemResult(
                pdf.relative_path,
                "failed",
                message="索引処理に失敗しました。依存関係と保存先を確認して再実行してください。",
            )

        # 新版が両方の検索索引に揃った時点で、SQLiteのready状態を一度に切り替える
        ready_entry = replace(entry, chunk_count=len(chunks), indexed_at=self._now())
        try:
            self._state.mark_ready(ready_entry)
        except Exception:
            # 旧版の状態が残っていれば、その版を引き続き検索対象にする
            logger.exception("新しい同期状態を有効化できませんでした: %s", pdf.relative_path)
            self._record_attempt_failure(previous, entry, "index_failed")
            return IngestionItemResult(
                pdf.relative_path,
                "failed",
                message="新しい索引を有効化できませんでした。再同期してください。",
            )

        try:
            current_ids = [chunk.point_id for chunk in chunks]
            self._vector_store.activate_points(current_ids)
            if self._keyword_index is not None:
                self._keyword_index.activate_points(current_ids)
        except Exception:
            # SQLite上は新版がreadyなので、次回の差分確認でpending状態の解除を再試行する
            logger.exception("同期ポイントを検索可能にできませんでした: %s", pdf.relative_path)
            return IngestionItemResult(
                pdf.relative_path,
                "failed",
                message="新しい索引を検索可能にできませんでした。再同期してください。",
            )

        # 状態の切替後に旧ポイントを掃除するため、失敗しても新版の検索は止まらない
        if not self._delete_stale_points(ready_entry):
            return IngestionItemResult(
                pdf.relative_path,
                "failed",
                message="新しい索引は利用できますが、更新前の索引を整理できませんでした。",
            )
        return IngestionItemResult(pdf.relative_path, "indexed", len(chunks), "索引しました")

    def _remove_missing(self, entry: SyncStateEntry) -> IngestionItemResult:
        # 同期元から消えたファイルの旧ポイント・キーワード索引・同期状態の除去
        processing = replace(entry, status="processing", failure_reason=None)
        try:
            self._state.mark_processing(processing)
            old_points = self._vector_store.list_by_source(entry.source_id)
            self._vector_store.delete_points([point.point_id for point in old_points])
            if self._keyword_index is not None:
                self._keyword_index.delete_source(entry.source_id)
            self._state.remove(entry.source_id)
            return IngestionItemResult(entry.relative_path, "removed", len(old_points), "削除しました")
        except Exception:
            self._mark_failed(processing, "remove_failed")
            return IngestionItemResult(
                entry.relative_path,
                "failed",
                message="削除済みファイルの索引を除去できませんでした。再実行してください。",
            )

    def _is_current(self, previous: SyncStateEntry | None, document: DocumentRef) -> bool:
        # 同じ版・同じパス・同じEmbeddingモデルでready済みなら最新とみなす
        return (
            previous is not None
            and previous.status == "ready"
            and previous.embedding_model == self._embedding_model
            and bool(previous.source_revision)
            and previous.source_revision == document.revision
            and previous.relative_path == document.relative_path
        )

    def _delete_stale_points(self, active_entry: SyncStateEntry) -> bool:
        # 有効な本文ハッシュ以外のQdrantポイントだけを削除する
        try:
            points = self._vector_store.list_by_source(active_entry.source_id)
            active_points = [
                point
                for point in points
                if point.file_hash == active_entry.file_hash
                and point.relative_path == active_entry.relative_path
            ]
            self._vector_store.activate_points([point.point_id for point in active_points])
            if self._keyword_index is not None:
                self._keyword_index.activate_points([point.point_id for point in active_points])
            stale_ids = [
                point.point_id
                for point in points
                if point.file_hash != active_entry.file_hash
                or point.relative_path != active_entry.relative_path
            ]
            if self._keyword_index is not None:
                self._keyword_index.delete_points(stale_ids)
            self._vector_store.delete_points(stale_ids)
            return True
        except Exception:
            logger.warning("更新前のQdrantポイントを削除できませんでした。", exc_info=True)
            return False

    def _record_attempt_failure(
        self,
        previous: SyncStateEntry | None,
        attempted: SyncStateEntry,
        reason: str,
    ) -> None:
        # readyな旧版があれば残し、初回同期・再構築中だけ失敗状態を保存する
        if previous is not None and previous.status == "ready":
            logger.warning(
                "更新に失敗したため、直前のready版を検索可能なまま保持します: %s (%s)",
                previous.relative_path,
                reason,
            )
            return
        self._mark_failed(attempted, reason)

    def _is_permanent_failure(self, previous: SyncStateEntry | None, document: DocumentRef) -> bool:
        return (
            previous is not None
            and previous.status == "failed"
            and previous.failure_reason in DocumentProcessingError.PERMANENT_CODES
            and previous.embedding_model == self._embedding_model
            and bool(previous.source_revision)
            and previous.source_revision == document.revision
        )

    def _has_retryable_failures(self, source: DocumentSource) -> bool:
        # 通信エラーなど一時的な失敗や、中断された処理中状態が残っていれば再走査する
        return any(
            source.owns(entry.source_id)
            and entry.status != "ready"
            and entry.failure_reason not in DocumentProcessingError.PERMANENT_CODES
            for entry in self._state.list_all()
        )

    def _ensure_keyword_index(self, entry: SyncStateEntry) -> None:
        # 旧版で作られた索引など、キーワード索引に未登録の文書をQdrantの内容から補完
        if self._keyword_index is None or entry.chunk_count == 0:
            return
        try:
            if self._keyword_index.has_source(entry.source_id):
                return
            chunks = self._vector_store.list_by_source(entry.source_id)
            if chunks:
                self._keyword_index.replace_source(entry.source_id, chunks)
        except Exception:
            # キーワード索引は補助的な検索経路のため、失敗しても同期全体は止めない
            logger.warning("キーワード索引を補完できませんでした: %s", entry.relative_path, exc_info=True)

    def _new_entry(self, document: DocumentRef, file_hash: str) -> SyncStateEntry:
        # 同期元のメタデータから同期管理DBの1行を組み立てる
        return SyncStateEntry(
            source_id=document.source_id,
            relative_path=document.relative_path,
            file_hash=file_hash,
            status="processing",
            chunk_count=0,
            embedding_model=self._embedding_model,
            indexed_at=self._now(),
            source_revision=document.revision,
            modified_time=document.modified_time,
        )

    @staticmethod
    def _notify(callback: ProgressCallback | None, done: int, total: int, current: str) -> None:
        if callback is not None:
            callback(done, total, current)

    def _mark_failed(self, entry: SyncStateEntry, reason: str) -> None:
        # 元の処理結果を優先する補助的な失敗状態記録
        try:
            self._state.mark_failed(entry, reason)
        except Exception:
            logger.warning("失敗状態を記録できませんでした: %s", entry.relative_path, exc_info=True)

    @staticmethod
    def _hash_file(path: Path) -> str:
        # 大きなPDFも一定メモリで扱うSHA-256計算
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for block in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _now() -> str:
        # タイムゾーン付きUTC時刻の生成
        return datetime.now(timezone.utc).isoformat()
