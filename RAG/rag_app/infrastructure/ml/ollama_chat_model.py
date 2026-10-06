"""タイムアウト設定と安全なエラー変換を備えたOllamaアダプター"""

from __future__ import annotations

from typing import Sequence

from rag_app.domain.errors import DependencyUnavailable, ModelUnavailable


# Ollamaの回答生成と稼働確認を担うチャットモデル
class OllamaChatModel:
    def __init__(self, host: str, model_name: str, timeout_seconds: float, client=None):
        self.model_name = model_name
        if client is None:
            try:
                from ollama import Client

                client = Client(host=host, timeout=timeout_seconds)
            except Exception as exc:
                raise DependencyUnavailable("Ollamaクライアントを初期化できません。") from exc
        self._client = client

    def generate(self, messages: Sequence[dict[str, str]]) -> str:
        # モデル不在と接続失敗を分けた利用者向けエラー変換
        try:
            response = self._client.chat(
                model=self.model_name,
                messages=list(messages),
                stream=False,
                options={"temperature": 0.1},
            )
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                raise ModelUnavailable(
                    "Ollamaモデルがありません。ollama pull で取得してください。"
                ) from exc
            raise DependencyUnavailable(
                "Ollamaに接続できません。Ollamaの起動とモデル取得を確認してください。"
            ) from exc

        message = getattr(response, "message", None)
        content = getattr(message, "content", "") if message is not None else ""
        if not isinstance(content, str) or not content.strip():
            raise DependencyUnavailable("Ollamaから空の回答が返されました。")
        return content.strip()

    def health(self) -> str:
        # 設定済みモデルの一覧存在確認
        try:
            response = self._client.list()
            models = getattr(response, "models", [])
            names = {
                getattr(model, "model", None) or getattr(model, "name", None)
                for model in models
            }
            return "ok" if self.model_name in names else "model_missing"
        except Exception:
            return "unavailable"
