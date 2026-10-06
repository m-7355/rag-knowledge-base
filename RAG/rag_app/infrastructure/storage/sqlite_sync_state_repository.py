"""SQLiteによる同期管理DB

役割分担:
    Qdrant  … 検索用DB（チャンク本文とベクトル）
    SQLite  … 同期管理DB（ファイルごとの版・内容ハッシュ・同期状態・最終同期時刻）

Webアプリの同期Workerスレッドと質問処理スレッドから同時に使うため、
1接続をロックで直列化して扱う。
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import fields, replace
from pathlib import Path

from rag_app.domain.errors import SyncStateError
from rag_app.domain.models import SYNC_STATUSES, SyncStateEntry
from rag_app.infrastructure.storage.json_manifest_repository import JsonManifestRepository


# 旧JSONマニフェストの取込済みを示すメタデータキー
_LEGACY_IMPORTED_KEY = "legacy_manifest_imported"
_COLUMNS = [field.name for field in fields(SyncStateEntry)]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    source_id       TEXT PRIMARY KEY,
    relative_path   TEXT NOT NULL,
    file_hash       TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('processing', 'ready', 'failed')),
    chunk_count     INTEGER NOT NULL,
    embedding_model TEXT NOT NULL,
    indexed_at      TEXT NOT NULL,
    failure_reason  TEXT,
    source_revision TEXT NOT NULL DEFAULT '',
    modified_time   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sync_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ファイル単位の同期状態と同期元チェックポイントを保存するリポジトリ
class SqliteSyncStateRepository:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Workerスレッドと質問スレッドで共有するためスレッド検査を外し、ロックで保護する
            self._connection = sqlite3.connect(str(path), check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            # 書込中でも読取を止めないWALモード
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise SyncStateError("同期管理DBを開けません。data フォルダーの権限を確認してください。") from exc

    # ---- 文書ごとの同期状態 ----

    def get(self, source_id: str) -> SyncStateEntry | None:
        row = self._fetch_one("SELECT * FROM documents WHERE source_id = ?", (source_id,))
        return self._entry(row) if row is not None else None

    def list_all(self) -> list[SyncStateEntry]:
        rows = self._fetch_all("SELECT * FROM documents ORDER BY relative_path")
        return [self._entry(row) for row in rows]

    def count_ready(self) -> int:
        row = self._fetch_one("SELECT COUNT(*) AS total FROM documents WHERE status = 'ready'")
        return int(row["total"]) if row is not None else 0

    def mark_processing(self, entry: SyncStateEntry) -> None:
        self._upsert(replace(entry, status="processing", failure_reason=None))

    def mark_ready(self, entry: SyncStateEntry) -> None:
        self._upsert(replace(entry, status="ready", failure_reason=None))

    def mark_failed(self, entry: SyncStateEntry, reason: str) -> None:
        self._upsert(replace(entry, status="failed", failure_reason=reason))

    def remove(self, source_id: str) -> None:
        self._execute("DELETE FROM documents WHERE source_id = ?", (source_id,))

    def clear(self) -> None:
        # 全件再構築時は文書状態と同期元チェックポイントをまとめて初期化
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute("DELETE FROM documents")
                    self._connection.execute(
                        "DELETE FROM sync_meta WHERE key <> ?", (_LEGACY_IMPORTED_KEY,)
                    )
            except sqlite3.Error as exc:
                raise SyncStateError("同期管理DBを初期化できません。") from exc

    # ---- 同期元ごとのチェックポイント ----

    def get_meta(self, key: str) -> str | None:
        row = self._fetch_one("SELECT value FROM sync_meta WHERE key = ?", (key,))
        return str(row["value"]) if row is not None else None

    def set_meta(self, key: str, value: str) -> None:
        self._execute(
            "INSERT INTO sync_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ---- 旧形式からの移行 ----

    def import_legacy_manifest(self, manifest_path: Path) -> int:
        """旧 index_manifest.json の内容を一度だけ取り込み、既存の索引をそのまま検索可能に保つ"""
        with self._lock:
            if self.get_meta(_LEGACY_IMPORTED_KEY) is not None:
                return 0
            imported = 0
            if manifest_path.exists():
                for entry in JsonManifestRepository(manifest_path).list_all():
                    if self.get(entry.source_id) is None:
                        self._upsert(entry)
                        imported += 1
            self.set_meta(_LEGACY_IMPORTED_KEY, "1")
            return imported

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # ---- 内部処理 ----

    def _upsert(self, entry: SyncStateEntry) -> None:
        if entry.status not in SYNC_STATUSES:
            raise SyncStateError("同期状態の値が不正です。")
        placeholders = ", ".join("?" for _ in _COLUMNS)
        updates = ", ".join(f"{column} = excluded.{column}" for column in _COLUMNS[1:])
        self._execute(
            f"INSERT INTO documents({', '.join(_COLUMNS)}) VALUES({placeholders}) "
            f"ON CONFLICT(source_id) DO UPDATE SET {updates}",
            tuple(getattr(entry, column) for column in _COLUMNS),
        )

    def _execute(self, sql: str, parameters: tuple = ()) -> None:
        with self._lock:
            try:
                # with文で成功時コミット・失敗時ロールバック
                with self._connection:
                    self._connection.execute(sql, parameters)
            except sqlite3.Error as exc:
                raise SyncStateError("同期管理DBへ保存できません。") from exc

    def _fetch_one(self, sql: str, parameters: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            try:
                return self._connection.execute(sql, parameters).fetchone()
            except sqlite3.Error as exc:
                raise SyncStateError("同期管理DBを読み込めません。") from exc

    def _fetch_all(self, sql: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            try:
                return self._connection.execute(sql, parameters).fetchall()
            except sqlite3.Error as exc:
                raise SyncStateError("同期管理DBを読み込めません。") from exc

    @staticmethod
    def _entry(row: sqlite3.Row) -> SyncStateEntry:
        return SyncStateEntry(**{column: row[column] for column in _COLUMNS})
