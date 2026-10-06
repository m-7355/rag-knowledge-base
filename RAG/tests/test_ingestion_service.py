import tempfile
import unittest
from pathlib import Path

from rag_app.application.sync.ingestion_service import DocumentIngestionService
from rag_app.domain.models import PageText, TextChunk, deterministic_point_id
from rag_app.infrastructure.sources.local_folder_source import LocalFolderSource
from rag_app.infrastructure.storage.sqlite_sync_state_repository import SqliteSyncStateRepository


# 外部依存を置き換える同期処理用テストダブル
class FakeExtractor:
    def extract(self, pdf, file_hash):
        text = pdf.absolute_path.read_text(encoding="utf-8")
        return [PageText(pdf.source_id, pdf.relative_path, pdf.file_name, file_hash, 1, text)]


class FakeChunker:
    def split(self, page):
        texts = [piece for piece in page.text.split("|") if piece.strip()]
        return [
            TextChunk(
                deterministic_point_id(
                    page.source_id, page.page_number, index, page.file_hash, page.relative_path
                ),
                page.source_id,
                page.relative_path,
                page.file_name,
                page.file_hash,
                page.page_number,
                index,
                text,
            )
            for index, text in enumerate(texts)
        ]


class FakeEmbedder:
    def __init__(self):
        self.fail = False
        self.calls = 0
        self.on_embed = None

    def embed_passages(self, texts):
        self.calls += 1
        if self.on_embed is not None:
            self.on_embed()
        if self.fail:
            raise RuntimeError("model unavailable")
        return [[float(index), 1.0] for index, _ in enumerate(texts)]


class FakeVectorStore:
    def __init__(self):
        self.points = {}
        self.upsert_calls = 0
        self.deleted = []

    def upsert(self, chunks, vectors, searchable=True):
        self.upsert_calls += 1
        self.points.update({chunk.point_id: chunk for chunk in chunks})

    def activate_points(self, point_ids):
        return None

    def list_by_source(self, source_id):
        return [chunk for chunk in self.points.values() if chunk.source_id == source_id]

    def delete_points(self, point_ids):
        self.deleted.extend(point_ids)
        for point_id in point_ids:
            self.points.pop(point_id, None)


class FakeKeywordIndex:
    def __init__(self):
        self.points = {}

    def upsert_chunks(self, chunks, searchable=True):
        self.points.update({chunk.point_id: chunk for chunk in chunks})

    def activate_points(self, point_ids):
        return None

    def replace_source(self, source_id, chunks):
        self.delete_source(source_id)
        self.upsert_chunks(chunks)

    def delete_source(self, source_id):
        self.points = {
            point_id: chunk for point_id, chunk in self.points.items()
            if chunk.source_id != source_id
        }

    def delete_points(self, point_ids):
        for point_id in point_ids:
            self.points.pop(point_id, None)

    def has_source(self, source_id):
        return any(chunk.source_id == source_id for chunk in self.points.values())

    def search(self, query, limit):
        return []


class IngestionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.documents_dir = root / "documents"
        self.documents_dir.mkdir()
        self.pdf_path = self.documents_dir / "manual.pdf"
        self.pdf_path.write_text("最初の内容|別のチャンク", encoding="utf-8")
        self.source = LocalFolderSource(self.documents_dir, root)
        self.embedder = FakeEmbedder()
        self.vector_store = FakeVectorStore()
        self.keyword_index = FakeKeywordIndex()
        self.sync_state = SqliteSyncStateRepository(root / "data" / "sync_state.sqlite3")
        self.service = DocumentIngestionService(
            FakeExtractor(),
            FakeChunker(),
            self.embedder,
            self.vector_store,
            self.sync_state,
            "e5-model",
            self.keyword_index,
        )

    def tearDown(self):
        self.sync_state.close()
        self.temporary_directory.cleanup()

    def sync(self):
        return self.service.sync_source(self.source, force=True)

    # 同一版の再実行でファイル取得・Embedding・保存を重複させない
    def test_same_revision_is_skipped_without_duplicate_points(self):
        first = self.sync()
        second = self.sync()

        self.assertEqual(first.indexed, 1)
        self.assertEqual(second.skipped, 1)
        self.assertEqual(self.vector_store.upsert_calls, 1)
        self.assertEqual(len(self.vector_store.points), 2)

    # 新しい版のEmbedding中も旧ready版を検索可能に保つ
    def test_old_ready_revision_stays_active_until_new_revision_is_ready(self):
        self.sync()
        previous = self.sync_state.list_all()[0]
        self.pdf_path.write_text("更新された内容|追加部分", encoding="utf-8")

        def assert_previous_revision_is_still_active():
            current = self.sync_state.get(previous.source_id)
            self.assertEqual(current.status, "ready")
            self.assertEqual(current.file_hash, previous.file_hash)

        self.embedder.on_embed = assert_previous_revision_is_still_active
        report = self.sync()
        self.embedder.on_embed = None

        current = self.sync_state.get(previous.source_id)
        self.assertEqual(report.indexed, 1)
        self.assertEqual(current.status, "ready")
        self.assertNotEqual(current.file_hash, previous.file_hash)
        self.assertFalse(set(chunk.point_id for chunk in self.vector_store.points.values()) & {
            point_id for point_id in self.vector_store.deleted
        })
        self.assertEqual(len(self.vector_store.points), 2)

    # Embedding失敗時に旧ready状態と旧ポイントを維持し、次回同期で再試行
    def test_failed_embedding_preserves_previous_ready_revision(self):
        self.sync()
        previous = self.sync_state.list_all()[0]
        old_ids = set(self.vector_store.points)
        self.pdf_path.write_text("更新された内容|追加部分", encoding="utf-8")
        self.embedder.fail = True

        report = self.sync()

        self.assertEqual(report.failed, 1)
        self.assertEqual(set(self.vector_store.points), old_ids)
        self.assertEqual(self.vector_store.deleted, [])
        self.assertEqual(set(self.keyword_index.points), old_ids)
        self.assertEqual(self.sync_state.get(previous.source_id).status, "ready")
        self.assertEqual(self.sync_state.get(previous.source_id).file_hash, previous.file_hash)

    # 同期元から削除されたPDFのベクトル・キーワード索引・状態を除去
    def test_deleted_pdf_points_and_sync_state_are_removed(self):
        self.sync()
        self.pdf_path.unlink()

        report = self.sync()

        self.assertEqual(report.removed, 1)
        self.assertEqual(self.vector_store.points, {})
        self.assertEqual(self.keyword_index.points, {})
        self.assertEqual(self.sync_state.list_all(), [])

    # 更新PDFは版ごとに異なるポイントIDへ書き、切替後に旧ポイントを消す
    def test_changed_pdf_refreshes_hash_and_removes_old_point_ids(self):
        self.sync()
        previous = self.sync_state.list_all()[0]
        old_ids = set(self.vector_store.points)
        self.pdf_path.write_text("更新後", encoding="utf-8")

        report = self.sync()

        self.assertEqual(report.indexed, 1)
        self.assertNotEqual(self.sync_state.get(previous.source_id).file_hash, previous.file_hash)
        self.assertTrue(old_ids.isdisjoint(self.vector_store.points))
        self.assertEqual(len(self.vector_store.points), 1)


if __name__ == "__main__":
    unittest.main()