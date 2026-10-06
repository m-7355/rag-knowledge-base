"""CLIとWebの依存関係を組み立てる構成ルート（Composition Root）

具体的なアダプター（Qdrant・SQLite・Google API・Ollamaなど）の生成はここだけで行い、
アプリケーション層にはポート（ports/interfaces.py）越しに渡す。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from rag_app.application.query.answer_service import RagAnswerService
from rag_app.application.query.hybrid_retriever import HybridRetriever
from rag_app.application.query.prompt_builder import PromptBuilder
from rag_app.application.query.relevance_policy import RelevancePolicy
from rag_app.application.sync.ingestion_service import DocumentIngestionService
from rag_app.application.sync.sync_worker import SyncWorker
from rag_app.config import (
    AppSettings,
    ConfigurationError,
    DriveSettings,
    load_drive_settings,
    load_settings,
)
from rag_app.infrastructure.ml.cross_encoder_reranker import CrossEncoderReranker
from rag_app.infrastructure.ml.e5_embedder import SentenceTransformerE5Embedder
from rag_app.infrastructure.ml.ollama_chat_model import OllamaChatModel
from rag_app.infrastructure.parsing.page_aware_chunker import PageAwareTextChunker
from rag_app.infrastructure.parsing.pdfium_extractor import PdfiumTextExtractor
from rag_app.infrastructure.sources.google_drive_source import GoogleDriveSource, drive_file_url
from rag_app.infrastructure.sources.local_folder_source import LocalFolderSource
from rag_app.infrastructure.storage.qdrant_vector_store import QdrantVectorStore
from rag_app.infrastructure.storage.sqlite_keyword_index import SqliteKeywordIndex
from rag_app.infrastructure.storage.sqlite_sync_state_repository import SqliteSyncStateRepository


# アプリケーション依存関係のライフサイクル管理
@dataclass
class ApplicationContainer:
    settings: AppSettings
    # GOOGLE_DRIVE_FOLDER_ID 未設定時はNone（Drive同期を行わない）
    drive_settings: DriveSettings | None
    embedder: SentenceTransformerE5Embedder
    vector_store: QdrantVectorStore
    sync_state: SqliteSyncStateRepository
    keyword_index: SqliteKeywordIndex
    chat_model: OllamaChatModel
    ingestion_service: DocumentIngestionService
    answer_service: RagAnswerService
    local_source: LocalFolderSource

    def build_drive_source(self, interactive: bool, folder_id: str | None = None) -> GoogleDriveSource:
        # OAuth認可（interactive=Falseなら保存済みトークンのみ）を行い、Drive同期元を作る
        from rag_app.infrastructure.sources.google_drive_auth import load_google_credentials

        drive_settings = (
            self.drive_settings
            if folder_id is None
            else load_drive_settings(self.settings.project_root, folder_id)
        )
        if drive_settings is None:
            raise ConfigurationError("GOOGLE_DRIVE_FOLDER_ID または --folder-id を指定してください。")
        credentials = load_google_credentials(
            drive_settings.client_secrets_path,
            drive_settings.token_path,
            interactive=interactive,
        )
        return GoogleDriveSource(credentials, drive_settings.folder_id, self.sync_state)

    def build_sync_worker(self) -> SyncWorker:
        # ローカルフォルダーは常に、Google Driveは設定されている場合だけ同期対象にする
        drive_factory: Callable[[bool], GoogleDriveSource] | None = None
        if self.drive_settings is not None:
            drive_factory = lambda interactive: self.build_drive_source(interactive)  # noqa: E731
        return SyncWorker(
            self.ingestion_service,
            self.sync_state,
            self.local_source,
            drive_factory,
            self.settings.sync_interval_seconds,
        )

    def close(self) -> None:
        # Qdrant Localのロックと SQLite接続の明示的な解放
        self.vector_store.close()
        self.keyword_index.close()
        self.sync_state.close()


def build_container(
    settings: AppSettings | None = None,
    validate_collection: bool = True,
) -> ApplicationContainer:
    # 設定をもとにアプリケーション層と各種アダプターを構成
    settings = settings or load_settings()
    drive_settings = load_drive_settings(settings.project_root, optional=True)
    data_dir = settings.project_root / "data"
    settings.documents_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    # 途中で失敗した場合に、開いた接続だけを逆順に閉じるための記録
    closers: list[Callable[[], None]] = []
    try:
        # 同期管理DB（旧JSONマニフェストがあれば初回だけ取り込み、既存索引を引き継ぐ）
        sync_state = SqliteSyncStateRepository(data_dir / "sync_state.sqlite3")
        closers.append(sync_state.close)
        sync_state.import_legacy_manifest(data_dir / "index_manifest.json")

        keyword_index = SqliteKeywordIndex(data_dir / "keyword_index.sqlite3")
        closers.append(keyword_index.close)

        embedder = SentenceTransformerE5Embedder(settings.embedding_model)
        vector_store = QdrantVectorStore(settings)
        closers.append(vector_store.close)
        if validate_collection:
            vector_store.ensure_collection(embedder.dimension(), settings.embedding_model)

        reranker = CrossEncoderReranker(settings.reranker_model) if settings.reranker_model else None
        chat_model = OllamaChatModel(
            settings.ollama_host,
            settings.ollama_model,
            settings.ollama_timeout_seconds,
        )

        # Sync側: 取得 → Parse → Chunk → Embedding → Qdrant／キーワード索引／同期管理DB
        ingestion_service = DocumentIngestionService(
            PdfiumTextExtractor(),
            PageAwareTextChunker(settings.chunk_size, settings.chunk_overlap),
            embedder,
            vector_store,
            sync_state,
            settings.embedding_model,
            keyword_index,
        )
        # Query側: Hybrid検索 → 状態照合 → しきい値 → Rerank → Context → LLM → 出典
        answer_service = RagAnswerService(
            HybridRetriever(embedder, vector_store, keyword_index, settings.retrieval_candidates),
            sync_state,
            RelevancePolicy(settings.min_relevance_score),
            PromptBuilder(),
            chat_model,
            settings.embedding_model,
            settings.top_k,
            settings.max_context_chars,
            settings.max_question_chars,
            reranker=reranker,
            source_url_resolver=drive_file_url,
        )
        return ApplicationContainer(
            settings=settings,
            drive_settings=drive_settings,
            embedder=embedder,
            vector_store=vector_store,
            sync_state=sync_state,
            keyword_index=keyword_index,
            chat_model=chat_model,
            ingestion_service=ingestion_service,
            answer_service=answer_service,
            local_source=LocalFolderSource(settings.documents_dir, settings.project_root),
        )
    except Exception:
        for close in reversed(closers):
            try:
                close()
            except Exception:
                pass
        raise
