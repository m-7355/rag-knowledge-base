"""注入されたアプリケーションサービスからFlaskを組み立てる生成処理"""

from flask import Flask

from rag_app.web.routes import register_routes


def create_app(
    answer_service,
    vector_store,
    chat_model,
    collection_name: str,
    max_question_chars: int,
    sync_worker=None,
):
    # 依存サービスをルートへ渡すFlaskアプリケーションの組立（sync_worker未指定時は同期APIを無効化）
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config["MAX_QUESTION_CHARS"] = max_question_chars
    app.config["SYNC_ENABLED"] = sync_worker is not None
    register_routes(app, answer_service, vector_store, chat_model, collection_name, sync_worker)
    return app
