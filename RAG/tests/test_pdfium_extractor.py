import unittest
from pathlib import Path
from unittest.mock import patch

from rag_app.domain.errors import DocumentProcessingError
from rag_app.domain.models import PdfFile
from rag_app.infrastructure.parsing.pdfium_extractor import PdfiumTextExtractor


class FakeTextPage:
    def __init__(self, text):
        self.text = text
        self.closed = False

    def get_text_range(self):
        return self.text

    def close(self):
        self.closed = True


class FakePage:
    def __init__(self, text):
        self.text_page = FakeTextPage(text)
        self.closed = False

    def get_textpage(self):
        return self.text_page

    def close(self):
        self.closed = True


class FakeDocument:
    def __init__(self, texts):
        self.pages = [FakePage(text) for text in texts]
        self.closed = False

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, index):
        return self.pages[index]

    def close(self):
        self.closed = True


class PdfiumExtractorTests(unittest.TestCase):
    def setUp(self):
        self.pdf = PdfFile("source", "documents/manual.pdf", "manual.pdf", Path("manual.pdf"))
        self.extractor = PdfiumTextExtractor()

    # 空ページを保ちながらPDF上のページ番号と資源解放を維持
    def test_page_numbers_follow_pdf_viewer_and_resources_are_closed(self):
        document = FakeDocument(["  1ページ目  ", " \n ", "3ページ目"])
        with patch("pypdfium2.PdfDocument", return_value=document):
            pages = self.extractor.extract(self.pdf, "sha256")

        self.assertEqual([page.page_number for page in pages], [1, 3])
        self.assertEqual([page.text for page in pages], ["1ページ目", "3ページ目"])
        self.assertTrue(document.closed)
        self.assertTrue(all(page.closed and page.text_page.closed for page in document.pages))

    # 空PDFとOCRが必要な画像PDFのエラー分類
    def test_empty_and_image_only_pdfs_are_distinguished(self):
        for texts, expected_code in (([], "empty_pdf"), (["  ", "\n"], "no_text")):
            with self.subTest(code=expected_code):
                document = FakeDocument(texts)
                with patch("pypdfium2.PdfDocument", return_value=document):
                    with self.assertRaises(DocumentProcessingError) as raised:
                        self.extractor.extract(self.pdf, "sha256")
                self.assertEqual(raised.exception.code, expected_code)
                self.assertTrue(document.closed)

    # 暗号化PDFと破損PDFのファイル単位エラー分類
    def test_encrypted_and_corrupt_pdfs_have_file_level_error_codes(self):
        cases = (("Password required", "encrypted_pdf"), ("Invalid xref table", "unreadable_pdf"))
        for message, expected_code in cases:
            with self.subTest(code=expected_code):
                with patch("pypdfium2.PdfDocument", side_effect=RuntimeError(message)):
                    with self.assertRaises(DocumentProcessingError) as raised:
                        self.extractor.extract(self.pdf, "sha256")
                self.assertEqual(raised.exception.code, expected_code)


if __name__ == "__main__":
    unittest.main()