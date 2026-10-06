"""PDFページ境界を越えない文字数ベースのテキスト分割"""

from __future__ import annotations

from langchain_text_splitters import RecursiveCharacterTextSplitter

from rag_app.domain.models import PageText, TextChunk, deterministic_point_id


# PDFページごとに検索用テキストを分割する処理
class PageAwareTextChunker:
    def __init__(self, chunk_size: int, chunk_overlap: int):
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            length_function=len,
            separators=["\n\n", "\n", "。", "！", "？", "．", "!", "?", "；", ";", "、", " ", ""],
            keep_separator=True,
            strip_whitespace=True,
        )

    def split(self, page_text: PageText) -> list[TextChunk]:
        # ページ本文のみを分割し、出典ページ情報を各チャンクへ維持
        text_parts = self._splitter.split_text(page_text.text)
        chunks: list[TextChunk] = []
        for chunk_index, text in enumerate(text_parts):
            cleaned = text.strip()
            if not cleaned:
                continue
            chunks.append(
                TextChunk(
                    point_id=deterministic_point_id(
                        page_text.source_id,
                        page_text.page_number,
                        chunk_index,
                        page_text.file_hash,
                        page_text.relative_path,
                    ),
                    source_id=page_text.source_id,
                    relative_path=page_text.relative_path,
                    file_name=page_text.file_name,
                    file_hash=page_text.file_hash,
                    page_number=page_text.page_number,
                    chunk_index=chunk_index,
                    text=cleaned,
                )
            )
        return chunks
