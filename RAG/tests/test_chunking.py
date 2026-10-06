import unittest

from rag_app.domain.models import PageText
from rag_app.infrastructure.parsing.page_aware_chunker import PageAwareTextChunker


class PageAwareChunkerTests(unittest.TestCase):
    # 分割サイズ・ページ番号・ポイントIDの維持
    def test_chunks_obey_size_and_keep_page_metadata(self):
        chunker = PageAwareTextChunker(chunk_size=60, chunk_overlap=12)
        page = PageText(
            source_id="source",
            relative_path="documents/manual.pdf",
            file_name="manual.pdf",
            file_hash="hash",
            page_number=3,
            text=("返品の申請は購入日から30日以内です。\n" * 12),
        )

        chunks = chunker.split(page)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk.text) <= 60 for chunk in chunks))
        self.assertTrue(all(chunk.page_number == 3 for chunk in chunks))
        self.assertEqual(len({chunk.point_id for chunk in chunks}), len(chunks))

    # 空白だけのページからチャンクを作らない確認
    def test_blank_page_produces_no_chunks(self):
        chunker = PageAwareTextChunker(chunk_size=60, chunk_overlap=12)
        page = PageText("source", "documents/a.pdf", "a.pdf", "hash", 1, " \n ")

        self.assertEqual(chunker.split(page), [])


if __name__ == "__main__":
    unittest.main()