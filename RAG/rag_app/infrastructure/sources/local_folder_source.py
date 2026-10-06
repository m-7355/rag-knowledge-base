"""documents配下を再帰走査するローカルフォルダー同期元（シンボリックリンクは追跡しない）"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from rag_app.domain.models import DocumentRef, PdfFile

# ローカル文書のソースIDは相対パスのSHA-256（16進64文字）
_LOCAL_SOURCE_ID = re.compile(r"[0-9a-f]{64}")


# 対象ルート内のPDFだけを列挙する同期元
class LocalFolderSource:
    name = "ローカルフォルダー"

    def __init__(self, documents_root: Path, project_root: Path):
        self.documents_root = documents_root.resolve()
        self.project_root = project_root.resolve()
        # 直近の走査で見つけたソースIDと実ファイルの対応
        self._paths: dict[str, Path] = {}

    def owns(self, source_id: str) -> bool:
        return bool(_LOCAL_SOURCE_ID.fullmatch(source_id))

    def has_changes(self) -> bool:
        # ローカル走査は軽いため毎回走査し、ファイル単位の版で差分を判定する
        return True

    def list_documents(self) -> list[DocumentRef]:
        # 文書フォルダー未作成時の空結果
        if not self.documents_root.exists():
            return []

        documents: list[DocumentRef] = []
        paths: dict[str, Path] = {}
        # ディレクトリリンクを除外しながらの再帰走査
        for current, directories, file_names in os.walk(self.documents_root, followlinks=False):
            current_path = Path(current)
            directories[:] = [
                name for name in directories if not (current_path / name).is_symlink()
            ]
            for file_name in file_names:
                candidate = current_path / file_name
                if candidate.suffix.lower() != ".pdf" or candidate.is_symlink():
                    continue
                # プロジェクト外参照や消失ファイルの除外
                try:
                    resolved = candidate.resolve(strict=True)
                    relative_path = resolved.relative_to(self.project_root).as_posix()
                    stat = resolved.stat()
                except (OSError, ValueError):
                    continue
                if not resolved.is_file():
                    continue
                # 相対パスを基準とする安定したソース識別子
                source_id = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()
                paths[source_id] = resolved
                documents.append(
                    DocumentRef(
                        source_id=source_id,
                        relative_path=relative_path,
                        file_name=candidate.name,
                        # 更新時刻とサイズが同じなら内容も同じとみなし、ハッシュ計算を省く
                        revision=f"{stat.st_mtime_ns}:{stat.st_size}",
                    )
                )

        self._paths = paths
        return sorted(documents, key=lambda document: document.relative_path.casefold())

    def fetch(self, document: DocumentRef) -> PdfFile:
        # ローカルファイルはコピーせず、そのままのパスを渡す
        return PdfFile(
            source_id=document.source_id,
            relative_path=document.relative_path,
            file_name=document.file_name,
            absolute_path=self._paths[document.source_id],
        )

    def release(self, pdf: PdfFile) -> None:
        # 元ファイルを削除しないよう何もしない
        return None

    def commit_checkpoint(self) -> None:
        # 走査ごとに全件を比較するため保存するチェックポイントはない
        return None
