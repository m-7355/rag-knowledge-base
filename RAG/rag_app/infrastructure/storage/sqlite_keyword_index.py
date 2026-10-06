"""SQLite FTS5（trigram）によるキーワード検索索引

ベクトル検索は「意味が近い文章」に強い一方、型番「ABC-123」のような固有文字列には弱い。
そのためチャンク本文をFTS5へも登録し、BM25で順位付けしたキーワード検索結果を
ベクトル検索結果と融合（Hybrid Search）する。

日本語は単語の区切りが空白で示されないため、形態素解析の代わりに
3文字単位（trigram）で索引し、質問からは漢字・カタカナ・英数字の語を抜き出して検索する。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import unicodedata
from pathlib import Path
from typing import Sequence

from rag_app.domain.errors import DependencyUnavailable
from rag_app.domain.models import TextChunk

logger = logging.getLogger(__name__)

# 1回の検索に使う語の上限（長い質問でOR条件が膨らみすぎないようにする）
_MAX_TERMS = 32
# 英数字の識別子（型番・コード値など）は区切らずに1語として扱う
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-./]*[A-Za-z0-9]")
# 漢字・カタカナの連続（ひらがなは助詞・活用語尾が多いため区切りとして扱う）
_CJK_RUN = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF々〆ヵヶ\u30A0-\u30FF]+")


def _normalize(text: str) -> str:
    # 全角英数字・半角カナの表記揺れをそろえる
    return unicodedata.normalize("NFKC", text)


def extract_terms(question: str) -> list[str]:
    """質問からtrigram索引で検索できる3文字以上の語を抜き出す"""
    text = _normalize(question)
    terms: list[str] = []
    for match in _IDENTIFIER.finditer(text):
        if len(match.group(0)) >= 3:
            terms.append(match.group(0))
    for match in _CJK_RUN.finditer(text):
        run = match.group(0)
        # 長い語は3文字ずつずらして部分一致させ、表記の一部違いにも強くする
        terms.extend(run[index : index + 3] for index in range(len(run) - 2))
    unique = list(dict.fromkeys(term.casefold() for term in terms))
    return unique[:_MAX_TERMS]


# チャンク本文の全文検索索引（ソース単位で置換・削除できる）
class SqliteKeywordIndex:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 同期Workerと質問処理の両スレッドから使うため、ロックで直列化する
            self._connection = sqlite3.connect(str(path), check_same_thread=False)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5("
                "text, point_id UNINDEXED, source_id UNINDEXED, index_state UNINDEXED, "
                "tokenize='trigram')"
            )
        except sqlite3.Error as exc:
            raise DependencyUnavailable(
                "キーワード検索索引を開けません。SQLiteのFTS5（trigram）が利用できるか確認してください。"
            ) from exc

    def replace_source(self, source_id: str, chunks: Sequence[TextChunk]) -> None:
        # 1ファイル分のキーワード索引をトランザクションで置き換える
        rows = [(_normalize(chunk.text), chunk.point_id, source_id, "ready") for chunk in chunks]
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute("DELETE FROM chunk_fts WHERE source_id = ?", (source_id,))
                    self._connection.executemany(
                        "INSERT INTO chunk_fts(text, point_id, source_id, index_state) "
                        "VALUES(?, ?, ?, ?)", rows
                    )
            except sqlite3.Error as exc:
                raise DependencyUnavailable("キーワード検索索引を更新できません。") from exc

    def upsert_chunks(self, chunks: Sequence[TextChunk], searchable: bool = True) -> None:
        # 新版だけを追加・更新し、準備中はキーワード検索から除外
        state = "ready" if searchable else "pending"
        rows = [(_normalize(chunk.text), chunk.point_id, chunk.source_id, state) for chunk in chunks]
        with self._lock:
            try:
                with self._connection:
                    self._connection.executemany(
                        "DELETE FROM chunk_fts WHERE point_id = ?",
                        [(chunk.point_id,) for chunk in chunks],
                    )
                    self._connection.executemany(
                        "INSERT INTO chunk_fts(text, point_id, source_id, index_state) "
                        "VALUES(?, ?, ?, ?)", rows
                    )
            except sqlite3.Error as exc:
                raise DependencyUnavailable("キーワード検索索引を更新できません。") from exc

    def activate_points(self, point_ids: Sequence[str]) -> None:
        # SQLiteのready状態切替後に新版ポイントをキーワード検索へ公開
        if not point_ids:
            return
        with self._lock:
            try:
                with self._connection:
                    self._connection.executemany(
                        "UPDATE chunk_fts SET index_state = 'ready' WHERE point_id = ?",
                        [(point_id,) for point_id in point_ids],
                    )
            except sqlite3.Error as exc:
                raise DependencyUnavailable("キーワード索引を検索可能にできません。") from exc

    def delete_source(self, source_id: str) -> None:
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute("DELETE FROM chunk_fts WHERE source_id = ?", (source_id,))
            except sqlite3.Error as exc:
                raise DependencyUnavailable("キーワード検索索引から削除できません。") from exc

    def delete_points(self, point_ids: Sequence[str]) -> None:
        # Qdrantで不要になった旧版のポイントだけを削除
        if not point_ids:
            return
        with self._lock:
            try:
                with self._connection:
                    self._connection.executemany(
                        "DELETE FROM chunk_fts WHERE point_id = ?",
                        [(point_id,) for point_id in point_ids],
                    )
            except sqlite3.Error as exc:
                raise DependencyUnavailable("キーワード索引の古いポイントを削除できません。") from exc

    def has_source(self, source_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM chunk_fts WHERE source_id = ? LIMIT 1", (source_id,)
            ).fetchone()
        return row is not None

    def search(self, query: str, limit: int) -> list[str]:
        terms = extract_terms(query)
        if not terms or limit <= 0:
            return []
        # 各語をフレーズとして引用し、FTS5の演算子として解釈させない
        match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        with self._lock:
            try:
                rows = self._connection.execute(
                    "SELECT point_id FROM chunk_fts WHERE chunk_fts MATCH ? "
                    "AND index_state = 'ready' "
                    "ORDER BY bm25(chunk_fts) LIMIT ?",
                    (match, limit),
                ).fetchall()
            except sqlite3.Error:
                logger.warning("キーワード検索に失敗したため、ベクトル検索のみで続行します。", exc_info=True)
                return []
        return [str(row[0]) for row in rows]

    def clear(self) -> None:
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute("DELETE FROM chunk_fts")
            except sqlite3.Error as exc:
                raise DependencyUnavailable("キーワード検索索引を初期化できません。") from exc

    def close(self) -> None:
        with self._lock:
            self._connection.close()
