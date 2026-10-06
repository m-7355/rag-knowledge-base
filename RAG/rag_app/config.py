"""プロジェクトルートを基準にするアプリケーション設定"""

from __future__ import annotations

import os
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """設定値の欠落または不正を示す例外"""


# 起動後に変更しないアプリケーション設定のまとまり
@dataclass(frozen=True)
class AppSettings:
    project_root: Path
    app_host: str
    app_port: int
    documents_dir: Path
    qdrant_path: Path
    qdrant_url: str
    collection_name: str
    ollama_host: str
    ollama_model: str
    embedding_model: str
    chunk_size: int
    chunk_overlap: int
    # LLMへ渡す最終的な根拠チャンク数
    top_k: int
    min_relevance_score: float
    max_context_chars: int
    max_question_chars: int
    ollama_timeout_seconds: float
    # Hybrid検索で集めてRerankerへ渡す候補数
    retrieval_candidates: int = 20
    # 空なら再順位付けを行わない
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    # バックグラウンド同期の実行間隔
    sync_interval_seconds: int = 300


# Google Drive同期だけで使う設定のまとまり
@dataclass(frozen=True)
class DriveSettings:
    folder_id: str
    client_secrets_path: Path
    token_path: Path


# 環境変数の整数変換と入力エラーの統一
def _integer(values: Mapping[str, str], key: str, default: int) -> int:
    try:
        return int(values.get(key, str(default)))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{key} は整数で指定してください。") from exc


# 正の整数が必要な設定値の共通検証
def _positive_integer(values: Mapping[str, str], key: str, default: int) -> int:
    value = _integer(values, key, default)
    if value <= 0:
        raise ConfigurationError(f"{key} は1以上で指定してください。")
    return value


