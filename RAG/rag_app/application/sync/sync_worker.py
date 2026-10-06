"""バックグラウンド同期Worker

質問処理（Query）とは別スレッドで、同期元（ローカルフォルダー・Google Drive）を最新に保つ。
利用者に索引コマンドを実行させず、アプリ起動後に自動で同期する。

状態遷移:
    checking ─(変更あり)─▶ syncing ─▶ idle ─(一定間隔 / 画面からの同期要求)─▶ checking
        └──(変更なし)───────────────▶ idle
    Google Driveは保存済みトークンが使えない場合 auth_required になり、
    画面の「接続」操作で authorizing（ブラウザーでの許可）へ進む。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from rag_app.application.sync.ingestion_service import DocumentIngestionService
from rag_app.domain.errors import GoogleAuthorizationRequired, GoogleDriveError
from rag_app.ports.interfaces import DocumentSource, SyncStateRepository

logger = logging.getLogger(__name__)

# 引数interactive=Trueのときだけブラウザーでの許可を行い、Drive同期元を返すファクトリー
DriveSourceFactory = Callable[[bool], DocumentSource]

# 画面へ返す失敗一覧の上限（大量失敗時に応答が肥大化しないようにする）
_MAX_FAILURES = 50
_RUNNING_STATES = frozenset({"checking", "syncing", "authorizing"})


# 同期に失敗したファイルと理由
@dataclass(frozen=True)
class SyncFailure:
    path: str
    message: str


# 同期中の進捗（同期元ごとに 0/total から数え直す）
@dataclass(frozen=True)
class SyncProgress:
    source: str = ""
    done: int = 0
    total: int = 0
    current: str = ""


# 直近に完了した同期の集計
@dataclass(frozen=True)
class SyncSummary:
    indexed: int = 0
    unchanged: int = 0
    removed: int = 0
    failed: int = 0


# 画面とヘルスチェックへ返す同期状態のスナップショット
@dataclass(frozen=True)
class SyncStatus:
    state: str
    drive_state: str
    drive_message: str
    # 検索可能な文書がまだなく、同期を実行中（＝初回セットアップ中）か
    initial_sync: bool
    searchable: bool
    ready_documents: int
    progress: SyncProgress
    last_synced_at: str | None
    last_summary: SyncSummary | None
    failures: list[SyncFailure] = field(default_factory=list)
    message: str = ""


# 同期元を定期的に差分同期するバックグラウンドWorker
class SyncWorker:
    def __init__(
        self,
        ingestion: DocumentIngestionService,
        sync_state: SyncStateRepository,
        local_source: DocumentSource | None,
        drive_source_factory: DriveSourceFactory | None,
        interval_seconds: float,
    ):
        self._ingestion = ingestion
        self._sync_state = sync_state
        self._local_source = local_source
        self._drive_source_factory = drive_source_factory
        self._interval_seconds = interval_seconds

        # 状態はWorkerスレッドが書き、Webリクエストのスレッドが読むためロックで保護する
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._force_requested = False
        self._authorize_requested = False
        self._drive_source: DocumentSource | None = None

        # 起動直後から同期確認中として扱い、初回同期前の質問を受け付けない
        self._state = "checking"
        self._drive_state = "disabled" if drive_source_factory is None else "checking"
        self._drive_message = ""
        self._progress = SyncProgress()
        self._last_synced_at: str | None = None
        self._last_summary: SyncSummary | None = None
        self._failures: list[SyncFailure] = []
        self._message = ""

    # ---- 外部（Web・アプリ起動処理）から呼ぶ操作 ----

    def start(self) -> None:
        if self._thread is not None:
            return
        # アプリ終了時に同期の途中でもプロセスを止められるようデーモンスレッドにする
        self._thread = threading.Thread(target=self._run, name="sync-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stopping.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def request_sync(self) -> None:
        # 画面の「今すぐ同期」: 変更検知を省略して全件を確認する
        with self._lock:
            self._force_requested = True
        self._wake.set()

    def request_drive_authorization(self) -> None:
        # 画面の「Google Driveに接続」: 次の同期でブラウザーによる許可を行う
        with self._lock:
            self._authorize_requested = True
            self._force_requested = True
        self._wake.set()

    def status(self) -> SyncStatus:
        ready_documents = self._sync_state.count_ready()
        with self._lock:
            running = self._state in _RUNNING_STATES
            return SyncStatus(
                state=self._state,
                drive_state=self._drive_state,
                drive_message=self._drive_message,
                initial_sync=running and ready_documents == 0,
                searchable=ready_documents > 0,
                ready_documents=ready_documents,
                progress=self._progress,
                last_synced_at=self._last_synced_at,
                last_summary=self._last_summary,
                failures=list(self._failures),
                message=self._message,
            )

    # ---- 同期処理本体 ----

    def run_once(self, force: bool = False, authorize: bool = False) -> None:
        """全同期元を1回ずつ差分同期する（Workerスレッドとテストから呼ぶ）"""
        self._set(state="checking", message="", progress=SyncProgress())
        summary = {"indexed": 0, "unchanged": 0, "removed": 0, "failed": 0}
        failures: list[SyncFailure] = []
        errors: list[str] = []

        for source in self._sources(authorize):
            try:
                report = self._ingestion.sync_source(
                    source,
                    force=force,
                    on_progress=lambda done, total, current, name=source.name: self._on_progress(
                        name, done, total, current
                    ),
                )
            except GoogleDriveError as exc:
                # Drive一覧取得の失敗はDriveだけを失敗扱いにし、他の同期元は続行する
                self._drive_source = None
                self._set(drive_state="error", drive_message=str(exc))
                errors.append(f"{source.name}: {exc}")
                continue
            except Exception:
                logger.exception("%s の同期に失敗しました。", source.name)
                errors.append(f"{source.name}の同期に失敗しました。依存サービスの状態を確認してください。")
                continue

            for item in report.items:
                if item.status == "failed":
                    failures.append(SyncFailure(item.relative_path, item.message))
            summary["indexed"] += report.indexed
            summary["unchanged"] += report.skipped
            summary["removed"] += report.removed
            summary["failed"] += report.failed

        self._set(
            state="error" if errors else "idle",
            message=" / ".join(errors),
            progress=SyncProgress(),
            last_synced_at=datetime.now(timezone.utc).isoformat(),
            last_summary=SyncSummary(**summary),
            failures=failures[:_MAX_FAILURES],
        )

    def _run(self) -> None:
        # 起動直後に1回同期し、その後は一定間隔または同期要求で繰り返す
        while not self._stopping.is_set():
            with self._lock:
                force, authorize = self._force_requested, self._authorize_requested
                self._force_requested = self._authorize_requested = False
                self._wake.clear()
            try:
                self.run_once(force=force, authorize=authorize)
            except Exception:
                logger.exception("バックグラウンド同期で予期しないエラーが発生しました。")
                self._set(state="error", message="同期で問題が発生しました。ログを確認してください。")
            self._wake.wait(self._interval_seconds)

    def _sources(self, authorize: bool) -> list[DocumentSource]:
        sources: list[DocumentSource] = []
        if self._local_source is not None:
            sources.append(self._local_source)
        drive = self._resolve_drive_source(authorize)
        if drive is not None:
            sources.append(drive)
        return sources

    def _resolve_drive_source(self, authorize: bool) -> DocumentSource | None:
        # 接続済みの同期元は再利用し、未接続時だけ認可（必要ならブラウザー）を行う
        if self._drive_source_factory is None:
            return None
        if self._drive_source is not None and not authorize:
            return self._drive_source
        if authorize:
            self._set(state="authorizing", drive_state="authorizing", drive_message="")
        try:
            self._drive_source = self._drive_source_factory(authorize)
            self._set(drive_state="connected", drive_message="")
        except GoogleAuthorizationRequired as exc:
            self._drive_source = None
            self._set(drive_state="auth_required", drive_message=str(exc))
        except GoogleDriveError as exc:
            self._drive_source = None
            self._set(drive_state="error", drive_message=str(exc))
        except Exception:
            logger.exception("Google Driveへ接続できませんでした。")
            self._drive_source = None
            self._set(drive_state="error", drive_message="Google Driveへ接続できませんでした。")
        if authorize:
            self._set(state="checking")
        return self._drive_source

    def _on_progress(self, source: str, done: int, total: int, current: str) -> None:
        # 走査が始まった時点で checking から syncing へ切り替える
        self._set(state="syncing", progress=SyncProgress(source, done, total, current))

    def _set(self, **changes) -> None:
        with self._lock:
            for name, value in changes.items():
                setattr(self, f"_{name}", value)
