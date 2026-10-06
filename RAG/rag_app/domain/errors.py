"""利用者向けメッセージへ変換するアプリケーション例外

例外階層:
    RagApplicationError
    ├─ DependencyUnavailable … Qdrant・Embedding・Ollamaなど外部依存の停止
    │   ├─ CollectionMismatchError
    │   └─ ModelUnavailable
    ├─ SyncStateError        … 同期管理DB（SQLite）の不正・保存失敗
    ├─ DocumentProcessingError … 1ファイル単位の取得・解析失敗
    ├─ QuestionValidationError … 質問入力の不正
    └─ GoogleDriveError      … Drive APIの失敗
        └─ GoogleAuthorizationRequired … ブラウザーでの許可が必要
"""


# アプリケーション境界で分類する例外階層
class RagApplicationError(Exception):
    """アプリケーション境界で変換する例外の基底型"""


class DependencyUnavailable(RagApplicationError):
    """必須のローカルモデルまたはデータサービスを利用できない状態"""


class CollectionMismatchError(DependencyUnavailable):
    """既存ベクトルコレクションと現在設定の不整合"""


class SyncStateError(RagApplicationError):
    """同期管理DB（旧索引マニフェストを含む）の不正または永続化失敗"""


class DocumentProcessingError(RagApplicationError):
    """PDFの安全な抽出または索引登録の失敗"""

    _MESSAGES = {
        "empty_pdf": "PDFにページがありません。",
        "no_text": "テキストを抽出できませんでした。画像PDFはOCRが必要です。",
        "encrypted_pdf": "暗号化されたPDFは登録できません。",
        "unreadable_pdf": "PDFを読み取れません。破損している可能性があります。",
        "document_changed": "索引処理中にPDFが変更されました。再実行してください。",
        "download_failed": "ファイルを取得できませんでした。権限と通信状態を確認してください。",
        "unreadable_file": "ファイルを読み込めません。",
    }

    # 同じ版のファイルを再処理しても結果が変わらない失敗（ファイル更新まで再試行しない）
    PERMANENT_CODES = frozenset({"empty_pdf", "no_text", "encrypted_pdf", "unreadable_pdf"})

    def __init__(self, code: str):
        self.code = code
        super().__init__(self.message_for(code))

    @classmethod
    def message_for(cls, code: str | None) -> str:
        # 失敗理由コードから利用者向けメッセージへの変換
        return cls._MESSAGES.get(code or "", "PDFの処理に失敗しました。")


class QuestionValidationError(RagApplicationError, ValueError):
    """API契約を満たさない質問入力"""


class ModelUnavailable(DependencyUnavailable):
    """Ollama接続中に設定済みモデルを利用できない状態"""


class GoogleDriveError(RagApplicationError):
    """Google Driveの認可・一覧取得・ダウンロードの失敗"""


class GoogleAuthorizationRequired(GoogleDriveError):
    """保存済みトークンが使えず、利用者のブラウザー操作による許可が必要な状態"""
