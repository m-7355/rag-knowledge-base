"""原子的な置換と厳格な破損検査を備えたJSONマニフェスト（旧形式）

現在の同期状態はSQLite（sqlite_sync_state_repository）で管理する。
このクラスは旧版で作られた data/index_manifest.json を移行時に読み込むために残している。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, replace
from pathlib import Path

from rag_app.domain.errors import SyncStateError
from rag_app.domain.models import SYNC_STATUSES, SyncStateEntry


# 索引状態をJSONファイルへ安全に永続化するリポジトリ
class JsonManifestRepository:
    VERSION = 1

    def __init__(self, path: Path):
        self.path = path
        self._entries: dict[str, SyncStateEntry] | None = None
        # 同一プロセス内の状態更新とファイル保存を直列化
        self._lock = threading.RLock()

    def get(self, source_id: str) -> SyncStateEntry | None:
        with self._lock:
            return self._load().get(source_id)

    def list_all(self) -> list[SyncStateEntry]:
        with self._lock:
            return list(self._load().values())

    def mark_processing(self, entry: SyncStateEntry) -> None:
        self._set(replace(entry, status="processing", failure_reason=None))

    def mark_ready(self, entry: SyncStateEntry) -> None:
        self._set(replace(entry, status="ready", failure_reason=None))

    def mark_failed(self, entry: SyncStateEntry, reason: str) -> None:
        self._set(replace(entry, status="failed", failure_reason=reason))

    def remove(self, source_id: str) -> None:
        with self._lock:
            entries = self._load()
            if source_id in entries:
                del entries[source_id]
                self._save()

    def clear(self) -> None:
        with self._lock:
            self._entries = {}
            self._save()

    def _set(self, entry: SyncStateEntry) -> None:
        with self._lock:
            self._load()[entry.source_id] = entry
            self._save()

    def _load(self) -> dict[str, SyncStateEntry]:
        # キャッシュ優先の読込と破損ファイルの厳格な拒否
        if self._entries is not None:
            return self._entries
        if not self.path.exists():
            self._entries = {}
            return self._entries

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("version") != self.VERSION or not isinstance(raw.get("entries"), dict):
                raise ValueError("unsupported manifest structure")
            entries: dict[str, SyncStateEntry] = {}
            for source_id, values in raw["entries"].items():
                entry = SyncStateEntry(**values)
                if source_id != entry.source_id:
                    raise ValueError("manifest source key mismatch")
                if entry.status not in SYNC_STATUSES:
                    raise ValueError("invalid manifest status")
                entries[source_id] = entry
        except Exception as exc:
            raise SyncStateError(
                "索引マニフェストを読み込めません。既存ファイルを退避して明示的に再構築してください。"
            ) from exc
        self._entries = entries
        return entries

    def _save(self) -> None:
        # 一時ファイルへの書込後に置換する原子的な保存
        entries = self._entries or {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f"{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(
                    {
                        "version": self.VERSION,
                        "entries": {
                            source_id: asdict(entry)
                            for source_id, entry in sorted(entries.items())
                        },
                    },
                    temporary_file,
                    ensure_ascii=False,
                    indent=2,
                )
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
        except Exception as exc:
            raise SyncStateError("索引マニフェストを保存できません。") from exc
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)
