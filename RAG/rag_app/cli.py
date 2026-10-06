"""運用・保守用のコマンドライン

通常はWebアプリ内の同期Workerが自動で同期するため、利用者が実行する必要はない。
障害調査や全件再構築など、管理者が明示的に同期したい場合に使う。

    python -m rag_app.cli ingest                   documents/ を今すぐ同期
    python -m rag_app.cli drive-sync [--folder-id] Google Driveを今すぐ同期（必要ならブラウザーで許可）
    python -m rag_app.cli rebuild-index --confirm  検索索引と同期状態を全削除して再構築

Qdrant Localは1プロセスしか開けないため、Webアプリを停止してから実行する。
"""

from __future__ import annotations

import argparse
import sys

from rag_app.bootstrap import ApplicationContainer, build_container
from rag_app.config import ConfigurationError, load_drive_settings, load_settings
from rag_app.domain.errors import RagApplicationError
from rag_app.domain.models import IngestionReport


def _parser() -> argparse.ArgumentParser:
    # 同期コマンドと破壊的操作の確認フラグ
    parser = argparse.ArgumentParser(prog="python -m rag_app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("ingest", help="documents/ 内のPDFを今すぐ同期します")
    rebuild = subparsers.add_parser("rebuild-index", help="検索索引と同期状態を破棄して再構築します")
    rebuild.add_argument(
        "--confirm",
        action="store_true",
        help="既存のQdrantコレクションと同期状態を削除することを確認します",
    )
    drive_sync = subparsers.add_parser("drive-sync", help="Google Driveのフォルダー配下を今すぐ同期します")
    drive_sync.add_argument("--folder-id", help="同期するフォルダーID（未指定時は GOOGLE_DRIVE_FOLDER_ID）")
    return parser


def _print_report(report: IngestionReport) -> None:
    # ファイル単位の結果と全体件数の表示
    for item in report.items:
        if item.status == "indexed":
            detail = f"{item.chunk_count}チャンク"
        elif item.status == "unchanged":
            detail = "変更なし"
        elif item.status == "removed":
            detail = "旧ポイントを削除"
        else:
            detail = item.message or "失敗"
        print(f"[{item.status}] {item.relative_path}: {detail}")
    print(
        "結果: "
        f"登録 {report.indexed} / スキップ {report.skipped} / "
        f"削除 {report.removed} / 失敗 {report.failed}"
    )


def _open_container(validate_collection: bool) -> ApplicationContainer | None:
    # 設定・モデル・保存先を初期化し、失敗時は利用者向けの理由だけを表示
    try:
        return build_container(load_settings(), validate_collection=validate_collection)
    except (ConfigurationError, RagApplicationError) as exc:
        print(f"起動できません: {exc}", file=sys.stderr)
    except Exception:
        print(
            "起動できません。Python依存関係、Embeddingモデル、Qdrant設定を確認してください。",
            file=sys.stderr,
        )
    return None


def main(argv: list[str] | None = None) -> int:
    # 引数検証から同期処理までのCLI制御
    parser = _parser()
    args = parser.parse_args(argv)
    # 既存索引の削除を伴う再構築の事前確認
    if args.command == "rebuild-index" and not args.confirm:
        parser.error("rebuild-index には既存索引を削除する --confirm が必要です。")
    if args.command == "drive-sync":
        # 重いモデル読込の前にDrive設定を検証し、設定漏れを早く返す
        try:
            load_drive_settings(load_settings().project_root, args.folder_id)
        except ConfigurationError as exc:
            print(f"起動できません: {exc}", file=sys.stderr)
            return 2

    container = _open_container(validate_collection=args.command != "rebuild-index")
    if container is None:
        return 2

    try:
        if args.command == "drive-sync":
            # 保存済みトークンが使えなければブラウザーを開いて許可を求める
            source = container.build_drive_source(interactive=True, folder_id=args.folder_id)
        else:
            if args.command == "rebuild-index":
                # 確認済みの全件削除（Driveの変更トークンも初期化し、次回は全件同期になる）
                container.vector_store.rebuild_collection(
                    container.embedder.dimension(), container.settings.embedding_model
                )
                container.keyword_index.clear()
                container.sync_state.clear()
            source = container.local_source

        # CLIでは変更検知を省略せず、必ず全件の版を確認する
        report = container.ingestion_service.sync_source(source, force=True)
        _print_report(report)
        if args.command == "rebuild-index" and container.drive_settings is not None:
            print("Google Driveの文書は、次回Webアプリ起動時に全件同期されます。")
        return 1 if report.failed else 0
    except (ConfigurationError, RagApplicationError) as exc:
        print(f"同期に失敗しました: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("同期に失敗しました。依存サービスと保存先を確認してください。", file=sys.stderr)
        return 1
    finally:
        # CLI終了時のQdrant・SQLite接続解放
        container.close()


if __name__ == "__main__":
    raise SystemExit(main())
