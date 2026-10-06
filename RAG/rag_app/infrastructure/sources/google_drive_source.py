"""Google Driveのフォルダー階層を同期元として扱うアダプター

差分同期の流れ:
    1. has_changes()       前回保存した変更トークンでChanges APIを確認し、変更がなければ走査を省略
    2. list_documents()    フォルダー階層を再帰走査し、メタデータ（版=md5/更新日時）だけを取得
    3. fetch()             版が変わった文書だけを一時ファイルへダウンロード
    4. commit_checkpoint() 同期完了後に、走査前に取得した変更トークンを保存
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Iterator

from rag_app.domain.errors import DocumentProcessingError, GoogleDriveError
from rag_app.domain.models import DocumentRef, PdfFile
from rag_app.ports.interfaces import SyncStateRepository


# ローカルPDFのソースIDと区別するDrive由来の接頭辞
DRIVE_SOURCE_PREFIX = "gdrive:"

_FOLDER_MIME = "application/vnd.google-apps.folder"
_PDF_MIME = "application/pdf"
# Google形式の文書はPDFへ書き出し、既存のPDF抽出処理をそのまま使う
_EXPORT_AS_PDF_MIMES = {
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.presentation",
}
_LIST_FIELDS = "nextPageToken, incompleteSearch, files(id, name, mimeType, modifiedTime, md5Checksum)"
_CHANGES_FIELDS = "nextPageToken, newStartPageToken, changes(fileId)"
_FILE_ID = re.compile(r"[A-Za-z0-9_-]+")

logger = logging.getLogger(__name__)


# Drive同期が管理するソースIDかどうかの判定
def is_drive_source(source_id: str) -> bool:
    return source_id.startswith(DRIVE_SOURCE_PREFIX)


# 出典からGoogle Drive上の元ファイルを開くためのURL（Drive以外はNone）
def drive_file_url(source_id: str) -> str | None:
    if not is_drive_source(source_id):
        return None
    file_id = source_id[len(DRIVE_SOURCE_PREFIX) :]
    if not _FILE_ID.fullmatch(file_id):
        return None
    return f"https://drive.google.com/file/d/{file_id}/view"


# 表示用パスの階層を壊すパス区切り・制御文字の置換
def _safe_name(name: str) -> str:
    cleaned = "".join("_" if char in "/\\" or not char.isprintable() else char for char in name)
    return cleaned.strip() or "(無題)"


# 指定フォルダー配下のPDF・Googleドキュメントを差分同期する同期元
class GoogleDriveSource:
    name = "Google Drive"

    def __init__(
        self,
        credentials: Any,
        folder_id: str,
        checkpoints: SyncStateRepository,
        service: Any | None = None,
    ):
        self._credentials = credentials
        self._folder_id = folder_id
        self._checkpoints = checkpoints
        self._service = service
        # 変更トークンはフォルダーごとに保存し、対象フォルダー変更時に混ざらないようにする
        self._checkpoint_key = f"gdrive:{folder_id}:changes_page_token"
        self._pending_token: str | None = None
        # 直近の走査で得たDriveのファイル情報（fetch時にMIME形式を参照）
        self._items: dict[str, dict] = {}

    def owns(self, source_id: str) -> bool:
        return is_drive_source(source_id)

    def has_changes(self) -> bool:
        saved_token = self._checkpoints.get_meta(self._checkpoint_key)
        if saved_token is None:
            # 初回は全件同期
            return True
        try:
            service = self._get_service()
            page_token = saved_token
            while True:
                response = self._execute(
                    service.changes().list(
                        pageToken=page_token,
                        pageSize=100,
                        fields=_CHANGES_FIELDS,
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                    )
                )
                # 対象フォルダー外の変更も含むが、安全側に倒して走査する
                if response.get("changes"):
                    return True
                if response.get("newStartPageToken"):
                    return False
                page_token = response.get("nextPageToken")
                if not page_token:
                    return True
        except GoogleDriveError:
            # トークン失効などで判定できない場合は全件走査へ切り替える
            logger.warning("Google Driveの変更確認に失敗したため全件を走査します。", exc_info=True)
            return True

    def list_documents(self) -> list[DocumentRef]:
        service = self._get_service()
        # 走査の前にトークンを取得し、走査中に起きた変更を次回の同期で拾えるようにする
        start = self._execute(service.changes().getStartPageToken(supportsAllDrives=True))
        self._pending_token = start.get("startPageToken")

        items: dict[str, dict] = {}
        documents: list[DocumentRef] = []
        for item, relative_path in self._walk(service):
            source_id = f"{DRIVE_SOURCE_PREFIX}{item['id']}"
            items[source_id] = item
            modified_time = str(item.get("modifiedTime", ""))
            documents.append(
                DocumentRef(
                    source_id=source_id,
                    relative_path=relative_path,
                    file_name=_safe_name(item["name"]),
                    # バイナリPDFは内容のMD5、Google形式は更新日時を版として使う
                    revision=str(item.get("md5Checksum") or modified_time),
                    modified_time=modified_time,
                )
            )
        self._items = items
        return documents

    def fetch(self, document: DocumentRef) -> PdfFile:
        # API由来の名前を使わない一時ファイルへ保存（索引後にreleaseで削除）
        with tempfile.NamedTemporaryFile(prefix="rag_gdrive_", suffix=".pdf", delete=False) as handle:
            destination = Path(handle.name)
        try:
            self._download(self._get_service(), self._items[document.source_id], destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return PdfFile(
            source_id=document.source_id,
            relative_path=document.relative_path,
            file_name=document.file_name,
            absolute_path=destination,
        )

    def release(self, pdf: PdfFile) -> None:
        try:
            pdf.absolute_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("一時ファイルを削除できませんでした。")

    def commit_checkpoint(self) -> None:
        if self._pending_token:
            self._checkpoints.set_meta(self._checkpoint_key, self._pending_token)
            self._pending_token = None

    def _get_service(self) -> Any:
        # Google APIクライアントを初回利用時にだけ組み立てる
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _build_service(self) -> Any:
        # Google APIクライアントを必要時に読み込む遅延依存
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise GoogleDriveError(
                "google-api-python-client がインストールされていません。requirements.txt をインストールしてください。"
            ) from exc
        return build("drive", "v3", credentials=self._credentials, cache_discovery=False)

    def _walk(self, service: Any) -> list[tuple[dict, str]]:
        # 起点がフォルダーであることの確認
        root = self._execute(
            service.files().get(fileId=self._folder_id, fields="id, name, mimeType", supportsAllDrives=True)
        )
        if root.get("mimeType") != _FOLDER_MIME:
            raise GoogleDriveError("指定したIDは Google Drive のフォルダーではありません。")

        targets: list[tuple[dict, str]] = []
        pending = [(root["id"], f"gdrive/{_safe_name(root['name'])}")]
        # 複数の親を持つフォルダーを二重に辿らないための記録
        visited = {root["id"]}
        # フォルダー → サブフォルダー → ファイルの順に辿る深さ優先走査
        while pending:
            folder_id, folder_path = pending.pop()
            for item in self._list_children(service, folder_id):
                item_path = f"{folder_path}/{_safe_name(item['name'])}"
                if item["mimeType"] == _FOLDER_MIME:
                    if item["id"] not in visited:
                        visited.add(item["id"])
                        pending.append((item["id"], item_path))
                elif item["mimeType"] == _PDF_MIME or item["mimeType"] in _EXPORT_AS_PDF_MIMES:
                    targets.append((item, item_path))
        return sorted(targets, key=lambda target: target[1].casefold())

    def _list_children(self, service: Any, folder_id: str) -> Iterator[dict]:
        # ページ単位で返される直下のファイル・フォルダー一覧の取得
        page_token = None
        while True:
            response = self._execute(
                service.files().list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields=_LIST_FIELDS,
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
            )
            # 欠けた一覧で同期すると、未取得分の索引を削除済みと誤判定する
            if response.get("incompleteSearch"):
                raise GoogleDriveError("Google Driveの一覧取得が不完全でした。時間をおいて再実行してください。")
            yield from response.get("files", [])
            page_token = response.get("nextPageToken")
            if not page_token:
                return

    def _download(self, service: Any, item: dict, destination: Path) -> None:
        # PDFはそのまま、Googleドキュメント・スライドはPDFへ書き出して取得
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseDownload

        if item["mimeType"] == _PDF_MIME:
            request = service.files().get_media(fileId=item["id"], supportsAllDrives=True)
        else:
            request = service.files().export_media(fileId=item["id"], mimeType=_PDF_MIME)

        try:
            with destination.open("wb") as file:
                downloader = MediaIoBaseDownload(file, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk(num_retries=3)
        except (HttpError, OSError) as exc:
            # 取得できなかったファイルは索引処理側でファイル単位の失敗として報告
            logger.warning("Google Driveからファイルを取得できませんでした: %s", _safe_name(item["name"]))
            raise DocumentProcessingError("download_failed") from exc

    @staticmethod
    def _execute(request: Any) -> dict:
        # 一時的な通信エラーを再試行し、HTTPエラーを利用者向けの例外へ変換
        from googleapiclient.errors import HttpError

        try:
            return request.execute(num_retries=3)
        except HttpError as exc:
            raise GoogleDriveError(
                f"Google Drive APIの呼び出しに失敗しました（HTTP {exc.resp.status}）。"
                "フォルダーIDとアクセス権を確認してください。"
            ) from exc
