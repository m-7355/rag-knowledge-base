import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_app.infrastructure.sources.local_folder_source import LocalFolderSource


class DocumentScannerTests(unittest.TestCase):
    # サブフォルダーを含むPDF検出と相対パス形式
    def test_scans_pdf_files_recursively_and_normalizes_relative_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = root / "documents"
            nested = documents / "manuals"
            nested.mkdir(parents=True)
            (documents / "top.PDF").write_bytes(b"not parsed by scanner")
            (nested / "guide.pdf").write_bytes(b"not parsed by scanner")
            (nested / "ignore.txt").write_text("not a PDF", encoding="utf-8")

            result = LocalFolderSource(documents, root).list_documents()

        self.assertEqual(
            [document.relative_path for document in result],
            ["documents/manuals/guide.pdf", "documents/top.PDF"],
        )
        self.assertEqual(len({pdf.source_id for pdf in result}), 2)

    # ファイル・ディレクトリのシンボリックリンク除外
    def test_symlinked_pdfs_and_directories_are_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = root / "documents"
            linked_directory = documents / "linked-dir"
            linked_directory.mkdir(parents=True)
            (documents / "linked.pdf").write_bytes(b"link target placeholder")
            (linked_directory / "hidden.pdf").write_bytes(b"link target placeholder")

            def is_symlink(path):
                return path.name in {"linked.pdf", "linked-dir"}

            with patch.object(type(documents), "is_symlink", autospec=True, side_effect=is_symlink):
                result = LocalFolderSource(documents, root).list_documents()

        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()