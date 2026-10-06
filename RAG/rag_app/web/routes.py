"""注入済みサービスへ処理を委譲するHTTPルート

    GET  /                  質問画面
    POST /api/chat          質問 → 根拠付き回答（初回同期中は503）
    GET  /api/health        Qdrant・Ollama・同期の稼働状況
    GET  /api/sync/status   同期の進捗・結果・失敗一覧
    POST /api/sync          今すぐ同期
    POST /api/drive/connect Google Driveの認可（ブラウザー）を開始
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import asdict

from flask import Flask, jsonify, render_template, request

from rag_app.domain.errors import DependencyUnavailable, QuestionValidationError

logger = logging.getLogger(__name__)

# localhost以外のホスト名でのアクセス（DNSリバインディング）を拒否する
_TRUSTED_HOSTS = ["127.0.0.1", "localhost"]


def register_routes(
    app: Flask,
    answer_service,
    vector_store,
    chat_model,
    collection_name: str,
    sync_worker=None,
):
    app.config["TRUSTED_HOSTS"] = _TRUSTED_HOSTS

    # 状態を変えるPOSTを、同一オリジンからのJSON送信だけに限定（CSRF対策）
    @app.before_request
    def protect_local_api():
        # TRUSTED_HOSTS外のHostヘッダーはここで400になる
        _ = request.host
        if request.method != "POST":
            return None
        origin = request.headers.get("Origin")
        if origin is not None and origin.rstrip("/") != request.host_url.rstrip("/"):
            return jsonify({"error": "許可されていない送信元です。"}), 403
        # フォーム送信などプリフライトを伴わないクロスサイト要求を拒否
        if not request.is_json:
            return jsonify({"error": "JSON形式で送信してください。"}), 415
        return None

    # 画面テンプレートと質問文字数上限の提供
    @app.get("/")
    def index():
        return render_template(
            "index.html",
            max_question_chars=app.config["MAX_QUESTION_CHARS"],
            sync_enabled=app.config["SYNC_ENABLED"],
        )

    # JSON入力検証と回答サービス呼出しを行うチャットAPI
    @app.post("/api/chat")
    def post_chat():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or "question" not in payload:
            return jsonify({"error": "JSON形式でquestionを指定してください。"}), 400
        question = payload["question"]
        if not isinstance(question, str):
            return jsonify({"error": "質問は文字列で指定してください。"}), 400
        normalized_question = question.strip()
        if not normalized_question:
            return jsonify({"error": "質問を入力してください。"}), 400
        max_chars = app.config["MAX_QUESTION_CHARS"]
        if len(normalized_question) > max_chars:
            return jsonify({"error": f"質問は{max_chars}文字以内で入力してください。"}), 400
        # 検索できる文書がまだない初回同期中は、空の索引で「根拠なし」と誤答しない
        if sync_worker is not None:
            sync_status = sync_worker.status()
            if sync_status.initial_sync:
                return jsonify({"error": "初回同期中です。完了すると質問できます。"}), 503
            if not sync_status.searchable:
                return jsonify({"error": "検索可能な資料がありません。同期元を接続して同期してください。"}), 503

        try:
            result = answer_service.answer(normalized_question)
            return jsonify(
                {
                    "answer": result.answer,
                    "abstained": result.abstained,
                    "sources": [asdict(source) for source in result.sources],
                }
            )
        except QuestionValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        except DependencyUnavailable:
            # 依存サービスの詳細を伏せた利用者向けエラー
            return (
                jsonify(
                    {
                        "error": "依存サービスを利用できません。Ollamaの起動、モデル取得、索引状態を確認してください。"
                    }
                ),
                503,
            )
        except Exception:
            # 内部例外の詳細を隠して追跡用IDだけを返す応答
            request_id = secrets.token_hex(8)
            logger.exception("Unexpected chat failure request_id=%s", request_id)
            return (
                jsonify(
                    {
                        "error": "回答処理で問題が発生しました。時間をおいて再試行してください。",
                        "request_id": request_id,
                    }
                ),
                500,
            )

    # 外部依存の稼働状況を個別に確認するヘルスAPI
    @app.get("/api/health")
    def get_health():
        try:
            qdrant_status = vector_store.health()
        except Exception:
            qdrant_status = "unavailable"
        try:
            ollama_status = chat_model.health()
        except Exception:
            ollama_status = "unavailable"
        healthy = qdrant_status == "ok" and ollama_status == "ok"
        return (
            jsonify(
                {
                    "status": "ok" if healthy else "unavailable",
                    "qdrant": qdrant_status,
                    "ollama": ollama_status,
                    "sync": sync_worker.status().state if sync_worker is not None else "disabled",
                    "collection": collection_name,
                }
            ),
            200 if healthy else 503,
        )

    # 同期Workerが渡された場合だけ同期用APIを公開
    if sync_worker is None:
        return

    # 画面の同期パネルが定期的に取得する同期状態
    @app.get("/api/sync/status")
    def get_sync_status():
        return jsonify(asdict(sync_worker.status()))

    # 「今すぐ同期」：同期はWorkerが非同期に行い、ここでは受付けだけ返す
    @app.post("/api/sync")
    def post_sync():
        sync_worker.request_sync()
        return jsonify({"accepted": True}), 202

    # 「Google Driveに接続」：WorkerがこのPCのブラウザーでOAuth認可画面を開く
    @app.post("/api/drive/connect")
    def post_drive_connect():
        if sync_worker.status().drive_state == "disabled":
            return jsonify({"error": "GOOGLE_DRIVE_FOLDER_ID が設定されていません。"}), 409
        sync_worker.request_drive_authorization()
        return jsonify({"accepted": True}), 202
