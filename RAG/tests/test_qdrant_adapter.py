import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qdrant_client.http.models import Distance, VectorParams

from rag_app.domain.errors import CollectionMismatchError, DependencyUnavailable
from rag_app.domain.models import TextChunk, deterministic_point_id
from rag_app.infrastructure.storage.qdrant_vector_store import QdrantVectorStore


class FakeQdrantClient:
    def __init__(self, vectors=None, exists=True):
        self.vectors = vectors
        self.exists = exists
        self.deleted = False
        self.created = []

    def collection_exists(self, name):
        return self.exists

    def get_collection(self, name):
        return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=self.vectors)))

    def create_collection(self, **kwargs):
        self.created.append(kwargs)
        self.exists = True

    def delete_collection(self, name):
        self.deleted = True
        self.exists = False


class QdrantCompatibilityTests(unittest.TestCase):
    def settings(self, root):
        return SimpleNamespace(
            qdrant_url="",
            qdrant_path=root / "data" / "qdrant",
            collection_name="pdf_knowledge_v1",
            embedding_model="e5-model",
        )

    # 次元不一致を検出して既存コレクションを保持
    def test_existing_dimension_mismatch_fails_without_deleting_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeQdrantClient(VectorParams(size=384, distance=Distance.COSINE))
            store = QdrantVectorStore(self.settings(Path(directory)), client=client)

            with self.assertRaises(CollectionMismatchError):
                store.ensure_collection(768, "e5-model")

            self.assertFalse(client.deleted)
            self.assertEqual(client.created, [])

    # 新規コレクションのCosineベクトル設定
    def test_missing_collection_is_created_with_cosine_vectors(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeQdrantClient(exists=False)
            store = QdrantVectorStore(self.settings(Path(directory)), client=client)

            store.ensure_collection(768, "e5-model")

            self.assertEqual(client.created[0]["vectors_config"].size, 768)
            self.assertEqual(client.created[0]["vectors_config"].distance, Distance.COSINE)

    # Qdrant Localの排他ロックに対する実行可能な案内
    def test_local_storage_lock_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory))
            error = RuntimeError(
                f"Storage folder {settings.qdrant_path} is already accessed by another instance of Qdrant client."
            )
            with patch("qdrant_client.QdrantClient", side_effect=error):
                with self.assertRaises(DependencyUnavailable) as raised:
                    QdrantVectorStore(settings)

        self.assertIn("別のプロセスで使用中", str(raised.exception))
        self.assertIn("索引CLI", str(raised.exception))

    # 内容更新中のpendingポイントを除外し、ready切替後に検索へ含める
    def test_pending_revision_is_hidden_until_activated(self):
        from qdrant_client import QdrantClient

        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(Path(directory))
            client = QdrantClient(":memory:")
            store = QdrantVectorStore(settings, client=client)
            store.ensure_collection(2, "e5-model")
            old = TextChunk(
                deterministic_point_id("source", 1, 0, "old-hash", "manual.pdf"),
                "source", "manual.pdf", "manual.pdf", "old-hash", 1, 0, "old revision",
            )
            new = TextChunk(
                deterministic_point_id("source", 1, 0, "new-hash", "manual.pdf"),
                "source", "manual.pdf", "manual.pdf", "new-hash", 1, 0, "new revision",
            )

            store.upsert([old], [[1.0, 0.0]])
            store.upsert([new], [[0.9, 0.1]], searchable=False)
            self.assertEqual([hit.chunk.file_hash for hit in store.search([1.0, 0.0], 5)], ["old-hash"])

            store.activate_points([new.point_id])
            hashes = {hit.chunk.file_hash for hit in store.search([1.0, 0.0], 5)}
            self.assertEqual(hashes, {"old-hash", "new-hash"})

            store.delete_points([old.point_id])
            self.assertEqual([hit.chunk.file_hash for hit in store.search([1.0, 0.0], 5)], ["new-hash"])
            store.close()


if __name__ == "__main__":
    unittest.main()