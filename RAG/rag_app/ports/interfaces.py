"""インフラストラクチャアダプターが実装する契約

アプリケーション層はこのプロトコルだけに依存し、Qdrant・SQLite・Google APIなどの
具体的なライブラリには依存しない（テストではFakeへ差し替える）。
"""

from __future__ import annotations

from typing import Protocol, Sequence

from rag_app.domain.models import (
    DocumentRef,
    PageText,
    PdfFile,
    RetrievedChunk,
    SyncStateEntry,
    TextChunk,
)


# ---- Sync（同期）側の契約 ----


# 同期元（ローカルフォルダー、Google Driveなど）の契約
class DocumentSource(Protocol):
    # 画面やログに表示する同期元の名前
    name: str

    # この同期元が管理するソースIDか（削除検知を他の同期元へ波及させない）
    def owns(self, source_id: str) -> bool: ...

    # 前回の同期以降に変更がありうるか（Falseなら走査自体を省略できる）
    def has_changes(self) -> bool: ...

    # 本文を取得せずに、現在存在する文書のメタデータだけを列挙
    def list_documents(self) -> list[DocumentRef]: ...

    # 変更があった文書だけ本文を取得し、ローカルで読めるPDFとして返す
    def fetch(self, document: DocumentRef) -> PdfFile: ...

    # fetchで作った一時ファイルなどの後片付け
    def release(self, pdf: PdfFile) -> None: ...

    # 同期完了後に、次回の変更検知の基準点を進める
    def commit_checkpoint(self) -> None: ...


# PDFページごとの本文抽出契約
class PdfTextExtractor(Protocol):
    def extract(self, pdf: PdfFile, file_hash: str) -> list[PageText]: ...


# ページ単位のテキスト分割契約
class TextChunker(Protocol):
    def split(self, page_text: PageText) -> list[TextChunk]: ...


# 同期状態（同期管理DB）の永続化契約
class SyncStateRepository(Protocol):
    def get(self, source_id: str) -> SyncStateEntry | None: ...

    def list_all(self) -> list[SyncStateEntry]: ...

    def count_ready(self) -> int: ...

    def mark_processing(self, entry: SyncStateEntry) -> None: ...

    def mark_ready(self, entry: SyncStateEntry) -> None: ...

    def mark_failed(self, entry: SyncStateEntry, reason: str) -> None: ...

    def remove(self, source_id: str) -> None: ...

    def clear(self) -> None: ...

    # 同期元ごとのチェックポイント（Driveの変更トークンなど）の保存
    def get_meta(self, key: str) -> str | None: ...

    def set_meta(self, key: str, value: str) -> None: ...


# ---- 検索索引（Syncで更新し、Queryで参照）の契約 ----


# 文書と質問のベクトル化契約
class EmbeddingProvider(Protocol):
    def dimension(self) -> int: ...

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


# ベクトル保存・検索・削除の契約
class VectorStore(Protocol):
    def ensure_collection(self, dimension: int, model_name: str) -> None: ...

    def upsert(
        self,
        chunks: Sequence[TextChunk],
        vectors: Sequence[Sequence[float]],
        searchable: bool = True,
    ) -> None: ...

    def activate_points(self, point_ids: Sequence[str]) -> None: ...

    # point_ids指定時はそのポイントだけを対象に類似度を計算する
    def search(
        self,
        vector: Sequence[float],
        limit: int,
        point_ids: Sequence[str] | None = None,
    ) -> list[RetrievedChunk]: ...

    def list_by_source(self, source_id: str) -> list[TextChunk]: ...

    def delete_points(self, point_ids: Sequence[str]) -> None: ...

    def health(self) -> str: ...

    def rebuild_collection(self, dimension: int, model_name: str) -> None: ...

    def close(self) -> None: ...


# 固有名詞・型番などを拾うキーワード（BM25）索引の契約
class KeywordIndex(Protocol):
    # 新版を旧版と併存させて事前登録し、状態切替前の検索欠落を防ぐ
    def upsert_chunks(self, chunks: Sequence[TextChunk], searchable: bool = True) -> None: ...

    def activate_points(self, point_ids: Sequence[str]) -> None: ...

    def replace_source(self, source_id: str, chunks: Sequence[TextChunk]) -> None: ...

    def delete_source(self, source_id: str) -> None: ...

    def delete_points(self, point_ids: Sequence[str]) -> None: ...

    def has_source(self, source_id: str) -> bool: ...

    # 関連度の高い順にポイントIDを返す
    def search(self, query: str, limit: int) -> list[str]: ...

    def clear(self) -> None: ...


# ---- Query（質問応答）側の契約 ----


# 検索候補を質問との関連性で並べ替える再順位付けの契約
class Reranker(Protocol):
    def rerank(self, query: str, candidates: Sequence[RetrievedChunk]) -> list[RetrievedChunk]: ...


# 回答生成モデルと稼働確認の契約
class ChatModel(Protocol):
    def generate(self, messages: Sequence[dict[str, str]]) -> str: ...

    def health(self) -> str: ...
