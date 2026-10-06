"""アプリケーション層とアダプターで共有するドメイン型

データの流れ:
    Sync : DocumentRef → PdfFile → PageText → TextChunk → (Qdrant / キーワード索引)
                                                     └ SyncStateEntry（同期管理DB）
    Query: 質問 → RetrievedChunk → AnswerResult / AnswerSource
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path


# 保存済みポイントIDの再現に使う固定UUID名前空間
_POINT_NAMESPACE = uuid.UUID("dcd49b4d-fce4-5f09-aed1-f3dd5d487c09")

# 同期状態として許可する値（readyだけが検索対象）
SYNC_STATUSES = frozenset({"processing", "ready", "failed"})


# ソースの内容版・ページ・チャンク番号から作る安定したポイントID
def deterministic_point_id(
    source_id: str,
    page_number: int,
    chunk_index: int,
    file_hash: str = "",
    relative_path: str = "",
) -> str:
    return str(
        uuid.uuid5(
            _POINT_NAMESPACE,
            f"{source_id}:{file_hash}:{relative_path}:{page_number}:{chunk_index}",
        )
    )


# 同期元に存在する1文書のメタデータ（本文はまだ取得していない）
@dataclass(frozen=True)
class DocumentRef:
    source_id: str
    relative_path: str
    file_name: str
    # 変更検知用の版識別子。前回と同じなら本文を取得せずにスキップできる
    revision: str
    modified_time: str = ""


# 本文を取得済みで、ローカルパスから読めるPDF
@dataclass(frozen=True)
class PdfFile:
    source_id: str
    relative_path: str
    file_name: str
    absolute_path: Path


# PDFページ単位の抽出済みテキスト
@dataclass(frozen=True)
class PageText:
    source_id: str
    relative_path: str
    file_name: str
    file_hash: str
    page_number: int
    text: str

    def __post_init__(self) -> None:
        # PDF閲覧画面と一致する1始まりのページ番号
        if self.page_number < 1:
            raise ValueError("PDF page numbers start at 1.")


# 検索用に分割したページ内テキスト
@dataclass(frozen=True)
class TextChunk:
    point_id: str
    source_id: str
    relative_path: str
    file_name: str
    file_hash: str
    page_number: int
    chunk_index: int
    text: str

    def __post_init__(self) -> None:
        # 不正なページ番号と空チャンクの排除
        if self.page_number < 1:
            raise ValueError("PDF page numbers start at 1.")
        if not self.text.strip():
            raise ValueError("A text chunk must contain non-whitespace text.")


# 検索されたチャンクと類似度（scoreはコサイン類似度、rerank_scoreは再順位付けの評価値）
@dataclass(frozen=True)
class RetrievedChunk:
    chunk: TextChunk
    score: float
    rerank_score: float | None = None


# 回答と一緒に返す出典情報（urlは元ファイルを開ける場合だけ設定）
@dataclass(frozen=True)
class AnswerSource:
    reference_id: str
    file_name: str
    relative_path: str
    page_number: int
    score: float
    excerpt: str
    url: str | None = None


# 回答本文・出典・棄却状態の結果
@dataclass(frozen=True)
class AnswerResult:
    answer: str
    sources: list[AnswerSource]
    abstained: bool
    abstain_reason: str | None = None


# 1ファイル分の同期状態（同期管理DBの1行）
@dataclass(frozen=True)
class SyncStateEntry:
    source_id: str
    relative_path: str
    # 本文のSHA-256。Qdrantのペイロードと一致する場合だけ検索結果を採用する
    file_hash: str
    status: str
    chunk_count: int
    embedding_model: str
    # 最後に同期処理を行った時刻（UTC）
    indexed_at: str
    failure_reason: str | None = None
    # 同期元の版識別子と更新日時（本文を取得しない差分判定に使う）
    source_revision: str = ""
    modified_time: str = ""


# 1ファイル分の索引処理結果
@dataclass(frozen=True)
class IngestionItemResult:
    relative_path: str
    status: str
    chunk_count: int = 0
    message: str = ""


# 1つの同期元に対する同期結果と集計値
@dataclass(frozen=True)
class IngestionReport:
    items: list[IngestionItemResult]
    # 変更検知で「変更なし」と判定し、ファイル走査自体を省略したか
    checked_only: bool = False

    # 状態ごとの処理件数を返す集計プロパティ群
    @property
    def indexed(self) -> int:
        return sum(item.status == "indexed" for item in self.items)

    @property
    def skipped(self) -> int:
        return sum(item.status == "unchanged" for item in self.items)

    @property
    def failed(self) -> int:
        return sum(item.status == "failed" for item in self.items)

    @property
    def removed(self) -> int:
        return sum(item.status == "removed" for item in self.items)
