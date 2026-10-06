"""SQLite FTS5キーワード索引の日本語・型番検索と公開状態のテスト"""

import tempfile
import unittest
from pathlib import Path

from rag_app.domain.models import TextChunk
from rag_app.infrastructure.storage.sqlite_keyword_index import SqliteKeywordIndex, extract_terms


def chunk(point_id="point-1", text="製品Aの型番ABC-123の保証期間は1年間です。"):
    return TextChunk(
        point_id=point_id,
        source_id="gdrive:file-1",
        relative_path="gdrive/manual.pdf",
        file_name="manual.pdf",
        file_hash="digest",
        page_number=3,
        chunk_index=0,
        text=text,
    )


class KeywordIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.index = SqliteKeywordIndex(Path(self.temporary_directory.name) / "keywords.sqlite3")

    def tearDown(self):
        self.index.close()
        self.temporary_directory.cleanup()

    def test_extracts_identifier_and_japanese_terms(self):
        terms = extract_terms("製品Aの型番 ABC-123、保証期間は？")

        self.assertIn("abc-123", terms)
        self.assertIn("保証期", terms)
        self.assertIn("証期間", terms)

    def test_pending_revision_is_hidden_until_activated(self):
        document = chunk()
        self.index.upsert_chunks([document], searchable=False)

        self.assertEqual(self.index.search("型番ABC-123の保証期間", 5), [])

        self.index.activate_points([document.point_id])

        self.assertEqual(self.index.search("型番ABC-123の保証期間", 5), [document.point_id])

    def test_old_and_new_revisions_can_be_staged_independently(self):
        old = chunk("old", "型番ABC-123の保証期間は1年間です。")
        new = chunk("new", "型番ABC-123の保証期間は2年間です。")
        self.index.upsert_chunks([old])
        self.index.upsert_chunks([new], searchable=False)

        self.assertEqual(self.index.search("ABC-123 保証期間", 5), [old.point_id])

        self.index.activate_points([new.point_id])
        self.index.delete_points([old.point_id])
        self.assertEqual(self.index.search("ABC-123 保証期間", 5), [new.point_id])


if __name__ == "__main__":
    unittest.main()
