"""PDFiumによるページ単位のテキスト抽出"""

from __future__ import annotations

from rag_app.domain.errors import DocumentProcessingError
from rag_app.domain.models import PageText, PdfFile


# PDFページ番号と抽出テキストを対応付けるアダプター
class PdfiumTextExtractor:
    def extract(self, pdf: PdfFile, file_hash: str) -> list[PageText]:
        # PDFiumを必要時に読み込む遅延依存
        try:
            import pypdfium2 as pdfium
        except ImportError as exc:
            raise RuntimeError("pypdfium2 がインストールされていません。") from exc

        try:
            document = pdfium.PdfDocument(str(pdf.absolute_path))
        except Exception as exc:
            if self._looks_encrypted(str(exc)):
                raise DocumentProcessingError("encrypted_pdf") from exc
            raise DocumentProcessingError("unreadable_pdf") from exc

        pages: list[PageText] = []
        try:
            page_count = len(document)
            if page_count == 0:
                raise DocumentProcessingError("empty_pdf")

            for page_index in range(page_count):
                page = None
                text_page = None
                try:
                    page = document[page_index]
                    text_page = page.get_textpage()
                    text = text_page.get_text_range().strip()
                    if text:
                        pages.append(
                            PageText(
                                source_id=pdf.source_id,
                                relative_path=pdf.relative_path,
                                file_name=pdf.file_name,
                                file_hash=file_hash,
                                page_number=page_index + 1,
                                text=text,
                            )
                        )
                except DocumentProcessingError:
                    raise
                except Exception as exc:
                    if self._looks_encrypted(str(exc)):
                        raise DocumentProcessingError("encrypted_pdf") from exc
                    raise DocumentProcessingError("unreadable_pdf") from exc
                finally:
                    if text_page is not None:
                        text_page.close()
                    if page is not None:
                        page.close()
        finally:
            # 成功・失敗にかかわらずPDFドキュメントを解放
            document.close()

        if not pages:
            # OCR対象となる画像PDFとテキスト抽出失敗の区別
            raise DocumentProcessingError("no_text")
        return pages

    @staticmethod
    def _looks_encrypted(message: str) -> bool:
        # PDFiumの例外文から暗号化状態を判定
        lowered = message.casefold()
        return any(word in lowered for word in ("password", "encrypted", "encryption"))
