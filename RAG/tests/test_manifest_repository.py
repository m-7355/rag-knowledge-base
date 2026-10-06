import json
import tempfile
import unittest
from pathlib import Path

from rag_app.domain.errors import SyncStateError
from rag_app.domain.models import SyncStateEntry
from rag_app.infrastructure.storage.json_manifest_repository import JsonManifestRepository
from rag_app.infrastructure.storage.sqlite_sync_state_repository import SqliteSyncStateRepository


def entry(status="processing"):
    return SyncStateEntry(
        source_id="source",
        relative_path="documents/a.pdf",
        file_hash="digest",
        status=status,
        chunk_count=2,
        embedding_model="model",
        indexed_at="2026-10-01T00:00:00+00:00",
    )


class ManifestRepositoryTests(unittest.TestCase):
    # 状態変更の永続化と一時ファイル残存防止
    def test_status_updates_are_atomically_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index_manifest.json"
            repository = JsonManifestRepository(path)
            repository.mark_processing(entry())
            repository.mark_ready(entry())

            self.assertEqual(repository.get("source").status, "ready")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 1)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    # 破損マニフェストを上書きせず明示エラーにする契約
    def test_corrupt_manifest_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index_manifest.json"
            path.write_text("{broken", encoding="utf-8")

            with self.assertRaises(SyncStateError):
                JsonManifestRepository(path).list_all()
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    # SQLiteの同期状態・変更トークンを再起動後も保持
    def test_sqlite_state_and_change_checkpoint_survive_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sync_state.sqlite3"
            repository = SqliteSyncStateRepository(path)
            repository.mark_ready(entry("ready"))
            repository.set_meta("drive:folder:token", "page-token")
            repository.close()

            reopened = SqliteSyncStateRepository(path)
            self.assertEqual(reopened.get("source").status, "ready")
            self.assertEqual(reopened.count_ready(), 1)
            self.assertEqual(reopened.get_meta("drive:folder:token"), "page-token")
            reopened.close()

    # 既存JSONのready索引を初回移行し、2回目は重複取込しない
    def test_legacy_manifest_is_imported_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_path = root / "index_manifest.json"
            legacy = JsonManifestRepository(legacy_path)
            legacy.mark_ready(entry("ready"))
            repository = SqliteSyncStateRepository(root / "sync_state.sqlite3")

            self.assertEqual(repository.import_legacy_manifest(legacy_path), 1)
            self.assertEqual(repository.get("source").status, "ready")
            self.assertEqual(repository.import_legacy_manifest(legacy_path), 0)
            repository.close()


if __name__ == "__main__":
    unittest.main()