# 相対パスのプロジェクトルート基準への解決
def _resolve_path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def load_settings(project_root: Path | None = None) -> AppSettings:
    """カレントディレクトリに依存しない設定の読込"""
    root = (project_root or Path(__file__).resolve().parents[1]).resolve()
    # 起動場所に依存しないプロジェクトルート基準の.env読込
    load_dotenv(dotenv_path=root / ".env", override=False)
    values = os.environ

    app_host = values.get("APP_HOST", "127.0.0.1").strip()
    if app_host != "127.0.0.1":
        raise ConfigurationError("APP_HOST は安全のため 127.0.0.1 に固定してください。")

    app_port = _positive_integer(values, "APP_PORT", 5000)
    if app_port > 65535:
        raise ConfigurationError("APP_PORT は65535以下で指定してください。")

    documents_dir = _resolve_path(root, values.get("DOCUMENTS_DIR", "documents"))
    try:
        # PDF探索対象をプロジェクト配下に限定する境界検査
        documents_dir.relative_to(root)
    except ValueError as exc:
        raise ConfigurationError("DOCUMENTS_DIR はプロジェクトルート配下にしてください。") from exc

    qdrant_path = _resolve_path(root, values.get("QDRANT_PATH", "data/qdrant"))
    qdrant_url = values.get("QDRANT_URL", "").strip()
    if qdrant_url:
        # Qdrant接続先をHTTPまたはHTTPSに限定する形式検証
        parsed_url = urlparse(qdrant_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ConfigurationError("QDRANT_URL は有効な http(s) URL で指定してください。")

    collection_name = values.get("QDRANT_COLLECTION", "pdf_knowledge_v1").strip()
    ollama_host = values.get("OLLAMA_HOST", "http://127.0.0.1:11434").strip()
    ollama_url = urlparse(ollama_host)
    # Ollama接続先をHTTPまたはHTTPSに限定する形式検証
    if ollama_url.scheme not in {"http", "https"} or not ollama_url.netloc:
        raise ConfigurationError("OLLAMA_HOST は有効な http(s) URL で指定してください。")

    ollama_model = values.get("OLLAMA_MODEL", "gemma3:12b").strip()
    embedding_model = values.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-base").strip()
    if not collection_name or not ollama_model or not embedding_model:
        raise ConfigurationError("QDRANT_COLLECTION、OLLAMA_MODEL、EMBEDDING_MODEL は空にできません。")

    chunk_size = _positive_integer(values, "CHUNK_SIZE", 700)
    chunk_overlap = _integer(values, "CHUNK_OVERLAP", 100)
    # オーバーラップをチャンクサイズ未満に保つ整合性検査
    if not 0 <= chunk_overlap < chunk_size:
        raise ConfigurationError("CHUNK_OVERLAP は0以上かつ CHUNK_SIZE 未満にしてください。")

    try:
        min_relevance_score = float(values.get("MIN_RELEVANCE_SCORE", "0.45"))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("MIN_RELEVANCE_SCORE は数値で指定してください。") from exc
    if not -1.0 <= min_relevance_score <= 1.0:
        raise ConfigurationError("MIN_RELEVANCE_SCORE は-1.0から1.0の範囲で指定してください。")

    timeout_text = values.get("OLLAMA_TIMEOUT_SECONDS", "360")
    # 無限値と非正値を除外する推論待ち時間の検証
    try:
        timeout_seconds = float(timeout_text)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("OLLAMA_TIMEOUT_SECONDS は正の数で指定してください。") from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ConfigurationError("OLLAMA_TIMEOUT_SECONDS は正の数で指定してください。")

    top_k = _positive_integer(values, "TOP_K", 5)
    retrieval_candidates = _positive_integer(values, "RETRIEVAL_CANDIDATES", 20)
    # 再順位付けの入力は最終件数以上必要
    if retrieval_candidates < top_k:
        raise ConfigurationError("RETRIEVAL_CANDIDATES は TOP_K 以上にしてください。")

    sync_interval_seconds = _positive_integer(values, "SYNC_INTERVAL_SECONDS", 300)
    # Drive APIの利用上限を圧迫しないよう極端に短い間隔を拒否
    if sync_interval_seconds < 30:
        raise ConfigurationError("SYNC_INTERVAL_SECONDS は30以上で指定してください。")

    return AppSettings(
        project_root=root,
        app_host=app_host,
        app_port=app_port,
        documents_dir=documents_dir,
        qdrant_path=qdrant_path,
        qdrant_url=qdrant_url,
        collection_name=collection_name,
        ollama_host=ollama_host,
        ollama_model=ollama_model,
        embedding_model=embedding_model,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        top_k=top_k,
        min_relevance_score=min_relevance_score,
        max_context_chars=_positive_integer(values, "MAX_CONTEXT_CHARS", 10000),
        max_question_chars=_positive_integer(values, "MAX_QUESTION_CHARS", 2000),
        ollama_timeout_seconds=timeout_seconds,
        retrieval_candidates=retrieval_candidates,
        reranker_model=values.get("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3").strip(),
        sync_interval_seconds=sync_interval_seconds,
    )


def load_drive_settings(
    project_root: Path,
    folder_id: str | None = None,
    optional: bool = False,
) -> DriveSettings | None:
    """Drive同期用設定の読込（引数のフォルダーIDを.envより優先、optional時は未設定ならNone）"""
    root = project_root.resolve()
    load_dotenv(dotenv_path=root / ".env", override=False)
    values = os.environ

    folder = (folder_id or values.get("GOOGLE_DRIVE_FOLDER_ID", "")).strip()
    if not folder:
        if optional:
            return None
        raise ConfigurationError("GOOGLE_DRIVE_FOLDER_ID または --folder-id を指定してください。")
    # Drive APIの検索式へ埋め込むため、IDに使われる文字以外を拒否
    if not re.fullmatch(r"[A-Za-z0-9_-]+", folder):
        raise ConfigurationError("Google DriveのフォルダーIDの形式が正しくありません。")

    return DriveSettings(
        folder_id=folder,
        client_secrets_path=_resolve_path(
            root, values.get("GOOGLE_CLIENT_SECRETS_PATH", "secrets/google_client_secret.json")
        ),
        token_path=_resolve_path(root, values.get("GOOGLE_TOKEN_PATH", "secrets/google_token.json")),
    )
