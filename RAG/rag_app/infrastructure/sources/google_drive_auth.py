"""Google DriveへのOAuth認可とアクセストークンの保存・再利用

バックグラウンド同期では interactive=False で呼び、ブラウザー操作が必要な場合は
勝手に画面を開かず GoogleAuthorizationRequired を返す（画面の「接続」操作で許可を始める）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rag_app.domain.errors import GoogleAuthorizationRequired, GoogleDriveError


# 索引に必要な読み取りだけを許可する最小権限のスコープ
DRIVE_READONLY_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def load_google_credentials(
    client_secrets_path: Path,
    token_path: Path,
    interactive: bool = True,
) -> Any:
    """保存済みトークンを再利用し、使えない場合だけブラウザーでOAuth認可を行う"""
    # Google関連ライブラリを必要時に読み込む遅延依存
    try:
        from google.auth.exceptions import RefreshError, TransportError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise GoogleDriveError(
            "Google Drive連携の依存関係がありません。requirements.txt をインストールしてください。"
        ) from exc

    credentials = None
    if token_path.exists():
        try:
            credentials = Credentials.from_authorized_user_file(str(token_path), DRIVE_READONLY_SCOPES)
        except ValueError:
            # 破損・形式不一致のトークンは再認可で置き換える
            credentials = None

    if credentials is not None and credentials.valid:
        return credentials

    # 期限切れアクセストークンのリフレッシュトークンによる更新
    if credentials is not None and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            _save_credentials(token_path, credentials)
            return credentials
        except RefreshError:
            # 取り消し・失効したリフレッシュトークンは再認可へ切り替える
            credentials = None
        except TransportError as exc:
            raise GoogleDriveError("Googleの認証サーバーに接続できません。") from exc

    if not client_secrets_path.is_file():
        # 画面にも表示されるため、絶対パスは含めず設定名で案内する
        raise GoogleDriveError(
            "OAuthクライアント情報が見つかりません。GOOGLE_CLIENT_SECRETS_PATH のファイルを配置してください。"
        )
    if not interactive:
        raise GoogleAuthorizationRequired("Google Driveへのアクセス許可が必要です。")

    # localhostで一時的に待ち受け、ブラウザーでの許可結果を受け取る
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), DRIVE_READONLY_SCOPES)
        credentials = flow.run_local_server(host="localhost", port=0, open_browser=True, timeout_seconds=300)
    except Exception as exc:
        raise GoogleDriveError(
            "Google Driveの認可に失敗しました。ブラウザーでアクセスを許可したか確認してください。"
        ) from exc

    _save_credentials(token_path, credentials)
    return credentials


def _save_credentials(token_path: Path, credentials: Any) -> None:
    # 次回以降に再認可を省略するためのトークン保存
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
