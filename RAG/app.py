"""ローカル専用Flaskアプリケーションの起動入口

起動 → 画面表示と同時に同期Workerを開始 → 質問は同期と独立して処理する。
利用者が索引コマンドを実行する必要はない。
"""

import logging
import sys

from rag_app.bootstrap import build_container
from rag_app.config import ConfigurationError, load_settings
from rag_app.domain.errors import RagApplicationError
from rag_app.web.app_factory import create_app


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # 起動設定エラーの利用者向け通知と終了コード
    try:
        settings = load_settings()
        container = build_container(settings)
    except (ConfigurationError, RagApplicationError) as exc:
        print(f"起動できません: {exc}", file=sys.stderr)
        return 2

    # 質問処理（Webリクエスト）とは別スレッドで同期を常時実行
    sync_worker = container.build_sync_worker()
    app = create_app(
        container.answer_service,
        container.vector_store,
        container.chat_model,
        settings.collection_name,
        settings.max_question_chars,
        sync_worker=sync_worker,
    )
    sync_worker.start()
    try:
        # 回答生成中でも同期状態の取得に応答できるようスレッド処理を有効化
        app.run(
            host="127.0.0.1",
            port=settings.app_port,
            debug=False,
            use_reloader=False,
            threaded=True,
        )
    finally:
        # 同期Workerを止めてからQdrant・SQLite接続を解放
        sync_worker.stop()
        container.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
